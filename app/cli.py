"""CLI entry points.

  python -m app.cli demo [--out data/demo] [--cards 3200]   regenerate demo set
  python -m app.cli run  [--run-id X]                      run the pipeline headless
  python -m app.cli serve [--port 8404]                    start the web console
  python -m app.cli seed                                   register the demo set
  python -m app.cli report <run_id>                        print the metrics summary
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import DATA_DIR, DEFAULT_PARAMS, RUNS_DIR
from app.utils import validate_params


def cmd_demo(args: argparse.Namespace) -> int:
    from app.generator import write_demo_csvs
    out = Path(args.out)
    res = write_demo_csvs(out, n_cards=args.cards, n_random_journeys=args.random)
    print(json.dumps(res.summary, ensure_ascii=False, indent=2))
    print(f"files written to {out}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from app import store
    from app.main import _ensure_demo_loaded
    store.init_db()
    _ensure_demo_loaded()
    for d in store.list_datasets():
        print(f"{d['id']}  {d['kind']:6s} {d['n_rows']:>8,}  {d['name']}")
    return 0


def _load_demo_frames() -> dict:
    from app.ingest import apply_mapping, detect_mapping, read_table
    demo = DATA_DIR / "demo"
    files = {"taps": "afc_taps.csv", "gps": "avl_gps.csv",
             "stops": "route_stops.csv", "truth": "ground_truth.csv"}
    frames = {}
    for kind, name in files.items():
        path = demo / name
        if not path.exists():
            continue
        df = read_table(path.read_bytes(), name)
        parsed, _ = apply_mapping(df, detect_mapping(df.columns, kind), kind)
        frames[kind] = parsed
    return frames


def cmd_run(args: argparse.Namespace) -> int:
    from app.pipeline import run_pipeline

    if args.dataset_dir:
        frames = _load_demo_frames() if not args.dataset_dir else _load_frames_from(args.dataset_dir)
    else:
        frames = _load_demo_frames()
    if not {"taps", "gps", "stops"} <= set(frames):
        print("缺少必需数据（taps/gps/stops）", file=sys.stderr)
        return 2
    params = DEFAULT_PARAMS
    if args.params:
        params.update(json.loads(Path(args.params).read_text(encoding="utf-8")))
    errs = validate_params(params)
    if errs:
        print("参数错误：" + "；".join(errs), file=sys.stderr)
        return 2
    run_id = args.run_id or "cli_run"

    def progress(pct: float, stage: str, msg: str) -> None:
        print(f"[{pct:5.1f}%] {stage:10s} {msg}", flush=True)

    result = run_pipeline(run_id, frames, params, RUNS_DIR, progress)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2, default=str))
    print(f"\nresults in {RUNS_DIR / run_id}")
    return 0


def _load_frames_from(directory: str) -> dict:
    from app.ingest import apply_mapping, detect_mapping, read_table
    base = Path(directory)
    aliases = {
        "taps": ["taps.csv", "afc_taps.csv", "afc.csv"],
        "gps": ["gps.csv", "avl_gps.csv", "avl.csv"],
        "stops": ["stops.csv", "route_stops.csv", "routes.csv"],
        "truth": ["truth.csv", "ground_truth.csv"],
    }
    frames = {}
    for kind, names in aliases.items():
        for name in names:
            path = base / name
            if path.exists():
                df = read_table(path.read_bytes(), name)
                parsed, _ = apply_mapping(df, detect_mapping(df.columns, kind), kind)
                frames[kind] = parsed
                break
    return frames


def cmd_report(args: argparse.Namespace) -> int:
    path = RUNS_DIR / args.run_id / "metrics.json"
    if not path.exists():
        print(f"找不到 {path}", file=sys.stderr)
        return 2
    data = json.loads(path.read_text(encoding="utf-8"))
    acc = data.get("accuracy") or {}
    rows = [
        ("刷卡记录", data.get("taps")),
        ("匹配率", data.get("match_rate")),
        ("下车站推算率", data.get("alight_rate")),
        ("下车站准确率", acc.get("alight_stop_accuracy")),
        ("确定结果准确率", acc.get("alight_stop_accuracy_confident")),
        ("确定结果占比", acc.get("confident_share")),
        ("上车准确率", acc.get("board_stop_accuracy")),
        ("换乘 F1", acc.get("transfer_f1")),
        ("OD 客流", data.get("od_total")),
        ("最大断面客流", data.get("max_load")),
        ("用时(秒)", data.get("elapsed_sec")),
    ]
    for k, v in rows:
        print(f"{k:<16} {v}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("app.main:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="公交 OD 推算平台")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="生成演示数据")
    p.add_argument("--out", default=str(DATA_DIR / "demo"))
    p.add_argument("--cards", type=int, default=3200)
    p.add_argument("--random", type=int, default=2400)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("seed", help="把演示数据登记到数据库")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("run", help="无界面运行推算")
    p.add_argument("--run-id", default=None)
    p.add_argument("--params", default=None, help="参数 JSON 文件")
    p.add_argument("--dataset-dir", default=None, help="包含 taps/gps/stops 的目录")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("serve", help="启动网页控制台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8404)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("report", help="打印某次运行的核心指标")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
