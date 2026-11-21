"""A ComfyUI workflow: a definition (comfyfarm.definitions) and the ComfyUI graph its
runner block wires it to. Checking that wiring, checking a machine's ComfyUI against the
graph, and building the graph for a job are here, on the machine that runs the job; the
manager reads only the definition's head (docs/decisions/0020, 0021)."""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass
from typing import Any

from .. import loaders as L
from ..definitions import (GATEWAY_TYPES, HEAD_ONLY, INPUT_TYPES, MAP_RUNNER_KEYS, RUNNER_PARAM_KEYS, RUNNER_SECTIONS,
                           TAKES, TEX_HEAD_KEYS, TEX_RUNNER_KEYS, Definition, DefinitionRegistry, ParamError)
from ..definitions import Port as _Port
from ..fileio.inputs import Sequence

log = logging.getLogger(__name__)


def to_process(m: dict, **head) -> dict:
    """A manifest.yaml's content as a process.yaml's (tools/manifest_to_process.py).
    `head` adds head-only fields (version, class, targets, limits, models, eval)."""
    out: dict = {"id": m.get("name")} if m.get("name") else {}
    out.update({k: v for k, v in m.items() if k not in ("name", "params", "preview", *RUNNER_SECTIONS)})
    out.update({k: v for k, v in head.items() if k in HEAD_ONLY})
    runner: dict = {"type": "comfyui"}
    wiring = {}
    if m.get("params"):
        out["params"] = {}
        for name, spec in m["params"].items():
            spec = spec or {}
            out["params"][name] = {k: v for k, v in spec.items() if k not in RUNNER_PARAM_KEYS}
            ad = L.get((spec.get("loader") or {}).get("class")) if spec.get("type") == "input" else None
            if ad and ad.lists_folder and "own_folder" not in spec:
                out["params"][name]["own_folder"] = True        # said in the head, where the manager reads it
            wired = {k: v for k, v in spec.items() if k in RUNNER_PARAM_KEYS}
            if wired:
                wiring[name] = wired
    for k in ("template", "node_packs", "sequence"):
        if k in m:
            runner[k] = m[k]
    if wiring:
        runner["params"] = wiring
    writes: dict = {}
    outputs = dict(m.get("outputs") or {})
    if "frames" in outputs:
        fr = dict(outputs["frames"] or {})
        writes["frames"] = {"ext": fr.pop("ext")} if "ext" in fr else {}
        outputs["frames"] = fr
    if "preview" in outputs:
        writes["preview"] = "own"
    elif "preview" in m:
        writes["preview"] = m["preview"]
    if outputs:
        runner["outputs"] = outputs
    if m.get("textures"):
        tex = m["textures"]
        writes["textures"] = {**{k: tex[k] for k in TEX_HEAD_KEYS if k in tex and k != "maps"},
                              "maps": [{k: v for k, v in mp.items() if k not in MAP_RUNNER_KEYS}
                                       for mp in tex.get("maps") or []]}
        runner["textures"] = {**{k: tex[k] for k in TEX_RUNNER_KEYS if k in tex and k != "maps"},
                              "maps": {mp["channel"]: {k: mp[k] for k in MAP_RUNNER_KEYS if k in mp}
                                       for mp in tex.get("maps") or []}}
    if writes:
        out["writes"] = writes
    out["runner"] = runner
    return out



# Used to estimate progress when no worker has told us which classes are output nodes.
FALLBACK_OUTPUT_CLASSES = {
    "SaveImage", "PreviewImage", "PreviewAny", "VHS_VideoCombine", "Image Save", "ComfyFarmSaveImage",
    "easy clearCacheAll", "SaveVideo", "SaveAnimatedWEBP",
}



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


@dataclass
class Port(_Port):
    """An input, and the loader node that fills it (loaders.py)."""

    @property
    def adapter(self) -> L.Loader | None:
        return L.get(self.loader.get("class"))


class Workflow(Definition):
    """A definition with its ComfyUI graph: the wiring checked, and the graph built from a
    job's request."""
    PORT = Port

    def __init__(self, directory):
        super().__init__(directory)
        # Node packs it needs that none of its node classes shows, named by their custom_nodes
        # folder: a pack that adds samplers or schedulers to a core node (RES4LYF's res_2s and
        # beta57 for KSampler). They join the packs found from its nodes in the farm's
        # environment file (comfyenv.spec.needs).
        self.node_packs = self._m.get("node_packs") or []
        self.wiring_problems = self.check_wiring()

    @property
    def loader(self) -> L.Loader | None:
        """The adapter for the input's loader node (None if it has none)."""
        return self.port.adapter if self.port else None

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

    def check_wiring(self) -> list[str]:
        """What is wrong with how the runner block wires the head to ComfyUI's graph: the
        same on every machine holding this fingerprint (an agent reports it, 0020)."""
        problems = []
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
            for t in p.targets:
                problems += self._check_target(f"param {p.name}", t.node, t.cls, t.input)
        if self.textures:
            for mp in self.textures.maps:
                for key in (self.textures.path_input, self.textures.prefix_input):
                    problems += self._check_target(f"textures map {mp.channel}", mp.node, mp.cls, key)
        for port in self.ports.values():
            problems += self._check_port_wiring(port)
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

    def static_check(self) -> list[str]:
        """Both: what `python -m comfyfarm check` and CI see."""
        return sorted(set(self.check_head() + self.check_wiring()))

    def _check_port_wiring(self, port: Port) -> list[str]:
        where = self._where(port)
        if port.takes not in TAKES:
            return []                   # the head's check says so
        problems = []
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
        if ad.lists_folder and port.own_folder in (None, False):
            problems.append(f"{where}: its loader {ad.cls} reads every file in the input's folder: say own_folder: "
                            f"true, so the farm gives it a folder of its own")
        if not port.required and port is not self.port and ad.lists_folder:
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
            # turns them into the named EXRs afterwards (textures.write_maps). The plan names
            # maps by channel; which node saves each is this copy's to say.
            nodes = {m.channel: m.node for m in self.textures.maps}
            for mp in textures["maps"]:
                inputs = prompt[nodes[mp["channel"]]]["inputs"]
                inputs[self.textures.path_input] = textures["raw_dir"]
                inputs[self.textures.prefix_input] = mp["prefix"]
        return prompt

    def build_from(self, request: dict) -> dict:
        """The prompt for a stored request. ParamError when this copy of the workflow isn't
        the one the job was planned with."""
        if request.get("definition") != self.digest:
            raise ParamError(f"The workflow {self.name} on this machine ({self.digest}) and the one the manager "
                             f"planned the job with ({request.get('definition')}) differ. Update the farm's code on "
                             f"both; a change that was not committed on either one causes this too.")

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


class WorkflowRegistry(DefinitionRegistry):
    """The ComfyUI workflows in a folder, their wiring checked too: an agent's, and
    `python -m comfyfarm check`'s. Definitions for another runner are left out."""
    KIND = Workflow
    RUNNER = "comfyui"
