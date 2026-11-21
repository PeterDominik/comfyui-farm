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
    stale: str | None = None            # why uv.lock doesn't match pyproject.toml (lock_differs())


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


def _name(n: str) -> str:
    return re.sub(r"[-_.]+", "-", n).lower()        # PEP 503, as uv writes names


def _marker(m: str | None) -> str:
    return re.sub(r"\s+", " ", (m or "").replace('"', "'")).strip()


def _spec(s: str | None) -> tuple:
    return tuple(sorted(p.replace(" ", "") for p in (s or "").split(",") if p.strip()))


REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*([^;]*?)\s*(?:;(.*))?$")


def _wanted(project: dict) -> dict:
    """pyproject.toml's dependencies, keyed by name: (extras, specifier, marker, index URL)."""
    uv = (project.get("tool") or {}).get("uv") or {}
    urls = {i.get("name"): i.get("url") for i in uv.get("index") or []}
    sources = uv.get("sources") or {}
    out = {}
    for d in (project.get("project") or {}).get("dependencies") or []:
        m = REQ.match(d)
        if not m:
            raise SpecError(f"can't read the dependency {d!r} in pyproject.toml")
        name = _name(m.group(1))
        src = next((v for k, v in sources.items() if _name(k) == name), None)
        index = urls.get(src.get("index")) if isinstance(src, dict) else None
        extras = tuple(sorted(_name(e) for e in (m.group(2) or "").split(",") if e.strip()))
        out[name] = (extras, _spec(m.group(3)), _marker(m.group(4)), index)
    return out


def lock_differs(project: dict, lock: dict) -> str | None:
    """Why uv.lock was made for another pyproject.toml, or None. uv records what the project
    asked for when it locked (its own package's requires-dist); a farm build uses the lock
    as it is (uv sync --frozen), so a pyproject.toml changed without locking again would
    build the old packages without a word."""
    name = _name((project.get("project") or {}).get("name") or "")
    me = next((p for p in lock.get("package") or [] if _name(p.get("name", "")) == name
               and (p.get("source") or {}).get("virtual")), None)
    if me is None:
        return f"it has no entry for the project {name or '(no name)'}"
    wanted = (project.get("project") or {}).get("requires-python")
    if wanted and _spec(lock.get("requires-python")) != _spec(wanted):
        return f"it was made for Python {lock.get('requires-python')}, pyproject.toml asks for {wanted}"
    locked = {_name(r["name"]): (tuple(sorted(_name(e) for e in r.get("extras") or [])), _spec(r.get("specifier")),
                                 _marker(r.get("marker")), r.get("index"))
              for r in (me.get("metadata") or {}).get("requires-dist") or []}
    asked = _wanted(project)
    for n in sorted(set(asked) | set(locked)):
        if n not in locked:
            return f"{n} is in pyproject.toml but not in the lock"
        if n not in asked:
            return f"{n} is in the lock but no longer in pyproject.toml"
        if asked[n] != locked[n]:
            a, b = asked[n], locked[n]
            what = "its index" if a[:3] == b[:3] else "its version" if a[1] != b[1] else "its extras or markers"
            return f"{n} changed ({what})"
    return None


def load(name: str, root: Path | None = None) -> Spec:
    """environments/<name> as it is now: a change after this makes another Spec, with its own
    fingerprint."""
    root = root or ENVS
    if not NAME.match(name or ""):
        raise SpecError(f"'{name}' isn't an environment name: lowercase letters, digits, - and _")
    folder = root / name
    if not (folder / "pyproject.toml").exists():
        known = ", ".join(sorted(names(root))) or "none"
        raise SpecError(f"no environment {name} in {folder.parent} (there: {known})")
    return load_dir(name, folder)


def load_dir(name: str, folder: Path) -> Spec:
    """An environment's files in any folder: the repo's, or a release's copy (release.py)."""
    if not NAME.match(name or ""):
        raise SpecError(f"'{name}' isn't an environment name: lowercase letters, digits, - and _")
    if not (folder / "pyproject.toml").exists():
        raise SpecError(f"{folder} has no pyproject.toml")
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
    stale = None
    if (folder / "uv.lock").exists():
        try:
            stale = lock_differs(project, tomllib.loads((folder / "uv.lock").read_text(encoding="utf-8")))
        except tomllib.TOMLDecodeError as e:
            stale = f"it can't be read ({e})"
    return Spec(name=name, folder=folder, python=version,
                dependencies=list((project.get("project") or {}).get("dependencies") or []),
                description=str((meta or {}).get("description") or "").strip(), check=check,
                locked=(folder / "uv.lock").exists(), fingerprint=fingerprint(folder), stale=stale)


def names(root: Path | None = None) -> list[str]:
    root = root or ENVS
    return sorted(p.name for p in root.iterdir() if (p / "pyproject.toml").exists()) if root.is_dir() else []
