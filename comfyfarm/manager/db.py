"""SQLite job store. The manager's database is the source of truth for job state;
ComfyUI's own queue and history are in memory and vanish when it restarts.

Job kinds: "job" (one ComfyUI run), and for texture sets too large for one run, a "set"
(what the artist submitted; one output folder; its status is worked out from its
batches) and its "batch" jobs (parent_id = the set), each an ordinary run."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from ..protocol import ACTIVE, FINISHED, HELD, ON_WORKER, now
from .eta import fold, units

log = logging.getLogger("comfyfarm")

__all__ = ["JobStore", "ACTIVE", "ON_WORKER", "HELD", "FINISHED", "now"]

JSON_FIELDS = ("params", "info", "warnings", "outputs", "prompt", "request")

# Stages worth a line in a job's timeline: the steps before and after the GPU run. While it
# runs, the stage is the node ComfyUI is on, which changes too often to keep.
STAGE_EVENTS = ("preparing", "postprocessing")
_COUNTER = re.compile(r"\s+\d+/\d+$")      # "converting EXR to PNG 12/240" is one step, not 240


def stage_label(stage: str | None) -> str:
    return _COUNTER.sub("", stage or "")

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
    prompt TEXT,                -- what its last run handed its backend, as the agent reported it (for ComfyUI
                                -- the API JSON); in rows from before agents built graphs, the graph to run
    request TEXT,               -- what the machine that runs it builds that from (workflows.request)
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
CREATE TABLE IF NOT EXISTS drained (
    worker TEXT PRIMARY KEY,
    by TEXT,
    since REAL
);
CREATE TABLE IF NOT EXISTS rollouts (         -- a release going to the machines one at a time (0031)
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,                       -- the environment
    release TEXT NOT NULL,
    machines TEXT NOT NULL,                   -- JSON: in order, the canary first
    before TEXT NOT NULL,                     -- JSON: machine -> what it had (fingerprint or null)
    step INTEGER NOT NULL DEFAULT 0,          -- the machine it is at
    state TEXT NOT NULL,                      -- running, paused, failed, done, rolled_back, cancelled
    error TEXT,
    by TEXT,
    started_at REAL,
    step_started REAL,
    updated_at REAL,
    gate TEXT                                 -- JSON: its evals, their jobs, what it drained (0053)
);
CREATE TABLE IF NOT EXISTS syncs (            -- a runner's environment synced on the machines in turn (0033)
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,                      -- running, failed, done, cancelled
    started_at REAL,
    updated_at REAL,
    data TEXT NOT NULL                        -- JSON: the rest (manager/syncs.py)
);
CREATE TABLE IF NOT EXISTS env_wanted (       -- the runner environments each machine should have (0030)
    worker TEXT NOT NULL,
    name TEXT NOT NULL,
    release TEXT,                             -- its fingerprint; NULL: removed
    by TEXT,
    since REAL,
    PRIMARY KEY (worker, name)
);
CREATE TABLE IF NOT EXISTS run_stats (
    workflow TEXT NOT NULL,
    gpu TEXT NOT NULL,
    runs INTEGER NOT NULL DEFAULT 0,
    w REAL, wx REAL, wy REAL, wxx REAL, wxy REAL,
    PRIMARY KEY (workflow, gpu)
);
CREATE TABLE IF NOT EXISTS publishes (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    user TEXT,
    state TEXT NOT NULL,        -- staging, dry_run, dispatched, failed
    show TEXT, shot TEXT, step TEXT, template TEXT, description TEXT, frames TEXT,
    workfile TEXT,              -- the staged .nk
    command TEXT,
    output TEXT,                -- dispatchGraph's
    tractor_id TEXT, title TEXT, error TEXT, notes TEXT,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS publishes_job ON publishes(job_id, created_at);
CREATE TABLE IF NOT EXISTS job_events (      -- a job's timeline: what happened to it, and when
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    at REAL NOT NULL,
    event TEXT NOT NULL,        -- submitted, a status, requeued, retried, cancel_requested, stage
    status TEXT,                -- the job's status after it
    worker TEXT,
    attempt TEXT,               -- the run's attempt id (jobs.prompt_id)
    detail TEXT                 -- JSON: stage, error, reason
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, id);
CREATE TABLE IF NOT EXISTS workflow_stats (
    workflow TEXT PRIMARY KEY,
    runs INTEGER NOT NULL DEFAULT 0,
    avg_seconds REAL,
    node_count INTEGER
);
CREATE TABLE IF NOT EXISTS job_metrics (      -- one small row per finished job, kept when the job is deleted (0040)
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                       -- job, set (what was submitted) or batch (one run of a set)
    parent_id TEXT,                           -- a batch's set
    workflow TEXT NOT NULL,                   -- the definition
    fingerprint TEXT,                         -- its fingerprint, pinned at submit
    user TEXT NOT NULL,
    show TEXT,
    worker TEXT,                              -- the machine that ran it last
    gpu TEXT,                                 -- that machine's GPU type
    outcome TEXT NOT NULL,                    -- done, failed or cancelled
    attempts INTEGER NOT NULL DEFAULT 0,
    units INTEGER,                            -- its frames or tiles
    created_at REAL NOT NULL,                 -- submitted
    assigned_at REAL,                         -- given to the machine (its last attempt)
    dispatched_at REAL,                       -- handed to the runner, its inputs ready
    started_at REAL,                          -- running on the GPU
    finished_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS job_metrics_finished ON job_metrics(finished_at);
CREATE TABLE IF NOT EXISTS gpu_samples (       -- each machine's GPUs over time, for the dashboard (0049)
    machine TEXT NOT NULL,
    gpu INTEGER NOT NULL,                     -- its index on the machine
    at REAL NOT NULL,
    util REAL,                                -- 0 to 100, from gpu_probe (nvidia-smi); none from ComfyUI
    used REAL,                                -- memory in use, bytes
    total REAL,                               -- memory, bytes
    jobs INTEGER                              -- jobs the machine held then
);
CREATE INDEX IF NOT EXISTS gpu_samples_machine ON gpu_samples(machine, at);
CREATE TABLE IF NOT EXISTS alerts (            -- what the manager alerted about, raised and cleared (0052)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,                        -- what it is about (machine_silent:wrk59467w-5090): one open at a time
    kind TEXT NOT NULL,                       -- its rule
    subject TEXT,                             -- the machine, rollout or sync, if any
    message TEXT NOT NULL,                    -- what is wrong and what to do, as it last read
    raised_at REAL NOT NULL,
    cleared_at REAL                           -- none while it holds
);
CREATE INDEX IF NOT EXISTS alerts_cleared ON alerts(cleared_at);
"""

# What a metrics row keeps of a job: who, what, where and how long, and never its params,
# prompts, paths or errors (decisions 0006, 0040).
METRICS_COLUMNS = ("job_id", "kind", "parent_id", "workflow", "fingerprint", "user", "show", "worker", "gpu",
                   "outcome", "attempts", "units", "created_at", "assigned_at", "dispatched_at", "started_at",
                   "finished_at")


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
            if "request" not in have:
                self._db.execute("ALTER TABLE jobs ADD COLUMN request TEXT")
            if "lease_until" not in have:   # an agent holds the job until then (protocol.py)
                self._db.execute("ALTER TABLE jobs ADD COLUMN lease_until REAL")
            if "gpu" not in have:           # the GPU type of the machine it was given to, for its metrics
                self._db.execute("ALTER TABLE jobs ADD COLUMN gpu TEXT")
            if "gate" not in {r[1] for r in self._db.execute("PRAGMA table_info(rollouts)")}:   # evals (0053)
                self._db.execute("ALTER TABLE rollouts ADD COLUMN gate TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_output_key ON jobs(output_key)")
            self._db.execute("CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_id)")
            # Jobs that finished before there were metrics rows get theirs, before anything
            # deletes them.
            marks = ",".join("?" * len(FINISHED))
            for (jid,) in self._db.execute(f"SELECT id FROM jobs WHERE status IN ({marks}) AND id NOT IN "
                                           f"(SELECT job_id FROM job_metrics)", FINISHED).fetchall():
                self._record_metrics(jid)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _row(row: sqlite3.Row | None, with_prompt: bool = False) -> dict | None:
        if row is None:
            return None
        d = dict(row)
        if not with_prompt:   # before parsing: a set's batches are listed often, prompts are big
            d.pop("prompt", None)
            d.pop("request", None)
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
            self._submitted(job)
        return self.get(job["id"])

    def insert_many(self, jobs: list[dict]) -> None:
        """Several jobs in one transaction (a set's batches): one disk sync, not hundreds."""
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for job in map(self._encoded, jobs):
                    self._db.execute(f"INSERT INTO jobs ({','.join(job)}) VALUES ({','.join('?' * len(job))})",
                                     tuple(job.values()))
                    self._submitted(job)
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def update(self, job_id: str, **fields: Any) -> dict | None:
        if fields:
            plain = dict(fields)
            for k in JSON_FIELDS:
                if k in fields and fields[k] is not None:
                    fields[k] = json.dumps(fields[k])
            sets = ",".join(f"{k}=?" for k in fields)
            with self._lock:
                before = self._before(job_id)
                self._db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
                self._changed(before, plain)
        return self.get(job_id)

    def transition(self, job_id: str, from_statuses: tuple, *, only_prompt: str | None = None,
                   **fields: Any) -> dict | None:
        """Update only if the job is still in one of from_statuses (guards races
        between websocket events, reconciliation and cancels), and with only_prompt, only
        while that is still its ComfyUI prompt (the same run, not a later attempt). None
        if it wasn't."""
        plain = dict(fields)
        for k in JSON_FIELDS:
            if k in fields and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        sets = ",".join(f"{k}=?" for k in fields)
        marks = ",".join("?" * len(from_statuses))
        where, args = f"id=? AND status IN ({marks})", [job_id, *from_statuses]
        if only_prompt is not None:
            where += " AND prompt_id=?"
            args.append(only_prompt)
        with self._lock:
            before = self._before(job_id)
            cur = self._db.execute(f"UPDATE jobs SET {sets} WHERE {where}", (*fields.values(), *args))
            if cur.rowcount == 0:
                return None
            self._changed(before, plain)
        return self.get(job_id)

    # ---------------------------------------------------------------- timeline
    # Written here, where every change to a job passes, so no caller can forget one. Always
    # called with the lock held, in the same breath as the change itself.
    def _event(self, job_id: str, event: str, status: str | None, worker: str | None = None,
               attempt: str | None = None, **detail: Any) -> None:
        detail = {k: v for k, v in detail.items() if v}
        self._db.execute("INSERT INTO job_events (job_id, at, event, status, worker, attempt, detail) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (job_id, now(), event, status, worker, attempt, json.dumps(detail) if detail else None))

    def _submitted(self, job: dict) -> None:
        kind = job.get("kind")
        self._event(job["id"], "submitted", job.get("status"), kind=kind if kind != "job" else None)

    def _before(self, job_id: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT id, status, stage, worker, prompt_id, cancel_requested FROM jobs WHERE id=?",
                                (job_id,)).fetchone()

    def _changed(self, before: sqlite3.Row | None, fields: dict) -> None:
        """The timeline line for one change, if it is worth one: a new status, a cancel
        asked for, or a new step while preparing or collecting outputs."""
        if before is None:
            return
        status = fields.get("status", before["status"])
        worker = fields.get("worker") or before["worker"]          # a requeue clears it: keep where it was
        attempt = fields.get("prompt_id") or before["prompt_id"]
        if status != before["status"]:
            event, detail = status, {"error": fields.get("error")}
            if status == "queued" and before["status"] in HELD:
                event = "requeued"
                detail["reason"] = (fields.get("warnings") or [None])[-1]
            elif status == "queued" and before["status"] in FINISHED:
                event = "retried"
            if status in STAGE_EVENTS or fields.get("stage") not in (None, status, "queued", "requeued"):
                detail["stage"] = stage_label(fields.get("stage"))
            self._event(before["id"], event, status, worker, attempt, **detail)
            if status in FINISHED:
                self._record_metrics(before["id"])
        elif fields.get("cancel_requested") and not before["cancel_requested"]:
            self._event(before["id"], "cancel_requested", status, worker, attempt)
        elif status in STAGE_EVENTS and fields.get("stage") is not None:
            label = stage_label(fields["stage"])
            if label != stage_label(before["stage"]):
                self._event(before["id"], "stage", status, worker, attempt, stage=label)

    # ---------------------------------------------------------------- metrics
    # Also written here, for the same reason: every way a job finishes passes through
    # _changed. A job finishing again (retried) replaces its row.
    def _record_metrics(self, job_id: str) -> None:
        """The finished job's metrics row (decision 0040). Never a reason for the change to the
        job to fail: a row that can't be written is logged."""
        try:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in FINISHED:
                return
            info = json.loads(row["info"] or "null") or {}
            request = json.loads(row["request"] or "null") or {}
            assigned = self._db.execute("SELECT at FROM job_events WHERE job_id=? AND event='preparing' "
                                        "ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
            rec = {"job_id": row["id"], "kind": row["kind"] or "job", "parent_id": row["parent_id"],
                   "workflow": row["workflow"], "fingerprint": request.get("definition"), "user": row["user"],
                   "show": info.get("show"), "worker": row["worker"], "gpu": row["gpu"], "outcome": row["status"],
                   "attempts": row["attempts"] or 0, "units": units({"info": info}),
                   "created_at": row["created_at"], "assigned_at": assigned["at"] if assigned else None,
                   "dispatched_at": row["dispatched_at"], "started_at": row["started_at"],
                   "finished_at": row["finished_at"] or now()}
            self._db.execute(f"INSERT OR REPLACE INTO job_metrics ({','.join(METRICS_COLUMNS)}) "
                             f"VALUES ({','.join('?' * len(METRICS_COLUMNS))})", tuple(rec[c] for c in METRICS_COLUMNS))
        except Exception as e:  # noqa: BLE001
            log.warning("metrics row for job %s: %s", job_id[:8], e)

    def job_metrics(self, since: float | None = None, until: float | None = None, limit: int = 10000) -> list[dict]:
        """Metrics rows of the jobs that finished between since and until, oldest first."""
        where, args = [], []
        if since is not None:
            where.append("finished_at >= ?")
            args.append(since)
        if until is not None:
            where.append("finished_at < ?")
            args.append(until)
        sql = "SELECT * FROM job_metrics" + (f" WHERE {' AND '.join(where)}" if where else "")
        return [dict(r) for r in self._q(sql + " ORDER BY finished_at, job_id LIMIT ?", (*args, limit))]

    # ---------------------------------------------------------------- GPU history (0049)
    def add_gpu_samples(self, rows: list[tuple]) -> None:
        """(machine, gpu, at, util, used, total, jobs) each."""
        if rows:
            with self._lock:
                self._db.executemany("INSERT INTO gpu_samples (machine, gpu, at, util, used, total, jobs) "
                                     "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def gpu_samples(self, machine: str, since: float, step: float) -> list[dict]:
        """A machine's samples since `since`, one per GPU every `step` seconds: use averaged,
        memory and jobs at their highest, oldest first."""
        return [dict(r) for r in self._q(
            "SELECT gpu, MIN(at) AS at, AVG(util) AS util, MAX(used) AS used, MAX(total) AS total, "
            "MAX(jobs) AS jobs FROM gpu_samples WHERE machine=? AND at>=? "
            "GROUP BY gpu, CAST(at / ? AS INTEGER) ORDER BY at, gpu", (machine, since, step))]

    def prune_gpu_samples(self, before: float) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM gpu_samples WHERE at < ?", (before,)).rowcount

    def machines_sampled(self) -> dict[str, float]:
        """Each machine in the GPU history, with its latest sample: when it last checked in, for
        the machines that haven't since the manager started (alerts, decision 0052)."""
        rows = self._q("SELECT machine, MAX(at) AS at FROM gpu_samples GROUP BY machine")
        return {r["machine"]: r["at"] for r in rows}

    # ---------------------------------------------------------------- alerts (0052)
    def open_alerts(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM alerts WHERE cleared_at IS NULL ORDER BY raised_at, id")]

    def cleared_alerts(self, limit: int = 50) -> list[dict]:
        """The latest cleared, newest first."""
        return [dict(r) for r in self._q("SELECT * FROM alerts WHERE cleared_at IS NOT NULL "
                                         "ORDER BY cleared_at DESC, id DESC LIMIT ?", (limit,))]

    def raise_alert(self, key: str, kind: str, subject: str | None, message: str, t: float) -> dict:
        with self._lock:
            cur = self._db.execute("INSERT INTO alerts (key, kind, subject, message, raised_at) VALUES (?, ?, ?, ?, ?)",
                                   (key, kind, subject, message, t))
        return {"id": cur.lastrowid, "key": key, "kind": kind, "subject": subject, "message": message,
                "raised_at": t, "cleared_at": None}

    def alert_message(self, alert_id: int, message: str) -> None:
        with self._lock:
            self._db.execute("UPDATE alerts SET message=? WHERE id=?", (message, alert_id))

    def clear_alert(self, alert_id: int, t: float) -> None:
        with self._lock:
            self._db.execute("UPDATE alerts SET cleared_at=? WHERE id=? AND cleared_at IS NULL", (t, alert_id))

    def prune_alerts(self, before: float) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM alerts WHERE cleared_at < ?", (before,)).rowcount

    def last_outcomes(self, n: int) -> dict[str, list[dict]]:
        """Each machine's last `n` runs that finished done or failed (cancelled ones say nothing
        of the machine), newest first, from job_metrics: failures in a row (alerts)."""
        out: dict[str, list[dict]] = {}
        for r in self._q("SELECT worker, job_id, workflow, outcome, finished_at FROM ("
                         "SELECT *, ROW_NUMBER() OVER (PARTITION BY worker ORDER BY finished_at DESC, job_id DESC) "
                         "AS n FROM job_metrics "
                         "WHERE kind != 'set' AND worker IS NOT NULL AND outcome IN ('done', 'failed')) "
                         "WHERE n <= ? ORDER BY worker, finished_at DESC, job_id DESC", (n,)):
            out.setdefault(r["worker"], []).append(dict(r))
        return out

    def metrics_totals(self) -> list[dict]:
        """job_metrics summed per definition, outcome and GPU type, runs only (a set's batches,
        not the set): how many, and for those done that ran, their seconds and frames; for
        those given to a machine, their wait (GET /metrics, decision 0048)."""
        ran = "outcome='done' AND started_at IS NOT NULL"
        return [dict(r) for r in self._q(
            f"SELECT workflow, outcome, COALESCE(gpu, '') AS gpu, COUNT(*) AS n, "
            f"SUM(CASE WHEN {ran} THEN finished_at - started_at END) AS run, "
            f"SUM(CASE WHEN {ran} THEN 1 ELSE 0 END) AS runs, "
            f"SUM(CASE WHEN {ran} THEN COALESCE(units, 0) END) AS frames, "
            f"SUM(CASE WHEN assigned_at IS NOT NULL THEN assigned_at - created_at END) AS wait, "
            f"SUM(CASE WHEN assigned_at IS NOT NULL THEN 1 ELSE 0 END) AS waits "
            f"FROM job_metrics WHERE kind != 'set' GROUP BY workflow, outcome, COALESCE(gpu, '') "
            f"ORDER BY workflow, outcome, gpu", ())]

    # The Usage tab's groups (decision 0050): a day in the manager's local time, or a column.
    USAGE_KEYS = {"day": "date(finished_at, 'unixepoch', 'localtime')", "show": "COALESCE(show, '')",
                  "workflow": "workflow", "machine": "COALESCE(worker, '')", "all": "''"}

    def usage(self, since: float, by: str, show: str | None = None, workflow: str | None = None) -> list[dict]:
        """Runs (jobs and a set's batches, not the set) that finished since `since`, summed per
        `by` (USAGE_KEYS): how many and how they ended, their seconds on a GPU (any outcome:
        a failed run held the GPU too) and those of runs that didn't finish done, the frames
        of those done, and their average wait to be given to a machine."""
        on_gpu = "started_at IS NOT NULL AND finished_at > started_at"
        where, args = ["kind != 'set'", "finished_at >= ?"], [since]
        if show is not None:
            where.append("COALESCE(show, '') = ?")
            args.append(show)
        if workflow is not None:
            where.append("workflow = ?")
            args.append(workflow)
        return [dict(r) for r in self._q(
            f"SELECT {self.USAGE_KEYS[by]} AS key, COUNT(*) AS jobs, "
            f"SUM(outcome = 'done') AS done, SUM(outcome = 'failed') AS failed, "
            f"SUM(outcome = 'cancelled') AS cancelled, "
            f"SUM(CASE WHEN {on_gpu} THEN finished_at - started_at ELSE 0 END) AS gpu_seconds, "
            f"SUM(CASE WHEN {on_gpu} AND outcome != 'done' THEN finished_at - started_at ELSE 0 END) "
            f"AS lost_seconds, "
            f"SUM(CASE WHEN outcome = 'done' THEN COALESCE(units, 0) ELSE 0 END) AS frames, "
            f"AVG(CASE WHEN assigned_at >= created_at THEN assigned_at - created_at END) AS wait_seconds "
            f"FROM job_metrics WHERE {' AND '.join(where)} GROUP BY key ORDER BY key", tuple(args))]

    def usage_values(self, since: float) -> dict:
        """The shows, definitions and machines of runs that finished since `since`: what the
        Usage tab's filters offer."""
        def distinct(col):
            return [r["v"] for r in self._q(f"SELECT DISTINCT {col} AS v FROM job_metrics "
                                             f"WHERE kind != 'set' AND finished_at >= ? ORDER BY v", (since,))]
        return {"shows": distinct("COALESCE(show, '')"), "workflows": distinct("workflow")}

    def events(self, job_id: str) -> list[dict]:
        """A job's timeline, oldest first."""
        out = []
        for r in self._q("SELECT at, event, status, worker, attempt, detail FROM job_events "
                         "WHERE job_id=? ORDER BY id", (job_id,)):
            d = dict(r)
            d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            out.append(d)
        return out

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
                       "WHERE status IN ('preparing','dispatched','running') AND kind != 'set'")
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

    def held_by(self, worker: str, with_prompt: bool = False) -> list[dict]:
        """The jobs leased to this agent (preparing, in its ComfyUI, or collecting outputs)."""
        return self.on_worker(worker, HELD) if with_prompt else [
            self._row(r) for r in self._q(f"SELECT * FROM jobs WHERE worker=? AND status IN ({','.join('?' * len(HELD))})",
                                          (worker, *HELD))]

    def lease_expired(self, t: float) -> list[dict]:
        """Held jobs whose agent hasn't renewed the lease by t."""
        marks = ",".join("?" * len(HELD))
        return [self._row(r) for r in self._q(
            f"SELECT * FROM jobs WHERE status IN ({marks}) AND (lease_until IS NULL OR lease_until < ?)", (*HELD, t))]

    def extend_leases(self, until: float) -> int:
        """Every held job's lease runs until at least `until` (the manager starting: its
        agents get a full lease to check in again)."""
        marks = ",".join("?" * len(HELD))
        with self._lock:
            return self._db.execute(f"UPDATE jobs SET lease_until=? WHERE status IN ({marks}) AND "
                                    f"(lease_until IS NULL OR lease_until < ?)", (until, *HELD, until)).rowcount

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
            self._db.execute("DELETE FROM job_events WHERE job_id NOT IN (SELECT id FROM jobs)")
            return cur.rowcount

    # ---------------------------------------------------------------- drained machines
    def drained(self) -> dict[str, dict]:
        """Machines taking no new jobs (drained from the dashboard): name -> {by, since}."""
        return {r["worker"]: {"by": r["by"], "since": r["since"]} for r in self._q("SELECT * FROM drained")}

    def set_drained(self, worker: str, by: str | None, on: bool) -> None:
        with self._lock:
            if on:
                self._db.execute("INSERT OR REPLACE INTO drained (worker, by, since) VALUES (?,?,?)",
                                 (worker, by, now()))
            else:
                self._db.execute("DELETE FROM drained WHERE worker=?", (worker,))

    # ---------------------------------------------------------------- environments wanted
    def env_wanted(self, worker: str | None = None) -> dict[str, dict[str, str | None]]:
        """worker -> {environment: release fingerprint, or None to remove it} (decision 0030)."""
        rows = self._q("SELECT * FROM env_wanted" + (" WHERE worker=?" if worker else ""), (worker,) if worker else ())
        out: dict[str, dict] = {}
        for r in rows:
            out.setdefault(r["worker"], {})[r["name"]] = r["release"]
        return out

    def set_env_wanted(self, worker: str, name: str, release: str | None, by: str | None) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO env_wanted (worker, name, release, by, since) VALUES (?,?,?,?,?)",
                             (worker, name, release, by, now()))

    def forget_env_wanted(self, worker: str, name: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM env_wanted WHERE worker=? AND name=?", (worker, name))

    # ---------------------------------------------------------------- rollouts
    @staticmethod
    def _rollout_row(row) -> dict:
        d = dict(row)
        d["machines"], d["before"] = json.loads(d["machines"]), json.loads(d["before"])
        gate = json.loads(d.pop("gate", None) or "{}")
        d.update({"evals": gate.get("evals") or [], "eval_jobs": gate.get("eval_jobs") or {},
                  "eval_started": gate.get("eval_started"), "drained_before": gate.get("drained_before") or {}})
        return d

    def rollouts(self, name: str | None = None, limit: int = 50) -> list[dict]:
        """Newest first."""
        rows = self._q("SELECT * FROM rollouts" + (" WHERE name=?" if name else "") +
                       " ORDER BY started_at DESC LIMIT ?", ((name, limit) if name else (limit,)))
        return [self._rollout_row(r) for r in rows]

    def save_rollout(self, r: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO rollouts (id, name, release, machines, before, step, state, error, by, "
                "started_at, step_started, updated_at, gate) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], r["name"], r["release"], json.dumps(r["machines"]), json.dumps(r["before"]), r["step"],
                 r["state"], r.get("error"), r.get("by"), r.get("started_at"), r.get("step_started"), now(),
                 json.dumps({k: r.get(k) for k in ("evals", "eval_jobs", "eval_started", "drained_before")})))

    # ---------------------------------------------------------------- syncs
    def syncs(self, limit: int = 50) -> list[dict]:
        """Newest first."""
        rows = self._q("SELECT * FROM syncs ORDER BY started_at DESC LIMIT ?", (limit,))
        return [{**json.loads(r["data"]), "id": r["id"], "state": r["state"], "started_at": r["started_at"],
                 "updated_at": r["updated_at"]} for r in rows]

    def save_sync(self, r: dict) -> None:
        rest = {k: v for k, v in r.items() if k not in ("id", "state", "started_at", "updated_at")}
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO syncs (id, state, started_at, updated_at, data) VALUES (?,?,?,?,?)",
                             (r["id"], r["state"], r.get("started_at"), now(), json.dumps(rest)))

    # ---------------------------------------------------------------- publishes
    @staticmethod
    def _publish_row(row) -> dict:
        d = dict(row)
        d["notes"] = json.loads(d["notes"]) if d.get("notes") else []
        return d

    def insert_publish(self, rec: dict) -> dict:
        rec = dict(rec, notes=json.dumps(rec.get("notes") or []))
        with self._lock:
            self._db.execute(f"INSERT INTO publishes ({','.join(rec)}) VALUES ({','.join('?' * len(rec))})",
                             tuple(rec.values()))
        return self.publish(rec["id"])

    def update_publish(self, pid: str, **fields) -> dict:
        if "notes" in fields:
            fields["notes"] = json.dumps(fields["notes"] or [])
        with self._lock:
            self._db.execute(f"UPDATE publishes SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
                             (*fields.values(), pid))
        return self.publish(pid)

    def publish(self, pid: str) -> dict | None:
        rows = self._q("SELECT * FROM publishes WHERE id=?", (pid,))
        return self._publish_row(rows[0]) if rows else None

    def publishes(self, job_id: str) -> list[dict]:
        """A job's publishes, the latest first."""
        return [self._publish_row(r) for r in
                self._q("SELECT * FROM publishes WHERE job_id=? ORDER BY created_at DESC", (job_id,))]

    def latest_publishes(self, job_ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(job_ids), 500):
            chunk = job_ids[i:i + 500]
            rows = self._q(f"SELECT * FROM publishes WHERE job_id IN ({','.join('?' * len(chunk))}) "
                           f"ORDER BY created_at", tuple(chunk))
            for r in rows:
                out[r["job_id"]] = self._publish_row(r)      # the latest wins
        return out

    def publish_requests(self) -> list[dict]:
        """Done jobs asked at submit to be published that haven't been yet."""
        rows = self._q("SELECT * FROM jobs WHERE status='done' AND json_extract(info, '$.publish_request') IS NOT NULL "
                       "AND NOT EXISTS (SELECT 1 FROM publishes p WHERE p.job_id = jobs.id) ORDER BY finished_at")
        return [self._row(r) for r in rows]

    # ---------------------------------------------------------------- stats
    def run_stats(self) -> dict[tuple[str, str], dict]:
        """Run times per (workflow, GPU type), as manager.eta fits them."""
        return {(r["workflow"], r["gpu"]): dict(r) for r in self._q("SELECT * FROM run_stats")}

    def record_gpu_run(self, workflow: str, gpu: str, size: float, seconds: float) -> None:
        with self._lock:
            rows = self._q("SELECT * FROM run_stats WHERE workflow=? AND gpu=?", (workflow, gpu))
            r = fold(dict(rows[0]) if rows else None, size, seconds)
            self._db.execute(
                "INSERT OR REPLACE INTO run_stats (workflow, gpu, runs, w, wx, wy, wxx, wxy) VALUES (?,?,?,?,?,?,?,?)",
                (workflow, gpu, r["runs"], r["w"], r["wx"], r["wy"], r["wxx"], r["wxy"]))

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
