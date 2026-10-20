"""SQLite job store. The gateway's database is the source of truth for job state;
ComfyUI's own queue and history are in memory and vanish when it restarts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

ACTIVE = ("queued", "dispatched", "running", "postprocessing")
ON_WORKER = ("dispatched", "running")
FINISHED = ("done", "failed", "cancelled")
JSON_FIELDS = ("params", "info", "warnings", "outputs", "prompt")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    workflow TEXT NOT NULL,
    user TEXT NOT NULL,
    name TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    params TEXT,
    info TEXT,
    prompt TEXT,
    worker TEXT,
    prompt_id TEXT,
    progress REAL NOT NULL DEFAULT 0,
    stage TEXT,
    error TEXT,
    warnings TEXT,
    outputs TEXT,
    output_dir TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    dispatched_at REAL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user);
CREATE INDEX IF NOT EXISTS jobs_output_dir ON jobs(output_dir);
CREATE TABLE IF NOT EXISTS workflow_stats (
    workflow TEXT PRIMARY KEY,
    runs INTEGER NOT NULL DEFAULT 0,
    avg_seconds REAL,
    node_count INTEGER
);
"""


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _row(row: sqlite3.Row | None, with_prompt: bool = False) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        for k in JSON_FIELDS:
            if k in d and d[k] is not None:
                d[k] = json.loads(d[k])
        if not with_prompt:
            d.pop("prompt", None)
        d["warnings"] = d.get("warnings") or []
        d["short_id"] = d["id"][:8]
        return d

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---------------------------------------------------------------- jobs
    def insert(self, job: dict) -> dict:
        job = dict(job)
        for k in JSON_FIELDS:
            if k in job and job[k] is not None:
                job[k] = json.dumps(job[k])
        cols = ",".join(job)
        with self._lock:
            self._db.execute(f"INSERT INTO jobs ({cols}) VALUES ({','.join('?' * len(job))})",
                             tuple(job.values()))
        return self.get(job["id"])

    def update(self, job_id: str, **fields: Any) -> dict | None:
        if fields:
            for k in JSON_FIELDS:
                if k in fields and fields[k] is not None:
                    fields[k] = json.dumps(fields[k])
            sets = ",".join(f"{k}=?" for k in fields)
            with self._lock:
                self._db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
        return self.get(job_id)

    def transition(self, job_id: str, from_statuses: tuple, **fields: Any) -> dict | None:
        """Update only if the job is still in one of from_statuses (guards races
        between websocket events, reconciliation and cancels). None if it wasn't."""
        for k in JSON_FIELDS:
            if k in fields and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        sets = ",".join(f"{k}=?" for k in fields)
        marks = ",".join("?" * len(from_statuses))
        with self._lock:
            cur = self._db.execute(
                f"UPDATE jobs SET {sets} WHERE id=? AND status IN ({marks})",
                (*fields.values(), job_id, *from_statuses))
            if cur.rowcount == 0:
                return None
        return self.get(job_id)

    def get(self, job_id: str, with_prompt: bool = False) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows and len(job_id) >= 6:
            rows = self._q("SELECT * FROM jobs WHERE id LIKE ? LIMIT 2", (job_id + "%",))
            if len(rows) != 1:
                return None
        return self._row(rows[0] if rows else None, with_prompt)

    def by_prompt_id(self, prompt_id: str) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE prompt_id=?", (prompt_id,))
        return self._row(rows[0] if rows else None)

    def list(self, user: str | None = None, statuses: tuple | None = None, limit: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if user:
            sql += " AND user=?"
            args.append(user)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self._q(sql, tuple(args))]

    def on_worker(self, worker: str, statuses: tuple = ON_WORKER) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        return [self._row(r, with_prompt=True) for r in self._q(
            f"SELECT * FROM jobs WHERE worker=? AND status IN ({marks})", (worker, *statuses))]

    def count_on_worker(self, worker: str) -> int:
        marks = ",".join("?" * len(ON_WORKER))
        return self._q(f"SELECT COUNT(*) FROM jobs WHERE worker=? AND status IN ({marks})",
                       (worker, *ON_WORKER))[0][0]

    def queued(self, with_prompt: bool = True) -> list[dict]:
        """Queued jobs in dispatch order: priority, then the user with the fewest
        jobs already on the farm (so one person can't hog the GPU), then age."""
        rows = self._q("""
            SELECT j.*, (SELECT COUNT(*) FROM jobs k WHERE k.user=j.user
                         AND k.status IN ('dispatched','running')) AS user_inflight
            FROM jobs j WHERE j.status='queued'
            ORDER BY j.priority DESC, user_inflight ASC, j.created_at ASC""")
        return [self._row(r, with_prompt=with_prompt) for r in rows]

    def output_dir_reserved(self, path: str) -> bool:
        """Promised to a job that hasn't finished (it may not have written frames yet)."""
        marks = ",".join("?" * len(ACTIVE))
        return bool(self._q(f"SELECT 1 FROM jobs WHERE output_dir=? COLLATE NOCASE AND status IN ({marks}) LIMIT 1",
                            (path, *ACTIVE)))

    def delete_finished_before(self, ts: float) -> int:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM jobs WHERE status IN ('done','failed','cancelled') AND finished_at < ?", (ts,))
            return cur.rowcount

    # ---------------------------------------------------------------- stats
    def stats(self, workflow: str) -> dict:
        rows = self._q("SELECT * FROM workflow_stats WHERE workflow=?", (workflow,))
        return dict(rows[0]) if rows else {"workflow": workflow, "runs": 0, "avg_seconds": None, "node_count": None}

    def record_run(self, workflow: str, seconds: float, node_count: int | None) -> None:
        s = self.stats(workflow)
        runs = s["runs"] + 1
        # Moving average weighted toward recent runs.
        avg = seconds if not s["avg_seconds"] else s["avg_seconds"] * 0.7 + seconds * 0.3
        with self._lock:
            self._db.execute(
                "INSERT INTO workflow_stats (workflow, runs, avg_seconds, node_count) VALUES (?,?,?,?) "
                "ON CONFLICT(workflow) DO UPDATE SET runs=excluded.runs, avg_seconds=excluded.avg_seconds, "
                "node_count=COALESCE(excluded.node_count, node_count)",
                (workflow, runs, avg, node_count))


def now() -> float:
    return time.time()
