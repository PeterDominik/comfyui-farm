"""What one machine's ComfyUI is, read from its folder, its Python and its HTTP API.

collect() gives a plain JSON-able dict (the agent sends it to the manager, `comfyfarm
comfyui verify` compares it right away):

  comfyui    version, git commit, the launch flags that change results
  python     its version, and the versions of the packages that matter (torch & co.)
  driver     the NVIDIA driver version
  packs      the node packs in every custom_nodes folder ComfyUI loads: where each is,
             its git commit or registry version, and a hash of its Python files
  model_paths  extra_model_paths.yaml as ComfyUI reads it
  models     where ComfyUI finds each model the farm's workflows use, and its size

Model folders are searched the way ComfyUI's folder_paths does: its own models/ folders
first, then extra_model_paths.yaml's in file order (is_default ones first), so a local
copy of a model shadows the shared library's."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

try:
    import tomllib                  # Python 3.11+
except ImportError:                 # 3.10 (Windows agents may run it): read what we need by hand
    tomllib = None

import yaml

from ..protocol import now

OS = "windows" if os.name == "nt" else "linux"

# Launch flags that change what a workflow produces or how fast: attention, precision,
# fast paths. Flags like --port or --listen don't matter.
RESULT_FLAGS = (
    "--use-sage-attention", "--use-flash-attention", "--use-pytorch-cross-attention", "--use-split-cross-attention",
    "--use-quad-cross-attention", "--disable-xformers", "--force-upcast-attention", "--dont-upcast-attention",
    "--fast", "--force-fp16", "--force-fp32", "--fp16-unet", "--bf16-unet", "--fp32-unet", "--fp64-unet",
    "--fp8_e4m3fn-unet", "--fp8_e5m2-unet", "--fp8_e8m0fnu-unet", "--fp16-vae", "--bf16-vae", "--fp32-vae",
    "--cpu-vae", "--fp8_e4m3fn-text-enc", "--fp8_e5m2-text-enc", "--fp16-text-enc", "--bf16-text-enc",
    "--fp32-text-enc", "--deterministic", "--force-channels-last", "--supports-fp8-compute",
)
# Flags followed by their own values (--fast fp16_accumulation fp8_matrix_mult).
FLAGS_WITH_VALUES = ("--fast",)

# Python packages whose version can change results or break nodes. Those not installed on
# the reference machine are left out of the environment file.
KEY_PACKAGES = (
    "torch", "torchvision", "torchaudio", "xformers", "sageattention", "flash-attn", "triton", "triton-windows",
    "transformers", "diffusers", "accelerate", "safetensors", "tokenizers", "sentencepiece", "huggingface-hub",
    "numpy", "scipy", "einops", "kornia", "spandrel", "timm", "av", "opencv-python", "opencv-python-headless",
    "onnxruntime", "onnxruntime-gpu", "bitsandbytes", "pillow", "comfyui-frontend-package",
)
# Packages that exist on one OS only: their pins apply there only.
OS_PACKAGES = {"triton": "linux", "triton-windows": "windows"}
# The whole version is compared (the CUDA build, +cu128, matters); for the others only the
# public version (sageattention 2.2.0 and 2.2.0+cu128torch2.7.1 are the same release).
FULL_VERSION = ("torch", "torchvision", "torchaudio")

MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx", ".pkl", ".pt2")

# ComfyUI's own model folders under <ComfyUI>/models (folder_paths.py), with the older
# names that still count, and the legacy names extra_model_paths.yaml may use.
DEFAULT_FOLDERS = {
    "checkpoints": ["checkpoints"], "configs": ["configs"], "loras": ["loras"], "vae": ["vae"],
    "text_encoders": ["text_encoders", "clip"], "diffusion_models": ["unet", "diffusion_models"],
    "clip_vision": ["clip_vision"], "style_models": ["style_models"], "embeddings": ["embeddings"],
    "diffusers": ["diffusers"], "vae_approx": ["vae_approx"], "controlnet": ["controlnet", "t2i_adapter"],
    "gligen": ["gligen"], "upscale_models": ["upscale_models"], "latent_upscale_models": ["latent_upscale_models"],
    "hypernetworks": ["hypernetworks"], "photomaker": ["photomaker"], "classifiers": ["classifiers"],
    "model_patches": ["model_patches"], "audio_encoders": ["audio_encoders"],
}
LEGACY = {"clip": "text_encoders", "unet": "diffusion_models"}

# The farm's own nodes: versioned with the repo (fileio.farmnodes), not here.
FARM_PACK = "comfyfarm_nodes"


def _quiet() -> dict:
    """No console window for a child process on Windows (the agent runs hidden)."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def norm(p: str) -> str:
    """A path for comparing: absolute, normalized, case-folded on Windows."""
    p = os.path.normpath(os.path.abspath(os.path.expanduser(str(p))))
    return os.path.normcase(p)


def rel(p: str, os_name: str = OS) -> str:
    """A path inside a library for comparing: forward slashes, no trailing slash (and any
    case on Windows)."""
    p = str(p).strip().replace("\\", "/").strip("/")
    return p.casefold() if os_name == "windows" else p


# ================================================================ ComfyUI itself
def comfyui_folder(cfg) -> str:
    """ComfyUI's folder from agent.yaml: comfyui_dir, else the folder of main.py in
    comfyui_launch (a portable install starts ComfyUI/main.py), else its cwd."""
    if getattr(cfg, "comfyui_dir", ""):
        return cfg.comfyui_dir
    launch = cfg.comfyui_launch
    for arg in launch.command[1:]:
        if os.path.basename(arg) == "main.py":
            return os.path.dirname(os.path.join(launch.cwd or ".", arg)) or launch.cwd
    return launch.cwd


def comfyui_python(cfg) -> str:
    """ComfyUI's Python from agent.yaml: comfyui_python, else comfyui_launch's program
    when it's a python."""
    if getattr(cfg, "comfyui_python", ""):
        return cfg.comfyui_python
    cmd = cfg.comfyui_launch.command
    if cmd and "python" in os.path.basename(cmd[0]).lower():
        return cmd[0]
    return ""


def launch_flags(argv: list[str] | None) -> list[str]:
    """The flags among ComfyUI's arguments that change results, in a stable form."""
    out, args = [], list(argv or [])
    i = 0
    while i < len(args):
        a = args[i]
        name = a.split("=", 1)[0]
        if name in RESULT_FLAGS:
            vals = [a.split("=", 1)[1]] if "=" in a else []
            if name in FLAGS_WITH_VALUES:
                while i + 1 < len(args) and not args[i + 1].startswith("-"):
                    i += 1
                    vals.append(args[i])
            out.append(" ".join([name, *sorted(vals)]))
        i += 1
    return sorted(set(out))


def extra_configs(argv: list[str] | None, comfyui_dir: str) -> list[str]:
    """The extra_model_paths files ComfyUI reads: its own, and --extra-model-paths-config."""
    files = [os.path.join(comfyui_dir, "extra_model_paths.yaml")]
    args = list(argv or [])
    for i, a in enumerate(args):
        if a == "--extra-model-paths-config":
            for v in args[i + 1:]:
                if v.startswith("-"):
                    break
                files.append(v if os.path.isabs(v) else os.path.join(comfyui_dir, v))
        elif a.startswith("--extra-model-paths-config="):
            files.append(a.split("=", 1)[1])
    return files


def git_head(folder: str | os.PathLike) -> dict | None:
    """{commit, url} of a git checkout (read from .git: no git needed), or None."""
    folder = Path(folder)
    try:
        g = folder / ".git"
        if g.is_file():                     # a worktree or submodule: "gitdir: <path>"
            m = re.match(r"gitdir:\s*(.+)", g.read_text(errors="replace").strip())
            if not m:
                return None
            g = Path(m[1]) if os.path.isabs(m[1]) else (folder / m[1]).resolve()
        if not (g / "HEAD").is_file():
            return None
        common = g
        if (g / "commondir").is_file():
            common = (g / (g / "commondir").read_text().strip()).resolve()
        head = (g / "HEAD").read_text().strip()
        commit = None
        if head.startswith("ref:"):
            ref = head[4:].strip()
            for base in (g, common):
                if (base / ref).is_file():
                    commit = (base / ref).read_text().strip()
                    break
            else:
                packed = common / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(errors="replace").splitlines():
                        if line.endswith(" " + ref):
                            commit = line.split()[0]
                            break
        else:
            commit = head
        url = None
        if (common / "config").is_file():
            m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', (common / "config").read_text(errors="replace"))
            url = m[1] if m else None
        return {"commit": commit, "url": url} if commit else None
    except OSError:
        return None


def comfyui_core(comfyui_dir: str, stats: dict | None) -> dict:
    version = ((stats or {}).get("system") or {}).get("comfyui_version")
    if not version:
        try:
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", Path(comfyui_dir, "comfyui_version.py").read_text())
            version = m[1] if m else None
        except OSError:
            version = None
    git = git_head(comfyui_dir) or {}
    return {"dir": comfyui_dir, "version": version, "commit": git.get("commit"), "url": git.get("url")}


# ================================================================ Python and driver
_PACKAGES_SCRIPT = r"""
import json, sys
import importlib.metadata as m
out = {"python": "%d.%d.%d" % sys.version_info[:3], "packages": {}}
for n in json.loads(sys.argv[1]):
    try:
        out["packages"][n] = m.version(n)
    except Exception:
        out["packages"][n] = None
print(json.dumps(out))
"""


def python_packages(python: str, names) -> dict:
    """ComfyUI's Python version and these packages' versions (None: not installed), read
    without importing them (fast, and no GPU)."""
    if not python:
        return {"error": "ComfyUI's Python isn't known: set comfyui_python in agent.yaml (or comfyui_launch)"}
    try:
        r = subprocess.run([python, "-I", "-c", _PACKAGES_SCRIPT, json.dumps(sorted(set(names)))],
                           capture_output=True, text=True, timeout=120, **_quiet())
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"couldn't run ComfyUI's Python {python}: {e}"}
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": f"{python} gave no package list: {(r.stderr or r.stdout).strip()[-300:]}"}


def driver_version() -> str | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--query-gpu=driver_version", "--format=csv,noheader"], capture_output=True,
                           text=True, timeout=30, **_quiet())
        lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        return lines[0] if lines else None
    except (OSError, subprocess.SubprocessError):
        return None


# ================================================================ model and node folders
def read_model_paths(files: list[str]) -> dict:
    """extra_model_paths.yaml files as ComfyUI reads them: [{file, name, base_path,
    is_default, folders: {key: [full paths]}, given: {key: [as written]}}]."""
    sections, errors = [], []
    for f in files:
        if not os.path.isfile(f):
            continue
        try:
            data = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            errors.append(f"{f}: {e}")
            continue
        here = os.path.dirname(os.path.abspath(f))
        for name, conf in data.items():
            if not isinstance(conf, dict):
                continue
            conf = dict(conf)
            base = conf.pop("base_path", None)
            if base:
                base = os.path.expandvars(os.path.expanduser(str(base)))
                if not os.path.isabs(base):
                    base = os.path.abspath(os.path.join(here, base))
            is_default = bool(conf.pop("is_default", False))
            folders, given = {}, {}
            for key, val in conf.items():
                parts = [y.strip() for y in str(val or "").split("\n") if y.strip()]
                full = []
                for y in parts:
                    p = os.path.join(base, y) if base else (y if os.path.isabs(y) else os.path.join(here, y))
                    full.append(os.path.normpath(p))
                folders[key], given[key] = full, parts
            sections.append({"file": f, "name": str(name), "base_path": base, "is_default": is_default,
                             "folders": folders, "given": given})
    return {"files": [f for f in files if os.path.isfile(f)], "sections": sections, "errors": errors}


def search_paths(comfyui_dir: str, model_paths: dict) -> dict[str, list[str]]:
    """Every model folder name and where ComfyUI looks for it, in its order."""
    models = os.path.join(comfyui_dir, "models")
    out = {k: [os.path.normpath(os.path.join(models, s)) for s in subs] for k, subs in DEFAULT_FOLDERS.items()}
    out["custom_nodes"] = [os.path.normpath(os.path.join(comfyui_dir, "custom_nodes"))]
    for sec in model_paths.get("sections") or []:
        for key, paths in sec["folders"].items():
            key = LEGACY.get(key, key)
            known = out.setdefault(key, [])
            for p in paths:
                if p in known:
                    if sec["is_default"] and known[0] != p:
                        known.remove(p)
                        known.insert(0, p)
                elif sec["is_default"]:
                    known.insert(0, p)
                else:
                    known.append(p)
    for key, paths in out.items():           # a node pack's own folder (models/LLM): added when it loads
        local = os.path.normpath(os.path.join(models, key))
        if key not in DEFAULT_FOLDERS and key != "custom_nodes" and local not in paths:
            paths.append(local)
    return out


def find_model(name: str, key: str | None, folders: dict[str, list[str]]) -> tuple[str | None, str | None]:
    """(folder name, file) where ComfyUI finds a model: in `key`'s folders, or the first
    folder name that has it when key is None."""
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    for k in ([key] if key else list(folders)):
        for d in folders.get(k) or []:
            f = os.path.join(d, *parts)
            if os.path.isfile(f):
                return k, f
    return None, None


def library_of(path: str, libraries: list[dict]) -> str | None:
    """The farm library (its studio path) a file or folder is in, or None."""
    if not path:
        return None
    p, real = norm(path), norm(os.path.realpath(path))
    for lib in libraries:
        local = lib.get(OS) or lib["path"]
        for root in {norm(local), norm(os.path.realpath(local))}:
            if p == root or p.startswith(root.rstrip(os.sep) + os.sep) or \
                    real == root or real.startswith(root.rstrip(os.sep) + os.sep):
                return lib["path"]
    # The same folder under another name (/shows/bank and /rdo/bank)
    cur = Path(path)
    for parent in [cur, *cur.parents]:
        for lib in libraries:
            try:
                if os.path.samefile(parent, lib.get(OS) or lib["path"]):
                    return lib["path"]
            except OSError:
                continue
    return None


# ================================================================ node packs
def source_hash(path: str | os.PathLike) -> str:
    """A hash of a node pack's Python files (line endings normalized, so a Windows and a
    Linux checkout of the same commit match)."""
    path = Path(path)
    h = hashlib.sha1()
    if path.is_file():
        files = [path]
        root = path.parent
    else:
        files, root = [], path
        for d, dirs, names in os.walk(path):
            dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "__pycache__")
            files += [Path(d, n) for n in names if n.endswith(".py")]
    for f in sorted(files, key=lambda x: x.relative_to(root).as_posix()):
        try:
            data = f.read_bytes().replace(b"\r\n", b"\n")
        except OSError:
            continue
        h.update(f.relative_to(root).as_posix().encode() + b"\0" + data + b"\0")
    return h.hexdigest()[:12]


def pyproject(folder: str | os.PathLike) -> dict:
    """A node pack's pyproject.toml: name, version, repository (registry installs have no git)."""
    try:
        text = Path(folder, "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return {}
    if tomllib is None:
        name, version = (re.search(rf'^{k}\s*=\s*"([^"]*)"', text, re.M) for k in ("name", "version"))
        url = re.search(r'^(?:Repository|repository|Source)\s*=\s*"([^"]*)"', text, re.M)
        return {"name": name and name[1], "version": version and version[1], "url": url and url[1]}
    try:
        data = tomllib.loads(text)
    except ValueError:
        return {}
    project = data.get("project") or {}
    urls = {str(k).lower(): v for k, v in (project.get("urls") or {}).items()}
    return {"name": project.get("name"), "version": project.get("version"),
            "url": urls.get("repository") or urls.get("source") or urls.get("homepage")}


def find_packs(folders: dict[str, list[str]], libraries: list[dict]) -> dict[str, list[dict]]:
    """Every node pack in the custom_nodes folders ComfyUI loads: {name: [where, ...]}
    (more than one place: ComfyUI loads one of them)."""
    out: dict[str, list[dict]] = {}
    for d in folders.get("custom_nodes") or []:
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            continue
        shared = library_of(d, libraries) is not None
        for e in entries:
            full = os.path.join(d, e)
            if e.startswith(".") or e == "__pycache__" or e.endswith(".disabled"):
                continue
            if os.path.isdir(full):
                name = e
            elif e.endswith(".py"):
                name = e[:-3]
            else:
                continue
            out.setdefault(name, []).append({"path": full, "shared": shared})
    return out


def pack_info(where: dict, with_hash: bool) -> dict:
    path = where["path"]
    info = {"path": path, "shared": where["shared"]}
    if os.path.isdir(path):
        git = git_head(path)
        if git:
            info["commit"], info["url"] = git["commit"], git["url"]
        pp = pyproject(path)
        if pp.get("version"):
            info["version"] = pp["version"]
        if not info.get("url") and pp.get("url"):
            info["url"] = pp["url"]
        if os.path.isfile(os.path.join(path, ".tracking")):
            info["registry"] = True         # installed from the Comfy registry (no git)
    if with_hash:
        info["hash"] = source_hash(path)
    return info


# ================================================================ everything
def collect(comfyui_dir: str, python: str = "", stats: dict | None = None, argv: list[str] | None = None,
            libraries: list[dict] | None = None, packs: list[str] | None = None,
            models: dict[str, str | None] | None = None, packages=None) -> dict:
    """This machine's ComfyUI. `packs`: the node packs to hash (None: all); `models`:
    {name: folder name or None}; `packages`: the Python packages to read. `stats`: ComfyUI's
    /system_stats when it runs (its version and launch arguments); `argv`: its launch
    command otherwise."""
    libraries = libraries or []
    state: dict = {"os": OS, "collected_at": now(), "notes": []}
    if not comfyui_dir or not os.path.isdir(comfyui_dir):
        state["error"] = (f"ComfyUI's folder {comfyui_dir} doesn't exist" if comfyui_dir else
                          "ComfyUI's folder isn't known: set comfyui_dir in agent.yaml (or comfyui_launch)")
        return state
    system = (stats or {}).get("system") or {}
    args = system.get("argv") or argv or []
    state["comfyui"] = comfyui_core(comfyui_dir, stats)
    state["comfyui"]["flags"] = launch_flags(args)
    py = python_packages(python, list(packages or KEY_PACKAGES))
    if py.get("error"):
        state["notes"].append(py["error"])
        if system.get("pytorch_version"):           # ComfyUI says which torch it runs
            py = {"packages": {"torch": system["pytorch_version"]}}
    state["python"] = py
    state["driver"] = driver_version()
    mp = read_model_paths(extra_configs(args, comfyui_dir))
    state["notes"] += mp.pop("errors")
    for sec in mp["sections"]:              # which library each points at (checked here, on this machine)
        sec["library"] = library_of(sec["base_path"], libraries) if sec["base_path"] else None
    state["model_paths"] = mp
    folders = search_paths(comfyui_dir, mp)
    found = find_packs(folders, libraries)
    state["packs"] = {name: [pack_info(w, with_hash=packs is None or name in packs) for w in where]
                      for name, where in found.items() if name != FARM_PACK}
    state["models"] = {}
    for name, key in (models or {}).items():
        k, f = find_model(name, key, folders)
        if f is None:
            state["models"][name] = {"folder": key, "missing": True,
                                     "searched": folders.get(key, []) if key else None}
            continue
        try:
            size = os.path.getsize(f)
        except OSError:
            size = None
        state["models"][name] = {"folder": k, "path": f, "size": size, "library": library_of(f, libraries)}
    return state
