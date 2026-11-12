"""The farm's own ComfyUI nodes (comfyui_nodes/comfyfarm_nodes): what their loaders read,
and which version of them a ComfyUI runs.

ComfyUI loads custom nodes once, at startup, so after a `git pull` it keeps running the
old ones until it restarts. Each farm node writes its version (a hash of its code and of
the fileio modules it uses) and the checkout it was loaded from into its description,
which ComfyUI's /object_info shows. The agent compares that with its own checkout and
restarts ComfyUI when they differ (agent.agent)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .errors import SequenceError
from .paths import _pattern_regex, normalize, parse_frames, parse_input

__all__ = ["CLASSES", "DEPENDS", "version", "marker", "loaded", "stale", "input_files"]

# Every node class in comfyui_nodes/comfyfarm_nodes.
CLASSES = ("ComfyFarmLoadImage", "ComfyFarmLoadImages", "ComfyFarmSaveImage", "ComfyFarmProAlpha",
           "ComfyFarmProAlphaPick")
# The fileio modules the nodes import: a change to one of them is a change to the nodes
# (tests/test_comfyui_nodes.py checks the list is complete).
DEPENDS = ("comfyfarm/fileio/errors.py", "comfyfarm/fileio/paths.py", "comfyfarm/fileio/farmnodes.py")
_MARK = re.compile(r"\[comfyfarm nodes (?P<version>[0-9a-f]+) from (?P<where>[^\]]*)\]")


def version(repo: str | os.PathLike) -> str:
    """The farm nodes' version in this checkout: a hash of their code."""
    root = Path(repo)
    files = sorted({*(root / "comfyui_nodes/comfyfarm_nodes").glob("*.py"), *(root / d for d in DEPENDS)})
    h = hashlib.sha1()
    for f in files:
        if not f.is_file():
            continue
        h.update(f.relative_to(root).as_posix().encode() + b"\0")
        h.update(f.read_bytes().replace(b"\r\n", b"\n") + b"\0")    # the same on Windows (autocrlf)
    return h.hexdigest()[:12]


def marker(ver: str, where: str | os.PathLike) -> str:
    """What a node adds to its description: its version and the checkout it came from."""
    return f"[comfyfarm nodes {ver} from {normalize(str(where))}]"


def loaded(object_info: dict) -> dict[str, tuple[str, str] | None]:
    """The farm node classes a ComfyUI has: (version, checkout) of each, or None for a
    node from before nodes carried their version."""
    out: dict[str, tuple[str, str] | None] = {}
    for cls in CLASSES:
        info = object_info.get(cls)
        if isinstance(info, dict):
            m = _MARK.search(str(info.get("description") or ""))
            out[cls] = (m["version"], m["where"]) if m else None
    return out


def stale(object_info: dict, repo: str | os.PathLike) -> str | None:
    """Why this ComfyUI doesn't run this checkout's farm nodes, or None when it does (or
    has none of them installed at all, which the workflow checks report instead)."""
    have = loaded(object_info)
    if not have:
        return None
    want = version(repo)
    missing = [c for c in CLASSES if c not in have]
    other = sorted({v for v in have.values() if v and v[0] != want})
    old = [c for c, v in have.items() if v is None]
    if not (missing or other or old):
        return None
    why = []
    if missing:
        why.append(f"it lacks {', '.join(missing)}")
    if other:
        why.append("it runs version " + ", ".join(f"{v} from {w}" for v, w in other))
    if old:
        why.append(f"its {', '.join(old)} {'is' if len(old) == 1 else 'are'} from before nodes had versions")
    return (f"ComfyUI doesn't run this checkout's farm nodes (version {want} in {normalize(str(repo))}): "
            + "; ".join(why) + ".")


def input_files(path: str, frames: str = "") -> list[tuple[int | None, str]]:
    """What ComfyFarm Load Image(s) reads, in order: (frame number, file).

    `path` is a file, or a frame pattern (plate.%04d.png, ####, $F4) optionally followed by
    a range or one frame, as copied from Nuke or the pipeline: /shows/x/plate.%04d.png
    1001-1158, /shows/x/plate.%04d.png 1050. `frames` lists the exact frames (1001-1158,
    1001,1002,1011) and wins over those. A pattern with neither reads every frame of it in
    its folder. Missing frames are an error, never skipped."""
    p, first, last = parse_input(path or "")
    if not p:
        raise SequenceError("No image path given.")
    directory, _, name = p.rpartition("/")
    directory += "/"
    rx = _pattern_regex(name)
    wanted = parse_frames(frames) if (frames or "").strip() else None
    if rx is None:
        if wanted and len(wanted) > 1:
            raise SequenceError(f"{name} is one image, not a frame pattern, so it can't give frames {frames}.")
        if not os.path.isfile(p):
            raise SequenceError(f"{p} doesn't exist.")
        return [(wanted[0] if wanted else None, p)]
    if wanted is None and first is not None:
        wanted = list(range(first, last + 1))
    try:
        names = os.listdir(directory)
    except OSError as e:
        raise SequenceError(f"Can't open {directory} ({e.strerror or e}).")
    by_frame = {int(m.group(1)): m.group(0) for m in map(rx.match, names) if m}   # %d also finds 0001
    if wanted is None:
        wanted = sorted(by_frame)
        if not wanted:
            raise SequenceError(f"No frames of {name} in {directory}.")
    missing = [f for f in wanted if f not in by_frame]
    if missing:
        shown = ", ".join(str(f) for f in missing[:12]) + (f" … ({len(missing)} missing)" if len(missing) > 12 else "")
        raise SequenceError(f"Missing frames of {directory}{name}: {shown}.")
    return [(f, directory + by_frame[f]) for f in wanted]
