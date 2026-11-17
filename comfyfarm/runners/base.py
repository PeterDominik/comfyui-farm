"""The seam between the agent and a runner.

A runner is resident: it lives as long as the agent and follows its backend (is it up, what
can it run, what is each run doing). The agent calls the runner to start, cancel and look
up runs; the runner calls the agent back (`Host`) when a run moves on. Both share the run
record the agent keeps (agent.Held): the runner sets its `pid` and `rt`, the agent the rest.

Protocols, for the reader and the tests: nothing here is imported at run time by a backend.
"""

from __future__ import annotations

from typing import Any, Protocol


class RunRejected(Exception):
    """The backend refused the job. The message is for the artist."""


class RunState(Protocol):
    """A run's live progress, kept by the runner on Held.rt."""
    last_push: float

    def progress(self) -> float: ...        # 0..1

    def nodes(self) -> int | None: ...      # steps the run went through, for the manager's ETAs


class Host(Protocol):
    """What a runner may ask of the agent that holds it."""
    name: str
    registered: bool                        # the manager knows this agent
    view: Any                               # how this machine sees studio paths (fileio.wire.View)

    def runs(self) -> list: ...             # the runs the agent holds
    def holds(self, run) -> bool: ...       # still this agent's (not dropped or replaced meanwhile)
    def by_pid(self, pid: str): ...
    def spawn(self, coro) -> None: ...      # a background task that ends with the agent

    async def send_status(self) -> None: ...
    async def send_nodes(self) -> None: ...
    async def send_environment(self) -> dict | None: ...

    async def run_started(self, run) -> None: ...
    async def run_progress(self, run, stage: str | None = None, force: bool = False) -> None: ...
    async def run_succeeded(self, run, result: Any = None) -> None: ...
    async def run_failed(self, run, message: str, traceback: str | None = None) -> None: ...
    async def run_interrupted(self, run) -> None: ...
    async def run_lost(self, run, reason: str) -> None: ...


class Runner(Protocol):
    """What the agent asks of a runner."""
    label: str                              # the backend's name in messages ("ComfyUI")
    ready: bool                             # up and checked: runs can be started

    def attach(self, host: Host) -> None: ...
    def start(self) -> list: ...            # its background tasks
    async def close(self) -> None: ...
    def status(self) -> dict: ...           # for the manager: online, connected, stats, ...
    def processes(self) -> dict: ...        # what it can run: {name: {digest, problems, expected_nodes}}

    async def submit(self, run) -> None: ...            # sets run.pid and run.rt; RunRejected if refused
    async def cancel(self, run) -> bool: ...            # True: gone without having run
    async def abandon(self, pid: str) -> None: ...      # stop it, no report wanted
    def give_up(self, pid: str) -> None: ...            # cancel it if it is ever seen again
    async def locate(self, run) -> tuple[str, Any]: ...  # running | pending | finished | absent, its result
    def follow(self, run) -> None: ...                  # a run found again: track its progress
    async def settle(self, run, result) -> None: ...    # a run that ended unseen: report how
    async def result(self, run) -> Any: ...             # a finished run's result
    def files(self, result, node) -> list: ...          # files one of its steps wrote
    async def fetch(self, entry, dest) -> None: ...     # copy one of them to dest
    async def forget(self, pid: str | None) -> None: ...  # drop the backend's record of the run
