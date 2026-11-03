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

A texture with more tiles than the manifest's `textures.batch` (CHORD: as many as fit in
the GPU's memory at once) is a set: one output folder, filled by batches of tiles that
each run through the workflow on their own (split_batches()).

The manifest's `textures` section lists the save node of each map and its channel.
ComfyUI writes PNGs into the job's scratch folder; once the job is done,
fileio.outputs.write_maps() converts each one with oiiotool straight to its final name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import naming as N
from . import paths as P
from .errors import SequenceError
from .inputs import texture_numbers

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import PathMapping

__all__ = ["TextureSpec", "MapSpec", "items_for", "resolve_tile", "name_tokens", "file_name", "name_pattern",
           "split_batches", "batch_label", "output_dir", "describe", "map_outputs"]


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
    batch: int | None = None             # most tiles per run; more make a set of batches

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
                   ext=str(d.get("ext", "png")).lstrip("."), batch=d.get("batch"))

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
        b = self.batch
        if b is not None and (isinstance(b, bool) or not isinstance(b, int) or b < 1):
            problems.append(f"textures.batch: {b!r} isn't a number of tiles (1 or more); leave it out to run "
                            f"every tile of a texture at once")
        return problems


def items_for(names: list[str], numbering: str) -> tuple[list[tuple[int, int]], bool]:
    """(UDIM, frame) for each input texture, and whether the names carried UDIMs.
    Refuses inputs where two images would get the same output name."""
    items, has_udim, by_key = [], False, {}
    for n in names:
        udim, frame, had = texture_numbers(n, numbering)
        key = (udim, frame)
        if key in by_key:
            hint = (" If these are animation frames rather than UDIM tiles, set Numbering to frame."
                    if numbering == "udim" else "")
            raise SequenceError(f"{by_key[key]} and {n} would both be written as UDIM {udim}, frame {frame:04d}."
                                  + hint)
        by_key[key] = n
        items.append(key)
        has_udim |= had
    if not items:
        raise SequenceError("No input textures.")
    return items, has_udim


def resolve_tile(choice: str, items: list[tuple[int, int]], has_udim: bool) -> str:
    """auto: a UDIM set is noTile, anything else a tileable 1001 texture."""
    udims = sorted({u for u, _ in items})
    if choice == "tile" and udims != [1001]:
        raise SequenceError(f"A tile texture is one 1001 tile, but this input has UDIMs "
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


def split_batches(sizes: list, batch: int | None) -> list[tuple[int, int]]:
    """[start, end) ranges of tiles, in order, each run through the workflow on its own:
    at most `batch` tiles, all the same size. The loader resizes and crops every image to
    the most common size of its batch, so tiles of another size would come back cropped.
    `sizes` has one entry per tile (None when unknown: those go together)."""
    out, start = [], 0
    for i in range(1, len(sizes) + 1):
        if i == len(sizes) or sizes[i] != sizes[start] or (batch and i - start >= batch):
            out.append((start, i))
            start = i
    return out


def batch_label(items: list, has_udim: bool) -> str:
    """'UDIMs 1001–1004', 'UDIM 1011', 'frames 1–8': which tiles a batch is."""
    v = varying(items)
    if v == "frame" or (v is None and not has_udim):
        nums, word = [i[1] for i in items], "frame"
    else:
        nums, word = [i[0] for i in items], "UDIM"
    if len(nums) == 1:
        return f"{word} {nums[0]}"
    return f"{word}s {nums[0]}–{nums[-1]}" + ("" if len(nums) == nums[-1] - nums[0] + 1 else f" ({len(nums)})")


def output_dir(dirs: list[str], **tokens) -> str:
    """The first folder template whose tokens are all known."""
    for tpl in dirs:
        try:
            return N.output_dir(tpl, **tokens)
        except SequenceError:
            continue
    raise SequenceError("Can't build the output folder from the input path. Give an Output folder instead.")


def describe(n_maps: int, items: list, has_udim: bool, numbering: str) -> str:
    """'15 EXRs (5 maps × 3 UDIM tiles)' for the destination preview."""
    n = len(items)
    unit = "UDIM tile" if has_udim else ("frame" if numbering == "frame" and n > 1 else "texture")
    return f"{n_maps * n} EXR{'s' if n_maps * n != 1 else ''} ({n_maps} maps × {n} {unit}{'s' if n != 1 else ''})"


def map_outputs(t: dict, out_dir: str, mappings: list[PathMapping], items: list) -> dict:
    """A job's outputs: each map's files as a pattern with its range (what a Nuke Read
    takes). For a set, `items` are the tiles of every batch that finished."""
    items = sorted(tuple(i) for i in items)
    maps_out = {}
    for m in t["maps"]:
        pat, first, last = name_pattern(t["name"], m, t["tokens"], items)
        full = f"{out_dir}/{pat}"
        maps_out[m["channel"]] = {"name": m["name"], "pattern": full, "pattern_posix": P.to_posix(full, mappings),
                                  "first": first, "last": last, "files": len(items)}
    main = maps_out[next((m for m in t["maps"] if m["color"]), t["maps"][0])["channel"]]
    return {"maps": maps_out, "files": len(items) * len(t["maps"]), "frames": len(items), "first": main["first"],
            "last": main["last"], "frame_pattern": main["pattern"], "frame_pattern_posix": main["pattern_posix"]}
