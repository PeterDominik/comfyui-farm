"""The manager's desk: where agents check in, take jobs and report (protocol.py).

Agents call it; it never calls them. Each job an agent takes is one run of it, named by an
attempt id (also its prompt id in ComfyUI); the desk acts on a report only while that run
is the job's current one. Jobs are leased to the agent holding them: every check-in
renews the lease, and jobs whose lease runs out (farm.lost_after_seconds) are queued again.

Which machine runs a job (_claim): the best free GPU that can run its workflow (its nodes
and models, and its manifest's `requires`: least VRAM, tags). The best is the one that
should run the job fastest: past runs of its workflow on that machine's GPU type, for the
job's size (manager.eta), so LTX goes to the 5090 before the A6000 though the A6000 has
more VRAM. GPU types that haven't run the workflow yet come after those that have, ranked
by agents.order in the manager's config, then by VRAM (as every machine is for a workflow
that has never run). Between equal cards, the one that last ran the workflow (its models
still loaded) goes first. A machine gets a second job, prepared ahead of its current one,
only when every GPU is busy. A machine drained from the dashboard takes no new jobs until
it's resumed.

The desk keeps what agents tell it about their ComfyUI in memory (a manager restart
forgets it; agents check in again). Jobs live in the database as before, with their
paths as this machine sees them; between machines they travel in the portable form of
fileio.wire."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field

from ..fileio import paths as P
from ..fileio import wire as W
from ..fileio.ocio import REPO
from ..protocol import HELD, ON_WORKER, build, duration, now
from . import eta as E
from .farm import Farm
from .rollouts import Rollouts
from .syncs import Syncs

log = logging.getLogger("comfyfarm")

# An agent that has called within this many seconds is alive. Its check-ins are held for
# up to its heartbeat_wait (10 s), so this leaves room for two.
ALIVE_SECONDS = 30.0
LEASE_CHECK_SECONDS = 5.0
# A machine's GPU is taken from when it gets a job until ComfyUI is done with it. Collecting
# the outputs (postprocessing) leaves the GPU free for the next job.
GPU_BUSY = ("preparing", "dispatched", "running")
# Jobs a machine holds for its GPU: the one it runs, and one prepared ahead.
PER_GPU = 2
# A job waiting this long for the machine it's meant for (one that stopped checking in but
# isn't counted out yet) goes to the next machine with room.
STEAL_SECONDS = 20.0


@dataclass
class AgentState:
    """What the desk knows about one agent and its ComfyUI."""
    name: str
    version: str | None = None
    url: str | None = None              # its ComfyUI, as the agent sees it
    max_inflight: int = 2               # jobs in its ComfyUI at once
    capacity: int = 3                   # jobs it holds at once (one more prepared ahead)
    last_seen: float | None = None
    seq: int = 0                        # its last check-in
    online: bool = False                # ComfyUI reachable
    connected: bool = False             # ComfyUI's event websocket
    checked: bool = False               # its node definitions received and checked
    stats: dict | None = None
    queue_remaining: int | None = None
    error: str | None = None
    problems: dict[str, list[str]] = field(default_factory=dict)
    expected: dict[str, int] = field(default_factory=dict)   # nodes a run of each workflow goes through, as it reports
    want_nodes: bool = False            # ask it for its node definitions again
    farm_nodes: dict | None = None      # its ComfyUI runs other farm nodes than its checkout (agent.py)
    was_alive: bool = False
    last_claimed: float = 0.0           # identical cards take turns
    last_workflow: str | None = None    # its models are probably still loaded
    tags: list[str] = field(default_factory=list)   # agent.yaml's tags and its OS (requires.tags)
    environment: dict | None = None     # how its backend differs from the farm's, as its agent reported it
    gpus: list[dict] | None = None      # its GPUs as its agent read them with nvidia-smi (gpu_probe); None: not read
    gpu_error: str | None = None        # why they couldn't be read
    want_release: bool = False          # ask it to free its VRAM (the Machines tab's Free VRAM)
    released: dict | None = None        # the last time it did, and how it went (decision 0023)
    envs: dict | None = None            # the runner environments it has built (decision 0029)
    env_wanted: dict = field(default_factory=dict)   # the releases it should have (decision 0030)
    want_envs: bool = False             # send it env_wanted in its next check-in reply
    env_work: dict | None = None        # what its environment manager is doing about them, as it says
    want_sync: dict | None = None       # tell it to sync its runner's environment in its next reply (0033)
    synced: dict | None = None          # how its last sync goes, as its agent says


def wanted_hash(wanted: dict) -> str | None:
    """A short name for one version of what a machine should have (None: nothing)."""
    if not wanted:
        return None
    return hashlib.sha256(json.dumps(wanted, sort_keys=True).encode()).hexdigest()[:12]


def vram(a: AgentState) -> int:
    """Its GPU's memory in bytes (0 until it has reported it)."""
    for dev in (a.stats or {}).get("devices") or []:
        try:
            return int(dev.get("vram_total") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def vram_gb(a: AgentState) -> int:
    """Its GPU's memory as the card is sold: a 5090 reports 31.8 GiB, and is 32."""
    return round(vram(a) / 2**30)


class Desk:
    def __init__(self, farm: Farm):
        self.farm = farm
        self.store = farm.store
        self.cfg = farm.cfg
        farm.desk = self
        self.agents: dict[str, AgentState] = {}
        # job id -> (agent, attempt, the check-in it was given in reply to): the agent must
        # list it from its next check-in on, or it never got it.
        self._offered: dict[str, tuple[str, str, int]] = {}
        # job id -> (the machine it's meant for, since when): another machine may take it
        # after STEAL_SECONDS.
        self._passed: dict[str, tuple[str, float]] = {}
        self._order: tuple[str, ...] = ()
        # Machines drained from the dashboard (kept in the database): name -> {by, since}.
        self.drained: dict[str, dict] = self.store.drained()
        self._changed: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        self.view = W.View(mappings=self.cfg.paths.mappings, scratch=farm.scratch_root, repo=P.normalize(str(REPO)))
        self.rollouts = Rollouts(self)      # environment releases going to the machines (decision 0031)
        self.syncs = Syncs(self)            # a runner's environment synced on them in turn (decision 0033)

    @property
    def lease_seconds(self) -> float:
        return self.cfg.farm.lost_after_seconds

    # ================================================================ lifecycle
    async def start(self) -> None:
        self._changed = asyncio.Event()
        # Agents get a full lease to check in again: the manager was away, not them.
        n = self.store.extend_leases(now() + self.lease_seconds)
        if n:
            log.info("%d job(s) on agents: waiting for them to check in", n)
        self._tasks.append(asyncio.create_task(self._lease_loop(), name="leases"))
        self._tasks.append(asyncio.create_task(self.rollouts.loop(), name="rollouts"))
        self._tasks.append(asyncio.create_task(self.syncs.loop(), name="syncs"))
        if self.cfg.gpu_history.days > 0:
            self._tasks.append(asyncio.create_task(self._gpu_loop(), name="gpu history"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def wake(self) -> None:
        """Re-evaluate the check-ins that are waiting (new jobs, cancels). Thread-safe."""
        loop = self.farm.loop
        if loop is None or self._changed is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._bump()
        else:
            loop.call_soon_threadsafe(self._bump)

    def _bump(self) -> None:
        ev, self._changed = self._changed, asyncio.Event()
        ev.set()

    async def _wait(self, timeout: float) -> None:
        ev = self._changed
        if ev is None or timeout <= 0:
            return
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    # ================================================================ agents
    def alive(self, name: str | None) -> bool:
        a = self.agents.get(name or "")
        return bool(a and a.last_seen and now() - a.last_seen < ALIVE_SECONDS)

    def usable(self, a: AgentState) -> bool:
        """Alive, with its ComfyUI up, running its checkout's farm nodes, and its node
        definitions checked."""
        return self.alive(a.name) and a.online and a.connected and a.checked and not a.farm_nodes

    def accepting(self, a: AgentState) -> bool:
        """Usable and not drained: it may take jobs."""
        return self.usable(a) and a.name not in self.drained

    def unmet(self, a: AgentState, workflow: str) -> str | None:
        """Why the workflow's manifest keeps it off this machine (requires), or None."""
        try:
            req = self.farm.registry.get(workflow).requires
        except Exception:  # noqa: BLE001 - an unknown workflow is the plan's to refuse
            return None
        need = req.get("vram_gb")
        if need:
            if not vram(a):
                return f"needs a GPU with {need:g} GB of VRAM (this machine hasn't reported its GPU yet)"
            if vram_gb(a) < need:
                return f"needs a GPU with {need:g} GB of VRAM (this one has {vram_gb(a)})"
        missing = [t for t in req.get("tags") or [] if t not in a.tags]
        if missing:
            return f"needs the tag{'s' if len(missing) > 1 else ''} {', '.join(missing)} (agent.yaml tags)"
        return None

    def requires_note(self, workflow: str) -> str | None:
        """For the plan: when no machine the farm knows may run the workflow now (requires,
        drained, offline), the job will wait; say so."""
        if not self.agents:
            return None
        fit = [a for a in self.agents.values() if self.unmet(a, workflow) is None]
        if not fit:
            why = self.unmet(next(iter(self.agents.values())), workflow)
            return f"No machine on the farm can take it now: it {why}. The job waits until one checks in."
        if not any(self.accepting(a) for a in fit):
            names = ", ".join(a.name for a in fit)
            state = "drained" if all(a.name in self.drained for a in fit) else "offline or drained"
            return f"The machines that can run it ({names}) are {state}: the job waits until one takes jobs."
        return None

    def request_release(self, name: str, by: str | None = None) -> dict:
        """Ask a machine's runner to free its VRAM; its agent does so when the farm has nothing
        running in it, and reports how it went with its status (`released`)."""
        a = self.agents[name]
        a.want_release = True
        log.info("worker %s: %s asked it to free its VRAM", name, by or "?")
        self.wake()
        return {"worker": name, "asked": True}

    def set_drained(self, name: str, on: bool, by: str | None = None) -> dict:
        """Drain a machine (it finishes what it holds and takes nothing new) or resume it.
        Kept in the database, so it lasts across manager restarts."""
        self.store.set_drained(name, by, on)
        if on:
            self.drained[name] = {"by": by, "since": now()}
            log.info("worker %s drained by %s: no new jobs", name, by or "?")
        else:
            self.drained.pop(name, None)
            log.info("worker %s resumed by %s", name, by or "?")
            self.wake()
        a = self.agents.get(name)
        if a is not None:
            self._publish(a)
        return {"worker": name, "drained": on}

    def online_count(self) -> int:
        return sum(1 for a in self.agents.values() if self.accepting(a))

    def run_estimate(self, a: AgentState, workflow: str, size: float, stats: dict) -> float | None:
        """Seconds a job of `workflow` with `size` frames (tiles) should take on this machine,
        from its GPU type's own past runs of it (manager.eta; `stats`: store.run_stats()), or
        None while that GPU type hasn't run it."""
        return E.estimate(stats.get((workflow, self.gpu_type(a))), size)

    def _rank_key(self, a: AgentState, turns: bool = True, workflow: str | None = None, size: float = 1,
                  stats: dict | None = None) -> tuple:
        """Better machines first. For a job (its `workflow` and `size`, with the run `stats`):
        the one that should run it fastest, from its GPU type's past runs of the workflow
        (run_estimate), then those whose GPU type hasn't run it yet. Then (and without a job,
        or for a workflow that has never run): those in agents.order, in its order, then by
        VRAM. Between equal ones (the same GPU type), the one that last ran `workflow` (its
        models still loaded), then turns (the one that took a job longest ago first)."""
        est = self.run_estimate(a, workflow, size, stats) if workflow and stats is not None else None
        order = [n.lower() for n in self.cfg.agents.order]
        name = a.name.lower()
        listed = order.index(name) if name in order else len(order)
        cold = 0 if workflow and a.last_workflow == workflow else 1
        return est is None, est or 0.0, listed, -vram_gb(a), cold, a.last_claimed if turns else 0.0, name

    def ranks(self) -> dict[str, int]:
        """Each machine's place in the order it gets jobs in (1 = first), all free, for a
        workflow that hasn't run on their GPU types yet: agents.order, then VRAM. A workflow
        with run times goes to its fastest GPU type first (Farm.run_times' `order`)."""
        ranked = sorted(list(self.agents.values()), key=lambda x: self._rank_key(x, turns=False))
        return {a.name: i + 1 for i, a in enumerate(ranked)}

    def summary(self, a: AgentState, ranks: dict[str, int] | None = None) -> dict:
        """One worker as /api/v1/workers and the dashboard show it."""
        gpu = None
        for dev in (a.stats or {}).get("devices", []) or []:
            gpu = {"name": dev.get("name"), "vram_total": dev.get("vram_total"), "vram_free": dev.get("vram_free")}
            break
        alive = self.alive(a.name)
        running = self.store.on_worker(a.name, ("running",))
        error = a.error
        if not alive:
            error = "The agent hasn't checked in" + (f" for {now() - a.last_seen:.0f} s." if a.last_seen else ".")
        return {
            "name": a.name, "url": a.url, "online": alive and a.online, "connected": alive and a.connected,
            "enabled": True, "max_inflight": a.max_inflight, "inflight": self.store.count_on_worker(a.name),
            "running_job": running[0]["id"] if running else None, "queue_remaining": a.queue_remaining,
            "last_seen": a.last_seen, "error": error, "gpu": gpu,
            "gpus": a.gpus if alive else None, "gpu_error": a.gpu_error if alive else None,
            "released": a.released if alive else None,
            "environments": a.envs if alive else None,
            "env_wanted": a.env_wanted, "env_work": a.env_work if alive else None,
            "sync": a.synced,           # its last sync, as its agent said (decision 0033)
            "comfyui_version": ((a.stats or {}).get("system") or {}).get("comfyui_version"),
            "problems": a.problems, "agent_version": a.version,
            "checked": a.checked,       # its node definitions read since ComfyUI (re)connected
            "farm_nodes": a.farm_nodes if alive else None,
            "rank": (ranks or self.ranks()).get(a.name),   # the order machines get jobs in
            "tags": a.tags,
            # Drained from the dashboard: finishes what it holds, takes nothing new.
            "drained": self.drained.get(a.name),
            "held": len(self.store.held_by(a.name)),
            # Workflows its manifests keep off this machine (requires), and why.
            "skips": {n: why for n in list(self.farm.registry.workflows)
                      if (why := self.unmet(a, n)) is not None},
            "last_workflow": a.last_workflow,
            # How its backend differs from the farm's environment file, as its agent found
            # (shown, never a reason to hold jobs back).
            "environment": a.environment,
        }

    def environments(self) -> dict:
        """What each agent that is checking in has built (decision 0029)."""
        return {a.name: a.envs for a in list(self.agents.values()) if self.alive(a.name) and a.envs is not None}

    def summaries(self) -> list[dict]:
        ranks = self.ranks()
        return [self.summary(a, ranks) for a in list(self.agents.values())]   # also called from threads

    def request_nodes(self) -> None:
        """Ask every agent to send its node definitions again (Check workers)."""
        for a in self.agents.values():
            a.want_nodes = True
        self.wake()

    def _publish(self, a: AgentState) -> None:
        """Tell the dashboards about this agent; about all of them when the order they get
        jobs in changed (an agent joined, or reported its GPU)."""
        ranks = self.ranks()
        order = tuple(sorted(ranks, key=ranks.get))
        changed, self._order = order != self._order, order
        for x in list(self.agents.values()) if changed else [a]:
            self.farm.events.publish({"type": "worker", "worker": self.summary(x, ranks)})

    def _known(self, msg: dict) -> AgentState | None:
        a = self.agents.get(msg.get("name") or "")
        if a is not None:
            a.last_seen = now()
        return a

    @staticmethod
    def _holds(job: dict | None, name: str, attempt: str | None) -> bool:
        """Whether this run of the job is the agent's: still held, by it, as this attempt."""
        return bool(job and attempt and job["status"] in HELD and job["worker"] == name and job["prompt_id"] == attempt)

    # ================================================================ messages
    def ping(self, msg: dict) -> dict:
        """`comfyfarm agent --check`: the manager's version, and the storage an agent must see."""
        name = msg.get("name") or ""
        # For `comfyfarm comfyui sync`: it only stops ComfyUI on a drained machine holding no jobs.
        return {"manager": build(), "lease_seconds": self.lease_seconds, "storage": self._storage_roots(),
                "known": name in self.agents, "drained": name in self.drained,
                "held": len(self.store.held_by(name)) if name else 0}

    def _storage_roots(self) -> list[str]:
        """The storage agents are sent paths on: one studio path per storage area. Other names
        for the same storage (/shows for /rdo/shows) never reach agents, since jobs carry the
        first mapping's studio path."""
        seen, roots = set(), []
        for m in self.cfg.paths.mappings:
            local = P.normalize(m.windows).rstrip("/").lower()
            if local not in seen:
                seen.add(local)
                roots.append(m.posix)
        return roots

    def hello(self, msg: dict) -> dict:
        name = str(msg.get("name") or "").strip()
        if not name:
            return {"error": "An agent needs a name (name in agent.yaml)."}
        a = self.agents.get(name) or AgentState(name)
        self.agents[name] = a
        a.version, a.url = msg.get("version"), msg.get("comfyui")
        a.max_inflight = int(msg.get("max_inflight") or 2)
        a.tags = sorted({str(t).lower() for t in msg.get("tags") or []})
        a.capacity = int(msg.get("capacity") or a.max_inflight + 1)
        a.last_seen, a.seq = now(), int(msg.get("seq") or 0)
        a.checked = False               # its node definitions follow
        a.env_wanted = self.store.env_wanted(name).get(name, {})
        a.want_envs = bool(a.env_wanted)    # an agent that restarted knows none of it
        a.synced = None                     # nor of a sync it was doing: told again (manager/syncs.py)
        if a.version and a.version != build():
            log.warning("agent %s runs %s, the manager %s: update it (git pull) to run the same code",
                        name, a.version, build())
        listed = {h["job"]: h.get("attempt") for h in msg.get("held") or []}
        adopt, drop = [], []
        for j in self.store.held_by(name, with_prompt=True):
            if listed.get(j["id"]) == j["prompt_id"]:
                continue
            # The agent restarted: it picks the job up again from its ComfyUI, or reports it lost.
            adopt.append(self._payload(j, a))
            self._offered[j["id"]] = (name, j["prompt_id"], a.seq)
        for jid, attempt in listed.items():
            if not self._holds(self.store.get(jid), name, attempt):
                drop.append({"job": jid, "attempt": attempt})
        log.info("agent %s checked in (%s, ComfyUI %s)%s", name, a.version or "?", a.url or "?",
                 f": {len(adopt)} job(s) to pick up again" if adopt else "")
        self._publish(a)
        return {"manager": build(), "lease_seconds": self.lease_seconds, "adopt": adopt, "drop": drop,
                # The agent says what it can run (`processes`) and keeps its backend's node
                # definitions to itself. Said for agents from before that was the only way.
                "wants": "processes"}

    def status(self, msg: dict) -> dict:
        a = self._known(msg)
        if a is None:
            return {"hello": True}
        before = self.accepting(a), a.online, a.connected, a.error
        a.online, a.connected = bool(msg.get("online")), bool(msg.get("connected"))
        a.stats, a.queue_remaining, a.error = msg.get("stats"), msg.get("queue_remaining"), msg.get("error")
        nodes_were, a.farm_nodes = a.farm_nodes, msg.get("farm_nodes") or None
        if isinstance(msg.get("released"), dict):
            a.released = msg["released"]
        if isinstance(msg.get("environments"), dict):  # what it has built (decision 0029)
            a.envs = {str(k): v for k, v in msg["environments"].items() if isinstance(v, dict)}
        if "env_wanted" in msg:         # an agent with an environment manager (decision 0030)
            a.env_work = msg.get("env_work") if isinstance(msg.get("env_work"), dict) else None
            if msg.get("env_wanted") != wanted_hash(a.env_wanted):
                a.want_envs = True      # it holds another version: a reply was lost, or it restarted
                self.wake()
        if "sync" in msg:               # how the sync the manager asked for goes (decision 0033)
            a.synced = msg["sync"] if isinstance(msg["sync"], dict) else None
        if "gpus" in msg:               # an agent with gpu_probe on; shown, not placed by yet (decision 0022)
            a.gpus = [g for g in msg.get("gpus") or [] if isinstance(g, dict)]
            a.gpu_error = msg.get("gpu_error") or None
        if a.farm_nodes and (nodes_were or {}).get("state") != a.farm_nodes.get("state"):
            log.warning("worker %s takes no new jobs: %s", a.name, a.farm_nodes.get("message"))
        if not a.connected:
            a.checked = False           # checked again when it reconnects: ComfyUI may have changed
        if not a.online and before[1]:
            log.warning("worker %s: ComfyUI offline: %s", a.name, a.error)
        self._publish(a)
        if self.accepting(a) and not before[0]:
            self.wake()
        return {}

    def nodes(self, msg: dict) -> dict:
        """What the agent says it can run (`processes`: each definition of its checkout with
        its fingerprint and what the machine lacks for it): what stops each workflow there."""
        a = self._known(msg)
        if a is None:
            return {"hello": True}
        a.problems, a.expected = {}, {}
        held = msg.get("processes")
        for name, wf in self.farm.registry.workflows.items():
            if held is None:            # an agent from before agents reported this (it sent node definitions)
                probs = [f"{a.name} runs farm code too old to say what it can run: update the farm's code on it"]
            else:
                self._broken(a, wf, held.get(name))
                probs = self._held_problems(a, wf, held.get(name))
                if not probs and held[name].get("expected_nodes"):
                    a.expected[name] = int(held[name]["expected_nodes"])
            if probs:
                a.problems[name] = probs
                for p in probs:
                    log.error("worker %s / workflow %s: %s", a.name, name, p)
        a.checked = True
        self._publish(a)
        self.wake()
        return {"problems": a.problems}

    @staticmethod
    def _broken(a: AgentState, wf, held: dict | None) -> None:
        """A definition the agent found broken (how it wires its runner's graph). The same
        on every machine with that fingerprint, so it disables the workflow for everyone
        until the definition is fixed (decision 0020)."""
        if not held or held.get("digest") != wf.digest or not held.get("broken"):
            return
        new = sorted(set(str(p) for p in held["broken"]) - set(wf.reported))
        if new:
            wf.reported = sorted(set(wf.reported) | set(new))
            log.error("workflow %s is disabled: %s found its definition broken: %s", wf.name, a.name, "; ".join(new))

    @staticmethod
    def _held_problems(a: AgentState, wf, held: dict | None) -> list[str]:
        """Why this agent can't run the workflow, from what it reported about its own copy."""
        if not held:
            return [f"{a.name} has no workflow {wf.name}: update the farm's code on it"]
        if held.get("digest") != wf.digest:
            return [f"its copy of the workflow ({held.get('digest')}) and the manager's ({wf.digest}) differ: update "
                    f"the farm's code on {a.name} and on the manager (a change not committed on either causes this too)"]
        return list(held.get("broken") or []) + list(held.get("problems") or [])

    def environment(self, msg: dict) -> dict:
        """How the agent's backend differs from the farm's environment file, as the agent
        compared it (`report`: when it was checked, what differs, what couldn't be checked).
        Kept as text for the Machines tab; jobs keep going there."""
        a = self._known(msg)
        if a is None:
            return {"hello": True}
        r = msg.get("report")
        if not isinstance(r, dict):     # an agent from when the manager compared: it sent what it read
            r = {"checked_at": now(), "file": True,
                 "notes": [f"{a.name} runs farm code too old to compare its environment: update the farm's code on it"]}
        before = (a.environment or {}).get("differs")
        core = r.get("comfyui") if isinstance(r.get("comfyui"), dict) else {}
        a.environment = {"checked_at": r.get("checked_at"), "file": bool(r.get("file")),
                         "comfyui": {"version": core.get("version"), "commit": core.get("commit")},
                         "differs": [str(x) for x in r.get("differs") or []],
                         "notes": [str(x) for x in r.get("notes") or []]}
        differs = a.environment["differs"]
        if differs and differs != before:
            log.warning("worker %s differs from the farm's environment: %s", a.name, "; ".join(differs[:5]) +
                        (f" (and {len(differs) - 5} more)" if len(differs) > 5 else ""))
        self._publish(a)
        return {}

    async def heartbeat(self, msg: dict) -> dict:
        """The agent's jobs (leases renewed); answered with what it should do: drop runs
        that aren't its any more, cancel, take new jobs. Held up to msg["wait"] seconds
        while there's nothing."""
        a = self._known(msg)
        if a is None:
            return {"hello": True}
        a.seq = int(msg.get("seq") or a.seq + 1)
        listed = {h["job"]: h for h in msg.get("held") or []}
        until = now() + self.lease_seconds
        for jid, h in listed.items():
            self.store.transition(jid, HELD, only_prompt=h.get("attempt"), lease_until=until)
        for j in self.store.held_by(a.name):
            if j["id"] in listed:
                continue
            offered = self._offered.get(j["id"])
            if offered and offered[1] == j["prompt_id"] and offered[2] >= a.seq:
                continue                # given in reply to this check-in: listed from the next
            self._offered.pop(j["id"], None)
            self.farm.lost(j, f"{a.name} doesn't have the job (it restarted, or never got it).")
        deadline = now() + min(max(float(msg.get("wait") or 0), 0.0), 30.0)
        seq = a.seq
        while True:
            if a.seq != seq:
                # A newer check-in from this agent: it gave up waiting for this one, or it
                # restarted. Nobody reads this reply, so nothing is given out in it.
                return {"drop": [], "cancel": [], "jobs": []}
            reply = self._instructions(a, listed)
            if any(reply.values()) or now() >= deadline:
                break
            await self._wait(deadline - now())
        a.last_seen = now()
        return reply

    def _instructions(self, a: AgentState, listed: dict) -> dict:
        drop, cancel = [], []
        for jid, h in listed.items():
            j = self.store.get(jid)
            if not self._holds(j, a.name, h.get("attempt")):
                drop.append({"job": jid, "attempt": h.get("attempt")})
            elif j["cancel_requested"] and j["status"] in ON_WORKER and not h.get("cancelling"):
                cancel.append({"job": jid, "attempt": h.get("attempt")})
        jobs = self._claim(a) if self.accepting(a) else []
        out: dict = {"drop": drop, "cancel": cancel, "jobs": jobs}
        if a.want_nodes:
            out["nodes"], a.want_nodes = True, False
        if a.want_release:
            out["release_vram"], a.want_release = True, False
        if a.want_envs:
            out["environments"] = {"wanted": dict(a.env_wanted), "hash": wanted_hash(a.env_wanted),
                                   "bank": self.farm.cfg.share.dir or None}
            a.want_envs = False
        if a.want_sync:
            out["sync"], a.want_sync = a.want_sync, None
        return out

    def want_environment(self, name: str, release: str | None, machines: list[str] | None, by: str | None,
                         forget: bool = False) -> dict[str, str | None]:
        """What these machines (default: every one the manager knows) should have of an
        environment: a release's fingerprint, None to remove it, or, with forget, nothing:
        it is left as it is (decision 0030). Told to each in its next check-in reply."""
        names = list(machines) if machines else sorted(self.agents)
        unknown = [m for m in names if m not in self.agents]
        if unknown:
            raise ValueError(f"no machine {', '.join(unknown)} (known: {', '.join(sorted(self.agents)) or 'none'})")
        if not names:
            raise ValueError("no machine has checked in yet")
        for m in names:
            a = self.agents[m]
            if forget:
                self.store.forget_env_wanted(m, name)
                a.env_wanted.pop(name, None)
            else:
                self.store.set_env_wanted(m, name, release, by)
                a.env_wanted[name] = release
            a.want_envs = True
        log.info("environment %s on %s: %s (by %s)", name, ", ".join(names),
                 "not managed" if forget else release or "removed", by or "?")
        self.wake()
        return {m: (None if forget else release) for m in names}

    def _claim(self, a: AgentState) -> list[dict]:
        """The queued jobs this agent takes now.

        Every machine taking jobs has slots: first its free GPU, if it has one; then, after
        every machine's free GPU, one job prepared ahead. Queued jobs fill the slots in queue
        order, each the best slot on a machine that can run its workflow (its nodes and
        models, and the manifest's `requires`): a free GPU before one ahead, then by rank
        (_rank_key: the GPU type that ran the workflow fastest for the job's size; between
        equal cards, the one that last ran the workflow first). This agent takes the jobs
        that land on its slots. So a job goes to the fastest free GPU for it, whichever agent
        checks in first; and a machine gets a second job only when every GPU is busy (or
        can't run it).

        A job no machine may run (requires) waits: a bigger machine may check in later. One
        that every machine allowed to run it lacks the nodes or models for fails now,
        saying why (strict_preflight)."""
        t = now()
        usable = [x for x in self.agents.values() if self.usable(x)]          # drained included
        accepting = [x for x in usable if x.name not in self.drained]
        slots: list[tuple[int, AgentState]] = []                             # (0 free GPU | 1 ahead, machine)
        for x in accepting:
            held = self.store.held_by(x.name)
            busy = sum(1 for j in held if j["status"] in GPU_BUSY)
            room = min(PER_GPU - busy, x.capacity - len(held))
            if room > 0 and busy == 0:
                slots.append((0, x))
                room -= 1
            if room > 0:
                slots.append((1, x))
        used = [False] * len(slots)
        strict = self.cfg.farm.strict_preflight
        mine = []
        unmet: dict[tuple[str, str], bool] = {}

        def allowed(x: AgentState, wf: str) -> bool:
            key = (x.name, wf)
            if key not in unmet:
                unmet[key] = self.unmet(x, wf) is not None
            return not unmet[key]

        def able(x: AgentState, wf: str) -> bool:
            return allowed(x, wf) and not (strict and x.problems.get(wf))

        stats: dict | None = None               # run times, read once there's a job to place

        def best(job: dict, among) -> int | None:
            nonlocal stats
            wf = job["workflow"]
            ks = [k for k in among if not used[k] and able(slots[k][1], wf)]
            if not ks:
                return None
            if stats is None:
                stats = self.store.run_stats()
            size = E.units(job)
            return min(ks, key=lambda k: (slots[k][0], self._rank_key(slots[k][1], workflow=wf, size=size,
                                                                        stats=stats)))

        for job in self.store.queued():
            wf = job["workflow"]
            # The farm's policy again, as it is now (decision 0046): a rule written since the job
            # was queued fails it rather than run it. A policy that can't be read holds the queue.
            refused = self.farm.refused_by_policy(job)
            if refused is not None:
                if refused.rule is not None:
                    self._passed.pop(job["id"], None)
                    self.farm._fail(job["id"], f"Not run: the farm's policy changed after it was queued. {refused}",
                                    ("queued",))
                continue
            fit = [x for x in usable if allowed(x, wf)]
            if fit and not any(able(x, wf) for x in fit):     # every machine it may go to lacks something
                probs = next(x.problems[wf] for x in fit if x.problems.get(wf))
                self._passed.pop(job["id"], None)
                self.farm._fail(job["id"], "The farm machine can't run this workflow: " + "; ".join(probs[:4])
                                + (f" (+{len(probs) - 4} more)" if len(probs) > 4 else "")
                                + ". Run `python -m comfyfarm check` on the farm machine.", ("queued",))
                continue
            i = best(job, range(len(slots)))
            if i is None:
                continue                # no room on a machine that can run it (or none may): it waits
            target = slots[i][1]
            if target is not a:
                since = self._passed.get(job["id"])
                if since is None or since[0] != target.name:
                    since = self._passed[job["id"]] = (target.name, t)
                if t - since[1] >= STEAL_SECONDS and able(a, wf):
                    k = best(job, [k for k in range(len(slots)) if slots[k][1] is a])
                    if k is not None:
                        log.info("job %s waited %.0f s for %s: %s takes it", job["id"][:8], t - since[1],
                                 target.name, a.name)
                        i = k
            used[i] = True
            if slots[i][1] is a:
                mine.append(job)
        out = []
        for job in mine:
            attempt = str(uuid.uuid4())
            j = self.store.transition(job["id"], ("queued",), status="preparing", worker=a.name, prompt_id=attempt,
                                      stage=f"on {a.name}", progress=0, dispatched_at=None, started_at=None,
                                      gpu=self.gpu_type(a), lease_until=now() + self.lease_seconds)
            self._passed.pop(job["id"], None)
            if j is None:
                continue                # cancelled meanwhile
            a.last_claimed, a.last_workflow = t, j["workflow"]
            self._offered[j["id"]] = (a.name, attempt, a.seq)
            self.farm._emit(j)
            out.append(self._payload(self.store.get(j["id"], with_prompt=True), a))
        return out

    # ================================================================ ETAs
    @staticmethod
    def gpu_type(a: AgentState) -> str:
        for dev in (a.stats or {}).get("devices") or []:
            return E.gpu_type(dev.get("name"))
        return E.gpu_type(None)

    def forecast(self, extra: list[dict] | None = None) -> dict[str, dict]:
        """When each queued or running job will be done, and on which machine:
        {job id: {"done_in": seconds or None, "worker": name, "run": its own seconds}}.

        Walks the queue the way _claim hands jobs out: each job to the best free machine
        that may run it (its fastest GPU type for the job, _rank_key); once every GPU is
        busy, prepared ahead on the best one with room;
        once those are full, to whichever frees first. A job's time comes from past runs of
        its workflow on that machine's GPU type, for its size (manager.eta). `extra`: jobs
        not queued yet (a plan), after the queue. Times unknown (no runs yet) are None, and
        so is everything behind them on that machine."""
        t = now()
        stats = self.store.run_stats()
        legacy: dict[str, float | None] = {}

        def run_time(job: dict, a: AgentState) -> float | None:
            wf, size = job["workflow"], E.units(job)
            row = stats.get((wf, self.gpu_type(a))) or E.combine([r for (w, _), r in stats.items() if w == wf])
            est = E.estimate(row, size)
            if est is None:                     # runs from before GPU types were recorded
                if wf not in legacy:
                    legacy[wf] = self.store.stats(wf)["avg_seconds"]
                est = legacy[wf]
            return est

        busy = [x for x in list(self.agents.values()) if self.usable(x)]    # drained ones finish their jobs
        machines = sorted([x for x in busy if x.name not in self.drained], key=self._rank_key)
        free_at: dict[str, float | None] = {}
        count: dict[str, int] = {}
        out: dict[str, dict] = {}
        for x in busy:
            at, n = t, 0
            jobs = sorted(self.store.held_by(x.name), key=lambda j: ("running", "dispatched", "preparing").index(j["status"])
                          if j["status"] in GPU_BUSY else 9)
            for j in jobs:
                if j["status"] not in GPU_BUSY:     # collecting outputs: done any moment, the GPU is free
                    out[j["id"]] = {"done_in": 0.0, "worker": x.name, "run": None}
                    continue
                n += 1
                est = run_time(j, x)
                if est is None or at is None:
                    at = None
                elif j["status"] == "running" and j.get("started_at"):
                    p = j.get("progress") or 0
                    left = est * (1 - p) if p > 0.05 else est - (t - j["started_at"])
                    at += max(left, 5.0)
                else:
                    at += est
                out[j["id"]] = {"done_in": None if at is None else at - t, "worker": x.name, "run": est}
            free_at[x.name], count[x.name] = at, n
        strict = self.cfg.farm.strict_preflight
        for job in self.store.queued() + list(extra or []):
            wf = job["workflow"]
            fit = [x for x in machines if self.unmet(x, wf) is None and not (strict and x.problems.get(wf))]
            if not fit:
                out[job["id"]] = {"done_in": None, "worker": None, "run": None}
                continue
            idle = [x for x in fit if count[x.name] == 0]
            room = [x for x in fit if count[x.name] < PER_GPU]
            size = E.units(job)

            def rank(x: AgentState, wf=wf, size=size) -> tuple:
                return self._rank_key(x, workflow=wf, size=size, stats=stats)
            if idle:
                m = min(idle, key=rank)         # the fastest free GPU for it
            elif room:
                m = min(room, key=rank)         # prepared ahead on the fastest
            else:                               # waits for whichever frees first
                m = min(fit, key=lambda x: (free_at[x.name] is None, free_at[x.name] or 0, rank(x)))
            est = run_time(job, m)
            at = free_at[m.name]
            at = None if at is None or est is None else max(at, t) + est
            free_at[m.name], count[m.name] = at, count[m.name] + 1
            out[job["id"]] = {"done_in": None if at is None else at - t, "worker": m.name, "run": est}
        return out

    def _payload(self, j: dict, a: AgentState) -> dict:
        """A job as an agent gets it: what it needs to run it, in portable form."""
        info = j["info"] or {}
        wf = self.farm.registry.workflows.get(j["workflow"])
        collect = info.get("collect")
        if collect is None:             # queued before agents collected outputs
            collect = wf.collect() if wf else {}
        expected = self.store.stats(j["workflow"]).get("node_count") or a.expected.get(j["workflow"]) or 20
        return W.portable({
            "job": j["id"], "attempt": j["prompt_id"], "status": j["status"], "workflow": j["workflow"],
            "user": j["user"], "kind": j["kind"], "name": j["name"], "attempts": j["attempts"],
            "cancel_requested": j["cancel_requested"], "request": j.get("request"),
            "info": info,
            "output_dir": j["output_dir"], "warnings": j["warnings"], "collect": collect or {},
            "expected_nodes": expected, "dispatched_at": j["dispatched_at"], "started_at": j["started_at"],
        }, self.view)

    def report(self, msg: dict) -> dict:
        """One step of a run. ok: false means the run isn't the job's current one (any
        more): the agent drops it."""
        a = self._known(msg)
        if a is None:
            return {"ok": False, "hello": True}
        jid, attempt, ev = msg.get("job"), msg.get("attempt"), msg.get("event")
        d = msg.get("data") or {}
        job = self.store.get(jid or "")
        if not self._holds(job, a.name, attempt):
            return {"ok": False, "status": job["status"] if job else None}
        F, lease = self.farm, {"lease_until": now() + self.lease_seconds}
        j = None
        if ev == "progress":
            # Lossy: applied only in the state the agent sent it in, so a late one can't
            # take a job back.
            fields = {k: d[k] for k in ("progress", "stage") if d.get(k) is not None}
            if fields and d.get("status") == job["status"]:
                j = self.store.transition(jid, (job["status"],), only_prompt=attempt, **fields, **lease)
                F._emit(j)
            return {"ok": True, "status": job["status"]}
        if ev == "dispatched":
            # `submitted` is what the runner handed its backend (for ComfyUI the API JSON): kept
            # for a person to look at, as reported, and never read by the manager.
            told = d.get("submitted")
            j = self.store.transition(jid, ("preparing",), only_prompt=attempt, status="dispatched",
                                      dispatched_at=d.get("at") or now(), stage="waiting for GPU",
                                      attempts=job["attempts"] + 1, **lease,
                                      **({"prompt": told} if isinstance(told, dict) else {}))
            if j:
                log.info("job %s -> %s (prompt %s)", jid[:8], a.name, str(d.get("prompt_id") or attempt)[:8])
            F._emit(j)
        elif ev == "running":
            j = self.store.transition(jid, ("dispatched",), only_prompt=attempt, status="running",
                                      started_at=d.get("at") or now(), stage=d.get("stage") or "starting", **lease)
            F._emit(j)
        elif ev == "postprocessing":
            j = self.store.transition(jid, ON_WORKER, only_prompt=attempt, status="postprocessing",
                                      stage="collecting outputs", progress=0.99, **lease)
            F._emit(j)
        elif ev == "done":
            outputs = W.localize(d.get("outputs") or {}, self.view)
            j = self.store.transition(jid, ("postprocessing",) + ON_WORKER, only_prompt=attempt, status="done",
                                      progress=1.0, stage="done", finished_at=now(), outputs=outputs,
                                      warnings=job["warnings"] + list(d.get("warnings") or []), lease_until=None)
            if j:
                if j["kind"] != "batch" and d.get("seconds") is not None:   # a set's ETA comes from its batches
                    F.record_run(j["workflow"], float(d["seconds"]), d.get("nodes") or None)
                if d.get("seconds") is not None:    # per GPU type and size, for ETAs (manager.eta)
                    self.store.record_gpu_run(j["workflow"], self.gpu_type(a), E.units(j),
                                              float(d["seconds"]) + float(d.get("prepare_seconds") or 0))
                log.info("job %s done on %s in %.0fs -> %s", jid[:8], a.name, float(d.get("seconds") or 0),
                         outputs.get("output_dir"))
                if (j.get("info") or {}).get("publish_request"):
                    F.publisher.wake()              # published, as asked at submit
            F._emit(j)
        elif ev == "failed":
            j = F._fail(jid, d.get("error") or f"Failed on {a.name}.", HELD, traceback=d.get("traceback"),
                        only_prompt=attempt)
        elif ev == "cancelled":
            j = F._finish_cancel(jid, only_prompt=attempt)
            j = j if j and j["status"] == "cancelled" else None
        elif ev == "lost":
            F.lost(job, d.get("reason") or f"{a.name} lost the job.")
            j = self.store.get(jid)
            j = j if j and not self._holds(j, a.name, attempt) else None
        else:
            return {"ok": False, "error": f"Unknown event {ev!r}."}
        self.wake()
        now_ = self.store.get(jid)
        return {"ok": j is not None, "status": now_["status"] if now_ else None}

    # ================================================================ GPU history
    async def _gpu_loop(self) -> None:
        """Every gpu_history.sample_seconds, each machine's GPUs as it last reported them
        (decision 0049); once an hour, samples older than gpu_history.days go."""
        pruned = 0.0
        while True:
            await asyncio.sleep(self.cfg.gpu_history.sample_seconds)
            try:
                self.sample_gpus()
                if now() - pruned >= 3600:
                    self.store.prune_gpu_samples(now() - self.cfg.gpu_history.days * 86400)
                    pruned = now()
            except Exception:  # noqa: BLE001
                log.exception("GPU history failed")

    def sample_gpus(self, t: float | None = None) -> int:
        """One sample of each GPU of each machine checking in: use and memory from gpu_probe,
        or memory from ComfyUI's report when the probe is off. Returns how many."""
        t = now() if t is None else t
        rows = []
        for a in self.agents.values():
            if not self.alive(a.name):
                continue
            jobs = len(self.store.held_by(a.name))
            if a.gpus:
                rows += [(a.name, int(g.get("index") or 0), t, g.get("utilization"), g.get("vram_used"),
                          g.get("vram_total"), jobs) for g in a.gpus]
                continue
            for i, dev in enumerate((a.stats or {}).get("devices") or []):
                total, free = dev.get("vram_total"), dev.get("vram_free")
                used = max(0, total - free) if total is not None and free is not None else None
                rows.append((a.name, int(dev.get("index", i) or 0), t, None, used, total, jobs))
        self.store.add_gpu_samples(rows)
        return len(rows)

    def gpu_history(self, name: str, hours: float, t: float | None = None) -> dict:
        """GET /api/v1/workers/{name}/gpu-history: its samples over the last `hours`, at most
        about 500 a GPU (averaged into longer steps when there are more)."""
        t = now() if t is None else t
        every = self.cfg.gpu_history.sample_seconds
        step = max(every, hours * 3600 / 500)
        return {"machine": name, "hours": hours, "step_seconds": round(step), "sample_seconds": every,
                "days": self.cfg.gpu_history.days, "samples": self.store.gpu_samples(name, t - hours * 3600, step)}

    # ================================================================ leases
    async def _lease_loop(self) -> None:
        while True:
            await asyncio.sleep(LEASE_CHECK_SECONDS)
            try:
                self.check_leases()
            except Exception:  # noqa: BLE001
                log.exception("lease check failed")

    def check_leases(self, t: float | None = None) -> None:
        """Jobs of agents that stopped checking in go back to the queue; and the dashboard
        hears about agents going quiet or coming back."""
        t = now() if t is None else t
        for j in self.store.lease_expired(t):
            self._offered.pop(j["id"], None)
            self.farm.lost(j, f"{j['worker'] or 'Its worker'} stopped checking in for over "
                              f"{duration(self.lease_seconds)}.")
        for a in self.agents.values():
            alive = self.alive(a.name)
            if alive != a.was_alive:
                a.was_alive = alive
                if not alive:
                    log.warning("agent %s stopped checking in", a.name)
                self._publish(a)
        for jid in [k for k, v in self._offered.items() if not self._holds(self.store.get(k), v[0], v[1])]:
            del self._offered[jid]
        for jid in [k for k in self._passed if (self.store.get(k) or {}).get("status") != "queued"]:
            del self._passed[jid]


__all__ = ["Desk", "AgentState", "ALIVE_SECONDS", "GPU_BUSY", "STEAL_SECONDS", "vram"]
