"""Studio paths and how this machine sees them, frame tokens (%04d, ####, $F4, ...),
show / seq / shot from where they sit in a path, and file name templates.

Linux workstations see show storage as /rdo/shows/...; a Windows machine sees the same
share as a UNC path. `paths.mappings` in config.yaml pairs the two."""

from __future__ import annotations

import os
import re
import string
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import PathMapping, PathsConfig

from .errors import SequenceError

__all__ = ["SequenceError", "normalize", "to_farm", "to_posix", "frame_pattern", "parse_input", "context_from_path",
           "source_stem", "split_input_path", "check_writable", "template_fields", "render",
           "LOADER_EXTENSIONS", "CONVERTED_SEQUENCE_EXTENSIONS"]


# Extensions VHS "Load Images (Path)" reads (ComfyUI's FolderOfImages.IMG_EXTENSIONS).
LOADER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".ppm", ".bmp", ".pgm", ".tif", ".tiff", ".webp"}

# %d, %04d, ####, @@@@, $F4
_TOKEN_RE = re.compile(r"%0?(\d*)d|#+|@+|\$F(\d*)")

# "<path> 1001-1158" (Nuke/pipeline style, range at the end)
_RANGE_RE = re.compile(r"^(?P<path>.*?\S)\s+(?P<first>-?\d+)\s*-\s*(?P<last>-?\d+)\s*$")


def normalize(path: str) -> str:
    """Forward slashes everywhere (works for UNC paths on Windows too)."""
    return path.strip().strip('"').replace("\\", "/")


def _starts_with(path: str, prefix: str, case_insensitive: bool) -> bool:
    p, pre = (path.lower(), prefix.lower()) if case_insensitive else (path, prefix)
    pre = pre.rstrip("/")
    return p == pre or p.startswith(pre + "/")


def to_farm(path: str, mappings: list[PathMapping]) -> str:
    """Workstation path -> path the farm machine can open."""
    path = normalize(path)
    for m in mappings:
        if _starts_with(path, m.posix, case_insensitive=False):
            return normalize(m.windows).rstrip("/") + path[len(m.posix.rstrip("/")):]
    return path


def to_posix(path: str, mappings: list[PathMapping]) -> str | None:
    """Farm path -> how Linux workstations see it (None if no mapping applies)."""
    path = normalize(path)
    for m in mappings:
        win = normalize(m.windows).rstrip("/")
        if _starts_with(path, win, case_insensitive=True):
            return m.posix.rstrip("/") + path[len(win):]
    if path.startswith("/") and not path.startswith("//"):
        return path
    return None


def frame_pattern(template: str) -> str:
    """'{shot}.{frame:04d}.{ext}' -> '{shot}.%04d.{ext}' (still str.format-able)."""
    return re.sub(
        r"\{frame(?::0?(\d+)d)?\}",
        lambda m: "%0" + (m.group(1) or "4") + "d",
        template,
    )


def parse_input(raw: str) -> tuple[str, int | None, int | None]:
    raw = raw.strip()
    m = _RANGE_RE.match(raw)
    if m:
        first, last = int(m.group("first")), int(m.group("last"))
        if last < first:
            raise SequenceError(f"Frame range {first}-{last} is backwards.")
        return normalize(m.group("path")), first, last
    return normalize(raw), None, None


def _pattern_regex(filename: str) -> re.Pattern | None:
    tokens = list(_TOKEN_RE.finditer(filename))
    if not tokens:
        return None
    if len(tokens) > 1:
        raise SequenceError(f"More than one frame token in '{filename}'.")
    t = tokens[0]
    return re.compile(
        "^" + re.escape(filename[: t.start()]) + r"(-?\d+)" + re.escape(filename[t.end():]) + "$"
    )


def _compile_context(pattern: str) -> re.Pattern:
    """'/shows/{show}/.published/{seq}/{shot}/' -> regex; each {name} is one folder."""
    out, pos = [], 0
    for m in re.finditer(r"\{(\w+)\}", pattern):
        out.append(re.escape(pattern[pos:m.start()]))
        out.append(f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    out.append(re.escape(pattern[pos:]))
    return re.compile("".join(out), re.IGNORECASE)


def context_from_path(path: str, patterns: list[str], shot_name: str = "{seq}_{shot_short}") -> dict:
    """show / seq / shot from where they sit in the path, not from what they're called,
    so any shot naming works. Earlier patterns win; later ones only fill gaps.

    Work areas name the shot folder by its short number (/shows/raa/bpash/1545/...);
    publishes use the full name (.published/bpash/bpash_1545/). A pattern can capture
    {shot_short} instead of {shot}, and the full shot name is built with `shot_name`."""
    ctx: dict = {}
    path = normalize(path)
    for pat in patterns:
        m = _compile_context(pat).search(path)
        if m:
            for k, v in m.groupdict().items():
                ctx.setdefault(k, v)
    if "shot" not in ctx and ctx.get("shot_short") and ctx.get("seq"):
        ctx["shot"] = shot_name.format(seq=ctx["seq"], shot_short=ctx["shot_short"])
    return ctx

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def source_stem(filename: str, directory: str) -> str:
    """'bpash_1545.preprocess.mp01_png.v1.%d.png' -> 'bpash_1545.preprocess.mp01_png.v1'
    (frame number and extension dropped). Used to name outputs after their source."""
    if not filename:
        stem = directory.rstrip("/").rpartition("/")[2]
    else:
        base, _ext = os.path.splitext(filename)
        t = _TOKEN_RE.search(base)
        if t:
            before, after = base[: t.start()].rstrip("._-"), base[t.end():].strip("._-")
            stem = before + (f"_{after}" if after else "")
        else:
            stem = base
    return _SAFE_NAME.sub("_", stem).strip("._-") or "output"

# Sequences converted to PNG before the workflow (see convert.py).
CONVERTED_SEQUENCE_EXTENSIONS = {".exr"}


def split_input_path(farm_path: str) -> tuple[str, str]:
    """(directory with trailing '/', file name or pattern); the name is '' for a folder."""
    if farm_path.endswith("/"):
        return farm_path, ""
    directory, _, filename = farm_path.rpartition("/")
    if "." not in filename and not _TOKEN_RE.search(filename):
        return farm_path + "/", ""   # a folder without trailing slash
    return directory + "/", filename


def check_writable(path: str, cfg: PathsConfig) -> None:
    """Refuse locked areas such as .published (publishing goes through Tractor)."""
    parts = {p.lower() for p in normalize(path).split("/")}
    for d in cfg.no_write_dirs:
        if d.lower() in parts:
            raise SequenceError(f"Can't write into {d}: it's locked, and publishing goes through "
                                f"Tractor. Pick a folder outside {d}.")


def template_fields(template: str) -> set[str]:
    return {f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(template) if f}


def render(template: str, what: str, **tokens) -> str:
    """str.format, but a token we couldn't work out gives a readable error instead of
    'None' ending up in a file name."""
    missing = sorted(k for k in template_fields(template) if tokens.get(k) in (None, "") and k != "frame")
    if missing:
        raise SequenceError(
            f"Can't build the {what}: no {', '.join(missing)} from the input path (expected a "
            f"publish, .../shows/<show>/.published/<seq>/<shot>/..., or a shot folder such as "
            f".../shows/<show>/<seq>/<shot>/movhistory/...). Give an Output folder instead.")
    return template.format(**tokens)
