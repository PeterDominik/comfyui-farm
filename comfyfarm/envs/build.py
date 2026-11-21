"""Building this machine's copy of an environment with uv (docs/decisions/0024).

Under the environments folder (agent.yaml's envs.dir, default data/envs):

    <dir>/<name>/<fingerprint>/     a build: a venv, its farm-env.json, and `ok` once checked
    <dir>/<name>/current            the fingerprint jobs run in
    <dir>/<name>/previous           the one before, kept to go back to
    <dir>/.uv-cache, .uv-python     uv's package cache and the Pythons it downloads
    <dir>/.uv-tool                  uv itself, installed with pip, unless envs.uv names one

A build is made in place and only becomes current once its check passed, so a job never
sees a half-built environment. Builds other than current and previous are removed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .spec import Spec

PYPI = "https://pypi.org/simple"        # the studio's own mirror can hold old versions (CLAUDE.md)
UV = "uv==0.13.9"                       # the version the builder test passed with on the pilot machines
WINDOWS = os.name == "nt"
# Index settings a machine may carry: the environment's pyproject.toml says where each
# package comes from (PyPI, the PyTorch index), so these are left out of every uv call.
UV_INDEX_VARS = ("UV_INDEX", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS",
                 "UV_INDEX_STRATEGY")


class BuildError(RuntimeError):
    """A step that failed, with what it printed last."""


@dataclass
class Builds:
    """What is built of one environment on this machine."""
    name: str
    current: str | None
    previous: str | None
    info: dict                          # fingerprint -> its farm-env.json


def clean_env() -> dict:
    """No pipeline Python settings (rez, PYTHONPATH) in a build or a check: the same rule as
    the start-up scripts and comfyenv.sync (CLAUDE.md: pipeline environment)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    env["PYTHONNOUSERSITE"] = "1"
    return env


def python_of(folder: Path) -> Path:
    return folder / ("Scripts/python.exe" if WINDOWS else "bin/python")


def run(cmd: list, env: dict | None = None, cwd=None, timeout: float = 3600) -> str:
    cmd = [str(c) for c in cmd]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}
    try:
        r = subprocess.run(cmd, env=env or clean_env(), cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, **flags)
    except FileNotFoundError:
        raise BuildError(f"{cmd[0]} isn't installed (or isn't on PATH)") from None
    except subprocess.TimeoutExpired:
        raise BuildError(f"{' '.join(cmd[:4])} … took over {timeout:.0f} s") from None
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise BuildError(f"{' '.join(cmd[:5])} … failed:\n{out.strip()[-1500:]}")
    return out


def uv_env(root: Path, project_env: Path | None = None) -> dict:
    env = {k: v for k, v in clean_env().items() if k not in UV_INDEX_VARS}
    env.update({"UV_CACHE_DIR": str(root / ".uv-cache"), "UV_PYTHON_INSTALL_DIR": str(root / ".uv-python"),
           "UV_PYTHON_PREFERENCE": "only-managed",   # its own Python, not this machine's
           # Packages are linked from the cache, so a second environment with the same torch
           # costs little; uv copies (and says so) where the disk can't link.
           "UV_LINK_MODE": "hardlink"})
    if project_env is not None:
        env["UV_PROJECT_ENVIRONMENT"] = str(project_env)
    return env


def get_uv(root: Path, configured: list[str] | None = None) -> list[str]:
    """uv: as envs.uv names it, or installed with pip into <dir>/.uv-tool (no admin, nothing on PATH)."""
    if configured:
        return list(configured)
    tool = root / ".uv-tool"
    exe = tool / ("Scripts/uv.exe" if WINDOWS else "bin/uv")
    if not exe.exists():
        root.mkdir(parents=True, exist_ok=True)
        base = getattr(sys, "_base_executable", None) or sys.executable
        run([base, "-m", "venv", str(tool)])
        run([str(python_of(tool)), "-m", "pip", "install", "--disable-pip-version-check", "--index-url", PYPI, UV])
    return [str(exe)]


def lock(spec: Spec, uv: list[str], root: Path) -> str:
    """Write the environment's uv.lock: every package pinned, with hashes, for Linux and
    Windows. Needs pypi.org and the PyTorch index; the result is committed."""
    return run([*uv, "lock", "--project", str(spec.folder)], env=uv_env(root))


def builds(root: Path, name: str) -> Builds:
    folder = root / name
    def pointer(which):
        p = folder / which
        if not p.exists():
            return None
        return p.read_text(encoding="utf-8").strip() or None
    info = {}
    if folder.is_dir():
        for b in sorted(folder.iterdir()):
            meta = b / "farm-env.json"
            if b.is_dir() and meta.exists():
                info[b.name] = {**json.loads(meta.read_text(encoding="utf-8")), "ok": (b / "ok").exists()}
    return Builds(name, pointer("current"), pointer("previous"), info)


def current_python(root: Path, name: str) -> Path | None:
    """The Python of the build jobs run in (None: not built here)."""
    b = builds(root, name)
    return python_of(root / name / b.current) if b.current and b.info.get(b.current, {}).get("ok") else None


def _point(folder: Path, which: str, fingerprint: str | None) -> None:
    p = folder / which
    if fingerprint is None:
        p.unlink(missing_ok=True)
        return
    tmp = folder / f".{which}.tmp"
    tmp.write_text(fingerprint + "\n", encoding="utf-8")
    os.replace(tmp, p)                  # whole or not at all, even if the machine stops here


def check(spec: Spec, build: Path, timeout: float = 600) -> str:
    """The spec's check command in the build ("python" is the build's own Python)."""
    cmd = [str(python_of(build)) if c == "python" else c for c in spec.check]
    return run(cmd, timeout=timeout, cwd=build)


def install(spec: Spec, root: Path, uv: list[str], force: bool = False, log=print) -> dict:
    """Build this machine's copy, check it, make it current (the one before becomes previous).
    Returns its farm-env.json. BuildError with what failed; the current build stays."""
    if not spec.locked:
        raise BuildError(f"{spec.name} has no uv.lock: run `python -m comfyfarm env lock {spec.name}` on a machine "
                         f"that reaches pypi.org, and commit environments/{spec.name}/uv.lock")
    if spec.stale:
        raise BuildError(f"{spec.name}'s uv.lock doesn't match its pyproject.toml: {spec.stale}. Run `python -m "
                         f"comfyfarm env lock {spec.name}` on a machine that reaches pypi.org, and commit the lock")
    fp = spec.fingerprint
    folder = root / spec.name
    build = folder / fp
    have = builds(root, spec.name)
    if not force and have.info.get(fp, {}).get("ok"):
        if have.current != fp:
            _point(folder, "previous", have.current)
            _point(folder, "current", fp)
        log(f"{spec.name} {fp} is built and checked: current")
        return have.info[fp]
    shutil.rmtree(build, ignore_errors=True)                # a build that didn't finish, or --force
    folder.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    log(f"{spec.name}: building {fp} with uv (Python {spec.python})")
    try:
        out = run([*uv, "sync", "--frozen", "--no-install-project", "--project", str(spec.folder),
                   "--python", spec.python], env=uv_env(root, build))
        built = time.monotonic() - start
        log(f"{spec.name}: checking it")
        said = check(spec, build)
    except BuildError:
        shutil.rmtree(build, ignore_errors=True)
        raise
    meta = {"name": spec.name, "fingerprint": fp, "python": spec.python, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "build_seconds": round(built, 1), "check": said.strip()[-500:],
            "copied": "falling back to full copy" in out.lower()}   # the disk couldn't hard-link from uv's cache
    (build / "farm-env.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (build / "ok").write_text("", encoding="utf-8")
    if have.current != fp:
        _point(folder, "previous", have.current)
        _point(folder, "current", fp)
    keep = {fp, have.current} if have.current != fp else {fp, have.previous}     # current and previous
    for other in folder.iterdir():
        if other.is_dir() and other.name not in keep:
            shutil.rmtree(other, ignore_errors=True)
            log(f"{spec.name}: removed the older build {other.name}")
    log(f"{spec.name} {fp}: built in {built:.0f} s, checked, current")
    return meta


def remove(root: Path, name: str) -> bool:
    folder = root / name
    if not folder.exists():
        return False
    shutil.rmtree(folder)
    return True
