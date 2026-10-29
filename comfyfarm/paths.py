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
    kind: str = "frames"          # frames (read directly) | exr | movie | image (converted first)
    load_dir: str | None = None   # where the loader reads from, when not `directory`
    convert: dict | None = None   # how to make the frames the loader reads (exr/movie/image)
    frames: list[int] | None = None   # the numbers read, when gaps are allowed (UDIM tiles)
    sizes: list | None = None     # (width, height) of each file read, when all were read

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


def _wanted_frames(frames, first: int | None, last: int | None, filename: str,
                   allow_gaps: bool) -> list[int]:
    """The frame numbers to read. Without gaps allowed, every frame of the range must be
    there; with them (UDIM tiles: 1001, 1002, 1011, ...), whichever are in the range."""
    if first is None:
        first, last = min(frames), max(frames)
    if allow_gaps:
        wanted = sorted(f for f in frames if first <= f <= last)
        if not wanted:
            raise SequenceError(f"No files of {filename} between {first} and {last}.")
        return wanted
    missing = [f for f in range(first, last + 1) if f not in frames]
    if missing:
        raise SequenceError(f"Missing frames in {filename}: {_format_frames(missing)}.")
    return list(range(first, last + 1))


def _resolve_converted_sequence(seq: Sequence, regex, cfg: PathsConfig, listdir,
                                allow_gaps: bool = False) -> Sequence:
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
    wanted = _wanted_frames(frames, first, last, seq.filename, allow_gaps)
    seq.first, seq.last, seq.frame_count = wanted[0], wanted[-1], len(wanted)
    if allow_gaps:
        seq.frames = wanted
    return seq


def split_input_path(farm_path: str) -> tuple[str, str]:
    """(directory with trailing '/', file name or pattern); the name is '' for a folder."""
    if farm_path.endswith("/"):
        return farm_path, ""
    directory, _, filename = farm_path.rpartition("/")
    if "." not in filename and not _TOKEN_RE.search(filename):
        return farm_path + "/", ""   # a folder without trailing slash
    return directory + "/", filename


def _loader_listing(directory: str, listdir, isfile) -> list[str]:
    """What VHS "Load Images (Path)" lists: sorted names, files only, loader extensions only."""
    return [n for n in sorted(listdir(directory)) if os.path.splitext(n)[1].lower() in LOADER_EXTENSIONS
            and isfile(directory + n)]


def resolve_sequence(
    raw: str,
    cfg: PathsConfig,
    show: str | None = None,
    shot: str | None = None,
    listdir=os.listdir,
    isfile=os.path.isfile,
    allow_gaps: bool = False,
) -> Sequence:
    """Turn what an artist typed into exactly what the loader node will read.

    VHS "Load Images (Path)" loads *every* image in a folder, sorted by name, then
    applies skip_first_images / image_load_cap. So for a frame range we list the
    folder the same way and work out skip/cap, refusing anything that would quietly
    load the wrong frames (gaps, foreign images interleaved, unreadable formats).
    `allow_gaps` is for UDIM tiles, which skip numbers (1001, 1002, 1011, ...).
    """
    if not raw or not raw.strip():
        raise SequenceError("No input sequence given.")
    path, first, last = parse_input(raw)
    farm_path = to_farm(path, cfg.mappings)
    directory, filename = split_input_path(farm_path)

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
        return _resolve_converted_sequence(seq, regex, cfg, listdir, allow_gaps)
    if not cfg.validate_inputs:
        if first is not None:
            seq.frame_count = last - first + 1
        return seq

    try:
        # Same listing VHS uses: sorted names, files only, loader extensions only.
        loadable = _loader_listing(directory, listdir, isfile)
    except OSError as e:
        raise SequenceError(f"The farm machine can't open the input folder {directory} ({e.strerror or e}).")
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

    wanted = _wanted_frames(frames, first, last, filename, allow_gaps)
    first, last = wanted[0], wanted[-1]
    idxs = [frames[f] for f in wanted]
    if idxs != list(range(idxs[0], idxs[0] + len(idxs))):
        raise SequenceError(
            f"Other images in {directory} sort between frames {first}-{last}, so the loader "
            f"can't read the range on its own. Move them or give a cleaner folder."
        )
    seq.first, seq.last = first, last
    seq.skip, seq.cap, seq.frame_count = idxs[0], len(idxs), len(idxs)
    if allow_gaps:
        seq.frames = wanted
    if seq.skip == 0 and seq.cap == len(loadable):
        seq.cap = 0
    return seq


def loaded_names(seq: Sequence, listdir=os.listdir, isfile=os.path.isfile) -> list[str]:
    """The source file behind each image the workflow gets, in the order it gets them.
    For EXRs that's the original EXR of each converted frame; for images scaled down to
    the workflow's work_resolution, the original image."""
    if seq.kind == "image":
        return list((seq.convert or {}).get("names") or [])
    if seq.kind == "exr":
        regex = _pattern_regex(seq.filename)
        if regex is None:
            return [seq.filename]
        by_frame = {int(m.group(1)): n for n, m in ((n, regex.match(n)) for n in listdir(seq.directory)) if m}
        frames = seq.frames or range(seq.first, seq.last + 1)
        return [by_frame[f] for f in frames if f in by_frame]
    if seq.kind != "frames":
        return []
    loadable = _loader_listing(seq.directory, listdir, isfile)
    return loadable[seq.skip: seq.skip + seq.cap] if seq.cap else loadable[seq.skip:]


# ============================================================== texture file names
# <name><sep><number>[<sep><number>].<ext>: rock_diffuse.1001.png, or as the TEX
# convention writes them, acescg_map1_noTile_defState_color01.1001.0000.exr (UDIM, frame).
_TEXTURE_NAME = re.compile(r"^(?P<stem>.*?)(?:(?P<sa>[._])(?P<a>\d+))?(?:(?P<sb>[._])(?P<b>\d+))?"
                           r"\.(?P<ext>[A-Za-z0-9]+)$")
TEXTURE_EXTENSIONS = LOADER_EXTENSIONS | CONVERTED_SEQUENCE_EXTENSIONS


def _is_udim(number: str | None) -> bool:
    return bool(number) and len(number) == 4 and int(number) >= 1001


def texture_numbers(name: str, numbering: str = "udim") -> tuple[int, int, bool]:
    """(UDIM, frame, whether the name carried a UDIM) for one texture file.

    Two numbers are <UDIM>.<frame>, as in the convention. One number is a UDIM tile
    (4 digits, 1001 and up) or, with numbering='frame', an animation frame. No number
    (or one that isn't a UDIM) is a single 1001 tile, frame 0."""
    m = _TEXTURE_NAME.match(name)
    a, b = (m.group("a"), m.group("b")) if m else (None, None)
    if b is not None and _is_udim(a):
        return int(a), int(b), True
    single = b if b is not None else a
    if single is not None:
        if numbering == "frame":
            return 1001, int(single), False
        if _is_udim(single):
            return int(single), 0, True
    return 1001, 0, False


def texture_input(raw: str, cfg: PathsConfig, listdir=os.listdir, isfile=os.path.isfile) -> str:
    """For texture workflows: a folder means the one texture inside it. Returns the input
    with the folder replaced by that texture's file or pattern (rock_diffuse.%04d.png),
    so its UDIM tiles are read and checked like any sequence. Anything else is returned
    unchanged."""
    path, first, last = parse_input(raw)
    directory, filename = split_input_path(to_farm(path, cfg.mappings))
    if filename:
        return raw
    try:
        names = sorted(n for n in listdir(directory)
                       if os.path.splitext(n)[1].lower() in TEXTURE_EXTENSIONS and isfile(directory + n))
    except OSError as e:
        raise SequenceError(f"The farm machine can't open the input folder {directory} ({e.strerror or e}).")
    if not names:
        raise SequenceError(f"No texture images (png, jpg, tif, exr…) in {directory}.")
    groups: dict[tuple, list] = {}
    for n in names:
        m = _TEXTURE_NAME.match(n)
        key = (m["stem"], m["sa"], len(m["a"] or ""), m["sb"], len(m["b"] or ""), m["ext"].lower())
        groups.setdefault(key, []).append(m)
    if len(groups) > 1:
        shown = sorted({ms[0].group(0) if len(ms) == 1 else f"{ms[0]['stem']} ({len(ms)} files)"
                        for ms in groups.values()})
        raise SequenceError(
            f"{directory} holds {len(groups)} different textures ({', '.join(shown[:6])}"
            f"{', …' if len(shown) > 6 else ''}). Give the diffuse texture's file or sequence instead of "
            f"the folder, one job per texture.")
    (stem, sa, la, sb, lb, _), ms = next(iter(groups.items()))
    ext = ms[0]["ext"]
    if len(ms) == 1:
        name = ms[0].group(0)
    else:
        a_vary, b_vary = len({m["a"] for m in ms}) > 1, len({m["b"] for m in ms}) > 1
        if a_vary and b_vary:
            raise SequenceError(f"{directory} holds UDIM tiles of several frames ({stem}.<UDIM>.<frame>). "
                                f"Give one frame's tiles, e.g. {stem}{sa}%0{la}d{sb}{ms[0]['b']}.{ext}.")
        if b_vary:
            name = f"{stem}{sa}{ms[0]['a']}{sb}%0{lb}d.{ext}"
        else:
            name = f"{stem}{sa}%0{la}d" + (f"{sb}{ms[0]['b']}" if sb else "") + f".{ext}"
    rng = f" {first}-{last}" if first is not None else ""
    return f"{path.rstrip('/')}/{name}{rng}"


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


def default_output_dir_display(cfg: PathsConfig, tag: str, template: str | None = None) -> str:
    """e.g. /rdo/shows/{show}/.../renders/{shot}/{shot}_alpha_v###"""
    d = (template or cfg.output_dir).replace("{root}", default_output_root_display(cfg)).replace("{tag}", tag)
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
