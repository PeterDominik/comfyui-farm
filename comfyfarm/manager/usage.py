"""The dashboard's Usage tab (decision 0050): jobs, GPU hours and failures over the last
days, per day, show, definition or machine, from job_metrics (decision 0040), so they
outlive the jobs.

Runs are counted: a job, or one batch of a texture set (each runs on its own), not the set.
A run's GPU seconds are from when it started on the GPU until it finished, outputs
collected, whatever its outcome: a failed run held the GPU too. job_metrics keeps a job's
last attempt only, so the earlier attempts of a retried job aren't counted. Days are the
manager's local days."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..protocol import now

__all__ = ["BY", "report", "since"]

BY = ("day", "show", "workflow", "machine")


def _label(registry, workflow: str) -> str:
    """A definition's label, or its name once it is no longer in the repo."""
    wf = registry.workflows.get(workflow)
    return wf.label if wf else workflow


def since(days: int, t: float | None = None) -> date:
    """The first of the last `days` days, today included: its midnight starts the span."""
    today = datetime.fromtimestamp(now() if t is None else t).date()
    return today - timedelta(days=days - 1)


def _numbers(r: dict | None) -> dict:
    r = r or {}
    out = {k: int(r.get(k) or 0) for k in ("jobs", "done", "failed", "cancelled", "frames")}
    out["gpu_seconds"] = round(float(r.get("gpu_seconds") or 0.0), 1)
    out["lost_seconds"] = round(float(r.get("lost_seconds") or 0.0), 1)
    out["wait_seconds"] = round(float(r["wait_seconds"]), 1) if r.get("wait_seconds") is not None else None
    return out


def report(store, registry, days: int = 30, by: str = "day", show: str | None = None, workflow: str | None = None,
           t: float | None = None) -> dict:
    """GET /api/v1/usage. `show` and `workflow` narrow it to one show ("" for jobs with none)
    or one definition. Per day: every day of the span, newest first, those without runs at
    zero. Otherwise the most GPU hours first."""
    if by not in BY:
        raise ValueError(f"by is one of {', '.join(BY)}, not {by!r}.")
    first = since(days, t)
    start = datetime.combine(first, datetime.min.time()).timestamp()
    rows = {r["key"]: r for r in store.usage(start, by, show, workflow)}

    def label(key: str) -> str:
        if by == "workflow":
            return _label(registry, key)
        if by == "show":
            return key or "no show"
        if by == "machine":
            return key or "not given to a machine"
        return key

    if by == "day":
        keys = [(first + timedelta(days=i)).isoformat() for i in range(days)][::-1]
        table = [{"key": k, "label": k, **_numbers(rows.get(k))} for k in keys]
    else:
        table = [{"key": k, "label": label(k), **_numbers(r)} for k, r in rows.items()]
        table.sort(key=lambda r: (-r["gpu_seconds"], -r["jobs"], r["label"].lower()))
    total = store.usage(start, "all", show, workflow)
    values = store.usage_values(start)
    return {"by": by, "days": days, "since": first.isoformat(), "show": show, "workflow": workflow,
            "rows": table, "total": _numbers(total[0] if total else None),
            "shows": values["shows"],
            "workflows": [{"name": w, "label": _label(registry, w)} for w in values["workflows"]]}
