"""What's in an image or a movie, read from its header: image size and channels (PNG and
EXR parsed here; jpg, tif, ... through OpenImageIO when it's installed), and a movie's
frame rate, frame count and timecode (ffprobe, or ffmpeg alone)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import ConvertConfig

from .errors import ConvertError, SequenceError
from . import tools as TL   # by module, so tests can patch find_tool in one place

__all__ = ["timecode_to_frame", "frame_to_timecode", "parse_probe", "probe_movie", "parse_ffmpeg_info", "probe_movie_ffmpeg",
           "read_movie_info", "image_channels", "image_size", "fit_size", "rgb_channels", "channel_args",
           "UNKNOWN_CHANNEL"]


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


def frame_to_timecode(frame: int, fps: float) -> str | None:
    """1001 at 24 fps -> '00:00:41:17' (timecode_to_frame backwards; drop-frame, with ';',
    at 29.97/59.94). None for a negative frame."""
    if frame < 0 or fps <= 0:
        return None
    nominal = round(fps)
    sep = ":"
    if nominal in (30, 60) and abs(fps - nominal) > 0.001:
        drop = 2 * nominal // 30
        per_10 = nominal * 600 - 9 * drop          # frames in ten minutes (the tenth keeps its numbers)
        per_1 = nominal * 60 - drop                 # in the other minutes
        tens, rest = divmod(frame, per_10)
        frame += 9 * drop * tens + (drop * ((rest - drop) // per_1) if rest > drop else 0)
        sep = ";"
    f = frame % nominal
    s = frame // nominal
    return f"{s // 3600 % 24:02d}:{s // 60 % 60:02d}:{s % 60:02d}{sep}{f:02d}"


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
    ffprobe = TL.find_tool("ffprobe", cc.tools.get("ffprobe"))
    if ffprobe:
        return probe_movie(path, ffprobe)
    return probe_movie_ffmpeg(path, TL.require_tool(cc, "ffmpeg"))

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
        if not inp:
            oiio.geterror()             # read it, or OpenImageIO prints it when Python exits
            return None
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
