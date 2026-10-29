"""EXR sequences and movies -> the PNG frames the workflow reads.

One ComfyUI graph handles every input format: before a job is dispatched, the gateway
converts non-PNG inputs into a scratch folder on the farm machine, and the loader node
reads from there.

- EXR: oiiotool with the repo's OCIO config (ocio/), scene_linear -> color_picking (the
  ACES Rec.709 output transform): a Nuke Write set to "default (color_picking)".
- Movies (.mov/.mp4/...): ffmpeg, numbered from the movie's timecode. Treated as
  display-referred already (look baked in), so no colour transform.
- work_resolution (a workflow's manifest, e.g. CHORD's 1024): EXRs are scaled down to
  it as they're converted, and png/jpg/tif inputs larger than it are scaled into the
  scratch folder too (kind "image"; no colour change). Inputs that fit are untouched.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from .config import ConvertConfig, OcioConfig, PathsConfig
from .paths import (Sequence, SequenceError, _pattern_regex, context_from_path, loaded_names, normalize,
                    parse_input, resolve_sequence, source_stem, texture_input, to_farm, to_posix)

MOVIE_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mxf"}


class ConvertError(RuntimeError):
    """Conversion failed while the job was running; the message is shown to the artist."""


# ============================================================== tools
def _pip_ffmpeg() -> str | None:
    """The ffmpeg binary that ships inside the imageio-ffmpeg wheel (no ffprobe)."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and os.path.isfile(exe) else None
    except Exception:  # noqa: BLE001 - not installed, or no binary for this platform
        return None


def find_tool(name: str, configured: str | list[str] | None = None) -> list[str] | None:
    """Configured command, else the venv next to this Python (pip-installed OpenImageIO
    puts oiiotool there), else ffmpeg from the imageio-ffmpeg package, else PATH."""
    if configured:
        return list(configured) if isinstance(configured, list) else [configured]
    here = Path(sys.executable).parent
    for cand in (here / f"{name}.exe", here / name):
        if cand.is_file():
            return [str(cand)]
    if name == "ffmpeg":
        exe = _pip_ffmpeg()
        if exe:
            return [exe]
    found = shutil.which(name)
    return [found] if found else None


def require_tool(cc: ConvertConfig, name: str) -> list[str]:
    cmd = find_tool(name, cc.tools.get(name))
    if not cmd:
        hint = {"oiiotool": "run scripts\\setup.ps1 (installs OpenImageIO into the gateway's venv)",
                "ffmpeg": "run scripts\\setup.ps1 (installs imageio-ffmpeg into the gateway's venv)",
                "ffprobe": "it is optional; set convert.tools.ffprobe to use one"}[name]
        raise SequenceError(f"{name} isn't available on the farm machine: {hint}.")
    return cmd


# ============================================================== timecode / movies
_TC = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})$")


def timecode_to_frame(tc: str, fps: float) -> int:
    """'00:00:41:17' at 24 fps -> 1001. Handles drop-frame (';') at 29.97/59.94."""
    m = _TC.match(tc.strip())
    if not m:
        raise ValueError(f"unreadable timecode {tc!r}")
    h, mi, se, sep, fr = int(m[1]), int(m[2]), int(m[3]), m[4], int(m[5])
    nominal = round(fps)
    frames = (h * 3600 + mi * 60 + se) * nominal + fr
    if sep in ";.," and nominal in (30, 60) and abs(fps - nominal) > 0.001:
        drop = 2 * nominal // 30
        minutes = h * 60 + mi
        frames -= drop * (minutes - minutes // 10)
    return frames


def parse_probe(data: dict) -> dict:
    """ffprobe JSON -> what we need from a movie."""
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise SequenceError("The movie has no video stream.")
    rate = video.get("avg_frame_rate") if video.get("avg_frame_rate") not in (None, "0/0") \
        else video.get("r_frame_rate")
    try:
        fps = Fraction(rate)
    except (TypeError, ValueError, ZeroDivisionError):
        raise SequenceError("Couldn't read the movie's frame rate.")
    if fps <= 0:
        raise SequenceError("Couldn't read the movie's frame rate.")
    frames = None
    if str(video.get("nb_frames") or "").isdigit():
        frames = int(video["nb_frames"])
    else:
        dur = video.get("duration") or (data.get("format") or {}).get("duration")
        if dur:
            frames = round(float(dur) * fps)
    if not frames:
        raise SequenceError("Couldn't work out how many frames the movie has.")
    tc = (video.get("tags") or {}).get("timecode")
    if not tc:
        for s in streams:
            if s.get("codec_tag_string") == "tmcd" or s.get("codec_type") == "data":
                tc = (s.get("tags") or {}).get("timecode") or tc
    tc = tc or ((data.get("format") or {}).get("tags") or {}).get("timecode")
    return {"fps": float(fps), "fps_str": f"{fps.numerator}/{fps.denominator}", "frames": frames,
            "timecode": tc, "width": video.get("width"), "height": video.get("height"),
            "codec": video.get("codec_name"), "color_space": video.get("color_space")}


def probe_movie(path: str, ffprobe: list[str]) -> dict:
    try:
        out = subprocess.run([*ffprobe, "-v", "error", "-print_format", "json", "-show_streams",
                              "-show_format", path], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SequenceError(f"Couldn't read the movie with ffprobe: {e}")
    if out.returncode != 0:
        raise SequenceError(f"ffprobe can't read {path}: {out.stderr.strip()[-300:]}")
    return parse_probe(json.loads(out.stdout or "{}"))


# ffmpeg prints NTSC rates rounded; map them back to the exact fractions.
_NTSC = {"23.98": Fraction(24000, 1001), "29.97": Fraction(30000, 1001), "47.95": Fraction(48000, 1001),
         "59.94": Fraction(60000, 1001), "119.88": Fraction(120000, 1001)}
_COLOR_SPACES = {"bt709", "bt470bg", "smpte170m", "bt2020nc", "bt2020c", "smpte240m", "fcc", "ycgco"}


def parse_ffmpeg_info(text: str, packets: int | None) -> dict:
    """`ffmpeg -i file` stderr (+ a packet count) -> the same dict as parse_probe."""
    video = None
    for m in re.finditer(r"^\s*Stream #\d+:\d+.*?: Video: (.+)$", text, re.MULTILINE):
        if "attached pic" not in m.group(1):
            video = m.group(1)
            break
    if video is None:
        raise SequenceError("The movie has no video stream.")
    rate = re.search(r", ([\d.]+) fps", video) or re.search(r", ([\d.]+) tbr", video)
    if not rate:
        raise SequenceError("Couldn't read the movie's frame rate.")
    fps = _NTSC.get(rate.group(1)) or Fraction(rate.group(1)).limit_denominator(1001)
    size = re.search(r", (\d{2,5})x(\d{2,5})\b", video)
    color = None
    pix = re.search(r"^\w+[^,]*?, \w+\(([^)]*)\)", video)
    if pix:
        for token in re.split(r"[,/\s]+", pix.group(1)):
            if token in _COLOR_SPACES:
                color = token
                break
    tc = re.search(r"^\s*timecode\s*:\s*(\d{1,2}:\d{2}:\d{2}[:;.,]\d{2,3})\s*$", text, re.MULTILINE)
    frames = packets
    if not frames:
        dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
        if dur:
            frames = round((int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3])) * fps)
    if not frames:
        raise SequenceError("Couldn't work out how many frames the movie has.")
    return {"fps": float(fps), "fps_str": f"{fps.numerator}/{fps.denominator}", "frames": frames,
            "timecode": tc.group(1) if tc else None, "width": int(size[1]) if size else None,
            "height": int(size[2]) if size else None, "codec": video.split()[0].strip(","),
            "color_space": color}


def probe_movie_ffmpeg(path: str, ffmpeg: list[str]) -> dict:
    """Movie info without ffprobe (pip's ffmpeg doesn't include it): stream details from
    `ffmpeg -i`, and the exact frame count from a packet listing (no decoding)."""
    try:
        info = subprocess.run([*ffmpeg, "-hide_banner", "-nostdin", "-i", path],
                              capture_output=True, text=True, timeout=60)
        count = subprocess.run([*ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
                                "-map", "0:v:0", "-c", "copy", "-f", "framecrc", "-"],
                               capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SequenceError(f"Couldn't read the movie with ffmpeg: {e}")
    if "Stream #" not in info.stderr:
        raise SequenceError(f"ffmpeg can't read {path}: {info.stderr.strip()[-300:]}")
    packets = sum(1 for line in count.stdout.splitlines() if line and not line.startswith("#")) \
        if count.returncode == 0 else None
    return parse_ffmpeg_info(info.stderr, packets)


def read_movie_info(path: str, cc: ConvertConfig) -> dict:
    """ffprobe when it's installed, else ffmpeg alone."""
    ffprobe = find_tool("ffprobe", cc.tools.get("ffprobe"))
    if ffprobe:
        return probe_movie(path, ffprobe)
    return probe_movie_ffmpeg(path, require_tool(cc, "ffmpeg"))


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


# ============================================================== OCIO
# The farm's OCIO config lives in the repo (ocio/config.ocio; config.yaml `ocio.config`).
# Its two conversions are a Nuke Write and Read set to "default (color_picking)" in Rodeo's
# published show config; the names below are its roles.
REPO = Path(__file__).resolve().parent.parent
LINEAR = "scene_linear"     # what EXRs are in (ACEScg)
IMAGE = "color_picking"     # what ComfyUI's PNGs are in (ACES Rec.709 output transform)
DATA = "data"               # tagged on data maps; their values are never converted
# Linear grey -> 8-bit PNG code through LINEAR -> IMAGE, measured in Nuke 16 with bri3's
# config_1_0_v1 (ocio/README.md). `check` and the tests hold the config to these.
NUKE_RAMP = {0.02: 23, 0.05: 43, 0.18: 99, 0.5: 170, 1.0: 209}


def ocio_config_path(oc: OcioConfig) -> str:
    """ocio.config as a full path: relative paths are inside the repo. Built-in configs
    (ocio://...) are passed through."""
    if oc.config.startswith("ocio://"):
        return oc.config
    path = Path(oc.config)
    return normalize(str(path if path.is_absolute() else REPO / path))


def ocio_setup(oc: OcioConfig, pc: PathsConfig) -> dict:
    """The conversion settings a job is frozen with: the config file and colour spaces."""
    config = ocio_config_path(oc)
    if pc.validate_inputs and not config.startswith("ocio://") and not os.path.isfile(config):
        raise SequenceError(f"No OCIO config at {config} (ocio.config in config.yaml). It comes with the repo: "
                            f"git pull on the farm machine.")
    return {"mode": "colorconvert", "config": config, "from": LINEAR, "to": IMAGE, "linear": LINEAR,
            "image": IMAGE, "data": DATA, "notes": []}


# ============================================================== channels
# oiiotool's `--ch R,G,B` fills channels the image doesn't have with 0 and only warns. A
# greyscale image has one channel, which OpenImageIO calls Y (CHORD's height, roughness and
# metalness PNGs, a greyscale EXR), so it would come out black. Each image's channels are
# read first, and grey is copied into R, G and B.
_PNG_CHANNELS = {0: ["Y"], 2: ["R", "G", "B"], 3: ["R", "G", "B"], 4: ["Y", "A"], 6: ["R", "G", "B", "A"]}
UNKNOWN_CHANNEL = "Unknown channel name"   # oiiotool's warning when --ch asks for a missing channel


def _png_channels(head: bytes) -> list[str] | None:
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR" or len(head) < 26:
        return None
    return _PNG_CHANNELS.get(head[25])


def _exr_attributes(fh, wanted: set[bytes]) -> dict[bytes, bytes]:
    """Raw values of the wanted attributes in an OpenEXR file's (first) header. Reads
    64 KB (headers are a few KB), and up to 1 MB if the header is longer than that."""
    import struct
    head = fh.read(1 << 16)
    out: dict[bytes, bytes] = {}
    if head[:4] != b"\x76\x2f\x31\x01":
        return out
    pos = 8
    while len(out) < len(wanted):
        end = head.find(b"\0", pos)
        tend = head.find(b"\0", end + 1) if end > pos else -1
        if end < 0 or (end > pos and (tend < 0 or tend + 5 > len(head))):
            if len(head) >= 1 << 20:
                break
            more = fh.read((1 << 20) - len(head))   # ran off the end of what was read
            if not more:
                break
            head += more
            continue
        if end == pos:                     # end of the header
            break
        size = struct.unpack_from("<i", head, tend + 1)[0]
        value = tend + 5
        if value + size > len(head) and len(head) < 1 << 20:
            more = fh.read((1 << 20) - len(head))
            if more:
                head += more
                continue
        if head[pos:end] in wanted:
            out[head[pos:end]] = head[value:value + size]
        pos = value + size
    return out


def _exr_channels(fh) -> list[str] | None:
    """Channel names from an OpenEXR file's (first) header."""
    raw = _exr_attributes(fh, {b"channels"}).get(b"channels")
    if not raw:
        return None
    names, p = [], 0
    while p < len(raw) and raw[p] != 0:
        e = raw.find(b"\0", p)
        if e < 0:
            return None
        names.append(raw[p:e].decode("utf-8", "replace"))
        p = e + 1 + 16                     # pixel type, pLinear + 3 reserved, x/y sampling
    return names or None


def _oiio_spec(path: str):
    """OpenImageIO's ImageSpec for formats not parsed here by hand (jpg, tif, ...), or None.
    OpenImageIO's Python module is installed with oiiotool in the farm's venv."""
    try:
        import OpenImageIO as oiio
        inp = oiio.ImageInput.open(path)
        if inp:
            try:
                return inp.spec()
            finally:
                inp.close()
    except Exception:  # noqa: BLE001
        pass
    return None


def image_channels(path: str) -> list[str] | None:
    """The image's channel names as oiiotool sees them, or None when they can't be read."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head[:4] == b"\x76\x2f\x31\x01":
                fh.seek(0)
                return _exr_channels(fh)
            names = _png_channels(head)
            if names or head[:8] == b"\x89PNG\r\n\x1a\n":
                return names
    except OSError:
        return None
    spec = _oiio_spec(path)
    return list(spec.channelnames) if spec is not None else None


def image_size(path: str) -> tuple[int, int] | None:
    """(width, height) of an image's display window, or None when it can't be read."""
    import struct
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24]) if head[12:16] == b"IHDR" and len(head) >= 24 else None
            if head[:4] == b"\x76\x2f\x31\x01":
                fh.seek(0)
                box = _exr_attributes(fh, {b"displayWindow"}).get(b"displayWindow")
                if box and len(box) >= 16:
                    x0, y0, x1, y1 = struct.unpack("<iiii", box[:16])
                    return x1 - x0 + 1, y1 - y0 + 1
                return None
    except OSError:
        return None
    spec = _oiio_spec(path)
    if spec is None:
        return None
    return (spec.full_width or spec.width, spec.full_height or spec.height)


def fit_size(size: tuple[int, int], limit: int) -> tuple[int, int]:
    """size scaled so its longer side is `limit` (aspect kept), or as it is if it fits."""
    w, h = size
    if max(w, h) <= limit:
        return w, h
    return (limit, max(1, round(h * limit / w))) if w >= h else (max(1, round(w * limit / h)), limit)


def resize_args(size: tuple[int, int] | None, limit: int | None) -> list[str]:
    """oiiotool options that scale an image down to fit `limit` (the workflow's
    work_resolution), keeping its aspect: nothing if it already fits or its size is unknown."""
    if not limit or not size or max(size) <= limit:
        return []
    w, h = size
    return ["--resize", f"{limit}x0" if w >= h else f"0x{limit}"]


def rgb_channels(names: list[str] | None) -> str | None:
    """The `--ch` list that gives R, G, B from an image with these channels: as they are,
    or one grey channel copied into all three. None if the image has neither."""
    if names is None:
        return "R,G,B"                     # unknown: oiiotool's own warning is caught instead
    upper = {n.upper() for n in names}
    if {"R", "G", "B"} <= upper or {"RY", "BY"} <= upper:   # RGB, or luminance/chroma (read as RGB)
        return "R,G,B"
    grey = next((n for n in names if n.upper() == "Y"), names[0] if len(names) == 1 else None)
    return f"R={grey},G={grey},B={grey}" if grey else None


def channel_args(src: str) -> str:
    """rgb_channels() for one file, or a ConvertError naming what's in it."""
    names = image_channels(src)
    ch = rgb_channels(names)
    if ch is None:
        raise ConvertError(f"{os.path.basename(src)} has no R, G, B or single grey channel "
                           f"(it has {', '.join(names[:8])}).")
    return ch


# ============================================================== resolve
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
                  work_resolution: int | None = None, preview: bool = False) -> Sequence:
    """Any supported input -> a Sequence. Converted kinds carry `convert` details.

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
    if texture and pc.validate_inputs:
        raw = texture_input(raw, pc)
    seq = resolve_sequence(raw, pc, show=show, shot=shot, allow_gaps=texture)
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
                       **ocio_setup(ocio or OcioConfig(), pc)}
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


def point_loader_at_scratch(seq: Sequence, scratch_root: str, job_id: str) -> None:
    """Converted frames go to <scratch>/<job id>/<source>.<frame>.png; the loader reads
    exactly those, so no skip/cap. Padding keeps name order = frame order."""
    d = normalize(f"{scratch_root}/{job_id}") + "/"
    numbers = seq.convert.get("frames") if seq.kind != "movie" else None   # a movie's is its frame count
    width = max(4, len(str(max(numbers) if numbers else seq.last if seq.last is not None else 1)))
    seq.load_dir = d
    seq.skip = seq.cap = 0
    seq.convert.update(dst_dir=d, dst_pattern=f"{seq.source}.%0{width}d.png")


def _config_display(config: str, pc: PathsConfig) -> str:
    """ocio/config.ocio for the repo's own config, else the path as artists see it."""
    if config.startswith("ocio://"):
        return config
    try:
        return Path(config).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return to_posix(config, pc.mappings) or config


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


# ============================================================== run
async def _run(cmd: list[str], env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, (err or b"").decode(errors="replace") + (out or b"").decode(errors="replace")


def png_compression_args(level) -> list[str]:
    """oiiotool options for a PNG zlib level (convert.exr.png_compression). 0 = no
    compression; 1-9 go through OpenImageIO's png:compressionLevel."""
    level = {"none": 0, "pngfast": 1, "default": 6}.get(level, level) if isinstance(level, str) else level
    level = max(0, min(9, int(level or 0)))
    if level == 0:
        return ["--compression", "none"]
    return ["--compression", "default", "--attrib:type=int", "png:compressionLevel", str(level)]


def exr_command(oiiotool: list[str], c: dict, src: str, dst: str, ch: str | None = None,
                resize: list[str] | None = None) -> list[str]:
    """The oiiotool call for one EXR frame: a Nuke Write set to "default (color_picking)"
    (also run by `comfyfarm check` and tools/ocio_compare.py). `ch` replaces the `--ch`
    list in convert.exr.extra_args, e.g. R=Y,G=Y,B=Y for a greyscale EXR. `resize`
    (resize_args(), for work_resolution) scales it in scene_linear, before the colour
    conversion, so that runs on the smaller image."""
    extra = list(c["extra_args"])
    if ch and "--ch" in extra[:-1]:
        extra[extra.index("--ch") + 1] = ch
    extra += resize or []
    out = ["-d", "uint8", *png_compression_args(c.get("png_compression", 0)), "-o", dst]
    if "display" in c:   # a job queued before the switch to the repo config, frozen with its own settings
        return [*oiiotool, "--colorconfig", c["config"], src, *extra,
                f"--ociodisplay:from={c['input_colorspace']}", c["display"], c["view"], *out]
    return [*oiiotool, "--colorconfig", c["config"], src, *extra,
            "--colorconvert", c["from"], c["to"], *out]


def image_command(oiiotool: list[str], c: dict, src: str, dst: str, resize: list[str]) -> list[str]:
    """The oiiotool call that scales one png/jpg/tif input down to work_resolution. No
    colour conversion: the pixels stay as the loader would have read them. Alpha isn't
    premultiplied on the way through (colour under transparent pixels is kept), the
    orientation a jpg's EXIF asks for is applied (the loader does that too), and the
    result is 8-bit, which is what the loader would have made of it."""
    return [*oiiotool, "--iconfig", "oiio:UnassociatedAlpha", "1", src, "--reorient", *resize,
            "-d", "uint8", *png_compression_args(c.get("png_compression", 0)), "-o", dst]


async def _convert_frames(c: dict, cc: ConvertConfig, progress, by_frame: dict[int, str],
                          frames: list[int], command, env: dict | None = None) -> None:
    """Run oiiotool on every frame, frame_workers at a time. `command(src, dst, resize)`
    builds the call; `resize` is the frame's resize_args() for work_resolution."""
    sem = asyncio.Semaphore(max(1, cc.frame_workers))
    limit = c.get("work_resolution")
    done = 0

    async def one(frame: int) -> None:
        nonlocal done
        src = c["src_dir"] + by_frame[frame]
        dst = c["dst_dir"] + (c["dst_pattern"] % frame)
        async with sem:
            resize = resize_args(await asyncio.to_thread(image_size, src), limit) if limit else []
            code, out = await _run(command(src, dst, resize), env)
        if code != 0 or not os.path.isfile(dst):
            raise ConvertError(f"oiiotool failed on {by_frame[frame]}: {out.strip()[-600:] or f'exit {code}'}")
        if UNKNOWN_CHANNEL in out:
            raise ConvertError(f"{by_frame[frame]} is missing channels, so it would convert black: "
                               f"{out.strip()[-300:]}")
        done += 1
        progress(done, len(frames))

    tasks = [asyncio.create_task(one(f)) for f in frames]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _listdir(d: str) -> list[str]:
    try:
        return os.listdir(d)
    except OSError as e:
        raise ConvertError(f"Can't open {d}: {e}")


async def convert_exr(c: dict, cc: ConvertConfig, progress) -> None:
    oiiotool = require_tool(cc, "oiiotool")
    names = _listdir(c["src_dir"])
    rx = _pattern_regex(c["src_pattern"])
    by_frame = {int(m.group(1)): n for n, m in ((n, rx.match(n)) for n in names) if m} if rx \
        else {c["first"]: c["src_pattern"]}
    frames = list(c.get("frames") or range(c["first"], c["last"] + 1))   # "frames": UDIM tiles, gaps allowed
    missing = [f for f in frames if f not in by_frame]
    if missing:
        raise ConvertError(f"Frames disappeared since submitting: {missing[:10]}")
    env = dict(os.environ, **c.get("context", {}))   # jobs frozen before ocio.context went
    ch = channel_args(c["src_dir"] + by_frame[frames[0]])   # a sequence's frames share their channels
    await _convert_frames(c, cc, progress, by_frame, frames,
                          lambda src, dst, resize: exr_command(oiiotool, c, src, dst, ch, resize), env)


async def convert_image(c: dict, cc: ConvertConfig, progress) -> None:
    """png/jpg/tif inputs larger than the workflow's work_resolution, scaled down."""
    oiiotool = require_tool(cc, "oiiotool")
    there = set(_listdir(c["src_dir"]))
    gone = [n for n in c["names"] if n not in there]
    if gone:
        raise ConvertError(f"Files disappeared since submitting: {', '.join(gone[:10])}")
    by_frame = dict(zip(c["frames"], c["names"]))
    await _convert_frames(c, cc, progress, by_frame, list(c["frames"]),
                          lambda src, dst, resize: image_command(oiiotool, c, src, dst, resize))


async def convert_movie(c: dict, cc: ConvertConfig, first: int, count: int, progress) -> None:
    ffmpeg = require_tool(cc, "ffmpeg")
    vf = f"select='between(n,{c['index_first']},{c['index_last']})'"
    if not c.get("color_space") or c["color_space"] in ("unknown", "reserved"):
        vf += ",scale=in_color_matrix=bt709"   # untagged HD: ffmpeg would otherwise assume BT.601
    cmd = [*ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", c["src"], "-vf", vf,
           "-fps_mode", "passthrough", "-start_number", str(first), "-pix_fmt", "rgb24",
           "-progress", "pipe:1", c["dst_dir"] + c["dst_pattern"]]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        async for line in proc.stdout:
            m = re.match(rb"frame=(\d+)", line)
            if m:
                progress(min(int(m[1]), count), count)
        err = (await proc.stderr.read()).decode(errors="replace")
        code = await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    written = len([n for n in os.listdir(c["dst_dir"]) if n.endswith(".png")])
    if code != 0 or written != count:
        raise ConvertError(f"ffmpeg wrote {written} of {count} frames: {err.strip()[-600:] or f'exit {code}'}")


async def convert(seq_info: dict, cc: ConvertConfig, progress) -> None:
    """Run the conversion described in a job's info['input']['convert']."""
    c = seq_info["convert"]
    shutil.rmtree(c["dst_dir"], ignore_errors=True)
    os.makedirs(c["dst_dir"], exist_ok=True)
    if c["kind"] == "exr":
        await convert_exr(c, cc, progress)
    elif c["kind"] == "image":
        await convert_image(c, cc, progress)
    elif c["kind"] == "movie":
        await convert_movie(c, cc, seq_info["first"], seq_info["frame_count"], progress)
    else:
        raise ConvertError(f"Unknown conversion {c['kind']}")


__all__ = ["MOVIE_EXTENSIONS", "ConvertError", "find_tool", "timecode_to_frame", "parse_probe",
           "parse_ffmpeg_info", "probe_movie_ffmpeg", "read_movie_info",
           "resolve_input", "point_loader_at_scratch", "summary", "convert"]
