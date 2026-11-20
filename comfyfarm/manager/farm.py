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
from ..fileio import preview as PV
from ..fileio import scratch as S
from ..fileio import textures as T
from ..fileio.errors import SequenceError
from ..protocol import ACTIVE, FINISHED, HELD, ON_WORKER, now
from ..workflows import ParamError, WorkflowRegistry
from ..publish.publisher import Publisher, PublishError, show_folder, summary as publish_summary
from ..shotgrid import ShotGrid
from ..shotgrid import menu as SM
from ..shotgrid import query as SQ
from ..shotgrid import versions as V
from ..shotgrid.lookup import NotFound, Refused, Unavailable
from . import eta as E
from .db import JobStore
from .events import EventBus

if TYPE_CHECKING:
    from .desk import Desk

log = logging.getLogger("comfyfarm")


def publish_target_ok(cfg, show: str) -> bool:
    """Whether a show can be published to: it has its folder (publish.show_folder)."""
    folder = show_folder(cfg, show)
    return folder is None or folder.is_dir()


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
        # Read-only lookups for the publish fields: only shows that can be published to.
        self.shotgrid = ShotGrid(cfg.shotgrid, keep=lambda code: publish_target_ok(cfg.publish, code))
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

    def submitted(self, job: dict) -> dict:
        """What the job's last run handed its backend, for a person to look at (the
        dashboard's API JSON): kept as the machine that ran it reported it, never read here."""
        if job.get("prompt") is None:
            raise FarmError("This job's API JSON is there once a machine has started it: the machine that runs "
                            "a job builds it." if job["kind"] != "set" else
                            "A set has no API JSON of its own: each of its batches has one.", 409)
        return job["prompt"]

    def timeline(self, job_id: str) -> dict:
        """What happened to a job and when, oldest first, with how long each step lasted
        (the last one up to now, while the job is still active)."""
        job = self.store.get(job_id)
        if not job:
            raise FarmError("Job not found.", 404)
        events = self.store.events(job["id"])
        end = now() if job["status"] in ACTIVE else None
        for e, nxt in zip(events, events[1:] + [None]):
            until = nxt["at"] if nxt else end
            e["seconds"] = round(max(until - e["at"], 0.0), 3) if until is not None else None
        return {"job": job["id"], "status": job["status"], "events": events}

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
        """How long each workflow takes on each GPU type, from its recent runs (the Run
        times tab). `order`: the order free GPUs of each type get the workflow's jobs in,
        fastest first for a job of its usual size (machines whose GPU type hasn't run it
        come after these)."""
        by_wf: dict[str, list[tuple[str, dict]]] = {}
        for (wf, gpu), row in self.store.run_stats().items():
            by_wf.setdefault(wf, []).append((gpu, row))
        out = []
        for wf, rows in by_wf.items():
            pooled = E.combine([row for _, row in rows])
            size = pooled["wx"] / pooled["w"] if pooled and pooled["wx"] else 1     # the workflow's usual size
            label = self.registry.workflows[wf].label if wf in self.registry.workflows else wf
            ranked = sorted(rows, key=lambda r: ((t := E.estimate(r[1], size)) is None, t or 0.0, r[0]))
            out += [{"workflow": wf, "label": label, "gpu": gpu, "order": i + 1, **E.describe(row)}
                    for i, (gpu, row) in enumerate(ranked)]
        return sorted(out, key=lambda r: (r["label"].lower(), r["order"]))

    # ================================================================ submit / cancel
    def plan(self, workflow: str, params: dict, show: str | None = None, shot: str | None = None,
             job_id: str = "preview") -> dict:
        """Work out everything about a job without queuing it: validated params, what the
        loader reads, and exactly where the frames will land and what they're called.
        Used by submit() and by the dashboard's live destination preview."""
        wf = self.registry.get(workflow)
        values = wf.validate(params or {})
        pc = self.cfg.paths
        # ShotGrid Version links among the inputs: read as the paths ShotGrid has for them
        # (`use`); the job keeps the links (values). The Version decides show and shot.
        use, sources, notes = self._version_inputs(wf, values)
        src = sources.get(wf.sequence_param) if wf.sequence_param else None
        if src:
            show, shot = show or src["show"], shot or src["shot"]
        try:
            seq = I.resolve_input(use[wf.sequence_param], pc, self.cfg.convert, show=show, shot=shot,
                                  texture=bool(wf.textures), ocio=self.cfg.ocio,
                                  work_resolution=wf.work_resolution, preview=job_id == "preview",
                                  listing=wf.port.reads_folder) \
                if wf.sequence_param else None
            if seq is not None and wf.port.takes == "image":
                I.one_image(seq, pc)        # one frame of a sequence or movie, named by the artist
            elif seq is not None:
                I.frame_limit(seq, wf.port.max_frames)
        except SequenceError as e:
            if src:
                raise SequenceError(f"ShotGrid Version {src['code']} ({src['kind']} {src['path']}): {e}") from None
            raise
        if seq is not None and seq.convert:
            S.point_loader_at_scratch(seq, self.scratch_root, job_id)
        others = self._plan_other_inputs(wf, use, seq, show, shot, job_id) if seq is not None else {}
        show_ = seq.show if seq else show
        shot_ = seq.shot if seq else shot
        seq_name = (src.get("sequence") if src else None) or (seq.seq_name if seq else None)
        source = seq.source if seq else wf.tag
        plan = {"notes": notes + (list((seq.convert or {}).get("notes", [])) if seq else []),
                "wf": wf, "values": values, "seq": seq, "inputs": others, "sources": sources,
                "show": show_, "shot": shot_, "seq_name": seq_name,
                "source": source, "out_dir": None, "version": None, "picked": None,
                "frames_prefix": f"_comfyfarm_{job_id[:8]}_raw",
                "preview_prefix": f"comfyfarm/{job_id[:8]}_{source}_{wf.tag}"}
        # No machine may take it now (requires, drained, offline): shown before submitting
        # and as a queued job's stage, but not kept as a warning (it passes).
        plan["waits"] = self.desk.requires_note(wf.name) if self.desk else None
        if wf.textures:
            return self._plan_textures(plan, job_id)
        if not wf.frames_ext:
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
                "ext": wf.frames_ext}
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

    def publish_preview(self, plan: dict, publish: dict) -> dict:
        """What publishing a job of this plan when it's done would be, checked as submit
        checks it but without queuing anything: {request} (completed: show and shot from
        the plan unless given, step, template, description, dry_run) or {why}. The plan
        endpoint's answer, so Batch Submit's rows say before they're sent."""
        if plan.get("batches"):
            return {"why": "Texture sets aren't published from the farm."}
        try:
            req = self.publisher.check(plan["wf"].name, publish, show=plan["show"], shot=plan["shot"])
        except (ParamError, PublishError) as e:
            return {"why": str(e)}
        return {"request": {**req, "dry_run": bool(publish.get("dry_run")) or self.cfg.publish.dry_run}}

    def batch_playlist(self, link: str) -> dict:
        """A ShotGrid Playlist as Batch Submit's rows: {playlist: {id, code, url, show},
        rows: [{version, input}]}, in the playlist's order. Each row's input is its
        Version's link, which the row checks (plan) and its job resolve as any Version link
        does; the Versions are cached by the lookup, so checking the rows doesn't ask
        ShotGrid again."""
        ref = V.parse_playlist(link)
        if not ref:
            raise FarmError("That's not a ShotGrid Playlist link (…/detail/Playlist/<id>) or a playlist id.")
        sgc = self.cfg.shotgrid
        if not V.same_site(ref, sgc.url):
            raise FarmError(f"That's a link to {ref['host']}, not to the farm's ShotGrid ({sgc.url}).")
        try:
            pl = self.shotgrid.playlist(ref["id"])
        except NotFound as e:
            raise FarmError(str(e), 404) from None
        except Refused as e:
            raise FarmError(f"ShotGrid couldn't give Playlist {ref['id']}'s Versions: {e}", 502) from None
        except Unavailable as e:
            raise FarmError(f"Playlists need the manager's ShotGrid lookups. {e}", 503) from None
        return {"playlist": {k: pl[k] for k in ("id", "code", "url", "show")},
                "rows": [{"version": v, "input": v["url"]} for v in pl["versions"]]}

    def batch_versions(self, ids) -> dict:
        """Versions picked in ShotGrid (its right-click on a selection: /#batch?versions=…)
        as Batch Submit's rows, in the order given: {source: {code, show, ids, missing},
        rows: [{version, input}]}, as batch_playlist."""
        try:
            ids = V.version_ids(ids)
        except ValueError as e:
            raise FarmError(str(e)) from None
        try:
            found = self.shotgrid.versions(ids)
        except Refused as e:
            raise FarmError(f"ShotGrid couldn't give those Versions: {e}", 502) from None
        except Unavailable as e:
            raise FarmError(f"Versions from ShotGrid need the manager's ShotGrid lookups. {e}", 503) from None
        if not found:
            raise FarmError(f"There's no Version {', '.join(map(str, ids))} in ShotGrid (or the farm's ShotGrid "
                            "script can't see them).", 404)
        shows = {v["show"] for v in found}
        have = {v["id"] for v in found}
        n = len(found)
        return {"source": {"code": f"{n} Version{'' if n == 1 else 's'} picked in ShotGrid",
                           "show": shows.pop() if len(shows) == 1 else None,
                           "ids": [v["id"] for v in found], "missing": [i for i in ids if i not in have]},
                "rows": [{"version": v, "input": v["url"]} for v in found]}

    def query_options(self, show: str, refresh: bool = False) -> dict:
        """What Batch Submit's ShotGrid query offers for a show (shotgrid.query.options)."""
        return SQ.options(self.shotgrid, show, refresh)

    def batch_query(self, spec: dict) -> dict:
        """Batch Submit's ShotGrid query (shotgrid.query): its Versions as rows, in shot and
        code order: {source: {code, show, query, matched, latest, more, notes}, rows:
        [{version, input}]}, as batch_playlist."""
        try:
            r = SQ.run(self.shotgrid, spec)
        except ValueError as e:
            raise FarmError(str(e)) from None
        except Refused as e:
            raise FarmError(f"ShotGrid refused the query: {e}", 502) from None
        except Unavailable as e:
            raise FarmError(f"The ShotGrid query needs the manager's ShotGrid lookups. {e}", 503) from None
        n, q = len(r["rows"]), r["query"]
        what = "latest Version" if q["latest"] else "Version"
        code = f"{n} {what}{'' if n == 1 else 's'} in {r['show']}" if n else f"No Version in {r['show']} matches"
        return {"source": {"code": code, "show": r["show"], "query": q, "matched": r["matched"], "latest": q["latest"],
                           "more": r["more"], "notes": r["notes"]},
                "rows": [{"version": v, "input": v["url"]} for v in r["rows"]]}

    def shotgrid_submit(self, workflow: str, link: str, user: str | None, publish: bool = False) -> dict:
        """ShotGrid's right-click Submit (+ Publish) on one Version (comfyfarm.shotgrid.menu):
        the job, with the workflow's defaults, as the ShotGrid user who clicked (else
        anonymous). FarmError when a Version alone can't run the workflow, or it isn't
        published; the job's own refusals as submit's (ParamError, SequenceError…)."""
        wf = self.registry.get(workflow)
        f = SM.fit(wf, self.publisher.publish_info(wf) is not None)
        if not f["submit"]:
            raise FarmError(f"{wf.label} can't be submitted from a Version alone: {f['why']}.")
        if publish and not f["publish"]:
            raise FarmError(self.publisher.not_published(wf) or f"{wf.label} isn't published: publishing isn't "
                            "switched on (publish.enabled in the manager's config.yaml).")
        return self.submit(wf.name, {wf.sequence_param: link}, (user or "").strip()[:64] or "anonymous",
                           publish={} if publish else None)

    def _version_inputs(self, wf, values: dict) -> tuple[dict, dict, list[str]]:
        """ShotGrid Version links among a job's inputs (shotgrid.versions): (the values with
        each link replaced by the path ShotGrid has for it and its frames, {param: the
        Version as the job records it}, notes). Its frames come first, over the Version's
        range unless frames follow the link; its movie when it has none, or none on disk."""
        ports = ([wf.port] if wf.port else []) + list(wf.other_ports)
        refs = {p.name: V.parse(values.get(p.name)) for p in ports}
        if not any(refs.values()):
            return values, {}, []
        pc, sgc = self.cfg.paths, self.cfg.shotgrid
        use, sources, notes = dict(values), {}, []
        for port in ports:
            ref = refs.get(port.name)
            if not ref:
                continue
            label = wf.params[port.name].label or port.name
            if not V.same_site(ref, sgc.url):
                raise SequenceError(f"{label}: that's a link to {ref['host']}, not to the farm's ShotGrid ({sgc.url}).")
            try:
                v = self.shotgrid.version(ref["id"])
            except Refused as e:
                raise SequenceError(f"{label}: ShotGrid couldn't give Version {ref['id']}'s paths: {e} "
                                    f"(shotgrid.version_fields in the manager's config.yaml)") from None
            except Unavailable as e:
                raise SequenceError(f"{label}: a ShotGrid Version link needs the manager's ShotGrid lookups. {e} "
                                    f"Paste the frames' path instead.") from None
            except NotFound as e:
                raise SequenceError(f"{label}: {e}") from None
            given = ref["first"] is not None
            # Its own range, unless frames were given, or the input takes the main input's frames.
            first, last = (ref["first"], ref["last"]) if given else \
                ((None, None) if port.match else (v["first"], v["last"]))
            kind, path, why = None, None, None
            if v["frames"]:
                kind, path = "frames", V.pad_hash(v["frames"], pc)
                if v["movie"] and pc.validate_inputs:        # not on disk (purged)? Then the movie.
                    try:
                        I.resolve_sequence(V.input_text(path, first, last), pc, listing=False)
                    except SequenceError as e:
                        kind, why = None, str(e)
            if kind is None:
                if not v["movie"]:
                    raise SequenceError(f"{label}: ShotGrid Version {v['code']} has no frames or movie path "
                                        f"({sgc.version_fields.get('frames')}, {sgc.version_fields.get('movie')}).")
                kind, path = "movie", v["movie"]
                first, last = (ref["first"], ref["last"]) if given else (None, None)   # the whole movie
                if why:
                    notes.append(f"{label}: the frames of ShotGrid Version {v['code']} can't be read ({why}), "
                                 f"so its movie is.")
            use[port.name] = V.input_text(path, first, last)
            sources[port.name] = {"id": v["id"], "code": v["code"], "url": v["url"], "kind": kind, "path": path,
                                  "first": first, "last": last, "status": v["status"],
                                  "entity_type": v["entity_type"], "entity": v["entity"], "show": v["show"],
                                  "sequence": v["sequence"], "shot": v["shot"]}
        return use, sources, notes

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
                                      preview=job_id == "preview", listing=port.reads_folder)
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
        maps = [{"name": m.name, "channel": m.channel, "colorspace": m.colorspace,
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
            "sources": plan.get("sources") or {},          # ShotGrid Versions given as inputs, by param
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
            # What the machine that runs it builds its graph from (decision 0013).
            request = wf.request(values, seq, out_dir, plan["frames_prefix"], plan["preview_prefix"],
                                 textures=plan.get("textures"), inputs=plan["inputs"])
            info = self._info(plan, seq, plan.get("textures"), plan.get("what"))
            if pub:
                info["publish_request"] = pub
            job = self.store.insert({
                "id": job_id, "workflow": wf.name, "user": user,
                "name": name or plan["shot"] or plan["source"],
                "status": "queued",
                "priority": priority, "params": values, "info": info,
                "prompt": None, "request": request, "output_dir": out_dir, "output_key": plan.get("output_key"),
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
            # ShotGrid Versions given as inputs (their links stay in params), by param
            "sources": plan.get("sources") or {},
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
            # What the agent collects when the run is done, and the preview movie it makes of
            # the frames (fileio.preview). No node ids: the agent's runner has its own copy.
            "collect": wf.collect(self._movie(wf, seq)),
        }

    def _movie(self, wf, seq) -> dict | None:
        """The preview movie's settings for a job (None: no movie): preview.*, at the
        input movie's frame rate when the input is one."""
        pv = self.cfg.preview
        if not pv.enabled or not wf.makes_preview:
            return None
        c = (seq.convert if seq else None) or {}
        fps = c.get("fps") if c.get("kind") == "movie" and c.get("fps") else pv.fps
        fps_str = c.get("fps_str") if c.get("kind") == "movie" and c.get("fps_str") else PV.rate_text(pv.fps)
        return {"fps": float(fps), "fps_str": fps_str, "max_width": pv.max_width, "crf": pv.crf}

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
                         "prompt": None,
                         "request": wf.request(values, bseq, out_dir, plan["frames_prefix"], plan["preview_prefix"],
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
