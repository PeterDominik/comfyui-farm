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
import os
import re
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import ConvertConfig

from .errors import ConvertError
from .media import UNKNOWN_CHANNEL, channel_args, image_size
from .paths import _pattern_regex
from .tools import require_tool


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


def resize_args(size: tuple[int, int] | None, limit: int | None) -> list[str]:
    """oiiotool options that scale an image down to fit `limit` (the workflow's
    work_resolution), keeping its aspect: nothing if it already fits or its size is unknown."""
    if not limit or not size or max(size) <= limit:
        return []
    w, h = size
    return ["--resize", f"{limit}x0" if w >= h else f"0x{limit}"]


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


__all__ = ["ConvertError", "png_compression_args", "resize_args", "exr_command", "image_command",
           "convert_exr", "convert_image", "convert_movie", "convert"]
