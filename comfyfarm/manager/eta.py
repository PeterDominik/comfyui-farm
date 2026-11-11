"""How long jobs take on each kind of GPU, and when queued ones will be done.

Every finished run is recorded per workflow and GPU type (an RTX 5090, whichever machine
it is in) with its size: the frames it read, or the texture tiles of a batch. From those
runs a job's time is fitted as overhead + time per frame, weighted toward recent runs,
so a 200-frame LTX job is expected to take longer than a 50-frame one, and a 3090 longer
than a 5090. While all runs had about the same size, the time is taken as proportional to
the size.

The same times decide where a job goes: the desk gives it to the free GPU type that ran
its workflow fastest, for its size (Desk._rank_key). Desk.forecast() walks the queue the
way the desk hands jobs out (the best free GPU, a job
prepared ahead on the best machine once every GPU is busy, then whichever frees first)
to say when each job will be done, and on which machine."""

from __future__ import annotations

import re

__all__ = ["DECAY", "gpu_type", "fold", "combine", "fit", "estimate", "describe", "units"]

# Each new run counts this much less than the one after it: about the last 5 runs matter.
DECAY = 0.8
FIELDS = ("w", "wx", "wy", "wxx", "wxy")


def gpu_type(name: str | None) -> str:
    """ComfyUI's device name without the device number and allocator:
    'cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync' -> 'NVIDIA GeForce RTX 5090'."""
    name = re.sub(r"^\s*cuda:\d+\s+", "", name or "")
    return re.sub(r"\s+:\s+\S+\s*$", "", name).strip() or "unknown GPU"


def fold(row: dict | None, size: float, seconds: float) -> dict:
    """The stats with one more run of `size` frames (or tiles) that took `seconds`."""
    r = {k: (row or {}).get(k) or 0.0 for k in FIELDS}
    out = {k: r[k] * DECAY for k in FIELDS}
    out["w"] += 1
    out["wx"] += size
    out["wy"] += seconds
    out["wxx"] += size * size
    out["wxy"] += size * seconds
    out["runs"] = int((row or {}).get("runs") or 0) + 1
    return out


def combine(rows: list[dict]) -> dict | None:
    """Several GPU types' stats as one (for a GPU that hasn't run the workflow yet)."""
    rows = [r for r in rows if r and r.get("w")]
    if not rows:
        return None
    out = {k: sum(r[k] for r in rows) for k in FIELDS}
    out["runs"] = sum(int(r.get("runs") or 0) for r in rows)
    return out


def fit(row: dict | None) -> tuple[float, float] | None:
    """(overhead seconds, seconds per frame or tile) from the stats, or None with no runs.
    Sizes that vary (3 runs or more, spread over 10% of the usual size) give a line through
    the runs; about one size so far gives a time proportional to the size."""
    if not row or not row.get("w"):
        return None
    w = row["w"]
    mx, my = row["wx"] / w, row["wy"] / w
    var = row["wxx"] / w - mx * mx
    if row.get("runs", 0) >= 3 and mx > 0 and var > (0.1 * mx) ** 2:     # sizes vary: overhead + per frame
        b = (row["wxy"] / w - mx * my) / var
        a = my - b * mx
        if b >= 0 and a >= 0:
            return a, b
    if mx > 0:
        return 0.0, my / mx
    return my, 0.0


def estimate(row: dict | None, size: float) -> float | None:
    """Seconds a run of `size` frames (or tiles) should take, or None with no runs yet."""
    f = fit(row)
    return None if f is None else f[0] + f[1] * size


def describe(row: dict) -> dict:
    """The stats as the dashboard shows them: runs, the usual size and time, and the fit."""
    w = row.get("w") or 0
    a, b = fit(row) or (None, None)
    return {"runs": int(row.get("runs") or 0), "size": round(row["wx"] / w, 1) if w else None,
            "seconds": round(row["wy"] / w, 1) if w else None,
            "overhead": None if a is None else round(a, 1), "per_unit": None if b is None else round(b, 2)}


def units(job: dict) -> int:
    """A job's size: the tiles of a texture batch, or the frames it reads (1 for a still)."""
    info = job.get("info") or {}
    batch = info.get("batch") or {}
    if batch.get("tiles"):
        return int(batch["tiles"])
    tex = info.get("textures") or {}
    if tex.get("items"):
        return len(tex["items"])
    return int(info.get("expected_frames") or (info.get("input") or {}).get("frame_count") or 1)
