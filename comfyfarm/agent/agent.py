"""An agent: runs the manager's jobs on this machine through its runners (protocol.py,
runners/base.py): ComfyUI (runners/comfyui.py) and the process runner (runners/process.py).
Each job goes to the runner whose definition it is (decision 0025). The runs of one runner
at a time hold the machine's card, and a runner that held it frees what it keeps there
before another one's run starts (the GPU claim, decision 0026).

It checks in with the manager and takes the jobs the manager gives it: one for its GPU,
and one prepared ahead when every GPU on the farm is busy (never more than its ComfyUI runs
at once, max_inflight, plus one). For each job it:

1. converts the input (EXR, movie, images larger than the workflow's work_resolution)
   into its own scratch folder,
2. hands the run to the runner, which starts it and tells the agent as it moves on
   (started, progress, succeeded, failed, interrupted, lost),
3. collects the outputs: frames renamed, texture maps written as EXRs, the preview
   movie made from the frames (or copied, from a workflow's own video node),
4. reports each step to the manager. A report that can't be delivered (the manager is
   restarting) waits and goes later, in order.

Jobs arrive with studio paths and @scratch/ / @repo/ (fileio.wire); the agent sees them
through its own paths.mappings, scratch folder and checkout. It keeps its jobs in memory:
after a restart the manager hands them back and the agent asks the runner where each
one is."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import AgentConfig, PathsConfig
from ..fileio import convert as C
from ..fileio import outputs as O
from ..fileio import paths as P
from ..fileio import preview as PV
from ..fileio import scratch as S
from ..fileio import wire as W
from ..fileio.errors import ConvertError, SequenceError
from ..fileio.ocio import REPO
from ..protocol import ON_WORKER, TERMINAL, LinkError, build, now
from ..runners import make_runners
from ..runners.base import Runner, RunRejected, RunState
from . import gpus as G

log = logging.getLogger("comfyfarm")
_DEFAULT_PATHS = PathsConfig()


@dataclass
class Held:
    """One run of a job this agent holds."""
    job: dict                       # the job as this machine sees it (wire.localize)
    attempt: str                    # the run's id, given by the manager
    status: str = "preparing"       # preparing | dispatched | running | postprocessing
    pid: str | None = None          # its id in the runner (the attempt id, unless the runner minted its own)
    rt: RunState | None = None      # its live progress, kept by the runner
    runner: Runner | None = None    # the runner whose definition it is (Agent.runner_for)
    notes: list = field(default_factory=list)   # warnings for the job from before it ran (the GPU claim)
    task: asyncio.Task | None = None
    slot: bool = False              # has one of the runner's max_inflight places
    submitting: bool = False
    cancelling: bool = False
    finished: bool = False          # its last report is on its way; forgotten once delivered
    dropped: bool = False           # the manager moved on: stop, report nothing
    dispatched_at: float | None = None
    started_at: float | None = None
    prepare_seconds: float = 0.0    # converting its inputs (for the manager's ETAs)
    outbox: list = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def id(self) -> str:
        return self.job["job"]


class Agent:
    CLAIM_RETRY_SECONDS = 10.0          # between asking the card's last runner to free it (the GPU claim)

    def __init__(self, cfg: AgentConfig, link, runner: Runner | None = None, runners: list | None = None):
        self.cfg = cfg
        self.link = link
        self.name = cfg.name
        # Its runners. The first is the machine's resident backend (ComfyUI): the one its
        # status, node definitions, environment and Free VRAM are about.
        self.runners: list[Runner] = list(runners) if runners else [runner] if runner else make_runners(cfg)
        self.runner: Runner = self.runners[0]
        for r in self.runners:
            r.attach(self)
        self.scratch_root = P.normalize(str(cfg.resolve(cfg.convert.scratch_dir)))
        self.view = W.View(mappings=cfg.paths.mappings, scratch=self.scratch_root, repo=P.normalize(str(REPO)))
        self.held: dict[str, Held] = {}          # job id -> its run
        self.problems: dict[str, list[str]] = {}
        # What workflows can ask for (requires.tags): agent.yaml's tags, and this OS.
        osname = "windows" if os.name == "nt" else "linux" if sys.platform.startswith("linux") else sys.platform
        self.tags = sorted({str(t).strip().lower() for t in cfg.tags if str(t).strip()} | {osname})
        self.environment_differs: list[str] = []
        # the manager
        self.registered = False
        self.manager_down = False
        self.lease_seconds: float | None = None
        self._seq = 0
        self._cleaned = False
        self._in_comfy = 0                       # runs holding a place on the card, whichever runner has them
        self._waiting: list[Held] = []           # runs waiting for one, in the order they came
        # The runner that used the card last: it may keep memory there (ComfyUI's models), so it
        # frees it before another runner's run starts. ComfyUI is resident: it may hold models now.
        self.card_owner: Runner | None = self.runner
        self._claim_lock: asyncio.Lock | None = None
        self._slots: asyncio.Condition | None = None
        self._convert_sem: asyncio.Semaphore | None = None
        self._tasks: list[asyncio.Task] = []
        self._bg: set[asyncio.Task] = set()
        # This machine's GPUs as nvidia-smi reads them (gpu_probe), and why it couldn't.
        self.gpus: list[dict] | None = None
        self.gpu_error: str | None = None
        self.released: dict | None = None       # the last time its runner freed its VRAM, and how it went
        self._releasing = False

    # ================================================================ lifecycle
    def _ensure_sync(self) -> None:
        if self._slots is None:
            self._slots = asyncio.Condition()
            self._claim_lock = asyncio.Lock()
            self._convert_sem = asyncio.Semaphore(max(1, self.cfg.convert.max_parallel_jobs))

    def start(self) -> None:
        self._ensure_sync()
        self._tasks = [asyncio.create_task(self._manager_loop(), name=f"manager-{self.name}"),
                       *(t for r in self.runners for t in r.start())]
        if self.cfg.gpu_probe.enabled:
            self._tasks.append(asyncio.create_task(self._gpu_loop(), name=f"gpus-{self.name}"))
        log.info("agent %s started: ComfyUI %s, manager %s", self.name, self.cfg.comfyui,
                 getattr(self.link, "shown", None) or getattr(self.link, "url", "in this process"))

    async def stop(self) -> None:
        """Stop following the runner and the manager. Jobs in ComfyUI carry on; the next
        start picks them up again."""
        tasks = self._tasks + list(self._bg) + [h.task for h in self.held.values() if h.task]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for r in self.runners:
            await r.close()
        close = getattr(self.link, "close", None)
        if close:
            await close()

    async def run_forever(self) -> None:
        self.start()
        try:
            await asyncio.Event().wait()
        finally:
            await self.stop()

    # ---------------------------------------------------------------- for the runner (runners.base.Host)
    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def by_pid(self, pid: str) -> Held | None:
        return next((h for h in self.held.values() if h.pid == pid), None)

    def runs(self) -> list[Held]:
        return list(self.held.values())

    def holds(self, h: Held) -> bool:
        return self.held.get(h.id) is h

    def runner_for(self, workflow: str) -> Runner | None:
        """The runner whose definition this is (ComfyUI's workflows, the process runner's processes)."""
        return next((r for r in self.runners if getattr(r, "knows", lambda _w: False)(workflow)), None)

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
            "max_inflight": self.cfg.max_inflight, "capacity": self.cfg.max_inflight + 1, "held": self._held_list(),
            "tags": self.tags})
        if reply.get("error"):
            raise LinkError(reply["error"])
        self.registered = True
        self.lease_seconds = reply.get("lease_seconds")
        for d in reply.get("drop") or []:
            await self._drop(d["job"], d["attempt"])
        for p in reply.get("adopt") or []:
            self._adopt(p)
        await self.send_status()
        if self.runner.object_info is not None and not self.runner.farm_nodes:
            await self.send_nodes()
        if self.runner.environment is not None:
            await self.send_environment()
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
            self.spawn(self.runner.refresh_object_info())
        if reply.get("release_vram") and not self._releasing:
            self.spawn(self.release_vram())

    async def release_vram(self) -> dict:
        """The manager asked (the Machines tab's Free VRAM): the runner frees what it keeps on
        the card, when the farm has nothing running in it (decision 0023). Reported with the
        status, with the GPUs read again when gpu_probe is on."""
        self._ensure_sync()
        self._releasing = True
        try:
            async with self._claim_lock:
                return await self._release_vram()
        finally:
            self._releasing = False

    async def _release_vram(self) -> dict:
        busy = sum(1 for h in self.held.values() if h.slot and h.runner is self.runner)
        if busy:
            result = {"ok": False, "before": None, "after": None,
                      "error": f"{busy} job(s) in {self.runner.label}: drain the machine and wait "
                               f"for them, then free its VRAM"}
        else:
            result = await self.runner.release()
        await self._record_release(self.runner, result)
        return self.released

    async def _record_release(self, runner: Runner, result: dict, quiet: bool = False) -> None:
        """Kept for the Machines tab (status `released`), with the GPUs read again when gpu_probe is on."""
        if self.cfg.gpu_probe.enabled:
            await self.probe_gpus()
        self.released = {"at": now(), **result}
        if result["ok"]:
            log.info("agent %s: %s freed its VRAM", self.name, runner.label)
        elif not quiet:
            log.warning("agent %s: %s didn't free its VRAM: %s", self.name, runner.label, result["error"])
        await self.send_status()

    async def probe_gpus(self) -> None:
        """Read this machine's GPUs (agent/gpus.py), off the event loop: nvidia-smi can be slow."""
        gpus, error = await asyncio.to_thread(G.probe, self.cfg.gpu_probe.command)
        if error and error != self.gpu_error:
            log.warning("agent %s: can't read its GPUs: %s", self.name, error)
        self.gpus, self.gpu_error = gpus, error

    async def _gpu_loop(self) -> None:
        while True:
            try:
                await self.probe_gpus()
                if self.registered:
                    await self.send_status()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("agent %s: reading its GPUs failed", self.name)
            await asyncio.sleep(max(1.0, self.cfg.gpu_probe.seconds))

    async def send_status(self) -> None:
        msg = {"name": self.name, **self.runner.status()}
        if self.cfg.gpu_probe.enabled:
            msg.update(gpus=self.gpus, gpu_error=self.gpu_error)
        if self.released is not None:
            msg["released"] = self.released
        try:
            reply = await self.link.status(msg)
            if reply.get("hello"):
                self.registered = False
        except LinkError:
            pass                        # sent again with the next one, or after hello

    async def send_nodes(self) -> None:
        try:
            processes = {name: v for r in reversed(self.runners) for name, v in r.processes().items()}
            reply = await self.link.nodes({"name": self.name, "processes": processes})
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
        h = Held(job=job, attempt=job["attempt"], pid=job["attempt"], runner=self.runner_for(job["workflow"]))
        self.held[h.id] = h
        log.info("agent %s took job %s (%s)", self.name, h.id[:8], job["workflow"])
        if h.runner is None:
            await self.run_failed(h, f"{self.name} has no workflow or process {job['workflow']}. Update the farm's "
                                     f"code on it.")
            return
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
                    await self.run_failed(h, f"Can't create the output folder {out_dir}: {e.strerror or e}")
                    return
            tex = info.get("textures")
            if tex:
                # Empty, so a second attempt can't mix its map images with the first one's.
                try:
                    await asyncio.to_thread(shutil.rmtree, tex["raw_dir"], True)
                    await asyncio.to_thread(os.makedirs, tex["raw_dir"], exist_ok=True)
                except OSError as e:
                    await self.run_failed(h, f"Can't create the scratch folder {tex['raw_dir']}: {e.strerror or e}")
                    return
            # The primary input first: converting it empties <scratch>/<job id>/, where the
            # other inputs convert into their own subfolders.
            todo = [(None, info.get("input") or {})] + [(n, x) for n, x in (info.get("inputs") or {}).items() if x]
            for what, seq_info in todo:
                if seq_info.get("convert"):
                    async with self._convert_sem:
                        t0 = now()
                        ok = not h.dropped and await self._convert(h, seq_info, what)
                        h.prepare_seconds += now() - t0
                        if not ok:
                            return
            if not h.dropped:
                await self._submit(h)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("agent %s: job %s failed", self.name, h.id[:8])
            await self.run_failed(h, f"{type(e).__name__}: {e}")

    async def _convert(self, h: Held, seq_info: dict, what: str | None = None) -> bool:
        """Convert one input (`what`: the name of an input besides the primary)."""
        conv = seq_info["convert"]
        kind, to = conv.get("kind", "?"), conv.get("to") or "PNG"
        at = f" at {conv['work_resolution']} px" if conv.get("scaled") else ""
        label = {"exr": f"converting EXR to {to}{at}", "movie": "extracting movie frames",
                 "image": f"scaling the input to {conv.get('work_resolution')} px"}.get(kind, kind)
        if what:
            label = f"{what}: {label}"
        t0, last = now(), [0.0]

        def progress(done: int, total: int) -> None:
            if now() - last[0] < 0.5 and done < total:
                return
            last[0] = now()
            self.spawn(self._report(h, "progress", lossy=True, status="preparing", stage=f"{label} {done}/{total}"))

        await self._report(h, "progress", lossy=True, status="preparing", stage=label)
        log.info("job %s: %s", h.id[:8], label)
        try:
            await C.convert(seq_info, self.cfg.convert, progress)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, (ConvertError, SequenceError)) else f"{type(e).__name__}: {e}"
            await self.run_failed(h, f"Converting {what or 'the input'} failed: {msg}")
            return False
        log.info("job %s: input ready in %.0fs", h.id[:8], now() - t0)
        return True

    async def _take_slot(self, h: Held) -> None:
        """Wait for a place on the card (the GPU claim, decision 0026), for the runner to be up,
        and for the runner that used the card before to free it."""
        async with self._slots:
            self._waiting.append(h)
            try:
                await self._slots.wait_for(lambda: self._may_start(h))
            finally:
                self._waiting.remove(h)
                self._slots.notify_all()        # a run that stopped waiting may have held others back
            self._in_comfy += 1
            h.slot = True
        while not h.runner.ready:
            await asyncio.sleep(0.25)
        await self._claim_card(h)

    def _max_runs(self, runner: Runner) -> int:
        """How many of a runner's runs share the card at once: ComfyUI's max_inflight (it queues
        them itself); the process runner's one."""
        return getattr(runner, "max_runs", None) or self.cfg.max_inflight

    def _may_start(self, h: Held) -> bool:
        mine = others = 0
        for x in self.held.values():
            if x.slot:
                if x.runner is h.runner:
                    mine += 1
                else:
                    others += 1
        if others or mine >= self._max_runs(h.runner):
            return False            # the card is another runner's until its runs end, or this one's is full
        for w in self._waiting:     # first come, first served between runners: none jumps the other's queue
            if w is h:
                break
            if w.runner is not h.runner:
                return False
        return True

    async def _claim_card(self, h: Held) -> None:
        """The runner that used the card last frees what it keeps there (ComfyUI's models)
        before this run's runner uses it. If it can't (ComfyUI busy with prompts that aren't the
        farm's), the run waits, saying why, for claim_wait_seconds; then it starts anyway and
        the job says so."""
        async with self._claim_lock:
            prev = self.card_owner
            if prev is not None and prev is not h.runner and getattr(prev, "online", True):
                deadline = now() + self.cfg.claim_wait_seconds
                tries = 0
                while not h.dropped:
                    result = await prev.release()
                    tries += 1
                    await self._record_release(prev, result, quiet=tries > 1)     # said once, not every try
                    if result["ok"]:
                        break
                    if now() >= deadline:
                        log.warning("agent %s: job %s starts on a card %s didn't free (%s)", self.name, h.id[:8],
                                    prev.label, result["error"])
                        h.notes.append(f"{prev.label} didn't free the GPU before this job started ({result['error']}), "
                                       f"so it shared the card")
                        break
                    await self._report(h, "progress", lossy=True, status="preparing",
                                       stage=f"waiting for {prev.label} to free the GPU: {result['error']}")
                    await asyncio.sleep(max(0.05, min(self.CLAIM_RETRY_SECONDS, deadline - now())))
            if not h.dropped:           # dropped while waiting: the card is still the other runner's
                self.card_owner = h.runner

    def _hold_slot(self, h: Held) -> None:
        """A run already in the runner (picked up again after a restart) takes a place as it is,
        and its runner has the card."""
        if not h.slot:
            h.slot = True
            self._in_comfy += 1
        self.card_owner = h.runner

    def _release(self, h: Held) -> None:
        if h.slot:
            h.slot = False
            self._in_comfy -= 1
            self.spawn(self._notify_slots())

    async def _notify_slots(self) -> None:
        self._ensure_sync()
        async with self._slots:
            self._slots.notify_all()

    async def _submit(self, h: Held) -> None:
        await self._take_slot(h)
        if h.dropped:
            self._release(h)
            return
        rejected = None
        h.submitting = True
        try:
            await h.runner.submit(h)            # sets h.pid and h.rt
        except RunRejected as e:
            rejected = str(e)
        finally:
            h.submitting = False
        if rejected is not None:
            await self.run_failed(h, rejected)
            return
        h.status, h.dispatched_at = "dispatched", now()
        log.info("job %s -> %s on %s (prompt %s)", h.id[:8], h.runner.label, self.name, h.pid[:8])
        # With what the runner handed its backend, which the manager keeps for people to look at.
        await self._report(h, "dispatched", at=h.dispatched_at, prompt_id=h.pid, submitted=h.runner.submitted(h))
        if h.cancelling and not h.dropped:
            if await h.runner.cancel(h):
                await self._cancelled(h)

    # ---------------------------------------------------------------- endings
    async def run_failed(self, h: Held, message: str, traceback: str | None = None) -> None:
        if h.finished or h.dropped:
            return
        if h.notes:
            message = f"{message} ({'; '.join(h.notes)})"     # e.g. the card it had to share
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

    async def run_lost(self, h: Held, reason: str) -> None:
        if h.finished or h.dropped:
            return
        if h.pid and h.runner is not None:
            h.runner.give_up(h.pid)
        if h.rt is not None:
            h.rt = None
        self._release(h)
        S.cleanup(h.job, self.scratch_root)
        await self._report(h, "lost", reason=reason)

    async def _cancel(self, h: Held) -> None:
        """The manager cancelled this job: take it out of the runner."""
        if h.cancelling or h.finished:
            return
        h.cancelling = True
        if h.status == "preparing":
            if h.submitting:
                return                  # _submit takes it out of the runner once it's in
            if h.task and not h.task.done():
                h.task.cancel()
                await asyncio.gather(h.task, return_exceptions=True)
            await self._cancelled(h)
        elif h.status in ON_WORKER:
            if await h.runner.cancel(h):
                await self._cancelled(h)
            # otherwise the runner's interrupted event finishes it
        # postprocessing: it finishes

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
            if h.runner is not None:
                h.runner.give_up(h.attempt)     # in case its submit landed as we stopped it
            h.task.cancel()
            await asyncio.gather(h.task, return_exceptions=True)
        if h.pid and h.status in ON_WORKER and h.runner is not None:
            await h.runner.abandon(h.pid)
        self._release(h)
        if h.status != "postprocessing":
            S.cleanup(h.job, self.scratch_root)

    def _adopt(self, payload: dict) -> None:
        """A job the manager has on this agent that it didn't know about (it restarted):
        follow it again in the runner, prepare it again, or report it lost."""
        job = W.localize(payload, self.view)
        h = Held(job=job, attempt=job["attempt"], status=job.get("status") or "preparing", pid=job["attempt"],
                 dispatched_at=job.get("dispatched_at"), started_at=job.get("started_at"),
                 runner=self.runner_for(job["workflow"]))
        self.held[h.id] = h
        self.spawn(self._reattach(h))

    async def _reattach(self, h: Held) -> None:
        self._ensure_sync()
        if h.status == "postprocessing":
            await self.run_lost(h, f"{self.name} restarted while collecting the outputs.")
            return
        if h.runner is None:
            await self.run_lost(h, f"{self.name} has no workflow or process {h.job['workflow']} any more.")
            return
        where, result = await h.runner.locate(h)        # once the runner answers
        if self.held.get(h.id) is not h:
            return                      # dropped meanwhile
        was = h.status
        if where in ("running", "pending"):
            self._hold_slot(h)
            h.status = "running" if where == "running" else "dispatched"
            h.runner.follow(h)
            h.dispatched_at = h.dispatched_at or now()
            log.info("agent %s picked job %s up again in %s", self.name, h.id[:8], h.runner.label)
            if was == "preparing":
                await self._report(h, "dispatched", at=h.dispatched_at, prompt_id=h.pid)
            if h.status == "running" and was != "running":
                h.started_at = h.started_at or now()
                await self._report(h, "running", at=h.started_at)
        elif where == "finished":
            self._hold_slot(h)
            if was == "preparing":
                h.status = "dispatched"
                await self._report(h, "dispatched", at=h.dispatched_at or now(), prompt_id=h.pid)
            await h.runner.settle(h, result)
        elif was == "preparing":
            log.info("agent %s: job %s never reached %s: preparing it again", self.name, h.id[:8], h.runner.label)
            h.task = asyncio.create_task(self._prepare_and_submit(h), name=f"job-{h.id[:8]}")
        else:
            await self.run_lost(h, f"{h.runner.label} on {self.name} lost the job while {self.name} was restarting.")

    def _clean_orphan_scratch(self) -> None:
        """Scratch folders of jobs this agent doesn't hold (finished while it was down)."""
        if self.cfg.convert.keep_failed:
            return
        keep = set(self.held)
        S.clean_orphans(self.scratch_root, lambda name: name in keep)

    # ================================================================ what the runner reports (runners.base.Host)
    async def run_progress(self, h: Held, stage: str | None = None, force: bool = False) -> None:
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
        self.spawn(self._report(h, "progress", lossy=True, **data))

    async def run_started(self, h: Held) -> None:
        if h.status == "dispatched":
            h.status, h.started_at = "running", now()
            await self._report(h, "running", at=h.started_at, stage="starting")

    async def run_interrupted(self, h: Held) -> None:
        """The run was stopped on the machine: our cancel, or somebody's interrupt."""
        if h.cancelling:
            await self._cancelled(h)
        else:
            await self.run_failed(h, "Interrupted on the farm machine.")

    # ================================================================ environment
    async def send_environment(self) -> None:
        """Send the runner's last report of how its environment differs from the farm's."""
        report = self.runner.environment
        if report is None:
            return
        differs = list(report.get("differs") or [])
        if differs and differs != self.environment_differs:
            log.warning("%s on %s differs from the farm's: %s", self.runner.label, self.name, "; ".join(differs))
        self.environment_differs = differs
        if not self.registered:
            return                          # sent after hello
        try:
            reply = await self.link.environment({"name": self.name, "report": report})
        except LinkError:
            return                          # sent again after hello
        if reply.get("hello"):
            self.registered = False

    # ================================================================ outputs
    async def run_succeeded(self, h: Held, hist: dict | None = None) -> None:
        if h.status not in ON_WORKER or self.held.get(h.id) is not h:
            return                      # already handled (duplicate event, cancel, reconcile)
        h.status = "postprocessing"
        rt, h.rt = h.rt, None
        await self._report(h, "postprocessing")
        if self.held.get(h.id) is not h:
            return                      # the manager said it isn't ours any more
        try:
            if hist is None:
                hist = await h.runner.result(h)
            outputs, warnings = await self._collect_outputs(h, hist or {})
            warnings = [*h.notes, *warnings]
            t = now()
            started = h.started_at or h.dispatched_at or t
            nodes = rt.nodes() if rt else None
            S.cleanup(h.job, self.scratch_root)
            self._release(h)
            log.info("job %s done in %.0fs -> %s", h.id[:8], t - started, outputs.get("output_dir"))
            await self._report(h, "done", outputs=W.portable(outputs, self.view), warnings=warnings,
                               seconds=t - started, nodes=nodes or None, prepare_seconds=round(h.prepare_seconds, 2))
        except Exception as e:  # noqa: BLE001
            log.exception("collecting outputs for %s failed", h.id[:8])
            await self.run_failed(h, f"Finished on the GPU but collecting outputs failed: {e}")
        finally:
            self._release(h)
            await h.runner.forget(h.pid)

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
                self.spawn(self._report(h, "progress", lossy=True, status="postprocessing",
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

        if collect.get("preview"):         # True; a job queued before agents found their nodes has the section
            entries = h.runner.preview(h, hist)
            if entries and out_dir:
                e = entries[-1]
                dest = Path(out_dir) / (info.get("preview_name") or _DEFAULT_PATHS.preview_name).format(**tokens)
                try:
                    await h.runner.fetch(e, dest)
                    O.readable(str(dest))
                    outputs["preview"] = P.normalize(str(dest))
                    outputs["preview_posix"] = P.to_posix(outputs["preview"], mappings)
                except Exception as ex:  # noqa: BLE001
                    warnings.append(f"Couldn't copy the preview video: {ex}")
            elif not entries:
                warnings.append(f"{h.runner.label} returned no preview video.")

        # The preview movie of the frames (fileio.preview). Not for one frame, and never a
        # reason to fail the job.
        movie = collect.get("movie")
        if movie and out_dir and "preview" not in outputs and (outputs.get("frames") or 0) >= 2:
            await self._report(h, "progress", lossy=True, status="postprocessing", stage="making the preview movie")
            dest = str(Path(out_dir) / (info.get("preview_name") or _DEFAULT_PATHS.preview_name).format(**tokens))
            try:
                await PV.make_movie(outputs, movie, dest, self.cfg.convert)
                O.readable(dest)
                outputs["preview"] = P.normalize(dest)
                outputs["preview_posix"] = P.to_posix(outputs["preview"], mappings)
            except Exception as ex:  # noqa: BLE001
                log.warning("preview movie for %s: %s", h.id[:8], ex)
                warnings.append(f"Couldn't make the preview movie: {ex}")
        return outputs, warnings


__all__ = ["Agent", "Held"]
