"""What the manager and its agents share: job states, and the messages between them.

The manager decides everything about a job and stores it; an agent runs it on its
ComfyUI and reports back. Agents only ever call the manager (they need no open port):

  hello      an agent starts: its name, its ComfyUI, how many jobs it takes at once, the
             jobs it holds. The reply lists jobs the manager has on it that it didn't
             mention (after a restart): it picks them up again from ComfyUI, or reports
             them lost.
  status     its ComfyUI's state: reachable, connected, GPU, queue (on change, and every
             few seconds for the dashboard). With gpu_probe on, also `gpus`: each card
             as nvidia-smi reads it (index, uuid, name, memory total, used and free,
             utilization, driver), and `gpu_error` when it can't be read.
  nodes      what it can run, when its ComfyUI connects (`processes`): each workflow of
             the agent's checkout with its fingerprint, what its ComfyUI lacks for it,
             what is wrong with how the definition wires the graph (`broken`, which
             disables the workflow on the manager), and how many nodes a run goes
             through. The node definitions stay on the agent.
  environment  how its ComfyUI differs from the farm's environment file (`report`: what
             differs and what couldn't be checked, as text), when ComfyUI starts and
             every agent.environment_seconds. The agent compares, with its own
             checkout's copy of the file; the manager shows the report.
  heartbeat  the jobs it holds, which renews their leases. The manager answers as soon as
             it has something for the agent (new jobs, cancels, jobs to drop) or after
             `wait` seconds.
  report     one step of one job's run: preparing progress, dispatched, running,
             progress, postprocessing, done, failed, cancelled, lost. `dispatched` may
             carry `submitted`: what the runner handed its backend, which the manager
             keeps for people to look at and never reads.

Every job an agent holds is one run of it, named by its attempt id (also the prompt id
in ComfyUI). The manager acts on a report only while that run is the job's current one,
so a late or duplicate report about an earlier run changes nothing. If an agent stops
checking in for farm.lost_after_seconds, the manager queues its jobs again.

A job names no node of its backend (docs/decisions/0018): it says what the run takes and
writes, and the agent's runner finds the nodes in its own copy of the definition, the one
the job's fingerprint pins.

Between machines, a job's paths are studio paths, with @scratch/ and @repo/ for the
agent's scratch folder and repo (fileio.wire). The manager keeps them in its database as
it sees them, so its API and dashboard are as before.

Messages are plain JSON. In `standalone` the agents run in the manager's process and
call it directly (agent.link.LocalLink); otherwise over HTTP (agent.link.HttpLink, the
/api/v1/agent/... endpoints)."""

from __future__ import annotations

import time

# Job statuses. queued: waiting for a worker; preparing: on a worker, getting ready
# (converting the input); dispatched: waiting in its ComfyUI's queue; running;
# postprocessing: outputs being collected (frames renamed, texture maps written); then
# done, failed or cancelled.
ACTIVE = ("queued", "preparing", "dispatched", "running", "postprocessing")
ON_WORKER = ("dispatched", "running")                                # in a ComfyUI
HELD = ("preparing", "dispatched", "running", "postprocessing")     # leased to an agent
FINISHED = ("done", "failed", "cancelled")

# report events
EVENTS = ("progress", "dispatched", "running", "postprocessing", "done", "failed", "cancelled", "lost")
TERMINAL = ("done", "failed", "cancelled", "lost")


def now() -> float:
    return time.time()


def duration(seconds: float) -> str:
    """300 -> '5 minutes', 45 -> '45 seconds': for messages artists read."""
    if seconds >= 90:
        m = round(seconds / 60)
        return f"{m} minutes"
    s = round(seconds)
    return f"{s} second{'' if s == 1 else 's'}"


_BUILD: str | None = None


def build() -> str:
    """This checkout's version: comfyfarm's version and its git commit, e.g. 0.1.0+a6800d0.
    Agents send it, so the manager can warn about one running other code."""
    global _BUILD
    if _BUILD is None:
        import subprocess
        from pathlib import Path

        from . import __version__
        commit = ""
        try:
            out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parent.parent,
                                 capture_output=True, text=True, timeout=10)
            commit = out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            pass
        _BUILD = __version__ + (f"+{commit}" if commit else "")
    return _BUILD


class LinkError(Exception):
    """The manager couldn't be reached, or refused the call."""


__all__ = ["ACTIVE", "ON_WORKER", "HELD", "FINISHED", "EVENTS", "TERMINAL", "now", "build", "LinkError"]
