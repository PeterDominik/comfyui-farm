"""Jobs as they travel between the manager and its agents.

Each machine sees files under its own paths: show storage as /rdo/shows/... on Linux, or
as a UNC path on a Windows machine (`paths.mappings`); its own scratch folder; its own
checkout of the repo. Between machines, a job's paths go in one portable form:

- show storage as studio paths (/rdo/shows/...),
- the scratch folder as @scratch/...,
- files from the repo (the OCIO config) as @repo/....

portable() turns one machine's view of a job into that form, localize() turns the form
into another machine's view. Every string that starts with one of the prefixes is
rewritten, wherever it is in the job (its info, its prompt, its outputs). Keys ending in
_posix already hold studio paths and are left as they are."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .paths import normalize

SCRATCH = "@scratch"
REPO = "@repo"
_WINDOWS = re.compile(r"^(//|[A-Za-z]:/)")

__all__ = ["SCRATCH", "REPO", "View", "portable", "localize"]


@dataclass
class View:
    """How one machine sees files: path mappings (objects with .posix and .windows, as
    in config.yaml's paths.mappings), its scratch folder and its checkout of the repo."""
    mappings: list = field(default_factory=list)
    scratch: str = ""
    repo: str = ""


def _prefix(path: str) -> str:
    return normalize(path).rstrip("/")


def _swap(s: str, pairs: list[tuple[str, str, bool]]) -> str:
    for frm, to, ci in pairs:
        a, b = (s.lower(), frm.lower()) if ci else (s, frm)
        if a == b or a.startswith(b + "/"):
            return to + s[len(frm):]
    return s


def _walk(obj: Any, pairs: list[tuple[str, str, bool]]) -> Any:
    if isinstance(obj, str):
        return _swap(obj, pairs)
    if isinstance(obj, dict):
        return {k: (v if isinstance(k, str) and k.endswith("_posix") else _walk(v, pairs)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_walk(v, pairs) for v in obj]
    return obj


def _ordered(pairs: list[tuple[str, str, bool]]) -> list[tuple[str, str, bool]]:
    """Longest prefix first: the scratch folder may sit inside the repo."""
    return sorted((p for p in pairs if p[0]), key=lambda p: len(p[0]), reverse=True)


def portable(obj: Any, view: View) -> Any:
    """This machine's paths -> the portable form (a new object; obj is left as it is)."""
    pairs = [(_prefix(m.windows), _prefix(m.posix), True) for m in view.mappings]
    for local, token in ((view.scratch, SCRATCH), (view.repo, REPO)):
        if local:
            local = _prefix(local)
            pairs.append((local, token, bool(_WINDOWS.match(local))))
    return _walk(obj, _ordered(pairs))


def localize(obj: Any, view: View) -> Any:
    """The portable form -> this machine's paths (a new object)."""
    pairs = [(_prefix(m.posix), _prefix(m.windows), False) for m in view.mappings]
    for local, token in ((view.scratch, SCRATCH), (view.repo, REPO)):
        if local:
            pairs.append((token, _prefix(local), False))
    return _walk(obj, _ordered(pairs))
