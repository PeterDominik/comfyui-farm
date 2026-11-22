"""A job's provenance file (decision 0038): one JSON file beside its outputs saying what ran,
where, on what, for whom and from what, and which files the farm wrote. A frame found on
disk months later says where it came from, and the retention sweeper (plan, phase 6) is to
delete only the files a provenance file lists.

The agent that ran the job writes it once the outputs are collected, when the job asks for
one (info.provenance, frozen at submit from the manager's provenance.enabled). It is named
by the first kind the job writes, like that kind's files (each handler's provenance()), and
lists what each handler says it wrote (written()). The agent turns its paths into the
portable form (studio paths, fileio.wire) before writing, so it reads the same from Linux
and Windows. A job whose file can't be written still succeeds, with a warning."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime

from .. import artifacts as A
from ..fileio import outputs as O
from ..fileio import paths as P

VERSION = 1             # the file's format (comfyfarm_provenance): a reader checks it

__all__ = ["VERSION", "name_for", "files", "document", "write"]


def name_for(job: dict) -> str | None:
    """The file's name in the job's output folder, from the first kind it writes that names
    one (frames or a texture set come first). None: it writes nothing the farm collects."""
    info = job.get("info") or {}
    for handler in A.for_job(job):
        name = handler.provenance(info)
        if name:
            return name
    return None


def files(out_dir: str | None, wrote: list[str]) -> list[str]:
    """What the job wrote, as names in its output folder (a full path for one outside it),
    each once, in the order the handlers gave them."""
    base = P.normalize(out_dir).rstrip("/") + "/" if out_dir else None
    seen: dict[str, None] = {}
    for f in wrote:
        f = P.normalize(str(f))
        seen.setdefault(f[len(base):] if base and f.startswith(base) else f, None)
    return list(seen)


def _when(t: float | None) -> str | None:
    """A time as this machine's local time with its offset, e.g. 2026-11-22T14:03:11-05:00."""
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None


def document(job: dict, *, machine: str, gpu: dict | None, farm: str, runner: dict, started: float,
             finished: float, outputs: dict, wrote: list[str], warnings: list[str]) -> dict:
    """The provenance of one finished run, from the job as the agent holds it (its frozen
    info and stored request) and what the agent knows of the run. Paths are this machine's:
    the caller makes them portable."""
    info = job.get("info") or {}
    request = job.get("request") or {}
    inp = info.get("input") or {}
    return {
        "comfyfarm_provenance": VERSION,
        "job": job.get("job"), "attempt": job.get("attempt"), "name": job.get("name"), "user": job.get("user"),
        # The definition as reviewed in the repo: its name and the fingerprint pinned at submit.
        "definition": {"name": job.get("workflow"), "fingerprint": request.get("definition")},
        "shot": {k: info.get(k) for k in ("show", "seq", "shot", "source", "tag", "version")},
        "ran": {"machine": machine, "gpu": gpu, "farm": farm, "runner": runner,
                "dispatched": _when(job.get("dispatched_at")), "started": _when(started),
                "finished": _when(finished), "seconds": round(finished - started, 1)},
        "inputs": {
            "input": info.get("input_posix"),
            "range": [inp.get("first"), inp.get("last")] if inp.get("first") is not None else None,
            "others": info.get("inputs_posix") or {},
            "shotgrid": info.get("sources") or {},          # ShotGrid Versions given as inputs, by param
        },
        "params": request.get("values") or {},
        "folder": info.get("output_dir"),
        "files": files(info.get("output_dir"), wrote),
        "artifacts": outputs.get("artifacts") or [],
        "warnings": list(warnings),
    }


def write(path: str, doc: dict) -> None:
    """Written whole or not at all: to a temporary file beside it, then put in its place, so
    nobody reads half a file. Readable by everyone, like the outputs (O.readable)."""
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".provenance-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    O.readable(path)
