"""Baseline timings: how long each workflow takes on each GPU, cold and warm, and where the
time goes. Read from the jobs the farm has already run; it submits nothing and changes nothing.

Run it from the repo folder on any machine that reaches the manager:

    python tools/baseline.py                        # the last 14 days, written to docs/baseline/
    python tools/baseline.py --days 3 --workflow wan22_i2i
    python tools/baseline.py --url http://farm-host:8000 --out /tmp/baseline

To get both a cold and a warm figure for a workflow on a machine, run it there twice in a
row (drain the other machines on the dashboard so the jobs land where you want them): the
first run after a different workflow is cold, the second is warm.

For each workflow, GPU and start it gives the median seconds of each step:

    wait      submitted until an agent takes it (the queue)
    prepare   the agent converts and copies inputs
    gpu wait  in ComfyUI's queue behind another job
    run       on the GPU
    collect   outputs converted, checked and moved; the preview made
    total     submitted until done

Jobs from before the farm kept a timeline (job_events) only have the times stored on the
job itself, so for them prepare is counted into wait, and collect into run.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "comfyfarm").is_dir())
STEPS = ("wait", "prepare", "gpu_wait", "run", "collect", "total")


def units(job: dict) -> int:
    """A job's size: the tiles of a texture batch, or the frames it reads (1 for a still)."""
    info = job.get("info") or {}
    return int((info.get("batch") or {}).get("tiles") or info.get("expected_frames") or 1)


def phases(job: dict, events: list[dict] | None) -> dict | None:
    """Seconds per step of a finished job's last run, or None if it can't be told."""
    done = job.get("finished_at")
    if job.get("status") != "done" or not done or not job.get("created_at"):
        return None
    at: dict[str, float] = {}
    for e in events or []:
        if e["event"] == "preparing":            # a new attempt: forget the one before
            at = {"submitted": at.get("submitted", job["created_at"])}
        if e["event"] in ("submitted", "preparing", "dispatched", "running", "postprocessing", "done"):
            at[e["event"]] = e["at"]
    if {"preparing", "dispatched", "running", "done"} <= at.keys():
        post = at.get("postprocessing", at["done"])
        out = {"wait": at["preparing"] - at.get("submitted", job["created_at"]),
               "prepare": at["dispatched"] - at["preparing"], "gpu_wait": at["running"] - at["dispatched"],
               "run": post - at["running"], "collect": at["done"] - post, "total": at["done"] - job["created_at"]}
    elif job.get("dispatched_at") and job.get("started_at"):
        out = {"wait": job["dispatched_at"] - job["created_at"], "prepare": None,
               "gpu_wait": job["started_at"] - job["dispatched_at"], "run": done - job["started_at"],
               "collect": None, "total": done - job["created_at"]}
    else:
        return None
    return {k: (None if v is None else round(max(v, 0.0), 2)) for k, v in out.items()}


def mark_cold(jobs: list[dict]) -> None:
    """Set job["cold"]: the job before it on the same machine ran another workflow, or there
    was none. The same rule the manager places jobs by. Give it every job, not a filtered list."""
    last: dict[str, str] = {}
    for j in sorted((j for j in jobs if j.get("worker") and j.get("started_at")), key=lambda j: j["started_at"]):
        j["cold"] = last.get(j["worker"]) != j["workflow"]
        last[j["worker"]] = j["workflow"]


def summarise(rows: list[dict]) -> list[dict]:
    """One line per workflow, GPU and start, from rows of {workflow, gpu, cold, units, steps}."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["workflow"], r["gpu"], bool(r["cold"])), []).append(r)
    out = []
    for (wf, gpu, cold), rs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], not kv[0][2])):
        line = {"workflow": wf, "gpu": gpu, "start": "cold" if cold else "warm", "runs": len(rs),
                "size": statistics.median(r["units"] for r in rs)}
        for step in STEPS:
            vals = [r["steps"][step] for r in rs if r["steps"].get(step) is not None]
            line[step] = round(statistics.median(vals), 1) if vals else None
        totals = [r["steps"]["total"] for r in rs]
        line["total_min"], line["total_max"] = round(min(totals), 1), round(max(totals), 1)
        out.append(line)
    return out


def markdown(lines: list[dict], meta: dict) -> str:
    def cell(v):
        return "" if v is None else (f"{v:g}" if isinstance(v, (int, float)) else str(v))
    head = ["Workflow", "GPU", "Start", "Runs", "Size", "Wait", "Prepare", "GPU wait", "Run", "Collect", "Total",
            "Total, min to max"]
    md = [f"# Baseline timings, {meta['date']}", "",
          f"Median seconds per step, from {meta['jobs']} finished jobs of the last {meta['days']:g} days on "
          f"{meta['manager']} (farm code {meta['version']}). Written by `tools/baseline.py`.", "",
          "Size is the median number of frames or tiles a job read. A start is cold when the job before it on",
          "that machine ran another workflow. " + (f"{meta['coarse']} job(s) from before the farm kept a timeline "
                                                   "count prepare into wait and collect into run." if meta["coarse"]
                                                   else "Every job had a timeline."), "",
          "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for ln in lines:
        spread = f"{ln['total_min']:g} to {ln['total_max']:g}"
        md.append("| " + " | ".join([ln["workflow"], ln["gpu"], ln["start"], cell(ln["runs"]), cell(ln["size"]),
                                     *(cell(ln[s]) for s in STEPS), spread]) + " |")
    if not lines:
        md.append("| no finished jobs in this period | | | | | | | | | | | |")
    return "\n".join(md) + "\n"


def collect(farm, days: float, limit: int, workflow: str | None = None) -> tuple[list[dict], dict]:
    """Rows for summarise(), read from the manager through the client module `farm`."""
    since = time.time() - days * 86400
    gpus = {w["name"]: ((w.get("gpu") or {}).get("name") or w["name"]) for w in farm.workers()}
    listed = farm._request("GET", f"/api/v1/jobs?status=done&limit={limit}")["jobs"]
    jobs = []
    for j in listed:                             # a texture set ran as batches: measure those
        jobs += farm.batches(j["id"]) if j.get("kind") == "set" else [j]
    mark_cold(jobs)
    rows, coarse = [], 0
    for j in jobs:
        if (j.get("finished_at") or 0) < since or (workflow and j["workflow"] != workflow):
            continue
        events = farm.events(j["id"])
        steps = phases(j, events)
        if not steps or "cold" not in j:
            continue
        coarse += steps["prepare"] is None
        rows.append({"job": j["id"][:8], "workflow": j["workflow"], "worker": j["worker"],
                     "gpu": gpus.get(j["worker"], j["worker"]), "cold": j["cold"], "units": units(j), "steps": steps})
    return rows, {"coarse": coarse, "jobs": len(rows)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", help="the manager's address (default: as the client finds it, from manager.json)")
    ap.add_argument("--days", type=float, default=14, help="jobs finished in the last N days (default 14)")
    ap.add_argument("--limit", type=int, default=2000, help="look at up to N finished jobs (default 2000)")
    ap.add_argument("--workflow", help="only this workflow")
    ap.add_argument("--out", default=str(ROOT / "docs" / "baseline"), help="folder to write to (default docs/baseline)")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(ROOT / "client"))
    import comfyfarm_client as farm
    if args.url:
        farm.set_url(args.url)
    try:
        health = farm.health()
        rows, counts = collect(farm, args.days, args.limit, args.workflow)
    except farm.FarmError as e:
        print(f"Can't read the farm: {e}", file=sys.stderr)
        return 1
    meta = {"date": time.strftime("%Y-%m-%d"), "days": args.days, "manager": farm.base_url() or "?",
            "version": health.get("version") or "?", **counts}
    lines = summarise(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    text = markdown(lines, meta)
    (out / f"baseline-{meta['date']}.md").write_text(text, encoding="utf-8")
    (out / f"baseline-{meta['date']}.json").write_text(
        json.dumps({"meta": meta, "summary": lines, "jobs": rows}, indent=1), encoding="utf-8")
    print(text)
    print(f"Written to {out / ('baseline-' + meta['date'] + '.md')} and .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
