"""The retention sweeper (decisions 0006, 0041): once a day, the finished jobs whose outputs
have expired, and for each the exact files it would delete: only those its provenance file
lists (decision 0038), and only when that file is the job's own and sits in the job's
output folder. For now it is a dry run: it writes what it would do, and deletes nothing
(plan, phase 6, step 4).

A job expires `retention.keep_days` after it finished, or after it was last sent to Tractor
to be published (dispatching a publish restarts the clock). A job with a publish on its way
is kept. A texture set expires as a whole, with all its batches' files."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from ..fileio import paths as P
from ..protocol import FINISHED, now

log = logging.getLogger("comfyfarm")

__all__ = ["Sweeper", "sweep", "text", "PROVENANCE_VERSION"]

PROVENANCE_VERSION = 1                      # the provenance files the sweeper reads (agent/provenance.py)
ON_ITS_WAY = ("queued", "dispatching")      # a publish not sent yet: its files are needed
RESTARTS = ("dispatched",)                  # a publish sent to Tractor restarts the clock
DAY = 86400.0
REPORT_NAME = re.compile(r"^\d{4}-\d\d-\d\d(?:T\d\d\d\d\d\d)?\.json$")


def _when(t: float | None) -> str | None:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None


def expiry(job: dict, publishes: list[dict], keep_days: float) -> tuple[float | None, float | None]:
    """When the job's outputs expire, and when it was last sent to be published. None for
    the expiry: kept, a publish is on its way (or was asked for at submit, not made yet)."""
    if any(p.get("state") in ON_ITS_WAY for p in publishes):
        return None, None
    if (job.get("info") or {}).get("publish_request") and not publishes and job.get("status") == "done":
        return None, None
    sent = max((p.get("finished_at") or p.get("created_at") or 0 for p in publishes if p.get("state") in RESTARTS),
               default=None)
    start = max(job.get("finished_at") or 0, sent or 0)
    return start + keep_days * DAY, sent


def _safe_name(name: str) -> bool:
    """A name in the folder, never a way out of it: relative, no '..', no drive."""
    if not isinstance(name, str) or not name.strip():
        return False
    n = name.replace("\\", "/")
    return not (n.startswith("/") or re.match(r"^[A-Za-z]:", n) or ".." in n.split("/"))


def _same_folder(a: str, b: str) -> bool:
    a, b = P.normalize(a).rstrip("/"), P.normalize(b).rstrip("/")
    return a == b or (os.name == "nt" and a.lower() == b.lower())


def _provenance(run: dict, folder: str) -> tuple[dict | None, str | None]:
    """A run's provenance file and its name, or why it can't be used."""
    path = (run.get("outputs") or {}).get("provenance")
    if not path:
        return None, None
    name = os.path.basename(path)
    if not _same_folder(os.path.dirname(path), folder):
        return None, f"{name} isn't in the job's output folder, so nothing is deleted."
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, f"{name} is gone, so nothing it listed is deleted."
    except (OSError, ValueError) as e:
        return None, f"{name} can't be read ({getattr(e, 'strerror', None) or e}), so nothing is deleted."
    if not isinstance(doc, dict) or doc.get("comfyfarm_provenance") != PROVENANCE_VERSION:
        return None, f"{name} isn't a provenance file this farm writes, so nothing is deleted."
    if doc.get("job") != run["id"]:
        return None, f"{name} belongs to job {str(doc.get('job'))[:8]}, not this one, so nothing is deleted."
    return doc, None


def _entry(job: dict, runs: list[dict], expires: float, sent: float | None, mappings) -> dict:
    """What the sweeper would do for one expired job (and its batches, for a set)."""
    folder = job.get("output_dir") or ""
    e = {"job": job["id"], "kind": job.get("kind") or "job", "workflow": job["workflow"], "user": job["user"],
         "show": (job.get("info") or {}).get("show"), "status": job["status"],
         "finished": _when(job.get("finished_at")), "published": _when(sent), "expired": _when(expires),
         "folder": P.to_posix(folder, mappings) or folder, "records": len(runs) + (job.get("kind") == "set"),
         "provenance": [], "delete": [], "bytes": 0, "missing": [], "problems": [], "left": None,
         "empties_folder": False}
    if not folder:
        e["problems"].append("No output folder: only its record goes.")
        return e
    listed = [r for r in runs if (r.get("outputs") or {}).get("provenance")]
    if not listed:
        e["problems"].append("No provenance file (provenance was off when it was submitted, or it didn't finish), "
                             "so its files stay: only its record goes.")
        return e
    delete: dict[str, int] = {}
    for run in listed:
        doc, problem = _provenance(run, folder)
        if problem:
            e["problems"].append(problem)
            continue
        e["provenance"].append(os.path.basename(run["outputs"]["provenance"]))
        for name in doc.get("files") or []:
            if not _safe_name(name):
                e["problems"].append(f"{name}: not in the job's folder, left alone.")
                continue
            path = os.path.join(folder, name)
            if os.path.islink(path) or (os.path.exists(path) and not os.path.isfile(path)):
                e["problems"].append(f"{name}: not a plain file, left alone.")
            elif os.path.isfile(path):
                try:
                    delete[name] = os.path.getsize(path)
                except OSError:
                    delete[name] = 0
            else:
                e["missing"].append(name)
    e["delete"] = sorted(delete)
    e["bytes"] = sum(delete.values())
    try:
        here = set(os.listdir(folder))
    except OSError as ex:
        e["problems"].append(f"The output folder can't be listed ({ex.strerror or ex}).")
        return e
    gone = {n.replace("\\", "/").split("/")[0] for n in e["delete"]} | set(e["provenance"])
    e["left"] = len(here - gone)
    e["empties_folder"] = e["left"] == 0 and not e["problems"]
    return e


def sweep(store, cfg, t: float | None = None) -> dict:
    """The dry run: every finished job (and texture set) whose outputs have expired, with
    the files it would delete; nothing is deleted. `cfg` is the manager's Config."""
    t = now() if t is None else t
    rc, mappings = cfg.retention, cfg.paths.mappings
    jobs, kept, publishing = [], 0, 0
    for job in store.list(statuses=FINISHED, limit=10_000_000):
        expires, sent = expiry(job, store.publishes(job["id"]), rc.keep_days)
        if expires is None:
            publishing += 1
            continue
        if expires > t:
            kept += 1
            continue
        runs = store.list(parent=job["id"], limit=10_000_000) if job.get("kind") == "set" else [job]
        jobs.append(_entry(job, runs, expires, sent, mappings))
    jobs.sort(key=lambda e: e["finished"] or "")
    return {"comfyfarm_sweep": 1, "at": _when(t), "dry_run": True, "keep_days": rc.keep_days,
            "expired": len(jobs), "files": sum(len(e["delete"]) + len(e["provenance"]) for e in jobs),
            "bytes": sum(e["bytes"] for e in jobs), "records": sum(e["records"] for e in jobs),
            "problems": sum(len(e["problems"]) for e in jobs), "kept": kept, "publishing": publishing,
            "jobs": jobs}


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def text(report: dict) -> str:
    """The report as people read it: what it would delete, job by job."""
    lines = [f"Retention sweep at {report['at']}: a dry run, nothing was deleted.",
             f"Outputs are kept {report['keep_days']:g} days after a job finished or was last sent to be published.",
             f"{report['expired']} expired job(s): {report['files']} file(s), {_size(report['bytes'])}, and "
             f"{report['records']} record(s) would go. {report['kept']} not expired yet; {report['publishing']} kept "
             f"while a publish is on its way. {report['problems']} problem(s).", ""]
    for e in report["jobs"]:
        lines.append(f"{e['job']}  {e['kind']}  {e['workflow']}  {e['user']}  {e['status']}  "
                     f"finished {e['finished']}" + (f"  published {e['published']}" if e["published"] else ""))
        lines.append(f"  folder {e['folder']}")
        if e["delete"] or e["provenance"]:
            lines.append(f"  would delete {len(e['delete'])} file(s), {_size(e['bytes'])}, and "
                         f"{', '.join(e['provenance'])}" + ("; the folder would then be empty" if e["empties_folder"]
                                                            else f"; {e['left']} other file(s) stay"
                                                            if e["left"] is not None else ""))
            for name in e["delete"]:
                lines.append(f"    {name}")
        for name in e["missing"]:
            lines.append(f"  already gone: {name}")
        for p in e["problems"]:
            lines.append(f"  ! {p}")
        lines.append("")
    return "\n".join(lines)


class Sweeper:
    """Runs the sweep once a day at retention.at (the manager's local time), when
    retention.enabled, and keeps its reports in data/sweeps/ (the last retention.reports)."""

    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.dir = Path(cfg.resolve(cfg.database)).parent / "sweeps"
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.cfg.retention.enabled:
            self._task = asyncio.create_task(self._run(), name="sweeper")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def next_run(self, t: float) -> float:
        """The next retention.at after t; today's if a run was missed (the manager was down)."""
        hh, mm = (int(x) for x in self.cfg.retention.at.split(":"))
        today = datetime.fromtimestamp(t).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if today.timestamp() <= t:
            last = self.reports()[:1]
            if not last or last[0]["date"] < today.date().isoformat():
                return t                        # today's hasn't run
            today += timedelta(days=1)
        return today.timestamp()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(max(0.0, self.next_run(now()) - now()))
            try:
                report = await asyncio.to_thread(self.run)
                log.info("retention sweep (dry run): %d expired job(s), %d file(s), %s would go; %d problem(s)",
                         report["expired"], report["files"], _size(report["bytes"]), report["problems"])
            except Exception:  # noqa: BLE001
                log.exception("retention sweep failed")
            await asyncio.sleep(60)             # past this minute, so it runs once

    def run(self, t: float | None = None) -> dict:
        """One sweep (a dry run), its report saved: data/sweeps/<date>.json."""
        report = sweep(self.store, self.cfg, t)
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(now() if t is None else t)
        path = self.dir / f"{stamp.date().isoformat()}.json"
        if path.exists():                       # a second run that day (asked for): its own file
            path = self.dir / f"{stamp.strftime('%Y-%m-%dT%H%M%S')}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        for old in self._files()[self.cfg.retention.reports:]:
            try:
                old.unlink()
            except OSError:
                pass
        return report

    def _files(self) -> list[Path]:
        try:
            return sorted((p for p in self.dir.iterdir() if REPORT_NAME.match(p.name)), reverse=True)
        except OSError:
            return []

    def reports(self) -> list[dict]:
        """The saved reports, newest first: their name, date and totals."""
        out = []
        for p in self._files():
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"name": p.stem, "date": p.stem[:10], "at": r.get("at"), "dry_run": r.get("dry_run"),
                        **{k: r.get(k) for k in ("expired", "files", "bytes", "records", "problems", "kept",
                                                 "publishing")}})
        return out

    def report(self, name: str) -> dict | None:
        """One saved report by its name (2026-11-23), or the latest."""
        files = self._files()
        if name != "latest":
            files = [p for p in files if p.stem == name]
        if not files:
            return None
        return json.loads(files[0].read_text(encoding="utf-8"))
