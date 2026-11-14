"""Background job execution for pipeline runs.

Runs are executed in a small thread pool; progress is written to SQLite so the
browser can poll without holding a request open.  Only one heavy run executes
at a time by default, keeping memory predictable on a laptop-class host.
"""
from __future__ import annotations

import queue
import threading
import traceback

import polars as pl

from app import store
from app.config import RUNS_DIR
from app.pipeline import run_pipeline

_queue: "queue.Queue[str]" = queue.Queue()
_worker_thread: threading.Thread | None = None
_lock = threading.Lock()


def _load_frames(dataset_ids: dict[str, str | None]) -> dict[str, pl.DataFrame]:
    from app.ingest import load_dataset_frame

    frames: dict[str, pl.DataFrame] = {}
    for kind in ("taps", "gps", "stops", "truth"):
        ds_id = dataset_ids.get(kind)
        if not ds_id:
            continue
        ds = store.get_dataset(ds_id)
        if ds is None:
            raise ValueError(f"数据集 {ds_id} 不存在")
        frames[kind] = load_dataset_frame(ds_id)
    return frames


def _execute(run_id: str) -> None:
    run = store.get_run(run_id)
    if run is None:
        return
    store.update_run(run_id, status="running", progress=1.0, stage="load",
                     message="准备数据", error=None)
    try:
        ids = list(run.get("dataset_ids") or [])
        ids += [None] * (4 - len(ids))
        frames = _load_frames(dict(zip(("taps", "gps", "stops", "truth"), ids[:4])))

        def progress(pct: float, stage: str, message: str) -> None:
            store.update_run(run_id, progress=round(float(pct), 1), stage=stage,
                             message=message)

        result = run_pipeline(run_id, frames, run["params"], RUNS_DIR, progress)
        store.update_run(
            run_id, status="succeeded", progress=100.0, stage="done", message="完成",
            summary=result["summary"], elapsed_sec=result.get("elapsed_sec"),
            finished_at=store._now(),
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI
        tb = traceback.format_exc(limit=8)
        # keep the traceback for developers but give the UI a readable reason
        message = str(exc) or exc.__class__.__name__
        hint = ""
        if isinstance(exc, ValueError):
            hint = "请检查所选数据集与参数设置"
        elif isinstance(exc, FileNotFoundError):
            hint = "结果文件缺失，请重新导入数据"
        elif "memory" in message.lower():
            hint = "内存不足，请减少数据量"
        store.update_run(run_id, status="failed", progress=100.0, stage="error",
                         message=f"{message}" + (f"（{hint}）" if hint else ""),
                         error=tb, finished_at=store._now())


def _worker() -> None:
    while True:
        run_id = _queue.get()
        try:
            _execute(run_id)
        finally:
            _queue.task_done()


def start_worker() -> None:
    global _worker_thread
    with _lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker, name="odp-worker", daemon=True)
            _worker_thread.start()


def submit(run_id: str, name: str, params: dict, dataset_ids: list[str]) -> dict:
    store.create_run(run_id, name, params, dataset_ids)
    start_worker()
    _queue.put(run_id)
    run = store.get_run(run_id)
    assert run is not None
    return run


def queue_depth() -> int:
    return _queue.qsize()
