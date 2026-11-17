"""Workflow templates (ComfyUI API JSON) plus manifests describing what is exposed."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import loaders as L
from .fileio import textures as T
from .fileio.inputs import Sequence

log = logging.getLogger(__name__)

# Used to estimate progress when no worker has told us which classes are output nodes.
FALLBACK_OUTPUT_CLASSES = {
    "SaveImage", "PreviewImage", "PreviewAny", "VHS_VideoCombine", "Image Save", "ComfyFarmSaveImage",
    "easy clearCacheAll", "SaveVideo", "SaveAnimatedWEBP",
}


class ParamError(ValueError):
    """Bad job parameter; the message is shown to the artist."""


# Param types the gateway wires in itself (no `targets` in the manifest):
#   input          -> the images the workflow takes (a Port): path mapping, frames, the
#                     loader's inputs (loaders.py)
#   sequence       -> the same, written the older way: an `images` input whose loader is in
#                     the manifest's `sequence:` section
#   output_folder  -> where results go; the gateway still adds <shot>/<shot>_<tag>_vNNN
GATEWAY_TYPES = ("input", "sequence", "output_folder")
INPUT_TYPES = ("input", "sequence")
TAKES = ("image", "images")


@dataclass
class Port:
    """A workflow's image input: what it takes, and the node that loads it.

    takes: image (exactly one image: a sequence or movie must name its frame) or images
    (a batch of frames). per_run (texture workflows): the most tiles one run takes; more
    make a set of runs into one folder.

    A workflow with several inputs names one `primary`: it names the outputs and numbers
    their frames. Another input with `match: <primary>` is read at the primary's frames
    (a matte for a plate). An optional input (required: false) left empty writes its
    `empty` values into the graph (e.g. a switch that skips its branch); given, its
    `given` values."""
    name: str                       # its param
    takes: str = "images"
    loader: dict = field(default_factory=dict)      # {node, class}
    path_node: dict | None = None   # informational: gets the input as resolved
    work_resolution: int | None = None
    max_frames: int | None = None
    per_run: int | None = None
    primary: bool = False
    match: str | None = None
    required: bool = True
    empty: list = field(default_factory=list)       # [{node, input, class, value}] when left empty
    given: list = field(default_factory=list)       # [{node, input, class, value}] when given

    @property
    def adapter(self) -> L.Loader | None:
        return L.get(self.loader.get("class"))


@dataclass
class Target:
    node: str
    input: str
    cls: str | None = None


@dataclass
class Param:
    name: str
    type: str
    label: str = ""
    default: Any = None
    required: bool = False
    help: str = ""
    min: float | None = None
    max: float | None = None
    multiple_of: int | None = None
    choices: list | None = None
    targets: list[Target] = field(default_factory=list)
    pattern: str | None = None    # text params: must match this regex (empty is always allowed)
    invalid: str = ""             # shown when the pattern doesn't match
    naming: bool = False          # only used in output file names (texture workflows): no targets

    takes: str | None = None      # input params: image | images

    def schema(self) -> dict:
        # Inputs show as "sequence" (a field for a path to images) whichever way the manifest
        # writes them, so clients that predate `input` keep working; `takes` says how many.
        kind = "sequence" if self.type in INPUT_TYPES else self.type
        d = {"name": self.name, "type": kind, "label": self.label or self.name,
             "required": self.required, "default": self.default, "help": self.help}
        if self.takes:
            d["takes"] = self.takes
        for k in ("min", "max", "multiple_of", "choices"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.naming:
            d["naming"] = True   # goes into the output file names: forms refresh the preview
        return d

    def coerce(self, value: Any) -> Any:
        n = self.label or self.name
        if self.type in ("text", "string", "input", "sequence", "output_folder"):
            if not isinstance(value, str):
                raise ParamError(f"{n} must be text.")
            if len(value) > 20000:
                raise ParamError(f"{n} is too long.")
            if self.pattern and value.strip() and not re.fullmatch(self.pattern, value.strip()):
                raise ParamError(f"{n} '{value.strip()}' won't do: {self.invalid or 'expected ' + self.pattern}")
            return value.strip() if self.pattern else value
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
                return True
            if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off"):
                return False
            if isinstance(value, int) and value in (0, 1):
                return bool(value)
            raise ParamError(f"{n} must be true or false.")
        if self.type in ("int", "float"):
            if isinstance(value, bool):
                raise ParamError(f"{n} must be a number.")
            try:
                num = int(value) if self.type == "int" else float(value)
                if self.type == "int" and isinstance(value, float) and value != int(value):
                    raise ValueError
            except (TypeError, ValueError):
                raise ParamError(f"{n} must be {'a whole number' if self.type == 'int' else 'a number'}.")
            if self.min is not None and num < self.min:
                raise ParamError(f"{n} must be at least {self.min:g}.")
            if self.max is not None and num > self.max:
                raise ParamError(f"{n} must be at most {self.max:g}.")
            if self.multiple_of and num % self.multiple_of:
                raise ParamError(f"{n} must be a multiple of {self.multiple_of}.")
            return num
        if self.type == "choice":
            if value not in (self.choices or []):
                raise ParamError(f"{n} must be one of: {', '.join(map(str, self.choices or []))}.")
            return value
        raise ParamError(f"{n} has unknown type {self.type}.")


def _target(d: dict) -> Target:
    return Target(node=str(d["node"]), input=d.get("input", ""), cls=d.get("class"))


def _is_link(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)


def _combo_options(spec: Any) -> list[str] | None:
    if not isinstance(spec, list) or not spec:
        return None
    opts = None
    if isinstance(spec[0], list):
        opts = spec[0]
    elif spec[0] == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        opts = spec[1].get("options")
    if opts and all(isinstance(o, str) for o in opts):
        return opts
    return None


def fit_to_options(prompt: dict, object_info: dict | None) -> dict:
    """File names in a prompt's lists (LoRAs, checkpoints) written as another machine's
    ComfyUI lists them: a workflow saved on Windows says LTX\\x.safetensors, a Linux ComfyUI
    lists LTX/x.safetensors and refuses anything else. Changes the prompt in place."""
    for node in prompt.values():
        info = (object_info or {}).get(node.get("class_type")) or {}
        spec = {}
        for section in ("required", "optional"):
            spec.update((info.get("input") or {}).get(section) or {})
        inputs = node.get("inputs") or {}
        for key, val in inputs.items():
            if not isinstance(val, str) or ("\\" not in val and "/" not in val):
                continue
            opts = _combo_options(spec.get(key))
            if not opts or val in opts:
                continue
            same = [o for o in opts if o.replace("\\", "/") == val.replace("\\", "/")]
            if same:
                inputs[key] = same[0]
    return prompt


class Workflow:
    def __init__(self, directory: Path):
        self.dir = directory
        m = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8")) or {}
        self.name: str = m.get("name") or directory.name
        self.label: str = m.get("label") or self.name
        self.description: str = (m.get("description") or "").strip()
        self.tag: str = m.get("tag") or self.name
        # Which machines may run it: the least GPU memory (GB, as the card is sold: a 5090 is
        # 32) and labels its agent.yaml must have (tags; linux / windows always set).
        req = m.get("requires") or {}
        self.requires: dict = {"vram_gb": req.get("vram_gb"), "tags": [str(t).lower() for t in req.get("tags") or []]} \
            if isinstance(req, dict) else {"bad": req}
        # Published through the studio's pipeline (comfyfarm.publish). Every video and image
        # workflow is; `publish: {step: lay}` names the step offered first, `publish: false`
        # keeps a workflow out. None: no section (the farm's defaults).
        pub = m.get("publish")
        self.publish: dict | None = (
            {"step": pub.get("step"), "graph": pub.get("graph") or "update_and_render_nuke"} if isinstance(pub, dict)
            else {"off": True} if pub is False else {"bad": pub} if pub is not None else None)
        # A preview movie of its frames, made by the agent (fileio.preview): every workflow
        # writing frames gets one unless `preview: false`.
        self.preview_movie = m.get("preview", True)
        # ShotGrid's right-click menu (comfyfarm.shotgrid.menu): where it goes is worked out
        # from its inputs; `shotgrid: false` keeps it out, `shotgrid: {submit: false}` out of
        # the Submit folders, `title:` names it there (default: its label).
        self.shotgrid = m.get("shotgrid", True)
        # Node packs it needs that none of its node classes shows, named by their custom_nodes
        # folder: a pack that adds samplers or schedulers to a core node (RES4LYF's res_2s and
        # beta57 for KSampler). They join the packs found from its nodes in the farm's
        # environment file (comfyenv.spec.needs).
        self.node_packs = m.get("node_packs") or []
        self.template: dict = json.loads((directory / m.get("template", "workflow_api.json")).read_text(encoding="utf-8"))
        # The definition's fingerprint: the manifest and the graph as parsed, so a checkout
        # with other line endings has the same one. A job names the one it was planned with,
        # and a machine whose copy differs doesn't build its graph (build_from).
        self.digest: str = hashlib.sha256(
            json.dumps([m, self.template], sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
        self.params: dict[str, Param] = {}
        for pname, p in (m.get("params") or {}).items():
            self.params[pname] = Param(
                name=pname, type=p.get("type", "string"), label=p.get("label", ""),
                default=p.get("default"), required=bool(p.get("required", False)),
                help=(p.get("help") or "").strip(), min=p.get("min"), max=p.get("max"),
                multiple_of=p.get("multiple_of"), choices=p.get("choices"),
                targets=[_target(t) for t in p.get("targets", [])],
                pattern=p.get("pattern"), invalid=(p.get("invalid") or "").strip(),
                naming=bool(p.get("naming", False)),
                takes=(p.get("takes") or "images") if p.get("type") in INPUT_TYPES else None,
            )
        # The image inputs. `type: sequence` is the older way to write an `images` input,
        # with its loader in the manifest's `sequence:` section.
        legacy = m.get("sequence") or {}
        self.ports: dict[str, Port] = {}
        for pname, p in (m.get("params") or {}).items():
            if p.get("type") == "input":
                self.ports[pname] = Port(pname, takes=p.get("takes") or "images", loader=p.get("loader") or {},
                                         path_node=p.get("path_node"), work_resolution=p.get("work_resolution"),
                                         max_frames=(p.get("frames") or {}).get("max"), per_run=p.get("per_run"),
                                         primary=bool(p.get("primary", False)), match=p.get("match"),
                                         required=bool(p.get("required", False)),
                                         empty=list(p.get("empty") or []), given=list(p.get("given") or []))
            elif p.get("type") == "sequence":
                self.ports[pname] = Port(pname, loader=legacy.get("loader") or {}, path_node=legacy.get("path_node"),
                                         required=bool(p.get("required", False)))
        # Inputs larger than this (pixels, longer side) are scaled down to it before the
        # workflow runs, keeping their aspect (convert.resolve_input). None = as they are.
        self.manifest_work_resolution = m.get("work_resolution") or None    # for every input without its own
        self.work_resolution = self.manifest_work_resolution or (self.port.work_resolution if self.port else None)
        self.outputs: dict = m.get("outputs") or {}
        # Texture workflows: several map save nodes, written as EXRs named by the TEX
        # convention (see textures.py) instead of one renamed frame sequence.
        self.textures: T.TextureSpec | None = T.TextureSpec.from_manifest(m["textures"]) if m.get("textures") else None
        self._batch_twice = bool(self.textures and self.textures.batch is not None and self.port and self.port.per_run)
        pr = self.port.per_run if self.port else None
        if self.textures and isinstance(pr, int) and not isinstance(pr, bool) and pr >= 1:
            self.textures.batch = pr                    # textures.batch: the older name
        self.problems: list[str] = self.static_check()

    # ------------------------------------------------------------------ checks
    @property
    def enabled(self) -> bool:
        return not self.problems

    @property
    def port(self) -> Port | None:
        """The primary image input: it names the outputs and numbers their frames (the only
        one, or the one marked primary)."""
        return next((p for p in self.ports.values() if p.primary), None) or next(iter(self.ports.values()), None)

    @property
    def other_ports(self) -> list[Port]:
        """The inputs besides the primary, in the manifest's order."""
        return [p for p in self.ports.values() if p is not self.port]

    @property
    def sequence_param(self) -> str | None:
        """The name of the image input's param."""
        return self.port.name if self.port else None

    @property
    def loader(self) -> L.Loader | None:
        """The adapter for the input's loader node (None if it has none)."""
        return self.port.adapter if self.port else None

    @property
    def output_folder_param(self) -> str | None:
        return next((n for n, p in self.params.items() if p.type == "output_folder"), None)

    def title(self, node_id: str) -> str:
        n = self.template.get(node_id) or {}
        return (n.get("_meta") or {}).get("title") or n.get("class_type") or node_id

    def _check_target(self, where: str, node: str, cls: str | None, input_name: str | None) -> list[str]:
        n = self.template.get(node)
        if n is None:
            return [f"{where}: node {node} is not in the workflow (re-exported? update the manifest)"]
        out = []
        if cls and n.get("class_type") != cls:
            out.append(f"{where}: node {node} is {n.get('class_type')}, manifest expects {cls}")
        if input_name and input_name not in n.get("inputs", {}):
            out.append(f"{where}: node {node} has no input '{input_name}'")
        return out

    @property
    def makes_preview(self) -> bool:
        """The agent makes a preview movie of this workflow's frames: it writes frames, has
        no video node of its own (outputs.preview), and its manifest doesn't say preview: false."""
        return bool(self.outputs.get("frames") and not self.textures and not self.outputs.get("preview")
                    and self.preview_movie is not False)

    def static_check(self) -> list[str]:
        problems = []
        if self.publish is not None and ("bad" in self.publish or
                                         (self.publish["step"] is not None and not isinstance(self.publish["step"], str))):
            problems.append("publish: needs to be a section like {step: lay}")
        if not isinstance(self.preview_movie, bool):
            problems.append("preview: needs to be true or false (false: no preview movie of the frames)")
        sg = self.shotgrid
        if not (isinstance(sg, bool) or (isinstance(sg, dict) and set(sg) <= {"submit", "title"}
                                          and isinstance(sg.get("submit", True), bool)
                                          and isinstance(sg.get("title", ""), str))):
            problems.append("shotgrid: needs to be false, or a section like {submit: false, title: <its name in the menu>}")
        packs = self.node_packs
        if not (isinstance(packs, list) and all(isinstance(p, str) and p.strip() and not re.search(r"[/\\]", p)
                                                 for p in packs)):
            problems.append("node_packs: needs to be a list of node pack folder names (as in custom_nodes), "
                            "e.g. [RES4LYF]")
        for p in self.params.values():
            if p.type in GATEWAY_TYPES or p.naming:
                if p.targets:
                    problems.append(f"param {p.name}: " + (f"type {p.type} is wired in by the gateway"
                                    if p.type in GATEWAY_TYPES else "naming params only go into file names")
                                    + "; remove its targets")
            elif not p.targets:
                problems.append(f"param {p.name}: no targets")
            if p.pattern:
                try:
                    re.compile(p.pattern)
                except re.error as e:
                    problems.append(f"param {p.name}: bad pattern ({e})")
            for t in p.targets:
                problems += self._check_target(f"param {p.name}", t.node, t.cls, t.input)
        for t in ("sequence", "output_folder"):         # several inputs are written `type: input`
            names = [n for n, p in self.params.items() if p.type == t]
            if len(names) > 1:
                problems.append(f"only one param may have type {t} (found {', '.join(names)})")
        kinds = {p.type for p in self.params.values() if p.type in INPUT_TYPES}
        if len(kinds) > 1:
            problems.append("inputs are written either `type: input` or the older `type: sequence`, not both")
        if self.output_folder_param and not (self.outputs.get("frames") or self.textures):
            problems.append(f"param {self.output_folder_param}: type output_folder needs outputs.frames "
                            f"or textures")
        if self.textures:
            if self.outputs.get("frames"):
                problems.append("textures and outputs.frames can't be used together")
            naming = {n for n, p in self.params.items() if p.naming}
            problems += self.textures.check(naming)
            for mp in self.textures.maps:
                for key in (self.textures.path_input, self.textures.prefix_input):
                    problems += self._check_target(f"textures map {mp.channel}", mp.node, mp.cls, key)
        wr = self.work_resolution
        if wr is not None and (isinstance(wr, bool) or not isinstance(wr, int) or wr < 64):
            problems.append(f"work_resolution: {wr!r} isn't a size in pixels (64 or more), "
                            f"e.g. 1024; leave it out to use inputs as they are")
        elif wr and not self.port:
            problems.append("work_resolution needs an input param (it scales the input)")
        req = self.requires
        if "bad" in req:
            problems.append(f"requires must be a mapping ({{vram_gb: 32, tags: [...]}}), not {req['bad']!r}")
        else:
            vg = req.get("vram_gb")
            if vg is not None and (isinstance(vg, bool) or not isinstance(vg, (int, float)) or vg <= 0):
                problems.append(f"requires.vram_gb {vg!r} isn't a number of GB")
        primaries = [n for n, p in self.ports.items() if p.primary]
        if len(primaries) > 1:
            problems.append(f"only one input can be primary (found {', '.join(primaries)})")
        elif len(self.ports) > 1 and not primaries:
            problems.append(f"several inputs ({', '.join(self.ports)}): mark the one that names and numbers the "
                            f"outputs primary: true")
        if len(self.ports) > 1 and self.textures:
            problems.append("texture workflows take one input")
        for port in self.ports.values():
            problems += self._check_port(port)
        for nid, n in self.template.items():
            if n.get("class_type") in L.VIDEO_LOADERS:
                problems.append(f"node {nid} '{self.title(nid)}' loads a movie ({n['class_type']}): farm workflows "
                                f"only take images. Load frames with ComfyFarm Load Images; the farm extracts a "
                                f"movie's frames itself")
        o = self.outputs
        if o.get("root_node"):
            r = o["root_node"]
            problems += self._check_target("outputs.root_node", str(r["node"]), r.get("class"), r.get("input"))
        if o.get("frames"):
            f = o["frames"]
            for key in ("path_input", "prefix_input"):
                problems += self._check_target("outputs.frames", str(f["node"]), f.get("class"), f.get(key))
        if o.get("preview"):
            pv = o["preview"]
            problems += self._check_target("outputs.preview", str(pv["node"]), pv.get("class"), pv.get("prefix_input"))
        return sorted(set(problems))

    def _check_port(self, port: Port) -> list[str]:
        legacy = getattr(self.params.get(port.name), "type", None) == "sequence"
        where = "sequence" if legacy else f"param {port.name}"
        problems = []
        if port.takes not in TAKES:
            return [f"{where}: takes must be image or images, not {port.takes!r}"]
        pn = port.path_node
        if pn:
            problems += self._check_target(f"{where}.path_node", str(pn["node"]), pn.get("class"), pn.get("input"))
        ld = port.loader
        if not ld:
            return problems + [f"{where}.loader missing"]
        ad = port.adapter
        if ad is None:
            return problems + [f"{where}.loader: class {ld.get('class')!r} isn't a loader the farm can fill "
                               f"(use {', '.join(L.LOADERS)})"]
        if ad.takes != port.takes:
            problems.append(f"{where}.loader: {ad.cls} loads {'one image' if ad.takes == 'image' else 'frames'}, so "
                            f"the input takes {ad.takes}, not {port.takes}")
        for inp in ad.inputs:
            problems += self._check_target(f"{where}.loader", str(ld["node"]), ld.get("class"), inp)
        mx = port.max_frames
        if mx is not None and (isinstance(mx, bool) or not isinstance(mx, int) or mx < 1):
            problems.append(f"{where}: frames.max {mx!r} isn't a number of frames (1 or more)")
        elif mx is not None and port.takes == "image":
            problems.append(f"{where}: frames.max is for inputs that take images; this one takes one image")
        pr = port.per_run
        if pr is not None:
            if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
                problems.append(f"{where}: per_run {pr!r} isn't a number of tiles (1 or more)")
            elif not self.textures:
                problems.append(f"{where}: per_run splits texture inputs into runs; frame inputs can't be split yet")
        if self._batch_twice:
            problems.append(f"{where}: per_run and textures.batch say the same thing; keep per_run")
        main = self.port
        if port is main:
            if len(self.ports) > 1 and not port.required:
                problems.append(f"{where}: the primary input names the outputs, so it must be required")
            if port.match:
                problems.append(f"{where}: the primary input can't match another")
        else:
            if port.per_run is not None:
                problems.append(f"{where}: per_run is for the primary input")
            if port.match is not None and port.match != main.name:
                problems.append(f"{where}: match names the primary input ({main.name}), not {port.match!r}")
            elif port.match and port.takes == "image" and main.takes == "images":
                problems.append(f"{where}: takes one image, so it can't match {main.name}'s frames")
        if not port.required and port is not main and ad.lists_folder:
            problems.append(f"{where}: an optional input needs a ComfyFarm loader ({ad.cls} can't be left empty)")
        for kind in ("empty", "given"):
            sets = getattr(port, kind)
            if sets and port.required:
                problems.append(f"{where}.{kind}: only optional inputs (required: false) can be left empty")
            for i, t in enumerate(sets):
                if not isinstance(t, dict) or "node" not in t or "input" not in t or "value" not in t:
                    problems.append(f"{where}.{kind}[{i}]: needs node, input and value")
                    continue
                problems += self._check_target(f"{where}.{kind}[{i}]", str(t["node"]), t.get("class"), t["input"])
        return problems

    def check_object_info(self, object_info: dict) -> list[str]:
        """Compare the template with what a worker actually has installed: missing node
        packs, inputs a node-pack update renamed, and model files that aren't there."""
        problems = []
        for nid, node in self.template.items():
            ct = node.get("class_type")
            info = object_info.get(ct)
            label = f"node {nid} '{self.title(nid)}'"
            if info is None:
                if (ct or "").startswith("ComfyFarm"):
                    problems.append(f"{label}: class {ct} is not installed. It comes with this repo: run "
                                    f"deploy\\windows\\setup.ps1 -ComfyUIDir <ComfyUI folder> (Windows) or "
                                    f"deploy/linux/setup.sh agent --comfyui <ComfyUI folder> (Linux), "
                                    f"then restart ComfyUI")
                else:
                    problems.append(f"{label}: class {ct} is not installed (missing custom node pack?)")
                continue
            spec = {}
            for section in ("required", "optional"):
                spec.update((info.get("input") or {}).get(section) or {})
            for key, val in (node.get("inputs") or {}).items():
                base = key.split(".", 1)[0]
                if base not in spec:
                    problems.append(
                        f"{label}: {ct} has no input '{base}' on this worker "
                        f"(node pack version differs from the one the workflow was built with)")
                    continue
                if _is_link(val) or "." in key:
                    continue
                opts = _combo_options(spec[base])
                if opts is not None and isinstance(val, str):
                    norm = {o.replace("\\", "/") for o in opts}
                    if val.replace("\\", "/") not in norm:
                        problems.append(f"{label}: '{val}' is not available for {key} ({self._missing_hint(val)})")
        return problems

    def _missing_hint(self, val: str) -> str:
        """Why a list value (a model, a sampler) may be missing on a worker."""
        if re.search(r"[/\\]|\.[A-Za-z0-9]{2,12}$", val):
            return "missing model/file?"
        packs = [p for p in self.node_packs if isinstance(p, str)] if isinstance(self.node_packs, list) else []
        if packs:
            return f"{'node pack' if len(packs) == 1 else 'node packs'} {', '.join(packs)} should provide it: installed?"
        return "missing model/file or node pack?"

    def expected_nodes(self, output_classes: set[str] | None = None) -> int:
        """How many nodes ComfyUI will run: ancestors of output nodes (an estimate;
        lazy switch branches that don't run are still counted)."""
        outs = output_classes or FALLBACK_OUTPUT_CLASSES
        roots = [nid for nid, n in self.template.items() if n.get("class_type") in outs]
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            nid = stack.pop()
            if nid in seen or nid not in self.template:
                continue
            seen.add(nid)
            for v in (self.template[nid].get("inputs") or {}).values():
                if _is_link(v):
                    stack.append(v[0])
        return max(len(seen), 1)

    # ------------------------------------------------------------------ build
    def validate(self, values: dict) -> dict:
        unknown = sorted(set(values) - set(self.params))
        if unknown:
            raise ParamError(f"Unknown parameter(s): {', '.join(unknown)}. "
                             f"Known: {', '.join(self.params)}.")
        out = {}
        for name, p in self.params.items():
            if name in values and values[name] is not None and values[name] != "":
                out[name] = p.coerce(values[name])
            elif p.required:
                raise ParamError(f"{p.label or name} is required.")
            elif p.default is not None:
                out[name] = p.coerce(p.default)
        return out

    def build(self, values: dict, seq=None, out_dir: str | None = None,
              frames_prefix: str | None = None, preview_prefix: str | None = None,
              textures: dict | None = None, inputs: dict | None = None) -> dict:
        """The prompt for one run. `seq` is the primary input as resolved, `inputs` the
        others by name (missing or None: left empty)."""
        prompt = copy.deepcopy(self.template)
        for name, p in self.params.items():
            if p.type in INPUT_TYPES or p.naming or name not in values:
                continue
            for t in p.targets:
                prompt[t.node]["inputs"][t.input] = values[name]
        port = self.port
        if seq is not None and port:
            self._fill_port(prompt, port, seq)
            for other in self.other_ports:      # the other inputs: given, or left empty
                self._fill_port(prompt, other, (inputs or {}).get(other.name))
        o = self.outputs
        if out_dir:
            if o.get("root_node"):
                r = o["root_node"]
                prompt[str(r["node"])]["inputs"][r["input"]] = out_dir
            if o.get("frames"):
                f = o["frames"]
                prompt[str(f["node"])]["inputs"][f["path_input"]] = out_dir
                if frames_prefix:
                    prompt[str(f["node"])]["inputs"][f["prefix_input"]] = frames_prefix
        if preview_prefix and o.get("preview"):
            pv = o["preview"]
            prompt[str(pv["node"])]["inputs"][pv["prefix_input"]] = preview_prefix
        if textures and self.textures:
            # Each map's save node writes PNGs into the job's scratch folder; the gateway
            # turns them into the named EXRs afterwards (textures.write_maps).
            for mp in textures["maps"]:
                inputs = prompt[mp["node"]]["inputs"]
                inputs[self.textures.path_input] = textures["raw_dir"]
                inputs[self.textures.prefix_input] = mp["prefix"]
        return prompt

    def request(self, values: dict, seq=None, out_dir: str | None = None,
                frames_prefix: str | None = None, preview_prefix: str | None = None,
                textures: dict | None = None, inputs: dict | None = None) -> dict:
        """What build() takes, as plain data: the manager stores this in place of the graph,
        and the machine that runs the job builds the graph from it (build_from)."""
        return {"definition": self.digest, "values": values, "seq": seq.as_dict() if seq is not None else None,
                "out_dir": out_dir, "frames_prefix": frames_prefix, "preview_prefix": preview_prefix,
                "textures": textures,
                "inputs": {k: v.as_dict() if v is not None else None for k, v in (inputs or {}).items()}}

    def build_from(self, request: dict) -> dict:
        """The prompt for a stored request. ParamError when this copy of the workflow isn't
        the one the job was planned with."""
        if request.get("definition") != self.digest:
            raise ParamError(f"This machine's copy of the workflow {self.name} ({self.digest}) is not the one the "
                             f"job was planned with ({request.get('definition')}). Update the farm's code on it.")

        def seq(d):
            return Sequence(**d) if d is not None else None
        return self.build(request["values"], seq(request.get("seq")), request.get("out_dir"),
                          request.get("frames_prefix"), request.get("preview_prefix"),
                          textures=request.get("textures"),
                          inputs={k: seq(v) for k, v in (request.get("inputs") or {}).items()})

    @staticmethod
    def _fill_port(prompt: dict, port: Port, seq) -> None:
        if seq is None:
            port.adapter.fill_empty(prompt[str(port.loader["node"])]["inputs"])
        else:
            pn = port.path_node
            if pn:
                rng = "" if seq.first is None else f" {seq.first}" if seq.first == seq.last else f" {seq.first}-{seq.last}"
                prompt[str(pn["node"])]["inputs"][pn["input"]] = seq.farm_path + rng
            port.adapter.fill(prompt[str(port.loader["node"])]["inputs"], seq)
        for t in port.empty if seq is None else port.given:
            prompt[str(t["node"])]["inputs"][t["input"]] = t["value"]

    def schema(self) -> dict:
        params = []
        for name, p in self.params.items():
            d = p.schema()
            port = self.ports.get(name)
            if port is not None:
                d["primary"] = port is self.port        # names the outputs (the destination line)
                if port.match:
                    d["match"] = port.match             # read at that input's frames
            params.append(d)
        return {
            "name": self.name, "label": self.label, "description": self.description,
            "tag": self.tag, "enabled": self.enabled, "problems": self.problems,
            "work_resolution": self.work_resolution,
            "requires": self.requires if "bad" not in self.requires else None,
            "publish": self.publish if self.publish and "bad" not in self.publish and "off" not in self.publish
            else None,
            "params": params,
            "maps": [mp.channel for mp in self.textures.maps] if self.textures else None,
            "preview": self.makes_preview or bool(self.outputs.get("preview")),
        }


class WorkflowRegistry:
    def __init__(self, directory: Path):
        self.directory = directory
        self.workflows: dict[str, Workflow] = {}
        self.load_errors: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self.workflows.clear()
        self.load_errors.clear()
        if not self.directory.is_dir():
            log.warning("workflows folder %s not found", self.directory)
            return
        for d in sorted(self.directory.iterdir()):
            if not (d / "manifest.yaml").exists():
                continue
            try:
                wf = Workflow(d)
                self.workflows[wf.name] = wf
                for p in wf.problems:
                    log.error("workflow %s: %s", wf.name, p)
            except Exception as e:  # noqa: BLE001 - report and keep the others
                self.load_errors[d.name] = str(e)
                log.exception("failed to load workflow %s", d.name)

    def get(self, name: str) -> Workflow:
        wf = self.workflows.get(name)
        if wf is None:
            raise ParamError(f"Unknown workflow '{name}'. Available: {', '.join(self.workflows) or 'none'}.")
        if not wf.enabled:
            raise ParamError(f"Workflow '{name}' is disabled: {'; '.join(wf.problems)}")
        return wf
