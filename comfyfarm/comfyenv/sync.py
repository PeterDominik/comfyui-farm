"""comfyfarm comfyui sync: bring this machine's ComfyUI to the farm's
(deploy/comfyui/environment.yaml).

It fixes what `verify` lists that it can fix safely, and says how to fix the rest:

  ComfyUI      a git checkout is fetched and checked out at the file's commit (refused
               while it has local changes, unless --force)
  node packs   git ones checked out at their commit, or cloned (a copy that isn't a git
               checkout is replaced by one); registry ones installed at their version from
               the Comfy registry; a local copy shadowing a shared pack, or a second copy,
               moved aside. Folders taken out go to <ComfyUI>/custom_nodes.old/
  packages     one `pip install` of ComfyUI's and the changed packs' requirements and the
               file's pins, with torch & co. (and the attention packages built for the GPU)
               held at the versions this machine has
  extra_model_paths.yaml  the farm's (deploy/comfyui/extra_model_paths.yaml), with the
               libraries as this OS sees them; the old one kept as
               extra_model_paths.yaml.<date>.bak

Never done, only explained: torch, torchvision, torchaudio and the compiled packages
(the command to run), the NVIDIA driver, launch flags (agent.yaml), Python's version,
models (they live in the libraries), and the shared packs in the farm library (every
machine's: changing them there changes them everywhere).

plan() only works out the steps; comfyenv.cli runs them (--apply) on a drained machine,
with ComfyUI stopped and the agent kept from starting it (agent.launch's hold file)."""

from __future__ import annotations

import datetime
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Callable

from . import machine as M
from . import spec as S

REGISTRY = "https://api.comfy.org"
PYPI = "https://pypi.org/simple"       # the studio's own package mirror can hold old versions
# Built for each machine's GPU and torch: kept as they are, installed by hand.
COMPILED = ("xformers", "sageattention", "flash-attn", "triton", "triton-windows")
HELD = tuple(M.FULL_VERSION) + COMPILED


class SyncError(Exception):
    """A step that couldn't be done, written for the person running sync."""


# ================================================================ commands
def clean_env() -> dict:
    """The environment for ComfyUI's Python and pip: no pipeline Python settings (rez)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def run(cmd: list, cwd=None, env=None, timeout: float = 3600) -> str:
    """Run a command and return its output; SyncError (with its last lines) if it fails."""
    cmd = [str(c) for c in cmd]
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, **M._quiet())
    except FileNotFoundError:
        raise SyncError(f"{cmd[0]} isn't installed (or isn't on PATH)") from None
    except subprocess.TimeoutExpired:
        raise SyncError(f"{' '.join(cmd[:4])} … took over {timeout:.0f} s") from None
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise SyncError(f"{' '.join(cmd[:5])} … failed:\n{out.strip()[-1500:]}")
    return out


def git(folder, *args, timeout: float = 900) -> str:
    return run(["git", "-C", folder, *args], timeout=timeout)


def has_commit(folder, commit: str) -> bool:
    try:
        git(folder, "cat-file", "-e", f"{commit}^{{commit}}")
        return True
    except SyncError:
        return False


def checkout(folder, commit: str, url: str | None = None, force: bool = False) -> str:
    """Check a git checkout out at `commit`, fetching it first if needed."""
    dirty = git(folder, "status", "--porcelain", "--untracked-files=no").strip()
    if dirty and not force:
        raise SyncError(f"{folder} has local changes:\n{dirty[:800]}\nCommit or stash them, or run sync with "
                        f"--force to discard them.")
    if not has_commit(folder, commit):
        try:
            git(folder, "fetch", "--quiet", "origin")
        except SyncError:
            if not url:
                raise
        if not has_commit(folder, commit) and url:
            git(folder, "fetch", "--quiet", url, commit)
    if not has_commit(folder, commit):
        raise SyncError(f"{folder}: commit {commit[:7]} isn't in {url or 'its origin'}")
    git(folder, "checkout", "--quiet", "--detach", *(["--force"] if force else []), commit)
    return f"at {commit[:7]}"


def clone(url: str, target: Path, commit: str) -> str:
    run(["git", "clone", "--quiet", url, target], timeout=1800)
    git(target, "checkout", "--quiet", "--detach", commit)
    return f"cloned at {commit[:7]}"


def aside(path, comfyui_dir, stamp: str) -> str:
    """Move a node pack out of custom_nodes (ComfyUI would load it) into custom_nodes.old."""
    dest_dir = Path(comfyui_dir) / "custom_nodes.old"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / f"{Path(path).name}.{stamp}"
    shutil.move(str(path), str(dest))
    return f"moved to {dest}"


# ================================================================ the Comfy registry
def get_json(url: str) -> dict:
    import httpx
    try:
        r = httpx.get(url, timeout=60, follow_redirects=True)
    except httpx.HTTPError as e:
        raise SyncError(f"{url}: {e}") from None
    if r.status_code != 200:
        raise SyncError(f"{url}: HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def download(url: str) -> bytes:
    import httpx
    try:
        r = httpx.get(url, timeout=600, follow_redirects=True)
    except httpx.HTTPError as e:
        raise SyncError(f"{url}: {e}") from None
    if r.status_code != 200:
        raise SyncError(f"{url}: HTTP {r.status_code}")
    return r.content


def registry_install(node_id: str, version: str, target: Path, comfyui_dir, stamp: str) -> str:
    """Install a node pack at a version from the Comfy registry, as ComfyUI-Manager does:
    its zip unpacked into custom_nodes/<id>, with a .tracking file listing the files. The
    folder it replaces is moved aside."""
    info = get_json(f"{REGISTRY}/nodes/{node_id}/install?version={version}")
    url = info.get("downloadUrl")
    if not url:
        raise SyncError(f"the Comfy registry has no download for {node_id} {version}")
    data = download(url)
    new = target.parent / (target.name + ".sync-new")
    if new.exists():
        shutil.rmtree(new)
    files = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            tops = {n.split("/", 1)[0] for n in names}
            strip = (len(tops) == 1 and all("/" in n for n in names) and
                     f"{next(iter(tops))}/pyproject.toml" in names)        # one folder holding it all
            for n in names:
                rel = n.split("/", 1)[1] if strip else n
                parts = [p for p in rel.split("/") if p]
                if not parts or rel.startswith("/") or ".." in parts or ":" in parts[0]:
                    raise SyncError(f"{node_id} {version}: the zip has a path outside its folder ({n})")
                out = new.joinpath(*parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(z.read(n))
                files.append("/".join(parts))
    except zipfile.BadZipFile:
        raise SyncError(f"{node_id} {version}: the registry's download isn't a zip") from None
    (new / ".tracking").write_text("\n".join(files) + "\n", encoding="utf-8")
    moved = ""
    if target.exists() or target.is_symlink():
        moved = "; the old one " + aside(target, comfyui_dir, stamp)
    new.rename(target)
    return f"{version} from the Comfy registry{moved}"


# ================================================================ extra_model_paths.yaml
MODEL_PATHS_HEADER = """\
# Written by `comfyfarm comfyui sync` from the farm's deploy/comfyui/extra_model_paths.yaml
# ({when}), with its libraries as this machine sees them. Change that file in the repo and
# sync again, rather than this one.
"""


def model_paths_text(farm_file, libraries: list[dict], os_name: str, when: str) -> str:
    """The farm's extra_model_paths.yaml as this OS sees the libraries (B:\\ on Windows)."""
    text = Path(farm_file).read_text(encoding="utf-8")

    def local(m: re.Match) -> str:
        lib = S._library(m.group(2).strip("'\""), libraries)
        entry = next((x for x in libraries if x["path"] == lib), None)
        if entry is None:
            return m.group(0)
        path = (entry.get(os_name) or entry["path"]).rstrip("\\/")
        return f"{m.group(1)}'{path}\\'" if os_name == "windows" else f"{m.group(1)}{path}/"
    return MODEL_PATHS_HEADER.format(when=when) + re.sub(r"^(\s*base_path:\s*)(\S+)\s*$", local, text, flags=re.M)


def write_model_paths(comfyui_dir, farm_file, libraries: list[dict], os_name: str, stamp: str) -> str:
    target = Path(comfyui_dir) / "extra_model_paths.yaml"
    text = model_paths_text(farm_file, libraries, os_name, datetime.date.today().isoformat())
    kept = ""
    if target.exists():
        backup = target.with_name(f"extra_model_paths.yaml.{stamp}.bak")
        shutil.copy2(target, backup)
        kept = f"; the old one kept as {backup.name}"
    target.write_text(text, encoding="utf-8", newline="\n")
    return "written" + kept


# ================================================================ pip
def pip_install(python: str, reqs: list[Path], pins: list[str], held: dict[str, str], index_url: str | None) -> str:
    """One pip install of the requirement files and pins, with `held` (torch & co.) kept at
    their versions: pip fails rather than replace them."""
    files = [r for r in reqs if Path(r).is_file()]      # a pack without requirements has none
    if not files and not pins:
        return "nothing to install"
    with tempfile.TemporaryDirectory() as d:
        cons = Path(d, "constraints.txt")
        cons.write_text("".join(f"{k}=={v}\n" for k, v in sorted(held.items())), encoding="utf-8")
        cmd = [python, "-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary", "-c", cons]
        if index_url:
            cmd += ["--index-url", index_url]
        for r in files:
            cmd += ["-r", r]
        cmd += pins
        out = run(cmd, env=clean_env(), timeout=3600)
    done = [line for line in out.splitlines() if line.startswith("Successfully installed")]
    return done[-1] if done else "already as pinned"


def torch_command(python: str, packages: dict, os_name: str) -> str:
    """The pip command that puts the farm's torch, torchvision and torchaudio on a machine."""
    pins = []
    for n in M.FULL_VERSION:
        v = packages.get(n)
        v = v.get(os_name) if isinstance(v, dict) else v
        if v:
            pins.append(f"{n}=={v}")
    cuda = S.cuda_of(next((p.split("==")[1] for p in pins if p.startswith("torch==")), None))
    index = f" --index-url https://download.pytorch.org/whl/cu{cuda[0]}{cuda[1]}" if cuda else ""
    return f"{python or 'python'} -m pip install {' '.join(pins)}{index}"


# ================================================================ the plan
@dataclass
class Step:
    title: str
    run: Callable[[], str] | None = None
    detail: list[str] = field(default_factory=list)


@dataclass
class Plan:
    steps: list[Step]
    manual: list[str]           # what sync doesn't do: how to do it by hand
    differs: list[dict]         # what verify lists


def _same(name: str, have: str | None, want: str) -> bool:
    if name in M.FULL_VERSION:
        return have == want
    return bool(have) and S._public(have) == S._public(want)


def plan(spec: dict, state: dict, farm_paths: list[dict], skip=(), *, comfyui_dir: str, python: str,
         force: bool = False, index_url: str | None = PYPI, stamp: str | None = None,
         farm_file=None) -> Plan:
    """What sync would do on this machine, from its state (machine.collect) and how it
    differs from the file (spec.compare)."""
    stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    farm_file = farm_file or S.FARM_MODEL_PATHS
    skip = set(skip or ())
    differs = S.compare(spec, state, farm_paths, skip)["differs"]
    kinds = {d["what"] for d in differs}
    os_name = state.get("os") or M.OS
    libraries = spec.get("libraries") or S.LIBRARIES
    nodes_dir = Path(comfyui_dir) / "custom_nodes"
    steps: list[Step] = []
    manual: list[str] = []
    reqs: list[Path] = []

    # ComfyUI and how it runs
    have, want = state.get("comfyui") or {}, spec.get("comfyui") or {}
    if "comfyui" in kinds:
        if want.get("commit") and have.get("commit"):
            steps.append(Step(f"ComfyUI {S._rev(have)} → {S._rev(want)} (git checkout)",
                              partial(checkout, comfyui_dir, want["commit"], want.get("url"), force)))
            reqs.append(Path(comfyui_dir) / "requirements.txt")
        else:
            manual.append(f"ComfyUI {S._rev(have)} isn't a git checkout here: install ComfyUI {S._rev(want)} "
                          f"({want.get('url') or 'github.com/Comfy-Org/ComfyUI'}) by hand")
    if "flags" in kinds:
        manual.append(f"Launch flags: start ComfyUI with {', '.join(want.get('flags') or []) or 'none of the result flags'} "
                      f"(comfyui_launch.command in agent.yaml); it runs with "
                      f"{', '.join(have.get('flags') or []) or 'none'}")
    py = (state.get("python") or {}).get("python")
    if "python" in kinds:
        manual.append(f"Python: ComfyUI's is {py}, the farm's {spec.get('python')}. That needs a new environment for "
                      f"ComfyUI; sync doesn't change Python")
    if "driver" in kinds:
        manual.append(f"NVIDIA driver {state.get('driver')}: the farm's torch needs {spec.get('driver_min')} or newer. "
                      f"Update the driver")

    # Node packs
    packs = state.get("packs") or {}
    for name, want_p in (spec.get("node_packs") or {}).items():
        users = set(want_p.get("for") or [])
        if users and users <= skip:
            continue                        # only for workflows this machine never gets
        where = packs.get(name) or []
        local = [w for w in where if not w.get("shared")]
        shared = [w for w in where if w.get("shared")]
        if want_p.get("shared"):
            for w in local:
                steps.append(Step(f"{name}: move the local copy {w['path']} aside (the farm library's is the farm's)",
                                  partial(aside, w["path"], comfyui_dir, stamp)))
            if not shared and "model_paths" not in kinds:
                manual.append(f"Node pack {name} should come from the farm library's custom_nodes, and ComfyUI "
                              f"doesn't find it there")
            elif shared and want_p.get("hash") and shared[0].get("hash") and shared[0]["hash"] != want_p["hash"]:
                manual.append(f"The shared node pack {name} ({shared[0]['path']}) isn't the file's ({S._rev(want_p)}). "
                              f"It's every machine's: update it in the library by hand, or snapshot again if it's "
                              f"the farm's now")
            continue
        first, extra = (local[0], local[1:]) if local else (None, [])
        for w in extra:
            steps.append(Step(f"{name}: move the second copy {w['path']} aside",
                              partial(aside, w["path"], comfyui_dir, stamp)))
        if first and (not want_p.get("hash") or first.get("hash") == want_p["hash"]):
            continue
        target = Path(first["path"]) if first and os.path.isdir(first["path"]) else nodes_dir / name
        now = S._rev(first) if first else "not installed"
        if want_p.get("commit") and want_p.get("url"):
            if first and first.get("commit") and os.path.isdir(first["path"]):
                steps.append(Step(f"{name} {now} → {S._rev(want_p)} (git checkout)",
                                  partial(checkout, target, want_p["commit"], want_p["url"], force)))
            else:
                def fresh(old=first and first["path"], url=want_p["url"], commit=want_p["commit"], target=target):
                    moved = f"; the old one {aside(old, comfyui_dir, stamp)}" if old else ""
                    return clone(url, target, commit) + moved
                what = f"{now}, not a git checkout" if first else now
                steps.append(Step(f"{name} ({what}) → {S._rev(want_p)} (git clone)", fresh))
        elif want_p.get("registry") and want_p.get("version"):
            node_id = want_p.get("id") or (first or {}).get("id") or name
            if first and not os.path.isdir(first["path"]):
                target = nodes_dir / name
            steps.append(Step(f"{name} {now} → {want_p['version']} (Comfy registry)",
                              partial(registry_install, node_id, want_p["version"], target, comfyui_dir, stamp)))
        else:
            manual.append(f"Node pack {name}: install {S._rev(want_p)} ({want_p.get('url') or 'no address recorded'}) "
                          f"by hand: the file records neither a git commit nor a registry version")
            continue
        reqs.append(target / "requirements.txt")

    # extra_model_paths.yaml
    if "model_paths" in kinds:
        steps.append(Step("extra_model_paths.yaml: the farm's, with the libraries as this machine sees them "
                          f"(the old one kept as extra_model_paths.yaml.{stamp}.bak)",
                          partial(write_model_paths, comfyui_dir, farm_file, libraries, os_name, stamp)))

    # Python packages
    pk = (state.get("python") or {}).get("packages") or {}
    pins, torch_differs, compiled = [], False, []
    for name, want_v in (spec.get("packages") or {}).items():
        if isinstance(want_v, dict):
            want_v = want_v.get(os_name)
        if not want_v or name not in pk or _same(name, pk.get(name), want_v):
            continue
        if name in M.FULL_VERSION:
            torch_differs = True
        elif name in COMPILED:
            compiled.append(f"{name} {pk.get(name) or 'not installed'} (the farm's {want_v})")
        else:
            pins.append(f"{name}=={want_v}")
    held = {n: pk[n] for n in HELD if pk.get(n)}
    if (pins or reqs) and not python:
        manual.append("ComfyUI's Python isn't known (comfyui_python in agent.yaml), so sync can't install packages")
    elif pins or reqs:
        shown_held = ", ".join(f"{k} {v}" for k, v in held.items() if k in M.FULL_VERSION) or "nothing"
        steps.append(Step(f"pip install{' ' + ' '.join(pins) if pins else ''}"
                          f"{' + the requirements of what changed' if reqs else ''} (holding {shown_held})",
                          partial(pip_install, python, reqs, pins, held, index_url)))
    if not pk and spec.get("packages"):
        manual.append("ComfyUI's Python couldn't be read, so its packages weren't compared")
    if torch_differs:
        manual.append(f"torch: sync keeps it as it is. To install the farm's (with ComfyUI stopped): "
                      f"{torch_command(python, spec.get('packages') or {}, os_name)}")
    if compiled:
        manual.append(f"Built for each GPU and torch, so installed by hand: {', '.join(compiled)}")

    # Models
    for d in differs:
        if d["what"] == "model":
            if "comes from" in d["message"] and "not from the farm's libraries" in d["message"]:
                manual.append(d["message"] + ": delete that copy (the library's is used then), or move it into a library")
            elif "isn't in ComfyUI" in d["message"]:
                manual.append(d["message"] + ": put it in a farm library" +
                              (" (if it's still missing after extra_model_paths.yaml)" if "model_paths" in kinds else ""))
            else:
                manual.append(d["message"] + ": the library's copy isn't the farm's; replace it, or snapshot again")
    return Plan(steps, manual, differs)
