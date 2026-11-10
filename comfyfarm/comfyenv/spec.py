"""The farm's ComfyUI: deploy/comfyui/environment.yaml, what every farm machine should run.

It's written from a reference machine (`comfyfarm comfyui snapshot`) and reviewed in git:

  comfyui      version, git commit, the launch flags that change results
  python       major.minor, and packages: torch & co. at their versions (a package that
               exists on one OS only, triton-windows, is pinned per OS)
  driver_min   the NVIDIA driver the pinned torch's CUDA build needs
  libraries    the shared model libraries (a studio path, and how Windows sees it)
  node_packs   the packs that provide a node in a farm workflow, or that its manifest names
               (node_packs: packs that only add samplers or schedulers): commit or registry
               version, a hash of their Python files, whether they live in a library
  models       the model files the farm's workflows name: folder, library, size

deploy/comfyui/extra_model_paths.yaml is the farm's extra_model_paths.yaml (studio paths):
every farm ComfyUI should use it as is.

compare() lists how a machine (machine.collect) differs. A difference never stops a
machine: the dashboard shows it, and `comfyfarm comfyui verify` prints it."""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

import yaml

from ..fileio.ocio import REPO
from . import machine as M

SPEC = REPO / "deploy" / "comfyui" / "environment.yaml"
FARM_MODEL_PATHS = REPO / "deploy" / "comfyui" / "extra_model_paths.yaml"

# The farm's two model libraries (Peter, 2026-11-05). Windows machines see /rdo/bank as B:.
LIBRARIES = [{"path": "/rdo/bank/comfyui", "windows": "B:\\comfyui"},
             {"path": "/rdo/bank/comfy_farm/comfyui", "windows": "B:\\comfy_farm\\comfyui"}]

# The NVIDIA driver a CUDA build of torch needs (major version).
DRIVER_FOR_CUDA = {(11, 8): 520, (12, 1): 530, (12, 4): 550, (12, 6): 560, (12, 8): 570, (12, 9): 575, (13, 0): 580}

HEADER = """\
# The farm's ComfyUI: what every farm machine should run. Written by
#   comfyfarm comfyui snapshot      (on the reference machine; review the diff, commit)
# and checked by `comfyfarm comfyui verify`, by every agent, and on the dashboard's
# Machines tab. A machine that differs keeps taking jobs; the differences are shown.
# Node packs and models: only those the farm's workflows use. See README, "The farm's ComfyUI".
"""


def load(path: str | os.PathLike = SPEC) -> dict | None:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def save(spec: dict, path: str | os.PathLike = SPEC) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(HEADER + "\n" + yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120,
                                                         default_flow_style=None),
                          encoding="utf-8", newline="\n")


class Cached:
    """The environment file, read again when it changes (the manager)."""

    def __init__(self, path: str | os.PathLike = SPEC, model_paths: str | os.PathLike = FARM_MODEL_PATHS):
        self.path, self.model_paths_path = Path(path), Path(model_paths)
        self._key = None
        self.spec: dict | None = None
        self.model_paths: list[dict] = []

    def get(self) -> tuple[dict | None, list[dict]]:
        key = tuple(_mtime(p) for p in (self.path, self.model_paths_path))
        if key != self._key:
            self._key = key
            self.spec = load(self.path)
            self.model_paths = farm_model_paths(self.model_paths_path)
        return self.spec, self.model_paths


def _mtime(p: Path):
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def farm_model_paths(path: str | os.PathLike = FARM_MODEL_PATHS) -> list[dict]:
    """The farm's extra_model_paths.yaml: [{name, library (studio path), is_default,
    given: {folder name: [paths as written]}}]."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    out = []
    for name, conf in data.items():
        if not isinstance(conf, dict):
            continue
        conf = dict(conf)
        base = str(conf.pop("base_path", "") or "").replace("\\", "/").rstrip("/")
        is_default = bool(conf.pop("is_default", False))
        given = {k: [y.strip() for y in str(v or "").split("\n") if y.strip()] for k, v in conf.items()}
        out.append({"name": str(name), "library": base, "is_default": is_default, "given": given})
    return out


# ================================================================ what the farm needs
def needs(registry, object_info: dict) -> dict:
    """The node packs and models the farm's workflows use, from a ComfyUI's node
    definitions: {"packs": {pack: [workflows]}, "models": {file: [workflows]},
    "unknown": [(workflow, class)]} (classes this ComfyUI doesn't have). Packs a manifest
    names in `node_packs` (one adding samplers to a core node shows in no node class)
    count without asking ComfyUI."""
    packs: dict[str, set] = {}
    models: dict[str, set] = {}
    unknown = []
    for name, wf in registry.workflows.items():
        declared = getattr(wf, "node_packs", None)
        for pack in declared if isinstance(declared, list) else []:
            if isinstance(pack, str) and pack.strip():
                packs.setdefault(pack.strip(), set()).add(name)
        for node in wf.template.values():
            ct = node.get("class_type")
            info = object_info.get(ct)
            if info is None:
                unknown.append((name, ct))
            else:
                mod = str(info.get("python_module") or "")
                if mod.startswith("custom_nodes."):
                    pack = mod.split(".", 1)[1]
                    if pack != M.FARM_PACK:
                        packs.setdefault(pack, set()).add(name)
            for val in (node.get("inputs") or {}).values():
                if isinstance(val, str) and val.lower().endswith(M.MODEL_EXT):
                    models.setdefault(val.replace("\\", "/"), set()).add(name)
    return {"packs": {k: sorted(v) for k, v in sorted(packs.items())},
            "models": {k: sorted(v) for k, v in sorted(models.items(), key=lambda kv: kv[0].lower())},
            "unknown": sorted(set(unknown))}


def wanted(spec: dict | None) -> dict:
    """What machine.collect() should read for this spec."""
    spec = spec or {}
    return {"libraries": spec.get("libraries") or LIBRARIES,
            "packs": sorted(spec.get("node_packs") or {}),
            "models": {n: m.get("folder") for n, m in (spec.get("models") or {}).items()},
            "packages": sorted(set(M.KEY_PACKAGES) | set(spec.get("packages") or {}))}


def cuda_of(torch: str | None) -> tuple[int, int] | None:
    m = re.search(r"\+cu(\d+)(\d)$", torch or "")
    return (int(m[1]), int(m[2])) if m else None


def driver_for(torch: str | None) -> int | None:
    cuda = cuda_of(torch)
    if not cuda:
        return None
    known = [v for k, v in sorted(DRIVER_FOR_CUDA.items()) if k <= cuda]
    return known[-1] if known else None


def snapshot(state: dict, need: dict, machine: str, old: dict | None = None) -> tuple[dict, list[str]]:
    """The environment file from the reference machine's state (machine.collect, with
    every pack hashed and the needed models looked up). Pins of another OS's packages and
    the libraries are kept from `old`. Also says what the reference itself lacks."""
    warnings = [f"{wf}: class {cls} isn't installed here" for wf, cls in need.get("unknown") or []]
    old = old or {}
    packs = {}
    for name, wfs in need["packs"].items():
        where = (state.get("packs") or {}).get(name) or []
        if not where:
            warnings.append(f"node pack {name} ({', '.join(wfs)}) isn't in this ComfyUI's custom_nodes folders")
            continue
        if len(where) > 1:
            warnings.append(f"node pack {name} is in {len(where)} places ({', '.join(w['path'] for w in where)}); "
                            f"the first is recorded")
        w = where[0]
        packs[name] = {k: w[k] for k in ("shared", "url", "commit", "version", "registry", "id", "hash")
                       if w.get(k) is not None and (k != "id" or w.get("registry"))}
        packs[name]["for"] = wfs
    models = {}
    for name, wfs in need["models"].items():
        m = (state.get("models") or {}).get(name)
        if not m or m.get("missing"):
            warnings.append(f"model {name} ({', '.join(wfs)}) isn't in this ComfyUI's model folders")
            continue
        if not m.get("library"):
            warnings.append(f"model {name} comes from {m['path']}, outside the farm's libraries: move it into one "
                            f"and snapshot again")
        models[name] = {"folder": m["folder"], "library": m.get("library"), "size": m["size"], "for": wfs}
    have = (state.get("python") or {}).get("packages") or {}
    old_pk = old.get("packages") or {}
    packages = {}
    for n in M.KEY_PACKAGES:
        if n in M.OS_PACKAGES:
            entry = dict(old_pk[n]) if isinstance(old_pk.get(n), dict) else {}
            if M.OS_PACKAGES[n] == state["os"]:
                if have.get(n):
                    entry[state["os"]] = have[n]
                else:
                    entry.pop(state["os"], None)
            if entry:
                packages[n] = entry
        elif have.get(n):
            packages[n] = have[n]
    py = (state.get("python") or {}).get("python")
    core = state.get("comfyui") or {}
    spec = {
        "reference": {"machine": machine, "os": state["os"], "date": datetime.date.today().isoformat()},
        "comfyui": {k: core.get(k) for k in ("version", "commit", "url", "flags") if core.get(k) is not None},
        "python": ".".join(py.split(".")[:2]) if py else None,
        "driver_min": driver_for(have.get("torch")),
        "packages": packages,
        "libraries": old.get("libraries") or LIBRARIES,
        "node_packs": packs,
        "models": models,
    }
    return {k: v for k, v in spec.items() if v is not None}, warnings + list(state.get("notes") or [])


# ================================================================ how a machine differs
def _public(v: str | None) -> str | None:
    return v.split("+", 1)[0] if v else v


def _size(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.2f} {unit}"
        n /= 1024
    return str(n)


def _rev(x: dict) -> str:
    parts = []
    if x.get("version"):
        parts.append(str(x["version"]))
    if x.get("commit"):
        parts.append(str(x["commit"])[:7])
    return " / ".join(parts) or "unknown version"


def excluded(registry, vram_bytes: int | None, tags) -> set[str]:
    """The workflows a machine never gets (their manifest's requires: VRAM, tags), whose
    node packs and models don't matter there. Unknown VRAM excludes nothing."""
    out = set()
    gb = round(vram_bytes / 2**30) if vram_bytes else None
    for name, wf in registry.workflows.items():
        req = getattr(wf, "requires", None) or {}
        if req.get("vram_gb") and gb is not None and gb < req["vram_gb"]:
            out.add(name)
        if set(req.get("tags") or []) - set(tags or []):
            out.add(name)
    return out


def compare(spec: dict | None, state: dict, farm_paths: list[dict] | None = None, skip=None) -> dict:
    """How a machine's ComfyUI differs from the environment file:
    {"differs": [{what, item, message}], "notes": [...]} (notes: what couldn't be checked).
    `skip`: workflows the machine never runs (requires): node packs and models only they
    use don't count there."""
    skip = set(skip or ())

    def unused(entry: dict) -> bool:
        users = set(entry.get("for") or [])
        return bool(users) and users <= skip
    notes = list(state.get("notes") or [])
    if state.get("error"):
        return {"differs": [], "notes": notes + [state["error"]]}
    if not spec:
        return {"differs": [], "notes": notes}
    out: list[dict] = []

    def diff(what: str, item: str, message: str) -> None:
        out.append({"what": what, "item": item, "message": message})

    os_name = state.get("os")
    libraries = spec.get("libraries") or LIBRARIES

    # ComfyUI and how it's started
    have, want = state.get("comfyui") or {}, spec.get("comfyui") or {}
    if (want.get("commit") and have.get("commit") and have["commit"] != want["commit"]) or \
            (want.get("version") and have.get("version") != want["version"] and
             not (want.get("commit") and have.get("commit") == want["commit"])):
        diff("comfyui", "ComfyUI", f"ComfyUI {_rev(have)}; the farm's is {_rev(want)}")
    hf, wf = set(have.get("flags") or []), set(want.get("flags") or [])
    if "flags" in want and hf != wf:
        diff("flags", "launch flags", f"ComfyUI runs with {', '.join(sorted(hf)) or 'no result flags'}; the farm's "
                                      f"with {', '.join(sorted(wf)) or 'none'}")

    # Python and packages
    py = state.get("python") or {}
    if spec.get("python") and py.get("python") and not str(py["python"]).startswith(str(spec["python"]) + "."):
        diff("python", "Python", f"Python {py['python']}; the farm's is {spec['python']}")
    pk = py.get("packages") or {}
    for name, want_v in (spec.get("packages") or {}).items():
        if isinstance(want_v, dict):
            want_v = want_v.get(os_name)
            if not want_v:
                continue
        if name not in pk:
            continue                    # not read (ComfyUI's Python couldn't be run)
        have_v = pk[name]
        same = have_v == want_v if name in M.FULL_VERSION else _public(have_v) == _public(want_v)
        if not same:
            diff("package", name, f"{name} {have_v}; the farm's is {want_v}" if have_v else
                 f"{name} isn't installed; the farm's is {want_v}")
    if spec.get("driver_min"):
        drv = state.get("driver")
        try:
            major = int(str(drv).split(".")[0]) if drv else None
        except ValueError:
            major = None
        if major is None:
            notes.append("couldn't read the NVIDIA driver version (nvidia-smi)")
        elif major < int(spec["driver_min"]):
            diff("driver", "NVIDIA driver", f"NVIDIA driver {drv}; torch {(spec.get('packages') or {}).get('torch')} "
                                            f"needs {spec['driver_min']} or newer")

    # Node packs
    packs = state.get("packs") or {}
    for name, want_p in (spec.get("node_packs") or {}).items():
        if unused(want_p):
            continue
        where = packs.get(name) or []
        users = ", ".join(want_p.get("for") or [])
        if not where:
            diff("pack", name, f"Node pack {name} isn't installed ({users} use it)")
            continue
        if len(where) > 1:
            diff("pack", name, f"Node pack {name} is in {len(where)} places ({', '.join(w['path'] for w in where)}): "
                               f"ComfyUI loads only one of them")
        w = where[0]
        if want_p.get("shared") and not w.get("shared"):
            diff("pack", name, f"Node pack {name} should come from the shared library; here it's {w['path']}")
        if want_p.get("hash") and w.get("hash") and w["hash"] != want_p["hash"]:
            local = _rev(w) == _rev(want_p)         # the same commit or release, edited
            diff("pack", name, f"Node pack {name} has local changes ({w['path']})" if local else
                 f"Node pack {name} is at {_rev(w)}; the farm's is {_rev(want_p)}")

    # Models
    models = state.get("models") or {}
    for name, want_m in (spec.get("models") or {}).items():
        if unused(want_m):
            continue
        m = models.get(name)
        if m is None:
            continue
        if m.get("missing"):
            diff("model", name, f"Model {name} isn't in ComfyUI's {want_m.get('folder') or 'model'} folders")
            continue
        if not m.get("library"):
            diff("model", name, f"Model {name} comes from {m['path']}, not from the farm's libraries")
        elif want_m.get("library") and m["library"] != want_m["library"]:
            diff("model", name, f"Model {name} comes from {m['library']}; the farm's is in {want_m['library']}")
        if m.get("library") and want_m.get("size") is not None and m.get("size") != want_m["size"]:
            diff("model", name, f"Model {name} is {_size(m.get('size'))} here ({m['path']}); the farm's is "
                                f"{_size(want_m['size'])}")

    if farm_paths:
        out += model_paths_differ(state.get("model_paths") or {}, farm_paths, libraries, os_name or M.OS)
    return {"differs": out, "notes": notes}


def _library(path: str, libraries: list[dict]) -> str:
    """The library a base path names, as libraries list it (its studio path), whichever
    form it's written in (/rdo/bank/comfyui/, B:\\comfyui)."""
    def key(p):
        return str(p).replace("\\", "/").rstrip("/").casefold()
    for lib in libraries:
        if any(form and key(form) == key(path) for form in (lib["path"], lib.get("windows"))):
            return lib["path"]
    return path


def model_paths_differ(mp: dict, farm: list[dict], libraries: list[dict], os_name: str = M.OS) -> list[dict]:
    """How a machine's extra_model_paths.yaml differs from the farm's: the same libraries,
    with the same folders. Which library a section points at was found on the machine
    (machine.collect); the manager only compares."""
    out = []

    def diff(message: str) -> None:
        out.append({"what": "model_paths", "item": "extra_model_paths.yaml", "message": message})

    if not mp.get("files"):
        diff("ComfyUI has no extra_model_paths.yaml: it doesn't see the farm's libraries")
        return out
    mine: dict[str, dict] = {}
    for sec in mp.get("sections") or []:
        base = sec.get("base_path")
        lib = sec["library"] if "library" in sec else (M.library_of(base, libraries) if base else None)
        if base and lib is None:
            diff(f"extra_model_paths.yaml: {sec['name']} points at {base}, which isn't one of the farm's libraries")
            continue
        if lib is None:
            continue
        got = mine.setdefault(lib, {"names": [], "is_default": sec["is_default"], "given": {}})
        got["names"].append(sec["name"])
        for k, v in sec["given"].items():
            got["given"].setdefault(k, []).extend(v)
    for f in farm:
        lib = _library(f["library"], libraries)
        got = mine.get(lib)
        if got is None:
            diff(f"extra_model_paths.yaml doesn't list the farm library {lib}")
            continue
        where = f"extra_model_paths.yaml, {lib}"
        if got["is_default"] != f["is_default"]:
            diff(f"{where}: is_default is {str(got['is_default']).lower()}; the farm's is {str(f['is_default']).lower()}")
        for key in sorted(set(f["given"]) | set(got["given"])):
            want = sorted(M.rel(x, os_name) for x in f["given"].get(key, []))
            have = sorted(M.rel(x, os_name) for x in got["given"].get(key, []))
            if want == have:
                continue
            if not have:
                diff(f"{where}: {key} isn't listed; the farm's is {', '.join(f['given'][key])}")
            elif not want:
                diff(f"{where}: {key} is listed ({', '.join(got['given'][key])}); the farm's file doesn't list it")
            else:
                diff(f"{where}: {key} is {', '.join(got['given'][key])}; the farm's is {', '.join(f['given'][key])}")
    return out
