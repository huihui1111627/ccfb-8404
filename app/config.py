"""Global configuration and filesystem helpers."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ODP_DATA_DIR", BASE_DIR / "data")).resolve()
DATASETS_DIR = DATA_DIR / "datasets"
RUNS_DIR = DATA_DIR / "runs"
DB_PATH = DATA_DIR / "app.db"

for _p in (DATA_DIR, DATASETS_DIR, RUNS_DIR):
    _p.mkdir(parents=True, exist_ok=True)

APP_NAME = "公交客流 OD 推算平台"
APP_VERSION = "1.0.0"

# Maximum rows returned to a browser table in one request.
MAX_PAGE_SIZE = 500

# Default inference / cleaning parameters. Kept in one place so the API can
# serve them to the UI and the pipeline can fall back when a key is missing.
DEFAULT_PARAMS: dict = {
    # GPS cleaning
    "gps_speed_max_kmh": 80.0,
    "gps_drift_max_m": 120.0,
    "stop_snap_radius_m": 90.0,
    "dwell_speed_kmh": 4.0,
    "min_dwell_sec": 8.0,
    # boarding inference
    "boarding_tap_tolerance_sec": 75.0,
    "boarding_search_sec": 240.0,
    # duplicate taps
    "dup_tap_window_sec": 90.0,
    # trip chain / alighting
    "transfer_window_min": 25.0,
    "transfer_walk_max_m": 800.0,
    "return_anchor_gap_min": 45.0,
    "walk_speed_mps": 1.2,
    "timing_slack_sec": 90.0,
    "max_ride_min": 120.0,
    "use_return_anchor": True,
    "use_hop_prior": True,
    "hop_prior_decay": 6.0,
    "prior_w_od": 1.5,
    "prior_w_alight": 0.4,
    "prior_w_board": 0.0,
    "prior_w_route": 0.0,
    "prior_w_dist": 4.0,
    "anchor_bonus": 60.0,
    "chain_link_window_min": 90,
    # operations
    "seat_capacity": 80,
    "time_periods": [
        {"name": "早间", "start": "05:00", "end": "07:00"},
        {"name": "早高峰", "start": "07:00", "end": "09:00"},
        {"name": "平峰", "start": "09:00", "end": "17:00"},
        {"name": "晚高峰", "start": "17:00", "end": "19:00"},
        {"name": "夜间", "start": "19:00", "end": "24:00"},
    ],
}

PARAM_LABELS = {
    "gps_speed_max_kmh": "GPS 速度上限 (km/h)",
    "gps_drift_max_m": "GPS 漂移阈值 (m)",
    "stop_snap_radius_m": "站点吸附半径 (m)",
    "dwell_speed_kmh": "停站判定速度 (km/h)",
    "min_dwell_sec": "最短停站时长 (s)",
    "boarding_tap_tolerance_sec": "刷卡-停站时间容差 (s)",
    "boarding_search_sec": "刷卡-车辆最大搜索时差 (s)",
    "dup_tap_window_sec": "重复刷卡窗口 (s)",
    "transfer_window_min": "换乘时间阈值 (min)",
    "transfer_walk_max_m": "换乘步行距离阈值 (m)",
    "return_anchor_gap_min": "返程锚点最小间隔 (min)",
    "walk_speed_mps": "步行速度 (m/s)",
    "timing_slack_sec": "时刻容差 (s)",
    "max_ride_min": "最长乘车时间 (min)",
    "use_return_anchor": "启用返程锚点",
    "use_hop_prior": "启用跳数先验兜底",
    "chain_link_window_min": "出行链关联时间窗 (min)",
    "hop_prior_decay": "跳数先验衰减长度 (站)",
    "prior_w_od": "先验权重 · OD",
    "prior_w_alight": "先验权重 · 站点吸引",
    "prior_w_board": "先验权重 · 站点发生",
    "prior_w_route": "先验权重 · 线路",
    "prior_w_dist": "先验权重 · 乘车距离",
    "anchor_bonus": "返程锚点加分",
    "seat_capacity": "车辆定员 (人)",
}
