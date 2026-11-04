"""Finding the command-line tools the farm runs: oiiotool (OpenImageIO, with OpenColorIO),
ffmpeg and ffprobe."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import ConvertConfig

from .errors import SequenceError

__all__ = ["find_tool", "require_tool"]


def _pip_ffmpeg() -> str | None:
    """The ffmpeg binary that ships inside the imageio-ffmpeg wheel (no ffprobe)."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and os.path.isfile(exe) else None
    except Exception:  # noqa: BLE001 - not installed, or no binary for this platform
        return None


def find_tool(name: str, configured: str | list[str] | None = None) -> list[str] | None:
    """Configured command, else the venv next to this Python (pip-installed OpenImageIO
    puts oiiotool there), else ffmpeg from the imageio-ffmpeg package, else PATH."""
    if configured:
        return list(configured) if isinstance(configured, list) else [configured]
    here = Path(sys.executable).parent
    for cand in (here / f"{name}.exe", here / name):
        if cand.is_file():
            return [str(cand)]
    if name == "ffmpeg":
        exe = _pip_ffmpeg()
        if exe:
            return [exe]
    found = shutil.which(name)
    return [found] if found else None


def require_tool(cc: ConvertConfig, name: str) -> list[str]:
    cmd = find_tool(name, cc.tools.get(name))
    if not cmd:
        hint = {"oiiotool": "install the requirements (OpenImageIO) with this machine's setup script in deploy/",
                "ffmpeg": "install the requirements (imageio-ffmpeg) with this machine's setup script in deploy/",
                "ffprobe": "it is optional; set convert.tools.ffprobe to use one"}[name]
        raise SequenceError(f"{name} isn't available on the farm machine: {hint}.")
    return cmd
