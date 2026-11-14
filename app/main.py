"""FastAPI application: REST API plus static SPA console."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import polars as pl
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import jobs, store
from app.config import (APP_NAME, APP_VERSION, DEFAULT_PARAMS, MAX_PAGE_SIZE,
                        PARAM_LABELS, RUNS_DIR)
from app.ingest import (KINDS, apply_mapping, dataset_path, detect_mapping,
                        load_dataset_frame, read_table,
                        save_dataset_frame)
from app.utils import df_columns, df_records, validate_params

app = FastAPI(title=APP_NAME, version=APP_VERSION)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"

DATASET_ORDER = ["taps", "gps", "stops", "truth"]


@app.on_event("startup")
def _startup() -> None:
    _init_once()
    store.mark_stale_runs_interrupted()
    jobs.start_worker()
    _ensure_demo_loaded()


def _ensure_demo_loaded() -> None:
    """First launch: ingest the bundled demo set so the UI is usable at once."""
    existing = {d["kind"] for d in store.list_datasets()}
    if {"taps", "gps", "stops"} <= existing:
        return
    demo = Path(__file__).resolve().parent.parent / "data" / "demo"
    files = {"taps": "afc_taps.csv", "gps": "avl_gps.csv",
             "stops": "route_stops.csv", "truth": "ground_truth.csv"}
    if not all((demo / f).exists() for f in files.values()):
        return
    for kind, name in files.items():
        if kind in existing:
            continue
        raw = (demo / name).read_bytes()
        try:
            df = read_table(raw, name)
            mapping = detect_mapping(df.columns, kind)
            parsed, stats = apply_mapping(df, mapping, kind)
        except Exception as exc:  # noqa: BLE001 - demo data is best effort
            print(f"[demo] skip {kind}: {exc}")
            continue
        ds = store.add_dataset(
            f"演示数据 · {KINDS[kind]['label']}", kind, name,
            parsed.height, len(parsed.columns), len(raw),
            {"demo": True, "stats": stats})
        save_dataset_frame(ds["id"], parsed)
        store.set_mapping(ds["id"], kind, mapping)


_init_lock = __import__("threading").Lock()
_initialised = False


def _init_once() -> None:
    """Idempotent initialisation, also invoked lazily by every request.

    TestClient in this project runs without a lifespan context, so relying on
    the startup event alone would leave the database schema uncreated.
    """
    global _initialised
    with _init_lock:
        if _initialised:
            return
        store.init_db()
        store.mark_stale_runs_interrupted()
        jobs.start_worker()
        _ensure_demo_loaded()
        _initialised = True


@app.middleware("http")
async def _ensure_init(request, call_next):
    _init_once()
    return await call_next(request)


# --------------------------------------------------------------------- meta
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "name": APP_NAME, "version": APP_VERSION,
            "queue": jobs.queue_depth()}


@app.get("/api/meta")
def meta() -> dict:
    return {
        "name": APP_NAME, "version": APP_VERSION,
        "kinds": KINDS,
        "default_params": DEFAULT_PARAMS,
        "param_labels": PARAM_LABELS,
        "dataset_order": DATASET_ORDER,
        "max_page_size": MAX_PAGE_SIZE,
    }


# ----------------------------------------------------------------- datasets
@app.get("/api/datasets")
def list_datasets() -> dict:
    items = store.list_datasets()
    return {"items": items, "by_kind": _datasets_by_kind(items)}


def _datasets_by_kind(items: list[dict]) -> dict:
    out: dict[str, list] = {k: [] for k in KINDS}
    for ds in items:
        out.setdefault(ds["kind"], []).append(ds)
    return out


@app.get("/api/datasets/{ds_id}")
def get_dataset(ds_id: str, limit: int = Query(20, ge=1, le=MAX_PAGE_SIZE),
                offset: int = Query(0, ge=0)) -> dict:
    ds = store.get_dataset(ds_id)
    if not ds:
        raise HTTPException(404, "数据集不存在")
    df = load_dataset_frame(ds_id)
    page = df.slice(offset, limit)
    return {
        "dataset": ds,
        "mapping": store.get_mapping(ds_id, ds["kind"]),
        "columns": df_columns(df),
        "rows": df_records(page),
        "total": df.height,
        "offset": offset,
        "limit": limit,
        "field_spec": KINDS[ds["kind"]]["fields"],
    }


@app.post("/api/datasets/upload")
async def upload_dataset(
    file: UploadFile = File(...),
    kind: str = Form(...),
    mapping_json: str | None = Form(None),
) -> dict:
    if kind not in KINDS:
        raise HTTPException(400, "未知数据类型")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    if len(raw) > 200 * 1024 * 1024:
        raise HTTPException(400, "文件超过 200MB 限制")
    try:
        df = read_table(raw, file.filename or "upload.csv")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"文件解析失败：{exc}") from exc
    if df.height == 0:
        raise HTTPException(400, "文件没有数据行")
    mapping = detect_mapping(df.columns, kind)
    if mapping_json:
        try:
            supplied = json.loads(mapping_json)
            mapping.update({k: v for k, v in supplied.items() if v})
        except json.JSONDecodeError:
            raise HTTPException(400, "字段映射 JSON 无效") from None
    try:
        parsed, stats = apply_mapping(df, mapping, kind)
    except ValueError as exc:
        # Keep the raw frame plus its source columns so the user can fix the
        # mapping interactively instead of re-uploading.
        ds = store.add_dataset(
            name=file.filename or "未命名", kind=kind, filename=file.filename or "",
            n_rows=df.height, n_cols=len(df.columns), size_bytes=len(raw),
            meta={"needs_mapping": True, "error": str(exc),
                  "source_columns": df.columns})
        save_dataset_frame(ds["id"], df)
        store.set_mapping(ds["id"], kind, mapping)
        raise HTTPException(422, {
            "message": str(exc), "dataset_id": ds["id"],
            "columns": df.columns, "mapping": mapping,
        }) from None
    ds = store.add_dataset(
        name=file.filename or "未命名", kind=kind, filename=file.filename or "",
        n_rows=parsed.height, n_cols=len(parsed.columns), size_bytes=len(raw),
        meta={"stats": stats, "source_columns": df.columns})
    save_dataset_frame(ds["id"], parsed)
    store.set_mapping(ds["id"], kind, mapping)
    return {"dataset": ds, "stats": stats, "mapping": mapping}


@app.post("/api/datasets/{ds_id}/mapping")
def update_mapping(ds_id: str, payload: dict = Body(...)) -> dict:
    ds = store.get_dataset(ds_id)
    if not ds:
        raise HTTPException(404, "数据集不存在")
    kind = ds["kind"]
    mapping = payload.get("mapping") or {}
    if not mapping:
        raise HTTPException(400, "缺少映射")
    meta = ds.get("meta") or {}
    if meta.get("needs_mapping"):
        # the stored frame is still the raw upload: normalise it now
        raw_df = load_dataset_frame(ds_id)
        try:
            parsed, stats = apply_mapping(raw_df, mapping, kind)
        except ValueError as exc:
            store.set_mapping(ds_id, kind, mapping)
            raise HTTPException(422, str(exc)) from None
        save_dataset_frame(ds_id, parsed)
        meta.pop("needs_mapping", None)
        meta.pop("error", None)
        meta["stats"] = stats
        with store._lock, store._conn() as conn:
            conn.execute("UPDATE datasets SET n_rows=?, n_cols=?, meta=? WHERE id=?",
                         (parsed.height, len(parsed.columns),
                          json.dumps(meta, ensure_ascii=False), ds_id))
        store.set_mapping(ds_id, kind, mapping)
        return {"ok": True, "mapping": mapping, "rows": parsed.height,
                "stats": stats, "note": "映射已应用"}
    store.set_mapping(ds_id, kind, mapping)
    return {"ok": True, "mapping": mapping, "note": "映射已保存"}


@app.delete("/api/datasets/{ds_id}")
def delete_dataset(ds_id: str) -> dict:
    ds = store.get_dataset(ds_id)
    if not ds:
        raise HTTPException(404, "数据集不存在")
    path = dataset_path(ds_id)
    if path.exists():
        path.unlink()
    store.delete_dataset(ds_id)
    return {"ok": True}


@app.get("/api/datasets/{ds_id}/columns")
def dataset_columns(ds_id: str) -> dict:
    ds = store.get_dataset(ds_id)
    if not ds:
        raise HTTPException(404, "数据集不存在")
    return {"columns": ds.get("meta", {}).get("source_columns") or [],
            "field_spec": KINDS[ds["kind"]]["fields"],
            "mapping": store.get_mapping(ds_id, ds["kind"]),
            "kind": ds["kind"]}


# --------------------------------------------------------------------- runs
@app.post("/api/runs")
def create_run(payload: dict = Body(...)) -> dict:
    """Launch an inference run.

    payload: {name, params, datasets: {taps, gps, stops, truth?}}
    """
    datasets = payload.get("datasets") or {}
    missing = [k for k in ("taps", "gps", "stops") if not datasets.get(k)]
    if missing:
        raise HTTPException(400, f"缺少数据集：{', '.join(missing)}")
    params = payload.get("params") or DEFAULT_PARAMS
    errs = validate_params(params)
    if errs:
        raise HTTPException(400, {"message": "参数不合法", "errors": errs})
    dataset_ids = [datasets.get(k) for k in DATASET_ORDER]
    name = payload.get("name") or f"推算 {store._now()[5:16].replace('T', ' ')}"
    run_id = store.new_id("run")
    run = jobs.submit(run_id, name, params, dataset_ids)
    return {"run": run}


@app.get("/api/runs")
def list_runs(limit: int = Query(50, ge=1, le=200)) -> dict:
    runs = store.list_runs()
    return {"items": runs[:limit], "total": len(runs)}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    out_dir = RUNS_DIR / run_id
    summary = None
    metrics_path = out_dir / "metrics.json"
    if metrics_path.exists():
        try:
            summary = json.loads(metrics_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = None
    return {"run": run, "has_results": (out_dir / "od_matrix.parquet").exists(),
            "metrics": summary}


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    shutil.rmtree(RUNS_DIR / run_id, ignore_errors=True)
    store.delete_run(run_id)
    return {"ok": True}


def _run_dir(run_id: str) -> Path:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    out = RUNS_DIR / run_id
    if not out.exists():
        raise HTTPException(409, "任务尚未生成结果")
    return out


def _load(run_id: str, name: str) -> pl.DataFrame | None:
    path = _run_dir(run_id) / f"{name}.parquet"
    if not path.exists():
        return None
    try:
        return pl.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None


def _table_response(df: pl.DataFrame, offset: int, limit: int,
                    sort: str | None, desc: bool,
                    filters: dict[str, str] | None = None) -> dict:
    if filters:
        for col, val in filters.items():
            if col in df.columns and val not in ("", "all", None):
                df = df.filter(pl.col(col).cast(pl.Utf8) == val)
    if sort and sort in df.columns:
        df = df.sort(sort, descending=desc, nulls_last=True)
    total = df.height
    page = df.slice(offset, limit)
    return {"columns": df_columns(df), "rows": df_records(page),
            "total": total, "offset": offset, "limit": limit}


# ------------------------------------------------------------------ results
@app.get("/api/runs/{run_id}/metrics")
def run_metrics(run_id: str) -> dict:
    out = _run_dir(run_id)
    metrics_path = out / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
    run = store.get_run(run_id)
    return {"metrics": metrics, "run": run}


@app.get("/api/runs/{run_id}/od")
def run_od(run_id: str, period: str | None = None, route: str | None = None,
           direction: int | None = None, min_passengers: int = 0,
           limit: int = Query(200, ge=1, le=MAX_PAGE_SIZE * 4),
           offset: int = Query(0, ge=0), sort: str = "passengers",
           desc: bool = True, aggregate: bool = False) -> dict:
    """OD matrix, optionally collapsed over period/route/direction.

    Without aggregation each row is one (period, route, direction) cell; the
    map switches to aggregated pairs so line weights reflect real volumes
    instead of being split across many small cells.
    """
    df = _load(run_id, "od_matrix")
    if df is None:
        raise HTTPException(409, "OD 结果不存在")
    if period and period not in ("all", "全部"):
        df = df.filter(pl.col("period") == period)
    if route and route not in ("all", ""):
        df = df.filter(pl.col("route_id") == route)
    if direction is not None and direction >= 0:
        df = df.filter(pl.col("direction") == direction)
    if aggregate and df.height:
        df = df.group_by(["board_stop_id", "alight_stop_id", "origin_name",
                          "dest_name"]).agg([
            pl.col("passengers").sum(),
            pl.col("travellers").sum(),
            pl.col("uncertain_share").mean().round(3),
            pl.col("board_lat").first(), pl.col("board_lon").first(),
            pl.col("alight_lat").first(), pl.col("alight_lon").first(),
            pl.col("avg_ride_sec").mean().alias("avg_ride_sec"),
            pl.col("avg_ride_dist_m").mean().alias("avg_ride_dist_m"),
            pl.lit("全部").alias("period"),
            pl.lit("全部").alias("route_id"),
            pl.lit(-1, dtype=pl.Int64).alias("direction"),
        ])
    if min_passengers:
        df = df.filter(pl.col("passengers") >= min_passengers)
    resp = _table_response(df, offset, limit, sort, desc)
    resp["summary"] = _od_summary(df)
    return resp


def _od_summary(df: pl.DataFrame) -> dict:
    if df.height == 0:
        return {"passengers": 0, "pairs": 0, "by_period": []}
    by_period = df.group_by("period").agg([
        pl.col("passengers").sum().alias("passengers"),
        pl.len().alias("pairs"),
    ]).sort("period").to_dicts()
    return {
        "passengers": int(df["passengers"].sum()),
        "pairs": df.height,
        "by_period": by_period,
        "top_pairs": df.group_by(["origin_name", "dest_name"]).agg(
            pl.col("passengers").sum().alias("passengers")
        ).sort("passengers", descending=True).head(10).to_dicts(),
    }


@app.get("/api/runs/{run_id}/legs")
def run_legs(run_id: str, card_id: str | None = None, uncertain: bool | None = None,
             method: str | None = None, period: str | None = None,
             route: str | None = None,
             limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
             offset: int = Query(0, ge=0), sort: str = "board_time",
             desc: bool = True) -> dict:
    df = _load(run_id, "legs")
    if df is None:
        raise HTTPException(409, "出行链结果不存在")
    if card_id:
        df = df.filter(pl.col("card_id") == card_id)
    if uncertain is not None:
        df = df.filter(pl.col("uncertain") == uncertain)
    if method and method not in ("all", ""):
        df = df.filter(pl.col("alight_method") == method)
    if period and period not in ("all", "全部"):
        df = df.filter(pl.col("period") == period)
    if route and route not in ("all", ""):
        df = df.filter(pl.col("route_id") == route)
    return _table_response(df, offset, limit, sort, desc)


@app.get("/api/runs/{run_id}/transfers")
def run_transfers(run_id: str, period: str | None = None,
                  limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
                  offset: int = Query(0, ge=0)) -> dict:
    df = _load(run_id, "legs")
    if df is None:
        raise HTTPException(409, "结果不存在")
    tf = df.filter(pl.col("is_transfer")).select([
        "card_id", "journey_id", "leg_index", "board_stop_id", "board_stop_name",
        "route_id", "board_time", "transfer_prev_stop", "transfer_walk_m",
        "transfer_wait_sec", "alight_stop_id", "alight_stop_name", "period",
        "uncertain",
    ])
    if period and period not in ("all", "全部"):
        tf = tf.filter(pl.col("period") == period)
    resp = _table_response(tf, offset, limit, "board_time", True)
    summary = {}
    if tf.height:
        summary = {
            "transfers": tf.height,
            "cards": int(tf["card_id"].n_unique()),
            "avg_wait_sec": round(float(tf["transfer_wait_sec"].mean() or 0), 1),
            "avg_walk_m": round(float(tf["transfer_walk_m"].mean() or 0), 1),
            "by_period": tf.group_by("period").agg(pl.len().alias("n")).sort("period").to_dicts(),
            "top_hubs": tf.group_by(["transfer_prev_stop", "board_stop_name"]).agg(
                pl.len().alias("n")).sort("n", descending=True).head(8).to_dicts(),
        }
    resp["summary"] = summary
    return resp


@app.get("/api/runs/{run_id}/flow")
def run_flow(run_id: str, period: str | None = None, route: str | None = None,
             direction: int | None = None,
             limit: int = Query(200, ge=1, le=MAX_PAGE_SIZE),
             offset: int = Query(0, ge=0), sort: str = "avg_load",
             desc: bool = True) -> dict:
    df = _load(run_id, "stop_flow")
    if df is None:
        raise HTTPException(409, "断面结果不存在")
    if period and period not in ("all", "全部"):
        df = df.filter(pl.col("period") == period)
    if route and route not in ("all", ""):
        df = df.filter(pl.col("route_id") == route)
    if direction is not None and direction >= 0:
        df = df.filter(pl.col("direction") == direction)
    resp = _table_response(df, offset, limit, sort, desc)
    summary = {}
    if df.height:
        summary = {
            "peak_load": int(df["peak_load"].max()),
            "avg_load": round(float(df["avg_load"].mean()), 1),
            "max_load_factor": round(float(df["peak_load_factor"].max()), 3),
            "over_capacity_trips": int((df["peak_load_factor"] > 1).sum()),
            "by_route": df.group_by("route_id").agg([
                pl.col("peak_load").max().alias("peak_load"),
                pl.col("avg_load").mean().alias("avg_load"),
                pl.col("peak_load_factor").max().alias("peak_load_factor"),
            ]).sort("peak_load", descending=True).to_dicts(),
        }
    resp["summary"] = summary
    return resp


@app.get("/api/runs/{run_id}/sections")
def run_sections(run_id: str, period: str | None = None, route: str | None = None,
                 direction: int | None = None, limit: int = Query(200, ge=1, le=MAX_PAGE_SIZE),
                 offset: int = Query(0, ge=0), sort: str = "load_factor",
                 desc: bool = True) -> dict:
    df = _load(run_id, "sections")
    if df is None:
        raise HTTPException(409, "断面结果不存在")
    if period and period not in ("all", "全部"):
        df = df.filter(pl.col("period") == period)
    if route and route not in ("all", ""):
        df = df.filter(pl.col("route_id") == route)
    if direction is not None and direction >= 0:
        df = df.filter(pl.col("direction") == direction)
    return _table_response(df, offset, limit, sort, desc)


@app.get("/api/runs/{run_id}/stop-board")
def run_stop_board(run_id: str, period: str | None = None,
                   limit: int = Query(300, ge=1, le=MAX_PAGE_SIZE),
                   offset: int = Query(0, ge=0), sort: str = "boardings",
                   desc: bool = True) -> dict:
    df = _load(run_id, "stop_board")
    if df is None:
        raise HTTPException(409, "站点客流结果不存在")
    if period and period not in ("all", "全部"):
        df = df.filter(pl.col("period") == period)
    return _table_response(df, offset, limit, sort, desc)


@app.get("/api/runs/{run_id}/taps")
def run_taps(run_id: str, status: str | None = None, route: str | None = None,
             limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
             offset: int = Query(0, ge=0), sort: str = "tap_time", desc: bool = True) -> dict:
    df = _load(run_id, "taps")
    if df is None:
        raise HTTPException(409, "匹配结果不存在")
    if status and status not in ("all", ""):
        df = df.filter(pl.col("status") == status)
    if route and route not in ("all", ""):
        df = df.filter(pl.col("route_id") == route)
    cols = ["tap_id", "card_id", "tap_time", "route_id", "vehicle_id", "status",
            "reason", "board_stop_id", "board_stop_name", "run_id", "run_pos",
            "match_method", "match_delta_sec", "match_delta_sec"]
    cols = list(dict.fromkeys([c for c in cols if c in df.columns]))
    return _table_response(df.select(cols), offset, limit, sort, desc)


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, route: str | None = None, vehicle: str | None = None,
               limit: int = Query(100, ge=1, le=MAX_PAGE_SIZE),
               offset: int = Query(0, ge=0), sort: str = "arr_time",
               desc: bool = False) -> dict:
    df = _load(run_id, "stop_events")
    if df is None:
        raise HTTPException(409, "停站事件不存在")
    if route and route not in ("all", ""):
        df = df.filter(pl.col("route_id") == route)
    if vehicle:
        df = df.filter(pl.col("vehicle_id") == vehicle)
    return _table_response(df, offset, limit, sort, desc)


@app.get("/api/runs/{run_id}/stops")
def run_stops(run_id: str) -> dict:
    """Stop catalogue with coordinates, used by the map."""
    df = _load(run_id, "stop_match")
    if df is None:
        raise HTTPException(409, "站点结果不存在")
    return {"items": df_records(df.limit(MAX_PAGE_SIZE))}


@app.get("/api/runs/{run_id}/filters")
def run_filters(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "任务不存在")
    result: dict[str, Any] = {"periods": [], "routes": [], "methods": []}
    if run["status"] != "succeeded":
        return result
    od = _load(run_id, "od_matrix")
    if od is not None and od.height:
        result["periods"] = sorted(od["period"].unique().to_list())
        result["routes"] = sorted(od["route_id"].unique().to_list())
    legs = _load(run_id, "legs")
    if legs is not None and legs.height:
        result["methods"] = sorted(legs["alight_method"].unique().to_list())
        result["directions"] = sorted(
            [int(x) for x in legs["direction"].drop_nulls().unique().to_list()])
    metrics = RUNS_DIR / run_id / "metrics.json"
    if metrics.exists():
        try:
            data = json.loads(metrics.read_text(encoding="utf-8"))
            result["periods"] = data.get("periods") or result["periods"]
        except json.JSONDecodeError:
            pass
    return result


@app.get("/api/runs/{run_id}/export")
def export_run(run_id: str, name: str = "od_matrix", fmt: str = "csv") -> Response:
    df = _load(run_id, name)
    if df is None:
        raise HTTPException(404, "结果不存在")
    if fmt == "parquet":
        data = df.write_parquet()
        return Response(content=data, media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{name}.parquet"'})
    csv = df.write_csv()
    return Response(content="﻿" + csv, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{run_id}_{name}.csv"'})


@app.get("/api/compare")
def compare_runs(ids: str = Query(...)) -> dict:
    run_ids = [r.strip() for r in ids.split(",") if r.strip()]
    if len(run_ids) < 2:
        raise HTTPException(400, "至少需要两个任务对比")
    runs = []
    for rid in run_ids:
        run = store.get_run(rid)
        if not run:
            raise HTTPException(404, f"任务 {rid} 不存在")
        out = RUNS_DIR / rid
        metrics = None
        for cand in (out / "metrics.json",):
            if cand.exists():
                try:
                    metrics = json.loads(cand.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    metrics = None
        od = _load(rid, "od_matrix")
        runs.append({"run": run, "metrics": metrics,
                     "od": od if od is not None and od.height else None})
    return {"runs": _compare_payload(runs)}


def _compare_payload(runs: list[dict]) -> list[dict]:
    out = []
    for item in runs:
        m = item["metrics"] or {}
        acc = m.get("accuracy") or {}
        od = item["od"]
        top = None
        if od is not None and od.height:
            top = od.group_by(["origin_name", "dest_name"]).agg(
                pl.col("passengers").sum().alias("p")).sort("p", descending=True).head(5)
            top = top.to_dicts()
        out.append({
            "run": item["run"],
            "params": (item["run"].get("params") or {}),
            "metrics": {
                "match_rate": m.get("match_rate"),
                "alight_rate": m.get("alight_rate"),
                "uncertain_rate": m.get("uncertain_rate"),
                "transfers": m.get("transfers"),
                "od_total": m.get("od_total"),
                "od_pairs": m.get("od_pairs"),
                "alight_stop_accuracy": acc.get("alight_stop_accuracy"),
                "alight_stop_accuracy_adjacent": acc.get("alight_stop_accuracy_adjacent"),
                "alight_stop_accuracy_confident": acc.get("alight_stop_accuracy_confident"),
                "transfer_f1": acc.get("transfer_f1"),
                "alight_time_mae_sec": acc.get("alight_time_mae_sec"),
                "max_load": m.get("max_load"),
                "avg_load_factor": m.get("avg_load_factor"),
                "elapsed_sec": m.get("elapsed_sec") or item["run"].get("elapsed_sec"),
            },
            "top_od": top,
        })
    return out


# --------------------------------------------------------------------- demo
@app.post("/api/demo/run")
def demo_run(payload: dict = Body(default={})) -> dict:
    """Create (if needed) demo datasets and immediately run the pipeline."""
    _ensure_demo_loaded()
    items = store.list_datasets()
    by_kind = {k: next((d for d in items if d["kind"] == k), None) for k in KINDS}
    if any(v is None for v in by_kind.values()):
        raise HTTPException(409, "演示数据缺失，请先运行 python -m app.cli demo")
    datasets = {k: v["id"] for k, v in by_kind.items()}
    params = payload.get("params")
    run_payload = {"name": payload.get("name") or "演示推算", "params": params or DEFAULT_PARAMS,
                   "datasets": datasets}
    return create_run(run_payload)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8404)
