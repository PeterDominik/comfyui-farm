"""The preview movie the agent makes from a job's frames: an H.264 MP4 next to them,
named by paths.preview_name, that the dashboard plays.

It's made while the outputs are collected, after the frames are renamed, so the GPU is
already free for the next job. The frames go in as they are (PNG is display-referred:
no colour conversion), scaled down to preview.max_width when they're wider, at the
job's frame rate (the input movie's, else preview.fps). The movie's timecode is its first
frame's number, so frame 1001 is 00:00:41:17 at 24 fps, and a preview read back as an input
is numbered like the frames. No burn-ins.

The settings are frozen into the job at submit (info.collect.movie: fps, max_width, crf),
so every agent makes the same movie. A preview that can't be made is a warning on the
job, never a failure: the frames are what counts."""

from __future__ import annotations

import os
import re
import uuid
from fractions import Fraction
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import ConvertConfig

from . import convert as C
from .errors import ConvertError
from .media import frame_to_timecode, image_size
from .tools import require_tool

__all__ = ["PRESET", "rate_text", "movie_size", "movie_command", "why", "make_movie"]

PRESET = "veryfast"     # x264: a few seconds for a shot's frames; slower presets barely shrink it


def rate_text(fps: float) -> str:
    """A frame rate as ffmpeg takes it exactly: 24 -> '24/1', 23.976 -> '24000/1001'."""
    for nominal in (24, 30, 48, 60, 120):
        if abs(fps - nominal * 1000 / 1001) < 0.005:
            return f"{nominal * 1000}/1001"
    r = Fraction(fps).limit_denominator(1001)
    return f"{r.numerator}/{r.denominator}"


def movie_size(size: tuple[int, int] | None, max_width: int | None) -> tuple[int, int] | None:
    """The movie's size: the frames' own, scaled down to max_width (aspect kept), both
    sides even (H.264 in 4:2:0 needs it). None when the frames' size isn't known."""
    if not size:
        return None
    w, h = size
    if max_width and w > max_width:
        w, h = max_width, max(2, round(h * max_width / w))
    return max(2, w - w % 2), max(2, h - h % 2)


def movie_command(ffmpeg: list[str], pattern: str, first: int, count: int, settings: dict,
                  size: tuple[int, int] | None, dst: str) -> list[str]:
    """ffmpeg for frames `pattern` (printf-style, %04d), first..first+count-1 -> dst (.mp4).
    settings: the job's info.collect.movie, {fps, fps_str, max_width, crf}."""
    fps = float(settings.get("fps") or 24)
    scale = (f"scale={size[0]}:{size[1]}:flags=lanczos" if size
             else r"scale=trunc(iw/2)*2:trunc(ih/2)*2")      # unknown size: just even
    # PNG is RGB: without out_color_matrix ffmpeg would write BT.601 numbers into a movie
    # tagged (and played) as BT.709, shifting the colours slightly.
    vf = f"{scale}:out_color_matrix=bt709:out_range=tv,format=yuv420p"
    cmd = [*ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
           "-framerate", settings.get("fps_str") or rate_text(fps), "-start_number", str(first), "-i", pattern,
           "-frames:v", str(count), "-vf", vf,
           "-c:v", "libx264", "-preset", PRESET, "-crf", str(settings.get("crf", 18)), "-pix_fmt", "yuv420p",
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    tc = frame_to_timecode(first, fps)
    if tc:
        cmd += ["-timecode", tc]
    return cmd + ["-movflags", "+faststart", "-f", "mp4", dst]


def why(out: str, limit: int = 300) -> str:
    """ffmpeg's errors, readable: each message once, without the [png @ 0x...] prefixes."""
    seen: list[str] = []
    for line in out.splitlines():
        line = re.sub(r"^(\[[^\]]*@ 0x[0-9a-f]+\]\s*)+", "", line.strip())
        if line and line not in seen and not line.startswith("Last message repeated"):
            seen.append(line)
    text = ""
    for line in seen:
        if text and len(text) + len(line) + 3 > limit:
            return text + " …"
        text = f"{text} / {line}" if text else line[:limit]
    return text


async def make_movie(frames: dict, settings: dict, dst: str, cc: ConvertConfig) -> str:
    """Write the preview movie of the renamed frames (rename_frames' result: frame_pattern,
    first, frames) to dst, whole or not at all. ConvertError says why it couldn't."""
    ffmpeg = require_tool(cc, "ffmpeg")
    pattern, first, count = frames["frame_pattern"], frames["first"], frames["frames"]
    try:
        size = movie_size(image_size(pattern % first), settings.get("max_width"))
    except (TypeError, ValueError):     # a % of its own in the path: ffmpeg finds the size
        size = None
    tmp = f"{dst[:-4]}.{uuid.uuid4().hex[:8]}.part.mp4"
    code, out = await C._run(movie_command(ffmpeg, pattern, first, count, settings, size, tmp))
    if code != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise ConvertError(f"ffmpeg couldn't make it: {why(out) or f'exit {code}'}")
    os.replace(tmp, dst)
    return dst
