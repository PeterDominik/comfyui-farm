"""SQLite job store. The manager's database is the source of truth for job state;
ComfyUI's own queue and history are in memory and vanish when it restarts.

Job kinds: "job" (one ComfyUI run), and for texture sets too large for one run, a "set"
(what the artist submitted; one output folder; its status is worked out from its
batches) and its "batch" jobs (parent_id = the set), each an ordinary run."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from ..protocol import ACTIVE, FINISHED, ON_WORKER, now

__all__ = ["JobStore", "ACTIVE", "ON_WORKER", "FINISHED", "now"]

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
    output_key TEXT,
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
            # Columns added after the first release (CREATE TABLE IF NOT EXISTS won't add them).
            have = {r[1] for r in self._db.execute("PRAGMA table_info(jobs)")}
            if "output_key" not in have:
                self._db.execute("ALTER TABLE jobs ADD COLUMN output_key TEXT")
            if "kind" not in have:
                self._db.execute("ALTER TABLE jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'job'")
            if "parent_id" not in have:
                self._db.execute("ALTER TABLE jobs ADD COLUMN parent_id TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_output_key ON jobs(output_key)")
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_id)")

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _row(row: sqlite3.Row | None, with_prompt: bool = False) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        if not with_prompt:   # before parsing: a set's batches are listed often, prompts are big
            d.pop("prompt", None)
        for k in JSON_FIELDS:
            if k in d and d[k] is not None:
                d[k] = json.loads(d[k])
        d["warnings"] = d.get("warnings") or []
        d["short_id"] = d["id"][:8]
        return d

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---------------------------------------------------------------- jobs
    @staticmethod
    def _encoded(job: dict) -> dict:
        job = dict(job)
        for k in JSON_FIELDS:
            if k in job and job[k] is not None:
                job[k] = json.dumps(job[k])
        return job

    def insert(self, job: dict) -> dict:
        job = self._encoded(job)
        cols = ",".join(job)
        with self._lock:
            self._db.execute(f"INSERT INTO jobs ({cols}) VALUES ({','.join('?' * len(job))})",
                             tuple(job.values()))
        return self.get(job["id"])

    def insert_many(self, jobs: list[dict]) -> None:
        """Several jobs in one transaction (a set's batches): one disk sync, not hundreds."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for job in map(self._encoded, jobs):
                    self._db.execute(f"INSERT INTO jobs ({','.join(job)}) VALUES ({','.join('?' * len(job))})",
                                     tuple(job.values()))
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

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

    def list(self, user: str | None = None, statuses: tuple | None = None, limit: int = 200,
             parent: str | None = None, top_level: bool = True, work_only: bool = False) -> list[dict]:
        """Newest first. By default what artists submitted: jobs and sets, not a set's
        batches. `parent`: that set's batches, in order. `top_level=False` includes
        batches; `work_only` leaves out sets (the farm's own loops: only real runs)."""
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if user:
            sql += " AND user=?"
            args.append(user)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if parent:
            sql += " AND parent_id=?"
            args.append(parent)
        elif top_level:
            sql += " AND parent_id IS NULL"
        if work_only:
            sql += " AND kind != 'set'"
        sql += " ORDER BY created_at ASC, rowid ASC LIMIT ?" if parent else " ORDER BY created_at DESC, rowid DESC LIMIT ?"
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

    def queued(self, with_prompt: bool = False) -> list[dict]:
        """Queued runs in dispatch order: priority, then the user with the fewest runs
        already on the farm (so one person can't hog the GPU), then the set (or job) with
        the fewest (so a big set lets other jobs in between its batches, the same
        artist's too), then age."""
        rows = self._q("SELECT * FROM jobs WHERE status='queued' AND kind != 'set' "
                       "ORDER BY created_at ASC, rowid ASC")
        busy = self._q("SELECT user, COALESCE(parent_id, id) AS grp FROM jobs "
                       "WHERE status IN ('dispatched','running') AND kind != 'set'")
        users, groups = Counter(r["user"] for r in busy), Counter(r["grp"] for r in busy)
        rows = sorted(rows, key=lambda r: (-r["priority"], users[r["user"]], groups[r["parent_id"] or r["id"]]))
        return [self._row(r, with_prompt=with_prompt) for r in rows]

    def batch_states(self, set_id: str) -> list[dict]:
        """What a set's status is worked out from: each batch's state, without its prompt
        or full info."""
        rows = self._q("""
            SELECT id, status, progress, error, cancel_requested, dispatched_at, started_at, finished_at,
                   json_extract(info, '$.batch.tiles') AS tiles, json_extract(info, '$.batch.label') AS label,
                   json_extract(info, '$.textures.items') AS items, json_extract(outputs, '$.files') AS files
            FROM jobs WHERE parent_id=? ORDER BY created_at ASC, rowid ASC""", (set_id,))
        return [dict(r) for r in rows]

    def queued_batches(self) -> Counter:
        """Converted batches waiting for the GPU, per set."""
        return Counter(r[0] for r in self._q(
            "SELECT parent_id FROM jobs WHERE status='queued' AND parent_id IS NOT NULL"))

    def output_reserved(self, key: str) -> bool:
        """This output (folder + file pattern) is promised to a job that hasn't finished,
        so it may not have written anything yet."""
        marks = ",".join("?" * len(ACTIVE))
        return bool(self._q(f"SELECT 1 FROM jobs WHERE output_key=? COLLATE NOCASE AND status IN ({marks}) LIMIT 1",
                            (key, *ACTIVE)))

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
