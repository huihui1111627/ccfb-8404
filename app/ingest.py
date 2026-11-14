"""Data ingestion: file reading, header auto-detection and field mapping.

Three source kinds are supported.  Each has a canonical schema; the user maps
messy source headers onto it.  Parsed results are stored as Parquet so every
run works from the same normalised snapshot.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
import polars as pl

from app.config import DATASETS_DIR

KINDS = {
    "taps": {
        "label": "刷卡数据 (AFC)",
        "fields": {
            "tap_id": {"label": "交易流水号", "required": False},
            "card_id": {"label": "卡号", "required": True},
            "tap_time": {"label": "刷卡时间", "required": True, "type": "datetime"},
            "route_id": {"label": "线路编号", "required": True},
            "vehicle_id": {"label": "车辆编号 (可选)", "required": False},
            "card_type": {"label": "卡类型", "required": False},
        },
    },
    "gps": {
        "label": "车辆 GPS (AVL)",
        "fields": {
            "vehicle_id": {"label": "车辆/车载机编号", "required": True},
            "route_id": {"label": "线路编号", "required": True},
            "direction": {"label": "行向", "required": False, "type": "direction"},
            "gps_time": {"label": "定位时间", "required": True, "type": "datetime"},
            "lon": {"label": "经度", "required": True, "type": "float"},
            "lat": {"label": "纬度", "required": True, "type": "float"},
            "speed_kmh": {"label": "瞬时速度 (km/h)", "required": False, "type": "float"},
        },
    },
    "stops": {
        "label": "线路站点表",
        "fields": {
            "route_id": {"label": "线路编号", "required": True},
            "route_name": {"label": "线路名称", "required": False},
            "direction": {"label": "方向/行向", "required": False, "type": "direction"},
            "stop_seq": {"label": "站点序号", "required": True, "type": "int"},
            "stop_id": {"label": "站点编号", "required": True},
            "stop_name": {"label": "站点名称", "required": False},
            "lon": {"label": "经度", "required": True, "type": "float"},
            "lat": {"label": "纬度", "required": True, "type": "float"},
        },
    },
    "truth": {
        "label": "真值对照 (可选)",
        "fields": {
            "tap_id": {"label": "交易流水号", "required": False},
            "card_id": {"label": "卡号", "required": False},
            "true_alight_stop_id": {"label": "实际下车站编号", "required": True},
            "true_board_stop_id": {"label": "实际上车站编号", "required": False},
            "true_route_id": {"label": "实际线路编号", "required": False},
            "true_direction": {"label": "实际方向", "required": False, "type": "direction"},
            "true_vehicle_id": {"label": "实际车辆编号", "required": False},
            "true_trip_id": {"label": "实际班次", "required": False},
            "true_alight_time": {"label": "实际下车时间", "required": False, "type": "datetime"},
            "true_board_time": {"label": "实际上车时间", "required": False, "type": "datetime"},
            "is_transfer": {"label": "是否换乘", "required": False},
            "has_tap": {"label": "是否有刷卡记录", "required": False},
            "is_duplicate": {"label": "是否重复刷卡", "required": False},
            "is_invalid": {"label": "是否异常卡", "required": False},
        },
    },
}

ALIASES: dict[str, list[str]] = {
    "tap_id": ["交易流水号", "流水号", "交易编号", "tap_id", "transactionid", "recordid", "id"],
    "card_id": ["卡号", "卡编号", "卡片编号", "乘客卡号", "card_id", "cardno", "cardnumber"],
    "tap_time": ["交易时间", "刷卡时间", "上车时间", "交易日期时间", "时间",
                 "tap_time", "transactiontime", "tradetime", "time"],
    "route_id": ["线路编号", "线路代码", "线路id", "线路", "route_id", "routeid", "lineid", "line", "route"],
    "card_type": ["卡类型", "卡种", "card_type", "cardtype", "卡类别"],
    "vehicle_id": ["车载机编号", "车辆编号", "车载编号", "车牌号", "车辆", "车辆id",
                   "vehicle_id", "vehicleid", "deviceid", "busid", "vehicle"],
    "direction": ["行向", "方向", "上下行", "direction", "dir"],
    "gps_time": ["定位时间", "gps时间", "卫星时间", "上传时间", "采集时间", "时间",
                 "gps_time", "gpstime", "time"],
    "lon": ["经度", "lon", "lng", "longitude", "x"],
    "lat": ["纬度", "lat", "latitude", "y"],
    "speed_kmh": ["瞬时速度", "速度", "车速", "speed", "speed_kmh", "speedkmh"],
    "route_name": ["线路名称", "线路名", "route_name", "routename"],
    "stop_seq": ["站点序号", "站序", "序号", "stop_seq", "stopseq", "seq", "order"],
    "stop_id": ["站点编号", "站点id", "站编号", "stop_id", "stopid", "stationid"],
    "stop_name": ["站点名称", "站名", "站点", "stop_name", "stopname", "stationname"],
    "true_alight_stop_id": ["实际下车站编号", "实际下车站", "true_alight_stop_id", "alight_stop_id"],
    "true_board_stop_id": ["实际上车站编号", "实际上车站", "true_board_stop_id", "board_stop_id"],
    "true_route_id": ["实际线路编号", "true_route_id"],
    "true_direction": ["实际方向", "true_direction"],
    "true_vehicle_id": ["实际车辆编号", "true_vehicle_id"],
    "true_trip_id": ["实际班次", "true_trip_id"],
    "true_alight_time": ["实际下车时间", "true_alight_time"],
    "true_board_time": ["实际上车时间", "true_board_time"],
    "is_transfer": ["是否换乘", "is_transfer"],
    "has_tap": ["是否有刷卡记录", "has_tap"],
    "is_duplicate": ["是否重复刷卡", "is_duplicate"],
    "is_invalid": ["是否异常卡", "is_invalid"],
}


def norm_header(s: str) -> str:
    return re.sub(r"[\s_\-()（）\[\]/]+", "", str(s)).lower()


def detect_mapping(columns: list[str], kind: str) -> dict[str, str | None]:
    """Best-effort header matching using alias tables."""
    fields = KINDS[kind]["fields"]
    normed = {norm_header(c): c for c in columns}
    out: dict[str, str | None] = {}
    used: set[str] = set()
    for field in fields:
        found = None
        for alias in [field, *ALIASES.get(field, [])]:
            key = norm_header(alias)
            if key in normed and normed[key] not in used:
                found = normed[key]
                break
        out[field] = found
        if found:
            used.add(found)
    return out


def _read_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_table(raw: bytes, filename: str) -> pl.DataFrame:
    name = filename.lower()
    if name.endswith((".xlsx", ".xls")):
        try:
            import pandas as pd  # system pandas, only used for Excel
            sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None, dtype=str)
            frames = []
            for df in sheets.values():
                frames.append(df)
            if len(frames) > 1:
                df = pd.concat(frames, ignore_index=True)
            else:
                df = frames[0]
            return pl.from_pandas(df, include_index=False)
        except Exception as exc:  # pragma: no cover - environment dependent
            raise ValueError(f"Excel 读取失败：{exc}") from exc
    if name.endswith(".parquet"):
        return pl.read_parquet(io.BytesIO(raw))
    if name.endswith((".json", ".jsonl")):
        text = _read_text(raw)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                data = data.get("data") or list(data.values())[0]
            return pl.DataFrame(data)
        except Exception:
            lines = [json.loads(line) for line in text.splitlines() if line.strip()]
            return pl.DataFrame(lines)
    text = _read_text(raw)
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        # JSON content behind a .csv extension
        data = json.loads(stripped)
        if isinstance(data, dict):
            data = next(iter(data.values()))
        return pl.DataFrame(data)
    # sniff the delimiter from the header line
    first_line = text.splitlines()[0] if text.splitlines() else ""
    sep = ","
    for cand in ("\t", ";", "|"):
        if first_line.count(cand) > first_line.count(sep):
            sep = cand
    try:
        return pl.read_csv(
            io.StringIO(text), separator=sep, infer_schema_length=0,
            truncate_ragged_lines=True, ignore_errors=True,
        )
    except Exception:
        return pl.read_csv(
            io.BytesIO(raw), separator=sep, infer_schema_length=0,
            truncate_ragged_lines=True, ignore_errors=True,
        )


def _normalise_direction(series: pl.Series) -> pl.Series:
    mapping = {
        "上行": 0, "下行": 1, "上": 0, "下": 1, "0": 0, "1": 1,
        "up": 0, "down": 1, "去程": 0, "回程": 1,
    }
    out = []
    for v in series.to_list():
        if v is None:
            out.append(None)
            continue
        key = str(v).strip().lower()
        out.append(mapping.get(key, None))
    s = pl.Series(out, dtype=pl.Int64)
    return s


def parse_time_series(s: pl.Series) -> pl.Series:
    if s.dtype.is_temporal():
        return s.cast(pl.Datetime("us"))
    if s.dtype.is_numeric():
        # epoch seconds or milliseconds
        maxv = s.drop_nulls().max() if s.len() else None
        if maxv is not None and float(maxv) > 1e11:
            return (s.cast(pl.Float64) / 1000).cast(pl.Int64).cast(
                pl.Datetime("ms")).cast(pl.Datetime("us"))
        if maxv is not None and float(maxv) > 1e8:
            return s.cast(pl.Int64).cast(pl.Datetime("s")).cast(pl.Datetime("us"))
    txt = s.cast(pl.Utf8).str.strip_chars().str.replace_all("/", "-")
    try:
        out = txt.str.to_datetime(strict=False, time_unit="us")
    except Exception:
        # strict=False still infers a single format; if nothing in the column
        # looks like a date it raises instead of returning nulls
        out = pl.Series([None] * len(txt), dtype=pl.Datetime("us"))
    # retry with explicit compact formats
    if out.is_null().any():
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                    "%Y%m%d%H%M%S"):
            try:
                retry = txt.str.to_datetime(format=fmt, strict=False, time_unit="us")
            except Exception:
                continue
            out = out.fill_null(retry)
    if out.is_null().all() and len(txt) > 0:
        sample = next((v for v in txt.head(3).to_list() if v), "")
        raise ValueError(
            f"时间列无法解析（示例值：{sample!r}），请确认映射到了正确的时间列")
    return out


def apply_mapping(df: pl.DataFrame, mapping: dict[str, str | None], kind: str) -> tuple[pl.DataFrame, dict]:
    """Rename mapped columns to canonical names, cast types, drop bad rows."""
    spec = KINDS[kind]["fields"]
    warnings: list[str] = []
    rename = {}
    for field, src in mapping.items():
        if field not in spec:
            continue
        if src and src in df.columns:
            rename[src] = field
        elif src and src not in df.columns:
            warnings.append(f"字段 {field} 映射的列 '{src}' 不存在")
    ds = df.rename(rename)
    missing_required = [f for f in spec if spec[f]["required"] and f not in ds.columns]
    if missing_required:
        labels = "、".join(spec[f]["label"] for f in missing_required)
        raise ValueError(f"缺少必填字段：{labels}")

    out_cols: dict[str, pl.Series] = {}
    null_counts: dict[str, int] = {}
    n_in = len(ds)
    for field, meta in spec.items():
        if field not in ds.columns:
            out_cols[field] = pl.Series(field, [None] * n_in, dtype=pl.Utf8)
            continue
        col = ds[field]
        ftype = meta.get("type")
        if ftype == "datetime":
            try:
                parsed = parse_time_series(col)
            except ValueError as exc:
                raise ValueError(f"{meta['label']}：{exc}") from None
            null_counts[field] = int(parsed.is_null().sum())
            out_cols[field] = parsed
        elif ftype == "float":
            parsed = col.cast(pl.Float64, strict=False)
            null_counts[field] = int(parsed.is_null().sum())
            out_cols[field] = parsed
        elif ftype == "int":
            parsed = col.cast(pl.Float64, strict=False).cast(pl.Int64, strict=False)
            null_counts[field] = int(parsed.is_null().sum())
            out_cols[field] = parsed
        elif ftype == "direction":
            parsed = _normalise_direction(col)
            null_counts[field] = int(parsed.is_null().sum())
            out_cols[field] = parsed
        else:
            parsed = col.cast(pl.Utf8).str.strip_chars()
            if field in ("card_id", "route_id", "stop_id", "vehicle_id", "route_name", "stop_name"):
                parsed = parsed.str.replace(r"\.0$", "")
            null_counts[field] = int(parsed.is_null().sum())
            out_cols[field] = parsed

    df_out = pl.DataFrame(out_cols)
    if kind == "stops" and "direction" in df_out.columns:
        df_out = df_out.with_columns(pl.col("direction").fill_null(0))
    if kind == "gps" and "direction" in df_out.columns:
        df_out = df_out.with_columns(pl.col("direction").fill_null(0))
    if kind == "taps" and "tap_id" in df_out.columns:
        df_out = df_out.with_row_index("_row").with_columns(
            pl.when(pl.col("tap_id").is_null() | (pl.col("tap_id") == ""))
            .then(pl.concat_str([pl.lit("auto_"), pl.col("_row").cast(pl.Utf8)]))
            .otherwise(pl.col("tap_id")).alias("tap_id")
        ).drop("_row")

    required = [f for f in spec if spec[f]["required"]]
    before = len(df_out)
    df_out = df_out.drop_nulls(subset=required)
    dropped = before - len(df_out)
    if dropped:
        warnings.append(f"删除必填字段为空的行 {dropped} 行")

    # sanity ranges
    if kind in ("gps", "stops"):
        bad_coord = df_out.filter(
            ~pl.col("lat").is_between(3, 60) | ~pl.col("lon").is_between(70, 140)
        ).height
        if bad_coord:
            df_out = df_out.filter(
                pl.col("lat").is_between(3, 60) & pl.col("lon").is_between(70, 140)
            )
            warnings.append(f"丢弃经纬度越界行 {bad_coord} 行")
    if kind == "stops":
        dup = df_out.height - df_out.select(
            ["route_id", "direction", "stop_seq"]).unique().height
        if dup:
            warnings.append(f"线路/方向/序号重复 {dup} 行，保留首条")
            df_out = df_out.unique(subset=["route_id", "direction", "stop_seq"], keep="first")
    if kind == "taps":
        dup = df_out.height - df_out.select(["tap_id"]).unique().height
        if dup:
            df_out = df_out.unique(subset=["tap_id"], keep="first")
            warnings.append(f"交易流水号重复，去重 {dup} 行")

    stats = {
        "rows_in": n_in,
        "rows_out": len(df_out),
        "null_counts": null_counts,
        "warnings": warnings,
    }
    return df_out, stats


def dataset_path(ds_id: str) -> Path:
    return DATASETS_DIR / f"{ds_id}.parquet"


def save_dataset_frame(ds_id: str, df: pl.DataFrame) -> Path:
    path = dataset_path(ds_id)
    df.write_parquet(path)
    return path


def load_dataset_frame(ds_id: str) -> pl.DataFrame:
    return pl.read_parquet(dataset_path(ds_id))
