"""Path mapping between Linux workstations and the Windows farm machine, and image
sequence resolution (frame patterns, frame ranges, what the loader node will read)."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass

from .config import PathMapping, PathsConfig

# Extensions VHS "Load Images (Path)" reads (ComfyUI's FolderOfImages.IMG_EXTENSIONS).
LOADER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".ppm", ".bmp", ".pgm", ".tif", ".tiff", ".webp"}

# %d, %04d, ####, @@@@, $F4
_TOKEN_RE = re.compile(r"%0?(\d*)d|#+|@+|\$F(\d*)")
# "<path> 1001-1158" (Nuke/pipeline style, range at the end)
_RANGE_RE = re.compile(r"^(?P<path>.*?\S)\s+(?P<first>-?\d+)\s*-\s*(?P<last>-?\d+)\s*$")


class SequenceError(ValueError):
    """The input can't be used; the message is shown to the artist as-is."""


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


@dataclass
class Sequence:
    raw: str
    farm_path: str          # full farm path, pattern included
    directory: str          # farm directory with trailing "/"
    filename: str           # file name or pattern; "" when a whole folder was given
    first: int | None
    last: int | None
    frame_count: int | None
    skip: int               # -> VHS skip_first_images
    cap: int                # -> VHS image_load_cap (0 = all)
    show: str | None
    shot: str | None

    def as_dict(self) -> dict:
        return asdict(self)


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


def _extract(regex: str, text: str, group: str) -> str | None:
    m = re.search(regex, text)
    if not m:
        return None
    return m.groupdict().get(group) or m.group(0)


def _format_frames(frames: list[int], limit: int = 12) -> str:
    shown = ", ".join(str(f) for f in frames[:limit])
    return shown + (f" … ({len(frames)} total)" if len(frames) > limit else "")


def resolve_sequence(
    raw: str,
    cfg: PathsConfig,
    show: str | None = None,
    shot: str | None = None,
    listdir=os.listdir,
    isfile=os.path.isfile,
) -> Sequence:
    """Turn what an artist typed into exactly what the loader node will read.

    VHS "Load Images (Path)" loads *every* image in a folder, sorted by name, then
    applies skip_first_images / image_load_cap. So for a frame range we list the
    folder the same way and work out skip/cap, refusing anything that would quietly
    load the wrong frames (gaps, foreign images interleaved, unreadable formats).
    """
    if not raw or not raw.strip():
        raise SequenceError("No input sequence given.")
    path, first, last = parse_input(raw)
    farm_path = to_farm(path, cfg.mappings)

    if farm_path.endswith("/"):
        directory, filename = farm_path, ""
    else:
        directory, _, filename = farm_path.rpartition("/")
        directory += "/"
        if "." not in filename and not _TOKEN_RE.search(filename):
            directory, filename = farm_path + "/", ""   # a folder without trailing slash

    regex = _pattern_regex(filename) if filename else None
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in LOADER_EXTENSIONS:
            raise SequenceError(
                f"The loader can't read '{ext}' files. Use png, jpg, tif or webp "
                f"(convert EXRs first)."
            )

    show = show or _extract(cfg.show_regex, farm_path, "show")
    shot = shot or _extract(cfg.shot_regex, filename, "shot") or _extract(cfg.shot_regex, farm_path, "shot")
    if not shot:
        shot = re.sub(r"[^A-Za-z0-9_]+", "_", directory.rstrip("/").rpartition("/")[2]) or "noshot"

    seq = Sequence(raw=raw.strip(), farm_path=farm_path, directory=directory, filename=filename,
                   first=first, last=last, frame_count=None, skip=0, cap=0, show=show, shot=shot)
    if not cfg.validate_inputs:
        if first is not None:
            seq.frame_count = last - first + 1
        return seq

    try:
        names = sorted(listdir(directory))
    except OSError as e:
        raise SequenceError(f"The farm machine can't open the input folder {directory} ({e.strerror or e}).")
    # Same listing VHS uses: sorted names, files only, loader extensions only.
    loadable = [n for n in names if os.path.splitext(n)[1].lower() in LOADER_EXTENSIONS
                and isfile(directory + n)]
    if not loadable:
        raise SequenceError(f"No loadable images in {directory}.")

    if regex is None:
        if filename:   # a single still
            if filename not in loadable:
                raise SequenceError(f"{filename} not found in {directory}.")
            idx = loadable.index(filename)
            seq.skip, seq.cap, seq.frame_count = idx, 1, 1
            return seq
        seq.skip, seq.cap, seq.frame_count = 0, 0, len(loadable)
        return seq

    frames: dict[int, int] = {}   # frame -> index in loader listing
    for idx, name in enumerate(loadable):
        m = regex.match(name)
        if m:
            frames[int(m.group(1))] = idx
    if not frames:
        raise SequenceError(f"No frames matching {filename} in {directory}.")

    if first is None:
        first, last = min(frames), max(frames)
    wanted = range(first, last + 1)
    missing = [f for f in wanted if f not in frames]
    if missing:
        raise SequenceError(f"Missing frames in {filename}: {_format_frames(missing)}.")

    idxs = [frames[f] for f in wanted]
    if idxs != list(range(idxs[0], idxs[0] + len(idxs))):
        raise SequenceError(
            f"Other images in {directory} sort between frames {first}-{last}, so the loader "
            f"can't read the range on its own. Move them or give a cleaner folder."
        )
    seq.first, seq.last = first, last
    seq.skip, seq.cap, seq.frame_count = idxs[0], len(idxs), len(idxs)
    if seq.skip == 0 and seq.cap == len(loadable):
        seq.cap = 0
    return seq


def output_root(cfg: PathsConfig, show: str | None, shot: str) -> str:
    if "{show}" in cfg.output_root and not show:
        raise SequenceError(
            "Couldn't work out the show from the input path; pass 'show' with the job."
        )
    return normalize(cfg.output_root.format(show=show or "", shot=shot)).rstrip("/")


def default_output_root_display(cfg: PathsConfig) -> str:
    """The config default as artists see it, e.g. /rdo/shows/{show}/.../renders."""
    root = normalize(cfg.output_root).rstrip("/")
    return to_posix(root, cfg.mappings) or root


def resolve_output_root(raw: str, cfg: PathsConfig, isdir=os.path.isdir) -> str:
    """An output folder an artist picked -> farm path, refusing anything unsafe.

    The folder must already exist (catches typos that would otherwise create junk
    folders on the share) and sit on storage the farm writes to."""
    path = to_farm(raw, cfg.mappings).rstrip("/")
    if not path:
        raise SequenceError("Output folder is empty.")
    if _TOKEN_RE.search(path.rpartition("/")[2]) or re.search(r"\s\d+\s*-\s*\d+$", path):
        raise SequenceError("Output folder should be a folder, not a frame pattern or range.")
    if ".." in path.split("/"):
        raise SequenceError("Output folder can't contain '..'.")
    allowed = [to_farm(a, cfg.mappings).rstrip("/") for a in cfg.allowed_output_roots] \
        or [normalize(m.windows).rstrip("/") for m in cfg.mappings]
    if allowed and not any(_starts_with(path, a, case_insensitive=True) for a in allowed):
        shown = ", ".join(to_posix(a, cfg.mappings) or a for a in allowed)
        raise SequenceError(f"Output folder must be under {shown}.")
    if not (path.startswith("//") or re.match(r"^[A-Za-z]:/", path) or path.startswith("/")):
        raise SequenceError("Output folder must be a full path.")
    if cfg.validate_inputs and not isdir(path):
        raise SequenceError(f"Output folder {raw.strip()} doesn't exist, or the farm machine can't see it. "
                            f"Create it first.")
    return path


def output_dir(cfg: PathsConfig, root: str, show: str | None, shot: str, tag: str, version: int) -> str:
    return normalize(cfg.output_dir.format(root=root, show=show or "", shot=shot, tag=tag, version=version))
