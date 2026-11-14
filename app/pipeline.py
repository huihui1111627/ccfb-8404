"""Core OD inference pipeline.

Stages
------
1. clean GPS (duplicates, drift spikes, impossible speeds)
2. snap fixes to route stops and detect dwell events -> vehicle stop events
3. segment vehicles into runs (trips) from the event sequence
4. match each AFC tap to a vehicle stop event -> inferred boarding stop / vehicle
5. infer alighting stop per card using trip-chain evidence (next tap, return
   anchor) scored against the downstream stops of the actual run
6. identify transfers, split cards into journeys
7. build OD matrix, stop board/alight counts, section loads and load factors
8. score against ground truth when supplied

Everything tabular is Polars; only the per-card sequential chain logic is a
Python loop, because it is inherently order-dependent.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import polars as pl

from app.config import DEFAULT_PARAMS
from app.utils import build_period_expr, deep_merge, haversine_m, jsonable, validate_params

Progress = Callable[[float, str, str], None]

R_EARTH = 6371008.8


def _noop(progress: float, stage: str, message: str) -> None:  # pragma: no cover
    pass


def _sec(v: float) -> _dt.timedelta:
    return _dt.timedelta(seconds=float(v))


def _as_str(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    exprs = [pl.col(c).cast(pl.Utf8).str.strip_chars() for c in cols if c in df.columns]
    return df.with_columns(exprs) if exprs else df


def dist_expr(lat1, lon1, lat2, lon2) -> pl.Expr:
    rad = math.pi / 180.0
    p1 = lat1 * rad
    p2 = lat2 * rad
    dp = p2 - p1
    dl = (lon2 - lon1) * rad
    a = (dp / 2).sin() ** 2 + p1.cos() * p2.cos() * (dl / 2).sin() ** 2
    return 2 * R_EARTH * a.clip(0, 1).sqrt().arcsin()


@dataclass
class StopCatalog:
    rows: pl.DataFrame                       # stop_id, stop_name, lat, lon
    by_route: dict[str, pl.DataFrame]        # route stop sequence, both directions
    seq_options: dict[tuple[str, int, str], list[int]]
    names: dict[str, str]
    coords: dict[str, tuple[float, float]]


class Pipeline:
    def __init__(self, params: dict, frames: dict[str, pl.DataFrame],
                 progress: Progress = _noop):
        self.p = deep_merge(DEFAULT_PARAMS, params or {})
        errs = validate_params(self.p)
        if errs:
            raise ValueError("；".join(errs))
        self.frames = frames
        self.progress = progress
        self.out: dict[str, pl.DataFrame] = {}
        self.metrics: dict = {}
        self.warnings: list[str] = []
        self._t0 = time.time()

    def log(self, pct: float, stage: str, msg: str) -> None:
        self.progress(pct, stage, msg)

    # ================================================================== main
    def run(self) -> dict:
        self.log(2, "load", "读取数据")
        frames = self.frames
        taps = _as_str(frames["taps"],
                       ["tap_id", "card_id", "route_id", "vehicle_id", "card_type"])
        gps = _as_str(frames["gps"], ["vehicle_id", "route_id"])
        stops = _as_str(frames["stops"], ["route_id", "route_name", "stop_id", "stop_name"])
        truth = frames.get("truth")
        if truth is not None and truth.height:
            truth = truth.with_columns([
                pl.col(c).cast(pl.Utf8).str.to_lowercase()
                .is_in(["true", "1", "是", "yes"]).fill_null(False).alias(c)
                for c in ("is_transfer", "is_duplicate", "is_invalid")
                if c in truth.columns
            ])
            for c in ("is_transfer", "is_duplicate", "is_invalid"):
                if c not in truth.columns:
                    truth = truth.with_columns(pl.lit(False).alias(c))
        if taps.height == 0:
            raise ValueError("刷卡数据为空")
        if gps.height == 0:
            raise ValueError("GPS 数据为空")
        if stops.height == 0:
            raise ValueError("站点数据为空")

        cat = self._prepare_stops(stops)
        self._cat = cat
        self._hop = 0
        self.log(8, "clean", "清洗刷卡数据")
        taps = self._clean_taps(taps)

        self.log(14, "gps", "清洗 GPS 漂移与重复")
        gps_clean, gps_stats = self._clean_gps(gps)
        self.log(26, "gps", "GPS 吸附站点并识别停站事件")
        events = self._detect_stop_events(gps_clean, cat)
        events = self._segment_runs(events, cat)
        self._events = events
        self.out["gps_clean"] = gps_clean.select(
            ["vehicle_id", "route_id", "direction", "gps_time", "lat", "lon",
             "implied_speed_kmh"])
        self.out["stop_events"] = events.select([
            "event_idx", "run_id", "run_pos", "vehicle_id", "route_id", "direction",
            "stop_id", "arr_time", "dep_time", "dwell_sec", "snap_m", "n_points",
        ])

        self.log(44, "match", "刷卡时间匹配车辆停站")
        matched, match_stats = self._match_taps(taps, events, cat)
        matched = self._flag_duplicates(matched)
        self.out["taps"] = matched

        self.log(58, "chain", "按出行链推断下车站")
        legs, chain_stats = self._infer_alighting(matched, cat)

        self.log(74, "transfer", "识别换乘并划分出行链")
        legs, transfer_stats = self._transfers_and_journeys(legs)
        legs = self._attach_period(legs)
        self.out["legs"] = legs

        self.log(82, "matrix", "汇总 OD 矩阵 / 断面客流 / 满载率")
        od, _ = self._od_matrix(legs)
        stop_flow, stop_board, sections = self._stop_flow(legs, events, cat)
        self.out["od_matrix"] = od
        self.out["stop_flow"] = stop_flow
        self.out["stop_board"] = stop_board
        self.out["sections"] = sections
        self.out["stop_match"] = self._stop_match_table(events, matched, cat)

        self.log(92, "metrics", "计算指标" + ("并与真值比对" if truth is not None else ""))
        metrics = self._build_metrics(matched, legs, events, od, stop_flow,
                                      gps_stats, match_stats, chain_stats, transfer_stats)
        if truth is not None and truth.height:
            metrics["accuracy"] = self._accuracy(truth, matched, legs, cat)
            acc = metrics["accuracy"]
            if acc.get("available"):
                def _pct(v):  # a value can be None when no confident leg exists
                    return f"{v:.1%}" if isinstance(v, (int, float)) else "-"
                metrics["warnings"].append(
                    f"下车站准确率 {_pct(acc.get('alight_stop_accuracy'))}"
                    f"（确定结果 {_pct(acc.get('alight_stop_accuracy_confident'))}，"
                    f"占 {_pct(acc.get('confident_share'))}）")
        self.metrics = metrics
        self.log(100, "done", "完成")
        return metrics

    # ================================================================== stops
    def _prepare_stops(self, stops: pl.DataFrame) -> StopCatalog:
        df = stops.drop_nulls(subset=["route_id", "stop_id", "lat", "lon"]).with_columns([
            pl.col("direction").cast(pl.Int64, strict=False).fill_null(0).alias("direction"),
            pl.col("stop_seq").cast(pl.Int64, strict=False),
            pl.col("lat").cast(pl.Float64), pl.col("lon").cast(pl.Float64),
        ])
        if df.filter(pl.col("stop_seq").is_null()).height:
            df = df.with_columns(pl.col("stop_seq").fill_null(
                pl.int_range(1, pl.len() + 1).over(["route_id", "direction"])))
        names: dict[str, str] = {}
        for r in df.select(["stop_id", "stop_name"]).drop_nulls().unique("stop_id").to_dicts():
            names[r["stop_id"]] = r.get("stop_name") or r["stop_id"]
        coords = {
            r["stop_id"]: (float(r["lat"]), float(r["lon"]))
            for r in df.group_by("stop_id").agg(
                pl.col("lat").median(), pl.col("lon").median()).to_dicts()
        }
        rows = pl.DataFrame([
            {"stop_id": sid, "stop_name": names.get(sid, sid), "lat": c[0], "lon": c[1]}
            for sid, c in coords.items()
        ])
        by_route: dict[str, pl.DataFrame] = {}
        seq_options: dict[tuple[str, int, str], list[int]] = {}
        for rid, sub in df.group_by("route_id"):
            rid = rid[0]
            by_route[rid] = sub.sort(["direction", "stop_seq"])
            for d, ds in sub.group_by("direction"):
                d = int(d[0])
                ds = ds.sort("stop_seq")
                for row in ds.iter_rows(named=True):
                    seq_options.setdefault((rid, d, row["stop_id"]), []).append(
                        int(row["stop_seq"]))
        for k in seq_options:
            seq_options[k] = sorted(set(seq_options[k]))
        return StopCatalog(rows, by_route, seq_options, names, coords)

    # ================================================================== taps
    def _clean_taps(self, taps: pl.DataFrame) -> pl.DataFrame:
        taps = taps.filter(pl.col("tap_time").is_not_null() & pl.col("card_id").is_not_null())
        taps = taps.unique(subset=["tap_id"], keep="first", maintain_order=True)
        return taps.sort(["card_id", "tap_time"])

    # =================================================================== gps
    def _clean_gps(self, gps: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
        p = self.p
        df = gps.drop_nulls(subset=["vehicle_id", "gps_time", "lat", "lon"]).with_columns([
            pl.col("lat").cast(pl.Float64), pl.col("lon").cast(pl.Float64),
        ])
        df = df.with_columns(
            pl.col("speed_kmh").cast(pl.Float64, strict=False) if "speed_kmh" in df.columns
            else pl.lit(None, dtype=pl.Float64).alias("speed_kmh"))
        df = df.with_columns(
            pl.col("direction").cast(pl.Int64, strict=False) if "direction" in df.columns
            else pl.lit(None, dtype=pl.Int64).alias("direction"))
        n0 = df.height
        dup = df.height - df.unique(subset=["vehicle_id", "gps_time"]).height
        df = df.unique(subset=["vehicle_id", "gps_time"], keep="first", maintain_order=True)
        df = df.sort(["vehicle_id", "gps_time"])
        dt_prev = pl.col("gps_time").diff().over("vehicle_id").dt.total_seconds()
        dt_next = (-pl.col("gps_time").diff(-1).over("vehicle_id").dt.total_seconds())
        d_prev = dist_expr(
            pl.col("lat").shift().over("vehicle_id"), pl.col("lon").shift().over("vehicle_id"),
            pl.col("lat"), pl.col("lon"))
        d_next = dist_expr(
            pl.col("lat"), pl.col("lon"),
            pl.col("lat").shift(-1).over("vehicle_id"),
            pl.col("lon").shift(-1).over("vehicle_id"))
        # distance between the neighbours themselves: small means this fix
        # jumped out and came straight back
        d_skip = dist_expr(
            pl.col("lat").shift().over("vehicle_id"),
            pl.col("lon").shift().over("vehicle_id"),
            pl.col("lat").shift(-1).over("vehicle_id"),
            pl.col("lon").shift(-1).over("vehicle_id"))
        sp_prev = (d_prev / dt_prev.clip(0.5, None) * 3.6).fill_null(0)
        sp_next = (d_next / dt_next.clip(0.5, None) * 3.6).fill_null(0)
        df = df.with_columns([
            d_prev.alias("_d_prev"), d_next.alias("_d_next"),
            d_skip.alias("_d_skip"),
            sp_prev.alias("_sp_prev"), sp_next.alias("_sp_next"),
        ])
        # A fix is drift when it sits far from BOTH neighbours while those
        # neighbours are close to each other -- the classic "jump out and back"
        # spike.  Requiring both sides stops us deleting the legitimate
        # neighbour of a spike along with it.
        maxs = float(p["gps_speed_max_kmh"])
        far = float(p["gps_drift_max_m"])
        df = df.with_columns(
            ((pl.col("_d_prev") > far) & (pl.col("_d_next") > far)
             & (pl.col("_sp_prev") > maxs) & (pl.col("_sp_next") > maxs)
             & (pl.col("_d_skip") < far * 2))
            .fill_null(False).alias("_drift"))
        n_drift = int(df["_drift"].sum())
        df = df.filter(~pl.col("_drift")).drop("_drift")
        df = df.unique(subset=["vehicle_id", "gps_time"], keep="first").sort(
            ["vehicle_id", "gps_time"])
        d_prev2 = dist_expr(
            pl.col("lat").shift().over("vehicle_id"), pl.col("lon").shift().over("vehicle_id"),
            pl.col("lat"), pl.col("lon"))
        dt2 = pl.col("gps_time").diff().over("vehicle_id").dt.total_seconds()
        df = df.with_columns(
            (d_prev2 / dt2.clip(0.5, 90) * 3.6).fill_null(0).clip(0, 200)
            .alias("implied_speed_kmh"))
        if n_drift:
            self.warnings.append(f"GPS 漂移点剔除 {n_drift} 个")
        if dup:
            self.warnings.append(f"GPS 重复时间戳去重 {dup} 条")
        stats = {
            "rows_in": n0, "rows_out": df.height, "duplicates_removed": dup,
            "drift_points_removed": n_drift, "vehicles": df["vehicle_id"].n_unique(),
            "span_start": jsonable(df["gps_time"].min()) if df.height else None,
            "span_end": jsonable(df["gps_time"].max()) if df.height else None,
        }
        return df, stats

    # ----------------------------------------------------------- stop events
    def _detect_stop_events(self, gps: pl.DataFrame, cat: StopCatalog) -> pl.DataFrame:
        p = self.p
        radius = float(p["stop_snap_radius_m"])
        frames = []
        for rid in gps["route_id"].drop_nulls().unique().to_list():
            route_stops = cat.by_route.get(rid)
            if route_stops is None or route_stops.height == 0:
                continue
            sub = gps.filter(pl.col("route_id") == rid)
            if sub.height == 0:
                continue
            st = route_stops.select(
                ["stop_id", "stop_name", "direction", "lat", "lon", "stop_seq"]
            ).unique(subset=["stop_id", "direction"], keep="first")
            joined = sub.with_row_index("_gid").join(
                st.with_row_index("_sid"), how="cross", suffix="_stop")
            joined = joined.with_columns([
                dist_expr(pl.col("lat"), pl.col("lon"), pl.col("lat_stop"),
                          pl.col("lon_stop")).alias("snap_m"),
                pl.when(pl.col("direction") == pl.col("direction_stop"))
                .then(0.0).otherwise(400.0).alias("_dir_penalty"),
            ])
            nearest = (joined.with_columns(
                (pl.col("snap_m") + pl.col("_dir_penalty")).alias("_rank"))
                .sort(["_gid", "_rank"]).group_by("_gid", maintain_order=True).first())
            nearest = nearest.filter(pl.col("snap_m") <= radius)
            if nearest.height:
                frames.append(nearest)
        if not frames:
            raise ValueError("GPS 无法吸附到任何站点，请检查站点表与 GPS 是否同一区域")
        pts = pl.concat(frames, how="vertical").sort(["vehicle_id", "gps_time"])
        gap_break = float(self.p.get("event_gap_break_sec", 180.0))
        pts = pts.with_columns([
            (pl.col("stop_id") != pl.col("stop_id").shift().over("vehicle_id"))
            .fill_null(True).alias("_stop_chg"),
            ((pl.col("gps_time").diff().over("vehicle_id").dt.total_seconds() > gap_break)
             .fill_null(True)).alias("_time_chg"),
        ]).with_columns(
            (pl.col("_stop_chg") | pl.col("_time_chg"))
            .cum_sum().over("vehicle_id").alias("_grp"))
        grp = pts.group_by(["vehicle_id", "_grp"], maintain_order=True).agg([
            pl.col("route_id").first(),
            pl.col("direction").drop_nulls().first().alias("direction"),
            pl.col("stop_id").first(), pl.col("stop_name").first().alias("stop_name"),
            pl.col("gps_time").min().alias("arr_time"),
            pl.col("gps_time").max().alias("dep_time"),
            pl.len().alias("n_points"),
            pl.col("snap_m").min().alias("snap_m"),
            pl.col("implied_speed_kmh").min().alias("min_speed"),
            pl.col("lat_stop").mean().alias("stop_lat"),
            pl.col("lon_stop").mean().alias("stop_lon"),
        ]).drop("_grp")
        grp = grp.with_columns(
            (pl.col("dep_time") - pl.col("arr_time")).dt.total_seconds().alias("dwell_sec"))
        grp = grp.filter(
            (pl.col("dwell_sec") >= float(p["min_dwell_sec"]))
            & (pl.col("min_speed") <= float(p["dwell_speed_kmh"]) * 1.6))
        if grp.height == 0:
            raise ValueError("未识别到任何停站事件，请放宽停站判定阈值")
        # merge a dwell that got fragmented by a lost GPS fix
        grp = grp.sort(["vehicle_id", "arr_time"]).with_columns([
            (pl.col("stop_id") != pl.col("stop_id").shift().over("vehicle_id")).alias("_sc"),
            ((pl.col("arr_time") - pl.col("dep_time").shift().over("vehicle_id"))
             .dt.total_seconds() > 90).alias("_tc"),
        ]).with_columns(
            (pl.col("_sc").fill_null(True) | pl.col("_tc").fill_null(True))
            .cum_sum().over("vehicle_id").alias("_egrp"))
        merged = grp.group_by(["vehicle_id", "_egrp"], maintain_order=True).agg([
            pl.col("route_id").first(),
            pl.col("direction").first(), pl.col("stop_id").first(),
            pl.col("stop_name").first(),
            pl.col("stop_lat").first(), pl.col("stop_lon").first(),
            pl.col("arr_time").min(), pl.col("dep_time").max(),
            pl.col("n_points").sum(), pl.col("snap_m").min(),
            pl.col("dwell_sec").max(),
        ]).drop("_egrp")
        return merged.sort(["vehicle_id", "arr_time"]).with_row_index("event_idx")

    def _segment_runs(self, events: pl.DataFrame, cat: StopCatalog) -> pl.DataFrame:
        rows = events.to_dicts()
        gap_max = float(self.p.get("run_break_gap_sec", 2400))
        by_vehicle: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_vehicle[r["vehicle_id"]].append(r)
        out: list[dict] = []
        run_no = 0
        for veh, evs in by_vehicle.items():
            evs.sort(key=lambda x: x["arr_time"])
            prev = None
            run_id = None
            pos = 0
            last_travel = 0
            for e in evs:
                rid = e["route_id"]
                d = int(e["direction"]) if e["direction"] is not None else None
                opts0 = cat.seq_options.get((rid, 0 if d is None else d, e["stop_id"]), [])
                opts = [s for s in opts0 if s > last_travel]
                new_run = False
                if prev is None:
                    new_run = True
                elif d is not None and prev["_d"] is not None and d != prev["_d"]:
                    new_run = True
                elif (e["arr_time"] - prev["dep_time"]).total_seconds() > gap_max:
                    new_run = True
                elif not opts:
                    new_run = True
                if new_run:
                    run_no += 1
                    run_id = f"{veh}#{run_no:04d}"
                    pos = 0
                    last_travel = 0
                    opts0 = cat.seq_options.get((rid, 0 if d is None else d, e["stop_id"]), [])
                    opts = [s for s in opts0 if s > 0] or opts0
                travel = min(opts) if opts else last_travel + 1
                pos += 1
                last_travel = travel
                e2 = dict(e)
                e2.update({"_d": d, "run_id": run_id, "run_pos": pos, "travel": travel})
                out.append(e2)
                prev = e2
        df = pl.DataFrame(out).drop(["_d", "travel"])
        counts = df.group_by("run_id").len().rename({"len": "_n"})
        df = df.join(counts, on="run_id").filter(pl.col("_n") >= 2).drop("_n")
        return df.sort(["vehicle_id", "arr_time"])

    # -------------------------------------------------------------- matching
    def _match_taps(self, taps: pl.DataFrame, events: pl.DataFrame,
                    cat: StopCatalog) -> tuple[pl.DataFrame, dict]:
        p = self.p
        tol = float(p["boarding_tap_tolerance_sec"])
        search = float(p["boarding_search_sec"])
        ev_by_route: dict[str, list[dict]] = defaultdict(list)
        for e in events.to_dicts():
            ev_by_route[e["route_id"]].append(e)
        dep_times = {}
        for rid, lst in ev_by_route.items():
            lst.sort(key=lambda x: x["dep_time"])
            dep_times[rid] = [e["dep_time"] for e in lst]

        results = []
        n_unmatched = 0
        n_ext = 0
        n_no_vehicle_id = 0
        for t in taps.iter_rows(named=True):
            rid = t["route_id"]
            tt = t["tap_time"]
            cands = []
            lst = ev_by_route.get(rid, [])
            tap_veh = t.get("vehicle_id") or None
            if lst:
                deps = dep_times[rid]
                lo = bisect.bisect_left(deps, tt - _sec(search))
                hi = bisect.bisect_right(deps, tt + _sec(search))
                for e in lst[max(0, lo - 3): hi + 3]:
                    arr, dep = e["arr_time"], e["dep_time"]
                    if arr - _sec(60) <= tt <= dep:
                        rank, delta = 0.0, (tt - dep).total_seconds()
                    elif dep < tt <= dep + _sec(tol):
                        rank, delta = 1.0, (tt - dep).total_seconds()
                    elif dep < tt <= dep + _sec(search):
                        rank, delta = 2.0, (tt - dep).total_seconds()
                    else:
                        continue
                    if tap_veh is not None and e["vehicle_id"] != tap_veh:
                        continue
                    cands.append((rank, abs(delta), delta, e))
            if not cands:
                n_unmatched += 1
                results.append({
                    **t, "status": "invalid", "reason": "no_gps_match",
                    "event_idx": None, "vehicle_id": None, "run_id": None,
                    "run_pos": None, "direction": None, "board_stop_id": None,
                    "board_stop_name": None, "board_time": None,
                    "event_arr": None, "event_dep": None,
                    "match_delta_sec": None, "match_method": "unmatched",
                    "board_lat": None, "board_lon": None,
                })
                continue
            cands.sort(key=lambda x: (x[0], x[1], str(x[3]["vehicle_id"])))
            # Without a vehicle id on the tap, different runs of the same route
            # are hard to tell apart.  Warn once so the accuracy figures are not
            # read as if the vehicle were known.
            if tap_veh is None:
                n_no_vehicle_id += 1
            rank, _, delta, e = cands[0]
            method = {0.0: "dwell", 1.0: "after_dep"}.get(rank, "extended")
            if rank == 2.0:
                n_ext += 1
            sid = e["stop_id"]
            lat, lon = cat.coords.get(sid, (None, None))
            results.append({
                **t, "status": "valid", "reason": "",
                "event_idx": e["event_idx"], "vehicle_id": e["vehicle_id"],
                "run_id": e["run_id"], "run_pos": e["run_pos"],
                "direction": e["direction"], "board_stop_id": sid,
                "board_stop_name": cat.names.get(sid, sid), "board_time": tt,
                "event_arr": e["arr_time"], "event_dep": e["dep_time"],
                "match_delta_sec": round(delta, 1), "match_method": method,
                "board_lat": lat, "board_lon": lon,
            })
        out = pl.DataFrame(results, infer_schema_length=None)
        stats = {
            "taps": taps.height,
            "matched": int((out["status"] == "valid").sum()),
            "unmatched": n_unmatched, "extended_matches": n_ext,
            "match_rate": round(1 - n_unmatched / max(1, taps.height), 4),
            "taps_without_vehicle": n_no_vehicle_id,
        }
        if n_no_vehicle_id > taps.height * 0.5:
            self.warnings.append(
                f"刷卡数据未提供车辆编号（{n_no_vehicle_id} 条），同线路多车时"
                "上车站与下车站会存在歧义，准确率将低于有车辆编号的情况")
        return out, stats

    def _flag_duplicates(self, taps: pl.DataFrame) -> pl.DataFrame:
        win = float(self.p["dup_tap_window_sec"])
        taps = taps.sort(["card_id", "tap_time"]).with_columns([
            pl.col("tap_time").diff().over("card_id").dt.total_seconds().alias("_gap"),
            pl.col("route_id").shift().over("card_id").alias("_prev_route"),
            pl.col("event_idx").shift().over("card_id").alias("_prev_event"),
        ])
        dup = ((pl.col("status") == "valid") & (pl.col("_gap") <= win)
               & ((pl.col("route_id") == pl.col("_prev_route"))
                  | (pl.col("event_idx") == pl.col("_prev_event"))))
        taps = taps.with_columns(dup.alias("_dup"))
        # duplicate of an *unmatched* tap carries no vehicle id, so a second tap
        # on the same route within the window is likely two taps for one ride
        dup_routed = ((pl.col("status") != "valid") & (pl.col("_gap") <= win)
                      & (pl.col("route_id") == pl.col("_prev_route")))
        taps = taps.with_columns((pl.col("_dup") | dup_routed.fill_null(False))
                                 .alias("_dup_any"))
        taps = taps.with_columns([
            pl.when(pl.col("_dup_any")).then(pl.lit("duplicate")).otherwise(pl.col("status"))
            .alias("status"),
            pl.when(pl.col("_dup_any")).then(pl.lit("duplicate_tap")).otherwise(pl.col("reason"))
            .alias("reason"),
        ]).drop(["_gap", "_prev_route", "_prev_event", "_dup", "_dup_any"])
        return taps.sort(["card_id", "tap_time"])

    # ============================================================== alighting
    def _infer_alighting(self, taps: pl.DataFrame, cat: StopCatalog):
        p = self.p
        events = self._events
        ev_by_run: dict[str, list[dict]] = defaultdict(list)
        for r in events.to_dicts():
            ev_by_run[r["run_id"]].append(r)
        for rid in ev_by_run:
            ev_by_run[rid].sort(key=lambda x: x["run_pos"])

        valid = taps.filter(pl.col("status") == "valid").sort(["card_id", "tap_time"])
        by_card: dict[str, list[dict]] = defaultdict(list)
        for t in valid.to_dicts():
            by_card[t["card_id"]].append(t)

        walk_speed = float(p["walk_speed_mps"])
        walk_max = float(p["transfer_walk_max_m"])
        slack = float(p["timing_slack_sec"])
        use_anchor = bool(p["use_return_anchor"])
        anchor_gap = float(p["return_anchor_gap_min"]) * 60
        max_ride = float(p["max_ride_min"]) * 60
        link_max = float(p.get("chain_link_window_min", 90)) * 60

        legs: list[dict] = []
        method_counts: dict[str, int] = defaultdict(int)
        unresolved = 0
        for card_id, card_taps in by_card.items():
            card_taps.sort(key=lambda x: x["tap_time"])
            first_board = card_taps[0]["board_stop_id"]
            anchor_ok = (use_anchor and len(card_taps) >= 2
                         and (card_taps[-1]["tap_time"] - card_taps[0]["tap_time"]).total_seconds()
                         >= anchor_gap)
            for i, t in enumerate(card_taps):
                next_t = card_taps[i + 1] if i + 1 < len(card_taps) else None
                run = ev_by_run.get(t["run_id"], [])
                pos = int(t["run_pos"] or 0)
                downstream = [e for e in run if e["run_pos"] > pos]
                if not downstream:
                    legs.append(self._leg_row(t, cat, None, "terminal_unresolved", "low",
                                              True, 0.0, None, None, 0))
                    method_counts["terminal_unresolved"] += 1
                    unresolved += 1
                    continue
                best = None
                for e in downstream:
                    score, method = self._score_candidate(
                        t, e, next_t, first_board, anchor_ok, cat, walk_speed,
                        walk_max, slack, max_ride, downstream[-1]["stop_id"], p,
                        link_max, len(card_taps))
                    if best is None or score > best[0]:
                        best = (score, method, e)
                score, method, e = best
                if method == "prior":
                    # no positive evidence: mark explicitly
                    method = ("terminal_fallback" if e["stop_id"] == downstream[-1]["stop_id"]
                              else "prior_fallback")
                conf = "high" if score >= 55 else ("medium" if score >= 30 else "low")
                uncertain = method in ("prior_fallback", "terminal_fallback",
                                       "terminal_unresolved") or score < 30
                legs.append(self._leg_row(t, cat, e, method, conf, uncertain, score,
                                          e["arr_time"], e["run_pos"], len(downstream)))
                method_counts[method] += 1
        df = pl.DataFrame(legs, infer_schema_length=None)
        # ---- second pass: give no-evidence legs a hop-count prior learned from
        # the legs we *could* resolve.  Without this the only sane guess for an
        # open-ended ride is "stayed on to the terminal", which is right only
        # when the passenger really went that far.
        if bool(p.get("use_hop_prior", True)) and df.height:
            df, n_reprior = self._apply_hop_prior(df)
            self._hop = n_reprior
        else:
            n_reprior = 0
        # a physical card cannot board a later vehicle before it left the previous one
        df = df.sort(["card_id", "board_time"])
        df = df.with_columns(
            pl.col("alight_time").shift().over("card_id").alias("_prev_alight"))
        df = df.with_columns(
            ((pl.col("_prev_alight").is_not_null())
             & (pl.col("board_time") < pl.col("_prev_alight") - _sec(slack)))
            .alias("_conflict"))
        df = df.with_columns([
            pl.when(pl.col("_conflict")).then(pl.lit("chain_conflict"))
            .otherwise(pl.col("alight_method")).alias("alight_method"),
            pl.when(pl.col("_conflict")).then(True).otherwise(pl.col("uncertain")).alias("uncertain"),
        ])
        n_conflict = int(df["_conflict"].sum())
        df = df.drop(["_prev_alight", "_conflict"])
        stats = {
            "legs": df.height, "methods": dict(method_counts),
            "uncertain": int(df["uncertain"].sum()),
            "unresolved": unresolved,
            "alight_inferred": int(df["alight_stop_id"].is_not_null().sum()),
            "chain_conflicts": n_conflict,
            "hop_prior_applied": n_reprior,
        }
        if unresolved:
            self.warnings.append(f"{unresolved} 条刷卡记录在终点站无下游站可推，已标记未推算")
        return df, stats

    def _apply_hop_prior(self, df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
        """Assign a most-likely alighting stop to legs with no direct evidence.

        The prior is learned from the legs we could resolve: how often each
        stop is an alighting point, how often it is an origin, and the joint
        OD frequency, all discounted by ride length.  This is still a guess,
        so such legs stay flagged ``uncertain`` and are reported separately
        in the accuracy metrics.
        """
        from collections import Counter as _Counter

        resolved = df.filter(~pl.col("uncertain")).filter(
            pl.col("alight_event_pos").is_not_null())
        if resolved.height < 20:
            return df, 0
        alight_cnt: _Counter = _Counter()
        board_cnt: _Counter = _Counter()
        od_cnt: dict[str, _Counter] = defaultdict(_Counter)
        route_alight: _Counter = _Counter()
        route_tot: _Counter = _Counter()
        for r in resolved.to_dicts():
            alight_cnt[r["alight_stop_id"]] += 1
            board_cnt[r["board_stop_id"]] += 1
            od_cnt[r["board_stop_id"]][r["alight_stop_id"]] += 1
            route_alight[(r["route_id"], r["alight_stop_id"])] += 1
            route_tot[r["route_id"]] += 1
        total = float(resolved.height)
        ride_dists = []
        for r in resolved.to_dicts():
            oc = self._cat.coords.get(r["board_stop_id"])
            ec = self._cat.coords.get(r["alight_stop_id"])
            if oc and ec:
                ride_dists.append(haversine_m(oc[0], oc[1], ec[0], ec[1]))
        if ride_dists:
            ride_med = float(pl.Series(ride_dists).median())
            logs = [math.log(max(50.0, d)) for d in ride_dists]
            ride_med_log = float(sum(logs) / len(logs))
            ride_sigma = max(0.25, float(pl.Series(logs).std() or 0.6))
        else:
            ride_med, ride_med_log, ride_sigma = 0.0, 0.0, 0.6

        ev_by_run: dict[str, list[dict]] = defaultdict(list)
        for r in self._events.to_dicts():
            ev_by_run[r["run_id"]].append(r)
        for v in ev_by_run.values():
            v.sort(key=lambda x: x["run_pos"])

        fallback = {"terminal_fallback", "prior_fallback"}
        affected = df.filter(pl.col("alight_method").is_in(list(fallback)))
        if affected.height == 0:
            return df, 0
        decay = float(self.p.get("hop_prior_decay", 4.0))
        # per-card context: a two-tap card's first boarding stop is a strong
        # destination candidate when the day ends where it began
        taps = self.out.get("taps")
        meta: dict[str, tuple[int, str]] = {}
        if taps is not None and taps.height:
            for cid, sub in taps.filter(pl.col("status") == "valid").group_by("card_id"):
                sub = sub.sort("tap_time")
                meta[cid[0]] = (sub.height, sub["board_stop_id"][0])
        updates: dict[str, dict] = {}
        for r in affected.to_dicts():
            cands = [e for e in ev_by_run.get(r["run_id"], [])
                     if e["run_pos"] > int(r["board_event_pos"])]
            if not cands:
                continue
            origin = r["board_stop_id"]
            n_card, first_board = meta.get(r["card_id"], (0, ""))
            anchored = n_card == 2 and first_board == origin
            best, best_score = None, -1e9
            for e in cands:
                hop = e["run_pos"] - int(r["board_event_pos"])
                p_od = od_cnt.get(origin, {}).get(e["stop_id"], 0) / total
                p_al = alight_cnt.get(e["stop_id"], 0) / total
                p_bd = board_cnt.get(e["stop_id"], 0) / total
                # route-local attractiveness: a stop that is heavily used
                # for boarding on *this* route is also a likely destination
                p_route = (route_alight.get((r["route_id"], e["stop_id"]), 0)
                           / route_tot.get(r["route_id"], 1))
                # distance evidence: real bus rides cluster around a typical
                # length, which rules out both the next stop and the far end
                ride_d = None
                oc = self._cat.coords.get(origin)
                ec = self._cat.coords.get(e["stop_id"])
                if oc and ec and ride_med > 0:
                    ride_d = haversine_m(oc[0], oc[1], ec[0], ec[1])
                dscore = 0.0
                if ride_d is not None:
                    dscore = -(((math.log(max(50.0, ride_d)) - ride_med_log) / ride_sigma) ** 2) / 2
                score = (self.p.get("prior_w_od", 1.0) * math.log(p_od + 0.0005)
                         + self.p.get("prior_w_alight", 0.7) * math.log(p_al + 0.0005)
                         + self.p.get("prior_w_board", 0.7) * math.log(p_bd + 0.0005)
                         + self.p.get("prior_w_route", 0.8) * math.log(p_route + 0.0005)
                         - hop / decay
                         + self.p.get("prior_w_dist", 1.5) * dscore
                         + (2.4 if (anchored and e["stop_id"] == origin) else 0.0))
                if score > best_score:
                    best, best_score = e, score
            if best is None:
                continue
            sid = best["stop_id"]
            lat, lon = self._cat.coords.get(sid, (None, None))
            updates[r["tap_id"]] = {
                "alight_stop_id": sid, "alight_stop_name": best["stop_name"],
                "alight_time": best["arr_time"], "alight_event_pos": best["run_pos"],
                "alight_lat": lat, "alight_lon": lon,
                "alight_method": "od_prior",
                "score": round(best_score, 2),
                "ride_sec": int((best["arr_time"] - r["board_time"]).total_seconds())
                if r.get("board_time") else None,
            }
        if not updates:
            return df, 0
        rows = []
        for r in df.to_dicts():
            u = updates.get(r["tap_id"])
            if u:
                r.update(u)
                r["confidence"] = "low"
                r["uncertain"] = True
            rows.append(r)
        return pl.DataFrame(rows, infer_schema_length=None), len(updates)

    def _score_candidate(self, t, e, next_t, first_board, anchor_ok, cat,
                         walk_speed, walk_max, slack, max_ride, last_stop_id, p,
                         link_max: float = 5400.0, n_card_taps: int = 0) -> tuple:
        """Score one downstream stop as the alighting point.

        Evidence, strongest first:
          * same stop as the next boarding tap, with a feasible waiting time
          * alighting near the next boarding tap, with a feasible walk
          * the day's first boarding stop (passenger returning home)
          * distance-to-next-boarding as a soft signal when nothing is exact
        """
        cand_stop = e["stop_id"]
        hops = int(e["run_pos"]) - int(t["run_pos"] or 0)
        ride = (e["arr_time"] - t["board_time"]).total_seconds() if t.get("board_time") else 0
        score = 2.0 + 2.0 * min(1.0, hops / 3.0)
        method = "prior"

        if next_t is not None:
            gap_total = (next_t["tap_time"] - t["tap_time"]).total_seconds()
            decay = 1.0 if gap_total <= 90 * 60 else (0.72 if gap_total <= 4 * 3600 else 0.55)
            # A walk/transfer link only makes sense when the next tap happens
            # soon after this ride.  Hours later the passenger went somewhere
            # else in between, so proximity of the stops carries no information.
            linked = gap_total <= link_max
            n_stop = next_t["board_stop_id"]
            arr = e["arr_time"]
            if n_stop and cand_stop == n_stop:
                gap = (next_t["tap_time"] - arr).total_seconds()
                if gap >= -slack:
                    # same physical stop is the strongest possible evidence
                    score += 80.0 * decay
                    method = "next_board_same_stop"
                else:
                    score -= 45.0
            elif n_stop and linked:
                c1, c2 = cat.coords.get(cand_stop), cat.coords.get(n_stop)
                if c1 and c2:
                    d = haversine_m(c1[0], c1[1], c2[0], c2[1])
                    gap = (next_t["tap_time"] - arr).total_seconds()
                    walk = d / walk_speed
                    if d <= walk_max and gap >= walk - slack and ride <= max_ride:
                        # Distance is the discriminator: a 50 m walk beats
                        # 700 m, but neither outranks an exact stop match.
                        prox = 1.0 - d / walk_max
                        score += (14.0 + 20.0 * prox * prox) * decay
                        method = "next_board_walk"
                    elif d <= walk_max:
                        score -= 30.0

        if anchor_ok and cand_stop == first_board and ride <= max_ride:
            # A return anchor is only trustworthy when the day has exactly one
            # earlier trip; with more trips the passenger often went elsewhere.
            score += float(p.get("anchor_bonus", 60.0)) if n_card_taps == 2 else 25.0
            if method == "prior":
                method = "return_anchor"

        if cand_stop == last_stop_id:
            score += 5.0
        if hops == 1:
            score -= 3.0
        if ride > max_ride:
            score -= 25.0
        return score, method

    def _leg_row(self, t, cat, e, method, conf, uncertain, score, alight_time,
                 alight_pos, n_candidates) -> dict:
        sid = e["stop_id"] if e else None
        lat, lon = cat.coords.get(sid, (None, None)) if sid else (None, None)
        bl, bo = t.get("board_lat"), t.get("board_lon")
        dist = round(haversine_m(bl, bo, lat, lon), 1) if None not in (bl, bo, lat, lon) else None
        ride = None
        if alight_time and t["board_time"]:
            ride = int((alight_time - t["board_time"]).total_seconds())
        return {
            "card_id": t["card_id"], "tap_id": t["tap_id"], "tap_time": t["tap_time"],
            "route_id": t["route_id"], "card_type": t.get("card_type"),
            "vehicle_id": t["vehicle_id"], "run_id": t["run_id"],
            "direction": t["direction"], "board_event_pos": t["run_pos"],
            "alight_event_pos": alight_pos,
            "board_stop_id": t["board_stop_id"],
            "board_stop_name": cat.names.get(t["board_stop_id"], t["board_stop_id"]),
            "alight_stop_id": sid, "alight_stop_name": cat.names.get(sid, sid) if sid else None,
            "board_time": t["board_time"], "alight_time": alight_time, "ride_sec": ride,
            "alight_method": method, "confidence": conf, "uncertain": uncertain,
            "score": round(score, 2), "candidate_count": n_candidates,
            "board_lat": bl, "board_lon": bo, "alight_lat": lat, "alight_lon": lon,
            "ride_dist_m": dist, "is_transfer": False, "transfer_prev_stop": None,
            "transfer_walk_m": None, "transfer_wait_sec": None,
            "journey_id": None, "leg_index": 0, "period": None,
        }

    # ============================================================= transfers
    def _transfers_and_journeys(self, legs: pl.DataFrame):
        if legs.height == 0:
            return legs, {"transfers": 0, "transfer_rate": 0.0}
        p = self.p
        win = float(p["transfer_window_min"]) * 60
        walk_max = float(p["transfer_walk_max_m"])
        slack = float(p["timing_slack_sec"])
        rows = legs.sort(["card_id", "board_time"]).to_dicts()
        out = []
        prev_by_card: dict[str, dict] = {}
        journey_seq: dict[str, int] = defaultdict(int)
        transfers = 0
        for r in rows:
            r = dict(r)
            prev = prev_by_card.get(r["card_id"])
            is_transfer = False
            walk = wait = None
            if prev and prev.get("alight_time") and r["board_time"]:
                gap = (r["board_time"] - prev["alight_time"]).total_seconds()
                a = (prev.get("alight_lat"), prev.get("alight_lon"))
                b = (r.get("board_lat"), r.get("board_lon"))
                dist = haversine_m(a[0], a[1], b[0], b[1]) if None not in (*a, *b) else 0.0
                if -slack <= gap <= win and dist <= walk_max:
                    is_transfer = True
                    walk, wait = round(dist, 1), int(gap)
                    transfers += 1
            if not is_transfer:
                journey_seq[r["card_id"]] += 1
            r["is_transfer"] = is_transfer
            r["transfer_prev_stop"] = prev["alight_stop_id"] if is_transfer else None
            r["transfer_walk_m"] = walk
            r["transfer_wait_sec"] = wait
            r["journey_id"] = f"{r['card_id']}-J{journey_seq[r['card_id']]:06d}"
            r["leg_index"] = (prev["leg_index"] + 1) if (is_transfer and prev) else 0
            out.append(r)
            prev_by_card[r["card_id"]] = r
        df = pl.DataFrame(out, infer_schema_length=None).sort(["card_id", "board_time"])
        return df, {"transfers": transfers,
                    "transfer_rate": round(transfers / max(1, df.height), 4)}

    def _attach_period(self, legs: pl.DataFrame) -> pl.DataFrame:
        if legs.height == 0:
            return legs
        return legs.with_columns([
            build_period_expr("board_time", self.p["time_periods"]).alias("period"),
            pl.col("board_time").dt.date().alias("service_date"),
        ])

    # =============================================================== matrices
    def _od_matrix(self, legs: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
        if legs.height == 0:
            return pl.DataFrame(), {"od_pairs": 0, "total": 0}
        agg = legs.filter(pl.col("alight_stop_id").is_not_null()).with_columns([
            pl.col("board_stop_name").alias("origin_name"),
            pl.col("alight_stop_name").alias("dest_name"),
        ]).group_by(
            ["board_stop_id", "alight_stop_id", "origin_name", "dest_name",
             "period", "route_id", "direction"]
        ).agg([
            pl.len().alias("passengers"),
            pl.col("card_id").n_unique().alias("travellers"),
            pl.col("uncertain").mean().alias("uncertain_share"),
            pl.col("board_lat").first(), pl.col("board_lon").first(),
            pl.col("alight_lat").first(), pl.col("alight_lon").first(),
            pl.col("ride_sec").mean().alias("avg_ride_sec"),
            pl.col("ride_dist_m").mean().alias("avg_ride_dist_m"),
        ]).with_columns(pl.col("uncertain_share").round(3))
        summary = {
            "od_pairs": agg.height, "total": int(agg["passengers"].sum()),
            "periods": sorted(agg["period"].unique().to_list()),
        }
        return agg.sort("passengers", descending=True), summary

    def _stop_flow(self, legs: pl.DataFrame, events: pl.DataFrame,
                   cat: StopCatalog):
        base = events.select([
            "run_id", "run_pos", "vehicle_id", "route_id", "direction", "stop_id",
            "arr_time", "dep_time",
        ])
        if legs.height:
            b = legs.filter(pl.col("alight_stop_id").is_not_null()).select([
                "run_id", pl.col("board_event_pos").cast(pl.Int64).alias("run_pos"),
                pl.col("period"),
            ]).group_by(["run_id", "run_pos", "period"]).agg(
                pl.len().cast(pl.Int64).alias("boardings")).with_columns(
                pl.lit(0, dtype=pl.Int64).alias("alightings")).select(
                ["run_id", "run_pos", "period", "boardings", "alightings"])
            al = legs.filter(pl.col("alight_stop_id").is_not_null()).select([
                "run_id", pl.col("alight_event_pos").cast(pl.Int64).alias("run_pos"),
                pl.col("period"),
            ]).group_by(["run_id", "run_pos", "period"]).agg(
                pl.len().cast(pl.Int64).alias("alightings")).with_columns(
                pl.lit(0, dtype=pl.Int64).alias("boardings")).select(
                ["run_id", "run_pos", "period", "boardings", "alightings"])
            delta = pl.concat([b, al], how="vertical").group_by(
                ["run_id", "run_pos", "period"]).agg([
                pl.col("boardings").sum(), pl.col("alightings").sum()])
        else:
            delta = pl.DataFrame(schema={
                "run_id": pl.Utf8, "run_pos": pl.UInt32, "period": pl.Utf8,
                "boardings": pl.Int64, "alightings": pl.Int64})
        flows = base.join(delta, on=["run_id", "run_pos"], how="left").with_columns([
            pl.col("boardings").fill_null(0), pl.col("alightings").fill_null(0),
            pl.col("period").fill_null("未分时段"),
        ]).with_columns(
            (pl.col("boardings") - pl.col("alightings")).alias("_net")
        ).sort(["run_id", "run_pos"]).with_columns(
            pl.col("_net").cum_sum().over("run_id").alias("load")
        ).with_columns(
            (pl.col("load") / float(self.p["seat_capacity"])).alias("load_factor"))
        # section load: the load carried between this stop and the next one
        max_lf = pl.col("load_factor").max().over(["run_id", "period"])
        flows = flows.with_columns(max_lf.alias("_run_peak_lf"))

        cap = float(self.p["seat_capacity"])
        agg = flows.group_by(
            ["route_id", "direction", "period", "stop_id", "run_pos"]
        ).agg([
            pl.col("boardings").sum().alias("boardings"),
            pl.col("alightings").sum().alias("alightings"),
            pl.col("load").mean().alias("avg_load"),
            pl.col("load").max().alias("peak_load"),
            pl.col("load_factor").mean().alias("load_factor"),
            pl.col("_run_peak_lf").max().alias("peak_load_factor"),
            pl.col("run_id").n_unique().alias("trips"),
        ]).with_columns([
            pl.col("avg_load").round(1),
            pl.col("load_factor").round(3),
            pl.col("peak_load_factor").round(3),
            pl.col("peak_load").cast(pl.Int64),
        ])
        agg = agg.join(cat.rows, on="stop_id", how="left")
        # stop-level board/alight totals across all routes (map view)
        board = flows.group_by(["stop_id", "period"]).agg([
            pl.col("boardings").sum().alias("boardings"),
            pl.col("alightings").sum().alias("alightings"),
            pl.col("run_id").n_unique().alias("trips"),
            pl.col("load_factor").max().alias("peak_load_factor"),
        ]).join(cat.rows, on="stop_id", how="left")
        # section table for the section-load chart
        sec = flows.sort(["route_id", "direction", "run_id", "run_pos"]).with_columns([
            pl.col("stop_id").shift(-1).over(["run_id"]).alias("_next_stop"),
            pl.col("run_pos").shift(-1).over(["run_id"]).alias("_next_pos"),
        ]).filter(pl.col("_next_pos").is_not_null())
        sections = sec.group_by(
            ["route_id", "direction", "period", "stop_id", "_next_stop"]
        ).agg([
            pl.col("load").mean().alias("avg_load"),
            pl.col("load").max().alias("peak_load"),
            (pl.col("load") / cap).mean().alias("load_factor"),
            pl.col("run_id").n_unique().alias("trips"),
        ]).with_columns([
            pl.col("avg_load").round(1),
            pl.col("peak_load").cast(pl.Int64),
            pl.col("load_factor").round(3),
        ]).rename({"stop_id": "from_stop", "_next_stop": "to_stop"})
        sections = sections.join(
            cat.rows.select(["stop_id", "stop_name", "lat", "lon"]),
            left_on="from_stop", right_on="stop_id", how="left")
        sections = sections.join(
            cat.rows.select(["stop_id", "stop_name", "lat", "lon"]).rename({
                "stop_id": "to_stop", "stop_name": "to_name", "lat": "to_lat",
                "lon": "to_lon"}), on="to_stop", how="left")
        return agg, board, sections

    def _stop_match_table(self, events: pl.DataFrame, taps: pl.DataFrame,
                          cat: StopCatalog) -> pl.DataFrame:
        boards = taps.filter(pl.col("status") == "valid").group_by("board_stop_id").agg([
            pl.len().alias("board_taps"),
            pl.col("match_delta_sec").abs().mean().alias("avg_match_delta"),
        ]).rename({"board_stop_id": "stop_id"})
        ev = events.group_by("stop_id").agg([
            pl.len().alias("stop_events"),
            pl.col("snap_m").mean().alias("avg_snap_m"),
            pl.col("vehicle_id").n_unique().alias("vehicles"),
        ])
        out = cat.rows.join(ev, on="stop_id", how="left").join(boards, on="stop_id", how="left")
        return out.with_columns([
            pl.col("stop_events").fill_null(0), pl.col("board_taps").fill_null(0),
            pl.col("vehicles").fill_null(0),
            pl.col("avg_snap_m").round(1), pl.col("avg_match_delta").round(1),
        ]).sort("board_taps", descending=True)

    # ================================================================ metrics
    def _build_metrics(self, tapped, legs, events, od, flows,
                       gps_stats, match_stats, chain_stats, transfer_stats) -> dict:
        by_status = {r["status"]: r["n"] for r in
                     tapped.group_by("status").len().rename({"len": "n"}).to_dicts()}
        n_valid = by_status.get("valid", 0)
        n_dup = by_status.get("duplicate", 0)
        n_inv = by_status.get("invalid", 0)
        inferred = int(legs["alight_stop_id"].is_not_null().sum()) if legs.height else 0
        uncertain = int(legs["uncertain"].sum()) if legs.height else 0
        conf = {k: int((legs["confidence"] == k).sum()) for k in ("high", "medium", "low")} \
            if legs.height else {"high": 0, "medium": 0, "low": 0}
        trip_counts = events.group_by("run_id").agg(
            pl.col("route_id").first()).group_by("route_id").agg(pl.len().alias("trips"))
        period_od = legs.filter(pl.col("alight_stop_id").is_not_null()).group_by("period").agg([
            pl.len().alias("trips"), pl.col("card_id").n_unique().alias("cards")
        ]).sort("period").to_dicts() if legs.height else []
        top_od = od.head(12).select(
            ["origin_name", "dest_name", "period", "passengers"]).to_dicts() if od.height else []
        peak = flows.sort("avg_load", descending=True).head(5).select(
            ["route_id", "direction", "period", "stop_name", "avg_load",
             "peak_load_factor"]).to_dicts() if flows.height else []
        busiest = None
        if flows.height:
            b = flows.group_by("stop_name").agg(
                pl.col("boardings").sum().alias("boardings")).sort(
                "boardings", descending=True)
            if b.height:
                busiest = b.row(0, named=True)
        return {
            "taps": tapped.height, "taps_valid": n_valid, "taps_duplicate": n_dup,
            "taps_invalid": n_inv, "taps_matched": match_stats["matched"],
            "match_rate": match_stats["match_rate"],
            "extended_matches": match_stats["extended_matches"],
            "gps": gps_stats,
            "legs": legs.height if legs.height else 0,
            "alight_inferred": inferred,
            "alight_rate": round(inferred / max(1, legs.height), 4) if legs.height else 0,
            "uncertain": uncertain,
            "uncertain_rate": round(uncertain / max(1, legs.height), 4) if legs.height else 0,
            "confidence": conf,
            "alight_methods": chain_stats.get("methods", {}),
            "chain_conflicts": chain_stats.get("chain_conflicts", 0),
            "transfers": transfer_stats.get("transfers", 0),
            "transfer_rate": transfer_stats.get("transfer_rate", 0),
            "journeys": int(legs["journey_id"].n_unique()) if legs.height else 0,
            "cards": int(legs["card_id"].n_unique()) if legs.height else 0,
            "od_pairs": od.height if od.height else 0,
            "od_total": int(od["passengers"].sum()) if od.height else 0,
            "stop_events": events.height,
            "vehicles": gps_stats["vehicles"],
            "trips_tracked": int(events["run_id"].n_unique()) if events.height else 0,
            "routes": trip_counts.height,
            "route_trips": {r["route_id"]: r["trips"] for r in trip_counts.to_dicts()},
            "max_load": float(flows["peak_load"].max()) if flows.height else 0,
            "avg_load_factor": round(float(flows["load_factor"].mean()), 3)
            if flows.height else 0.0,
            "seat_capacity": self.p["seat_capacity"],
            "busiest_stop": busiest, "top_od": top_od, "peak_sections": peak,
            "period_od": period_od,
            "periods": [p["name"] for p in self.p["time_periods"]],
            "warnings": list(self.warnings),
            "elapsed_sec": round(time.time() - self._t0, 2),
            "params": self.p,
        }

    # =============================================================== accuracy
    def _accuracy(self, truth: pl.DataFrame, taps: pl.DataFrame,
                  legs: pl.DataFrame, cat: StopCatalog) -> dict:
        truth = truth.rename({"is_transfer": "truth_transfer"})
        for c in ("is_duplicate", "is_invalid"):
            if c in truth.columns:
                truth = truth.with_columns(
                    pl.col(c).cast(pl.Utf8).str.to_lowercase()
                    .is_in(["true", "1", "是", "yes"]).fill_null(False).alias(c))
            else:
                truth = truth.with_columns(pl.lit(False).alias(c))
        truth = truth.filter(pl.col("true_alight_stop_id").is_not_null())
        if truth.height == 0:
            return {"available": False}
        valid_truth = truth.filter(~pl.col("is_duplicate") & ~pl.col("is_invalid"))
        tap_ids = set(taps["tap_id"].to_list())
        ref = valid_truth.filter(pl.col("tap_id").is_in(list(tap_ids)))
        inferred = legs.select([
            "tap_id", pl.col("board_stop_id").alias("i_board"),
            pl.col("alight_stop_id").alias("i_alight"),
            pl.col("vehicle_id").alias("i_vehicle"),
            pl.col("direction").alias("i_direction"),
            pl.col("alight_time").alias("i_alight_time"),
            pl.col("is_transfer").alias("i_transfer"),
            pl.col("uncertain"), pl.col("confidence"), pl.col("alight_method"),
        ])
        j = ref.join(inferred, on="tap_id", how="left")
        n = j.height
        got = j.filter(pl.col("i_alight").is_not_null())
        alight_ok = (got["i_alight"] == got["true_alight_stop_id"])
        # Adjacent-stop tolerance: a neighbouring stop is usually the correct
        # trip with a small positional error, which matters operationally.
        tol = float(self.p.get("accuracy_adjacent_tolerance_m", 700.0))
        within = []
        for r in got.select(["i_board", "i_alight", "true_alight_stop_id"]).to_dicts():
            if r["i_alight"] == r["true_alight_stop_id"]:
                within.append(True)
                continue
            a = cat.coords.get(r["i_alight"])
            b = cat.coords.get(r["true_alight_stop_id"])
            within.append(bool(a and b and haversine_m(a[0], a[1], b[0], b[1]) <= tol))
        adjacent_acc = (sum(within) / len(within)) if within else None
        board_ok = (got["i_board"] == got["true_board_stop_id"])
        veh_ok = (got["i_vehicle"] == got["true_vehicle_id"])
        dir_ok = (got["i_direction"].cast(pl.Int64) == got["true_direction"].cast(pl.Int64))
        conf_rows = got.filter(~pl.col("uncertain"))
        conf_ok = (conf_rows["i_alight"] == conf_rows["true_alight_stop_id"])
        mae_s = (got["i_alight_time"] - got["true_alight_time"]).dt.total_seconds()
        mae_s = mae_s.drop_nulls()
        # per-method accuracy
        per_method = []
        for m in got["alight_method"].drop_nulls().unique().to_list():
            sub = got.filter(pl.col("alight_method") == m)
            ok = (sub["i_alight"] == sub["true_alight_stop_id"])
            per_method.append({"method": m, "n": sub.height,
                               "acc": round(float(ok.mean()), 4)})
        per_method.sort(key=lambda x: -x["n"])
        # transfer detection
        tpos = int(j["truth_transfer"].fill_null(False).sum())
        ipos = int(j["i_transfer"].fill_null(False).sum())
        both = int((j["truth_transfer"].fill_null(False)
                    & j["i_transfer"].fill_null(False)).sum())
        prec = both / ipos if ipos else None
        rec = both / tpos if tpos else None
        f1 = (2 * prec * rec / (prec + rec)) if prec and rec else None
        # cleaning recall
        dup_truth = truth.filter(pl.col("is_duplicate"))
        inv_truth = truth.filter(pl.col("is_invalid"))
        dup_flagged_ids = set(taps.filter(pl.col("status") == "duplicate")["tap_id"].to_list())
        inv_flagged_ids = set(taps.filter(pl.col("status") == "invalid")["tap_id"].to_list())
        dup_hit = sum(1 for t in dup_truth["tap_id"].to_list() if t in dup_flagged_ids)
        inv_hit = sum(1 for t in inv_truth["tap_id"].to_list() if t in inv_flagged_ids)
        # hour-level OD similarity (cosine on OD pairs) is computed elsewhere if needed
        return {
            "available": True,
            "truth_rows": truth.height,
            "reference_legs": n,
            "evaluated_legs": got.height,
            "tap_coverage": round(got.height / max(1, n), 4),
            "alight_stop_accuracy": round(float(alight_ok.mean()), 4) if got.height else None,
            "alight_stop_accuracy_adjacent": round(adjacent_acc, 4)
            if adjacent_acc is not None else None,
            "adjacent_tolerance_m": tol,
            "alight_stop_accuracy_confident": round(float(conf_ok.mean()), 4)
            if conf_rows.height else None,
            "confident_share": round(conf_rows.height / max(1, got.height), 4),
            "board_stop_accuracy": round(float(board_ok.mean()), 4) if got.height else None,
            "vehicle_accuracy": round(float(veh_ok.mean()), 4) if got.height else None,
            "direction_accuracy": round(float(dir_ok.mean()), 4) if got.height else None,
            "alight_time_mae_sec": round(float(mae_s.mean()), 1) if mae_s.len() else None,
            "alight_time_p90_sec": round(float(mae_s.quantile(0.9)), 1) if mae_s.len() else None,
            "per_method": per_method,
            "transfer_precision": round(prec, 4) if prec is not None else None,
            "transfer_recall": round(rec, 4) if rec is not None else None,
            "transfer_f1": round(f1, 4) if f1 else None,
            "duplicate_recall": round(dup_hit / dup_truth.height, 4)
            if dup_truth.height else None,
            "duplicate_flagged": len(dup_flagged_ids),
            "invalid_recall": round(inv_hit / inv_truth.height, 4)
            if inv_truth.height else None,
            "invalid_flagged": len(inv_flagged_ids),
            "errors": self._error_table(got, cat),
        }

    @staticmethod
    def _error_table(got: pl.DataFrame, cat: StopCatalog) -> list[dict]:
        bad = got.filter(pl.col("i_alight") != got["true_alight_stop_id"])
        if bad.height == 0:
            return []
        tab = bad.group_by(["i_board", "i_alight", "true_alight_stop_id"]).len().rename(
            {"len": "n"}).sort("n", descending=True).head(10)
        out = []
        for r in tab.to_dicts():
            out.append({
                "board": cat.names.get(r["i_board"], r["i_board"]),
                "inferred": cat.names.get(r["i_alight"], r["i_alight"]),
                "actual": cat.names.get(r["true_alight_stop_id"], r["true_alight_stop_id"]),
                "n": r["n"],
            })
        return out

    # ================================================================ persist
    def save(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, df in self.out.items():
            if not isinstance(df, pl.DataFrame):
                continue
            try:
                df.write_parquet(out_dir / f"{name}.parquet")
            except Exception as exc:  # pragma: no cover
                self.warnings.append(f"保存 {name} 失败: {exc}")
        payload = {k: v for k, v in self.metrics.items() if k != "params"}
        (out_dir / "metrics.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def run_pipeline(run_id: str, frames: dict[str, pl.DataFrame], params: dict,
                 runs_dir: Path, progress: Progress = _noop) -> dict:
    pipe = Pipeline(params, frames, progress)
    # stop_events is produced during run(); alighting inference needs the
    # segmented events, so hand it in through the instance after segmentation.
    metrics = pipe.run()
    pipe.save(runs_dir / run_id)
    return {"summary": {k: v for k, v in metrics.items() if k != "params"},
            "elapsed_sec": metrics.get("elapsed_sec")}
