"""The agent side of the farm: a WorkerRunner per ComfyUI. It submits the jobs the
manager dispatches to it, follows them live over ComfyUI's websocket, checks its view
against ComfyUI's queue and history (after restarts and missed events), watches the
machine's health, and collects the outputs when a job finishes: frames renamed, texture
maps written as EXRs, the preview copied.

It reports everything through its host (protocol.JobHost). For now that's the manager's
Farm, in the same process."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

from ..config import Config, WorkerConfig
from ..fileio import outputs as O
from ..fileio import paths as P
from ..fileio import scratch as S
from ..protocol import ON_WORKER, JobHost, now
from .comfy import ComfyClient, ComfyRejected, describe_rejection

log = logging.getLogger("comfyfarm")


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


class WorkerRunner:
    """One ComfyUI: what's running on it, and everything that talks to it."""

    def __init__(self, cfg: WorkerConfig, farm_cfg: Config, host: JobHost):
        self.cfg = cfg               # this worker: name, url, max_inflight
        self.farm_cfg = farm_cfg
        self.host = host
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
        self.abandoned: set[str] = set()       # prompt ids we gave up on; cancel if seen again
        self.runtime: dict[str, Runtime] = {}  # prompt_id -> Runtime
        self.scratch_root = P.normalize(str(farm_cfg.resolve(farm_cfg.convert.scratch_dir)))
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()
        # The reconnect and the periodic check both reconcile; one at a time, so they can't
        # both act on the same lost job.
        self._reconcile_lock = asyncio.Lock()

    def summary(self) -> dict:
        gpu = None
        for dev in (self.stats or {}).get("devices", []) or []:
            gpu = {"name": dev.get("name"), "vram_total": dev.get("vram_total"),
                   "vram_free": dev.get("vram_free")}
            break
        current = self.host.jobs_on_worker(self.name, ("running",))
        return {
            "name": self.name, "url": self.cfg.url, "online": self.online,
            "connected": self.ws_connected, "enabled": self.cfg.enabled,
            "max_inflight": self.cfg.max_inflight, "inflight": self.host.inflight(self.name),
            "running_job": current[0]["id"] if current else None,
            "queue_remaining": self.queue_remaining, "last_seen": self.last_seen,
            "error": self.error, "gpu": gpu,
            "comfyui_version": ((self.stats or {}).get("system") or {}).get("comfyui_version"),
            "problems": self.problems,
        }

    # ================================================================ lifecycle
    def start(self) -> None:
        """Start following this ComfyUI: health checks, its event websocket, and the
        periodic cross-check against its queue and history."""
        self._tasks = [asyncio.create_task(self._health_loop(), name=f"health-{self.name}"),
                       asyncio.create_task(self._ws_loop(), name=f"ws-{self.name}"),
                       asyncio.create_task(self._reconcile_loop(), name=f"reconcile-{self.name}")]

    async def stop(self) -> None:
        for t in self._tasks + list(self._bg):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._bg, return_exceptions=True)
        await self.client.close()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def forget(self, prompt_id: str) -> None:
        """Stop following a prompt whose job ended on the manager's side."""
        self.runtime.pop(prompt_id, None)

    # ================================================================ dispatch / cancel
    async def dispatch(self, job: dict) -> bool | None:
        info = job["info"] or {}
        titles = {nid: (n.get("_meta") or {}).get("title") or n.get("class_type")
                  for nid, n in job["prompt"].items()}
        out_dir = info.get("output_dir")
        if out_dir:
            try:
                await asyncio.to_thread(os.makedirs, out_dir, exist_ok=True)
            except OSError as e:
                self.host.fail(job["id"], f"Can't create the output folder {out_dir}: {e.strerror or e}",
                               ("queued",))
                return False
        tex = info.get("textures")
        if tex:
            # Empty, so a second attempt can't mix its map images with the first one's.
            try:
                await asyncio.to_thread(shutil.rmtree, tex["raw_dir"], True)
                await asyncio.to_thread(os.makedirs, tex["raw_dir"], exist_ok=True)
            except OSError as e:
                self.host.fail(job["id"], f"Can't create the scratch folder {tex['raw_dir']}: {e.strerror or e}",
                               ("queued",))
                return False
        pid = job["id"] if job["attempts"] == 0 else str(uuid.uuid4())
        extra = {"comfyfarm_job": job["id"], "comfyfarm_user": job["user"]}
        try:
            pid, node_errors = await self.client.submit(job["prompt"], pid, self.client_id, extra)
        except ComfyRejected as e:
            self.host.fail(job["id"], "ComfyUI rejected the job: " + describe_rejection(e.body, titles),
                           ("queued",))
            return False
        except httpx.HTTPError as e:
            # The request may have landed even though we got no answer; our own prompt id
            # lets us check instead of risking a duplicate run.
            try:
                running, pending = await self.client.queue()
                if pid not in running and pid not in pending and not await self.client.history(pid):
                    raise LookupError
                node_errors = {}
            except Exception:  # noqa: BLE001
                log.warning("submit to %s failed: %s", self.name, e)
                return None
        if node_errors:
            try:
                await self.client.delete_queued([pid])
            except httpx.HTTPError:
                self.abandoned.add(pid)
            body = {"error": {"message": "Some outputs failed validation"}, "node_errors": node_errors}
            self.host.fail(job["id"], "ComfyUI rejected the job: " + describe_rejection(body, titles), ("queued",))
            return False
        j = self.host.transition(job["id"], ("queued",), status="dispatched", worker=self.name, prompt_id=pid,
                                 dispatched_at=now(), stage="waiting for GPU", attempts=job["attempts"] + 1)
        if j is None:   # cancelled while we were submitting
            try:
                await self.client.delete_queued([pid])
            except httpx.HTTPError:
                self.abandoned.add(pid)
            return False
        self._new_runtime(j, pid, titles)
        log.info("job %s -> %s (prompt %s)", j["id"][:8], self.name, pid[:8])
        self.host.emit(j)
        return True

    async def cancel(self, job_id: str, prompt_id: str) -> bool:
        """Take a job out of ComfyUI: deleted from its queue if waiting, interrupted if
        running. True if it's gone without having run (the caller finishes the cancel);
        otherwise ComfyUI's interrupt event finishes it."""
        pid = prompt_id
        try:
            running, pending = await self.client.queue()
            if pid in pending:
                await self.client.delete_queued([pid])
                running, pending = await self.client.queue()
            if pid in running:
                # Only interrupt what is actually running: older ComfyUI builds ignore
                # prompt_id and would stop whichever job is on the GPU.
                await self.client.interrupt(pid)
            elif pid not in pending and not await self.client.history(pid):
                return True   # it was only waiting; now gone
        except httpx.HTTPError as e:
            log.warning("cancel %s: worker %s unreachable (%s); will cancel when it returns",
                        job_id[:8], self.name, e)
            self.abandoned.add(pid)
        return False

    def _new_runtime(self, job: dict, pid: str, titles: dict | None = None) -> Runtime:
        wf = self.host.workflow(job["workflow"])
        stats = self.host.workflow_stats(job["workflow"])
        expected = stats.get("node_count") or (wf.expected_nodes(self.output_classes) if wf else 20)
        if titles is None:
            prompt = (self.host.job(job["id"], with_prompt=True) or {}).get("prompt") or {}
            titles = {nid: (n.get("_meta") or {}).get("title") or n.get("class_type") for nid, n in prompt.items()}
        rt = Runtime(job_id=job["id"], expected=expected, titles=titles)
        self.runtime[pid] = rt
        return rt

    # ================================================================ monitoring
    async def _ws_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.client.ws_url(self.client_id), max_size=None,
                                              open_timeout=10, ping_interval=20, ping_timeout=30) as ws:
                    self.ws_connected = True
                    backoff = 1.0
                    log.info("connected to %s events", self.name)
                    # A reconnect usually means ComfyUI restarted, maybe after node packs
                    # or models changed: re-run the preflight before dispatching again.
                    self.checked = False
                    await self.refresh_object_info()
                    self.host.worker_changed(self)
                    await self._reconcile()
                    self.host.wake()
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            continue   # live preview images
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        try:
                            await self._on_message(msg)
                        except Exception:  # noqa: BLE001
                            log.exception("error handling %s event", msg.get("type"))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("ws %s: %s", self.name, e)
            if self.ws_connected:
                self.ws_connected = False
                log.warning("lost event connection to %s", self.name)
                self.host.worker_changed(self)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _runtime_for(self, pid: str) -> tuple[dict | None, Runtime | None]:
        rt = self.runtime.get(pid)
        job = self.host.job(rt.job_id) if rt else self.host.job_by_prompt(pid)
        if job is None:
            return None, None
        if rt is None and job["status"] in ON_WORKER:
            rt = self._new_runtime(job, pid)
        return job, rt

    def _push_progress(self, job: dict, rt: Runtime, stage: str | None = None, force: bool = False) -> None:
        t = now()
        if not force and t - rt.last_push < 0.5:
            return
        rt.last_push = t
        fields = {"progress": round(rt.progress(), 4)}
        if stage:
            fields["stage"] = stage
        self.host.emit(self.host.transition(job["id"], ON_WORKER, **fields))

    async def _on_message(self, msg: dict) -> None:
        kind, data = msg.get("type"), msg.get("data") or {}
        if kind == "status":
            self.queue_remaining = ((data.get("status") or {}).get("exec_info") or {}).get("queue_remaining")
            return
        pid = data.get("prompt_id")
        if not pid:
            return
        if pid in self.abandoned:
            return
        job, rt = self._runtime_for(pid)
        if job is None:
            return
        jid = job["id"]

        if kind == "execution_start":
            j = self.host.transition(jid, ("dispatched",), status="running", started_at=now(), stage="starting")
            self.host.emit(j)
        elif kind == "execution_cached" and rt:
            rt.done.update(data.get("nodes") or [])
            self._push_progress(job, rt, "using cached results")
        elif kind == "executing" and rt:
            node = data.get("node")
            if job["status"] == "dispatched":
                job = self.host.transition(jid, ("dispatched",), status="running", started_at=now()) or job
            if rt.current and rt.current != node:
                rt.done.add(rt.current)
            rt.current, rt.frac = node, 0.0
            if node is None:   # older ComfyUI signals completion this way
                self._spawn(self._finalize(jid, pid))
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
            self._spawn(self._finalize(jid, pid))
        elif kind == "execution_error":
            title = rt.titles.get(str(data.get("node_id")), "") if rt else ""
            msg_ = (f"{data.get('node_type')} (node {data.get('node_id')}"
                    f"{' ' + repr(title) if title else ''}): {str(data.get('exception_message', '')).strip()}")
            self.host.fail(jid, msg_, ON_WORKER, traceback="".join(data.get("traceback") or []) or None)
            self._spawn(self._drop_history(pid))
        elif kind == "execution_interrupted":
            if job["cancel_requested"]:
                self.host.finish_cancel(jid)
            else:
                self.host.fail(jid, "Interrupted on the farm machine.", ON_WORKER)
            self._spawn(self._drop_history(pid))

    async def _drop_history(self, pid: str) -> None:
        if not self.farm_cfg.farm.delete_comfy_history:
            return
        try:
            await self.client.delete_history([pid])
        except httpx.HTTPError:
            pass

    # ================================================================ reconcile
    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.farm_cfg.farm.reconcile_seconds)
            if not (self.online and self.ws_connected):
                continue
            try:
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("reconcile %s failed", self.name)

    async def _reconcile(self) -> None:
        """Correct our view from ComfyUI's queue and history: catches events missed while
        disconnected, and jobs ComfyUI lost because it restarted."""
        async with self._reconcile_lock:
            await self._reconcile_once()

    async def _reconcile_once(self) -> None:
        t0 = now()
        jobs = [j for j in self.host.jobs_on_worker(self.name) if (j["dispatched_at"] or 0) < t0 - 2]
        if not jobs and not self.abandoned:
            return
        running, pending = await self.client.queue()
        for pid in list(self.abandoned):
            try:
                if pid in pending:
                    await self.client.delete_queued([pid])
                elif pid in running:
                    await self.client.interrupt(pid)
            except httpx.HTTPError:
                continue
            if pid not in running:
                self.abandoned.discard(pid)
        for j in jobs:
            pid = j["prompt_id"]
            if pid in running:
                if j["status"] == "dispatched":
                    self.host.emit(self.host.transition(j["id"], ("dispatched",), status="running",
                                                        started_at=now()))
                if pid not in self.runtime:
                    self._new_runtime(j, pid)
                continue
            if pid in pending:
                continue
            hist = await self.client.history(pid)
            if hist:
                status = hist.get("status") or {}
                if status.get("status_str") == "success" or (status.get("completed") and status.get("status_str") != "error"):
                    self._spawn(self._finalize(j["id"], pid, hist))
                else:
                    message, interrupted, tb = _history_failure(hist)
                    if interrupted and j["cancel_requested"]:
                        self.host.finish_cancel(j["id"])
                    else:
                        self.host.fail(j["id"], message, ON_WORKER, traceback=tb)
                    self._spawn(self._drop_history(pid))
                continue
            await self._lost(j, f"ComfyUI on {self.name} lost the job (restarted?).")

    async def _lost(self, job: dict, reason: str) -> None:
        """ComfyUI lost a job (restarted, or this machine was unreachable too long): stop
        following it, cancel it if it ever shows up again, and let the manager requeue it."""
        if job.get("prompt_id"):
            self.runtime.pop(job["prompt_id"], None)
            self.abandoned.add(job["prompt_id"])
        self.host.lost(job, reason)

    # ================================================================ health
    async def _health_loop(self) -> None:
        fails = 0
        while True:
            try:
                self.stats = await self.client.system_stats()
                self.last_seen = now()
                fails = 0
                if not self.online:
                    self.online, self.error = True, None
                    log.info("worker %s online", self.name)
                    self.host.worker_changed(self)
                    self.host.wake()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                if self.online and fails >= 2:
                    self.online, self.offline_since = False, now()
                    self.error = str(e) or type(e).__name__
                    log.warning("worker %s offline: %s", self.name, self.error)
                    self.host.worker_changed(self)
                if not self.online and now() - self.offline_since > self.farm_cfg.farm.lost_after_seconds:
                    for j in self.host.jobs_on_worker(self.name):
                        await self._lost(j, f"Worker {self.name} was unreachable for over "
                                           f"{self.farm_cfg.farm.lost_after_seconds / 60:.0f} minutes.")
            await asyncio.sleep(self.farm_cfg.farm.health_seconds)

    async def refresh_object_info(self) -> None:
        try:
            oi = await self.client.object_info()
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't read node definitions from %s: %s", self.name, e)
            self.checked = True
            return
        self.output_classes = {k for k, v in oi.items() if isinstance(v, dict) and v.get("output_node")}
        self.problems = self.host.workflow_problems(oi)
        for name, probs in self.problems.items():
            for p in probs:
                log.error("worker %s / workflow %s: %s", self.name, name, p)
        self.checked = True

    # ================================================================ outputs
    async def _finalize(self, job_id: str, pid: str, hist: dict | None = None) -> None:
        j = self.host.transition(job_id, ON_WORKER, status="postprocessing",
                                 stage="collecting outputs", progress=0.99)
        if j is None:
            return   # already handled (duplicate event, cancel, reconcile)
        rt = self.runtime.pop(pid, None)
        self.host.emit(j)
        try:
            if hist is None:
                for _ in range(10):   # history is written just after the success event
                    hist = await self.client.history(pid)
                    if hist:
                        break
                    await asyncio.sleep(0.5)
            outputs, warnings = await self._collect_outputs(j, hist or {})
            t = now()
            done = self.host.transition(job_id, ("postprocessing",), status="done", progress=1.0,
                                        stage="done", finished_at=t, outputs=outputs,
                                        warnings=j["warnings"] + warnings)
            self.host.emit(done)
            S.cleanup(j, self.scratch_root)
            started = j["started_at"] or j["dispatched_at"] or t
            nodes = len(rt.done | ({rt.current} if rt.current else set())) if rt else None
            if j["kind"] != "batch":   # a set's ETA comes from its own batches
                self.host.record_run(j["workflow"], t - started, nodes or None)
            log.info("job %s done in %.0fs -> %s", job_id[:8], t - started, outputs.get("output_dir"))
        except Exception as e:  # noqa: BLE001
            log.exception("collecting outputs for %s failed", job_id[:8])
            self.host.fail(job_id, f"Finished on the GPU but collecting outputs failed: {e}", ("postprocessing",))
        finally:
            await self._drop_history(pid)
            self.host.wake()

    async def _collect_outputs(self, job: dict, hist: dict) -> tuple[dict, list[str]]:
        info = job["info"] or {}
        wf = self.host.workflow(job["workflow"])
        pc = self.farm_cfg.paths
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
                self.host.emit(self.host.transition(job["id"], ("postprocessing",),
                                                    stage=f"writing EXR maps {done}/{total}"))

            outputs.update(await O.write_maps(tex, out_dir, self.farm_cfg.textures, self.farm_cfg.convert,
                                              pc.mappings, progress))

        fr = wf.outputs.get("frames")
        if fr and out_dir:
            res = await asyncio.to_thread(O.rename_frames, info, fr.get("ext", "png"), pc.frame_name, pc.mappings)
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
                    await self.client.download(e["filename"], e.get("subfolder", ""), e.get("type", "output"), dest)
                    outputs["preview"] = P.normalize(str(dest))
                    outputs["preview_posix"] = P.to_posix(outputs["preview"], pc.mappings)
                except Exception as ex:  # noqa: BLE001
                    warnings.append(f"Couldn't copy the preview video: {ex}")
            elif not entries:
                warnings.append("ComfyUI returned no preview video.")
        return outputs, warnings


__all__ = ["WorkerRunner", "Runtime"]
