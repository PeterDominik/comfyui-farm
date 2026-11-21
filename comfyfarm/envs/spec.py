"""An environment folder of the repo (environments/<name>/), and its fingerprint."""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..fileio.ocio import REPO

ENVS = REPO / "environments"
FILES = ("pyproject.toml", "uv.lock", ".python-version", "env.yaml")     # what a build is made from
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class SpecError(ValueError):
    """An environment folder that can't be used, said so a person can fix it."""


@dataclass
class Spec:
    name: str
    folder: Path
    python: str                         # the version uv installs, from .python-version
    dependencies: list[str]
    description: str = ""
    check: list[str] = field(default_factory=list)    # run in a build; "python" is the build's own
    locked: bool = False                # has its uv.lock
    fingerprint: str = ""               # of its files as they were read (fingerprint())


def fingerprint(folder: Path) -> str:
    """The folder's files' content, line endings aside (a Windows checkout has the same one).
    A build is named by it, so a changed file makes a new build."""
    h = hashlib.sha256()
    for f in FILES:
        path = folder / f
        h.update(f.encode() + b"\0")
        if path.exists():
            h.update(path.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def load(name: str, root: Path | None = None) -> Spec:
    """The folder as it is now: a change after this makes another Spec, with its own fingerprint."""
    root = root or ENVS
    if not NAME.match(name or ""):
        raise SpecError(f"'{name}' isn't an environment name: lowercase letters, digits, - and _")
    folder = root / name
    if not (folder / "pyproject.toml").exists():
        known = ", ".join(sorted(names(root))) or "none"
        raise SpecError(f"no environment {name} in {folder.parent} (there: {known})")
    try:
        project = tomllib.loads((folder / "pyproject.toml").read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SpecError(f"{name}/pyproject.toml: {e}") from None
    version = (folder / ".python-version").read_text(encoding="utf-8").strip() \
        if (folder / ".python-version").exists() else ""
    if not re.fullmatch(r"3\.\d+(\.\d+)?", version):
        raise SpecError(f"{name}/.python-version: needs the Python version, e.g. 3.12")
    if (project.get("tool", {}).get("uv", {}) or {}).get("package", True) is not False:
        raise SpecError(f"{name}/pyproject.toml: needs [tool.uv] package = false (an environment, not a package)")
    meta = yaml.safe_load((folder / "env.yaml").read_text(encoding="utf-8")) if (folder / "env.yaml").exists() else {}
    check = (meta or {}).get("check") or []
    if not (isinstance(check, list) and check and all(isinstance(c, str) for c in check)):
        raise SpecError(f"{name}/env.yaml: needs check: [python, -c, ...], the command that tells a good build")
    return Spec(name=name, folder=folder, python=version,
                dependencies=list((project.get("project") or {}).get("dependencies") or []),
                description=str((meta or {}).get("description") or "").strip(), check=check,
                locked=(folder / "uv.lock").exists(), fingerprint=fingerprint(folder))


def names(root: Path | None = None) -> list[str]:
    root = root or ENVS
    return sorted(p.name for p in root.iterdir() if (p / "pyproject.toml").exists()) if root.is_dir() else []
