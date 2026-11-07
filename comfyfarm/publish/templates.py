"""Publish templates on disk: each workflow's, the global ones, and every version saved.

  <publish.templates>/<workflow>/<name>.nk                    a workflow's own templates
  <publish.templates>/_global/<name>.nk                       for every workflow, after its own
  <publish.templates>/.history/<scope>/<name>/<stamp>_<user>.nk   each version put in place
  <publish.templates>/.history/<scope>/<name>/log.jsonl       who saved or restored which, when

A template is named by its key: `default` (the workflow's own) or `_global/default`. (Global:
what the pipeline calls templates shared between steps. They were "generic" at first, in
_generic/; that folder is moved to _global/ when the manager starts, and old keys still work.)

The manager writes templates (Save as Nuke Publish Template in Nuke, the dashboard's Nuke
Publish Templates tab), so artists don't need write access to the bank. Anyone may save one;
every save and restore keeps a copy of the version it puts in place, and a file found changed
on disk (put there by hand) is kept first, so a bad save can always be put back."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import nuke as N

__all__ = ["GLOBAL", "TemplateStore", "key_of", "split_key", "scope_of", "canonical", "label", "valid_name"]

GLOBAL = "_global"
LEGACY = "_generic"          # what the global templates were called at first
HISTORY = ".history"
LOG = "log.jsonl"
MAX_BYTES = 32 * 2**20
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_USER = re.compile(r"[^A-Za-z0-9_.-]+")


def scope_of(scope: str) -> str:
    """A scope as it's called now (_generic is _global)."""
    return GLOBAL if scope == LEGACY else scope


def key_of(scope: str, name: str) -> str:
    return f"{GLOBAL}/{name}" if scope_of(scope) == GLOBAL else name


def split_key(key: str, workflow: str) -> tuple[str, str]:
    """(scope, name) of a template key, for a workflow's publish."""
    for prefix in (GLOBAL + "/", LEGACY + "/"):
        if key.startswith(prefix):
            return GLOBAL, key[len(prefix):]
    return workflow, key


def canonical(key: str) -> str:
    """A template key as it's called now (_generic/x is _global/x)."""
    return key_of(*split_key(key, "")) if key.startswith(LEGACY + "/") else key


def label(key: str) -> str:
    """How a template shows in the forms: default, or default (global)."""
    scope, name = split_key(key, "")
    return f"{name} (global)" if scope == GLOBAL else key


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name or "")) and not name.lower().endswith(".nk")


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class TemplateStore:
    """The templates folder: listing, checking, saving with history, restoring."""

    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- where
    def folder(self, scope: str) -> Path:
        return self.root / scope

    def path(self, scope: str, name: str) -> Path:
        return self.folder(scope) / f"{name}.nk"

    def _history_dir(self, scope: str, name: str) -> Path:
        return self.root / HISTORY / scope / name

    def names(self, scope: str) -> list[str]:
        """A scope's templates, default first."""
        return N.templates(self.folder(scope))

    def move_legacy(self) -> list[str]:
        """Move the global templates from _generic/ to _global/ (and their history), once:
        only where _global/ doesn't exist yet. What was moved, or why not."""
        done = []
        for old, new in ((self.root / LEGACY, self.root / GLOBAL),
                         (self.root / HISTORY / LEGACY, self.root / HISTORY / GLOBAL)):
            if not old.is_dir():
                continue
            if new.exists():
                done.append(f"{old} is still there: {new} exists too, so it was left for you to merge")
                continue
            try:
                old.rename(new)
                done.append(f"moved {old} to {new}")
            except OSError as e:
                done.append(f"couldn't move {old} to {new} ({e.strerror or e})")
        return done

    # ---------------------------------------------------------------- checking
    def problems(self, scope: str, name: str, inputs) -> list[str]:
        """Why a template can't publish a workflow with these inputs (None: any workflow,
        input Reads aren't checked)."""
        try:
            return N.Template.read(self.path(scope, name)).problems(inputs)
        except OSError as e:
            return [f"it can't be read ({e.strerror or e})"]

    @staticmethod
    def text_problems(text: str, inputs) -> list[str]:
        if not text or not text.strip():
            return ["the script is empty"]
        if len(text.encode("utf-8", "replace")) > MAX_BYTES:
            return [f"the script is larger than {MAX_BYTES // 2**20} MB"]
        t = N.Template(text)
        if not t.nodes:
            return ["it doesn't look like a Nuke script (no nodes)"]
        return t.problems(inputs)

    # ---------------------------------------------------------------- history
    def history(self, scope: str, name: str) -> list[dict]:
        """Every version put in place, latest first: {version, time, user, action, note,
        size, current}. `current`: what's in place now."""
        entries = self._log(scope, name)
        live = self._live_sha(scope, name)
        cur = next((e for e in reversed(entries) if live and e.get("sha1") == live), None)
        return [{**{k: v for k, v in e.items() if k != "sha1"}, "current": e is cur} for e in reversed(entries)]

    def info(self, scope: str, name: str) -> dict:
        """The template's latest save: {saved_by, saved_at, versions, changed_on_disk}."""
        entries = self._log(scope, name)
        live = self._live_sha(scope, name)
        last = entries[-1] if entries else None
        return {"saved_by": last.get("user") if last else None, "saved_at": last.get("time") if last else None,
                "versions": len(entries), "changed_on_disk": bool(live and last and last.get("sha1") != live)}

    def read(self, scope: str, name: str, version: str | None = None) -> bytes:
        """A template's text: the one in place, or a version from its history."""
        if version:
            if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[0-9]{3}_[A-Za-z0-9_.-]+", version):
                raise FileNotFoundError(version)
            return (self._history_dir(scope, name) / f"{version}.nk").read_bytes()
        return self.path(scope, name).read_bytes()

    def _log(self, scope: str, name: str) -> list[dict]:
        out = []
        try:
            lines = (self._history_dir(scope, name) / LOG).read_text(encoding="utf-8").splitlines()
        except OSError:
            return out
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _live_sha(self, scope: str, name: str) -> str | None:
        try:
            return _sha(self.path(scope, name).read_bytes())
        except OSError:
            return None

    def _keep(self, scope: str, name: str, data: bytes, user: str, action: str, note: str = "",
              when: float | None = None) -> dict:
        """A copy of a version into the history, and its log line."""
        hdir = self._history_dir(scope, name)
        hdir.mkdir(parents=True, exist_ok=True)
        when = time.time() if when is None else when
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when)) + f"-{int(when * 1000) % 1000:03d}"
        who = _USER.sub("_", user or "unknown").strip("_") or "unknown"
        version = f"{stamp}_{who}"
        n = 1
        while (hdir / f"{version}.nk").exists():           # two saves in the same millisecond
            n += 1
            version = f"{stamp}_{who}-{n}"
        _write(hdir / f"{version}.nk", data)
        entry = {"version": version, "time": when, "user": user or "unknown", "action": action, "note": note,
                 "size": len(data), "sha1": _sha(data)}
        with open(hdir / LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        try:
            os.chmod(hdir / LOG, 0o644)
        except OSError:
            pass
        return entry

    # ---------------------------------------------------------------- changing
    def save(self, scope: str, name: str, data: bytes, user: str, action: str = "save", note: str = "") -> dict:
        """Put a version in place, keeping it in the history (and whatever was there, if
        the history doesn't have it yet)."""
        with self._lock:
            dest = self.path(scope, name)
            if dest.exists():
                old = dest.read_bytes()
                entries = self._log(scope, name)
                if not entries or entries[-1].get("sha1") != _sha(old):
                    self._keep(scope, name, old, "(on disk)", "found",
                               "there before the history kept it" if not entries else "changed on disk",
                               when=dest.stat().st_mtime)
            entry = self._keep(scope, name, data, user, action, note)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _write(dest, data)
            return {k: v for k, v in entry.items() if k != "sha1"}

    def restore(self, scope: str, name: str, version: str, user: str) -> dict:
        data = self.read(scope, name, version)
        return self.save(scope, name, data, user, action="restore", note=f"restored {version}")


def _write(path: Path, data: bytes) -> None:
    """Written whole (a reader never sees half a script), readable by everyone."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o644)
    except OSError:
        pass
    os.replace(tmp, path)
