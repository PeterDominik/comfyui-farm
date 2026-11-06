"""The manager's side of publishing: what a workflow can be published with, checking a
request, and running it (staging the template, dispatching it) off the event loop.

A publish is asked for at submit ("publish when done": kept in the job's info as
publish_request, run when the job is done) or on a finished job (POST
/api/v1/jobs/{id}/publish). Each one is a row in the publishes table: queued, then
dispatched (the Tractor job id and title from dispatchGraph's output), dry_run
(dispatchGraph --dry-run: checked by Daiquiri, nothing sent) or failed (why, and the
output)."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..protocol import now
from ..workflows import ParamError
from . import dispatch as D
from . import nuke as N

log = logging.getLogger("comfyfarm")

__all__ = ["Publisher", "PublishError"]

# Show and shot codes as Daiquiri takes them.
_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class PublishError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def summary(rec: dict | None) -> dict | None:
    """A publish as job lists carry it."""
    if not rec:
        return None
    keys = ("id", "state", "show", "shot", "step", "template", "description", "frames", "tractor_id", "title",
            "error", "created_at", "finished_at", "workfile", "user", "notes")
    return {k: rec.get(k) for k in keys}


class Publisher:
    def __init__(self, farm):
        self.farm = farm
        self.cfg = farm.cfg.publish
        self.store = farm.store
        self._pool = ThreadPoolExecutor(max_workers=max(1, self.cfg.workers), thread_name_prefix="publish")
        self._wake: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._cleaned = 0.0
        self._lock = threading.Lock()

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="publisher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._pool.shutdown(wait=False, cancel_futures=True)

    def wake(self) -> None:
        """A job was done: publish it if that was asked for. Safe from any thread."""
        if self._loop and self._wake:
            self._loop.call_soon_threadsafe(self._wake.set)

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), 60)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await asyncio.to_thread(self.publish_requested)
                if time.time() - self._cleaned > 3600:
                    self._cleaned = time.time()
                    await asyncio.to_thread(self.clean_staging)
            except Exception:  # noqa: BLE001
                log.exception("publishing finished jobs failed")

    # ================================================================ what's possible
    def templates_dir(self, workflow: str) -> Path:
        return Path(self.cfg.templates) / workflow

    def options(self, workflow: str) -> dict:
        """What the forms offer for a workflow: whether it publishes, its templates (and
        why any can't be used), the steps and the defaults."""
        wf = self.farm.registry.get(workflow)
        folder = self.templates_dir(wf.name)
        out = {"workflow": wf.name, "enabled": self.cfg.enabled, "dry_run": self.cfg.dry_run, "steps": self.cfg.steps,
               "step": (wf.publish or {}).get("step"), "description": self.cfg.description, "folder": str(folder),
               "templates": [], "unusable": {}, "why": None}
        if not wf.publish or "bad" in wf.publish:
            out["why"] = f"{wf.label} isn't published (its manifest has no publish section)."
            return out
        if not self.cfg.enabled:
            out["why"] = "Publishing isn't switched on (publish.enabled in the manager's config.yaml)."
            return out
        inputs = list(wf.ports)
        for name in N.templates(folder):
            try:
                probs = N.Template.read(folder / f"{name}.nk").problems(inputs)
            except OSError as e:
                probs = [f"can't be read ({e.strerror or e})"]
            if probs:
                out["unusable"][name] = "; ".join(probs)
            else:
                out["templates"].append(name)
        if not out["templates"]:
            out["why"] = (f"No usable template in {folder}" +
                          (f" ({'; '.join(f'{n}: {w}' for n, w in out['unusable'].items())})" if out["unusable"] else "")
                          + ": save one there (README, Publishing).")
        return out

    def check(self, workflow: str, req: dict, show: str | None = None, shot: str | None = None) -> dict:
        """A publish request, completed with the defaults and checked: {show, shot, step,
        template, description}. ParamError says what's wrong."""
        opts = self.options(workflow)
        if opts["why"]:
            raise ParamError(opts["why"])
        step = str(req.get("step") or opts["step"] or "").strip()
        if not step:
            raise ParamError("Pick the step to publish to.")
        if step not in self.cfg.steps:
            raise ParamError(f"Step {step!r} isn't one of the studio's ({', '.join(self.cfg.steps)}).")
        template = str(req.get("template") or "").strip() or opts["templates"][0]
        if template not in opts["templates"]:
            why = opts["unusable"].get(template)
            raise ParamError(f"Template {template} can't be used: {why}." if why else
                             f"There's no template {template} in {opts['folder']} ({', '.join(opts['templates'])}).")
        show_ = str(req.get("show") or show or "").strip()
        shot_ = str(req.get("shot") or shot or "").strip()
        if not show_ or not _CODE.fullmatch(show_):
            raise ParamError("Publishing needs the show: the input path doesn't name one, so enter it." if not show_
                             else f"Show {show_!r} isn't a show code.")
        if not shot_ or not _CODE.fullmatch(shot_):
            raise ParamError("Publishing needs the shot: the input path doesn't name one, so enter it." if not shot_
                             else f"Shot {shot_!r} isn't a shot code.")
        desc = str(req.get("description") or "").strip() or self.cfg.description
        if len(desc) > 500:
            raise ParamError("The description is too long (500 characters at most).")
        return {"show": show_, "shot": shot_, "step": step, "template": template, "description": desc}

    # ================================================================ publishing
    def start_publish(self, job_id: str, req: dict, user: str, dry_run: bool = False, wait: bool = False) -> dict:
        """Check a publish of a finished job and start it: the record, queued (done when
        `wait`). Raises PublishError / ParamError when it can't be done."""
        job = self.store.get(job_id)
        if not job:
            raise PublishError("Job not found.", 404)
        if job.get("kind") == "set":
            raise PublishError("Texture sets aren't published from the farm.")
        if job["status"] != "done":
            raise PublishError(f"Only a finished job can be published (this one is {job['status']}).", 409)
        outputs = job.get("outputs") or {}
        if not (outputs.get("frame_pattern_posix") or outputs.get("frame_pattern")):
            raise PublishError("This job has no output frames to publish.")
        info = job.get("info") or {}
        r = self.check(job["workflow"], req, show=info.get("show"), shot=info.get("shot"))
        dry = bool(dry_run) or self.cfg.dry_run
        rec = self.store.insert_publish({
            "id": str(uuid.uuid4()), "job_id": job_id, "created_at": now(), "user": user, "state": "queued",
            **r, "frames": N.frames_text(list(range(outputs.get("first") or 0, (outputs.get("last") or 0) + 1)))
            if outputs.get("first") is not None else None, "notes": ["dry run: dispatchGraph --dry-run"] if dry else []})
        self._changed(job_id)
        if wait:
            return self._publish(rec["id"], dry)
        self._pool.submit(self._publish, rec["id"], dry)
        return rec

    def publish_requested(self) -> None:
        """Publish the finished jobs that asked for it at submit."""
        for job in self.store.publish_requests():
            req = dict((job.get("info") or {}).get("publish_request") or {})
            user = req.pop("user", None) or job["user"]
            dry = bool(req.pop("dry_run", False))
            try:
                self.start_publish(job["id"], req, user, dry_run=dry, wait=True)
            except (PublishError, ParamError) as e:
                # Recorded, so it isn't tried again and the artist sees why.
                self.store.insert_publish({"id": str(uuid.uuid4()), "job_id": job["id"], "created_at": now(),
                                           "user": user, "state": "failed", "error": str(e), "finished_at": now(),
                                           **{k: req.get(k) for k in ("show", "shot", "step", "template",
                                                                     "description")}})
                self._changed(job["id"])
                log.warning("job %s: publishing it as asked at submit failed: %s", job["id"][:8], e)

    def _publish(self, pid: str, dry: bool) -> dict:
        """Stage the template and dispatch it (in a worker thread)."""
        rec = self.store.publish(pid)
        job = self.store.get(rec["job_id"])
        wf = self.farm.registry.get(job["workflow"])
        outputs = job.get("outputs") or {}
        dest = Path(self.cfg.staging) / job["id"] / f"{rec['template']}.nk"
        try:
            staged = N.stage(self.templates_dir(wf.name) / f"{rec['template']}.nk", dest, job=job["id"],
                             workflow=wf.name,
                             output={"path": outputs.get("frame_pattern_posix") or outputs.get("frame_pattern"),
                                     "first": outputs.get("first"), "last": outputs.get("last")},
                             inputs=self._inputs(job, wf), colorspaces=self.cfg.read_colorspace)
        except (OSError, ValueError) as e:
            return self._finish(rec, state="failed", error=f"Couldn't stage the template: {e}")
        desc = rec["description"] + self.cfg.description_suffix.format(job=job["id"][:8], user=rec["user"] or job["user"],
                                                                      workflow=wf.name)
        values = {"graph": (wf.publish or {}).get("graph") or "update_and_render_nuke",
                  "show": rec["show"], "shot": rec["shot"], "step": rec["step"], "description": desc,
                  "workfile": str(dest), "frames": rec["frames"] or "forceInput", "first": outputs.get("first") or "",
                  "last": outputs.get("last") or "", "job": job["id"], "user": rec["user"] or job["user"]}
        try:
            argv = D.command(self.cfg.shell, self.cfg.command + (" --dry-run" if dry else ""), values)
        except ValueError as e:
            return self._finish(rec, state="failed", error=str(e), workfile=str(dest))
        self.store.update_publish(pid, state="dispatching", workfile=str(dest), command=argv[-1],
                                  notes=list(rec.get("notes") or []) + staged["notes"])
        self._changed(job["id"])
        log.info("job %s: publishing to %s %s %s with %s%s", job["id"][:8], rec["show"], rec["shot"], rec["step"],
                 rec["template"], " (dry run)" if dry else "")
        code, out = D.run(argv, self.cfg.timeout)
        found = D.parse(out, self.cfg.job_id_pattern)
        if code != 0:
            last = next((line for line in reversed(out.strip().splitlines()) if line.strip()), "")
            return self._finish(rec, state="failed", output=out[-20000:],
                                error=f"dispatchGraph failed (exit {code}): {last[:300]}")
        notes = list(self.store.publish(pid).get("notes") or [])
        if not dry and not found.get("tractor_id"):
            notes.append("dispatchGraph ran, but its output names no Tractor job: check it below")
        return self._finish(rec, state="dry_run" if dry else "dispatched", output=out[-20000:], notes=notes,
                            tractor_id=found.get("tractor_id"), title=found.get("title"))

    def _finish(self, rec: dict, **fields) -> dict:
        done = self.store.update_publish(rec["id"], finished_at=now(), **fields)
        self._changed(rec["job_id"])
        if fields.get("state") == "failed":
            log.warning("job %s: publish failed: %s", rec["job_id"][:8], fields.get("error"))
        elif fields.get("tractor_id"):
            log.info("job %s: published, Tractor job %s", rec["job_id"][:8], fields["tractor_id"])
        return done

    def _inputs(self, job: dict, wf) -> dict:
        """The job's inputs for Placeholder_Input_<param> Reads: their studio paths and frames."""
        info = job.get("info") or {}
        out: dict = {}

        def one(x: dict | None, posix: str | None) -> dict | None:
            if not x:
                return None
            return {"path": posix or x.get("farm_path"), "first": x.get("first"), "last": x.get("last"),
                    "kind": x.get("kind")}
        if wf.port is not None and info.get("input"):
            out[wf.port.name] = one(info["input"], info.get("input_posix"))
        for name, x in (info.get("inputs") or {}).items():
            out[name] = one(x, (info.get("inputs_posix") or {}).get(name))
        return out

    def _changed(self, job_id: str) -> None:
        """Tell the dashboards (the job, with its latest publish)."""
        job = self.store.get(job_id)
        if job:
            job["publish"] = summary(self.store.latest_publishes([job_id]).get(job_id))
            self.farm._emit(job)

    def clean_staging(self) -> int:
        """Remove staged scripts older than publish.keep_days (one folder per job)."""
        root = Path(self.cfg.staging)
        cutoff = time.time() - self.cfg.keep_days * 86400
        removed = 0
        try:
            entries = list(root.iterdir())
        except OSError:
            return 0
        for d in entries:
            try:
                uuid.UUID(d.name)
            except ValueError:
                continue                    # not ours
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d)
                    removed += 1
            except OSError:
                continue
        return removed
