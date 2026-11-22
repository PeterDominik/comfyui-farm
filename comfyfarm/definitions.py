"""Process definitions: what the farm can run, in the half any runner shares.

A definition is a folder with one of two files (docs/decisions/0015, 0019):

- process.yaml: a head any runner shares (id, label, class, targets, requires, limits,
  models, eval, params, what it writes, publish ...) and a `runner:` block only that
  runner reads. For `runner.type: process` the block names the environment and the
  command (runners/process.py). For `runner.type: comfyui` it holds the graph's file and which nodes
  each parameter, input and output drives.
- manifest.yaml: the older form for ComfyUI workflows, with the wiring written inside each
  parameter. It loads as `runner.type: comfyui`.

This module reads both and checks the head: it is what the manager plans and validates
jobs with, and it imports nothing of any runner. How a ComfyUI workflow wires its graph,
and building that graph, is runners/comfyui_workflow.py (decision 0021)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import artifacts as A
from .fileio import textures as T

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- process.yaml <-> manifest.yaml
# Where a process.yaml keeps what a manifest.yaml mixes into its params and top level.
RUNNER_PARAM_KEYS = ("targets", "loader", "path_node", "empty", "given")   # per param: the nodes it drives
RUNNER_SECTIONS = ("template", "node_packs", "sequence", "outputs", "textures")
# The head's fields that manifests don't have. Read and checked now; the scheduler acts on
# them from later phases (classes and limits with the process runner, targets with Tractor,
# models with the policy gates, eval with the evals).
HEAD_ONLY = ("version", "class", "targets", "limits", "models", "eval")
JOB_CLASSES = ("interactive", "batch")
TARGETS = ("pool", "tractor")
RUNNER_TYPES = ("comfyui", "process")
# A process runner's block (runners/process.py, decision 0025): the environment the command
# runs in (environments/<name>) and the command. Nothing else: it has no graph and no nodes.
PROCESS_KEYS = ("environment", "command")
# What a definition writes (the head's `writes:`, docs/decisions/0019) and which nodes write
# it (the runner block's `outputs:` and `textures:`). A manifest's outputs and textures
# sections hold both halves. Other kinds of output take the generic route (decision 0036):
# writes.<kind>, said by the handler of that kind (comfyfarm/artifacts), and for ComfyUI
# the node that writes them, runner.outputs.<kind>.
WRITES_KEYS = ("frames", "preview", "textures")
OUTPUTS_KEYS = ("root_node", "frames", "preview")
GENERIC_WIRING_KEYS = ("node", "class", "prefix_input")
TEX_HEAD_KEYS = ("name", "dir", "ext", "batch", "maps")
TEX_RUNNER_KEYS = ("path_input", "prefix_input", "maps")
MAP_HEAD_KEYS = ("channel", "name", "colorspace", "color", "bits")
MAP_RUNNER_KEYS = ("node", "class")


def _section(d, where: str) -> dict:
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ValueError(f"{where}: needs to be a section")
    return d


def _keys(d: dict, where: str, allowed, elsewhere: dict | None = None) -> None:
    """Unknown keys of a section, and ones that belong in the other half (`elsewhere`)."""
    for k in d:
        if elsewhere and k in elsewhere:
            raise ValueError(f"{where}.{k}: belongs in {elsewhere[k]} in a process.yaml")
        if k not in allowed:
            raise ValueError(f"{where}: unknown key {k} (it takes {', '.join(allowed)})")


def _together(head: dict, runner: dict, key: str, head_where: str, runner_where: str, what: str) -> bool:
    if (key in head) != (key in runner):
        have, need = (head_where, runner_where) if key in head else (runner_where, head_where)
        raise ValueError(f"{have}.{key} needs {need}.{key}: one says {what}, the other which node does it")
    return key in head


def _writes_to_manifest(writes: dict, runner: dict) -> tuple[dict | None, dict | None, Any]:
    """The head's writes and the runner block's outputs and textures as a manifest's
    outputs and textures sections, and its top-level preview (None when not said)."""
    w = _section(writes, "writes")
    generic = A.generic_kinds()
    _keys(w, "writes", (*WRITES_KEYS, *generic), {"root_node": "runner.outputs"})
    ro = _section(runner.get("outputs"), "runner.outputs")
    _keys(ro, "runner.outputs", (*OUTPUTS_KEYS, *generic))
    outputs: dict = {}
    if "root_node" in ro:
        outputs["root_node"] = ro["root_node"]
    if _together(w, ro, "frames", "writes", "runner.outputs", "that it writes frames"):
        wf, rf = _section(w["frames"], "writes.frames"), _section(ro["frames"], "runner.outputs.frames")
        _keys(wf, "writes.frames", ("ext",), {k: "runner.outputs.frames" for k in ("node", "class", "path_input",
                                                                                      "prefix_input")})
        if "ext" in rf:
            raise ValueError("runner.outputs.frames.ext: what the frames are written as belongs in writes.frames.ext")
        outputs["frames"] = {**rf, **wf}
    preview = w.get("preview")
    if preview == "own" or "preview" in ro:
        if preview != "own":
            raise ValueError("runner.outputs.preview: the run makes its own preview video, so say writes.preview: own")
        if "preview" not in ro:
            raise ValueError("writes.preview: own needs runner.outputs.preview: the node that writes the video")
        outputs["preview"] = ro["preview"]
        preview = None
    elif preview is not None and not isinstance(preview, bool):
        raise ValueError("writes.preview: true (the farm makes a preview movie of the frames), false or own")
    tr = _section(runner.get("textures"), "runner.textures")
    textures = None
    if _together(w, {"textures": 1} if "textures" in runner else {}, "textures", "writes", "runner",
                 "which texture maps it writes"):
        tw = _section(w["textures"], "writes.textures")
        _keys(tw, "writes.textures", TEX_HEAD_KEYS, {k: "runner.textures" for k in TEX_RUNNER_KEYS if k != "maps"})
        _keys(tr, "runner.textures", TEX_RUNNER_KEYS, {k: "writes.textures" for k in TEX_HEAD_KEYS if k != "maps"})
        maps_w, maps_r = tw.get("maps") or [], _section(tr.get("maps"), "runner.textures.maps")
        if not isinstance(maps_w, list) or not all(isinstance(m, dict) and m.get("channel") for m in maps_w):
            raise ValueError("writes.textures.maps: needs to be a list of maps, each with its channel")
        channels = [m["channel"] for m in maps_w]
        for c, wiring in maps_r.items():
            if c not in channels:
                raise ValueError(f"runner.textures.maps.{c}: writes.textures.maps has no map with that channel")
            _keys(_section(wiring, f"runner.textures.maps.{c}"), f"runner.textures.maps.{c}", MAP_RUNNER_KEYS,
                  {k: "writes.textures.maps" for k in MAP_HEAD_KEYS})
        merged = []
        for m in maps_w:
            _keys(m, f"writes.textures.maps ({m['channel']})", MAP_HEAD_KEYS,
                  {k: f"runner.textures.maps.{m['channel']}" for k in MAP_RUNNER_KEYS})
            if m["channel"] not in maps_r:
                raise ValueError(f"writes.textures.maps ({m['channel']}): needs runner.textures.maps.{m['channel']}, "
                                 f"the node that saves it")
            merged.append({**m, **maps_r[m["channel"]]})
        textures = {**{k: tw[k] for k in TEX_HEAD_KEYS if k in tw and k != "maps"},
                    **{k: tr[k] for k in TEX_RUNNER_KEYS if k in tr and k != "maps"}, "maps": merged}
    kinds = {}
    for kind in (k for k in generic if k in w or k in ro):
        _together(w, ro, kind, "writes", "runner.outputs", f"that it writes {kind}")
        kinds[kind] = A.by_kind(kind).head(w[kind])
        wiring = _section(ro[kind], f"runner.outputs.{kind}")
        _keys(wiring, f"runner.outputs.{kind}", GENERIC_WIRING_KEYS)
        if "node" not in wiring:
            raise ValueError(f"runner.outputs.{kind}: needs the node that writes it ({{node: \"12\"}})")
        outputs[kind] = wiring
    if kinds and textures is not None:
        raise ValueError(f"writes.{next(iter(kinds))}: a texture set writes only its maps")
    return outputs or None, textures, preview, kinds


def _process_block(p: dict, runner: dict) -> dict:
    """A process runner's definition as a manifest-like dict: the head as for any runner, its
    writes as `outputs` (frames only), and its block under `process`."""
    comfy = sorted(k for k in runner if k in ("params", *RUNNER_SECTIONS))
    if comfy:
        raise ValueError(f"runner.{comfy[0]}: is ComfyUI wiring; a process runner takes {' and '.join(PROCESS_KEYS)}")
    unknown = sorted(set(runner) - {"type", *PROCESS_KEYS})
    if unknown:
        raise ValueError(f"runner block: unknown key(s) {', '.join(unknown)} (a process runner takes "
                         f"{' and '.join(PROCESS_KEYS)})")
    missing = [k for k in PROCESS_KEYS if k not in runner]
    if missing:
        raise ValueError(f"runner: a process runner needs {' and '.join(missing)} (runner: {{type: process, "
                         f"environment: gpu-test, command: [python, \"{{definition}}/run.py\"]}})")
    m = {k: v for k, v in p.items() if k not in ("runner", "id", "writes", *HEAD_ONLY)}
    if p.get("id"):
        m["name"] = p["id"]
    for name, spec in (p.get("params") or {}).items():
        wired = sorted(k for k in RUNNER_PARAM_KEYS if k in (spec or {}))
        if wired:
            raise ValueError(f"params.{name}: {', '.join(wired)} is ComfyUI wiring; a process gets its params in "
                             f"job.json")
    w = _section(p.get("writes"), "writes")
    generic = A.generic_kinds()
    _keys(w, "writes", ("frames", "preview", *generic))
    kinds = {kind: A.by_kind(kind).head(w[kind]) for kind in generic if kind in w}
    if kinds:
        m["writes"] = kinds             # the generic route (decision 0036)
    if "frames" in w:
        wf = _section(w["frames"], "writes.frames")
        _keys(wf, "writes.frames", ("ext",))
        m["outputs"] = {"frames": {"ext": wf.get("ext", "png")}}
    if "preview" in w:
        if w["preview"] == "own":
            raise ValueError("writes.preview: own isn't something a process can do yet; true (the farm makes the "
                             "preview movie of its frames) or false")
        m["preview"] = w["preview"]
    m["process"] = {k: runner[k] for k in PROCESS_KEYS}
    return m


def runner_type(directory: Path) -> str | None:
    """Which runner a definition folder is for, without loading it (None: can't tell)."""
    if not (directory / "process.yaml").exists():
        return "comfyui"                # a manifest.yaml is a ComfyUI workflow
    try:
        p = yaml.safe_load((directory / "process.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    runner = p.get("runner") if isinstance(p, dict) else None
    return runner.get("type") if isinstance(runner, dict) else None


def from_process(p: dict) -> tuple[dict, dict]:
    """A process.yaml as (the manifest it is equal to, its head-only fields)."""
    runner = p.get("runner")
    if not isinstance(runner, dict) or not runner.get("type"):
        raise ValueError("process.yaml needs a runner block with a type (runner: {type: comfyui, ...})")
    if runner["type"] not in RUNNER_TYPES:
        raise ValueError(f"runner type '{runner['type']}' isn't supported (supported: {', '.join(RUNNER_TYPES)})")
    for k in ("outputs", "textures"):
        if k in p:
            raise ValueError(f"{k}: in a process.yaml, what it writes goes under writes:"
                             + (f" and which nodes write it under runner.{k}" if runner["type"] == "comfyui" else ""))
    if "preview" in p:
        raise ValueError("preview: in a process.yaml it is writes.preview (true, false or own)")
    misplaced = sorted(k for k in RUNNER_SECTIONS if k in p)
    if misplaced:
        raise ValueError(f"{', '.join(misplaced)}: belongs in the runner block of a process.yaml")
    if runner["type"] == "process":
        return _process_block(p, runner), {k: p[k] for k in HEAD_ONLY if k in p}
    unknown = sorted(set(runner) - {"type", "params", *RUNNER_SECTIONS})
    if unknown:
        raise ValueError(f"runner block: unknown key(s) {', '.join(unknown)}")
    for k in ("outputs", "textures"):
        if k in p:
            raise ValueError(f"{k}: in a process.yaml, what it writes goes under writes: and which nodes write it "
                             f"under runner.{k}")
    if "preview" in p:
        raise ValueError("preview: in a process.yaml it is writes.preview (true, false or own)")
    misplaced = sorted(k for k in RUNNER_SECTIONS if k in p)
    if misplaced:
        raise ValueError(f"{', '.join(misplaced)}: belongs in the runner block of a process.yaml")
    m = {k: v for k, v in p.items() if k not in ("runner", "id", "writes", *HEAD_ONLY)}
    if p.get("id"):
        m["name"] = p["id"]
    params = {name: dict(spec or {}) for name, spec in (p.get("params") or {}).items()}
    for name, spec in params.items():
        wired = sorted(k for k in RUNNER_PARAM_KEYS if k in spec)
        if wired:
            raise ValueError(f"params.{name}: {', '.join(wired)} belong under runner.params.{name} in a process.yaml")
    for name, wiring in (runner.get("params") or {}).items():
        if name not in params:
            raise ValueError(f"runner.params.{name}: no such parameter in params")
        extra = sorted(set(wiring or {}) - set(RUNNER_PARAM_KEYS))
        if extra:
            raise ValueError(f"runner.params.{name}: unknown key(s) {', '.join(extra)}")
        params[name].update(wiring or {})
    if params:
        m["params"] = params
    for k in ("template", "node_packs", "sequence"):
        if k in runner:
            m[k] = runner[k]
    outputs, textures, preview, kinds = _writes_to_manifest(p.get("writes"), runner)
    if kinds:
        m["writes"] = kinds             # the generic route (decision 0036)
    if outputs is not None:
        m["outputs"] = outputs
    if textures is not None:
        m["textures"] = textures
    if preview is not None:
        m["preview"] = preview
    return m, {k: p[k] for k in HEAD_ONLY if k in p}


def read_definition(directory: Path) -> tuple[dict, dict, str]:
    """(manifest, head-only fields, the file it came from) of a definition folder."""
    process, manifest = directory / "process.yaml", directory / "manifest.yaml"
    if process.exists() and manifest.exists():
        raise ValueError("has both process.yaml and manifest.yaml: keep one")
    if process.exists():
        m, head = from_process(yaml.safe_load(process.read_text(encoding="utf-8")) or {})
        return m, head, process.name
    m = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    if "process" in m:
        raise ValueError("process: a definition for the process runner is a process.yaml (runner: {type: process})")
    if "writes" in m:
        raise ValueError("writes: is a process.yaml's (a manifest.yaml says outputs: and textures:)")
    return m, {}, manifest.name


def folder_digest(directory: Path, skip: tuple = ("process.yaml", "manifest.yaml")) -> dict:
    """Every file of a definition folder but its definition file, with a hash of its content
    (line endings aside, so a Windows checkout has the same one): a process definition's
    fingerprint covers the code its command runs."""
    out = {}
    for f in sorted(directory.rglob("*")):
        rel = f.relative_to(directory).as_posix()
        if not f.is_file() or rel in skip or "__pycache__" in f.parts or f.suffix in (".pyc", ".pyo"):
            continue
        out[rel] = hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    return out


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
    `given` values. own_folder: the runner reads every file in the input's folder, so the
    farm gives it a folder of its own."""
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
    own_folder: Any = None                           # True / False as said; None: not said

    @property
    def reads_folder(self) -> bool:
        """The runner reads the input's whole folder, not the files it is given: the manager
        stages it into a folder of its own. Said in the definition (own_folder), so it is
        known without the runner's loaders; the runner checks it against its loader."""
        return self.own_folder is True



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


class Definition:
    """A definition's head, as the manager knows it (docs/decisions/0020, 0021): its own
    problems, and those agents holding the same fingerprint found in its wiring
    (`reported`). The ComfyUI half (runners/comfyui_workflow.Workflow) adds the wiring."""
    PORT = Port                                 # what an input is; a runner's half adds its loader

    def __init__(self, directory: Path):
        self.dir = directory
        m, head, self.source = read_definition(directory)
        # Which runner runs it, and what that runner's block says (process: environment, command).
        self.runner_type = "process" if "process" in m else "comfyui"
        self.process: dict | None = m.get("process")
        # The head's own fields (process.yaml): kept and checked, acted on from later phases.
        self.version = head.get("version")
        self.job_class: str = head.get("class") or "interactive"
        self.targets: list = head["targets"] if "targets" in head else ["pool"]
        self.limits: dict = head.get("limits") or {}
        self.models: list = head.get("models") or []
        self.models_listed: bool = "models" in head     # models: [] says it uses no weights (decision 0044)
        self.eval: dict | None = head.get("eval")
        self._head = head
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
        self._m = m                             # the definition as read: the runner's half reads its block
        # The graph's file (a comfyui runner block's `template`): hashed into the fingerprint
        # below, read only by the runner (runners/comfyui_workflow.py). A process has none.
        self.template: dict | None = None if self.process is not None else \
            json.loads((directory / m.get("template", "workflow_api.json")).read_text(encoding="utf-8"))
        # The definition's fingerprint: the manifest and the graph as parsed, so a checkout
        # with other line endings has the same one; for a process, the files its command runs.
        # A job names the one it was planned with, and a machine whose copy differs doesn't
        # run it (build_from, the process runner).
        body = self.template if self.process is None else folder_digest(directory)
        self.digest: str = hashlib.sha256(      # a manifest's is what it always was: head-only fields count when set
            json.dumps([m, body, *([head] if head else [])], sort_keys=True, default=str)
            .encode("utf-8")).hexdigest()[:16]
        self.params: dict[str, Param] = {}
        self._own_folder_elsewhere = sorted(n for n, p in (m.get("params") or {}).items()
                                            if "own_folder" in (p or {}) and (p or {}).get("type") not in INPUT_TYPES)
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
                self.ports[pname] = self.PORT(pname, takes=p.get("takes") or "images", loader=p.get("loader") or {},
                                              path_node=p.get("path_node"), work_resolution=p.get("work_resolution"),
                                              max_frames=(p.get("frames") or {}).get("max"), per_run=p.get("per_run"),
                                              primary=bool(p.get("primary", False)), match=p.get("match"),
                                              required=bool(p.get("required", False)),
                                              empty=list(p.get("empty") or []), given=list(p.get("given") or []),
                                              own_folder=p.get("own_folder"))
            elif p.get("type") == "sequence":
                self.ports[pname] = self.PORT(pname, loader=legacy.get("loader") or {},
                                              path_node=legacy.get("path_node"),
                                              required=bool(p.get("required", False)),
                                              own_folder=p.get("own_folder", legacy.get("own_folder")))
        # Inputs larger than this (pixels, longer side) are scaled down to it before the
        # workflow runs, keeping their aspect (convert.resolve_input). None = as they are.
        self.manifest_work_resolution = m.get("work_resolution") or None    # for every input without its own
        self.work_resolution = self.manifest_work_resolution or (self.port.work_resolution if self.port else None)
        self.outputs: dict = m.get("outputs") or {}
        # Kinds of output on the generic route (writes.<kind>, decision 0036), as their handlers read them.
        self.writes: dict = dict(m.get("writes") or {})
        # Texture workflows: several map save nodes, written as EXRs named by the TEX
        # convention (see textures.py) instead of one renamed frame sequence.
        self.textures: T.TextureSpec | None = T.TextureSpec.from_manifest(m["textures"]) if m.get("textures") else None
        self._batch_twice = bool(self.textures and self.textures.batch is not None and self.port and self.port.per_run)
        pr = self.port.per_run if self.port else None
        if self.textures and isinstance(pr, int) and not isinstance(pr, bool) and pr >= 1:
            self.textures.batch = pr                    # textures.batch: the older name
        self.head_problems: list[str] = self.check_head()
        self.wiring_problems: list[str] = []    # the runner's checks of its block (runners/comfyui_workflow.py)
        self.reported: list[str] = []           # what agents holding this fingerprint found (the manager)

    # ------------------------------------------------------------------ checks
    @property
    def problems(self) -> list[str]:
        return sorted(set(self.head_problems + self.wiring_problems + self.reported))

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
    def output_folder_param(self) -> str | None:
        return next((n for n, p in self.params.items() if p.type == "output_folder"), None)

    @property
    def frames_ext(self) -> str | None:
        """What its frames are written as (png, exr): None when it writes no frames."""
        f = self.outputs.get("frames")
        return f.get("ext", "png") if f else None

    @property
    def own_preview(self) -> bool:
        """The run makes its own preview video (outputs.preview), which the agent copies."""
        return bool(self.outputs.get("preview"))

    def collect(self, movie: dict | None = None) -> dict:
        """What the agent collects after a run, with no node in it: the agent's runner finds
        its nodes in its own copy of the definition (pinned by the job's fingerprint)."""
        return {"frames": {"ext": self.frames_ext} if self.frames_ext else None, "preview": self.own_preview,
                "movie": movie}

    @property
    def makes_preview(self) -> bool:
        """The agent makes a preview movie of this workflow's frames: it writes frames, has
        no video node of its own (outputs.preview), and its manifest doesn't say preview: false."""
        return bool(self.outputs.get("frames") and not self.textures and not self.outputs.get("preview")
                    and self.preview_movie is not False)

    def _check_head(self) -> list[str]:
        """The head-only fields of a process.yaml."""
        problems = []
        if self.version is not None and not (isinstance(self.version, int) and not isinstance(self.version, bool)
                                             and self.version >= 1):
            problems.append("version: needs to be a whole number, 1 or more")
        if self.job_class not in JOB_CLASSES:
            problems.append(f"class: needs to be one of {', '.join(JOB_CLASSES)}")
        if not (isinstance(self.targets, list) and self.targets and all(t in TARGETS for t in self.targets)):
            problems.append(f"targets: needs to be a list of {' and/or '.join(TARGETS)}, e.g. [pool]")
        limits = self.limits
        minutes = limits.get("max_minutes", 1) if isinstance(limits, dict) else None
        if not isinstance(limits, dict) or set(limits) - {"max_minutes"} or isinstance(minutes, bool) \
                or not isinstance(minutes, (int, float)) or minutes <= 0:
            problems.append("limits: needs to be a section like {max_minutes: 30}")
        if not (isinstance(self.models, list) and all(
                isinstance(x, dict) and isinstance(x.get("file"), str) and x["file"].strip()
                and isinstance(x.get("licence"), str) and x["licence"].strip() and set(x) <= {"file", "licence"}
                for x in self.models)):
            problems.append("models: needs to be a list of {file: <the weights file>, licence: <its licence>}")
        if self.eval is not None and not isinstance(self.eval, dict):
            problems.append("eval: needs to be a section (the reference job: inputs, params, tolerance)")
        return problems

    def check_head(self) -> list[str]:
        """What is wrong with the definition's head: what any runner shares, checked without
        reading its runner block's wiring or graph."""
        problems = self._check_head()
        problems += [f"param {n}: own_folder is for inputs (type: input)" for n in self._own_folder_elsewhere]
        step = (self.publish or {}).get("step")         # publish: false is {"off": True}: no step
        if self.publish is not None and ("bad" in self.publish or (step is not None and not isinstance(step, str))):
            problems.append("publish: needs to be a section like {step: lay}")
        if not isinstance(self.preview_movie, bool):
            problems.append("preview: needs to be true or false (false: no preview movie of the frames)")
        sg = self.shotgrid
        if not (isinstance(sg, bool) or (isinstance(sg, dict) and set(sg) <= {"submit", "title"}
                                          and isinstance(sg.get("submit", True), bool)
                                          and isinstance(sg.get("title", ""), str))):
            problems.append("shotgrid: needs to be false, or a section like {submit: false, title: <its name in the menu>}")
        for p in self.params.values():
            if p.pattern:
                try:
                    re.compile(p.pattern)
                except re.error as e:
                    problems.append(f"param {p.name}: bad pattern ({e})")
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
            problems += self._check_port_head(port)
        return sorted(set(problems))

    def _where(self, port: Port) -> str:
        legacy = getattr(self.params.get(port.name), "type", None) == "sequence"
        return "sequence" if legacy else f"param {port.name}"

    def _check_port_head(self, port: Port) -> list[str]:
        where = self._where(port)
        problems = []
        if port.takes not in TAKES:
            return [f"{where}: takes must be image or images, not {port.takes!r}"]
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
        if port.own_folder not in (None, True, False):
            problems.append(f"{where}: own_folder is true or false, not {port.own_folder!r}")
        return problems

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

    def request(self, values: dict, seq=None, out_dir: str | None = None,
                frames_prefix: str | None = None, preview_prefix: str | None = None,
                textures: dict | None = None, inputs: dict | None = None, writes: dict | None = None) -> dict:
        """What build() takes, as plain data: the manager stores this in place of the graph,
        and the machine that runs the job builds the graph from it (build_from). `writes`:
        where each kind on the generic route goes and what its files start with (0036)."""
        return {"definition": self.digest, "values": values, "seq": seq.as_dict() if seq is not None else None,
                "out_dir": out_dir, "frames_prefix": frames_prefix, "preview_prefix": preview_prefix,
                "textures": textures,
                "inputs": {k: v.as_dict() if v is not None else None for k, v in (inputs or {}).items()},
                **({"writes": writes} if writes else {})}

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


class DefinitionRegistry:
    """The definitions in a folder, one per subfolder with a process.yaml or a manifest.yaml.
    This one is the manager's: their heads checked here (decision 0020), whatever runs them.
    A runner's registry sets RUNNER and reads only the definitions it runs."""
    KIND = Definition
    RUNNER: str | None = None

    def __init__(self, directory: Path):
        self.directory = directory
        self.workflows: dict[str, Definition] = {}
        self.load_errors: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        self.workflows.clear()
        self.load_errors.clear()
        if not self.directory.is_dir():
            log.warning("workflows folder %s not found", self.directory)
            return
        for d in sorted(self.directory.iterdir()):
            if not (d / "manifest.yaml").exists() and not (d / "process.yaml").exists():
                continue
            if self.RUNNER is not None and runner_type(d) not in (self.RUNNER, None):
                continue                # another runner's; one whose type can't be read reports why
            try:
                wf = self.KIND(d)
                self.workflows[wf.name] = wf
                for p in wf.problems:
                    log.error("workflow %s: %s", wf.name, p)
            except Exception as e:  # noqa: BLE001 - report and keep the others
                self.load_errors[d.name] = str(e)
                log.exception("failed to load workflow %s", d.name)

    def get(self, name: str) -> Definition:
        wf = self.workflows.get(name)
        if wf is None:
            raise ParamError(f"Unknown workflow '{name}'. Available: {', '.join(self.workflows) or 'none'}.")
        if not wf.enabled:
            raise ParamError(f"Workflow '{name}' is disabled: {'; '.join(wf.problems)}")
        return wf
