"""Small shared helpers: distances, JSON-safe conversion, time periods."""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Iterable

import polars as pl

EARTH_R_M = 6371008.8  # mean Earth radius, metres


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_M * math.asin(math.sqrt(min(1.0, a)))


def parse_hhmm(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def build_period_expr(column: str, periods: Iterable[dict]) -> pl.Expr:
    """Map a datetime column to a period label using local minute-of-day.

    Periods whose end is '24:00' include everything after their start.
    Note: dt.hour()/dt.minute() are Int8 in Polars 2.x, so the arithmetic
    must be widened first -- 08:00 would otherwise overflow to a negative
    minute count.
    """
    whens = []
    minute = (pl.col(column).dt.hour().cast(pl.Int32) * 60
              + pl.col(column).dt.minute().cast(pl.Int32))
    for p in periods:
        start = parse_hhmm(p["start"])
        end = parse_hhmm(p["end"])
        whens.append(((minute >= start) & (minute < end), pl.lit(p["name"])))
    if not whens:
        return pl.lit("全天")
    expr = pl.when(whens[0][0]).then(whens[0][1])
    for cond, val in whens[1:]:
        expr = expr.when(cond).then(val)
    return expr.otherwise(pl.lit("未分时段"))


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 4)
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def df_records(df: pl.DataFrame, limit: int | None = None) -> list[dict]:
    if limit is not None:
        df = df.head(limit)
    return [{k: _jsonable(v) for k, v in row.items()} for row in df.to_dicts()]


def df_columns(df: pl.DataFrame) -> list[dict]:
    n = len(df)
    return [
        {"name": name, "dtype": str(dtype), "non_null": n - int(df[name].null_count())}
        for name, dtype in zip(df.columns, df.dtypes)
    ]


def jsonable(v: Any) -> Any:
    return _jsonable(v)


def to_num(v: Any, default: float | None = None) -> float | None:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def validate_params(params: dict) -> list[str]:
    errs: list[str] = []
    bounds = {
        "gps_speed_max_kmh": (10, 200),
        "gps_drift_max_m": (10, 2000),
        "stop_snap_radius_m": (10, 500),
        "dwell_speed_kmh": (0, 60),
        "min_dwell_sec": (0, 300),
        "boarding_tap_tolerance_sec": (5, 900),
        "boarding_search_sec": (30, 1800),
        "dup_tap_window_sec": (0, 600),
        "transfer_window_min": (1, 120),
        "transfer_walk_max_m": (50, 3000),
        "return_anchor_gap_min": (1, 240),
        "walk_speed_mps": (0.3, 3.0),
        "timing_slack_sec": (0, 600),
        "max_ride_min": (5, 300),
        "chain_link_window_min": (10, 360),
        "hop_prior_decay": (1.0, 30.0),
        "seat_capacity": (10, 300),
    }
    for key, (lo, hi) in bounds.items():
        val = to_num(params.get(key))
        if val is None:
            errs.append(f"参数 {key} 缺失或非数字")
        elif not lo <= val <= hi:
            errs.append(f"参数 {key} 应在 {lo} ~ {hi} 之间")
    periods = params.get("time_periods") or []
    if not periods:
        errs.append("至少需要一个时段")
    else:
        prev_end = -1
        for p in periods:
            try:
                s, e = parse_hhmm(p["start"]), parse_hhmm(p["end"])
            except Exception:
                errs.append(f"时段 {p.get('name')} 时间格式应为 HH:MM")
                continue
            if s >= e:
                errs.append(f"时段 {p.get('name')} 起止时间非法")
            if s < prev_end:
                errs.append(f"时段 {p.get('name')} 与上一时段重叠")
            prev_end = max(prev_end, e)
    return errs
