"""Job metrics as people read them (decision 0040): the store's rows (db.job_metrics) with
local times and how long each part took, as JSON or as CSV for a spreadsheet.

One row per finished job: who ran what, for which show, on which machine and GPU, how it
ended and how long each part took. Never its params, prompts, paths or errors, so the rows
can be kept after the job and shared beyond the farm's admins."""

from __future__ import annotations

import csv
import io
from datetime import datetime

__all__ = ["COLUMNS", "parse_time", "rows", "to_csv"]

# What the endpoint gives, in order: the CSV's header.
COLUMNS = ("job", "kind", "set", "workflow", "fingerprint", "user", "show", "worker", "gpu", "outcome", "attempts",
           "units", "submitted", "assigned", "dispatched", "started", "finished",
           "wait_seconds", "prepare_seconds", "gpu_wait_seconds", "run_seconds", "total_seconds")


def parse_time(text: str | None) -> float | None:
    """A time as given in a request: seconds since 1970, or an ISO date or date and time in
    the manager's local time (2026-11-01, 2026-11-01T09:00). ValueError for anything else."""
    if text is None or not str(text).strip():
        return None
    text = str(text).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        raise ValueError(f"{text!r} isn't a time: give a date (2026-11-01), a date and time "
                         f"(2026-11-01T09:00) or seconds since 1970.") from None


def _when(t: float | None) -> str | None:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None


def _span(a: float | None, b: float | None) -> float | None:
    return round(float(b - a), 1) if a is not None and b is not None and b >= a else None


def rows(stored: list[dict]) -> list[dict]:
    """Each stored row with local times and how long each part took:
    wait (submitted -> given to a machine), prepare (-> handed to the runner, inputs ready),
    gpu_wait (-> running on the GPU), run (-> finished, outputs collected) and total."""
    out = []
    for r in stored:
        out.append({
            "job": r["job_id"], "kind": r["kind"], "set": r["parent_id"], "workflow": r["workflow"],
            "fingerprint": r["fingerprint"], "user": r["user"], "show": r["show"], "worker": r["worker"],
            "gpu": r["gpu"], "outcome": r["outcome"], "attempts": r["attempts"], "units": r["units"],
            "submitted": _when(r["created_at"]), "assigned": _when(r["assigned_at"]),
            "dispatched": _when(r["dispatched_at"]), "started": _when(r["started_at"]),
            "finished": _when(r["finished_at"]),
            "wait_seconds": _span(r["created_at"], r["assigned_at"]),
            "prepare_seconds": _span(r["assigned_at"], r["dispatched_at"]),
            "gpu_wait_seconds": _span(r["dispatched_at"], r["started_at"]),
            "run_seconds": _span(r["started_at"], r["finished_at"]),
            "total_seconds": _span(r["created_at"], r["finished_at"]),
        })
    return out


def to_csv(table: list[dict]) -> str:
    """The rows as CSV, with a header: what a spreadsheet opens."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in table:
        w.writerow({k: "" if r.get(k) is None else r[k] for k in COLUMNS})
    return buf.getvalue()
