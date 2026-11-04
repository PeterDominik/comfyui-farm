"""An agent: runs the manager's jobs on one ComfyUI (protocol.py).

It checks in with the manager and takes jobs from it, as many as its ComfyUI runs at once
(max_inflight) plus one prepared ahead. For each job it:

1. converts the input (EXR, movie, images larger than the workflow's work_resolution)
   into its own scratch folder,
2. submits the prompt to ComfyUI, with the run's attempt id as the prompt id,
3. follows it over ComfyUI's websocket, and checks its queue and history (events
   missed, ComfyUI restarted),
4. collects the outputs: frames renamed, texture maps written as EXRs, the preview
   copied,
5. reports each step to the manager. A report that can't be delivered (the manager is
   restarting) waits and goes later, in order.

Jobs arrive with studio paths and @scratch/ / @repo/ (fileio.wire); the agent sees them
through its own paths.mappings, scratch folder and checkout. It keeps its jobs in memory:
after a restart the manager hands them back and the agent picks them up again from
ComfyUI."""

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

from ..config import AgentConfig, PathsConfig
from ..fileio import convert as C
from ..fileio import outputs as O
from ..fileio import paths as P
from ..fileio import scratch as S
from ..fileio import wire as W
from ..fileio.errors import ConvertError, SequenceError
from ..fileio.ocio import REPO
from ..protocol import ON_WORKER, TERMINAL, LinkError, build, duration, now
from ..workflows import fit_to_options
from .comfy import ComfyClient, ComfyRejected, describe_rejection
from .launch import Launcher

log = logging.getLogger("comfyfarm")
_DEFAULT_PATHS = PathsConfig()


@dataclass
class Runtime:
    """Live progress of one job in ComfyUI (not persisted)."""
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


def _titles(prompt: dict) -> dict:
    return {nid: (n.get("_meta") or {}).get("title") or n.get("class_type") for nid, n in (prompt or {}).items()}


@dataclass
class Held:
    """One run of a job this agent holds."""
    job: dict                       # the job as this machine sees it (wire.localize)
    attempt: str                    # the run's id, given by the manager
    status: str = "preparing"       # preparing | dispatched | running | postprocessing
    pid: str | None = None          # its prompt id in ComfyUI (the attempt id, unless an old ComfyUI minted its own)
    rt: Runtime | None = None
    task: asyncio.Task | None = None
    slot: bool = False              # has one of ComfyUI's max_inflight places
    submitting: bool = False
    cancelling: bool = False
    finished: bool = False          # its last report is on its way; forgotten once delivered
    dropped: bool = False           # the manager moved on: stop, report nothing
    dispatched_at: float | None = None
    started_at: float | None = None
    outbox: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def id(self) -> str:
        return self.job["job"]


class Agent:
    def __init__(self, cfg: AgentConfig, link, client: ComfyClient | None = None):
        self.cfg = cfg
        self.link = link
        self.name = cfg.name
        self.client = client or ComfyClient(cfg.comfyui)
        self.client_id = "comfyfarm-" + uuid.uuid5(uuid.NAMESPACE_URL, cfg.comfyui + cfg.name).hex[:12]
        self.launcher = Launcher(cfg.name, cfg.comfyui_launch, cfg.resolve(cfg.log_dir), cfg.base_dir)
        self.scratch_root = P.normalize(str(cfg.resolve(cfg.convert.scratch_dir)))
        self.view = W.View(mappings=cfg.paths.mappings, scratch=self.scratch_root, repo=P.normalize(str(REPO)))
        self.held: dict[str, Held] = {}          # job id -> its run
        # ComfyUI
        self.online = False
        self.ws_connected = False
        self.checked = False
        self.offline_since = now()
        self.stats: dict | None = None
        self.object_info: dict | None = None
        self.output_classes: set[str] | None = None
        self.problems: dict[str, list[str]] = {}
        self.queue_remaining: int | None = None
        self.error: str | None = None
        self.abandoned: set[str] = set()         # prompt ids given up on: cancelled if seen again
        # the manager
        self.registered = False
        self.manager_down = False
        self.lease_seconds: float | None = None
        self._seq = 0
        self._cleaned = False
        self._in_comfy = 0                       # runs holding one of ComfyUI's places
        self._slots: asyncio.Condition | None = None
        self._convert_sem: asyncio.Semaphore | None = None
        self._reconcile_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()

    # ================================================================ lifecycle
    def _ensure_sync(self) -> None:
        if self._slots is None:
            self._slots = asyncio.Condition()
            self._convert_sem = asyncio.Semaphore(max(1, self.cfg.convert.max_parallel_jobs))

    def start(self) -> None:
        self._ensure_sync()
        self._tasks = [asyncio.create_task(self._manager_loop(), name=f"manager-{self.name}"),
                       asyncio.create_task(self._health_loop(), name=f"health-{self.name}"),
                       asyncio.create_task(self._ws_loop(), name=f"ws-{self.name}"),
                       asyncio.create_task(self._reconcile_loop(), name=f"reconcile-{self.name}")]
        log.info("agent %s started: ComfyUI %s, manager %s", self.name, self.cfg.comfyui,
                 getattr(self.link, "shown", None) or getattr(self.link, "url", "in this process"))

    async def stop(self) -> None:
        """Stop following ComfyUI and the manager. Jobs in ComfyUI carry on; the next start
        picks them up again."""
        tasks = self._tasks + list(self._bg) + [h.task for h in self.held.values() if h.task]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.close()
        close = getattr(self.link, "close", None)
        if close:
            await close()

    async def run_forever(self) -> None:
        self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def by_pid(self, pid: str) -> Held | None:
        return next((h for h in self.held.values() if h.pid == pid), None)

    # ================================================================ the manager
    async def _manager_loop(self) -> None:
        backoff = self.cfg.retry_seconds
        while True:
            try:
                if not self.registered:
                    await self._hello()
                await self._flush_all()
                t0 = now()
                reply = await self._heartbeat()
                if self.manager_down:
                    log.info("agent %s: the manager is back (%s)", self.name, getattr(self.link, "url", ""))
                    self.manager_down = False
                backoff = self.cfg.retry_seconds
                await self._obey(reply)
                if not reply.get("hello") and not any(reply.values()) and now() - t0 < 0.5:
                    await asyncio.sleep(1.0)    # a manager that doesn't hold check-ins: don't spin
            except asyncio.CancelledError:
                raise
            except LinkError as e:
                if not self.manager_down:
                    log.warning("agent %s: can't reach the manager: %s (jobs carry on; reports wait)", self.name, e)
                    self.manager_down = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except Exception:  # noqa: BLE001
                log.exception("agent %s: manager loop error", self.name)
                await asyncio.sleep(2.0)

    def _held_list(self) -> list[dict]:
        return [{"job": h.id, "attempt": h.attempt, "status": h.status, "cancelling": h.cancelling}
                for h in self.held.values()]

    async def _hello(self) -> None:
        self._seq += 1
        reply = await self.link.hello({
            "name": self.name, "version": build(), "comfyui": self.cfg.comfyui, "seq": self._seq,
            "max_inflight": self.cfg.max_inflight, "capacity": self.cfg.max_inflight + 1, "held": self._held_list()})
        if reply.get("error"):
            raise LinkError(reply["error"])
        self.registered = True
        self.lease_seconds = reply.get("lease_seconds")
        for d in reply.get("drop") or []:
            await self._drop(d["job"], d["attempt"])
        for p in reply.get("adopt") or []:
            self._adopt(p)
        await self._send_status()
        if self.object_info is not None:
            await self._send_nodes()
        if not self._cleaned:
            self._cleaned = True
            self._clean_orphan_scratch()

    async def _heartbeat(self) -> dict:
        self._seq += 1
        reply = await self.link.heartbeat({"name": self.name, "seq": self._seq, "held": self._held_list(),
                                           "wait": self.cfg.heartbeat_wait})
        if reply.get("hello"):
            self.registered = False     # the manager restarted and doesn't know us yet
        return reply

    async def _obey(self, reply: dict) -> None:
        for d in reply.get("drop") or []:
            await self._drop(d["job"], d["attempt"])
        for c in reply.get("cancel") or []:
            h = self.held.get(c["job"])
            if h and h.attempt == c["attempt"]:
                await self._cancel(h)
        for p in reply.get("jobs") or []:
            await self._take(p)
        if reply.get("nodes"):
            self._spawn(self.refresh_object_info())

    async def _send_status(self) -> None:
        try:
            reply = await self.link.status({
                "name": self.name, "online": self.online, "connected": self.ws_connected, "stats": self.stats,
                "queue_remaining": self.queue_remaining, "error": self.error})
            if reply.get("hello"):
                self.registered = False
        except LinkError:
            pass                        # sent again with the next one, or after hello

    async def _send_nodes(self) -> None:
        try:
            reply = await self.link.nodes({"name": self.name, "object_info": self.object_info or {}})
        except LinkError:
            return                      # sent again after hello
        if reply.get("hello"):
            self.registered = False
            return
        self.problems = reply.get("problems") or {}
        for wf, probs in self.problems.items():
            log.warning("agent %s can't run %s: %s", self.name, wf, "; ".join(probs[:3]))

    # ---------------------------------------------------------------- reports
    async def _report(self, h: Held, event: str, lossy: bool = False, **data) -> None:
        msg = {"name": self.name, "job": h.id, "attempt": h.attempt, "event": event, "data": data}
        if lossy:                       # progress: dropped if it can't go now
            if h.outbox or h.finished:
                return
            try:
                reply = await self.link.report(msg)
            except LinkError:
                return
            if not reply.get("ok") and not reply.get("hello"):
                await self._stale(h, reply)
            return
        h.outbox.append(msg)
        if event in TERMINAL:
            h.finished = True
        await self._flush(h)

    async def _flush(self, h: Held) -> bool:
        """Deliver h's reports in order. False if the manager can't be reached (they go
        with the next check-in)."""
        async with h.lock:
            while h.outbox and not h.dropped:
                msg = h.outbox[0]
                try:
                    reply = await self.link.report(msg)
                except LinkError:
                    return False
                if reply.get("hello"):              # the manager restarted: hello first, then resend
                    self.registered = False
                    return False
                if h.outbox and h.outbox[0] is msg:
                    h.outbox.pop(0)
                if not reply.get("ok"):
                    h.outbox.clear()
                    await self._stale(h, reply)
                    return True
            if h.finished and not h.dropped:
                self._forget(h)
        return True

    async def _flush_all(self) -> None:
        for h in list(self.held.values()):
            if h.outbox:
                await self._flush(h)

    async def _stale(self, h: Held, reply: dict) -> None:
        """The manager doesn't count this run any more (cancelled, or given to another
        worker): stop it."""
        log.info("agent %s: job %s isn't ours any more (%s): dropping it", self.name, h.id[:8], reply.get("status"))
        await self._drop(h.id, h.attempt)

    def _forget(self, h: Held) -> None:
        self._release(h)
        if self.held.get(h.id) is h:
            del self.held[h.id]

    # ================================================================ jobs
    async def _take(self, payload: dict) -> None:
        job = W.localize(payload, self.view)
        old = self.held.get(job["job"])
        if old is not None:
            await self._drop(old.id, old.attempt)      # an earlier run of the same job
        h = Held(job=job, attempt=job["attempt"], pid=job["attempt"])
        self.held[h.id] = h
        log.info("agent %s took job %s (%s)", self.name, h.id[:8], job["workflow"])
        h.task = asyncio.create_task(self._prepare_and_submit(h), name=f"job-{h.id[:8]}")

    async def _prepare_and_submit(self, h: Held) -> None:
        self._ensure_sync()
        try:
            info = h.job["info"] or {}
            out_dir = info.get("output_dir")
            if out_dir:
                try:
                    await asyncio.to_thread(O.makedirs_readable, out_dir)
                except OSError as e:
                    await self._fail(h, f"Can't create the output folder {out_dir}: {e.strerror or e}")
                    return
            tex = info.get("textures")
            if tex:
                # Empty, so a second attempt can't mix its map images with the first one's.
                try:
                    await asyncio.to_thread(shutil.rmtree, tex["raw_dir"], True)
                    await asyncio.to_thread(os.makedirs, tex["raw_dir"], exist_ok=True)
                except OSError as e:
                    await self._fail(h, f"Can't create the scratch folder {tex['raw_dir']}: {e.strerror or e}")
                    return
            seq_info = info.get("input") or {}
            if seq_info.get("convert"):
                async with self._convert_sem:
                    if h.dropped or not await self._convert(h, seq_info):
                        return
            if not h.dropped:
                await self._submit(h)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("agent %s: job %s failed", self.name, h.id[:8])
            await self._fail(h, f"{type(e).__name__}: {e}")

    async def _convert(self, h: Held, seq_info: dict) -> bool:
        conv = seq_info["convert"]
        kind, to = conv.get("kind", "?"), conv.get("to") or "PNG"
        at = f" at {conv['work_resolution']} px" if conv.get("scaled") else ""
        label = {"exr": f"converting EXR to {to}{at}", "movie": "extracting movie frames",
                 "image": f"scaling the input to {conv.get('work_resolution')} px"}.get(kind, kind)
        t0, last = now(), [0.0]

        def progress(done: int, total: int) -> None:
            if now() - last[0] < 0.5 and done < total:
                return
            last[0] = now()
            self._spawn(self._report(h, "progress", lossy=True, status="preparing", stage=f"{label} {done}/{total}"))

        await self._report(h, "progress", lossy=True, status="preparing", stage=label)
        log.info("job %s: %s", h.id[:8], label)
        try:
            await C.convert(seq_info, self.cfg.convert, progress)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, (ConvertError, SequenceError)) else f"{type(e).__name__}: {e}"
            await self._fail(h, f"Converting the input failed: {msg}")
            return False
        log.info("job %s: input ready in %.0fs", h.id[:8], now() - t0)
        return True

    async def _take_slot(self, h: Held) -> None:
        """Wait for one of ComfyUI's places (max_inflight), and for ComfyUI to be up."""
        async with self._slots:
            await self._slots.wait_for(lambda: self._in_comfy < self.cfg.max_inflight)
            self._in_comfy += 1
            h.slot = True
        while not (self.online and self.ws_connected and self.checked):
            await asyncio.sleep(0.25)

    def _hold_slot(self, h: Held) -> None:
        """A run already in ComfyUI (picked up again after a restart) takes a place as it is."""
        if not h.slot:
            h.slot = True
            self._in_comfy += 1

    def _release(self, h: Held) -> None:
        if h.slot:
            h.slot = False
            self._in_comfy -= 1
            self._spawn(self._notify_slots())

    async def _notify_slots(self) -> None:
        self._ensure_sync()
        async with self._slots:
            self._slots.notify_all()

    async def _submit(self, h: Held) -> None:
        await self._take_slot(h)
        if h.dropped:
            self._release(h)
            return
        prompt = fit_to_options(h.job["prompt"], self.object_info)   # LTX\\x vs LTX/x on this OS
        titles = _titles(prompt)
        extra = {"comfyfarm_job": h.id, "comfyfarm_user": h.job.get("user")}
        h.submitting = True
        try:
            while True:
                try:
                    pid, node_errors = await self.client.submit(prompt, h.attempt, self.client_id, extra)
                    break
                except ComfyRejected as e:
                    await self._fail(h, "ComfyUI rejected the job: " + describe_rejection(e.body, titles))
                    return
                except httpx.HTTPError as e:
                    # The request may have landed even though we got no answer; the attempt
                    # id lets us check instead of risking a duplicate run.
                    try:
                        running, pending = await self.client.queue()
                        if h.attempt in running or h.attempt in pending or await self.client.history(h.attempt):
                            pid, node_errors = h.attempt, {}
                            break
                    except Exception:  # noqa: BLE001
                        pass
                    log.warning("agent %s: submitting job %s failed (%s); trying again", self.name, h.id[:8], e)
                    await asyncio.sleep(2.0)
                    while not (self.online and self.ws_connected and self.checked):
                        await asyncio.sleep(0.25)
        finally:
            h.submitting = False
        h.pid = pid
        if node_errors:
            try:
                await self.client.delete_queued([pid])
            except httpx.HTTPError:
                self.abandoned.add(pid)
            body = {"error": {"message": "Some outputs failed validation"}, "node_errors": node_errors}
            await self._fail(h, "ComfyUI rejected the job: " + describe_rejection(body, titles))
            return
        h.status, h.dispatched_at = "dispatched", now()
        h.rt = Runtime(job_id=h.id, expected=h.job.get("expected_nodes") or 20, titles=titles)
        log.info("job %s -> ComfyUI on %s (prompt %s)", h.id[:8], self.name, pid[:8])
        await self._report(h, "dispatched", at=h.dispatched_at, prompt_id=pid)
        if h.cancelling and not h.dropped:
            if await self._cancel_in_comfy(h):
                await self._cancelled(h)

    # ---------------------------------------------------------------- endings
    async def _fail(self, h: Held, message: str, traceback: str | None = None) -> None:
        if h.finished or h.dropped:
            return
        self._release(h)
        if not self.cfg.convert.keep_failed:
            S.cleanup(h.job, self.scratch_root)
        await self._report(h, "failed", error=message, traceback=traceback)

    async def _cancelled(self, h: Held) -> None:
        if h.finished or h.dropped:
            return
        self._release(h)
        S.cleanup(h.job, self.scratch_root)
        await self._report(h, "cancelled")

    async def _lost(self, h: Held, reason: str) -> None:
        if h.finished or h.dropped:
            return
        if h.pid:
            self.abandoned.add(h.pid)
        if h.rt is not None:
            h.rt = None
        self._release(h)
        S.cleanup(h.job, self.scratch_root)
        await self._report(h, "lost", reason=reason)

    async def _cancel(self, h: Held) -> None:
        """The manager cancelled this job: take it out of ComfyUI."""
        if h.cancelling or h.finished:
            return
        h.cancelling = True
        if h.status == "preparing":
            if h.submitting:
                return                  # _submit takes it out of ComfyUI once it's in
            if h.task and not h.task.done():
                h.task.cancel()
                await asyncio.gather(h.task, return_exceptions=True)
            await self._cancelled(h)
        elif h.status in ON_WORKER:
            if await self._cancel_in_comfy(h):
                await self._cancelled(h)
            # otherwise ComfyUI's interrupt event finishes it
        # postprocessing: it finishes

    async def _cancel_in_comfy(self, h: Held) -> bool:
        """Delete it from ComfyUI's queue if waiting, interrupt it if running. True if it's
        gone without having run (the caller finishes the cancel)."""
        pid = h.pid
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
                return True             # it was only waiting; now gone
        except httpx.HTTPError as e:
            log.warning("cancel %s: ComfyUI on %s unreachable (%s); cancelled when it returns", h.id[:8], self.name, e)
            self.abandoned.add(pid)
            return True
        return False

    async def _drop(self, job_id: str, attempt: str) -> None:
        """This run isn't the job's current one any more (the manager cancelled it, or gave
        the job to another run): stop it without reporting."""
        h = self.held.get(job_id)
        if h is None or h.attempt != attempt:
            return
        if h.finished and not h.outbox:
            self._forget(h)             # it ended as we reported
            return
        log.info("agent %s: dropping job %s", self.name, job_id[:8])
        h.dropped = True
        del self.held[job_id]
        h.outbox.clear()
        if h.task and not h.task.done() and h.task is not asyncio.current_task():
            self.abandoned.add(h.attempt)   # in case its submit landed as we stopped it
            h.task.cancel()
            await asyncio.gather(h.task, return_exceptions=True)
        if h.pid and h.status in ON_WORKER:
            self.abandoned.add(h.pid)
            try:
                running, pending = await self.client.queue()
                if h.pid in pending:
                    await self.client.delete_queued([h.pid])
                elif h.pid in running:
                    await self.client.interrupt(h.pid)
            except httpx.HTTPError:
                pass                    # reconcile cancels it when ComfyUI is back
        self._release(h)
        if h.status != "postprocessing":
            S.cleanup(h.job, self.scratch_root)

    def _adopt(self, payload: dict) -> None:
        """A job the manager has on this agent that it didn't know about (it restarted):
        follow it again in ComfyUI, prepare it again, or report it lost."""
        job = W.localize(payload, self.view)
        h = Held(job=job, attempt=job["attempt"], status=job.get("status") or "preparing", pid=job["attempt"],
                 dispatched_at=job.get("dispatched_at"), started_at=job.get("started_at"))
        self.held[h.id] = h
        self._spawn(self._reattach(h))

    async def _reattach(self, h: Held) -> None:
        self._ensure_sync()
        if h.status == "postprocessing":
            await self._lost(h, f"{self.name} restarted while collecting the outputs.")
            return
        while True:
            while not (self.online and self.ws_connected):
                await asyncio.sleep(0.25)
            try:
                running, pending = await self.client.queue()
                hist = None if h.pid in running or h.pid in pending else await self.client.history(h.pid)
                break
            except httpx.HTTPError:
                await asyncio.sleep(1.0)
        if self.held.get(h.id) is not h:
            return                      # dropped meanwhile
        was = h.status
        if h.pid in running or h.pid in pending:
            self._hold_slot(h)
            h.status = "running" if h.pid in running else "dispatched"
            h.rt = Runtime(job_id=h.id, expected=h.job.get("expected_nodes") or 20, titles=_titles(h.job["prompt"]))
            h.dispatched_at = h.dispatched_at or now()
            log.info("agent %s picked job %s up again in ComfyUI", self.name, h.id[:8])
            if was == "preparing":
                await self._report(h, "dispatched", at=h.dispatched_at, prompt_id=h.pid)
            if h.status == "running" and was != "running":
                h.started_at = h.started_at or now()
                await self._report(h, "running", at=h.started_at)
        elif hist:
            self._hold_slot(h)
            if was == "preparing":
                h.status = "dispatched"
                await self._report(h, "dispatched", at=h.dispatched_at or now(), prompt_id=h.pid)
            await self._from_history(h, hist)
        elif was == "preparing":
            log.info("agent %s: job %s never reached ComfyUI: preparing it again", self.name, h.id[:8])
            h.task = asyncio.create_task(self._prepare_and_submit(h), name=f"job-{h.id[:8]}")
        else:
            await self._lost(h, f"ComfyUI on {self.name} lost the job while {self.name} was restarting.")

    async def _from_history(self, h: Held, hist: dict) -> None:
        """A run that ended in ComfyUI while we weren't listening."""
        if h.finished or h.dropped:
            return
        status = hist.get("status") or {}
        if status.get("status_str") == "success" or (status.get("completed") and status.get("status_str") != "error"):
            await self._finalize(h, hist)
            return
        message, interrupted, tb = _history_failure(hist)
        if interrupted and h.cancelling:
            await self._cancelled(h)
        else:
            await self._fail(h, message, traceback=tb)
        self._spawn(self._drop_history(h.pid))

    def _clean_orphan_scratch(self) -> None:
        """Scratch folders of jobs this agent doesn't hold (finished while it was down)."""
        if self.cfg.convert.keep_failed:
            return
        keep = set(self.held)
        S.clean_orphans(self.scratch_root, lambda name: name in keep)

    # ================================================================ ComfyUI: events
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
                    # or models changed: check the workflows again before taking work.
                    self.checked = False
                    await self.refresh_object_info()
                    await self._reconcile()
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            continue    # live preview images
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
                self.checked = False
                log.warning("lost event connection to %s", self.name)
                await self._send_status()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _progress(self, h: Held, stage: str | None = None, force: bool = False) -> None:
        rt = h.rt
        if rt is None:
            return
        t = now()
        if not force and t - rt.last_push < 0.5:
            return
        rt.last_push = t
        data = {"status": h.status, "progress": round(rt.progress(), 4)}
        if stage:
            data["stage"] = stage
        self._spawn(self._report(h, "progress", lossy=True, **data))

    async def _running(self, h: Held) -> None:
        if h.status == "dispatched":
            h.status, h.started_at = "running", now()
            await self._report(h, "running", at=h.started_at, stage="starting")

    async def _on_message(self, msg: dict) -> None:
        kind, data = msg.get("type"), msg.get("data") or {}
        if kind == "status":
            self.queue_remaining = ((data.get("status") or {}).get("exec_info") or {}).get("queue_remaining")
            return
        pid = data.get("prompt_id")
        if not pid or pid in self.abandoned:
            return
        h = self.by_pid(pid)
        if h is None or h.status not in ON_WORKER:
            return
        rt = h.rt
        if kind == "execution_start":
            await self._running(h)
        elif kind == "execution_cached" and rt:
            rt.done.update(data.get("nodes") or [])
            await self._progress(h, "using cached results")
        elif kind == "executing" and rt:
            node = data.get("node")
            await self._running(h)
            if rt.current and rt.current != node:
                rt.done.add(rt.current)
            rt.current, rt.frac = node, 0.0
            if node is None:            # older ComfyUI signals completion this way
                self._spawn(self._finalize(h))
            else:
                await self._progress(h, rt.titles.get(node, node), force=True)
        elif kind == "progress" and rt:
            node = data.get("node")
            if node and node != rt.current:
                if rt.current:
                    rt.done.add(rt.current)
                rt.current = node
            mx = data.get("max") or 0
            rt.frac = (data.get("value") or 0) / mx if mx else 0.0
            await self._progress(h, f"{rt.titles.get(rt.current, rt.current)} {data.get('value')}/{mx}")
        elif kind == "executed" and rt:
            if data.get("node"):
                rt.done.add(data["node"])
        elif kind == "execution_success":
            self._spawn(self._finalize(h))
        elif kind == "execution_error":
            title = rt.titles.get(str(data.get("node_id")), "") if rt else ""
            message = (f"{data.get('node_type')} (node {data.get('node_id')}"
                       f"{' ' + repr(title) if title else ''}): {str(data.get('exception_message', '')).strip()}")
            await self._fail(h, message, traceback="".join(data.get("traceback") or []) or None)
            self._spawn(self._drop_history(pid))
        elif kind == "execution_interrupted":
            if h.cancelling:
                await self._cancelled(h)
            else:
                await self._fail(h, "Interrupted on the farm machine.")
            self._spawn(self._drop_history(pid))

    async def _drop_history(self, pid: str | None) -> None:
        if not pid or not self.cfg.delete_comfy_history:
            return
        try:
            await self.client.delete_history([pid])
        except httpx.HTTPError:
            pass

    # ================================================================ ComfyUI: reconcile
    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.reconcile_seconds)
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
        disconnected, and runs ComfyUI lost because it restarted. One at a time: the
        reconnect and the periodic check must not both act on the same lost run."""
        async with self._reconcile_lock:
            await self._reconcile_once()

    async def _reconcile_once(self) -> None:
        t0 = now()
        runs = [h for h in self.held.values() if h.status in ON_WORKER and (h.dispatched_at or 0) < t0 - 2]
        if not runs and not self.abandoned:
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
        for h in runs:
            if self.held.get(h.id) is not h or h.status not in ON_WORKER:
                continue                # dropped or finished meanwhile
            if h.pid in running:
                await self._running(h)
                if h.rt is None:
                    h.rt = Runtime(job_id=h.id, expected=h.job.get("expected_nodes") or 20,
                                   titles=_titles(h.job["prompt"]))
                continue
            if h.pid in pending:
                continue
            hist = await self.client.history(h.pid)
            if self.held.get(h.id) is not h or h.status not in ON_WORKER:
                continue
            if hist:
                self._spawn(self._from_history(h, hist))
                continue
            await self._lost(h, f"ComfyUI on {self.name} lost the job (restarted?).")

    # ================================================================ ComfyUI: health
    async def _health_loop(self) -> None:
        fails = 0
        while True:
            reachable = False
            try:
                self.stats = await self.client.system_stats()
                reachable, fails = True, 0
                if not self.online:
                    self.online, self.error = True, None
                    log.info("worker %s online", self.name)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                if self.online and fails >= 2:
                    self.online, self.offline_since = False, now()
                    self.error = str(e) or type(e).__name__
                    log.warning("worker %s offline: %s", self.name, self.error)
                if not self.online and now() - self.offline_since > self.cfg.lost_after_seconds:
                    for h in [h for h in self.held.values() if h.status in ON_WORKER]:
                        await self._lost(h, f"ComfyUI on {self.name} was unreachable for over "
                                            f"{duration(self.cfg.lost_after_seconds)}.")
            try:
                self.launcher.tick(reachable)   # starts ComfyUI if agent.yaml says how and it isn't running
            except Exception:  # noqa: BLE001
                log.exception("starting ComfyUI on %s failed", self.name)
            if self.registered:
                await self._send_status()
            await asyncio.sleep(self.cfg.health_seconds)

    async def refresh_object_info(self) -> None:
        """Read ComfyUI's node definitions and send them to the manager, which checks the
        workflows against them."""
        try:
            oi = await self.client.object_info()
        except Exception as e:  # noqa: BLE001
            log.warning("couldn't read node definitions from %s: %s", self.name, e)
            self.checked = True
            return
        self.object_info = oi
        self.output_classes = {k for k, v in oi.items() if isinstance(v, dict) and v.get("output_node")}
        if self.registered:
            await self._send_nodes()
        self.checked = True
        if self.registered:
            await self._send_status()

    # ================================================================ outputs
    async def _finalize(self, h: Held, hist: dict | None = None) -> None:
        if h.status not in ON_WORKER or self.held.get(h.id) is not h:
            return                      # already handled (duplicate event, cancel, reconcile)
        h.status = "postprocessing"
        rt, h.rt = h.rt, None
        await self._report(h, "postprocessing")
        if self.held.get(h.id) is not h:
            return                      # the manager said it isn't ours any more
        try:
            if hist is None:
                for _ in range(10):     # history is written just after the success event
                    hist = await self.client.history(h.pid)
                    if hist:
                        break
                    await asyncio.sleep(0.5)
            outputs, warnings = await self._collect_outputs(h, hist or {})
            t = now()
            started = h.started_at or h.dispatched_at or t
            nodes = len(rt.done | ({rt.current} if rt.current else set())) if rt else None
            S.cleanup(h.job, self.scratch_root)
            self._release(h)
            log.info("job %s done in %.0fs -> %s", h.id[:8], t - started, outputs.get("output_dir"))
            await self._report(h, "done", outputs=W.portable(outputs, self.view), warnings=warnings,
                               seconds=t - started, nodes=nodes or None)
        except Exception as e:  # noqa: BLE001
            log.exception("collecting outputs for %s failed", h.id[:8])
            await self._fail(h, f"Finished on the GPU but collecting outputs failed: {e}")
        finally:
            self._release(h)
            await self._drop_history(h.pid)

    async def _collect_outputs(self, h: Held, hist: dict) -> tuple[dict, list[str]]:
        info = h.job["info"] or {}
        collect = h.job.get("collect") or {}
        mappings = self.cfg.paths.mappings
        out_dir = info.get("output_dir")
        outputs: dict = {"output_dir": out_dir, "output_dir_posix": info.get("output_dir_posix")}
        warnings: list[str] = []
        tokens = dict(info.get("name_tokens") or {})

        tex = info.get("textures")
        if tex and out_dir:
            last = [0.0]

            def progress(done: int, total: int) -> None:
                if now() - last[0] < 0.5 and done < total:
                    return
                last[0] = now()
                self._spawn(self._report(h, "progress", lossy=True, status="postprocessing",
                                         stage=f"writing EXR maps {done}/{total}"))

            outputs.update(await O.write_maps(tex, out_dir, self.cfg.textures, self.cfg.convert, mappings, progress))

        fr = collect.get("frames")
        if fr and out_dir:
            res = await asyncio.to_thread(O.rename_frames, info, fr.get("ext", "png"), _DEFAULT_PATHS.frame_name,
                                          mappings)
            outputs.update(res)
            exp = info.get("expected_frames")
            if exp and res["frames"] != exp:
                warnings.append(f"Got {res['frames']} output frames from {exp} input frames; "
                                f"numbering starts at {res['first']} and may not line up with the plate.")

        pv = collect.get("preview")
        if pv:
            node_out = (hist.get("outputs") or {}).get(str(pv["node"])) or {}
            entries = node_out.get("gifs") or node_out.get("videos") or node_out.get("images") or []
            if entries and out_dir:
                e = entries[-1]
                dest = Path(out_dir) / (info.get("preview_name") or _DEFAULT_PATHS.preview_name).format(**tokens)
                try:
                    await self.client.download(e["filename"], e.get("subfolder", ""), e.get("type", "output"), dest)
                    O.readable(str(dest))
                    outputs["preview"] = P.normalize(str(dest))
                    outputs["preview_posix"] = P.to_posix(outputs["preview"], mappings)
                except Exception as ex:  # noqa: BLE001
                    warnings.append(f"Couldn't copy the preview video: {ex}")
            elif not entries:
                warnings.append("ComfyUI returned no preview video.")
        return outputs, warnings


__all__ = ["Agent", "Held", "Runtime"]
