"""Turning what ComfyUI wrote into the farm's outputs: frames renamed to the shot's frame
numbers, and texture maps written as named EXRs (colour maps color_picking -> scene_linear
with the repo's OCIO config, data maps kept)."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import ConvertConfig, PathMapping, TexturesConfig

from . import convert as C
from . import media as M
from . import paths as P
from .errors import ConvertError
from .textures import file_name, map_outputs
from .tools import require_tool


def readable(path: str) -> None:
    """Read permission for everyone (and listing, for a folder), on top of what it has: the
    farm's outputs are for the whole studio, whatever umask ComfyUI or the agent run with.
    Nothing on Windows, where the share's permissions decide."""
    if os.name == "nt":
        return
    try:
        mode = os.stat(path).st_mode
        add = 0o555 if stat.S_ISDIR(mode) else 0o444
        if mode & add != add:
            os.chmod(path, stat.S_IMODE(mode) | add)
    except OSError:
        pass                            # not ours to change: leave it as it is


def makedirs_readable(path: str) -> None:
    """os.makedirs(path, exist_ok=True), with every folder it creates readable by everyone."""
    created, p = [], os.path.abspath(path)
    while not os.path.isdir(p):
        created.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    os.makedirs(path, exist_ok=True)
    for d in reversed(created):
        readable(d)


def map_command(oiiotool: list[str], ocio: dict, m: dict, src: str, dst: str, ch: str = "R,G,B") -> list[str]:
    """oiiotool for one map image -> its final EXR (also run by `comfyfarm check`).
    `ocio` is the job's frozen convert.ocio_setup(): colour maps go image -> linear (a Nuke
    Read set to "default (color_picking)"); data maps are only tagged. `ch` comes from
    convert.channel_args(): greyscale maps (height, roughness, metalness) are copied into
    R, G and B (R=Y,G=Y,B=Y) rather than asked for R, G, B they don't have."""
    cmd = [*oiiotool, "--colorconfig", ocio["config"], src, "--ch", ch]
    cmd += ["--colorconvert", ocio["image"], ocio["linear"]] if m["color"] else ["--iscolorspace", ocio["data"]]
    return cmd + ["-d", m["bits"], "--compression", "zip", "-o", dst]


def _raw_files(names: list[str], prefix: str, ext: str) -> list[str]:
    """A save node's images, in the order it wrote them (prefix_0001.png, ...)."""
    rx = re.compile(re.escape(prefix) + r"(?:\D?(\d+))?\." + re.escape(ext) + "$", re.IGNORECASE)
    return [n for _, n in sorted((int(m.group(1) or 0), n) for n, m in ((n, rx.match(n)) for n in names) if m)]


async def write_maps(t: dict, out_dir: str, tc: TexturesConfig, cc: ConvertConfig,
                     mappings: list[PathMapping], progress) -> dict:
    """Convert every map image ComfyUI wrote into its named EXR in out_dir.

    t is the job's info['textures'] (frozen at submit). Each save node wrote one image
    per input texture, in input order, so the n-th image is the n-th UDIM/frame."""
    oiiotool = require_tool(cc, "oiiotool")
    raw = t["raw_dir"].rstrip("/")
    try:
        names = os.listdir(raw)
    except OSError:
        names = []
    items = [tuple(i) for i in t["items"]]
    work = []
    for m in t["maps"]:
        files = _raw_files(names, m["prefix"], t["ext"])
        if len(files) != len(items):
            raise ConvertError(f"The {m['name']} map ({m['channel']}) came back as {len(files)} image(s) for "
                                 f"{len(items)} input texture(s), so they can't be named reliably.")
        for f, (udim, frame) in zip(files, items):
            work.append((m, f"{raw}/{f}", f"{out_dir}/{file_name(t['name'], m, t['tokens'], udim, frame)}"))

    sem = asyncio.Semaphore(max(1, tc.workers))
    env = dict(os.environ, **t["ocio"].get("context", {}))
    done = 0

    async def one(m: dict, src: str, dst: str) -> None:
        nonlocal done
        # oiiotool picks the format from the extension, so the temporary name ends in .exr too.
        tmp = f"{dst[:-4]}.{uuid.uuid4().hex[:8]}.part.exr"
        ch = M.channel_args(src)
        async with sem:
            code, out = await C._run(map_command(oiiotool, t["ocio"], m, src, tmp, ch), env)
        if code == 0 and M.UNKNOWN_CHANNEL in out:
            code, out = 1, f"{os.path.basename(src)} is missing channels, so the map would be black: {out}"
        if code != 0 or not os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise ConvertError(f"oiiotool failed writing {os.path.basename(dst)}: "
                                 f"{out.strip()[-600:] or f'exit {code}'}")
        await asyncio.to_thread(os.replace, tmp, dst)
        readable(dst)
        done += 1
        progress(done, len(work))

    tasks = [asyncio.create_task(one(*w)) for w in work]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return map_outputs(t, out_dir, mappings, items)


def rename_frames(info: dict, ext: str, frame_name: str, mappings: list[PathMapping]) -> dict:
    """Rename the frames a save node wrote (<frames_prefix>_00001.png, ...) to the job's
    frame names, numbered from its first input frame. `info` is the job's frozen info;
    `frame_name` is paths.frame_name, for jobs queued before it was frozen per job."""
    out_dir = info["output_dir"]
    prefix = info["frames_prefix"]
    names = sorted(n for n in os.listdir(out_dir)
                   if n.startswith(prefix) and n.lower().endswith("." + ext.lower()))
    if not names:
        raise RuntimeError(f"no frames were written to {out_dir}")
    first = info.get("first_frame")
    first = 1 if first is None else first
    template = info.get("frame_name") or frame_name
    tokens = dict(info.get("name_tokens") or {}, ext=ext)
    for i, n in enumerate(names):
        dst = os.path.join(out_dir, template.format(frame=first + i, **tokens))
        os.replace(os.path.join(out_dir, n), dst)
        readable(dst)
    pattern = f"{out_dir}/{P.frame_pattern(template).format(**tokens)}"
    return {"frames": len(names), "first": first, "last": first + len(names) - 1,
            "frame_pattern": pattern, "frame_pattern_posix": P.to_posix(pattern, mappings)}


__all__ = ["readable", "makedirs_readable", "map_command", "write_maps", "rename_frames"]
