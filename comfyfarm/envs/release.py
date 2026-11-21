"""Releases: an environment as the manager publishes it to the bank, for agents to install
(decision 0028).

    <bank>/environments/<name>/<fingerprint>/
        pyproject.toml, uv.lock, .python-version, env.yaml     copied from the repo as committed
        release.json    {name, fingerprint, python, description, files: {file: sha256},
                         published_at, published_by, commit}

The bank is the farm's shared folder (share.dir, where manager.json is). A release is
written once, to a temporary folder renamed into place, and never changed: the same
fingerprint is the same files. Whoever installs one copies it to the machine first and
checks every file against its hash there, so what is built is what was published."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import spec as S

DIR = "environments"                    # under the bank
META = "release.json"


class ReleaseError(ValueError):
    """A release that can't be published or used, said so a person can act on it."""


@dataclass
class Release:
    name: str
    fingerprint: str
    folder: Path
    meta: dict

    @property
    def published_at(self) -> str:
        return str(self.meta.get("published_at") or "")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def root(bank: Path) -> Path:
    return Path(bank) / DIR


def checkout(repo: Path, folder: Path) -> tuple[str | None, list[str]]:
    """(the commit, the environment's files changed since it). (None, []) without git."""
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    try:
        head = git("rev-parse", "HEAD")
        if head.returncode != 0:
            return None, []
        changed = git("status", "--porcelain", "--", str(folder))
    except (OSError, subprocess.TimeoutExpired):
        return None, []
    return head.stdout.strip(), [ln[3:] for ln in changed.stdout.splitlines() if ln.strip()]


def publish(spec: S.Spec, bank: Path, commit: str | None = None, by: str | None = None) -> tuple[Release, bool]:
    """The release of this environment as it is, in the bank: (it, whether it is new). The
    same fingerprint already there is the same release, checked and left as it is."""
    if not spec.locked:
        raise ReleaseError(f"{spec.name} has no uv.lock: run `python -m comfyfarm env lock {spec.name}` and commit it")
    if spec.stale:
        raise ReleaseError(f"{spec.name}'s uv.lock doesn't match its pyproject.toml: {spec.stale}. Lock it again "
                           f"and commit both")
    dest = root(bank) / spec.name / spec.fingerprint
    if dest.exists():
        return load(dest), False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{spec.fingerprint}.{os.getpid()}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        files = {}
        for f in S.FILES:
            if (spec.folder / f).exists():
                shutil.copyfile(spec.folder / f, tmp / f)
                files[f] = sha(tmp / f)
        meta = {"name": spec.name, "fingerprint": spec.fingerprint, "python": spec.python,
                "description": spec.description, "files": files,
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "published_by": by or f"{getpass.getuser()}@{socket.gethostname()}", "commit": commit}
        (tmp / META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        load(tmp, expect=(spec.name, spec.fingerprint))        # what landed in the bank is what was meant
        try:
            os.rename(tmp, dest)
        except OSError:
            if not dest.exists():
                raise
            # Published by someone else just now: theirs stands.
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return load(dest), True


def load(folder: Path, expect: tuple[str, str] | None = None) -> Release:
    """A release folder, checked: its release.json, every file against its hash, and the
    fingerprint they make against the one it is named by."""
    folder = Path(folder)
    name, fp = expect or (folder.parent.name, folder.name)
    try:
        meta = json.loads((folder / META).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ReleaseError(f"{folder} isn't a release: its {META} can't be read ({e})") from None
    if meta.get("name") != name or meta.get("fingerprint") != fp:
        raise ReleaseError(f"{folder} says it is {meta.get('name')} {meta.get('fingerprint')}, not {name} {fp}")
    files = meta.get("files") or {}
    if "pyproject.toml" not in files or "uv.lock" not in files:
        raise ReleaseError(f"release {name} {fp} lists no pyproject.toml or uv.lock")
    for f, want in files.items():
        if f not in S.FILES:
            raise ReleaseError(f"release {name} {fp} lists {f}, which isn't an environment's file")
        path = folder / f
        if not path.is_file() or sha(path) != want:
            raise ReleaseError(f"release {name} {fp}: {f} doesn't match its hash (changed, or copied badly). Publish "
                               f"it again from the repo")
    extra = sorted(f for f in S.FILES if f not in files and (folder / f).exists())
    if extra:
        raise ReleaseError(f"release {name} {fp} has files its {META} doesn't list: {', '.join(extra)}")
    if S.fingerprint(folder) != fp:
        raise ReleaseError(f"release {name} {fp}: its files make the fingerprint {S.fingerprint(folder)}")
    return Release(name=name, fingerprint=fp, folder=folder, meta=meta)


def listed(bank: Path, name: str | None = None) -> list[dict]:
    """What the bank has, newest first in each environment, from release.json alone (not
    checked: load() is, before anything is built from one)."""
    out = []
    base = root(bank)
    for env in sorted(base.iterdir()) if base.is_dir() else []:
        if not env.is_dir() or (name and env.name != name):
            continue
        rows = []
        for rel in env.iterdir():
            if rel.is_dir() and not rel.name.startswith("."):
                try:
                    meta = json.loads((rel / META).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {"error": f"no readable {META}"}
                rows.append({"name": env.name, "fingerprint": rel.name, **meta})
        out += sorted(rows, key=lambda r: str(r.get("published_at") or ""), reverse=True)
    return out


def find(bank: Path, name: str, which: str = "latest") -> Release:
    """A release by fingerprint, or the latest published, checked."""
    if which == "latest":
        rows = [r for r in listed(bank, name) if "error" not in r]
        if not rows:
            raise ReleaseError(f"the bank ({root(bank)}) has no release of {name}: publish one with "
                               f"`python -m comfyfarm env publish {name}` on the manager's host")
        which = rows[0]["fingerprint"]
    folder = root(bank) / name / which
    if not folder.is_dir():
        raise ReleaseError(f"the bank ({root(bank)}) has no release {name} {which}")
    return load(folder)


def fetch(release: Release, into: Path) -> S.Spec:
    """The release copied to this machine and checked there: what is built is what was
    published, whatever happens to the bank meanwhile."""
    shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True)
    for f in release.meta["files"]:
        shutil.copyfile(release.folder / f, into / f)
    shutil.copyfile(release.folder / META, into / META)
    load(into, expect=(release.name, release.fingerprint))
    spec = S.load_dir(release.name, into)
    if spec.fingerprint != release.fingerprint:     # load() checked it; this is the same, said once more
        raise ReleaseError(f"release {release.name} {release.fingerprint} copied as {spec.fingerprint}")
    return spec
