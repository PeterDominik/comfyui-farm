"""Texture workflows: one diffuse texture in, a set of PBR maps out, written as EXRs
named by the texture department's [TEX] naming convention:

    <colorspace>_<uvset>_<tile>_<state>_<channel>[-<description>].<UDIM>.<frame>.exr
    acescg_map1_noTile_defState_color01.1001.0000.exr

What the convention asks for, and how the gateway does it:

- EXR, linear, 16-bit half; 32-bit float for bump and displacement maps (`bits`).
- Colour maps (color01, ...) come out of ComfyUI as color_picking PNGs, so they're
  converted to linear, like a Nuke Read set to "default (color_picking)" (config.yaml
  `ocio:`, the repo's OCIO config). Data maps (normal, bump, roughness, metalness) keep
  their values.
- bmp01, nrm01, dsp01 and cav01 belong to maps published by modeling, so generated maps
  start at 02 (nrm02, bmp02). The manifest check refuses the reserved names.
- A tileable texture is one 1001 tile ("tile"); a UDIM set is "noTile".
- The frame is 4 digits, 0000 for a texture that isn't animated.
- There's no version in the names, so each run goes into its own versioned folder.

The manifest's `textures` section lists the save node of each map and its channel.
ComfyUI writes PNGs into the job's scratch folder; once the job is done, write_maps()
converts each one with oiiotool straight to its final name.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from dataclasses import dataclass, field

from . import convert as C
from . import paths as P
from .config import ConvertConfig, PathMapping, TexturesConfig

NAME_TOKENS = {"colorspace", "uvset", "tile", "state", "channel", "description", "udim", "frame"}
DIR_TOKENS = {"root", "show", "seq", "shot", "asset", "source", "tag", "version"}
DEFAULT_NAME = "{colorspace}_{uvset}_{tile}_{state}_{channel}{description}.{udim:04d}.{frame:04d}.exr"
# Where a job's maps go: the first of these whose tokens the input path gives.
DEFAULT_DIRS = [
    "{root}/{shot}/{source}.{tag}_v{version:03d}",
    "{root}/{asset}/{source}.{tag}_v{version:03d}",
    "{root}/{source}.{tag}_v{version:03d}",
]
# Params the gateway reads by name for the file names (manifest params with naming: true).
NAMING_PARAMS = {"uvset", "tile", "state", "description", "numbering"}
DEFAULTS = {"uvset": "map1", "tile": "auto", "state": "defState", "description": "", "numbering": "udim"}
CHANNEL_RE = re.compile(r"^[a-z]+\d{2}$")
RESERVED_CHANNELS = {"bmp01", "nrm01", "dsp01", "cav01"}   # modeling's published maps


@dataclass
class MapSpec:
    name: str                  # what the map is (basecolor, normal, ...), for messages
    node: str                  # its save node in the API JSON
    channel: str               # TEX channel: color01, nrm02, bmp02, spcr01, metal01, ...
    cls: str | None = None     # class guard, like every manifest target
    colorspace: str = "acescg"  # the colorspace token in the file name
    color: bool = False        # colour: converted from color_picking to scene_linear
    bits: str = "half"         # half | float


@dataclass
class TextureSpec:
    maps: list[MapSpec]
    name: str = DEFAULT_NAME
    dirs: list[str] = field(default_factory=lambda: list(DEFAULT_DIRS))
    path_input: str = "output_path"      # save node input that gets the scratch folder
    prefix_input: str = "filename_prefix"
    ext: str = "png"                     # what the save nodes write

    @classmethod
    def from_manifest(cls, d: dict) -> "TextureSpec":
        maps = [MapSpec(name=m.get("name") or m["channel"], node=str(m["node"]), channel=m["channel"],
                        cls=m.get("class"), colorspace=m.get("colorspace", "acescg"),
                        color=bool(m.get("color", False)), bits=m.get("bits", "half"))
                for m in d.get("maps") or []]
        dirs = d.get("dir") or DEFAULT_DIRS
        return cls(maps=maps, name=d.get("name") or DEFAULT_NAME,
                   dirs=[dirs] if isinstance(dirs, str) else list(dirs),
                   path_input=d.get("path_input", "output_path"), prefix_input=d.get("prefix_input", "filename_prefix"),
                   ext=str(d.get("ext", "png")).lstrip("."))

    def check(self, naming_params: set[str]) -> list[str]:
        problems = []
        if not self.maps:
            problems.append("textures: no maps")
        fields = P.template_fields(self.name)
        if fields - NAME_TOKENS:
            problems.append(f"textures.name: unknown token(s) {', '.join(sorted(fields - NAME_TOKENS))}")
        for need in ("channel", "udim", "frame"):
            if need not in fields:
                problems.append(f"textures.name needs {{{need}}}, or maps would overwrite each other")
        if not self.name.lower().endswith(".exr"):
            problems.append("textures.name must end in .exr (TEX convention)")
        seen = set()
        for m in self.maps:
            where = f"textures map {m.channel}"
            if not CHANNEL_RE.match(m.channel):
                problems.append(f"{where}: channel should be a TEX channel with 2 digits, e.g. color01 or nrm02")
            if m.channel in RESERVED_CHANNELS:
                problems.append(f"{where}: {m.channel} is reserved for maps published by modeling; "
                                f"generated maps start at 02 ({m.channel[:-2]}02)")
            if m.channel in seen:
                problems.append(f"{where}: channel used twice")
            seen.add(m.channel)
            if m.bits not in ("half", "float"):
                problems.append(f"{where}: bits must be half or float")
        for d in self.dirs:
            f = P.template_fields(d)
            if f - DIR_TOKENS:
                problems.append(f"textures.dir '{d}': unknown token(s) {', '.join(sorted(f - DIR_TOKENS))}")
            if "version" not in f:
                problems.append(f"textures.dir '{d}' needs {{version}}: file names carry no version")
        for p in sorted(naming_params - NAMING_PARAMS):
            problems.append(f"param {p}: naming params can only be {', '.join(sorted(NAMING_PARAMS))}")
        return problems


# ============================================================== names
def items_for(names: list[str], numbering: str) -> tuple[list[tuple[int, int]], bool]:
    """(UDIM, frame) for each input texture, and whether the names carried UDIMs.
    Refuses inputs where two images would get the same output name."""
    items, has_udim, by_key = [], False, {}
    for n in names:
        udim, frame, had = P.texture_numbers(n, numbering)
        key = (udim, frame)
        if key in by_key:
            hint = (" If these are animation frames rather than UDIM tiles, set Numbering to frame."
                    if numbering == "udim" else "")
            raise P.SequenceError(f"{by_key[key]} and {n} would both be written as UDIM {udim}, frame {frame:04d}."
                                  + hint)
        by_key[key] = n
        items.append(key)
        has_udim |= had
    if not items:
        raise P.SequenceError("No input textures.")
    return items, has_udim


def resolve_tile(choice: str, items: list[tuple[int, int]], has_udim: bool) -> str:
    """auto: a UDIM set is noTile, anything else a tileable 1001 texture."""
    udims = sorted({u for u, _ in items})
    if choice == "tile" and udims != [1001]:
        raise P.SequenceError(f"A tile texture is one 1001 tile, but this input has UDIMs "
                              f"{', '.join(map(str, udims[:8]))}{'…' if len(udims) > 8 else ''}. "
                              f"Use noTile (or auto).")
    if choice in ("tile", "noTile"):
        return choice
    return "noTile" if has_udim else "tile"


def name_tokens(values: dict, tile: str) -> dict:
    get = lambda k: values.get(k) or DEFAULTS[k]  # noqa: E731
    desc = values.get("description") or ""
    return {"uvset": get("uvset"), "tile": tile, "state": get("state"), "description": f"-{desc}" if desc else ""}


def file_name(template: str, m: dict, tokens: dict, udim: int, frame: int) -> str:
    return template.format(colorspace=m["colorspace"], channel=m["channel"], udim=udim, frame=frame, **tokens)


def varying(items: list[tuple[int, int]]) -> str | None:
    """Which number changes across the files: 'udim', 'frame', 'both' or None (one file)."""
    u, f = len({i[0] for i in items}) > 1, len({i[1] for i in items}) > 1
    return "both" if u and f else "udim" if u else "frame" if f else None


def name_pattern(template: str, m: dict, tokens: dict, items: list) -> tuple[str, int | None, int | None]:
    """One map's files as a %04d pattern over whichever number changes, with its range
    (what a Nuke Read takes). A single file is just its name."""
    v = varying(items)
    if v in ("udim", "frame"):
        pat = re.sub(r"\{" + v + r"(?::0?(\d+)d)?\}", lambda x: "%0" + (x.group(1) or "4") + "d", template)
        nums = [i[0] if v == "udim" else i[1] for i in items]
        other = {"frame": items[0][1]} if v == "udim" else {"udim": items[0][0]}
        return pat.format(colorspace=m["colorspace"], channel=m["channel"], **other, **tokens), min(nums), max(nums)
    return file_name(template, m, tokens, *items[0]), None, None


def output_dir(dirs: list[str], **tokens) -> str:
    """The first folder template whose tokens are all known."""
    for tpl in dirs:
        try:
            return P.output_dir(tpl, **tokens)
        except P.SequenceError:
            continue
    raise P.SequenceError("Can't build the output folder from the input path. Give an Output folder instead.")


def describe(n_maps: int, items: list, has_udim: bool, numbering: str) -> str:
    """'15 EXRs (5 maps × 3 UDIM tiles)' for the destination preview."""
    n = len(items)
    unit = "UDIM tile" if has_udim else ("frame" if numbering == "frame" and n > 1 else "texture")
    return f"{n_maps * n} EXR{'s' if n_maps * n != 1 else ''} ({n_maps} maps × {n} {unit}{'s' if n != 1 else ''})"


# ============================================================== EXR writing
def map_command(oiiotool: list[str], ocio: dict, m: dict, src: str, dst: str) -> list[str]:
    """oiiotool for one map image -> its final EXR (also run by `comfyfarm check`).
    `ocio` is the job's frozen convert.ocio_setup(): colour maps go image -> linear (a Nuke
    Read set to "default (color_picking)"); data maps are only tagged."""
    cmd = [*oiiotool, "--colorconfig", ocio["config"], src, "--ch", "R,G,B"]
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
    oiiotool = C.require_tool(cc, "oiiotool")
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
            raise C.ConvertError(f"The {m['name']} map ({m['channel']}) came back as {len(files)} image(s) for "
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
        async with sem:
            code, out = await C._run(map_command(oiiotool, t["ocio"], m, src, tmp), env)
        if code != 0 or not os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise C.ConvertError(f"oiiotool failed writing {os.path.basename(dst)}: "
                                 f"{out.strip()[-600:] or f'exit {code}'}")
        await asyncio.to_thread(os.replace, tmp, dst)
        done += 1
        progress(done, len(work))

    tasks = [asyncio.create_task(one(*w)) for w in work]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    maps_out = {}
    for m in t["maps"]:
        pat, first, last = name_pattern(t["name"], m, t["tokens"], items)
        full = f"{out_dir}/{pat}"
        maps_out[m["channel"]] = {"name": m["name"], "pattern": full, "pattern_posix": P.to_posix(full, mappings),
                                  "first": first, "last": last, "files": len(items)}
    main = maps_out[next((m for m in t["maps"] if m["color"]), t["maps"][0])["channel"]]
    return {"maps": maps_out, "files": len(work), "frames": len(items), "first": main["first"],
            "last": main["last"], "frame_pattern": main["pattern"], "frame_pattern_posix": main["pattern_posix"]}


__all__ = ["TextureSpec", "MapSpec", "items_for", "resolve_tile", "name_tokens", "file_name", "name_pattern",
           "output_dir", "describe", "map_command", "write_maps"]
