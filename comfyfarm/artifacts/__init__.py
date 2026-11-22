"""Typed artifacts (decision 0034): each kind of output the farm writes is one handler, a
module of this package named by its key under the head's `writes:` and defining HANDLER.
They are found by listing the package, so a new kind is one more file here.

    frames     a frame sequence, renamed to the shot's frame numbers, with its preview movie
    textures   a texture set: maps written as EXRs named by the TEX convention

base.py says what a handler is. The manager plans a job's outputs with the handler of what
its definition writes (for_definition), and the agent prepares and collects them with the
handler of what the job writes (for_job)."""

from __future__ import annotations

import importlib
import pkgutil

from .base import Handler, PlanContext, PlanError, PrepareError, RunContext

_found: list | None = None


def handlers() -> list[Handler]:
    """Every handler in this package, by module name."""
    global _found
    if _found is None:
        names = sorted(m.name for m in pkgutil.iter_modules(__path__) if m.name != "base")
        _found = [h for n in names if (h := getattr(importlib.import_module(f"{__name__}.{n}"), "HANDLER", None))]
    return list(_found)


def for_definition(wf) -> Handler | None:
    """The handler of what a definition writes, or None when it writes nothing the farm collects."""
    return next((h for h in handlers() if h.writes(wf)), None)


def for_job(job: dict) -> Handler | None:
    """The handler of what a job writes, from the job alone (the agent has no definition's head)."""
    return next((h for h in handlers() if h.collects(job)), None)


__all__ = ["Handler", "PlanContext", "PlanError", "PrepareError", "RunContext", "handlers", "for_definition",
           "for_job"]
