"""Where outputs go and what they're called: the output root and folder, the folder an
artist picks, and whether a version's files are already there. (Texture map names, by
the TEX convention, are in fileio.textures.)"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import PathsConfig

from .errors import SequenceError
from .paths import (LOADER_EXTENSIONS, _TOKEN_RE, _pattern_regex, _starts_with, check_writable, normalize, render,
                    to_farm, to_posix)

__all__ = ["output_root", "default_output_root_display", "default_output_dir_display", "resolve_output_folder",
           "sequence_exists", "output_dir"]


def output_root(cfg: PathsConfig, show: str | None, seq: str | None = None, shot: str | None = None) -> str:
    if "{show}" in cfg.output_root and not show:
        raise SequenceError(
            "Couldn't work out the show from the input path. Give an Output folder instead."
        )
    return normalize(cfg.output_root.format(show=show or "", seq=seq or "", shot=shot or "")).rstrip("/")


def default_output_root_display(cfg: PathsConfig) -> str:
    """The config default as artists see it, e.g. /rdo/shows/{show}/.../renders."""
    root = normalize(cfg.output_root).rstrip("/")
    return to_posix(root, cfg.mappings) or root


def default_output_dir_display(cfg: PathsConfig, tag: str, template: str | None = None) -> str:
    """e.g. /rdo/shows/{show}/.../renders/{shot}/{shot}_alpha_v###"""
    d = (template or cfg.output_dir).replace("{root}", default_output_root_display(cfg)).replace("{tag}", tag)
    return re.sub(r"\{version[^}]*\}", "###", d)


def resolve_output_folder(raw: str, cfg: PathsConfig, isdir=os.path.isdir) -> str:
    """The folder an artist typed -> farm path. It replaces output_root; the shot
    folder goes inside it. Its parent must exist (catches typos without creating junk
    folder trees on the share); the folder itself is created if needed."""
    path = to_farm(raw, cfg.mappings).rstrip("/")
    if not path:
        raise SequenceError("Output folder is empty.")
    if _TOKEN_RE.search(path.rpartition("/")[2]) or re.search(r"\s\d+\s*-\s*\d+$", path) \
            or os.path.splitext(path)[1].lower() in LOADER_EXTENSIONS | {".exr", ".mov", ".mp4"}:
        raise SequenceError("Output folder should be a folder, not a file, frame pattern or range.")
    if ".." in path.split("/"):
        raise SequenceError("Output folder can't contain '..'.")
    if not (path.startswith("//") or re.match(r"^[A-Za-z]:/", path) or path.startswith("/")):
        raise SequenceError("Output folder must be a full path.")
    allowed = [to_farm(a, cfg.mappings).rstrip("/") for a in cfg.allowed_output_roots] \
        or [normalize(m.windows).rstrip("/") for m in cfg.mappings]
    if allowed and not any(_starts_with(path, a, case_insensitive=True) for a in allowed):
        shown = ", ".join(to_posix(a, cfg.mappings) or a for a in allowed)
        raise SequenceError(f"Output folder must be under {shown}.")
    check_writable(path, cfg)
    parent = path.rpartition("/")[0]
    if cfg.validate_inputs and parent and not isdir(parent):
        shown = to_posix(parent, cfg.mappings) or parent
        raise SequenceError(f"{shown} doesn't exist, or the farm machine can't see it. "
                            f"Create it first (only the last folder is created for you).")
    return path


def sequence_exists(directory: str, pattern: str, listdir=os.listdir) -> bool:
    """Any file of this frame pattern (e.g. 'x.alpha_v002.%04d.png') already in the folder?"""
    try:
        names = listdir(directory)
    except OSError:   # folder doesn't exist yet
        return False
    rx = _pattern_regex(pattern)
    return any(rx.match(n) for n in names) if rx else pattern in names


def output_dir(template: str, **tokens) -> str:
    return normalize(render(template, "output folder", **tokens)).rstrip("/")
