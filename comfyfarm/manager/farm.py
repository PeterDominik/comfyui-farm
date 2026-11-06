"""The manager's side of the farm: submitting and planning jobs (validated inputs,
versioned output folders), texture sets, the queue and its order, cancels and retries.

Running jobs is the agents' work: one per ComfyUI, in this process (standalone) or on
other machines. They take jobs from the queue through the manager's desk (manager.desk,
protocol.py), convert the inputs, run them on their ComfyUI, collect the outputs and
report back; the Farm applies what they report to the jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from typing import TYPE_CHECKING

from ..config import Config
from ..fileio import inputs as I
from ..fileio import naming as N
from ..fileio import ocio as OC
from ..fileio import paths as P
from ..fileio import scratch as S
from ..fileio import textures as T
from ..fileio.errors import SequenceError
from ..protocol import ACTIVE, FINISHED, HELD, ON_WORKER, now
from ..workflows import ParamError, WorkflowRegistry
from ..publish.publisher import Publisher, summary as publish_summary
from . import eta as E
from .db import JobStore
from .events import EventBus

if TYPE_CHECKING:
    from .desk import Desk

log = logging.getLogger("comfyfarm")


class FarmError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Farm:
    """Job queue, sets, cancels; the agents' reports, applied (through manager.desk)."""

    def __init__(self, cfg: Config, store: JobStore, registry: WorkflowRegistry, events: EventBus):
        self.cfg = cfg
        self.store = store
        self.registry = registry
        self.events = events
        self.desk: Desk | None = None     # where agents take jobs and report (set by Desk)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._submit_lock = threading.Lock()
        self._set_seen: dict[str, tuple] = {}             # set id -> last state published
        # Where inputs are converted, as the jobs are planned. It's sent to the agents as
        # @scratch/ (fileio.wire), so each converts into its own scratch folder.
        self.scratch_root = P.normalize(str(cfg.resolve(cfg.convert.scratch_dir)))
        # Publishing finished jobs through the studio's pipeline (comfyfarm.publish).
        self.publisher = Publisher(self)

    # ================================================================ lifecycle
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.events.bind(self.loop)
        # Jobs waiting for the manager's own conversion (before agents converted their
        # inputs) go back in the queue: an agent converts them.
        for j in self.store.list(statuses=("preparing",), limit=10000, top_level=False, work_only=True):
            if not j["worker"]:
                self.store.transition(j["id"], ("preparing",), status="queued", stage="queued")
        for s_ in self.store.list(statuses=ACTIVE, limit=10000):
            if s_["kind"] == "set":
                self._refresh_set(s_["id"])
        if self.desk:
            await self.desk.start()
        await self.publisher.start()
        log.info("manager started")

    async def stop(self) -> None:
        await self.publisher.stop()
        if self.desk:
            await self.desk.stop()

    def wake(self) -> None:
        """Something changed that an agent may be waiting for: a job to take, a cancel.
        Safe from any thread."""
        if self.desk:
            self.desk.wake()

    def online_workers(self) -> int:
        return self.desk.online_count() if self.desk else 0

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
        """Seconds left for a set: its unfinished batches at the pace of its finished ones
        (when the forecast doesn't know)."""
        rows = self.store.batch_states(set_id)
        took = [r["finished_at"] - (r["started_at"] or r["dispatched_at"]) for r in rows
                if r["status"] == "done" and r["finished_at"] and (r["started_at"] or r["dispatched_at"])]
        if not took:
            return None
        avg = sum(took) / len(took)
        left = sum(1 - (r["progress"] or 0) for r in rows if r["status"] in ACTIVE)
        return avg * left / max(1, self.online_workers())

    def annotate(self, jobs: list[dict]) -> list[dict]:
        """Add queue_position, eta_seconds (until it's done) and, for queued jobs,
        next_worker (the machine expected to take it). Times come from past runs of each
        workflow on each GPU type, for the job's size (desk.forecast)."""
        fc = self.desk.forecast() if self.desk else {}
        position = {j["id"]: i + 1 for i, j in enumerate(self.store.queued(with_prompt=False))}
        published = self.store.latest_publishes([j["id"] for j in jobs if j["status"] == "done"])
        for j in jobs:
            j["publish"] = publish_summary(published.get(j["id"]))     # the latest publish of it
            j["queue_position"] = position.get(j["id"])
            j["next_worker"] = None
            j["eta_seconds"] = None
            if j["status"] not in ACTIVE:
                continue
            if j.get("kind") == "set":
                left = [fc.get(r["id"], {}).get("done_in") for r in self.store.batch_states(j["id"])
                        if r["status"] in ACTIVE]
                eta = max(left) if left and None not in left else self.set_eta(j["id"])
            else:
                f = fc.get(j["id"]) or {}
                eta = f.get("done_in")
                if j["status"] == "queued":
                    j["next_worker"] = f.get("worker")
            j["eta_seconds"] = round(eta) if eta is not None else None
        return jobs

    def plan_eta(self, plan: dict) -> dict | None:
        """For the plan line: about how long the job will take, where, and when it'll be
        done behind the queue."""
        if not self.desk:
            return None
        wf = plan["wf"]
        if plan.get("batches"):
            jobs = [{"id": f"plan-{i}", "workflow": wf.name, "info": {"batch": {"tiles": b - a}}}
                    for i, (a, b) in enumerate(plan["batches"])]
        else:
            seq = plan["seq"]
            size = len(plan["textures"]["items"]) if plan.get("textures") else (seq.frame_count if seq else 1)
            jobs = [{"id": "plan-0", "workflow": wf.name, "info": {"expected_frames": size or 1}}]
        fc = self.desk.forecast(extra=jobs)
        got = [fc.get(j["id"]) or {} for j in jobs]
        if not got or any(g.get("done_in") is None for g in got):
            return None
        runs = [g["run"] for g in got if g.get("run") is not None]
        return {"done_in": round(max(g["done_in"] for g in got)), "run": round(sum(runs)) if runs else None,
                "worker": got[0].get("worker"), "workers": sorted({g["worker"] for g in got if g.get("worker")})}

    def run_times(self) -> list[dict]:
        """How long each workflow takes on each GPU type, from its recent runs (the
        Machines tab)."""
        out = [{"workflow": wf, "label": (self.registry.workflows[wf].label if wf in self.registry.workflows else wf),
                "gpu": gpu, **E.describe(row)} for (wf, gpu), row in self.store.run_stats().items()]
        return sorted(out, key=lambda r: (r["label"].lower(), r["gpu"]))

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
                              work_resolution=wf.work_resolution, preview=job_id == "preview",
                              listing=wf.loader.lists_folder) \
            if wf.sequence_param else None
        if seq is not None and wf.port.takes == "image":
            I.one_image(seq, pc)        # one frame of a sequence or movie, named by the artist
        elif seq is not None:
            I.frame_limit(seq, wf.port.max_frames)
        if seq is not None and seq.convert:
            S.point_loader_at_scratch(seq, self.scratch_root, job_id)
        others = self._plan_other_inputs(wf, values, seq, show, shot, job_id) if seq is not None else {}
        show_ = seq.show if seq else show
        shot_ = seq.shot if seq else shot
        seq_name = seq.seq_name if seq else None
        source = seq.source if seq else wf.tag
        plan = {"notes": list((seq.convert or {}).get("notes", [])) if seq else [],
                "wf": wf, "values": values, "seq": seq, "inputs": others,
                "show": show_, "shot": shot_, "seq_name": seq_name,
                "source": source, "out_dir": None, "version": None, "picked": None,
                "frames_prefix": f"_comfyfarm_{job_id[:8]}_raw",
                "preview_prefix": f"comfyfarm/{job_id[:8]}_{source}_{wf.tag}"}
        # No machine may take it now (requires, drained, offline): shown before submitting
        # and as a queued job's stage, but not kept as a warning (it passes).
        plan["waits"] = self.desk.requires_note(wf.name) if self.desk else None
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

    def _plan_other_inputs(self, wf, values: dict, main, show, shot, job_id: str) -> dict:
        """The inputs besides the primary, resolved: {name: Sequence, or None when left
        empty}. One that matches the primary is read at its frames: a matte given as a
        pattern alone gets the plate's range, and a matte with other frames is refused.
        Each converts into its own scratch folder, <scratch>/<job id>/<input>/."""
        pc, out = self.cfg.paths, {}
        main_label = wf.params[wf.port.name].label or wf.port.name
        for port in wf.other_ports:
            raw = values.get(port.name)
            if not raw:
                out[port.name] = None
                continue
            label = wf.params[port.name].label or port.name
            path, first, _ = P.parse_input(raw)
            if port.match and first is None and main.first is not None:
                raw = f"{path} {main.first}" + (f"-{main.last}" if main.last != main.first else "")
            try:
                seq = I.resolve_input(raw, pc, self.cfg.convert, show=show, shot=shot, ocio=self.cfg.ocio,
                                      work_resolution=port.work_resolution or wf.manifest_work_resolution,
                                      preview=job_id == "preview", listing=port.adapter.lists_folder)
                if port.takes != "image":
                    I.frame_limit(seq, port.max_frames)
            except SequenceError as e:
                raise SequenceError(f"{label}: {e}") from None
            if port.takes == "image":
                I.one_image(seq, pc, who=label)
            if port.match:
                want, got = I.frame_numbers(main), I.frame_numbers(seq)
                if want != got:
                    raise SequenceError(f"{label} has frames {P.frames_text(got or [])}, and {main_label} "
                                        f"{P.frames_text(want or [])}: they must be the same frames.")
            if seq.convert:
                S.point_loader_at_scratch(seq, self.scratch_root, f"{job_id}/{port.name}")
            out[port.name] = seq
        return out

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
        # More tiles than fit in one run (textures.batch), or tiles of different sizes (one
        # run's images are one batch, all one size): a set of batches, filling one folder.
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
            "picked": plan["picked"], "version": plan["version"],
            "notes": plan["notes"] + ([plan["waits"]] if plan.get("waits") else []),
            "eta": self.plan_eta(plan),
            "input_kind": seq.kind if seq else None, "convert": I.summary(seq, pc),
            "first_file": plan.get("first_file"), "last_file": plan.get("last_file"),
            "what": plan.get("what"), "asset": plan.get("asset"),
            "maps": [m["channel"] for m in plan["textures"]["maps"]] if plan.get("textures") else None,
            "batches": len(plan["batches"]) if plan.get("batches") else None,
            "inputs": {n: ({"frames": x.frame_count, "first": x.first, "last": x.last, "kind": x.kind,
                            "convert": I.summary(x, pc)} if x else None)
                       for n, x in (plan.get("inputs") or {}).items()},
        }

    def submit(self, workflow: str, params: dict, user: str, name: str | None = None,
               priority: int = 0, show: str | None = None, shot: str | None = None,
               publish: dict | None = None) -> dict:
        """Validate and queue a job. Runs in a thread (touches network storage). `publish`:
        publish it when it's done ({show, shot, step, template, description}, checked now)."""
        job_id = str(uuid.uuid4())
        with self._submit_lock:   # output folders are reserved by inserting the job
            plan = self.plan(workflow, params, show=show, shot=shot, job_id=job_id)
            wf, values, seq, out_dir = plan["wf"], plan["values"], plan["seq"], plan["out_dir"]
            pub = None
            if publish is not None:
                if plan.get("batches"):
                    raise ParamError("Texture sets aren't published from the farm.")
                pub = {**self.publisher.check(wf.name, publish, show=plan["show"], shot=plan["shot"]), "user": user}
                if publish.get("dry_run"):
                    pub["dry_run"] = True       # dispatchGraph --dry-run: Daiquiri checks it, sends nothing
            if plan.get("batches"):
                job = self._insert_set(plan, job_id, user, name, priority)
                log.info("set %s queued by %s: %s %s, %d batches -> %s", job_id[:8], user, wf.name,
                         plan["source"], len(plan["batches"]), out_dir)
                self._emit(job)
                self.wake()
                return job
            prompt = wf.build(values, seq, out_dir, plan["frames_prefix"], plan["preview_prefix"],
                              textures=plan.get("textures"), inputs=plan["inputs"])
            info = self._info(plan, seq, plan.get("textures"), plan.get("what"))
            if pub:
                info["publish_request"] = pub
            job = self.store.insert({
                "id": job_id, "workflow": wf.name, "user": user,
                "name": name or plan["shot"] or plan["source"],
                "status": "queued",
                "priority": priority, "params": values, "info": info,
                "prompt": prompt, "output_dir": out_dir, "output_key": plan.get("output_key"),
                "created_at": now(), "stage": plan.get("waits") or "queued",
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
            # The other inputs, by name (None: left empty). The agent converts each.
            "inputs": {n: (x.as_dict() if x else None) for n, x in (plan.get("inputs") or {}).items()},
            "inputs_posix": {n: (P.to_posix(x.farm_path, pc.mappings) if x else None)
                             for n, x in (plan.get("inputs") or {}).items()},
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
            # What the agent collects when the run is done, from the manifest the prompt
            # was built with.
            "collect": {"frames": wf.outputs.get("frames"), "preview": wf.outputs.get("preview")},
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
                         "name": f"{name} · {label}", "status": "queued",
                         "priority": priority, "params": values, "info": binfo,
                         "prompt": wf.build(values, bseq, out_dir, plan["frames_prefix"], plan["preview_prefix"],
                                            textures=btex),
                         "output_dir": out_dir, "output_key": None, "created_at": t,
                         "stage": "queued", "warnings": []})
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
            self.store.transition(b["id"], ("failed", "cancelled"), status="queued", stage="queued", error=None,
                                  progress=0, worker=None, prompt_id=None, dispatched_at=None, started_at=None,
                                  finished_at=None, cancel_requested=0, outputs=None, lease_until=None)
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
        again = dict(workflow=old["workflow"], params=old["params"], user=user, name=old["name"],
                     priority=old["priority"], show=info.get("show"), shot=info.get("shot"))
        asked = {k: v for k, v in (info.get("publish_request") or {}).items() if k != "user"}
        if asked:                           # published when done, as the first run was
            try:
                return self.submit(**again, publish=asked)
            except ParamError as e:         # publishing has changed since (a template gone…): run it anyway
                log.warning("job %s run again without publishing it when done: %s", job_id[:8], e)
        return self.submit(**again)

    async def cancel(self, job_id: str) -> dict:
        job = self.store.get(job_id)
        if not job:
            raise FarmError("Job not found.", 404)
        jid = job["id"]
        if job["kind"] == "set":
            return await self._cancel_set(job)
        if job["status"] in ("queued", "preparing"):
            # Not in a ComfyUI yet, so it's cancelled now. An agent preparing it (converting
            # its input) is told to drop it, and anything it reports about it is ignored.
            j = self.store.transition(jid, ("queued", "preparing"), status="cancelled", stage="cancelled",
                                      finished_at=now(), cancel_requested=1)
            if j:
                self._emit(j)
                self.wake()
                return j
            job = self.store.get(jid)
        if job["status"] in ON_WORKER:
            if self.desk is None or not self.desk.alive(job["worker"]):
                return self._finish_cancel(jid)   # its agent is gone; told to drop it if it comes back
            # Its agent takes it out of ComfyUI and reports it cancelled.
            j = self.store.transition(jid, ON_WORKER, cancel_requested=1, stage="cancelling")
            self._emit(j)
            self.wake()
            return j or self.store.get(jid)
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

    def _finish_cancel(self, job_id: str, only_prompt: str | None = None) -> dict | None:
        j = self.store.transition(job_id, ("queued", "preparing", "dispatched", "running"), only_prompt=only_prompt,
                                  status="cancelled", stage="cancelled", finished_at=now())
        self._emit(j)
        self.wake()
        return j or self.store.get(job_id)

    def _fail(self, job_id: str, message: str, from_statuses: tuple = ACTIVE,
              traceback: str | None = None, only_prompt: str | None = None) -> dict | None:
        j = self.store.transition(job_id, from_statuses, only_prompt=only_prompt, status="failed", error=message,
                                  stage="failed", finished_at=now())
        if j:
            log.warning("job %s failed: %s", job_id[:8], message)
            if traceback:
                info = dict(j["info"] or {})
                info["traceback"] = traceback[-8000:]
                j = self.store.update(job_id, info=info)
        self._emit(j)
        self.wake()
        return j

    # ================================================================ what agents report
    def lost(self, job: dict, reason: str) -> None:
        """An agent lost a job's run (its ComfyUI restarted, the agent restarted or stopped
        checking in): queue it again, or give up after farm.max_attempts runs in ComfyUI.

        Only about the run the job is on now (job["prompt_id"] is the run's attempt id).
        The caller's view of the job can be stale: the job may have been queued again and
        taken by an agent since. Requeuing it then would run it twice, so a report about an
        earlier run is ignored."""
        pid = job.get("prompt_id")
        job = self.store.get(job["id"])
        if not job or job["status"] not in HELD or not pid or job["prompt_id"] != pid:
            return
        if job["cancel_requested"]:
            self._finish_cancel(job["id"], only_prompt=pid)
        elif job["attempts"] < self.cfg.farm.max_attempts:
            j = self.store.transition(job["id"], HELD, only_prompt=pid, status="queued", worker=None,
                                      prompt_id=None, progress=0, stage="requeued", started_at=None,
                                      dispatched_at=None, lease_until=None, warnings=job["warnings"] + [reason])
            if j is None:
                return
            log.warning("job %s requeued: %s", job["id"][:8], reason)
            self._emit(j)
            self.wake()
        else:
            self._fail(job["id"], f"{reason} Gave up after {job['attempts']} attempts.", HELD, only_prompt=pid)

    def record_run(self, workflow: str, seconds: float, node_count: int | None) -> None:
        self.store.record_run(workflow, seconds, node_count)


__all__ = ["Farm", "FarmError", "ParamError"]
