"""The ComfyUI runner: one ComfyUI server, resident, models kept warm (base.Runner).

It submits a run's prompt with the attempt id as the prompt id, follows it over ComfyUI's
websocket, and checks ComfyUI's queue and history for what the events missed (a
reconnect, a ComfyUI restart). It also keeps this machine's ComfyUI itself: starts it when
agent.yaml says how (agent.launch), reads its node definitions and its environment
(comfyenv) for the manager, restarts it for new farm nodes, and syncs it to the farm's
environment file when the manager says (decision 0033).

ComfyUI loads the farm's own nodes (comfyui_nodes/) only at startup. When it runs other
ones than this checkout has (after a `git pull`), the agent takes no new jobs, and once
it holds none this runner restarts ComfyUI, if it started it (comfyui_launch).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

from ..agent import launch as L
from ..agent.launch import Launcher
from ..comfyenv import machine as EM
from ..comfyenv import spec as ES
from ..comfyenv import sync as Y
from ..config import AgentConfig
from ..fileio import farmnodes as FN
from ..fileio import wire as W
from ..fileio.ocio import REPO
from ..protocol import ON_WORKER, duration, now
from ..definitions import ParamError
from .comfyui_workflow import WorkflowRegistry, fit_to_options
from .base import Host, RunRejected
from .comfy import ComfyClient, ComfyRejected, describe_rejection

log = logging.getLogger("comfyfarm")


class SyncRefused(Exception):
    """Why this machine's ComfyUI isn't synced as the manager asked, said for a person."""


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

    def nodes(self) -> int | None:
        """How many nodes ran (for the manager's ETAs)."""
        return len(self.done | ({self.current} if self.current else set())) or None


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


class ComfyUIRunner:
    label = "ComfyUI"
    max_runs = None                     # agent.yaml's max_inflight: ComfyUI queues them itself

    def __init__(self, cfg: AgentConfig, client: ComfyClient | None = None):
        self.cfg = cfg
        self.name = cfg.name
        self.host: Host | None = None
        self.client = client or ComfyClient(cfg.comfyui)
        self.client_id = "comfyfarm-" + uuid.uuid5(uuid.NAMESPACE_URL, cfg.comfyui + cfg.name).hex[:12]
        self.launcher = Launcher(cfg.name, cfg.comfyui_launch, cfg.resolve(cfg.log_dir), cfg.base_dir)
        self.online = False
        self.ws_connected = False
        self.checked = False
        self.offline_since = now()
        self.stats: dict | None = None
        self.object_info: dict | None = None
        self.output_classes: set[str] | None = None
        self.queue_remaining: int | None = None
        self.error: str | None = None
        # ComfyUI runs other farm nodes than this checkout has: {"state": waiting | restarting
        # | manual, "message": why}; None while they're the checkout's.
        self.farm_nodes: dict | None = None
        # This machine's ComfyUI environment (comfyenv), sent to the manager: read when
        # ComfyUI (re)connects and every environment_seconds.
        self.environment: dict | None = None    # how it differs from the farm's file: the report for the manager
        self.env = ES.Cached()                  # this checkout's environment file, read again when it changes
        self._env_next = 0.0
        self._env_stale = True
        self._env_busy = False
        self._restarted_for: str | None = None   # the checkout's node version ComfyUI was restarted for
        self._restarting = False
        self._cant_restart = False               # its ComfyUI wasn't started by this agent
        self.abandoned: set[str] = set()         # prompt ids given up on: cancelled if seen again
        self._registry: WorkflowRegistry | None = None   # this checkout's workflows, read when first needed
        self._reconcile_lock = asyncio.Lock()

    # ================================================================ the agent's side
    def attach(self, host: Host) -> None:
        self.host = host

    def start(self) -> list:
        return [asyncio.create_task(self._health_loop(), name=f"health-{self.name}"),
                asyncio.create_task(self._ws_loop(), name=f"ws-{self.name}"),
                asyncio.create_task(self._reconcile_loop(), name=f"reconcile-{self.name}")]

    async def close(self) -> None:
        await self.client.close()

    @property
    def ready(self) -> bool:
        return bool(self.online and self.ws_connected and self.checked)

    def status(self) -> dict:
        return {"online": self.online, "connected": self.ws_connected, "stats": self.stats,
                "queue_remaining": self.queue_remaining, "error": self.error, "farm_nodes": self.farm_nodes}

    def _runtime(self, h) -> Runtime:
        try:
            prompt = self._prompt(h)
        except RunRejected:
            prompt = None               # a run found again whose graph can't be rebuilt: no node titles
        return Runtime(job_id=h.id, expected=h.job.get("expected_nodes") or 20, titles=_titles(prompt))

    def _workflows(self) -> dict:
        if self._registry is None:
            folder = self.cfg.workflows_dir
            self._registry = WorkflowRegistry(self.cfg.resolve(folder) if folder else Path(REPO) / "workflows")
        return self._registry.workflows

    def knows(self, process: str) -> bool:
        return process in self._workflows()

    def processes(self) -> dict:
        """What this machine can run, for the manager: each workflow of this checkout with its
        fingerprint, what its ComfyUI lacks for it (node packs, inputs, models), what is wrong
        with how the definition wires ComfyUI's graph (`broken`: the same on every machine with
        that fingerprint, so it disables the workflow on the manager), and how many nodes a run
        goes through."""
        oi = self.object_info or {}
        return {name: {"digest": wf.digest, "problems": wf.check_object_info(oi), "broken": wf.wiring_problems,
                       "expected_nodes": wf.expected_nodes(self.output_classes)}
                for name, wf in self._workflows().items()}

    def _prompt(self, h) -> dict:
        """The run's graph, built here from the job's request with this checkout's copy of the
        workflow (kept on the job, so it's built once)."""
        job = h.job
        if job.get("prompt") is None:
            request = job.get("request")
            if not request:             # a row from when the manager built and stored graphs
                raise RunRejected("This job was queued before the farm changed how it stores jobs, and can't "
                                  "run as it is. Submit it again.")
            wf = self._workflows().get(job["workflow"])
            if wf is None:
                raise RunRejected(f"{self.name} has no workflow {job['workflow']}. Update the farm's code on it.")
            try:
                # Paths written into the graph itself are the studio's: as this machine sees them,
                # like the rest of the job (which the agent localized when it took it).
                job["prompt"] = W.localize(wf.build_from(request), self.host.view)
            except ParamError as e:
                raise RunRejected(str(e)) from None
        return job["prompt"]

    def submitted(self, h) -> dict | None:
        """The API JSON this run was queued with (None for a run found again after a restart)."""
        return h.job.get("prompt")

    async def submit(self, h) -> None:
        """Put the run's prompt in ComfyUI's queue. Sets h.pid and h.rt."""
        prompt = fit_to_options(self._prompt(h), self.object_info)   # LTX\\x vs LTX/x on this OS
        titles = _titles(prompt)
        extra = {"comfyfarm_job": h.id, "comfyfarm_user": h.job.get("user")}
        while True:
            try:
                pid, node_errors = await self.client.submit(prompt, h.attempt, self.client_id, extra)
                break
            except ComfyRejected as e:
                raise RunRejected("ComfyUI rejected the job: " + describe_rejection(e.body, titles)) from None
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
                while not self.ready:
                    await asyncio.sleep(0.25)
        h.pid = pid
        if node_errors:
            try:
                await self.client.delete_queued([pid])
            except httpx.HTTPError:
                self.abandoned.add(pid)
            body = {"error": {"message": "Some outputs failed validation"}, "node_errors": node_errors}
            raise RunRejected("ComfyUI rejected the job: " + describe_rejection(body, titles))
        h.rt = Runtime(job_id=h.id, expected=h.job.get("expected_nodes") or 20, titles=titles)

    async def cancel(self, h) -> bool:
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

    def give_up(self, pid: str) -> None:
        self.abandoned.add(pid)

    async def abandon(self, pid: str) -> None:
        """Take a run nobody wants any more out of ComfyUI."""
        self.abandoned.add(pid)
        try:
            running, pending = await self.client.queue()
            if pid in pending:
                await self.client.delete_queued([pid])
            elif pid in running:
                await self.client.interrupt(pid)
        except httpx.HTTPError:
            pass                    # reconcile cancels it when ComfyUI is back

    async def locate(self, h) -> tuple[str, dict | None]:
        """Where a run the agent was handed back is, once ComfyUI answers: running, pending,
        finished (with its history entry) or absent."""
        while True:
            while not (self.online and self.ws_connected):
                await asyncio.sleep(0.25)
            try:
                running, pending = await self.client.queue()
                hist = None if h.pid in running or h.pid in pending else await self.client.history(h.pid)
                break
            except httpx.HTTPError:
                await asyncio.sleep(1.0)
        where = "running" if h.pid in running else "pending" if h.pid in pending else "finished" if hist else "absent"
        return where, hist

    def follow(self, h) -> None:
        h.rt = self._runtime(h)

    async def settle(self, h, hist: dict) -> None:
        """A run that ended in ComfyUI while we weren't listening."""
        if h.finished or h.dropped:
            return
        status = hist.get("status") or {}
        if status.get("status_str") == "success" or (status.get("completed") and status.get("status_str") != "error"):
            await self.host.run_succeeded(h, hist)
            return
        message, interrupted, tb = _history_failure(hist)
        if interrupted:
            await self.host.run_interrupted(h)
        else:
            await self.host.run_failed(h, message, traceback=tb)
        self.host.spawn(self.forget(h.pid))

    async def result(self, h) -> dict | None:
        """The finished run's history entry (written just after the success event)."""
        hist = None
        for _ in range(10):
            hist = await self.client.history(h.pid)
            if hist:
                break
            await asyncio.sleep(0.5)
        return hist

    def warnings(self, h, hist: dict | None) -> list[str]:
        return []                       # what goes wrong in ComfyUI fails the run, and the agent checks the frames

    def preview(self, h, hist: dict | None) -> list:
        """The preview video the graph made itself (its definition's outputs.preview), as
        ComfyUI lists the files: the node is this checkout's, the copy the run was built from."""
        wf = self._workflows().get(h.job.get("workflow"))
        pv = (wf.outputs.get("preview") if wf else None) or {}
        if not pv.get("node"):
            return []
        node_out = ((hist or {}).get("outputs") or {}).get(str(pv["node"])) or {}
        return node_out.get("gifs") or node_out.get("videos") or node_out.get("images") or []

    def files(self, h, hist: dict | None, kind: str, spec: dict) -> list:
        """What the run wrote of a kind on the generic route (decision 0036): the files ComfyUI
        lists for the node this checkout's definition names (runner.outputs.<kind>.node)."""
        wf = self._workflows().get(h.job.get("workflow"))
        node = ((wf.outputs.get(kind) if wf else None) or {}).get("node")
        if not node:
            return []
        node_out = ((hist or {}).get("outputs") or {}).get(str(node)) or {}
        return [e for v in node_out.values() if isinstance(v, list) for e in v
                if isinstance(e, dict) and e.get("filename")]

    async def fetch(self, entry: dict, dest) -> None:
        await self.client.download(entry["filename"], entry.get("subfolder", ""), entry.get("type", "output"), dest)

    async def forget(self, pid: str | None) -> None:
        if not pid or not self.cfg.delete_comfy_history:
            return
        try:
            await self.client.delete_history([pid])
        except httpx.HTTPError:
            pass

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
                await self.host.send_status()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _on_message(self, msg: dict) -> None:
        kind, data = msg.get("type"), msg.get("data") or {}
        if kind == "status":
            self.queue_remaining = ((data.get("status") or {}).get("exec_info") or {}).get("queue_remaining")
            return
        pid = data.get("prompt_id")
        if not pid or pid in self.abandoned:
            return
        host = self.host
        h = host.by_pid(pid)
        if h is None or h.status not in ON_WORKER:
            return
        rt = h.rt
        if kind == "execution_start":
            await host.run_started(h)
        elif kind == "execution_cached" and rt:
            rt.done.update(data.get("nodes") or [])
            await host.run_progress(h, "using cached results")
        elif kind == "executing" and rt:
            node = data.get("node")
            await host.run_started(h)
            if rt.current and rt.current != node:
                rt.done.add(rt.current)
            rt.current, rt.frac = node, 0.0
            if node is None:            # older ComfyUI signals completion this way
                host.spawn(host.run_succeeded(h))
            else:
                await host.run_progress(h, rt.titles.get(node, node), force=True)
        elif kind == "progress" and rt:
            node = data.get("node")
            if node and node != rt.current:
                if rt.current:
                    rt.done.add(rt.current)
                rt.current = node
            mx = data.get("max") or 0
            rt.frac = (data.get("value") or 0) / mx if mx else 0.0
            await host.run_progress(h, f"{rt.titles.get(rt.current, rt.current)} {data.get('value')}/{mx}")
        elif kind == "executed" and rt:
            if data.get("node"):
                rt.done.add(data["node"])
        elif kind == "execution_success":
            host.spawn(host.run_succeeded(h))
        elif kind == "execution_error":
            title = rt.titles.get(str(data.get("node_id")), "") if rt else ""
            message = (f"{data.get('node_type')} (node {data.get('node_id')}"
                       f"{' ' + repr(title) if title else ''}): {str(data.get('exception_message', '')).strip()}")
            await host.run_failed(h, message, traceback="".join(data.get("traceback") or []) or None)
            host.spawn(self.forget(pid))
        elif kind == "execution_interrupted":
            await host.run_interrupted(h)
            host.spawn(self.forget(pid))

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

    def _mine(self) -> list:
        """The agent's runs that are ComfyUI's. Another runner's (a process) never reached
        ComfyUI: it isn't ComfyUI's to find in its queue, or to lose (decision 0025)."""
        return [h for h in self.host.runs() if getattr(h, "runner", None) in (self, None)]

    async def _reconcile_once(self) -> None:
        host = self.host
        t0 = now()
        runs = [h for h in self._mine() if h.status in ON_WORKER and (h.dispatched_at or 0) < t0 - 2]
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
            if not host.holds(h) or h.status not in ON_WORKER:
                continue                # dropped or finished meanwhile
            if h.pid in running:
                await host.run_started(h)
                if h.rt is None:
                    h.rt = self._runtime(h)
                continue
            if h.pid in pending:
                continue
            hist = await self.client.history(h.pid)
            if not host.holds(h) or h.status not in ON_WORKER:
                continue
            if hist:
                host.spawn(self.settle(h, hist))
                continue
            await host.run_lost(h, f"ComfyUI on {self.name} lost the job (restarted?).")

    # ================================================================ ComfyUI: health
    async def _lost_with_comfyui(self) -> None:
        """ComfyUI unreachable too long: its runs are lost. Not another runner's."""
        for h in [h for h in self._mine() if h.status in ON_WORKER]:
            await self.host.run_lost(h, f"ComfyUI on {self.name} was unreachable for over "
                                        f"{duration(self.cfg.lost_after_seconds)}.")

    async def _health_loop(self) -> None:
        host = self.host
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
                    await self._lost_with_comfyui()
            try:
                self.launcher.tick(reachable)   # starts ComfyUI if agent.yaml says how and it isn't running
            except Exception:  # noqa: BLE001
                log.exception("starting ComfyUI on %s failed", self.name)
            if not self._hold_tick():
                try:
                    await self._farm_nodes_tick(reachable)
                except Exception:  # noqa: BLE001
                    log.exception("checking the farm nodes on %s failed", self.name)
            if reachable and self.object_info is not None and not self._env_busy and \
                    (self._env_stale or now() >= self._env_next):
                host.spawn(self.check_environment())
            if host.registered:
                await host.send_status()
            await asyncio.sleep(self.cfg.health_seconds)

    def _hold_tick(self) -> bool:
        """While `comfyfarm comfyui sync` updates ComfyUI (the launcher's hold file), the
        agent takes no jobs and says why; True then. Once the hold is gone, ComfyUI is
        started again and its environment read again."""
        hold = self.launcher.held()
        if hold:
            if (self.farm_nodes or {}).get("state") != "syncing":
                log.warning("agent %s takes no new jobs: %s", self.name, hold)
            self.farm_nodes = {"state": "syncing", "message": hold}
            return True
        if (self.farm_nodes or {}).get("state") == "syncing":
            log.info("ComfyUI on %s was updated: it's started again", self.name)
            self.farm_nodes, self._env_stale = None, True
        return False

    async def _farm_nodes_tick(self, reachable: bool) -> None:
        """After every health check: a `git pull` may have changed the farm nodes; once
        ComfyUI runs older ones and this agent holds no jobs, restart it."""
        if not (reachable and self.ws_connected and self.object_info is not None) or self._restarting:
            return
        was = self.farm_nodes
        if self._check_farm_nodes():
            if was and self.host.registered:     # the checkout's again (it went back): tell the manager
                await self.host.send_nodes()
        elif self.farm_nodes["state"] == "waiting" and not self.host.runs() and not self.queue_remaining:
            await self._restart_comfyui()           # idle: no farm job, and nothing run by hand either

    def _check_farm_nodes(self) -> bool:
        """Whether ComfyUI runs this checkout's farm nodes (it has none at all counts too:
        the workflow checks say what's missing). Sets self.farm_nodes when it doesn't."""
        why = FN.stale(self.object_info or {}, REPO)
        if why is None:
            if self.farm_nodes:
                log.info("ComfyUI on %s runs this checkout's farm nodes", self.name)
            self.farm_nodes = None
            return True
        if self._restarted_for == FN.version(REPO):
            state, msg = "manual", (why + " Restarting ComfyUI didn't change that: check that ComfyUI's "
                                    "custom_nodes/comfyfarm_nodes links to this checkout (the setup script makes "
                                    "the link), then restart ComfyUI.")
        elif self.launcher.enabled and not self._cant_restart:
            state, msg = "waiting", why + " ComfyUI will be restarted to load them once its jobs are done."
        else:
            state, msg = "manual", why + (" This agent didn't start ComfyUI, so it can't restart it:"
                                          if self._cant_restart else "") + " Restart ComfyUI to load them."
        if (self.farm_nodes or {}).get("message") != msg:
            log.warning("agent %s takes no new jobs: %s", self.name, msg)
        self.farm_nodes = {"state": state, "message": msg}
        return False

    async def _restart_comfyui(self) -> None:
        """Restart ComfyUI for this checkout's farm nodes (the agent holds no jobs)."""
        want = FN.version(REPO)
        self._restarting, self._restarted_for = True, want
        self.farm_nodes = {"state": "restarting",
                           "message": f"Restarting ComfyUI to load this checkout's farm nodes (version {want})."}
        if self.host.registered:
            await self.host.send_status()
        try:
            ok = await asyncio.to_thread(self.launcher.restart)
        finally:
            self._restarting = False
        if not ok:
            self._restarted_for, self._cant_restart = None, True
            self._check_farm_nodes()
            return
        self._cant_restart = False
        self.object_info = None         # read again when it's back (its websocket reconnects)

    # ================================================================ ComfyUI: VRAM
    RELEASED = 2**30                    # torch holding less than this counts as released
    RELEASE_SECONDS = 30.0              # how long ComfyUI gets to unload

    @staticmethod
    def _held(stats: dict | None) -> int | None:
        """What ComfyUI's torch holds on its card (torch_vram_total: reserved, models and cache)."""
        for dev in (stats or {}).get("devices") or []:
            held = dev.get("torch_vram_total")
            return int(held) if isinstance(held, (int, float)) else None
        return None

    async def release(self) -> dict:
        """Unload ComfyUI's models and empty its cache (ComfyUI's /free), and wait until its
        torch holds less than RELEASED. Only when its queue is empty: ComfyUI frees between
        prompts, and a prompt queued after would load the models again."""
        if not self.online:
            return {"ok": False, "before": None, "after": None, "error": "ComfyUI isn't running"}
        before = self._held(self.stats)
        try:
            running, pending = await self.client.queue()
            if running or pending:
                return {"ok": False, "before": before, "after": before,
                        "error": f"ComfyUI has {len(running | pending)} prompt(s) running or queued: free its "
                                 f"VRAM once they're done"}
            await self.client.free()
            after = before
            deadline = asyncio.get_running_loop().time() + self.RELEASE_SECONDS
            while True:
                self.stats = await self.client.system_stats()
                after = self._held(self.stats)
                if after is not None and after < self.RELEASED:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    gb = f"{after / 2**30:.1f} GB" if after is not None else "an unknown amount"
                    return {"ok": False, "before": before, "after": after,
                            "error": f"ComfyUI still holds {gb} after {self.RELEASE_SECONDS:g} s"}
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "before": before, "after": None, "error": f"ComfyUI didn't free its VRAM: {e}"}
        log.info("worker %s: ComfyUI freed its VRAM (%s -> %s GiB)", self.name,
                 f"{before / 2**30:.1f}" if before is not None else "?", f"{after / 2**30:.1f}")
        return {"ok": True, "before": before, "after": after, "error": None}

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
        self._env_stale = True              # ComfyUI (re)started: read its environment again
        # Other farm nodes than the checkout's: the manager gets no node definitions, so it
        # gives this agent no jobs until ComfyUI runs the checkout's (_farm_nodes_tick).
        if self._check_farm_nodes() and self.host.registered:
            await self.host.send_nodes()
        self.checked = True
        if self.host.registered:
            await self.host.send_status()

    # ================================================================ environment
    def _environment_report(self, spec: dict | None, farm_paths: list[dict], state: dict) -> dict:
        """What the manager shows on the Machines tab: plain text, nothing it has to understand."""
        devices = (self.stats or {}).get("devices") or [{}]
        # Workflows whose manifest keeps them off this machine (requires): their packs and models don't count.
        self._workflows()
        skip = ES.excluded(self._registry, devices[0].get("vram_total"), self.host.tags)
        result = ES.compare(spec, state, farm_paths, skip=skip)
        core = state.get("comfyui") or {}
        return {"checked_at": state.get("collected_at"), "file": spec is not None,
                "comfyui": {"version": core.get("version"), "commit": core.get("commit")},
                "differs": [d["message"] for d in result["differs"]], "notes": result["notes"]}

    async def check_environment(self) -> dict | None:
        """Read this machine's ComfyUI (version and launch flags, Python packages, driver,
        node packs, extra_model_paths.yaml, the farm's models), compare it with this
        checkout's copy of the farm's environment file (comfyenv), and send the manager
        what differs. Returns what was read."""
        if self._env_busy:
            return None
        self._env_busy, self._env_stale = True, False
        state = None
        try:
            spec, farm_paths = self.env.get()
            folder = EM.comfyui_folder(self.cfg)
            if folder and not os.path.isabs(folder):
                folder = str(self.cfg.resolve(folder))
            argv = list(self.cfg.comfyui_launch.command[1:])
            state = await asyncio.to_thread(EM.collect, folder, EM.comfyui_python(self.cfg), self.stats, argv,
                                            **ES.wanted(spec))
            self.environment = self._environment_report(spec, farm_paths, state)
            await self.host.send_environment()
        except Exception:  # noqa: BLE001
            log.exception("reading the ComfyUI environment on %s failed", self.name)
        finally:
            self._env_busy = False
            self._env_next = now() + self.cfg.environment_seconds
        return state

    # ================================================================ environment: the sync
    SYNC_POLL = 1.0                     # seconds between looks at ComfyUI coming back

    async def sync(self, order: dict, say) -> dict:
        """Bring this machine's ComfyUI to the farm's environment file when the manager says,
        as `comfyfarm comfyui sync --apply` does by hand (decision 0033). `say` gets how it
        goes after each change (the agent sends it with its status); returns how it ended.

        This checkout's file must be the version the manager names (`file`). Nothing the
        sync does itself: done, ComfyUI untouched. Else, with no farm job in ComfyUI and none
        in its queue: the hold in place (no jobs; the launcher leaves ComfyUI down), ComfyUI
        stopped, the steps done in order until one fails, the hold gone, ComfyUI started
        again and waited for until its node definitions are read, then compared again."""
        rec = {"id": str(order.get("id") or ""), "state": "checking", "since": now(), "step": None,
               "steps": [], "differs": [], "manual": [], "note": None, "error": None}

        async def tell(**changes) -> None:
            rec.update(changes)
            await say(json.loads(json.dumps(rec)))      # a copy: the steps go on growing

        await tell()
        try:
            await self._sync(order, rec, tell)
        except SyncRefused as e:
            await tell(state="failed", step=None, error=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("syncing ComfyUI on %s failed", self.name)
            await tell(state="failed", step=None, error=f"the sync failed: {e}")
        return json.loads(json.dumps(rec))

    async def _sync(self, order: dict, rec: dict, tell) -> None:
        if not self.cfg.envs.managed:
            raise SyncRefused("envs.managed is off in this machine's agent.yaml: the manager changes none of its "
                              "environments. Turn it on, or sync it by hand (comfyfarm comfyui sync --apply)")
        here = ES.fingerprint(self.env.path, self.env.model_paths_path)
        if here is None:
            raise SyncRefused(f"this checkout has no farm environment file ({self.env.path}): update it (git pull)")
        if order.get("file") and here != order["file"]:
            raise SyncRefused(f"this machine's checkout has another version of the farm's ComfyUI environment ({here}) "
                              f"than the manager's host ({order['file']}): update its checkout (git pull), then start "
                              f"the sync again")
        if not self.launcher.enabled:
            raise SyncRefused("this agent doesn't start ComfyUI (comfyui_launch in agent.yaml), so it can't stop it "
                              "and start it again: sync it by hand (comfyfarm comfyui sync --apply)")
        force = bool(order.get("force"))
        plan = await self._sync_plan(force)
        if not plan.differs:
            await tell(state="done", note="it matches the farm's ComfyUI: nothing to do")
            return
        if not plan.steps:
            await tell(state="done", differs=[d["message"] for d in plan.differs], manual=plan.manual,
                       note="nothing the sync does itself: the rest is by hand")
            return
        if self._mine():
            raise SyncRefused(f"it holds {len(self._mine())} ComfyUI job(s): drain it and wait for them first")
        if self.online:
            running, pending = await self.client.queue()
            if running or pending:
                raise SyncRefused(f"ComfyUI has {len(running | pending)} prompt(s) running or queued that aren't the "
                                  f"farm's: wait for them, then start the sync again")
            if self.launcher.running_pid() is None:
                raise SyncRefused("ComfyUI runs here, but this agent didn't start it, so it won't stop it: stop it "
                                  "yourself (the agent then starts it, comfyui_launch), then start the sync again")
        failed = None
        L.write_hold(self.launcher.hold_file, f"The manager is syncing ComfyUI to the farm's environment (sync "
                                              f"{rec['id']}, since {time.strftime('%H:%M')}): no jobs until it's done.")
        self._hold_tick()
        try:
            await tell(state="stopping", step="stopping ComfyUI")
            pid = await asyncio.to_thread(self.launcher.stop)
            rec["steps"].append(["stop ComfyUI", f"stopped (pid {pid})" if pid else "it wasn't running"])
            self.object_info, self.checked = None, False        # read again once it is back
            for i, step in enumerate(plan.steps, 1):
                await tell(state="applying", step=f"{i}/{len(plan.steps)}: {step.title}")
                try:
                    out = await asyncio.to_thread(step.run) if step.run else "by hand"
                except Exception as e:  # noqa: BLE001 - a SyncError, or a file that couldn't be moved
                    rec["steps"].append([step.title, f"failed: {e}"])
                    failed = f"{step.title} failed:\n{e}"
                    break
                rec["steps"].append([step.title, out or "done"])
        finally:
            L.release_hold(self.launcher.hold_file)
            self._hold_tick()
            if self.launcher.running_pid() is None:
                self.launcher.start()                           # now, not restart_after later
        wait = self.cfg.comfyui_launch.startup_seconds + 60
        await tell(state="starting", step="starting ComfyUI again")
        if not await self._back(wait):
            raise SyncRefused((f"{failed}\n" if failed else "") + f"ComfyUI didn't come back within {wait:.0f} s "
                              f"after the sync: see {self.launcher.log_file}")
        after = await self._sync_plan(force)
        self._env_stale = True                                  # the Machines tab's comparison, read again
        left = {"differs": [d["message"] for d in after.differs], "manual": after.manual}
        if failed:
            await tell(state="failed", step=None, error=f"{failed}\nThe steps after it weren't done; ComfyUI was "
                                                        f"started again.", **left)
        else:
            await tell(state="done", step=None, note="synced" if not after.differs else
                       "synced; what is left is by hand", **left)

    async def _sync_plan(self, force: bool):
        """What the sync would do now, from this machine read as `comfyfarm comfyui sync` reads it."""
        spec, farm_paths = self.env.get()
        if spec is None:
            raise SyncRefused(f"this checkout has no farm environment file ({self.env.path}): update it (git pull)")
        folder = EM.comfyui_folder(self.cfg)
        if folder and not os.path.isabs(folder):
            folder = str(self.cfg.resolve(folder))
        python = EM.comfyui_python(self.cfg)
        state = await asyncio.to_thread(EM.collect, folder, python, self.stats if self.online else None,
                                        list(self.cfg.comfyui_launch.command[1:]), **ES.wanted(spec))
        if state.get("error"):
            raise SyncRefused(f"can't read this machine's ComfyUI: {state['error']}")
        self._workflows()
        devices = (self.stats or {}).get("devices") or [{}]
        skip = ES.excluded(self._registry, devices[0].get("vram_total") or state.get("gpu_memory"), self.host.tags)
        return Y.plan(spec, state, farm_paths, skip, comfyui_dir=folder, python=python, force=force,
                      index_url=Y.PYPI, farm_file=self.env.model_paths_path)

    async def _back(self, wait: float) -> bool:
        """ComfyUI answering again, its events followed and its node definitions read, within `wait` s."""
        end = now() + wait
        while now() < end:
            if self.online and self.ws_connected and self.checked and self.object_info is not None:
                return True
            await asyncio.sleep(self.SYNC_POLL)
        return False


__all__ = ["ComfyUIRunner", "Runtime", "SyncRefused"]
