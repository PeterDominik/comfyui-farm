"""The farm: job submission, dispatch to ComfyUI workers, live monitoring over each
worker's websocket, reconciliation after restarts, and output collection."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

from . import convert as C
from . import paths as P
from . import textures as T
from .comfy import ComfyClient, ComfyRejected, describe_rejection
from .config import Config, WorkerConfig
from .db import ACTIVE, ON_WORKER, JobStore, now
from .events import EventBus
from .workflows import ParamError, WorkflowRegistry

log = logging.getLogger("comfyfarm")


class FarmError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Runtime:
    """Live progress of one job on a worker (not persisted)."""
    job_id: str
    expected: int
    titles: dict
    done: set = field(default_factory=set)
    current: str | None = None
    frac: float = 0.0
    last_push: float = 0.0

    def progress(self) -> float:
        return min((len(self.done) + self.frac) / max(self.expected, 1), 0.99)


class Worker:
    def __init__(self, cfg: WorkerConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.client = ComfyClient(cfg.url)
        self.client_id = "comfyfarm-" + uuid.uuid5(uuid.NAMESPACE_URL, cfg.url + cfg.name).hex[:12]
        self.online = False
        self.ws_connected = False
        self.checked = False
        self.offline_since = now()
        self.last_seen: float | None = None
        self.stats: dict | None = None
        self.output_classes: set[str] | None = None
        self.problems: dict[str, list[str]] = {}
        self.queue_remaining: int | None = None
        self.error: str | None = None
        self.abandoned: set[str] = set()   # prompt ids we gave up on; cancel if seen again

    def summary(self, store: JobStore) -> dict:
        gpu = None
        for dev in (self.stats or {}).get("devices", []) or []:
            gpu = {"name": dev.get("name"), "vram_total": dev.get("vram_total"),
                   "vram_free": dev.get("vram_free")}
            break
        current = store.on_worker(self.name, ("running",))
        return {
            "name": self.name, "url": self.cfg.url, "online": self.online,
            "connected": self.ws_connected, "enabled": self.cfg.enabled,
            "max_inflight": self.cfg.max_inflight, "inflight": store.count_on_worker(self.name),
            "running_job": current[0]["id"] if current else None,
            "queue_remaining": self.queue_remaining, "last_seen": self.last_seen,
            "error": self.error, "gpu": gpu,
            "comfyui_version": ((self.stats or {}).get("system") or {}).get("comfyui_version"),
            "problems": self.problems,
        }


def _history_failure(hist: dict) -> tuple[str, bool, str | None]:
    """(message, interrupted, traceback) from a failed ComfyUI history entry."""
    for name, data in (hist.get("status") or {}).get("messages") or []:
        if name == "execution_interrupted":
            return "Interrupted on the farm machine.", True, None
        if name == "execution_error":
            tb = "".join(data.get("traceback") or []) or None
            return (f"{data.get('node_type')} (node {data.get('node_id')}): "
                    f"{data.get('exception_message', '').strip()}"), False, tb
    return "ComfyUI reported an error.", False, None


class Farm:
    def __init__(self, cfg: Config, store: JobStore, registry: WorkflowRegistry, events: EventBus):
        self.cfg = cfg
        self.store = store
        self.registry = registry
        self.events = events
        self.workers = {w.name: Worker(w) for w in cfg.workers if w.enabled}
        self.runtime: dict[str, Runtime] = {}      # prompt_id -> Runtime
        self.loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._submit_lock = threading.Lock()
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()
        self._converting: dict[str, asyncio.Task] = {}   # job id -> conversion task
        self._convert_wake: asyncio.Event | None = None
        self.scratch_root = P.normalize(str(cfg.resolve(cfg.convert.scratch_dir)))

    # ================================================================ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.events.bind(self.loop)
        self._wake = asyncio.Event()
        for j in self.store.list(statuses=("postprocessing",), limit=10000):
            self.store.update(j["id"], status="failed", finished_at=now(), stage="failed",
                              error="The gateway restarted while collecting outputs. Frames written "
                                    f"so far are in {j['info'].get('output_dir')}.")
        for w in self.workers.values():
            self._tasks += [asyncio.create_task(self._health_loop(w), name=f"health-{w.name}"),
                            asyncio.create_task(self._ws_loop(w), name=f"ws-{w.name}"),
                            asyncio.create_task(self._reconcile_loop(w), name=f"reconcile-{w.name}")]
        self._tasks.append(asyncio.create_task(self._dispatch_loop(), name="dispatch"))
        self._convert_wake = asyncio.Event()
        self._tasks.append(asyncio.create_task(self._convert_loop(), name="convert"))
        await asyncio.to_thread(self._clean_orphan_scratch)
        log.info("farm started with workers: %s", ", ".join(self.workers) or "none")

    async def stop(self) -> None:
        for t in self._tasks + list(self._bg):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._bg, return_exceptions=True)
        for w in self.workers.values():
            await w.client.close()

    def wake(self) -> None:
        if self.loop and self._wake:
            self.loop.call_soon_threadsafe(self._wake.set)
        if self.loop and self._convert_wake:
            self.loop.call_soon_threadsafe(self._convert_wake.set)

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    # ================================================================ events
    def _emit(self, job: dict | None) -> None:
        if job:
            self.events.publish({"type": "job", "job": job})

    def _emit_worker(self, w: Worker) -> None:
        self.events.publish({"type": "worker", "worker": w.summary(self.store)})

    def annotate(self, jobs: list[dict]) -> list[dict]:
        """Add queue_position and a rough eta_seconds (from past run times)."""
        n_workers = max(1, sum(1 for w in self.workers.values() if w.online))
        avg = {}

        def avg_for(wf: str) -> float | None:
            if wf not in avg:
                avg[wf] = self.store.stats(wf)["avg_seconds"]
            return avg[wf]

        ahead = 0.0
        for j in self.store.list(statuses=("running",), limit=1000):
            a = avg_for(j["workflow"])
            ahead += a * (1 - (j["progress"] or 0)) if a else 0
        for j in self.store.list(statuses=("dispatched",), limit=1000):
            ahead += avg_for(j["workflow"]) or 0
        position, eta = {}, {}
        for i, j in enumerate(self.store.queued(with_prompt=False)):
            position[j["id"]] = i + 1
            eta[j["id"]] = ahead / n_workers
            ahead += avg_for(j["workflow"]) or 0
        for j in jobs:
            j["queue_position"] = position.get(j["id"])
            a = avg_for(j["workflow"])
            if j["status"] == "queued":
                j["eta_seconds"] = round(eta.get(j["id"], 0) + (a or 0)) if a else None
            elif j["status"] in ("dispatched", "running"):
                j["eta_seconds"] = round(a * (1 - (j["progress"] or 0))) if a else None
            else:
                j["eta_seconds"] = None
        return jobs

    # ================================================================ submit / cancel
    def plan(self, workflow: str, params: dict, show: str | None = None, shot: str | None = None,
             job_id: str = "preview") -> dict:
        """Work out everything about a job without queuing it: validated params, what the
        loader reads, and exactly where the frames will land and what they're called.
        Used by submit() and by the dashboard's live destination preview."""
        wf = self.registry.get(workflow)
        values = wf.validate(params or {})
        pc = self.cfg.paths
        seq = C.resolve_input(values[wf.sequence_param], pc, self.cfg.convert, show=show, shot=shot,
                              texture=bool(wf.textures), ocio=self.cfg.ocio) \
            if wf.sequence_param else None
        if seq is not None and seq.convert:
            C.point_loader_at_scratch(seq, self.scratch_root, job_id)
        show_ = seq.show if seq else show
        shot_ = seq.shot if seq else shot
        seq_name = seq.seq_name if seq else None
        source = seq.source if seq else wf.tag
        plan = {"notes": list((seq.convert or {}).get("notes", [])) if seq else [],
                "wf": wf, "values": values, "seq": seq, "show": show_, "shot": shot_, "seq_name": seq_name,
                "source": source, "out_dir": None, "version": None, "picked": None,
                "frames_prefix": f"_comfyfarm_{job_id[:8]}_raw",
                "preview_prefix": f"comfyfarm/{job_id[:8]}_{source}_{wf.tag}"}
        if wf.textures:
            return self._plan_textures(plan, job_id)
        if not wf.outputs.get("frames"):
            return plan

        picked = values.get(wf.output_folder_param) if wf.output_folder_param else None
        root = P.resolve_output_folder(picked, pc) if picked else P.output_root(pc, show_, seq_name, shot_)
        dir_template = pc.output_dir
        if picked and not shot_ and "{shot" in dir_template:
            dir_template = "{root}"
            plan["notes"].append("No shot in the input path, so the frames go straight into the output folder.")
        versioned = "{version" in pc.frame_name or "{version" in dir_template
        exists = P.sequence_exists if pc.validate_inputs else (lambda _d, _p: False)
        base = {"source": source, "show": show_, "seq": seq_name, "shot": shot_, "tag": wf.tag,
                "ext": wf.outputs["frames"].get("ext", "png")}
        for v in range(1, 1000):
            out_dir = P.output_dir(dir_template, root=root, version=v, **base)
            tokens = dict(base, version=v, folder=out_dir.rpartition("/")[2])
            pattern = P.render(P.frame_pattern(pc.frame_name), "file name", **tokens)
            key = f"{out_dir}/{pattern}"
            # Taken = files of this version already there, or promised to a queued job.
            if not (self.store.output_reserved(key) or exists(out_dir, pattern)):
                break
            if not versioned:
                raise FarmError(f"{P.to_posix(key, pc.mappings) or key} already exists. Add {{version}} "
                                f"to paths.frame_name so new runs don't overwrite it.", 409)
        else:
            raise FarmError(f"No free version in {out_dir} (999 tried).")
        P.check_writable(out_dir, pc)
        plan.update(out_dir=out_dir, version=v, picked=bool(picked), output_key=key)
        plan["name_tokens"] = tokens
        first = seq.first if seq and seq.first is not None else 1
        count = seq.frame_count if seq else None
        plan["first_frame"] = first
        plan["first_file"] = P.render(pc.frame_name, "file name", frame=first, **tokens)
        plan["last_file"] = P.render(pc.frame_name, "file name", frame=first + count - 1, **tokens) if count else None
        return plan

    def _plan_textures(self, plan: dict, job_id: str) -> dict:
        """Texture workflows: which UDIM/frame each input texture is, the TEX file names of
        every map, and a versioned folder for this run (the names carry no version)."""
        pc, wf, values, seq = self.cfg.paths, plan["wf"], plan["values"], plan["seq"]
        spec = wf.textures
        if seq is None:
            raise FarmError("This workflow needs an input texture.")
        numbering = values.get("numbering") or T.DEFAULTS["numbering"]
        if pc.validate_inputs:
            names = P.loaded_names(seq)
        else:   # can't list the input: go by the frame numbers alone
            nums = seq.frames or (range(seq.first, seq.last + 1) if seq.first is not None else [None])
            names = [f"x.{n:04d}.png" if n is not None else "x.png" for n in nums]
        items, has_udim = T.items_for(names, numbering)
        tokens = T.name_tokens(values, T.resolve_tile(values.get("tile") or "auto", items, has_udim))

        picked = values.get(wf.output_folder_param) if wf.output_folder_param else None
        root = P.resolve_output_folder(picked, pc) if picked else \
            P.output_root(pc, plan["show"], plan["seq_name"], plan["shot"])
        asset = P.context_from_path(seq.farm_path, pc.context_patterns, pc.shot_name).get("asset")
        base = {"root": root, "show": plan["show"], "seq": plan["seq_name"], "shot": plan["shot"],
                "asset": asset, "source": plan["source"], "tag": wf.tag}

        def taken(d: str) -> bool:   # files there already, or promised to a queued job
            if self.store.output_reserved(d + "/"):
                return True
            try:
                return pc.validate_inputs and bool(os.listdir(d))
            except OSError:
                return False

        for v in range(1, 1000):
            out_dir = T.output_dir(spec.dirs, version=v, **base)
            if not taken(out_dir):
                break
        else:
            raise FarmError(f"No free version for {out_dir} (999 tried).")
        P.check_writable(out_dir, pc)
        maps = [{"name": m.name, "node": m.node, "channel": m.channel, "colorspace": m.colorspace,
                 "color": m.color, "bits": m.bits, "prefix": f"_cf{job_id[:8]}_{m.channel}"} for m in spec.maps]
        textures = {"raw_dir": f"{self.scratch_root}/{job_id}/maps", "name": spec.name, "ext": spec.ext,
                    "tokens": tokens, "items": [list(i) for i in items], "maps": maps, "has_udim": has_udim,
                    "numbering": numbering,
                    # Frozen per job, like the input conversion: the repo's OCIO config.
                    "ocio": C.ocio_setup(self.cfg.ocio, pc)}
        shown = next((m for m in maps if m["color"]), maps[0])
        plan.update(out_dir=out_dir, version=v, picked=bool(picked), output_key=out_dir + "/", textures=textures,
                    first_frame=seq.first,
                    first_file=T.file_name(spec.name, shown, tokens, *items[0]),
                    last_file=T.file_name(spec.name, shown, tokens, *items[-1]) if len(items) > 1 else None,
                    what=T.describe(len(maps), items, has_udim, numbering), asset=asset)
        return plan

    def plan_summary(self, plan: dict) -> dict:
        """The JSON-safe part of a plan, as the dashboard shows it."""
        pc = self.cfg.paths
        seq = plan["seq"]
        out = plan["out_dir"]
        return {
            "show": plan["show"], "seq": plan["seq_name"], "shot": plan["shot"], "source": plan["source"],
            "frames": seq.frame_count if seq else None,
            "first": seq.first if seq else None, "last": seq.last if seq else None,
            "output_dir": out, "output_dir_posix": P.to_posix(out, pc.mappings) if out else None,
            "picked": plan["picked"], "version": plan["version"], "notes": plan["notes"],
            "input_kind": seq.kind if seq else None, "convert": C.summary(seq, pc),
            "first_file": plan.get("first_file"), "last_file": plan.get("last_file"),
            "what": plan.get("what"), "asset": plan.get("asset"),
            "maps": [m["channel"] for m in plan["textures"]["maps"]] if plan.get("textures") else None,
        }

    def submit(self, workflow: str, params: dict, user: str, name: str | None = None,
               priority: int = 0, show: str | None = None, shot: str | None = None) -> dict:
        """Validate and queue a job. Runs in a thread (touches network storage)."""
        pc = self.cfg.paths
        job_id = str(uuid.uuid4())
        with self._submit_lock:   # output folders are reserved by inserting the job
            plan = self.plan(workflow, params, show=show, shot=shot, job_id=job_id)
            wf, values, seq, out_dir = plan["wf"], plan["values"], plan["seq"], plan["out_dir"]
            prompt = wf.build(values, seq, out_dir, plan["frames_prefix"], plan["preview_prefix"],
                              textures=plan.get("textures"))
            info = {
                "show": plan["show"], "seq": plan["seq_name"], "shot": plan["shot"], "source": plan["source"],
                "tag": wf.tag, "version": plan["version"],
                "input": seq.as_dict() if seq else None,
                "input_posix": P.to_posix(seq.farm_path, pc.mappings) if seq else None,
                "first_frame": plan.get("first_frame"),
                "expected_frames": seq.frame_count if seq else None,
                "output_dir": out_dir, "output_dir_picked": plan["picked"],
                "output_dir_posix": P.to_posix(out_dir, pc.mappings) if out_dir else None,
                "notes": plan["notes"], "input_kind": seq.kind if seq else None,
                "convert_summary": C.summary(seq, pc),
                "frames_prefix": plan["frames_prefix"], "preview_prefix": plan["preview_prefix"],
                # Frozen per job, so a config edit can't rename frames of jobs already queued.
                "name_tokens": plan.get("name_tokens"), "frame_name": pc.frame_name,
                "preview_name": pc.preview_name,
                # Texture workflows: every map's file names, also frozen per job.
                "textures": plan.get("textures"), "what": plan.get("what"),
            }
            job = self.store.insert({
                "id": job_id, "workflow": wf.name, "user": user,
                "name": name or plan["shot"] or plan["source"],
                "status": "preparing" if seq and seq.convert else "queued",
                "priority": priority, "params": values, "info": info,
                "prompt": prompt, "output_dir": out_dir, "output_key": plan.get("output_key"),
                "created_at": now(), "stage": "waiting to convert input" if seq and seq.convert else "queued",
                "warnings": list(plan["notes"]),
            })
        log.info("job %s queued by %s: %s %s -> %s", job_id[:8], user, wf.name, plan["source"], out_dir)
        self._emit(job)
        self.wake()
        return job

    def retry(self, job_id: str, user: str) -> dict:
        old = self.store.get(job_id)
        if not old:
            raise FarmError("Job not found.", 404)
        info = old["info"] or {}
        return self.submit(old["workflow"], old["params"], user, name=old["name"],
                           priority=old["priority"], show=info.get("show"), shot=info.get("shot"))

    async def cancel(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if not job:
            raise FarmError("Job not found.", 404)
        jid = job["id"]
        if job["status"] in ("preparing", "queued"):
            j = self.store.transition(jid, ("preparing", "queued"), status="cancelled", stage="cancelled",
                                      finished_at=now())
            if j:
                task = self._converting.pop(jid, None)
                if task:
                    task.cancel()   # kills oiiotool/ffmpeg
                self._cleanup_scratch(j)
                self._emit(j)
                return j
            job = self.store.get(jid)
        if job["status"] in ON_WORKER:
            w = self.workers.get(job["worker"])
            pid = job["prompt_id"]
            job = self.store.update(jid, cancel_requested=1, stage="cancelling")
            self._emit(job)
            if w is None:
                return self._finish_cancel(jid)
            try:
                running, pending = await w.client.queue()
                if pid in pending:
                    await w.client.delete_queued([pid])
                    running, pending = await w.client.queue()
                if pid in running:
                    # Only interrupt what is actually running: older ComfyUI builds ignore
                    # prompt_id and would stop whichever job is on the GPU.
                    await w.client.interrupt(pid)
                elif pid not in pending and not await w.client.history(pid):
                    return self._finish_cancel(jid)   # it was only waiting; now gone
            except httpx.HTTPError as e:
                log.warning("cancel %s: worker %s unreachable (%s); will cancel when it returns",
                            jid[:8], w.name, e)
                w.abandoned.add(pid)
            return self.store.get(jid)   # the interrupt event finishes it
        if job["status"] == "postprocessing":
            raise FarmError("The job already finished on the GPU and is collecting outputs.", 409)
        raise FarmError(f"Job is already {job['status']}.", 409)

    def _finish_cancel(self, job_id: str) -> dict | None:
        j = self.store.transition(job_id, ON_WORKER + ("queued",), status="cancelled",
                                  stage="cancelled", finished_at=now())
        if j and j.get("prompt_id"):
            self.runtime.pop(j["prompt_id"], None)
        if j:
            self._cleanup_scratch(j)
        self._emit(j)
        self.wake()
        return j or self.store.get(job_id)

    def _fail(self, job_id: str, message: str, from_statuses: tuple = ACTIVE,
              traceback: str | None = None) -> dict | None:
        j = self.store.transition(job_id, from_statuses, status="failed", error=message,
                                  stage="failed", finished_at=now())
        if j:
            log.warning("job %s failed: %s", job_id[:8], message)
            if traceback:
                info = dict(j["info"] or {})
                info["traceback"] = traceback[-8000:]
                j = self.store.update(job_id, info=info)
            if j.get("prompt_id"):
                self.runtime.pop(j["prompt_id"], None)
            if not self.cfg.convert.keep_failed:
                self._cleanup_scratch(j)
        self._emit(j)
        self.wake()
        return j

    # ================================================================ input conversion
    def _scratch_dir(self, job: dict) -> str | None:
        """<scratch>/<job id>/: converted input frames, and the raw images of texture maps
        (in its maps/ subfolder)."""
        info = job.get("info") or {}
        conv = (info.get("input") or {}).get("convert")
        if conv:
            return conv.get("dst_dir")
        tex = info.get("textures")
        return P.normalize(tex["raw_dir"]).rstrip("/").rpartition("/")[0] + "/" if tex else None

    def _cleanup_scratch(self, job: dict) -> None:
        d = self._scratch_dir(job)
        if d and P.normalize(d).startswith(self.scratch_root + "/"):
            shutil.rmtree(d, ignore_errors=True)

    def _clean_orphan_scratch(self) -> None:
        """Converted frames of jobs that are gone (finished while the gateway was down)."""
        if not os.path.isdir(self.scratch_root):
            return
        active = {j["id"] for j in self.store.list(statuses=ACTIVE, limit=100000)}
        failed_keep = self.cfg.convert.keep_failed
        for name in os.listdir(self.scratch_root):
            if name in active:
                continue
            job = self.store.get(name)
            if failed_keep and job and job["status"] == "failed":
                continue
            shutil.rmtree(os.path.join(self.scratch_root, name), ignore_errors=True)

    async def _convert_loop(self) -> None:
        """Convert EXR/movie inputs ahead of the GPU: jobs wait in 'preparing', then join
        the queue in their original order once their frames are ready."""
        while True:
            try:
                await asyncio.wait_for(self._convert_wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._convert_wake.clear()
            try:
                waiting = [j for j in reversed(self.store.list(statuses=("preparing",), limit=10000))
                           if j["id"] not in self._converting]
                for job in waiting:
                    if len(self._converting) >= max(1, self.cfg.convert.max_parallel_jobs):
                        break
                    task = asyncio.create_task(self._convert_job(job["id"]), name=f"convert-{job['id'][:8]}")
                    self._converting[job["id"]] = task
                    task.add_done_callback(lambda _t, jid=job["id"]: self._converting.pop(jid, None))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("convert loop error")

    async def _convert_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job or job["status"] != "preparing":
            return
        seq_info = (job["info"] or {}).get("input") or {}
        kind = seq_info.get("convert", {}).get("kind", "?")
        to = seq_info.get("convert", {}).get("to") or "PNG"
        label = {"exr": f"converting EXR to {to}", "movie": "extracting movie frames"}.get(kind, kind)
        t0 = now()
        last = [0.0]

        def progress(done: int, total: int) -> None:
            if now() - last[0] < 0.5 and done < total:
                return
            last[0] = now()
            self._emit(self.store.transition(job_id, ("preparing",), stage=f"{label} {done}/{total}"))

        self._emit(self.store.transition(job_id, ("preparing",), stage=label))
        log.info("job %s: %s", job_id[:8], label)
        try:
            await C.convert(seq_info, self.cfg.convert, progress)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, (C.ConvertError, P.SequenceError)) else f"{type(e).__name__}: {e}"
            self._fail(job_id, f"Converting the input failed: {msg}", ("preparing",))
            return
        j = self.store.transition(job_id, ("preparing",), status="queued", stage="queued")
        if j:
            log.info("job %s: input ready in %.0fs", job_id[:8], now() - t0)
            self._emit(j)
            self.wake()
        else:   # cancelled meanwhile
            self._cleanup_scratch(self.store.get(job_id) or {})

    async def _requeue_or_fail(self, w: Worker, job: dict, reason: str) -> None:
        if job.get("prompt_id"):
            self.runtime.pop(job["prompt_id"], None)
            w.abandoned.add(job["prompt_id"])
        if job["cancel_requested"]:
            self._finish_cancel(job["id"])
        elif job["attempts"] < self.cfg.farm.max_attempts:
            j = self.store.transition(job["id"], ON_WORKER, status="queued", worker=None, prompt_id=None,
                                      progress=0, stage="requeued", started_at=None,
                                      warnings=job["warnings"] + [reason])
            log.warning("job %s requeued: %s", job["id"][:8], reason)
            self._emit(j)
            self.wake()
        else:
            self._fail(job["id"], f"{reason} Gave up after {job['attempts']} attempts.", ON_WORKER)

    # ================================================================ dispatch
    async def _dispatch_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self._dispatch_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("dispatch error")

    async def _dispatch_once(self) -> None:
        online = [w for w in self.workers.values() if w.online and w.ws_connected and w.checked]
        if not online:
            return
        for job in self.store.queued():
            capable = online
            if self.cfg.farm.strict_preflight:
                capable = [w for w in online if not w.problems.get(job["workflow"])]
                if not capable:
                    probs = online[0].problems[job["workflow"]]
                    self._fail(job["id"], "The farm machine can't run this workflow: " + "; ".join(probs[:4])
                               + (f" (+{len(probs) - 4} more)" if len(probs) > 4 else "")
                               + ". Run `python -m comfyfarm check` on the farm machine.", ("queued",))
                    continue
            free = [w for w in capable if self.store.count_on_worker(w.name) < w.cfg.max_inflight]
            if not free:
                continue
            w = min(free, key=lambda x: self.store.count_on_worker(x.name))
            if await self._dispatch(w, job) is None:
                return   # worker trouble; try again on the next tick

    async def _dispatch(self, w: Worker, job: dict) -> bool | None:
        info = job["info"] or {}
        titles = {nid: (n.get("_meta") or {}).get("title") or n.get("class_type")
                  for nid, n in job["prompt"].items()}
        out_dir = info.get("output_dir")
        if out_dir:
            try:
                await asyncio.to_thread(os.makedirs, out_dir, exist_ok=True)
            except OSError as e:
                self._fail(job["id"], f"Can't create the output folder {out_dir}: {e.strerror or e}", ("queued",))
                return False
        tex = info.get("textures")
        if tex:
            # Empty, so a second attempt can't mix its map images with the first one's.
            try:
                await asyncio.to_thread(shutil.rmtree, tex["raw_dir"], True)
                await asyncio.to_thread(os.makedirs, tex["raw_dir"], exist_ok=True)
            except OSError as e:
                self._fail(job["id"], f"Can't create the scratch folder {tex['raw_dir']}: {e.strerror or e}", ("queued",))
                return False
        pid = job["id"] if job["attempts"] == 0 else str(uuid.uuid4())
        extra = {"comfyfarm_job": job["id"], "comfyfarm_user": job["user"]}
        try:
            pid, node_errors = await w.client.submit(job["prompt"], pid, w.client_id, extra)
        except ComfyRejected as e:
            self._fail(job["id"], "ComfyUI rejected the job: " + describe_rejection(e.body, titles), ("queued",))
            return False
        except httpx.HTTPError as e:
            # The request may have landed even though we got no answer; our own prompt id
            # lets us check instead of risking a duplicate run.
            try:
                running, pending = await w.client.queue()
                if pid not in running and pid not in pending and not await w.client.history(pid):
                    raise LookupError
                node_errors = {}
            except Exception:  # noqa: BLE001
                log.warning("submit to %s failed: %s", w.name, e)
                return None
        if node_errors:
            try:
                await w.client.delete_queued([pid])
            except httpx.HTTPError:
                w.abandoned.add(pid)
            body = {"error": {"message": "Some outputs failed validation"}, "node_errors": node_errors}
            self._fail(job["id"], "ComfyUI rejected the job: " + describe_rejection(body, titles), ("queued",))
            return False
        j = self.store.transition(job["id"], ("queued",), status="dispatched", worker=w.name, prompt_id=pid,
                                  dispatched_at=now(), stage="waiting for GPU", attempts=job["attempts"] + 1)
        if j is None:   # cancelled while we were submitting
            try:
                await w.client.delete_queued([pid])
            except httpx.HTTPError:
                w.abandoned.add(pid)
            return False
        self._new_runtime(w, j, pid, titles)
        log.info("job %s -> %s (prompt %s)", j["id"][:8], w.name, pid[:8])
        self._emit(j)
        return True

    def _new_runtime(self, w: Worker, job: dict, pid: str, titles: dict | None = None) -> Runtime:
        wf = self.registry.workflows.get(job["workflow"])
        stats = self.store.stats(job["workflow"])
        expected = stats.get("node_count") or (wf.expected_nodes(w.output_classes) if wf else 20)
        if titles is None:
            prompt = (self.store.get(job["id"], with_prompt=True) or {}).get("prompt") or {}
            titles = {nid: (n.get("_meta") or {}).get("title") or n.get("class_type") for nid, n in prompt.items()}
        rt = Runtime(job_id=job["id"], expected=expected, titles=titles)
        self.runtime[pid] = rt
        return rt

    # ================================================================ monitoring
    async def _ws_loop(self, w: Worker) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(w.client.ws_url(w.client_id), max_size=None,
                                              open_timeout=10, ping_interval=20, ping_timeout=30) as ws:
                    w.ws_connected = True
                    backoff = 1.0
                    log.info("connected to %s events", w.name)
                    # A reconnect usually means ComfyUI restarted, maybe after node packs
                    # or models changed: re-run the preflight before dispatching again.
                    w.checked = False
                    await self._refresh_object_info(w)
                    self._emit_worker(w)
                    await self._reconcile(w)
                    self.wake()
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            continue   # live preview images
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        try:
                            await self._on_message(w, msg)
                        except Exception:  # noqa: BLE001
                            log.exception("error handling %s event", msg.get("type"))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("ws %s: %s", w.name, e)
            if w.ws_connected:
                w.ws_connected = False
                log.warning("lost event connection to %s", w.name)
                self._emit_worker(w)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _runtime_for(self, w: Worker, pid: str) -> tuple[dict | None, Runtime | None]:
        rt = self.runtime.get(pid)
        job = self.store.get(rt.job_id) if rt else self.store.by_prompt_id(pid)
        if job is None:
            return None, None
        if rt is None and job["status"] in ON_WORKER:
            rt = self._new_runtime(w, job, pid)
        return job, rt

    def _push_progress(self, job: dict, rt: Runtime, stage: str | None = None, force: bool = False) -> None:
        t = now()
        if not force and t - rt.last_push < 0.5:
            return
        rt.last_push = t
        fields = {"progress": round(rt.progress(), 4)}
        if stage:
            fields["stage"] = stage
        self._emit(self.store.transition(job["id"], ON_WORKER, **fields))

    async def _on_message(self, w: Worker, msg: dict) -> None:
        kind, data = msg.get("type"), msg.get("data") or {}
        if kind == "status":
            w.queue_remaining = ((data.get("status") or {}).get("exec_info") or {}).get("queue_remaining")
            return
        pid = data.get("prompt_id")
        if not pid:
            return
        if pid in w.abandoned:
            return
        job, rt = self._runtime_for(w, pid)
        if job is None:
            return
        jid = job["id"]

        if kind == "execution_start":
            j = self.store.transition(jid, ("dispatched",), status="running", started_at=now(), stage="starting")
            self._emit(j)
        elif kind == "execution_cached" and rt:
            rt.done.update(data.get("nodes") or [])
            self._push_progress(job, rt, "using cached results")
        elif kind == "executing" and rt:
            node = data.get("node")
            if job["status"] == "dispatched":
                job = self.store.transition(jid, ("dispatched",), status="running", started_at=now()) or job
            if rt.current and rt.current != node:
                rt.done.add(rt.current)
            rt.current, rt.frac = node, 0.0
            if node is None:   # older ComfyUI signals completion this way
                self._spawn(self._finalize(w, jid, pid))
            else:
                self._push_progress(job, rt, rt.titles.get(node, node), force=True)
        elif kind == "progress" and rt:
            node = data.get("node")
            if node and node != rt.current:
                if rt.current:
                    rt.done.add(rt.current)
                rt.current = node
            mx = data.get("max") or 0
            rt.frac = (data.get("value") or 0) / mx if mx else 0.0
            self._push_progress(job, rt, f"{rt.titles.get(rt.current, rt.current)} "
                                         f"{data.get('value')}/{mx}")
        elif kind == "executed" and rt:
            if data.get("node"):
                rt.done.add(data["node"])
        elif kind == "execution_success":
            self._spawn(self._finalize(w, jid, pid))
        elif kind == "execution_error":
            title = rt.titles.get(str(data.get("node_id")), "") if rt else ""
            msg_ = (f"{data.get('node_type')} (node {data.get('node_id')}"
                    f"{' ' + repr(title) if title else ''}): {str(data.get('exception_message', '')).strip()}")
            self._fail(jid, msg_, ON_WORKER, traceback="".join(data.get("traceback") or []) or None)
            self._spawn(self._drop_history(w, pid))
        elif kind == "execution_interrupted":
            if job["cancel_requested"]:
                self._finish_cancel(jid)
            else:
                self._fail(jid, "Interrupted on the farm machine.", ON_WORKER)
            self._spawn(self._drop_history(w, pid))

    async def _drop_history(self, w: Worker, pid: str) -> None:
        if not self.cfg.farm.delete_comfy_history:
            return
        try:
            await w.client.delete_history([pid])
        except httpx.HTTPError:
            pass

    # ================================================================ reconcile
    async def _reconcile_loop(self, w: Worker) -> None:
        while True:
            await asyncio.sleep(self.cfg.farm.reconcile_seconds)
            if not (w.online and w.ws_connected):
                continue
            try:
                await self._reconcile(w)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("reconcile %s failed", w.name)

    async def _reconcile(self, w: Worker) -> None:
        """Correct our view from ComfyUI's queue and history: catches events missed while
        disconnected, and jobs ComfyUI lost because it restarted."""
        t0 = now()
        jobs = [j for j in self.store.on_worker(w.name) if (j["dispatched_at"] or 0) < t0 - 2]
        if not jobs and not w.abandoned:
            return
        running, pending = await w.client.queue()
        for pid in list(w.abandoned):
            try:
                if pid in pending:
                    await w.client.delete_queued([pid])
                elif pid in running:
                    await w.client.interrupt(pid)
            except httpx.HTTPError:
                continue
            if pid not in running:
                w.abandoned.discard(pid)
        for j in jobs:
            pid = j["prompt_id"]
            if pid in running:
                if j["status"] == "dispatched":
                    self._emit(self.store.transition(j["id"], ("dispatched",), status="running", started_at=now()))
                if pid not in self.runtime:
                    self._new_runtime(w, j, pid)
                continue
            if pid in pending:
                continue
            hist = await w.client.history(pid)
            if hist:
                status = hist.get("status") or {}
                if status.get("status_str") == "success" or (status.get("completed") and status.get("status_str") != "error"):
                    self._spawn(self._finalize(w, j["id"], pid, hist))
                else:
                    message, interrupted, tb = _history_failure(hist)
                    if interrupted and j["cancel_requested"]:
                        self._finish_cancel(j["id"])
                    else:
                        self._fail(j["id"], message, ON_WORKER, traceback=tb)
                    self._spawn(self._drop_history(w, pid))
                continue
            await self._requeue_or_fail(w, j, f"ComfyUI on {w.name} lost the job (restarted?).")

    # ================================================================ health
    async def _health_loop(self, w: Worker) -> None:
        fails = 0
        while True:
            try:
                w.stats = await w.client.system_stats()
                w.last_seen = now()
                fails = 0
                if not w.online:
                    w.online, w.error = True, None
                    log.info("worker %s online", w.name)
                    self._emit_worker(w)
                    self.wake()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                if w.online and fails >= 2:
                    w.online, w.offline_since = False, now()
                    w.error = str(e) or type(e).__name__
                    log.warning("worker %s offline: %s", w.name, w.error)
                    self._emit_worker(w)
                if not w.online and now() - w.offline_since > self.cfg.farm.lost_after_seconds:
                    for j in self.store.on_worker(w.name):
                        await self._requeue_or_fail(
                            w, j, f"Worker {w.name} was unreachable for over "
                                  f"{self.cfg.farm.lost_after_seconds / 60:.0f} minutes.")
            await asyncio.sleep(self.cfg.farm.health_seconds)

    async def _refresh_object_info(self, w: Worker) -> None:
        try:
            oi = await w.client.object_info()
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't read node definitions from %s: %s", w.name, e)
            w.checked = True
            return
        w.output_classes = {k for k, v in oi.items() if isinstance(v, dict) and v.get("output_node")}
        w.problems = {}
        for name, wf in self.registry.workflows.items():
            probs = wf.check_object_info(oi)
            if probs:
                w.problems[name] = probs
                for p in probs:
                    log.error("worker %s / workflow %s: %s", w.name, name, p)
        w.checked = True

    async def check_workers(self) -> dict:
        for w in self.workers.values():
            if w.online:
                await self._refresh_object_info(w)
        return {w.name: w.summary(self.store) for w in self.workers.values()}

    # ================================================================ outputs
    async def _finalize(self, w: Worker, job_id: str, pid: str, hist: dict | None = None) -> None:
        j = self.store.transition(job_id, ON_WORKER, status="postprocessing",
                                  stage="collecting outputs", progress=0.99)
        if j is None:
            return   # already handled (duplicate event, cancel, reconcile)
        rt = self.runtime.pop(pid, None)
        self._emit(j)
        try:
            if hist is None:
                for _ in range(10):   # history is written just after the success event
                    hist = await w.client.history(pid)
                    if hist:
                        break
                    await asyncio.sleep(0.5)
            outputs, warnings = await self._collect_outputs(w, j, hist or {})
            t = now()
            done = self.store.transition(job_id, ("postprocessing",), status="done", progress=1.0,
                                         stage="done", finished_at=t, outputs=outputs,
                                         warnings=j["warnings"] + warnings)
            self._emit(done)
            self._cleanup_scratch(j)
            started = j["started_at"] or j["dispatched_at"] or t
            nodes = len(rt.done | ({rt.current} if rt.current else set())) if rt else None
            self.store.record_run(j["workflow"], t - started, nodes or None)
            log.info("job %s done in %.0fs -> %s", job_id[:8], t - started, outputs.get("output_dir"))
        except Exception as e:  # noqa: BLE001
            log.exception("collecting outputs for %s failed", job_id[:8])
            self._fail(job_id, f"Finished on the GPU but collecting outputs failed: {e}", ("postprocessing",))
        finally:
            await self._drop_history(w, pid)
            self.wake()

    async def _collect_outputs(self, w: Worker, job: dict, hist: dict) -> tuple[dict, list[str]]:
        info = job["info"] or {}
        wf = self.registry.workflows.get(job["workflow"])
        pc = self.cfg.paths
        out_dir = info.get("output_dir")
        outputs: dict = {"output_dir": out_dir, "output_dir_posix": info.get("output_dir_posix")}
        warnings: list[str] = []
        tokens = dict(info.get("name_tokens") or {})
        if wf is None:
            return outputs, ["Workflow no longer loaded; outputs left as ComfyUI wrote them."]

        tex = info.get("textures")
        if tex and out_dir:
            last = [0.0]

            def progress(done: int, total: int) -> None:
                if now() - last[0] < 0.5 and done < total:
                    return
                last[0] = now()
                self._emit(self.store.transition(job["id"], ("postprocessing",), stage=f"writing EXR maps {done}/{total}"))

            outputs.update(await T.write_maps(tex, out_dir, self.cfg.textures, self.cfg.convert, pc.mappings, progress))

        fr = wf.outputs.get("frames")
        if fr and out_dir:
            res = await asyncio.to_thread(self._rename_frames, info, fr.get("ext", "png"))
            outputs.update(res)
            exp = info.get("expected_frames")
            if exp and res["frames"] != exp:
                warnings.append(f"Got {res['frames']} output frames from {exp} input frames; "
                                f"numbering starts at {res['first']} and may not line up with the plate.")

        pv = wf.outputs.get("preview")
        if pv:
            node_out = (hist.get("outputs") or {}).get(str(pv["node"])) or {}
            entries = node_out.get("gifs") or node_out.get("videos") or node_out.get("images") or []
            if entries and out_dir:
                e = entries[-1]
                dest = Path(out_dir) / (info.get("preview_name") or pc.preview_name).format(**tokens)
                try:
                    await w.client.download(e["filename"], e.get("subfolder", ""), e.get("type", "output"), dest)
                    outputs["preview"] = P.normalize(str(dest))
                    outputs["preview_posix"] = P.to_posix(outputs["preview"], pc.mappings)
                except Exception as ex:  # noqa: BLE001
                    warnings.append(f"Couldn't copy the preview video: {ex}")
            elif not entries:
                warnings.append("ComfyUI returned no preview video.")
        return outputs, warnings

    def _rename_frames(self, info: dict, ext: str) -> dict:
        pc = self.cfg.paths
        out_dir = info["output_dir"]
        prefix = info["frames_prefix"]
        names = sorted(n for n in os.listdir(out_dir)
                       if n.startswith(prefix) and n.lower().endswith("." + ext.lower()))
        if not names:
            raise RuntimeError(f"no frames were written to {out_dir}")
        first = info.get("first_frame")
        first = 1 if first is None else first
        template = info.get("frame_name") or pc.frame_name
        tokens = dict(info.get("name_tokens") or {}, ext=ext)
        for i, n in enumerate(names):
            os.replace(os.path.join(out_dir, n), os.path.join(out_dir, template.format(frame=first + i, **tokens)))
        pattern = f"{out_dir}/{P.frame_pattern(template).format(**tokens)}"
        return {"frames": len(names), "first": first, "last": first + len(names) - 1,
                "frame_pattern": pattern, "frame_pattern_posix": P.to_posix(pattern, pc.mappings)}


__all__ = ["Farm", "FarmError", "ParamError"]
