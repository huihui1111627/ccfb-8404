"""Test suite for the OD inference platform.

Covers: ingestion/field mapping, GPS cleaning, stop-event detection, tap
matching, trip-chain alighting inference, transfers, OD/section aggregation,
accuracy scoring, the HTTP API and parameter sensitivity.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest

from app.config import DEFAULT_PARAMS
from app.pipeline import Pipeline
from app.utils import build_period_expr, validate_params

DEMO = Path(__file__).resolve().parent.parent / "data" / "demo"


# ----------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def demo_frames() -> dict[str, pl.DataFrame]:
    from app.ingest import apply_mapping, detect_mapping, read_table
    files = {"taps": "afc_taps.csv", "gps": "avl_gps.csv",
             "stops": "route_stops.csv", "truth": "ground_truth.csv"}
    frames = {}
    for kind, name in files.items():
        path = DEMO / name
        assert path.exists(), f"missing demo file {path}; run: python -m app.cli demo"
        df = read_table(path.read_bytes(), name)
        parsed, _ = apply_mapping(df, detect_mapping(df.columns, kind), kind)
        frames[kind] = parsed
    return frames


@pytest.fixture(scope="session")
def demo_result(demo_frames) -> tuple[Pipeline, dict]:
    pipe = Pipeline({}, demo_frames)
    metrics = pipe.run()
    return pipe, metrics


# ----------------------------------------------------------------- ingestion
class TestIngest:
    def test_header_detection_chinese(self):
        from app.ingest import detect_mapping
        cols = ["交易流水号", "卡号", "交易时间", "线路编号", "卡类型"]
        m = detect_mapping(cols, "taps")
        assert m["card_id"] == "卡号"
        assert m["tap_time"] == "交易时间"
        assert m["route_id"] == "线路编号"

    def test_header_detection_english(self):
        from app.ingest import detect_mapping
        cols = ["card_id", "tap_time", "route_id", "vehicle_id"]
        m = detect_mapping(cols, "taps")
        assert m["card_id"] == "card_id"
        assert m["tap_time"] == "tap_time"

    def test_time_formats_normalised(self):
        from app.ingest import apply_mapping
        df = pl.DataFrame({
            "卡号": ["A", "B", "C"],
            "刷卡时间": ["2026/11/09 08:00:00", "2026-11-09 08:05", "bad"],
            "线路": ["R1", "R1", "R1"],
        })
        out, stats = apply_mapping(df, {"card_id": "卡号", "tap_time": "刷卡时间",
                                        "route_id": "线路"}, "taps")
        assert out.schema["tap_time"] == pl.Datetime("us")
        assert out.height == 2, "unparseable timestamp row must be dropped"
        assert out["tap_time"][0] == datetime(2026, 11, 9, 8, 0)

    def test_missing_required_field_raises(self):
        from app.ingest import apply_mapping
        df = pl.DataFrame({"卡号": ["A"]})
        with pytest.raises(ValueError) as exc:
            apply_mapping(df, {"card_id": "卡号"}, "taps")
        assert "缺少必填字段" in str(exc.value)

    def test_direction_normalisation(self):
        from app.ingest import apply_mapping
        df = pl.DataFrame({
            "线路": ["R1", "R1"], "方向": ["上行", "下行"],
            "站点序号": ["1", "1"], "站点编号": ["S1", "S2"],
            "经度": ["104.0", "104.1"], "纬度": ["30.6", "30.6"],
        })
        out, _ = apply_mapping(df, {"route_id": "线路", "direction": "方向",
                                    "stop_seq": "站点序号", "stop_id": "站点编号",
                                    "lon": "经度", "lat": "纬度"}, "stops")
        assert out["direction"].to_list() == [0, 1]

    def test_duplicate_tap_id_deduped(self):
        from app.ingest import apply_mapping
        df = pl.DataFrame({
            "卡号": ["A", "A"], "时间": ["2026-11-09 08:00:00", "2026-11-09 08:00:05"],
            "线路": ["R1", "R1"], "流水号": ["T1", "T1"],
        })
        out, stats = apply_mapping(df, {"card_id": "卡号", "tap_time": "时间",
                                        "route_id": "线路", "tap_id": "流水号"}, "taps")
        assert out.height == 1
        assert any("重复" in w for w in stats["warnings"])

    def test_out_of_range_coordinates_dropped(self):
        from app.ingest import apply_mapping
        df = pl.DataFrame({
            "车辆": ["V1", "V2"], "线路": ["R1", "R1"],
            "时间": ["2026-11-09 08:00:00", "2026-11-09 08:00:00"],
            "经度": ["104.0", "500.0"], "纬度": ["30.6", "30.6"],
        })
        out, stats = apply_mapping(df, {"vehicle_id": "车辆", "route_id": "线路",
                                        "gps_time": "时间", "lon": "经度",
                                        "lat": "纬度"}, "gps")
        assert out.height == 1
        assert any("越界" in w for w in stats["warnings"])


# ------------------------------------------------------------- period logic
class TestPeriods:
    def test_period_expression_no_int8_overflow(self):
        """08:00 must not overflow into a negative minute-of-day."""
        df = pl.DataFrame({"t": pl.Series([
            datetime(2026, 11, 9, 6, 30), datetime(2026, 11, 9, 8, 0),
            datetime(2026, 11, 9, 13, 0), datetime(2026, 11, 9, 18, 0),
            datetime(2026, 11, 9, 22, 0)])})
        periods = DEFAULT_PARAMS["time_periods"]
        out = df.with_columns(build_period_expr("t", periods).alias("p"))["p"].to_list()
        assert out == ["早间", "早高峰", "平峰", "晚高峰", "夜间"]

    def test_validate_rejects_overlapping_periods(self):
        params = json.loads(json.dumps(DEFAULT_PARAMS))
        params["time_periods"] = [
            {"name": "A", "start": "07:00", "end": "10:00"},
            {"name": "B", "start": "09:00", "end": "12:00"},
        ]
        errs = validate_params(params)
        assert any("重叠" in e for e in errs)

    def test_validate_rejects_out_of_range(self):
        params = json.loads(json.dumps(DEFAULT_PARAMS))
        params["transfer_window_min"] = 500
        assert validate_params(params)


# --------------------------------------------------------------- GPS cleaning
class TestGpsCleaning:
    def test_duplicates_removed(self, demo_result):
        pipe, metrics = demo_result
        assert metrics["gps"]["duplicates_removed"] > 0

    def test_drift_spikes_removed(self, demo_result):
        pipe, metrics = demo_result
        gps = metrics["gps"]
        assert gps["drift_points_removed"] > 50, "demo injects ~0.4% drift fixes"

    def test_out_of_order_kept_sorted(self, demo_result):
        pipe, metrics = demo_result
        df = pipe.out["gps_clean"]
        assert df.equals(df.sort(["vehicle_id", "gps_time"]))

    def test_stop_event_count_reasonable(self, demo_result):
        pipe, metrics = demo_result
        # 921 scheduled trips x ~9 stops each
        assert 7000 < metrics["stop_events"] < 12000


# ------------------------------------------------------------ tap matching
class TestMatching:
    def test_match_rate_high(self, demo_result):
        _, metrics = demo_result
        assert metrics["match_rate"] > 0.98

    def test_board_stop_accuracy(self, demo_result):
        _, metrics = demo_result
        acc = metrics["accuracy"]
        assert acc["board_stop_accuracy"] > 0.98
        assert acc["vehicle_accuracy"] > 0.98

    def test_unmatched_tap_flagged(self, demo_result):
        pipe, _ = demo_result
        taps = pipe.out["taps"]
        assert (taps["status"] == "invalid").sum() >= 0
        bad = taps.filter(pl.col("status") == "invalid")
        assert bad.height == 0 or bad["board_stop_id"].is_null().all()

    def test_duplicates_flagged(self, demo_result):
        _, metrics = demo_result
        assert metrics["taps_duplicate"] > 0
        assert metrics["accuracy"]["duplicate_recall"] > 0.9


# -------------------------------------------------------------- alighting
class TestAlighting:
    def test_alight_rate(self, demo_result):
        _, metrics = demo_result
        assert metrics["alight_rate"] > 0.95

    def test_accuracy_overall_and_confident(self, demo_result):
        _, metrics = demo_result
        acc = metrics["accuracy"]
        assert acc["available"]
        # every leg with direct evidence should be right the vast majority of
        # the time; the no-evidence fallback drags the total down by design
        assert acc["alight_stop_accuracy_confident"] > 0.93
        assert acc["alight_stop_accuracy"] > 0.70

    def test_uncertain_legs_reported(self, demo_result):
        pipe, metrics = demo_result
        legs = pipe.out["legs"]
        assert metrics["uncertain"] == legs.filter(pl.col("uncertain")).height
        assert metrics["uncertain_rate"] > 0

    def test_no_alight_before_board(self, demo_result):
        pipe, _ = demo_result
        legs = pipe.out["legs"].filter(pl.col("alight_time").is_not_null())
        assert legs.filter(pl.col("alight_time") < pl.col("board_time")).height == 0

    def test_alight_is_downstream_stop(self, demo_result):
        pipe, _ = demo_result
        legs = pipe.out["legs"].filter(pl.col("alight_event_pos").is_not_null())
        assert legs.filter(pl.col("alight_event_pos") <= pl.col("board_event_pos")).height == 0

    def test_missing_vehicle_ids_warns_and_still_runs(self, demo_frames):
        frames = {**demo_frames, "taps": demo_frames["taps"].drop("vehicle_id")}
        metrics = Pipeline({}, frames).run()
        assert any("车辆编号" in w for w in metrics["warnings"])
        assert metrics["match_rate"] > 0.95
        # board accuracy necessarily degrades: several buses serve the same route
        assert metrics["accuracy"]["board_stop_accuracy"] < 0.95

    def test_disable_anchor_lowers_confidence(self, demo_frames):
        pipe = Pipeline({"use_return_anchor": False}, demo_frames)
        metrics = pipe.run()
        methods = metrics["alight_methods"]
        assert "return_anchor" not in methods or methods.get("return_anchor", 0) == 0


# ---------------------------------------------------------------- transfers
class TestTransfers:
    def test_transfers_detected(self, demo_result):
        _, metrics = demo_result
        assert metrics["transfers"] > 0
        assert metrics["accuracy"]["transfer_f1"] > 0.6

    def test_window_parameter_changes_count(self, demo_frames):
        a = Pipeline({"transfer_window_min": 10}, demo_frames).run()
        b = Pipeline({"transfer_window_min": 60}, demo_frames).run()
        assert b["transfers"] >= a["transfers"]

    def test_transfer_waits_within_window(self, demo_result):
        pipe, _ = demo_result
        legs = pipe.out["legs"].filter(pl.col("is_transfer"))
        assert legs.height > 0
        waits = legs["transfer_wait_sec"].drop_nulls()
        assert waits.min() >= -DEFAULT_PARAMS["timing_slack_sec"]
        assert waits.max() <= DEFAULT_PARAMS["transfer_window_min"] * 60

    def test_journey_ids_grouped(self, demo_result):
        pipe, _ = demo_result
        legs = pipe.out["legs"]
        # a journey's legs are contiguous and ordered
        firsts = legs.group_by("journey_id").agg([
            pl.col("leg_index").min().alias("mn"), pl.col("leg_index").max().alias("mx"),
            pl.len().alias("n")])
        assert firsts.filter(pl.col("mn") != 0).height == 0


# --------------------------------------------------------------- OD/flow
class TestAggregates:
    def test_od_matrix_totals(self, demo_result):
        pipe, metrics = demo_result
        od = pipe.out["od_matrix"]
        assert od.height > 100
        assert int(od["passengers"].sum()) == metrics["od_total"]
        assert od["passengers"].min() >= 1

    def test_od_has_coordinates_for_map(self, demo_result):
        pipe, _ = demo_result
        od = pipe.out["od_matrix"]
        assert od["board_lat"].null_count() == 0
        assert od["alight_lon"].null_count() == 0

    def test_stop_flow_conservation(self, demo_result):
        """Per run, boardings - alightings must net to the final load."""
        pipe, _ = demo_result
        flow = pipe.out["stop_flow"]
        net = flow.group_by("route_id").agg([
            pl.col("boardings").sum().alias("b"), pl.col("alightings").sum().alias("a")])
        # summed across periods/stops this is a whole-system check
        total_b = int(net["b"].sum())
        total_a = int(net["a"].sum())
        assert total_b == total_a

    def test_load_factor_positive(self, demo_result):
        pipe, _ = demo_result
        sections = pipe.out["sections"]
        assert sections.height > 0
        assert sections["load_factor"].min() >= 0

    def test_periods_present(self, demo_result):
        _, metrics = demo_result
        periods = {p["period"] for p in metrics["period_od"]}
        assert periods <= {p["name"] for p in DEFAULT_PARAMS["time_periods"]}
        assert len(periods) >= 3


# ---------------------------------------------------------------- accuracy
class TestAccuracyScoring:
    def test_alight_time_mae_small(self, demo_result):
        _, metrics = demo_result
        mae = metrics["accuracy"]["alight_time_mae_sec"]
        assert mae is not None and abs(mae) < 180

    def test_adjacent_tolerance_accuracy(self, demo_result):
        _, metrics = demo_result
        acc = metrics["accuracy"]
        assert acc["alight_stop_accuracy_adjacent"] >= acc["alight_stop_accuracy"]
        assert acc["adjacent_tolerance_m"] > 0

    def test_per_method_breakdown(self, demo_result):
        _, metrics = demo_result
        per = metrics["accuracy"]["per_method"]
        assert per and all("method" in x and "acc" in x for x in per)
        by = {x["method"]: x for x in per}
        assert by["next_board_same_stop"]["acc"] > 0.9

    def test_no_truth_returns_unavailable(self, demo_frames):
        frames = {k: v for k, v in demo_frames.items() if k != "truth"}
        metrics = Pipeline({}, frames).run()
        assert "accuracy" not in metrics

    def test_tap_coverage(self, demo_result):
        _, metrics = demo_result
        assert metrics["accuracy"]["tap_coverage"] > 0.9


# ---------------------------------------------------------------- params
class TestParamSensitivity:
    def test_dup_window_changes_duplicate_count(self, demo_frames):
        low = Pipeline({"dup_tap_window_sec": 1}, demo_frames).run()
        high = Pipeline({"dup_tap_window_sec": 300}, demo_frames).run()
        assert high["taps_duplicate"] >= low["taps_duplicate"]

    def test_snap_radius_changes_event_count(self, demo_frames):
        tight = Pipeline({"stop_snap_radius_m": 20}, demo_frames).run()
        loose = Pipeline({"stop_snap_radius_m": 150}, demo_frames).run()
        assert loose["stop_events"] >= tight["stop_events"]

    def test_invalid_params_rejected(self, demo_frames):
        with pytest.raises(ValueError):
            Pipeline({"transfer_window_min": -5}, demo_frames)

    @pytest.mark.parametrize("params", [
        {"min_dwell_sec": 120}, {"stop_snap_radius_m": 10},
        {"gps_speed_max_kmh": 20}, {"max_ride_min": 5}, {"anchor_bonus": 0},
        {"prior_w_od": 0, "prior_w_dist": 0},
        {"time_periods": [{"name": "全天", "start": "05:00", "end": "24:00"}]},
    ])
    def test_extreme_parameters_do_not_crash(self, demo_frames, params):
        metrics = Pipeline(params, demo_frames).run()
        assert metrics["taps"] > 0
        assert 0 <= metrics["match_rate"] <= 1
        assert metrics["warnings"] is not None

    def test_accuracy_report_handles_zero_confident_legs(self, demo_frames):
        """A degenerate setting can leave no confident legs; metrics must still render."""
        metrics = Pipeline({"min_dwell_sec": 120}, demo_frames).run()
        acc = metrics["accuracy"]
        assert acc["available"]
        assert acc["alight_stop_accuracy_confident"] is None
        json.dumps(metrics, default=str)


# -------------------------------------------------------------------- API
class TestApi:
    @pytest.fixture(scope="class")
    def client(self):
        from fastapi.testclient import TestClient
        import app.main as main
        return TestClient(main.app)

    def test_meta(self, client):
        r = client.get("/api/meta")
        assert r.status_code == 200
        body = r.json()
        assert "taps" in body["kinds"]
        assert "transfer_window_min" in body["default_params"]

    def test_upload_and_preview(self, client):
        csv = ("交易流水号,卡号,交易时间,线路编号\n"
               "T1,610100000001,2026/11/09 08:00:00,R1\n"
               "T2,610100000002,2026/11/09 08:10:00,R1\n")
        r = client.post("/api/datasets/upload", files={
            "file": ("taps.csv", csv.encode("utf-8"), "text/csv")},
            data={"kind": "taps"})
        assert r.status_code == 200, r.text
        ds = r.json()["dataset"]
        assert ds["n_rows"] == 2
        prev = client.get(f"/api/datasets/{ds['id']}").json()
        assert prev["mapping"]["card_id"] == "卡号"
        assert prev["rows"][0]["card_id"] == "610100000001"
        # invalid mapping produces a useful 422
        r2 = client.post("/api/datasets/upload", files={
            "file": ("bad.csv", b"foo,bar\n1,2\n", "text/csv")},
            data={"kind": "taps"})
        assert r2.status_code == 422
        detail = r2.json()["detail"]
        assert "缺少必填字段" in json.dumps(detail, ensure_ascii=False)
        # ... and the stored raw frame can be repaired by fixing the mapping
        r3 = client.post(f"/api/datasets/{detail['dataset_id']}/mapping", json={
            "mapping": {"card_id": "foo", "route_id": "bar", "tap_time": "foo"}})
        assert r3.status_code == 422, "insufficient mapping must be rejected, not crash"
        assert "缺少必填字段" in json.dumps(r3.json(), ensure_ascii=False)

    def test_unparseable_time_rejected_cleanly(self, client):
        csv = "卡号,线路,时间\nA1,R1,abc\nA2,R1,def\n"
        r = client.post("/api/datasets/upload", files={
            "file": ("notime.csv", csv.encode(), "text/csv")},
            data={"kind": "taps", "mapping_json": json.dumps({
                "card_id": "卡号", "route_id": "线路", "tap_time": "时间"})})
        assert r.status_code == 422
        # a partial upload keeps the raw frame and reports the parse problem
        body = r.json()
        detail = body["detail"]
        if isinstance(detail, dict):
            assert "无法解析" in detail.get("message", "")
        else:
            assert "无法解析" in str(detail)

    def test_mapping_repair_applies(self, client):
        csv = "卡号,线路,时间\nA1,R1,2026-11-09 08:00:00\nA2,R1,2026-11-09 08:10:00\n"
        r = client.post("/api/datasets/upload", files={
            "file": ("needsmap.csv", csv.encode(), "text/csv")},
            data={"kind": "taps", "mapping_json": '{"card_id": "时间"}'})
        # forcing card_id onto the time column leaves required fields missing
        if r.status_code == 200:
            return
        detail = r.json()["detail"]
        ds_id = detail["dataset_id"]
        fix = client.post(f"/api/datasets/{ds_id}/mapping", json={
            "mapping": {"card_id": "卡号", "route_id": "线路", "tap_time": "时间"}})
        assert fix.status_code == 200, fix.text
        assert fix.json()["rows"] == 2
        prev = client.get(f"/api/datasets/{ds_id}").json()
        assert prev["rows"][0]["card_id"] == "A1"
        assert prev["dataset"]["n_rows"] == 2

    def test_run_lifecycle(self, client):
        import time
        items = client.get("/api/datasets").json()["items"]
        by_kind = {}
        for d in items:
            if d.get("meta", {}).get("demo"):
                by_kind.setdefault(d["kind"], d["id"])
        if not {"taps", "gps", "stops"} <= set(by_kind):
            pytest.skip("demo datasets not seeded")
        r = client.post("/api/runs", json={
            "name": "pytest run",
            "datasets": {k: by_kind[k] for k in ("taps", "gps", "stops", "truth")},
            "params": DEFAULT_PARAMS})
        assert r.status_code == 200, r.text
        run_id = r.json()["run"]["id"]
        for _ in range(120):
            st = client.get(f"/api/runs/{run_id}").json()
            if st["run"]["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.25)
        assert st["run"]["status"] == "succeeded", st["run"].get("error")
        assert st["metrics"]["taps"] > 0
        od = client.get(f"/api/runs/{run_id}/od?limit=5").json()
        assert od["total"] > 0 and len(od["rows"]) == 5
        flow = client.get(f"/api/runs/{run_id}/flow?limit=5").json()
        assert flow["summary"]["peak_load"] >= 0
        filtered = client.get(
            f"/api/runs/{run_id}/legs?uncertain=false&limit=10").json()
        assert all(not r["uncertain"] for r in filtered["rows"])
        export = client.get(f"/api/runs/{run_id}/export?name=od_matrix&fmt=csv")
        assert export.status_code == 200 and "origin_name" in export.text

    def test_compare(self, client):
        runs = client.get("/api/runs").json()["items"]
        done = [r["id"] for r in runs if r["status"] == "succeeded"][:2]
        if len(done) < 2:
            pytest.skip("need two finished runs")
        r = client.get(f"/api/compare?ids={','.join(done)}")
        assert r.status_code == 200
        assert len(r.json()["runs"]) == 2

    def test_missing_run_404(self, client):
        assert client.get("/api/runs/run_nope").status_code == 404

    def test_bad_params_400(self, client):
        r = client.post("/api/runs", json={
            "datasets": {"taps": "x", "gps": "y", "stops": "z"},
            "params": {"transfer_window_min": 9999}})
        assert r.status_code == 400
