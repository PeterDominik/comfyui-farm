"""Workflow templates (ComfyUI API JSON) plus manifests describing what is exposed."""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# Used to estimate progress when no worker has told us which classes are output nodes.
FALLBACK_OUTPUT_CLASSES = {
    "SaveImage", "PreviewImage", "PreviewAny", "VHS_VideoCombine", "Image Save",
    "easy clearCacheAll", "SaveVideo", "SaveAnimatedWEBP",
}


class ParamError(ValueError):
    """Bad job parameter; the message is shown to the artist."""


# Param types the gateway wires in itself (no `targets` in the manifest):
#   sequence       -> input image sequence: path mapping, frame range, loader skip/cap
#   output_folder  -> where results go; the gateway still adds <shot>/<shot>_<tag>_vNNN
GATEWAY_TYPES = ("sequence", "output_folder")


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

    def schema(self) -> dict:
        d = {"name": self.name, "type": self.type, "label": self.label or self.name,
             "required": self.required, "default": self.default, "help": self.help}
        for k in ("min", "max", "multiple_of", "choices"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        return d

    def coerce(self, value: Any) -> Any:
        n = self.label or self.name
        if self.type in ("text", "string", "sequence", "output_folder"):
            if not isinstance(value, str):
                raise ParamError(f"{n} must be text.")
            if len(value) > 20000:
                raise ParamError(f"{n} is too long.")
            return value
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


class Workflow:
    def __init__(self, directory: Path):
        self.dir = directory
        m = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8")) or {}
        self.name: str = m.get("name") or directory.name
        self.label: str = m.get("label") or self.name
        self.description: str = (m.get("description") or "").strip()
        self.tag: str = m.get("tag") or self.name
        self.template: dict = json.loads((directory / m.get("template", "workflow_api.json")).read_text(encoding="utf-8"))
        self.params: dict[str, Param] = {}
        for pname, p in (m.get("params") or {}).items():
            self.params[pname] = Param(
                name=pname, type=p.get("type", "string"), label=p.get("label", ""),
                default=p.get("default"), required=bool(p.get("required", False)),
                help=(p.get("help") or "").strip(), min=p.get("min"), max=p.get("max"),
                multiple_of=p.get("multiple_of"), choices=p.get("choices"),
                targets=[_target(t) for t in p.get("targets", [])],
            )
        self.sequence: dict = m.get("sequence") or {}
        self.outputs: dict = m.get("outputs") or {}
        self.problems: list[str] = self.static_check()

    # ------------------------------------------------------------------ checks
    @property
    def enabled(self) -> bool:
        return not self.problems

    @property
    def sequence_param(self) -> str | None:
        return next((n for n, p in self.params.items() if p.type == "sequence"), None)

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

    def static_check(self) -> list[str]:
        problems = []
        for p in self.params.values():
            if p.type in GATEWAY_TYPES:
                if p.targets:
                    problems.append(f"param {p.name}: type {p.type} is wired in by the gateway; "
                                    f"remove its targets")
            elif not p.targets:
                problems.append(f"param {p.name}: no targets")
            for t in p.targets:
                problems += self._check_target(f"param {p.name}", t.node, t.cls, t.input)
        for t in GATEWAY_TYPES:
            names = [n for n, p in self.params.items() if p.type == t]
            if len(names) > 1:
                problems.append(f"only one param may have type {t} (found {', '.join(names)})")
        if self.output_folder_param and not self.outputs.get("frames"):
            problems.append(f"param {self.output_folder_param}: type output_folder needs outputs.frames")
        if self.sequence_param:
            pn = self.sequence.get("path_node")
            if pn:
                problems += self._check_target("sequence.path_node", str(pn["node"]), pn.get("class"), pn.get("input"))
            ld = self.sequence.get("loader")
            if not ld:
                problems.append("sequence.loader missing")
            else:
                for inp in ("directory", "skip_first_images", "image_load_cap"):
                    problems += self._check_target("sequence.loader", str(ld["node"]), ld.get("class"), inp)
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

    def check_object_info(self, object_info: dict) -> list[str]:
        """Compare the template with what a worker actually has installed: missing node
        packs, inputs a node-pack update renamed, and model files that aren't there."""
        problems = []
        for nid, node in self.template.items():
            ct = node.get("class_type")
            info = object_info.get(ct)
            label = f"node {nid} '{self.title(nid)}'"
            if info is None:
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
                        problems.append(f"{label}: '{val}' is not available for {key} (missing model/file?)")
        return problems

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
              frames_prefix: str | None = None, preview_prefix: str | None = None) -> dict:
        prompt = copy.deepcopy(self.template)
        for name, p in self.params.items():
            if p.type == "sequence" or name not in values:
                continue
            for t in p.targets:
                prompt[t.node]["inputs"][t.input] = values[name]
        if seq is not None and self.sequence:
            pn = self.sequence.get("path_node")
            if pn:
                rng = f" {seq.first}-{seq.last}" if seq.first is not None else ""
                prompt[str(pn["node"])]["inputs"][pn["input"]] = seq.farm_path + rng
            ld = prompt[str(self.sequence["loader"]["node"])]["inputs"]
            ld["directory"] = seq.directory
            ld["skip_first_images"] = seq.skip
            ld["image_load_cap"] = seq.cap
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
        return prompt

    def schema(self) -> dict:
        return {
            "name": self.name, "label": self.label, "description": self.description,
            "tag": self.tag, "enabled": self.enabled, "problems": self.problems,
            "params": [p.schema() for p in self.params.values()],
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
