"""The seam between the farm and a kind of output it writes (decision 0034).

A handler is one kind of artifact: what a definition says it writes under the head's
`writes:` (frames, textures). It has a half for each side of the farm:

  the manager's   writes(wf) says whether a definition writes this kind; plan() works out,
                  when a job is planned, where its files go and what they are called (an
                  output folder with a free version), into the plan the job is made from
  the agent's     collects(job) says whether a job writes this kind, from the job alone;
                  prepare() runs before the run (folders the runner writes into), and
                  collect() after it: what the runner wrote turned into the farm's files,
                  as the outputs the manager keeps and the warnings for the artist

Handlers import neither the manager nor the agent: what they need comes in a context. The
manager's errors are PlanError (with the HTTP status the manager answers with), the
agent's PrepareError (the job fails with its message).

Protocols, for the reader and the tests: nothing here is imported at run time by a
handler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol


class PlanError(Exception):
    """Where a job's outputs would go can't be worked out. Said for the artist."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class PrepareError(Exception):
    """A run can't be prepared for this kind of output: the job fails with this message."""


@dataclass
class PlanContext:
    """What the manager gives a handler's plan()."""
    cfg: Any                                # the manager's Config (paths, ocio, preview)
    job_id: str                             # "preview" while the dashboard only looks
    scratch_root: str                       # the agents' scratch folder, as jobs name it
    reserved: Callable[[str], bool]         # files promised to a job still queued or running


@dataclass
class RunContext:
    """What the agent gives a handler before and after a run."""
    job: dict                               # the job as the agent holds it: info (frozen at submit), collect
    cfg: Any                                # the agent's AgentConfig
    run: Any = None                         # the run (agent.Held): its id
    runner: Any = None                      # the runner that ran it: its own preview video (preview, fetch)
    result: Any = None                      # what the runner returned when the run ended
    report: Callable[[str], Awaitable[None]] | None = None   # a postprocessing stage, for the dashboard
    spawn: Callable[[Awaitable], None] | None = None          # a report sent without waiting for it

    @property
    def info(self) -> dict:
        return self.job.get("info") or {}


class Handler(Protocol):
    kind: str                               # its key under the head's writes:, and its module's name

    def writes(self, wf) -> bool: ...                   # the definition writes this kind (the manager)
    def plan(self, plan: dict, ctx: PlanContext) -> dict: ...    # where it goes; PlanError if it can't
    def collects(self, job: dict) -> bool: ...          # the job writes this kind (the agent)
    async def prepare(self, ctx: RunContext) -> None: ...        # before the run; PrepareError
    async def collect(self, ctx: RunContext) -> tuple[dict, list[str]]: ...   # after: outputs, warnings
