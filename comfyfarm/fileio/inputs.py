"""What an artist typed -> exactly what the workflow's loader will read.

Image sequences (frame patterns, ranges, UDIM tiles with gaps), a folder holding one
texture, and movies (numbered from their timecode, slate dropped). Inputs ComfyUI can't
read directly carry `convert` details (EXR, movie, or an image larger than the
workflow's work_resolution); fileio.convert carries them out. Read-only: nothing here
writes a file."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import ConvertConfig, OcioConfig, PathsConfig

from .errors import SequenceError
from .media import fit_size, image_size, read_movie_info, timecode_to_frame
from .ocio import _config_display, ocio_setup
from .paths import (CONVERTED_SEQUENCE_EXTENSIONS, LOADER_EXTENSIONS, _pattern_regex, context_from_path, normalize,
                    parse_input, source_stem, split_input_path, to_farm, to_posix)
from .tools import require_tool

__all__ = ["Sequence", "MOVIE_EXTENSIONS", "TEXTURE_EXTENSIONS", "resolve_sequence", "loaded_names", "texture_numbers",
           "folder_input", "texture_input", "resolve_movie", "has_slate", "scaled_text", "resolve_input", "work_sizes",
           "batch_sequence", "load_path", "summary"]


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


def _format_frames(frames: list[int], limit: int = 12) -> str:
    shown = ", ".join(str(f) for f in frames[:limit])
    return shown + (f" … ({len(frames)} total)" if len(frames) > limit else "")


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
    listing: bool = True,
) -> Sequence:
    """Turn what an artist typed into exactly what the loader node will read.

    `listing`: the loader lists the folder, as VHS "Load Images (Path)" does: it loads
    *every* image in it, sorted by name, then applies skip_first_images / image_load_cap.
    So for a frame range we list the folder the same way and work out skip/cap, refusing
    anything that would quietly load the wrong frames (gaps, foreign images interleaved,
    unreadable formats). Without it (ComfyFarm Load Images, which reads the exact files of
    a pattern and frame list), every wanted frame must simply be there.
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
    if not listing and filename:        # the exact files: no folder listing to fit
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
    regex = _pattern_regex(seq.filename) if seq.filename else None
    if regex is not None:               # a pattern: its frames (VHS was checked to read exactly those)
        by_frame = {int(m.group(1)): n for n, m in ((n, regex.match(n)) for n in listdir(seq.directory)) if m}
        frames = seq.frames or range(seq.first, seq.last + 1)
        return [by_frame[f] for f in frames if f in by_frame]
    if seq.filename and not seq.cap:    # one image, read as it is
        return [seq.filename]
    loadable = _loader_listing(seq.directory, listdir, isfile)
    return loadable[seq.skip: seq.skip + seq.cap] if seq.cap else loadable[seq.skip:]

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


def folder_input(raw: str, cfg: PathsConfig, listdir=os.listdir, isfile=os.path.isfile,
                 texture: bool = False) -> str:
    """A folder means the one sequence (or texture, with its UDIM tiles) inside it. Returns
    the input with the folder replaced by that sequence's file or pattern
    (rock_diffuse.%04d.png), so it is read and checked like any sequence; a folder holding
    several is refused with their names. Anything else is returned unchanged."""
    path, first, last = parse_input(raw)
    directory, filename = split_input_path(to_farm(path, cfg.mappings))
    if filename:
        return raw
    what = "texture" if texture else "image sequence"
    try:
        names = sorted(n for n in listdir(directory)
                       if os.path.splitext(n)[1].lower() in TEXTURE_EXTENSIONS and isfile(directory + n))
    except OSError as e:
        raise SequenceError(f"The farm machine can't open the input folder {directory} ({e.strerror or e}).")
    if not names:
        raise SequenceError(f"No {'texture ' if texture else ''}images (png, jpg, tif, exr…) in {directory}.")
    groups: dict[tuple, list] = {}
    for n in names:
        m = _TEXTURE_NAME.match(n)
        key = (m["stem"], m["sa"], len(m["a"] or ""), m["sb"], len(m["b"] or ""), m["ext"].lower())
        groups.setdefault(key, []).append(m)
    if len(groups) > 1:
        shown = sorted({ms[0].group(0) if len(ms) == 1 else f"{ms[0]['stem']} ({len(ms)} files)"
                        for ms in groups.values()})
        raise SequenceError(
            f"{directory} holds {len(groups)} different {what}s ({', '.join(shown[:6])}"
            f"{', …' if len(shown) > 6 else ''}). Give the "
            + ("diffuse texture's file or sequence instead of the folder, one job per texture."
               if texture else "sequence's file pattern instead of the folder, e.g. …/plate.%04d.png."))
    (stem, sa, la, sb, lb, _), ms = next(iter(groups.items()))
    ext = ms[0]["ext"]
    if len(ms) == 1:
        name = ms[0].group(0)
    else:
        a_vary, b_vary = len({m["a"] for m in ms}) > 1, len({m["b"] for m in ms}) > 1
        if a_vary and b_vary:
            if not texture:
                raise SequenceError(f"{directory} holds files numbered two ways ({stem}.<n>.<n>). Give the "
                                    f"sequence's file pattern instead of the folder.")
            raise SequenceError(f"{directory} holds UDIM tiles of several frames ({stem}.<UDIM>.<frame>). "
                                f"Give one frame's tiles, e.g. {stem}{sa}%0{la}d{sb}{ms[0]['b']}.{ext}.")
        if b_vary:
            name = f"{stem}{sa}{ms[0]['a']}{sb}%0{lb}d.{ext}"
        else:
            name = f"{stem}{sa}%0{la}d" + (f"{sb}{ms[0]['b']}" if sb else "") + f".{ext}"
    rng = f" {first}-{last}" if first is not None else ""
    return f"{path.rstrip('/')}/{name}{rng}"


def texture_input(raw: str, cfg: PathsConfig, listdir=os.listdir, isfile=os.path.isfile) -> str:
    """For texture workflows: a folder means the one texture inside it (folder_input)."""
    return folder_input(raw, cfg, listdir, isfile, texture=True)

MOVIE_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mxf"}


def resolve_movie(raw: str, pc: PathsConfig, cc: ConvertConfig, show: str | None = None,
                  shot: str | None = None) -> Sequence:
    path, first, last = parse_input(raw)
    farm_path = to_farm(path, pc.mappings)
    if not os.path.isfile(farm_path):
        raise SequenceError(f"{to_posix(farm_path, pc.mappings) or farm_path} doesn't exist, or the farm "
                            f"machine can't see it.")
    directory, _, filename = farm_path.rpartition("/")
    ctx = context_from_path(farm_path, pc.context_patterns, pc.shot_name)
    info = read_movie_info(farm_path, cc)
    slate = has_slate(farm_path, cc)
    if slate and info["frames"] < 2:
        raise SequenceError("The movie only has its slate frame.")
    notes = []
    if info["timecode"]:
        try:
            tc_start = timecode_to_frame(info["timecode"], info["fps"])   # the movie's first frame
        except ValueError as e:
            raise SequenceError(f"The movie's timecode can't be used: {e}.")
    else:
        tc_start = cc.movie.default_start - (1 if slate else 0)
        notes.append(f"The movie has no timecode, so frames are numbered from {cc.movie.default_start}.")
    # A slate keeps its timecode number but isn't used: the plate starts one frame later.
    start = tc_start + (1 if slate else 0)
    end = tc_start + info["frames"] - 1
    if first is None:
        first, last = start, end
    elif first < start or last > end:
        what = f"the movie's plate runs {start}-{end} (frame {tc_start} is the slate)" if slate \
            else f"the movie runs {start}-{end}"
        raise SequenceError(f"Frames {first}-{last} are outside the movie: {what}, from its timecode "
                            f"{info['timecode'] or 'none'}.")
    seq = Sequence(raw=raw.strip(), farm_path=farm_path, directory=directory + "/", filename=filename,
                   first=first, last=last, frame_count=last - first + 1, skip=0, cap=0,
                   show=show or ctx.get("show"), shot=shot or ctx.get("shot"), seq_name=ctx.get("seq"),
                   source=source_stem(filename, ""), kind="movie")
    seq.convert = {"kind": "movie", "src": farm_path, "start": start, "end": end,
                   "slate_frame": tc_start if slate else None,
                   "index_first": first - tc_start, "index_last": last - tc_start, "notes": notes, **info}
    return seq


def has_slate(path: str, cc: ConvertConfig) -> bool:
    """First frame is a slate: a .mov inside a movhistory folder (see convert.movie.slate)."""
    sl = cc.movie.slate
    parts = [p.lower() for p in normalize(path).split("/")[:-1]]
    ext = os.path.splitext(path)[1].lower()
    return ext in {e.lower() for e in sl.extensions} and any(f.lower() in parts for f in sl.folders)


def _sizes(seq: Sequence, names: list[str], every: bool) -> list[tuple[int, int] | None]:
    """Image sizes of the input's files: all of them, or the first only (a sequence's
    frames share their size; UDIM tiles may not)."""
    return [image_size(seq.directory + n) for n in (names if every else names[:1])]


def scaled_text(sizes: list[tuple[int, int] | None], limit: int | None) -> str | None:
    """'4096×4096 → 1024×1024' when work_resolution scales the input down, else None.
    With several sizes, the first that's scaled, and how many are."""
    if not limit:
        return None
    big = [s for s in sizes if s and max(s) > limit]
    if not big:
        return None
    (w, h), (fw, fh) = big[0], fit_size(big[0], limit)
    text = f"{w}×{h} → {fw}×{fh}"
    return text + (f" ({len(big)} of {len(sizes)} images)" if len(sizes) > 1 and len(big) < len(sizes) else "")


def resolve_input(raw: str, pc: PathsConfig, cc: ConvertConfig, show: str | None = None,
                  shot: str | None = None, texture: bool = False, ocio: OcioConfig | None = None,
                  work_resolution: int | None = None, preview: bool = False, listing: bool = True) -> Sequence:
    """Any supported input -> a Sequence. Converted kinds carry `convert` details.

    A folder means the one sequence or texture inside it (folder_input). `listing`: the
    workflow's loader lists the folder (VHS Load Images (Path)) rather than reading the
    exact files (ComfyFarm Load Images); see resolve_sequence.

    With `texture` (texture workflows such as CHORD): a folder means the texture in it,
    UDIM tiles may skip numbers, and movies are refused. EXRs are converted the same way
    for every workflow (`ocio`).

    `work_resolution` (the workflow's manifest): images larger than this many pixels on
    their longer side are scaled down to it before the workflow, keeping their aspect.
    EXRs are scaled as they're converted; png/jpg/tif inputs that are too large are
    converted for it too (kind "image"), and read as they are when they fit.

    Texture inputs have every tile's size read (UDIM tiles may differ), into seq.sizes;
    a `preview` (the dashboard's live destination) reads only the first, which is enough
    to show what will happen and doesn't read hundreds of headers per keystroke."""
    if not raw or not raw.strip():
        raise SequenceError("No input sequence given.")
    path, _, _ = parse_input(raw)
    if os.path.splitext(path)[1].lower() in MOVIE_EXTENSIONS:
        if texture:
            raise SequenceError("This workflow takes texture images (png, jpg, tif or exr), not movies.")
        return resolve_movie(raw, pc, cc, show, shot)
    if pc.validate_inputs:
        raw = folder_input(raw, pc, texture=texture)
    seq = resolve_sequence(raw, pc, show=show, shot=shot, allow_gaps=texture, listing=listing)
    limit = work_resolution or None
    every = texture and not preview
    if seq.kind == "exr":
        scaled = None
        if pc.validate_inputs:
            require_tool(cc, "oiiotool")
            if limit:
                sizes = _sizes(seq, loaded_names(seq), every)
                seq.sizes = sizes if every else None
                scaled = scaled_text(sizes, limit)
        seq.convert = {"kind": "exr", "src_dir": seq.directory, "src_pattern": seq.filename,
                       "first": seq.first, "last": seq.last, "frames": seq.frames,
                       "extra_args": list(cc.exr.extra_args), "png_compression": cc.exr.png_compression,
                       "work_resolution": limit, "scaled": scaled,
                       **ocio_setup(ocio, pc)}
    elif seq.kind == "frames" and limit and pc.validate_inputs:
        names = loaded_names(seq)
        sizes = _sizes(seq, names, every)
        seq.sizes = sizes if every else None
        scaled = scaled_text(sizes, limit)
        if scaled:   # too large: scaled into the scratch folder, like a converted EXR
            require_tool(cc, "oiiotool")
            if seq.frames:
                numbers = list(seq.frames)
            elif seq.first is not None and len(names) == seq.last - seq.first + 1:
                numbers = list(range(seq.first, seq.last + 1))
            else:            # a still, or a folder of images: numbered in the loader's order
                numbers = list(range(1, len(names) + 1))
            seq.kind = "image"
            seq.convert = {"kind": "image", "src_dir": seq.directory, "names": names, "frames": numbers,
                           "png_compression": cc.exr.png_compression, "work_resolution": limit,
                           "scaled": scaled, "notes": []}
    return seq


def work_sizes(seq: Sequence, names: list[str], limit: int | None) -> list[tuple[int, int] | None]:
    """The size each loaded image has when the workflow gets it: after work_resolution
    for converted inputs. Reads the files' headers if resolve_input didn't."""
    raw = seq.sizes if seq.sizes and len(seq.sizes) == len(names) else _sizes(seq, names, True)
    raw = [tuple(s) if s else None for s in raw]
    if seq.kind in ("exr", "image") and limit:
        return [fit_size(s, limit) if s else None for s in raw]
    return raw


def _numbers(seq: Sequence) -> list[int] | None:
    """The frame (or UDIM) number of each image the loader gets, in order."""
    if seq.kind == "image":
        return list(seq.convert["frames"])
    if seq.frames:
        return list(seq.frames)
    if seq.first is not None and seq.last is not None:
        return list(range(seq.first, seq.last + 1))
    return None


def batch_sequence(seq: Sequence, start: int, end: int) -> Sequence:
    """The part of a texture input one batch of a set reads: images start..end-1 of what
    the whole input would load. Converted inputs convert only those; read directly, the
    loader skips to them (they're consecutive in its listing, as the whole input was)."""
    b = copy.deepcopy(seq)
    nums = _numbers(seq)
    if nums:
        sub = nums[start:end]
        b.first, b.last = sub[0], sub[-1]
        if seq.frames:
            b.frames = sub
    b.frame_count = end - start
    b.sizes = seq.sizes[start:end] if seq.sizes else None
    if b.kind == "frames":
        b.skip, b.cap = seq.skip + start, end - start
    c = b.convert
    if c and c["kind"] == "exr" and nums:
        c.update(frames=sub, first=sub[0], last=sub[-1])
    elif c and c["kind"] == "image":
        c.update(names=c["names"][start:end], frames=c["frames"][start:end])
    return b


def load_path(seq: Sequence) -> tuple[str, list[int] | None]:
    """What a loader that reads exact files (ComfyFarm Load Images) gets for this input:
    the file or frame pattern, and its frames in order (None for one image read as it is).
    Converted inputs are read from the scratch folder they're converted into."""
    c = seq.convert
    if c and c.get("dst_dir"):
        return c["dst_dir"] + c["dst_pattern"], _numbers(seq) or [1]
    if _pattern_regex(seq.filename) is None:
        return seq.directory + seq.filename, None
    return seq.directory + seq.filename, _numbers(seq)


def summary(seq: Sequence | None, pc: PathsConfig) -> dict | None:
    """What the dashboard shows about a conversion."""
    if not seq or not seq.convert:
        return None
    c = seq.convert
    if c["kind"] == "exr":
        return {"kind": "exr", "mode": "colorconvert", "from": c["from"], "to": c["to"],
                "config": _config_display(c["config"], pc),
                "work_resolution": c.get("work_resolution"), "scaled": c.get("scaled")}
    if c["kind"] == "image":
        return {"kind": "image", "work_resolution": c["work_resolution"], "scaled": c["scaled"]}
    return {"kind": "movie", "timecode": c.get("timecode"), "fps": c["fps"], "start": c["start"],
            "end": c["end"], "slate_frame": c.get("slate_frame"), "codec": c.get("codec"),
            "width": c.get("width"), "height": c.get("height")}
