"""Scratch space: a folder per job on the local disk of the machine that runs it.

<scratch>/<job id>/ holds the job's converted input frames (what the loader reads), the
other inputs' converted frames in a subfolder each (<input>/), and in its maps/ subfolder
the raw images of texture maps before they become EXRs. It's deleted when the job
finishes."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from typing import TYPE_CHECKING

from .paths import normalize

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from .inputs import Sequence

__all__ = ["point_loader_at_scratch", "job_dir", "cleanup", "clean_orphans"]


def point_loader_at_scratch(seq: Sequence, scratch_root: str, job_id: str) -> None:
    """Converted frames go to <scratch>/<job id>/<source>.<frame>.png; the loader reads
    exactly those, so no skip/cap. Padding keeps name order = frame order."""
    d = normalize(f"{scratch_root}/{job_id}") + "/"
    numbers = seq.convert.get("frames") if seq.kind != "movie" else None   # a movie's is its frame count
    width = max(4, len(str(max(numbers) if numbers else seq.last if seq.last is not None else 1)))
    seq.load_dir = d
    seq.skip = seq.cap = 0
    seq.convert.update(dst_dir=d, dst_pattern=f"{seq.source}.%0{width}d.png")


def job_dir(job: dict) -> str | None:
    """<scratch>/<job id>/: converted input frames, the other inputs' in their own
    subfolders (<input>/), and the raw images of texture maps (in its maps/ subfolder)."""
    info = job.get("info") or {}
    conv = (info.get("input") or {}).get("convert")
    if conv:
        return conv.get("dst_dir")
    for other in (info.get("inputs") or {}).values():
        dst = ((other or {}).get("convert") or {}).get("dst_dir")
        if dst:
            return normalize(dst).rstrip("/").rpartition("/")[0] + "/"
    tex = info.get("textures")
    return normalize(tex["raw_dir"]).rstrip("/").rpartition("/")[0] + "/" if tex else None


def cleanup(job: dict, scratch_root: str) -> None:
    """Delete a job's scratch folder (only ever inside scratch_root)."""
    d = job_dir(job)
    if d and normalize(d).startswith(scratch_root + "/"):
        shutil.rmtree(d, ignore_errors=True)


def clean_orphans(scratch_root: str, keep: Callable[[str], bool]) -> None:
    """Delete the scratch folders of jobs that are gone (finished while nothing was
    running), except those `keep(job id)` wants kept."""
    if not os.path.isdir(scratch_root):
        return
    for name in os.listdir(scratch_root):
        if not keep(name):
            shutil.rmtree(os.path.join(scratch_root, name), ignore_errors=True)
