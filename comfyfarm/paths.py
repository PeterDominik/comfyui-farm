"""Path mapping between Linux workstations and the Windows farm machine, and image
sequence resolution (frame patterns, frame ranges, what the loader node will read)."""

from __future__ import annotations

import os
import re
import string
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
    seq_name: str | None = None   # sequence code from the path, e.g. "bpash"
    source: str = ""              # input file name without frame number/extension
    kind: str = "frames"          # frames (read directly) | exr | movie (converted first)
    load_dir: str | None = None   # where the loader reads from, when not `directory`
    convert: dict | None = None   # how to make the frames the loader reads (exr/movie)

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


def _format_frames(frames: list[int], limit: int = 12) -> str:
    shown = ", ".join(str(f) for f in frames[:limit])
    return shown + (f" … ({len(frames)} total)" if len(frames) > limit else "")


# Sequences converted to PNG before the workflow (see convert.py).
CONVERTED_SEQUENCE_EXTENSIONS = {".exr"}


def _resolve_converted_sequence(seq: Sequence, regex, cfg: PathsConfig, listdir) -> Sequence:
    """EXR etc: every wanted frame must exist; the converter writes exactly those frames,
    so the loader needs no skip/cap."""
    first, last = seq.first, seq.last
    if not cfg.validate_inputs:
        seq.frame_count = (last - first + 1) if first is not None else None
        return seq
    try:
        names = listdir(seq.directory)
    except OSError as e:
        raise SequenceError(f"The farm machine can't open the input folder {seq.directory} ({e.strerror or e}).")
    if regex is None:   # one file
        if seq.filename not in names:
            raise SequenceError(f"{seq.filename} not found in {seq.directory}.")
        seq.first = seq.last = first if first is not None else 1
        seq.frame_count = 1
        return seq
    frames = {int(m.group(1)) for m in map(regex.match, names) if m}
    if not frames:
        raise SequenceError(f"No frames matching {seq.filename} in {seq.directory}.")
    if first is None:
        first, last = min(frames), max(frames)
    missing = [f for f in range(first, last + 1) if f not in frames]
    if missing:
        raise SequenceError(f"Missing frames in {seq.filename}: {_format_frames(missing)}.")
    seq.first, seq.last, seq.frame_count = first, last, last - first + 1
    return seq


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
    ext = os.path.splitext(filename)[1].lower() if filename else ""
    if filename and ext not in LOADER_EXTENSIONS and ext not in CONVERTED_SEQUENCE_EXTENSIONS:
        raise SequenceError(
            f"Can't use '{ext}' files. Use a png, jpg, tif, webp or EXR sequence, or a "
            f".mov/.mp4 movie."
        )

    ctx = context_from_path(farm_path, cfg.context_patterns, cfg.shot_name)
    show = show or ctx.get("show")
    shot = shot or ctx.get("shot")   # None when the path isn't a publish path; never guessed

    seq = Sequence(raw=raw.strip(), farm_path=farm_path, directory=directory, filename=filename,
                   first=first, last=last, frame_count=None, skip=0, cap=0, show=show, shot=shot,
                   seq_name=ctx.get("seq"), source=source_stem(filename, directory))
    if ext in CONVERTED_SEQUENCE_EXTENSIONS:
        seq.kind = "exr"
        return _resolve_converted_sequence(seq, regex, cfg, listdir)
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


def check_writable(path: str, cfg: PathsConfig) -> None:
    """Refuse locked areas such as .published (publishing goes through Tractor)."""
    parts = {p.lower() for p in normalize(path).split("/")}
    for d in cfg.no_write_dirs:
        if d.lower() in parts:
            raise SequenceError(f"Can't write into {d}: it's locked, and publishing goes through "
                                f"Tractor. Pick a folder outside {d}.")


def output_root(cfg: PathsConfig, show: str | None, seq: str | None = None, shot: str | None = None) -> str:
    if "{show}" in cfg.output_root and not show:
        raise SequenceError(
            "Couldn't work out the show from the input path. Give an Output folder instead."
        )
    return normalize(cfg.output_root.format(show=show or "", seq=seq or "", shot=shot or "")).rstrip("/")


def default_output_root_display(cfg: PathsConfig) -> str:
    """The config default as artists see it, e.g. /rdo/shows/{show}/.../renders."""
    root = normalize(cfg.output_root).rstrip("/")
    return to_posix(root, cfg.mappings) or root


def default_output_dir_display(cfg: PathsConfig, tag: str) -> str:
    """e.g. /rdo/shows/{show}/.../renders/{shot}/{shot}_alpha_v###"""
    d = cfg.output_dir.replace("{root}", default_output_root_display(cfg)).replace("{tag}", tag)
    return re.sub(r"\{version[^}]*\}", "###", d)


def resolve_output_folder(raw: str, cfg: PathsConfig, isdir=os.path.isdir) -> str:
    """The folder an artist typed -> farm path. It replaces output_root; the shot
    folder goes inside it. Its parent must exist (catches typos without creating junk
    folder trees on the share); the folder itself is created if needed."""
    path = to_farm(raw, cfg.mappings).rstrip("/")
    if not path:
        raise SequenceError("Output folder is empty.")
    if _TOKEN_RE.search(path.rpartition("/")[2]) or re.search(r"\s\d+\s*-\s*\d+$", path) \
            or os.path.splitext(path)[1].lower() in LOADER_EXTENSIONS | {".exr", ".mov", ".mp4"}:
        raise SequenceError("Output folder should be a folder, not a file, frame pattern or range.")
    if ".." in path.split("/"):
        raise SequenceError("Output folder can't contain '..'.")
    if not (path.startswith("//") or re.match(r"^[A-Za-z]:/", path) or path.startswith("/")):
        raise SequenceError("Output folder must be a full path.")
    allowed = [to_farm(a, cfg.mappings).rstrip("/") for a in cfg.allowed_output_roots] \
        or [normalize(m.windows).rstrip("/") for m in cfg.mappings]
    if allowed and not any(_starts_with(path, a, case_insensitive=True) for a in allowed):
        shown = ", ".join(to_posix(a, cfg.mappings) or a for a in allowed)
        raise SequenceError(f"Output folder must be under {shown}.")
    check_writable(path, cfg)
    parent = path.rpartition("/")[0]
    if cfg.validate_inputs and parent and not isdir(parent):
        shown = to_posix(parent, cfg.mappings) or parent
        raise SequenceError(f"{shown} doesn't exist, or the farm machine can't see it. "
                            f"Create it first (only the last folder is created for you).")
    return path


def sequence_exists(directory: str, pattern: str, listdir=os.listdir) -> bool:
    """Any file of this frame pattern (e.g. 'x.alpha_v002.%04d.png') already in the folder?"""
    try:
        names = listdir(directory)
    except OSError:   # folder doesn't exist yet
        return False
    rx = _pattern_regex(pattern)
    return any(rx.match(n) for n in names) if rx else pattern in names


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


def output_dir(template: str, **tokens) -> str:
    return normalize(render(template, "output folder", **tokens)).rstrip("/")
