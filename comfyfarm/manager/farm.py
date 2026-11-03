"""The manager side of the farm: submitting and planning jobs (validated inputs, versioned
output folders), texture sets, the queue and its order, dispatching to workers, input
conversion ahead of the GPU, cancels and retries.

Everything that talks to a ComfyUI (submitting a prompt, following it over the websocket,
checking ComfyUI's queue and history, collecting the outputs) is a worker's job: see
comfyfarm.agent.worker.WorkerRunner. The Farm is the host the workers report to
(protocol.JobHost). For now they run in this process, one per configured ComfyUI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from typing import TYPE_CHECKING

from ..config import Config
from ..fileio import convert as C
from ..fileio import inputs as I
from ..fileio import naming as N
from ..fileio import ocio as OC
from ..fileio import paths as P
from ..fileio import scratch as S
from ..fileio import textures as T
from ..protocol import ACTIVE, FINISHED, ON_WORKER, now
from ..workflows import ParamError, Workflow, WorkflowRegistry
from .db import JobStore
from .events import EventBus

if TYPE_CHECKING:   # the manager never imports the agent; workers are attached to it
    from ..agent.worker import WorkerRunner

log = logging.getLogger("comfyfarm")


class FarmError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Farm:
    """Job queue, sets and dispatch; the host its workers report to (protocol.JobHost)."""

    def __init__(self, cfg: Config, store: JobStore, registry: WorkflowRegistry, events: EventBus):
        self.cfg = cfg
        self.store = store
        self.registry = registry
        self.events = events
        # The workers that run jobs, by name: one per ComfyUI, attached with attach().
        self.workers: dict[str, WorkerRunner] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._submit_lock = threading.Lock()
        self._tasks: list[asyncio.Task] = []
        self._converting: dict[str, asyncio.Task] = {}   # job id -> conversion task
        self._converting_set: dict[str, str] = {}         # job id -> its set, for batches converting
        self._set_seen: dict[str, tuple] = {}             # set id -> last state published
        self._convert_wake: asyncio.Event | None = None
        self.scratch_root = P.normalize(str(cfg.resolve(cfg.convert.scratch_dir)))

    def attach(self, worker: WorkerRunner) -> None:
        """Add a worker that runs this farm's jobs on its ComfyUI."""
        self.workers[worker.name] = worker

    # ================================================================ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.events.bind(self.loop)
        self._wake = asyncio.Event()
        for j in self.store.list(statuses=("postprocessing",), limit=10000, top_level=False, work_only=True):
            self.store.update(j["id"], status="failed", finished_at=now(), stage="failed",
                              error="The gateway restarted while collecting outputs. Frames written "
                                    f"so far are in {j['info'].get('output_dir')}.")
        for s_ in self.store.list(statuses=ACTIVE, limit=10000):
            if s_["kind"] == "set":
                self._refresh_set(s_["id"])
        for w in self.workers.values():
            w.start()   # its health, event and reconcile loops
        self._tasks.append(asyncio.create_task(self._dispatch_loop(), name="dispatch"))
        self._convert_wake = asyncio.Event()
        self._tasks.append(asyncio.create_task(self._convert_loop(), name="convert"))
        await asyncio.to_thread(self._clean_orphan_scratch)
        log.info("farm started with workers: %s", ", ".join(self.workers) or "none")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for w in self.workers.values():
            await w.stop()

    def wake(self) -> None:
        if self.loop and self._wake:
            self.loop.call_soon_threadsafe(self._wake.set)
        if self.loop and self._convert_wake:
            self.loop.call_soon_threadsafe(self._convert_wake.set)

    # ================================================================ events
    def _emit(self, job: dict | None) -> None:
        if job:
            self.events.publish({"type": "job", "job": job})
            if job.get("parent_id"):
                self._refresh_set(job["parent_id"])

    # ================================================================ texture sets
    def _refresh_set(self, set_id: str) -> dict | None:
        """Work out a set's status, progress and outputs from its batches; called whenever
        a batch changes. The set itself never runs: it's what the artist sees."""
        parent = self.store.get(set_id)
        rows = self.store.batch_states(set_id)
        if not parent or not rows:
            return parent
        info = parent["info"] or {}
        tex = info.get("textures") or {}
        unit = "UDIMs" if tex.get("has_udim") else ("frames" if tex.get("numbering") == "frame" else "textures")
        total = sum(r["tiles"] or 0 for r in rows) or 1
        done = [r for r in rows if r["status"] == "done"]
        missed = [r for r in rows if r["status"] in ("failed", "cancelled")]
        busy = [r for r in rows if r["status"] in ON_WORKER + ("postprocessing",)]
        active = [r for r in rows if r["status"] in ACTIVE]
        done_tiles = sum(r["tiles"] or 0 for r in done)
        progress = min((done_tiles + sum((r["tiles"] or 0) * (r["progress"] or 0) for r in busy)) / total, 1.0)
        fields: dict = {"progress": round(progress, 4), "error": None, "outputs": None}
        failed_note = f", {len(missed)} batch{'es' if len(missed) != 1 else ''} failed" if missed else ""
        if active:
            started = busy or done or missed
            fields.update(status="running" if started else "queued", finished_at=None,
                          stage=f"{done_tiles}/{total} {unit}" + failed_note)
        else:
            if parent["cancel_requested"]:
                status, stage = "cancelled", f"cancelled: {done_tiles}/{total} {unit} written"
            elif missed:
                first = next((r["error"] for r in missed if r["error"]), None)
                labels = ", ".join(r["label"] or "?" for r in missed[:6]) + ("…" if len(missed) > 6 else "")
                status, stage = "failed", f"{done_tiles}/{total} {unit}" + failed_note
                fields["error"] = (f"{len(missed)} of {len(rows)} batches didn't finish ({labels})"
                                   + (f": {first.rstrip('.')}" if first else "") + ". Retry failed batches to finish "
                                   "the set in the same folder.")
            else:
                status, stage = "done", f"{done_tiles}/{total} {unit}"
            fields.update(status=status, stage=stage,
                          finished_at=max((r["finished_at"] or 0) for r in rows) or now())
        if done and tex:
            items = [i for r in done for i in json.loads(r["items"] or "[]")]
            fields["outputs"] = {"output_dir": parent["output_dir"], "output_dir_posix": info.get("output_dir_posix"),
                                 **T.map_outputs(tex, parent["output_dir"], self.cfg.paths.mappings, items)}
        starts = [r["started_at"] or r["dispatched_at"] for r in rows if r["started_at"] or r["dispatched_at"]]
        fields["started_at"] = min(starts) if starts else None
        fields["dispatched_at"] = min((r["dispatched_at"] for r in rows if r["dispatched_at"]), default=None)
        key = (fields["status"], fields["stage"], fields["progress"], fields["error"], len(done))
        if self._set_seen.get(set_id) == key and parent["status"] == fields["status"]:
            return parent
        self._set_seen[set_id] = key
        j = self.store.update(set_id, **fields)
        if j and fields["status"] in FINISHED and parent["status"] not in FINISHED:
            log.info("set %s %s: %s", set_id[:8], fields["status"], fields["stage"])
        self.events.publish({"type": "job", "job": j})
        return j

    def set_eta(self, set_id: str) -> float | None:
        """Seconds left for a set: its unfinished batches at the pace of its finished ones."""
        rows = self.store.batch_states(set_id)
        took = [r["finished_at"] - (r["started_at"] or r["dispatched_at"]) for r in rows
                if r["status"] == "done" and r["finished_at"] and (r["started_at"] or r["dispatched_at"])]
        if not took:
            return None
        avg = sum(took) / len(took)
        left = sum(1 - (r["progress"] or 0) for r in rows if r["status"] in ACTIVE)
        return avg * left / max(1, sum(1 for w in self.workers.values() if w.online))

    def annotate(self, jobs: list[dict]) -> list[dict]:
        """Add queue_position and a rough eta_seconds (from past run times)."""
        n_workers = max(1, sum(1 for w in self.workers.values() if w.online))
        avg = {}

        def avg_for(wf: str) -> float | None:
            if wf not in avg:
                avg[wf] = self.store.stats(wf)["avg_seconds"]
            return avg[wf]

        ahead = 0.0
        for j in self.store.list(statuses=("running",), limit=1000, top_level=False, work_only=True):
            a = avg_for(j["workflow"])
            ahead += a * (1 - (j["progress"] or 0)) if a else 0
        for j in self.store.list(statuses=("dispatched",), limit=1000, top_level=False, work_only=True):
            ahead += avg_for(j["workflow"]) or 0
        position, eta = {}, {}
        for i, j in enumerate(self.store.queued(with_prompt=False)):
            position[j["id"]] = i + 1
            eta[j["id"]] = ahead / n_workers
            ahead += avg_for(j["workflow"]) or 0
        for j in jobs:
            j["queue_position"] = position.get(j["id"])
            a = avg_for(j["workflow"])
            if j.get("kind") == "set":
                eta = self.set_eta(j["id"]) if j["status"] in ACTIVE else None
                j["eta_seconds"] = round(eta) if eta is not None else None
            elif j["status"] == "queued":
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
        seq = I.resolve_input(values[wf.sequence_param], pc, self.cfg.convert, show=show, shot=shot,
                              texture=bool(wf.textures), ocio=self.cfg.ocio,
                              work_resolution=wf.work_resolution, preview=job_id == "preview") \
            if wf.sequence_param else None
        if seq is not None and seq.convert:
            S.point_loader_at_scratch(seq, self.scratch_root, job_id)
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
        root = N.resolve_output_folder(picked, pc) if picked else N.output_root(pc, show_, seq_name, shot_)
        dir_template = pc.output_dir
        if picked and not shot_ and "{shot" in dir_template:
            dir_template = "{root}"
            plan["notes"].append("No shot in the input path, so the frames go straight into the output folder.")
        versioned = "{version" in pc.frame_name or "{version" in dir_template
        exists = N.sequence_exists if pc.validate_inputs else (lambda _d, _p: False)
        base = {"source": source, "show": show_, "seq": seq_name, "shot": shot_, "tag": wf.tag,
                "ext": wf.outputs["frames"].get("ext", "png")}
        for v in range(1, 1000):
            out_dir = N.output_dir(dir_template, root=root, version=v, **base)
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
            names = I.loaded_names(seq)
        else:   # can't list the input: go by the frame numbers alone
            nums = seq.frames or (range(seq.first, seq.last + 1) if seq.first is not None else [None])
            names = [f"x.{n:04d}.png" if n is not None else "x.png" for n in nums]
        items, has_udim = T.items_for(names, numbering)
        tokens = T.name_tokens(values, T.resolve_tile(values.get("tile") or "auto", items, has_udim))

        picked = values.get(wf.output_folder_param) if wf.output_folder_param else None
        root = N.resolve_output_folder(picked, pc) if picked else \
            N.output_root(pc, plan["show"], plan["seq_name"], plan["shot"])
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
                    "ocio": OC.ocio_setup(self.cfg.ocio, pc)}
        shown = next((m for m in maps if m["color"]), maps[0])
        what = T.describe(len(maps), items, has_udim, numbering)
        # More tiles than fit in one run (textures.batch), or tiles of different sizes (the
        # loader would crop them to one size): a set of batches, filling one folder.
        batches = None
        if len(items) > 1 and (spec.batch or pc.validate_inputs):
            sizes = I.work_sizes(seq, names, wf.work_resolution) \
                if pc.validate_inputs and job_id != "preview" else [None] * len(items)
            ranges = T.split_batches(sizes, spec.batch)
            if len(ranges) > 1:
                batches = ranges
                what += f" in {len(ranges)} batches of up to {max(b - a for a, b in ranges)}"
        plan.update(out_dir=out_dir, version=v, picked=bool(picked), output_key=out_dir + "/", textures=textures,
                    first_frame=seq.first,
                    first_file=T.file_name(spec.name, shown, tokens, *items[0]),
                    last_file=T.file_name(spec.name, shown, tokens, *items[-1]) if len(items) > 1 else None,
                    what=what, asset=asset, batches=batches)
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
            "input_kind": seq.kind if seq else None, "convert": I.summary(seq, pc),
            "first_file": plan.get("first_file"), "last_file": plan.get("last_file"),
            "what": plan.get("what"), "asset": plan.get("asset"),
            "maps": [m["channel"] for m in plan["textures"]["maps"]] if plan.get("textures") else None,
            "batches": len(plan["batches"]) if plan.get("batches") else None,
        }

    def submit(self, workflow: str, params: dict, user: str, name: str | None = None,
               priority: int = 0, show: str | None = None, shot: str | None = None) -> dict:
        """Validate and queue a job. Runs in a thread (touches network storage)."""
        job_id = str(uuid.uuid4())
        with self._submit_lock:   # output folders are reserved by inserting the job
            plan = self.plan(workflow, params, show=show, shot=shot, job_id=job_id)
            wf, values, seq, out_dir = plan["wf"], plan["values"], plan["seq"], plan["out_dir"]
            if plan.get("batches"):
                job = self._insert_set(plan, job_id, user, name, priority)
                log.info("set %s queued by %s: %s %s, %d batches -> %s", job_id[:8], user, wf.name,
                         plan["source"], len(plan["batches"]), out_dir)
                self._emit(job)
                self.wake()
                return job
            prompt = wf.build(values, seq, out_dir, plan["frames_prefix"], plan["preview_prefix"],
                              textures=plan.get("textures"))
            info = self._info(plan, seq, plan.get("textures"), plan.get("what"))
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

    def _info(self, plan: dict, seq, textures: dict | None, what: str | None) -> dict:
        """A job's frozen info: everything about it worked out at submit."""
        pc, wf, out_dir = self.cfg.paths, plan["wf"], plan["out_dir"]
        return {
            "show": plan["show"], "seq": plan["seq_name"], "shot": plan["shot"], "source": plan["source"],
            "tag": wf.tag, "version": plan["version"],
            "input": seq.as_dict() if seq else None,
            "input_posix": P.to_posix(seq.farm_path, pc.mappings) if seq else None,
            "first_frame": plan.get("first_frame"),
            "expected_frames": seq.frame_count if seq else None,
            "output_dir": out_dir, "output_dir_picked": plan["picked"],
            "output_dir_posix": P.to_posix(out_dir, pc.mappings) if out_dir else None,
            "notes": plan["notes"], "input_kind": seq.kind if seq else None,
            "convert_summary": I.summary(seq, pc),
            "frames_prefix": plan["frames_prefix"], "preview_prefix": plan["preview_prefix"],
            # Frozen per job, so a config edit can't rename frames of jobs already queued.
            "name_tokens": plan.get("name_tokens"), "frame_name": pc.frame_name,
            "preview_name": pc.preview_name,
            # Texture workflows: every map's file names, also frozen per job.
            "textures": textures, "what": what,
        }

    def _insert_set(self, plan: dict, set_id: str, user: str, name: str | None, priority: int) -> dict:
        """A texture set: the set the artist sees (one output folder, reserved by it) and
        a batch job per range of tiles, each an ordinary run into that folder."""
        wf, values, seq, out_dir = plan["wf"], plan["values"], plan["seq"], plan["out_dir"]
        tex, ranges = plan["textures"], plan["batches"]
        items = [tuple(i) for i in tex["items"]]
        name = name or plan["shot"] or plan["source"]
        unit = "UDIMs" if tex["has_udim"] else ("frames" if tex["numbering"] == "frame" else "textures")
        t = now()
        info = self._info(plan, seq, tex, plan["what"])
        info["input"]["convert"] = None        # the batches convert their own tiles
        info["set"] = {"batches": len(ranges), "tiles": len(items), "batch": wf.textures.batch}
        rows = [{"id": set_id, "kind": "set", "workflow": wf.name, "user": user, "name": name, "status": "queued",
                 "priority": priority, "params": values, "info": info, "prompt": None, "output_dir": out_dir,
                 "output_key": plan.get("output_key"), "created_at": t, "stage": f"0/{len(items)} {unit}",
                 "warnings": list(plan["notes"])}]
        for k, (a, b) in enumerate(ranges):
            bid = str(uuid.uuid4())
            bseq = I.batch_sequence(seq, a, b)
            if bseq.convert:
                S.point_loader_at_scratch(bseq, self.scratch_root, bid)
            btex = dict(tex, items=tex["items"][a:b], raw_dir=f"{self.scratch_root}/{bid}/maps",
                        maps=[dict(m, prefix=f"_cf{bid[:8]}_{m['channel']}") for m in tex["maps"]])
            label = T.batch_label(items[a:b], tex["has_udim"])
            binfo = self._info(plan, bseq, btex, T.describe(len(btex["maps"]), items[a:b], tex["has_udim"],
                                                             tex["numbering"]))
            binfo["batch"] = {"set": set_id, "index": k + 1, "of": len(ranges), "tiles": b - a, "label": label}
            rows.append({"id": bid, "kind": "batch", "parent_id": set_id, "workflow": wf.name, "user": user,
                         "name": f"{name} · {label}", "status": "preparing" if bseq.convert else "queued",
                         "priority": priority, "params": values, "info": binfo,
                         "prompt": wf.build(values, bseq, out_dir, plan["frames_prefix"], plan["preview_prefix"],
                                            textures=btex),
                         "output_dir": out_dir, "output_key": None, "created_at": t,
                         "stage": "waiting to convert input" if bseq.convert else "queued", "warnings": []})
        self.store.insert_many(rows)
        return self._refresh_set(set_id) or self.store.get(set_id)

    def retry_failed(self, job_id: str) -> dict:
        """Run a set's failed and cancelled batches again, into the same folder (or one
        batch, given its id). The rest of the set is kept."""
        job = self.store.get(job_id)
        if not job:
            raise FarmError("Job not found.", 404)
        if job["kind"] == "set":
            targets = self.store.list(parent=job["id"], statuses=("failed", "cancelled"), limit=100000)
            set_id = job["id"]
        elif job["kind"] == "batch" and job["status"] in ("failed", "cancelled"):
            targets, set_id = [job], job["parent_id"]
        else:
            raise FarmError("Only a set's failed or cancelled batches can be retried in place; "
                            "use Run again for a new run.", 409)
        if not targets:
            raise FarmError("No failed or cancelled batches to retry.", 409)
        self.store.update(set_id, cancel_requested=0)
        self._set_seen.pop(set_id, None)
        for b in targets:
            convert = ((b["info"] or {}).get("input") or {}).get("convert")
            self.store.transition(b["id"], ("failed", "cancelled"), status="preparing" if convert else "queued",
                                  stage="waiting to convert input" if convert else "queued", error=None,
                                  progress=0, worker=None, prompt_id=None, dispatched_at=None, started_at=None,
                                  finished_at=None, cancel_requested=0, outputs=None)
        log.info("set %s: retrying %d batch(es)", set_id[:8], len(targets))
        j = self._refresh_set(set_id)
        self.wake()
        return j or self.store.get(set_id)

    def retry(self, job_id: str, user: str) -> dict:
        old = self.store.get(job_id)
        if not old:
            raise FarmError("Job not found.", 404)
        if old["kind"] == "batch":
            raise FarmError("That's one batch of a set: Retry failed on the set reruns it in place, Run again "
                            "on the set runs the whole set again.", 409)
        info = old["info"] or {}
        return self.submit(old["workflow"], old["params"], user, name=old["name"],
                           priority=old["priority"], show=info.get("show"), shot=info.get("shot"))

    async def cancel(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if not job:
            raise FarmError("Job not found.", 404)
        jid = job["id"]
        if job["kind"] == "set":
            return await self._cancel_set(job)
        if job["status"] in ("preparing", "queued"):
            j = self.store.transition(jid, ("preparing", "queued"), status="cancelled", stage="cancelled",
                                      finished_at=now())
            if j:
                task = self._converting.pop(jid, None)
                if task:
                    task.cancel()   # kills oiiotool/ffmpeg
                S.cleanup(j, self.scratch_root)
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
            if await w.cancel(jid, pid):
                return self._finish_cancel(jid)   # it was only waiting; now gone
            return self.store.get(jid)   # the interrupt event finishes it
        if job["status"] == "postprocessing":
            raise FarmError("The job already finished on the GPU and is collecting outputs.", 409)
        raise FarmError(f"Job is already {job['status']}.", 409)

    async def _cancel_set(self, job: dict) -> dict:
        """Cancel every batch that hasn't finished; batches collecting outputs finish."""
        if job["status"] not in ACTIVE:
            raise FarmError(f"Job is already {job['status']}.", 409)
        self.store.update(job["id"], cancel_requested=1)
        for b in self.store.list(parent=job["id"], statuses=ACTIVE, limit=100000):
            if b["status"] == "postprocessing":
                continue
            try:
                await self.cancel(b["id"])
            except FarmError:
                pass
        return self._refresh_set(job["id"]) or self.store.get(job["id"])

    def _finish_cancel(self, job_id: str) -> dict | None:
        j = self.store.transition(job_id, ON_WORKER + ("queued",), status="cancelled",
                                  stage="cancelled", finished_at=now())
        if j:
            self._forget(j)
            S.cleanup(j, self.scratch_root)
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
            self._forget(j)
            if not self.cfg.convert.keep_failed:
                S.cleanup(j, self.scratch_root)
        self._emit(j)
        self.wake()
        return j

    # ================================================================ input conversion
    def _clean_orphan_scratch(self) -> None:
        """Converted frames of jobs that are gone (finished while the gateway was down)."""
        active = {j["id"] for j in self.store.list(statuses=ACTIVE, limit=100000, top_level=False)}

        def keep(name: str) -> bool:
            if name in active:
                return True
            job = self.store.get(name)
            return bool(self.cfg.convert.keep_failed and job and job["status"] == "failed")

        S.clean_orphans(self.scratch_root, keep)

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
                waiting = [j for j in reversed(self.store.list(statuses=("preparing",), limit=10000,
                                                               top_level=False, work_only=True))
                           if j["id"] not in self._converting]
                # A set's batches are converted only a few ahead of the GPU, so other jobs'
                # inputs get converted in between and scratch space stays small.
                ahead = self.store.queued_batches()
                ahead.update(p for p in self._converting_set.values())
                for job in waiting:
                    if len(self._converting) >= max(1, self.cfg.convert.max_parallel_jobs):
                        break
                    pid = job.get("parent_id")
                    if pid and ahead[pid] >= max(1, self.cfg.convert.batches_ahead):
                        continue
                    if pid:
                        ahead[pid] += 1
                        self._converting_set[job["id"]] = pid
                    task = asyncio.create_task(self._convert_job(job["id"]), name=f"convert-{job['id'][:8]}")
                    self._converting[job["id"]] = task
                    task.add_done_callback(lambda _t, jid=job["id"]: (self._converting.pop(jid, None),
                                                                      self._converting_set.pop(jid, None)))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("convert loop error")

    async def _convert_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if not job or job["status"] != "preparing":
            return
        seq_info = (job["info"] or {}).get("input") or {}
        conv = seq_info.get("convert", {})
        kind = conv.get("kind", "?")
        to = conv.get("to") or "PNG"
        at = f" at {conv['work_resolution']} px" if conv.get("scaled") else ""
        label = {"exr": f"converting EXR to {to}{at}", "movie": "extracting movie frames",
                 "image": f"scaling the input to {conv.get('work_resolution')} px"}.get(kind, kind)
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
            S.cleanup(self.store.get(job_id) or {}, self.scratch_root)

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
            job = self.store.get(job["id"], with_prompt=True)
            if job is None or job["status"] != "queued":
                continue
            if await w.dispatch(job) is None:
                return   # worker trouble; try again on the next tick

    async def check_workers(self) -> dict:
        for w in self.workers.values():
            if w.online:
                await w.refresh_object_info()
        return {w.name: w.summary() for w in self.workers.values()}

    # ================================================================ host for the workers
    # protocol.JobHost: what a WorkerRunner reads and reports through.
    def job(self, job_id: str, with_prompt: bool = False) -> dict | None:
        return self.store.get(job_id, with_prompt=with_prompt)

    def job_by_prompt(self, prompt_id: str) -> dict | None:
        return self.store.by_prompt_id(prompt_id)

    def jobs_on_worker(self, worker: str, statuses: tuple = ON_WORKER) -> list[dict]:
        return self.store.on_worker(worker, statuses)

    def inflight(self, worker: str) -> int:
        return self.store.count_on_worker(worker)

    def transition(self, job_id: str, from_statuses: tuple, **fields) -> dict | None:
        return self.store.transition(job_id, from_statuses, **fields)

    def emit(self, job: dict | None) -> None:
        self._emit(job)

    def fail(self, job_id: str, message: str, from_statuses: tuple = ACTIVE,
             traceback: str | None = None) -> dict | None:
        return self._fail(job_id, message, from_statuses, traceback=traceback)

    def finish_cancel(self, job_id: str) -> dict | None:
        return self._finish_cancel(job_id)

    def lost(self, job: dict, reason: str) -> None:
        """A worker lost a job (its ComfyUI restarted, or it was unreachable too long):
        queue it again, or give up after farm.max_attempts."""
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

    def record_run(self, workflow: str, seconds: float, node_count: int | None) -> None:
        self.store.record_run(workflow, seconds, node_count)

    def worker_changed(self, worker: WorkerRunner) -> None:
        self.events.publish({"type": "worker", "worker": worker.summary()})

    def workflow(self, name: str) -> Workflow | None:
        return self.registry.workflows.get(name)

    def workflow_stats(self, name: str) -> dict:
        return self.store.stats(name)

    def workflow_problems(self, object_info: dict) -> dict[str, list[str]]:
        problems = {}
        for name, wf in self.registry.workflows.items():
            probs = wf.check_object_info(object_info)
            if probs:
                problems[name] = probs
        return problems

    def _forget(self, job: dict) -> None:
        """A job that ended here (failed, cancelled): its worker stops following it."""
        w = self.workers.get(job.get("worker") or "")
        if w is not None and job.get("prompt_id"):
            w.forget(job["prompt_id"])


__all__ = ["Farm", "FarmError", "ParamError"]
