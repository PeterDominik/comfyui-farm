"""Typed artifacts (decision 0034): each kind of output the farm writes is one handler, a
module of this package named by its key under the head's `writes:` and defining HANDLER.
They are found by listing the package, so a new kind is one more file here.

    frames     a frame sequence, renamed to the shot's frame numbers, with its preview movie
    textures   a texture set: maps written as EXRs named by the TEX convention
    file       any other files a run writes, named after the job (base.Generic, 0036)

base.py says what a handler is. The manager plans a job's outputs with the handlers of what
its definition writes (for_definition), and the agent prepares and collects them with the
handlers of what the job writes (for_job). Frames or a texture set come first: they choose
the folder, and the other kinds write into it."""

from __future__ import annotations

import importlib
import pkgutil

from ..fileio.paths import normalize
from .base import Handler, PlanContext, PlanError, PrepareError, RunContext

PROVENANCE = ".provenance.json"     # how each kind names a job's provenance file (decision 0038)
RESUME = ".resume"                  # and a batch job's resume folder, named the same way (0063)
_found: list | None = None


def handlers() -> list[Handler]:
    """Every handler in this package, by module name."""
    global _found
    if _found is None:
        names = sorted(m.name for m in pkgutil.iter_modules(__path__) if m.name != "base")
        _found = [h for n in names if (h := getattr(importlib.import_module(f"{__name__}.{n}"), "HANDLER", None))]
    return list(_found)


def _ordered(found: list) -> list:
    return sorted(found, key=lambda h: (getattr(h, "generic", False), h.kind))


def for_definition(wf) -> list[Handler]:
    """The handlers of what a definition writes (none: nothing the farm collects)."""
    return _ordered([h for h in handlers() if h.writes(wf)])


def for_job(job: dict) -> list[Handler]:
    """The handlers of what a job writes, from the job alone (the agent has no definition's head)."""
    return _ordered([h for h in handlers() if h.collects(job)])


def by_kind(kind: str) -> Handler | None:
    return next((h for h in handlers() if h.kind == kind), None)


def generic_kinds() -> list[str]:
    """The kinds a definition writes through the generic route (base.Generic): writes.<kind>,
    and for ComfyUI the node that writes them (runner.outputs.<kind>)."""
    return [h.kind for h in handlers() if getattr(h, "generic", False)]


def resume_dir(job: dict) -> str | None:
    """Where a batch job keeps what it resumes from (decision 0063): a folder in its output
    folder, named like its outputs as its provenance file is (plate.batchtest_v001.resume).
    None: it has no output folder, or writes nothing the farm names."""
    info = job.get("info") or {}
    out = info.get("output_dir")
    for handler in for_job(job):
        name = handler.provenance(info)
        if name and out:
            base = name[:-len(PROVENANCE)] if name.endswith(PROVENANCE) else name
            return normalize(out).rstrip("/") + "/" + base + RESUME
    return None


def merge(into: dict, more: dict) -> dict:
    """What several handlers give, together: their `writes` side by side, their
    `artifacts` one after the other; anything else is one handler's own."""
    for k, v in more.items():
        if k == "writes":
            into[k] = {**(into.get(k) or {}), **v}
        elif k == "artifacts":
            into[k] = [*(into.get(k) or []), *v]
        else:
            into[k] = v
    return into


__all__ = ["Handler", "PlanContext", "PlanError", "PrepareError", "RunContext", "handlers", "for_definition",
           "for_job", "by_kind", "generic_kinds", "merge", "resume_dir", "PROVENANCE", "RESUME"]
