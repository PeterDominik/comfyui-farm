"""What the manager and the agent share: job states, and the interface an agent reports
its jobs through.

The manager decides everything about a job and stores it; an agent runs it on its
ComfyUI and reports back. For now both run in one process and the manager's Farm is the
host every WorkerRunner reports to. Later the same calls go over HTTP, so this is the
list of what that protocol has to carry."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .workflows import Workflow

# Job statuses. preparing: input being converted; queued: waiting for a worker;
# dispatched: waiting in a ComfyUI's queue; running; postprocessing: outputs being
# collected (frames renamed, texture maps written); then done, failed or cancelled.
ACTIVE = ("preparing", "queued", "dispatched", "running", "postprocessing")
ON_WORKER = ("dispatched", "running")
FINISHED = ("done", "failed", "cancelled")


def now() -> float:
    return time.time()


class JobHost(Protocol):
    """What a worker needs from the manager. Job dicts are as JobStore returns them."""

    # ---- reading jobs
    def job(self, job_id: str, with_prompt: bool = False) -> dict | None:
        """One job (None if it's gone)."""

    def job_by_prompt(self, prompt_id: str) -> dict | None:
        """The job a ComfyUI prompt id belongs to."""

    def jobs_on_worker(self, worker: str, statuses: tuple = ON_WORKER) -> list[dict]:
        """Jobs the manager believes this worker has, with their prompts."""

    def inflight(self, worker: str) -> int:
        """How many jobs this worker has in its ComfyUI (dispatched or running)."""

    # ---- reporting
    def transition(self, job_id: str, from_statuses: tuple, **fields: Any) -> dict | None:
        """Update a job only if it's still in one of from_statuses (None if it isn't:
        cancelled or finished meanwhile). Doesn't publish the change; emit() does."""

    def emit(self, job: dict | None) -> None:
        """Publish a job's new state to dashboards and clients."""

    def fail(self, job_id: str, message: str, from_statuses: tuple, traceback: str | None = None) -> dict | None:
        """The job failed on the worker."""

    def finish_cancel(self, job_id: str) -> dict | None:
        """A cancel the manager asked for has taken effect on the worker."""

    def lost(self, job: dict, reason: str) -> None:
        """The worker lost the job (ComfyUI restarted, machine unreachable): run it
        again, or fail it after farm.max_attempts."""

    def record_run(self, workflow: str, seconds: float, node_count: int | None) -> None:
        """How long a finished run took, for ETAs and progress."""

    def worker_changed(self, worker: Any) -> None:
        """The worker's state changed (online, connected, problems): publish it."""

    def wake(self) -> None:
        """Something changed that may let more jobs be dispatched."""

    # ---- workflows
    def workflow(self, name: str) -> Workflow | None:
        """A workflow's manifest and template."""

    def workflow_stats(self, name: str) -> dict:
        """Past runs of a workflow: avg_seconds, node_count."""

    def workflow_problems(self, object_info: dict) -> dict[str, list[str]]:
        """What each workflow is missing on a ComfyUI with this object_info (node packs,
        inputs, models); only workflows with problems are listed."""
