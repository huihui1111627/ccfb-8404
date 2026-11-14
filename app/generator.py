"""Synthetic bus network generator with ground-truth alighting stops.

The feed deliberately mimics the three separate source systems an operator
has: AFC boarding taps, AVL/GPS positions, and a static route/stop table.
A fourth file holds ground truth, which never enters the inference pipeline --
it is only joined afterwards to score accuracy.

Demand is generated as realistic activity chains (home -> work -> home, with
optional transfers and midday trips), so that trip-chain inference and the
return-anchor heuristic have something meaningful to find.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl

SERVICE_DATE = datetime(2026, 11, 9)
RNG_SEED = 20261109

NODE_NAMES = [
    ["火车北站", "人民北路", "梁家巷", "驷马桥", "动物园", "昭觉寺"],
    ["西门车站", "通惠门", "文殊院", "春熙路", "牛市口", "东大路"],
    ["金沙遗址", "文化宫", "天府广场", "盐市口", "九眼桥", "东湖公园"],
    ["清水河", "武侯祠", "锦里", "华西坝", "四川大学", "三官堂"],
    ["双楠", "红牌楼", "神仙树", "桐梓林", "火车南站", "环球中心"],
]

LAT0, LON0 = 30.7000, 104.0200
DLAT, DLON = -0.0110, 0.0125

ROUTE_DEFS: dict[str, dict] = {
    "R1": {"name": "1路", "nodes": [(0, c) for c in range(6)], "n_veh": 4},
    "R2": {"name": "2路", "nodes": [(1, c) for c in range(6)], "n_veh": 4},
    "R3": {"name": "3路", "nodes": [(2, c) for c in range(6)], "n_veh": 4},
    "R4": {"name": "4路", "nodes": [(3, c) for c in range(6)], "n_veh": 4},
    "R5": {"name": "5路", "nodes": [(4, c) for c in range(6)], "n_veh": 4},
    "R6": {"name": "6路", "nodes": [(r, 0) for r in range(5)], "n_veh": 3},
    "R7": {"name": "7路", "nodes": [(r, 3) for r in range(5)], "n_veh": 3},
    "R8": {
        "name": "8路环线",
        "nodes": [(2, 1), (2, 2), (1, 2), (1, 3), (2, 3), (3, 3), (3, 2), (3, 1), (2, 1)],
        "n_veh": 3,
        "loop": True,
    },
}

DWELL_INTERMEDIATE = 26.0
DWELL_TERMINAL = 95.0
LAYOVER = 9 * 60.0
SERVICE_START = 6 * 3600
SERVICE_END = 22 * 3600 + 1800
GPS_INTERVAL = 10.0
WALK_SPEED = 1.25

HOUR_WEIGHTS = {
    6: 0.35, 7: 1.0, 8: 0.95, 9: 0.55, 10: 0.35, 11: 0.45, 12: 0.50,
    13: 0.35, 14: 0.30, 15: 0.35, 16: 0.50, 17: 1.0, 18: 0.90, 19: 0.50,
    20: 0.30, 21: 0.18, 22: 0.08,
}


def node_id(r: int, c: int) -> str:
    return f"S{r}{c}"


def node_latlon(r: int, c: int) -> tuple[float, float]:
    return LAT0 + r * DLAT, LON0 + c * DLON


def node_name(r: int, c: int) -> str:
    return NODE_NAMES[r][c]


@dataclass(frozen=True)
class StopDef:
    stop_id: str
    stop_name: str
    lat: float
    lon: float


@dataclass
class Trip:
    trip_id: str
    vehicle_id: str
    route_id: str
    direction: int
    stops: list[dict]
    start: datetime
    end: datetime


@dataclass
class Leg:
    leg_index: int
    journey_id: str
    trip: Trip
    board_stop: str
    board_seq: int
    board_time: datetime
    alight_stop: str
    alight_seq: int
    alight_time: datetime
    is_transfer: bool = False
    transfer_prev_trip: str | None = None
    walk_m: float = 0.0


@dataclass
class GeneratorResult:
    stops: pl.DataFrame
    taps: pl.DataFrame
    gps: pl.DataFrame
    truth: pl.DataFrame
    trips: pl.DataFrame
    summary: dict = field(default_factory=dict)
    drift_df: pl.DataFrame | None = None


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(min(1.0, a)))


class Generator:
    def __init__(self, seed: int = RNG_SEED, n_cards: int = 1800, n_random_journeys: int = 1500):
        self.rng = np.random.default_rng(seed)
        self.n_cards = n_cards
        self.n_random_journeys = n_random_journeys
        self.stops: dict[str, StopDef] = {}
        self.route_stops: dict[str, list[StopDef]] = {}
        self.trips_by_key: dict[tuple[str, int, str], list[tuple[datetime, Trip]]] = {}
        self.trips: list[Trip] = []
        self.cards: dict[str, list[Leg]] = {}
        self.journey_counter = 0
        self._build_network()
        self._build_trips()
        self._build_templates()
        self._build_demand()

    # ------------------------------------------------------------------ network
    def _build_network(self) -> None:
        for r in range(5):
            for c in range(6):
                sid = node_id(r, c)
                lat, lon = node_latlon(r, c)
                self.stops[sid] = StopDef(sid, node_name(r, c), lat, lon)

        for rid, spec in ROUTE_DEFS.items():
            path: list[StopDef] = []
            nodes = spec["nodes"]
            for i, (r, c) in enumerate(nodes):
                sid = node_id(r, c)
                if i > 0:
                    pr, pc = nodes[i - 1]
                    if (pr, pc) != (r, c):
                        mid_id = f"M{pr}{pc}{r}{c}"
                        if mid_id not in self.stops:
                            mlat = (node_latlon(pr, pc)[0] + node_latlon(r, c)[0]) / 2
                            mlon = (node_latlon(pr, pc)[1] + node_latlon(r, c)[1]) / 2
                            self.stops[mid_id] = StopDef(
                                mid_id, f"{node_name(pr, pc)}—{node_name(r, c)}中段", mlat, mlon
                            )
                        path.append(self.stops[mid_id])
                path.append(self.stops[sid])
            self.route_stops[rid] = path

    def _stop_table(self) -> pl.DataFrame:
        rows = []
        for rid, path in self.route_stops.items():
            for direction in (0, 1):
                seq_stops = path if direction == 0 else list(reversed(path))
                for idx, s in enumerate(seq_stops, start=1):
                    rows.append({
                        "route_id": rid,
                        "route_name": ROUTE_DEFS[rid]["name"],
                        "direction": direction,
                        "stop_seq": idx,
                        "stop_id": s.stop_id,
                        "stop_name": s.stop_name,
                        "lat": round(s.lat, 6),
                        "lon": round(s.lon, 6),
                    })
        return pl.DataFrame(rows)

    def _stop_ids(self, rid: str, direction: int) -> list[str]:
        ids = [s.stop_id for s in self.route_stops[rid]]
        return ids if direction == 0 else list(reversed(ids))

    # ------------------------------------------------------------------ trips
    def _build_trips(self) -> None:
        for rid, spec in ROUTE_DEFS.items():
            path = self.route_stops[rid]
            for v in range(spec["n_veh"]):
                vehicle_id = f"{rid}-V{v + 1:02d}"
                t = SERVICE_DATE + timedelta(
                    seconds=SERVICE_START + v * 420 + int(self.rng.integers(0, 170))
                )
                direction = 0
                while t < SERVICE_DATE + timedelta(seconds=SERVICE_END):
                    seq_stops = path if direction == 0 else list(reversed(path))
                    stops = []
                    cur = t
                    for i, s in enumerate(seq_stops):
                        if i == 0:
                            arr = cur
                        else:
                            prev = seq_stops[i - 1]
                            dist = haversine(prev.lat, prev.lon, s.lat, s.lon)
                            speed = min(32.0, max(13.0, float(self.rng.normal(21.5, 2.2))))
                            cur = cur + timedelta(seconds=dist / (speed / 3.6))
                            arr = cur
                        is_terminal = i in (0, len(seq_stops) - 1)
                        dwell = (DWELL_TERMINAL if is_terminal else DWELL_INTERMEDIATE)
                        dwell += float(self.rng.normal(0, 4.5))
                        dep = arr + timedelta(seconds=max(9.0, dwell))
                        stops.append({
                            "stop_id": s.stop_id, "stop_seq": i + 1,
                            "arr": arr, "dep": dep, "lat": s.lat, "lon": s.lon,
                        })
                        cur = dep
                    start = stops[0]["dep"]
                    end = stops[-1]["arr"]
                    trip_id = f"{vehicle_id}-{start.strftime('%H%M%S')}-{direction}"
                    trip = Trip(trip_id, vehicle_id, rid, direction, stops, start, end)
                    self.trips.append(trip)
                    for st in stops:
                        self.trips_by_key.setdefault((rid, direction, st["stop_id"]), []).append(
                            (st["dep"], trip)
                        )
                    t = end + timedelta(seconds=LAYOVER + float(self.rng.normal(0, 55)))
                    direction = 1 - direction
        for key in self.trips_by_key:
            self.trips_by_key[key].sort(key=lambda x: x[0])
        self._trip_rows_cache = self._trip_rows()

    def _trip_rows(self) -> pl.DataFrame:
        rows = []
        for tr in self.trips:
            for st in tr.stops:
                rows.append({
                    "trip_id": tr.trip_id, "vehicle_id": tr.vehicle_id,
                    "route_id": tr.route_id, "direction": tr.direction,
                    "stop_id": st["stop_id"], "stop_seq": st["stop_seq"],
                    "arr_time": st["arr"], "dep_time": st["dep"],
                    "lat": st["lat"], "lon": st["lon"],
                })
        return pl.DataFrame(rows)

    # ------------------------------------------------------------------ templates
    def _build_templates(self) -> None:
        """OD templates are lists of legs a passenger can actually ride."""
        self.direct_templates: list[dict] = []
        self.templates_from: dict[str, list[dict]] = {}
        for rid in ROUTE_DEFS:
            for direction in (0, 1):
                seq = self._stop_ids(rid, direction)
                for i in range(len(seq)):
                    if seq.count(seq[i]) > 1:
                        continue  # loop route repeated terminal: ambiguous boarding
                    for j in range(i + 2, len(seq)):
                        if seq[i] == seq[j]:
                            continue
                        tpl = {"legs": [{"route_id": rid, "direction": direction,
                                         "board": seq[i], "alight": seq[j]}],
                               "walk_m": 0.0, "hops": j - i}
                        self.direct_templates.append(tpl)
                        self.templates_from.setdefault(seq[i], []).append(tpl)

        # transfer templates: two routes sharing an interchange stop
        served: dict[str, list[tuple[str, int]]] = {}
        for rid in ROUTE_DEFS:
            for direction in (0, 1):
                for sid in self._stop_ids(rid, direction):
                    if (rid, direction) not in served.setdefault(sid, []):
                        served[sid].append((rid, direction))
        transfer: list[dict] = []
        for sid, items in served.items():
            if len(items) < 2:
                continue
            for (r1, d1) in items:
                idx1 = self._stop_ids(r1, d1).index(sid)
                before = self._stop_ids(r1, d1)[:idx1]
                if not before:
                    continue
                for (r2, d2) in items:
                    if r2 == r1 and d2 == d1:
                        continue
                    seq2 = self._stop_ids(r2, d2)
                    idx2 = seq2.index(sid)
                    after = seq2[idx2 + 1:]
                    if not after:
                        continue
                    for a in before[:: max(1, len(before) // 3)]:
                        if self._stop_ids(r1, d1).count(a) > 1:
                            continue
                        for b in after[:: max(1, len(after) // 3)]:
                            if b == a:
                                continue
                            transfer.append({
                                "legs": [
                                    {"route_id": r1, "direction": d1, "board": a, "alight": sid},
                                    {"route_id": r2, "direction": d2, "board": sid, "alight": b},
                                ],
                                "walk_m": 0.0,
                                "hops": (idx1 - self._stop_ids(r1, d1).index(a))
                                        + (len(seq2) - 1 - idx2),
                                "walk_extra": 90.0,
                            })
        # dedupe transfer templates
        seen = set()
        uniq = []
        for t in transfer:
            k = tuple((l["route_id"], l["direction"], l["board"], l["alight"]) for l in t["legs"])
            if k not in seen:
                seen.add(k)
                uniq.append(t)
        self.transfer_templates = uniq
        for t in uniq:
            self.templates_from.setdefault(t["legs"][0]["board"], []).append(t)
        self.all_templates = self.direct_templates + uniq
        # Urban bus trips are short: the probability of riding k stops decays
        # roughly exponentially.  Sampling uniformly over all (origin, dest)
        # pairs would make every stop pair equally likely and produce flat,
        # unrealistic OD matrices.
        self.template_weights = np.array([
            math.exp(-t.get("hops", 3) / 4.2) for t in self.all_templates
        ], dtype=float)
        self.template_weights /= self.template_weights.sum()

    def _sample_template(self) -> dict:
        idx = int(self.rng.choice(len(self.all_templates), p=self.template_weights))
        return self.all_templates[idx]

    def _reverse_template(self, tpl: dict) -> dict:
        legs = []
        for leg in reversed(tpl["legs"]):
            legs.append({
                "route_id": leg["route_id"],
                "direction": 1 - leg["direction"],
                "board": leg["alight"],
                "alight": leg["board"],
            })
        return {"legs": legs, "walk_m": tpl.get("walk_m", 0.0)}

    # ------------------------------------------------------------------ demand
    def _sample_time(self, kind: str) -> datetime:
        day = SERVICE_DATE
        if kind == "morning":
            mins = float(np.clip(self.rng.normal(8 * 60, 42), 6 * 60, 10 * 60 + 10))
        elif kind == "evening":
            mins = float(np.clip(self.rng.normal(18 * 60, 48), 16 * 60, 21 * 60 + 40))
        elif kind == "midday":
            mins = float(self.rng.uniform(11 * 60 + 20, 14 * 60))
        else:
            hours = list(HOUR_WEIGHTS)
            weights = np.array([HOUR_WEIGHTS[h] for h in hours], dtype=float)
            weights /= weights.sum()
            h = int(self.rng.choice(hours, p=weights))
            mins = h * 60 + float(self.rng.uniform(0, 60))
        return day + timedelta(seconds=int(mins * 60))

    def _materialize(self, tpl: dict, desired: datetime, prev_leg: Leg | None = None) -> list[Leg] | None:
        legs: list[Leg] = []
        cur = desired
        for i, spec in enumerate(tpl["legs"]):
            if i > 0:
                walk = tpl.get("walk_m", 0.0)
                cur = legs[-1].alight_time + timedelta(
                    seconds=walk / WALK_SPEED + 55 + float(self.rng.uniform(0, 40))
                )
                if legs[-1].alight_stop == spec["board"]:
                    cur = legs[-1].alight_time + timedelta(seconds=60 + float(self.rng.uniform(0, 60)))
            events = self.trips_by_key.get(
                (spec["route_id"], spec["direction"], spec["board"]), []
            )
            # A waiting passenger boards the first bus that departs after they
            # reach the stop; reverse that: choose a departure, then walk back
            # to the moment they arrived.  This yields self-consistent taps.
            cand = [(d, tr) for d, tr in events if d >= cur]
            if not cand:
                return None
            dep, trip = cand[0]
            if dep > cur + timedelta(minutes=38):
                return None
            if i > 0 and dep > cur + timedelta(minutes=22):
                # a second leg of one journey is a transfer: keep the wait
                # inside a realistic transfer window so "transfer" in the
                # ground truth means the same thing the platform infers
                return None
            arr_at_stop = dep - timedelta(seconds=float(self.rng.uniform(25, 240)))
            if arr_at_stop < cur:
                arr_at_stop = cur
            seq = [s["stop_id"] for s in trip.stops]
            try:
                bi = seq.index(spec["board"])
                ai = seq.index(spec["alight"], bi + 1)
            except ValueError:
                return None
            if bi >= len(trip.stops) - 1:
                return None
            board_stop = trip.stops[bi]
            alight_stop = trip.stops[ai]
            is_transfer = False
            if i > 0:
                wait = (board_stop["dep"] - legs[-1].alight_time).total_seconds()
                is_transfer = 0 <= wait <= 25 * 60
            leg = Leg(
                leg_index=i, journey_id="", trip=trip,
                board_stop=spec["board"], board_seq=bi + 1, board_time=board_stop["dep"],
                alight_stop=spec["alight"], alight_seq=ai + 1, alight_time=alight_stop["arr"],
                is_transfer=is_transfer,
                transfer_prev_trip=legs[-1].trip.trip_id if (is_transfer and legs) else None,
                walk_m=tpl.get("walk_m", 0.0) if i > 0 else 0.0,
            )
            if leg.alight_time <= leg.board_time:
                return None
            legs.append(leg)
            cur = leg.alight_time
        return legs

    def _add_journey(self, card_id: str, legs: list[Leg], prev_end: datetime | None) -> datetime | None:
        if prev_end is not None and legs[0].board_time < prev_end:
            return None
        jid = f"J{self.journey_counter:05d}"
        for i, leg in enumerate(legs):
            leg.leg_index = i
            leg.journey_id = jid
        self.cards.setdefault(card_id, []).extend(legs)
        self.journey_counter += 1
        return legs[-1].alight_time

    def _build_demand(self) -> None:
        n_commute = int(self.n_cards * 0.58)
        n_random = int(self.n_cards * 0.27)
        n_single = self.n_cards - n_commute - n_random
        card_no = 0

        # ---- commute cards: home -> work in the morning, work -> home at night
        for _ in range(n_commute):
            card_id = f"6101{card_no:08d}"
            card_no += 1
            for _try in range(6):
                tpl = self._sample_template()
                if len(tpl["legs"]) == 2 and self.rng.random() < 0.5:
                    tpl = self.direct_templates[int(self.rng.integers(0, len(self.direct_templates)))]
                rev = self._reverse_template(tpl)
                m = self._materialize(tpl, self._sample_time("morning"))
                if not m:
                    continue
                if self.rng.random() < 0.86:
                    e = self._materialize(rev, self._sample_time("evening"))
                    if not e:
                        e = None
                else:
                    e = None
                # 22% of commuters make a midday trip from work and back
                mid = None
                if self.rng.random() < 0.22:
                    opts = [t for t in self.templates_from.get(m[-1].alight_stop, [])
                            if len(t["legs"]) == 1]
                    if opts:
                        mpl = opts[int(self.rng.integers(0, len(opts)))]
                        mid = self._materialize(mpl, self._sample_time("midday"))
                ends = []
                prev_end = None
                prev = self._add_journey(card_id, m, prev_end)
                if prev is None:
                    continue
                ends.append(prev)
                if mid:
                    prev = self._add_journey(card_id, mid, ends[-1])
                    if prev:
                        ends.append(prev)
                if e:
                    prev = self._add_journey(card_id, e, ends[-1])
                    if prev:
                        ends.append(prev)
                break

        # ---- random cards
        for _ in range(n_random):
            card_id = f"6101{card_no:08d}"
            card_no += 1
            desired = self._sample_time("random")
            prev_end = None
            n_j = int(self.rng.integers(1, 4))
            for _j in range(n_j):
                tpl = self._sample_template()
                legs = self._materialize(tpl, desired)
                if not legs:
                    continue
                new_end = self._add_journey(card_id, legs, prev_end)
                if new_end is None:
                    continue
                prev_end = new_end
                desired = prev_end + timedelta(minutes=float(self.rng.uniform(35, 300)))

        # ---- single-trip cards
        for _ in range(n_single):
            card_id = f"6101{card_no:08d}"
            card_no += 1
            tpl = self._sample_template()
            legs = self._materialize(tpl, self._sample_time("random"))
            if legs:
                self._add_journey(card_id, legs, None)

    # ------------------------------------------------------------------ taps
    def _taps_and_truth(self):
        taps: list[dict] = []
        truth: list[dict] = []
        tap_i = 0
        for card_id, legs in self.cards.items():
            legs = sorted(legs, key=lambda x: x.board_time)
            for leg in legs:
                if self.rng.random() < 0.015:
                    truth.append(self._truth_row("", card_id, leg, has_tap=False))
                    continue
                # passenger taps between arrival and departure
                offset = float(self.rng.uniform(2, 25))
                tap_time = leg.board_time - timedelta(seconds=offset)
                if self.rng.random() < 0.02:
                    tap_time = leg.board_time + timedelta(seconds=float(self.rng.uniform(0, 7)))
                tap_time = max(tap_time, leg.board_time - timedelta(seconds=70))
                tap_id = f"T{tap_i:08d}"
                tap_i += 1
                taps.append({
                    "tap_id": tap_id, "card_id": card_id, "tap_time": tap_time,
                    "route_id": leg.trip.route_id,
                    "vehicle_id": leg.trip.vehicle_id,
                    "card_type": "学生卡" if hash(card_id) % 11 == 0 else "普通卡",
                })
                truth.append(self._truth_row(tap_id, card_id, leg, has_tap=True))
                if self.rng.random() < 0.014:
                    dup_id = f"T{tap_i:08d}"
                    tap_i += 1
                    taps.append({
                        "tap_id": dup_id, "card_id": card_id,
                        "tap_time": tap_time + timedelta(seconds=float(self.rng.uniform(6, 50))),
                        "route_id": leg.trip.route_id,
                        "vehicle_id": leg.trip.vehicle_id,
                        "card_type": taps[-1]["card_type"],
                    })
                    truth.append(self._truth_row(dup_id, card_id, leg, has_tap=True, is_duplicate=True))
                if self.rng.random() < 0.010:
                    bad_id = f"T{tap_i:08d}"
                    tap_i += 1
                    bad_time = tap_time + timedelta(
                        seconds=float(self.rng.choice([-1, 1]) * self.rng.uniform(300, 1500))
                    )
                    taps.append({
                        "tap_id": bad_id, "card_id": card_id, "tap_time": bad_time,
                        "route_id": leg.trip.route_id,
                        "vehicle_id": leg.trip.vehicle_id,
                        "card_type": taps[-1]["card_type"],
                    })
                    truth.append(self._truth_row(bad_id, card_id, leg, has_tap=True, is_invalid=True))
        taps_df = pl.DataFrame(taps).with_columns(pl.col("tap_time").dt.truncate("1s"))
        truth_df = pl.DataFrame(truth).with_columns(pl.col("tap_time").dt.truncate("1s"))
        return (
            taps_df.sort(["card_id", "tap_time"]),
            truth_df.sort(["card_id", "tap_time"]),
        )

    def _truth_row(self, tap_id: str, card_id: str, leg: Leg, has_tap: bool,
                   is_duplicate: bool = False, is_invalid: bool = False) -> dict:
        return {
            "tap_id": tap_id, "card_id": card_id, "journey_id": leg.journey_id,
            "leg_index": leg.leg_index, "tap_time": leg.board_time,
            "true_vehicle_id": leg.trip.vehicle_id, "true_trip_id": leg.trip.trip_id,
            "true_route_id": leg.trip.route_id, "true_direction": leg.trip.direction,
            "true_board_stop_id": leg.board_stop, "true_alight_stop_id": leg.alight_stop,
            "true_board_time": leg.board_time, "true_alight_time": leg.alight_time,
            "is_transfer": leg.is_transfer, "transfer_prev_trip": leg.transfer_prev_trip,
            "walk_m": round(leg.walk_m, 1), "has_tap": has_tap,
            "is_duplicate": is_duplicate, "is_invalid": is_invalid,
        }

    # ------------------------------------------------------------------ gps
    def _gps(self) -> pl.DataFrame:
        rng = self.rng
        frames = []
        for tr in self.trips:
            times, lats, lons, speeds = [], [], [], []
            t = tr.stops[0]["arr"] - timedelta(seconds=20)
            while t < tr.start:
                times.append(t); lats.append(tr.stops[0]["lat"]); lons.append(tr.stops[0]["lon"])
                speeds.append(0.0)
                t += timedelta(seconds=GPS_INTERVAL)
            for i in range(len(tr.stops) - 1):
                a, b = tr.stops[i], tr.stops[i + 1]
                seg_start, seg_end = a["dep"], b["arr"]
                dur = (seg_end - seg_start).total_seconds()
                if dur <= 0:
                    continue
                dist = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                speed_kmh = dist / dur * 3.6 if dur else 0.0
                n = max(1, int(dur // GPS_INTERVAL))
                for k in range(n):
                    ft = k / max(1, n)
                    fs = 0.5 - 0.5 * math.cos(math.pi * min(1.0, max(0.0, ft)))
                    times.append(seg_start + timedelta(seconds=k * GPS_INTERVAL))
                    lats.append(a["lat"] + (b["lat"] - a["lat"]) * fs)
                    lons.append(a["lon"] + (b["lon"] - a["lon"]) * fs)
                    if ft < 0.10 or ft > 0.90:
                        speeds.append(float(rng.uniform(0, 6)))
                    else:
                        speeds.append(max(0.0, speed_kmh + float(rng.normal(0, 2.2))))
                dd = (b["dep"] - b["arr"]).total_seconds()
                if dd > 0:
                    for k in range(max(1, int(dd // GPS_INTERVAL))):
                        times.append(b["arr"] + timedelta(seconds=k * GPS_INTERVAL))
                        lats.append(b["lat"]); lons.append(b["lon"])
                        speeds.append(float(rng.uniform(0, 3)))
            if not times:
                continue
            lat_arr = np.array(lats, dtype=float) + rng.normal(0, 0.000055, size=len(lats))
            lon_arr = np.array(lons, dtype=float) + rng.normal(0, 0.000065, size=len(lons))
            spd = np.array(speeds)
            frames.append(pl.DataFrame({
                "vehicle_id": tr.vehicle_id, "route_id": tr.route_id,
                "direction": tr.direction, "gps_time": times,
                "lon": lon_arr, "lat": lat_arr, "speed_kmh": spd,
            }))
        gps = pl.concat(frames, how="vertical")
        gps = gps.with_columns(pl.col("gps_time").dt.truncate("1s"))
        # GPS drift: a fix that jumps hundreds of metres away and back.  This
        # is injected after concatenation so short trips get their share too.
        lat = gps["lat"].to_numpy().copy()
        lon = gps["lon"].to_numpy().copy()
        n_drift = int(len(lat) * 0.004)
        self._drift_injected = n_drift
        idx = self.rng.choice(len(lat), size=n_drift, replace=False)
        drift_keys = set(zip(gps["vehicle_id"].gather(idx).to_list(),
                             gps["gps_time"].gather(idx).to_list()))
        lat[idx] += self.rng.normal(0, 0.0035, size=len(idx))
        lon[idx] += self.rng.normal(0, 0.0045, size=len(idx))
        gps = gps.with_columns([pl.Series("lat", lat), pl.Series("lon", lon)])
        self._drift_keys = drift_keys
        n = len(gps)
        keep = np.ones(n, dtype=bool)
        keep[self.rng.choice(n, size=int(n * 0.012), replace=False)] = False
        gps = gps.filter(pl.Series(keep))
        dup = gps.sample(int(len(gps) * 0.003), seed=int(self.rng.integers(1, 10**9)))
        gps = pl.concat([gps, dup.with_columns(pl.col("gps_time") + pl.duration(seconds=1))],
                        how="vertical")
        return gps.sort(["vehicle_id", "gps_time"])

    # ------------------------------------------------------------------ main
    def generate(self) -> GeneratorResult:
        taps, truth = self._taps_and_truth()
        gps = self._gps()
        if self._drift_keys:
            keys_df = pl.DataFrame({
                "vehicle_id": [k[0] for k in self._drift_keys],
                "gps_time": [k[1] for k in self._drift_keys],
            })
            drift_df = gps.join(keys_df, on=["vehicle_id", "gps_time"], how="semi") \
                .select(["vehicle_id", "gps_time"]).with_columns(
                    pl.lit(True).alias("is_drift"))
        else:
            drift_df = pl.DataFrame(schema={"vehicle_id": pl.Utf8,
                                            "gps_time": pl.Datetime, "is_drift": pl.Boolean})
        stops = self._stop_table()
        valid_truth = truth.filter(~pl.col("is_duplicate") & ~pl.col("is_invalid"))
        summary = {
            "routes": len(ROUTE_DEFS),
            "vehicles": self._trip_rows_cache["vehicle_id"].n_unique(),
            "trips": self._trip_rows_cache["trip_id"].n_unique(),
            "stops": stops["stop_id"].n_unique(),
            "cards": len(self.cards),
            "taps": len(taps),
            "gps_rows": len(gps),
            "truth_legs": valid_truth.height,
            "journeys": truth["journey_id"].n_unique(),
            "transfer_legs": int(valid_truth.filter(pl.col("is_transfer")).height),
            "missing_taps": truth.filter(~pl.col("has_tap")).height,
            "duplicate_taps": truth.filter(pl.col("is_duplicate")).height,
            "invalid_taps": truth.filter(pl.col("is_invalid")).height,
            "gps_drift_injected": getattr(self, "_drift_injected", 0),
        }
        return GeneratorResult(stops, taps, gps, truth, self._trip_rows_cache, summary,
                               drift_df)


def write_demo_csvs(out_dir: Path, seed: int = RNG_SEED, n_cards: int = 1800,
                    n_random_journeys: int = 1500) -> GeneratorResult:
    """Write the four raw source files using deliberately messy headers.

    The column names and date format mimic exports from three unrelated
    systems, so the field-mapping screen has real work to do.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    res = Generator(seed=seed, n_cards=n_cards, n_random_journeys=n_random_journeys).generate()

    taps = res.taps.with_columns(
        pl.col("tap_time").dt.strftime("%Y/%m/%d %H:%M:%S").alias("交易时间"),
    ).select([
        pl.col("tap_id").alias("交易流水号"),
        pl.col("card_id").alias("卡号"),
        pl.col("交易时间"),
        pl.col("route_id").alias("线路编号"),
        pl.col("vehicle_id").alias("车载机编号"),
        pl.col("card_type").alias("卡类型"),
    ])
    taps.write_csv(out_dir / "afc_taps.csv")

    gps = res.gps.with_columns(
        pl.col("gps_time").dt.strftime("%Y-%m-%d %H:%M:%S").alias("定位时间")
    ).select([
        pl.col("vehicle_id").alias("车载机编号"),
        pl.col("route_id").alias("线路"),
        pl.col("direction").alias("行向"),
        pl.col("定位时间"),
        pl.col("lon").round(6).alias("经度"),
        pl.col("lat").round(6).alias("纬度"),
        pl.col("speed_kmh").round(2).alias("瞬时速度"),
    ])
    gps.write_csv(out_dir / "avl_gps.csv")

    meets = res.stops.with_columns(
        pl.col("route_name").alias("线路名称")
    ).select([
        pl.col("route_id").alias("线路代码"),
        pl.col("route_name").alias("线路名称"),
        pl.col("direction").alias("方向"),
        pl.col("stop_seq").alias("站点序号"),
        pl.col("stop_id").alias("站点编号"),
        pl.col("stop_name").alias("站点名称"),
        pl.col("lon").alias("经度"),
        pl.col("lat").alias("纬度"),
    ])
    meets.write_csv(out_dir / "route_stops.csv")

    truth = res.truth.with_columns(
        pl.col("true_board_time").dt.strftime("%Y-%m-%d %H:%M:%S").alias("实际上车时间"),
        pl.col("true_alight_time").dt.strftime("%Y-%m-%d %H:%M:%S").alias("实际下车时间"),
    ).select([
        "tap_id", "card_id", "journey_id", "leg_index", "实际上车时间", "实际下车时间",
        pl.col("true_vehicle_id"), pl.col("true_trip_id"), pl.col("true_route_id"),
        pl.col("true_direction"), pl.col("true_board_stop_id"), pl.col("true_alight_stop_id"),
        "is_transfer", "transfer_prev_trip", "has_tap", "is_duplicate", "is_invalid",
    ])
    # record how many drift fixes were injected so the evaluator can report
    # precision/recall of GPS cleaning instead of a bare count
    drift_count = int(res.summary.get("gps_drift_injected", 0))
    truth.write_csv(out_dir / "ground_truth.csv")
    drift_df = getattr(res, "drift_df", None)
    if drift_df is not None and drift_df.height:
        drift_df.write_csv(out_dir / "gps_drift_truth.csv")
    (out_dir / "meta.json").write_text(
        __import__("json").dumps({"gps_drift_injected": drift_count,
                                  "seed": seed}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_dir / "summary.json").write_text(
        __import__("json").dumps(res.summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return res
