"""SQLite persistence for datasets, field mappings, runs and job state.

The metadata store is deliberately small: heavy tabular artefacts live as
Parquet files under ``data/``; SQLite only keeps pointers and parameters so a
run stays reproducible.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import DB_PATH

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _lock, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                filename TEXT,
                n_rows INTEGER DEFAULT 0,
                n_cols INTEGER DEFAULT 0,
                size_bytes INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                meta TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS field_mappings (
                dataset_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                mapping TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (dataset_id, kind),
                FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                name TEXT,
                status TEXT NOT NULL,
                params TEXT NOT NULL,
                progress REAL DEFAULT 0,
                stage TEXT DEFAULT '',
                message TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                finished_at TEXT,
                elapsed_sec REAL,
                dataset_ids TEXT DEFAULT '[]',
                summary TEXT DEFAULT '{}',
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
            """
        )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def add_dataset(name: str, kind: str, filename: str, n_rows: int, n_cols: int,
                size_bytes: int, meta: dict | None = None,
                ds_id: str | None = None) -> dict:
    ds_id = ds_id or new_id("ds")
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO datasets(id,name,kind,filename,n_rows,n_cols,size_bytes,created_at,meta)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (ds_id, name, kind, filename, n_rows, n_cols, size_bytes, _now(),
             json.dumps(meta or {}, ensure_ascii=False)),
        )
    return get_dataset(ds_id)


def get_dataset(ds_id: str) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM datasets WHERE id=?", (ds_id,)).fetchone()
    return _row_dataset(row) if row else None


def _row_dataset(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def list_datasets() -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM datasets ORDER BY created_at DESC, rowid DESC"
        ).fetchall()
    return [_row_dataset(r) for r in rows]


def delete_dataset(ds_id: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM datasets WHERE id=?", (ds_id,))


def set_mapping(ds_id: str, kind: str, mapping: dict) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO field_mappings(dataset_id,kind,mapping,updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(dataset_id,kind) DO UPDATE SET mapping=excluded.mapping,"
            " updated_at=excluded.updated_at",
            (ds_id, kind, json.dumps(mapping, ensure_ascii=False), _now()),
        )


def get_mapping(ds_id: str, kind: str) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT mapping FROM field_mappings WHERE dataset_id=? AND kind=?", (ds_id, kind)
        ).fetchone()
    return json.loads(row["mapping"]) if row else None


def create_run(run_id: str, name: str, params: dict, dataset_ids: list[str]) -> None:
    with _lock, _conn() as conn:
        conn.execute(
            "INSERT INTO runs(id,name,status,params,created_at,dataset_ids)"
            " VALUES(?,?,?,?,?,?)",
            (run_id, name, "queued", json.dumps(params, ensure_ascii=False),
             _now(), json.dumps(dataset_ids)),
        )


def update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"status", "progress", "stage", "message", "finished_at",
               "elapsed_sec", "summary", "error", "params", "name"}
    clean = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        clean[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
    sets = ", ".join(f"{k}=?" for k in clean)
    with _lock, _conn() as conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id=?", (*clean.values(), run_id))


def get_run(run_id: str) -> dict | None:
    with _lock, _conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _row_run(row) if row else None


def _row_run(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("params", "summary"):
        d[k] = json.loads(d.get(k) or "{}")
    d["dataset_ids"] = json.loads(d.get("dataset_ids") or "[]")
    return d


def list_runs() -> list[dict]:
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT id,name,status,params,progress,stage,message,created_at,finished_at,"
            "elapsed_sec,dataset_ids,summary,error FROM runs ORDER BY created_at DESC"
        ).fetchall()
    return [_row_run(r) for r in rows]


def delete_run(run_id: str) -> None:
    with _lock, _conn() as conn:
        conn.execute("DELETE FROM runs WHERE id=?", (run_id,))


def mark_stale_runs_interrupted() -> int:
    """Runs left 'running' after a process restart can never finish."""
    with _lock, _conn() as conn:
        cur = conn.execute(
            "UPDATE runs SET status='failed', message='服务重启，任务中断',"
            " error='interrupted' WHERE status IN ('queued','running')"
        )
        return cur.rowcount
