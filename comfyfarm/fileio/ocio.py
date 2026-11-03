"""The farm's OCIO config, kept in the repo (ocio/config.ocio; config.yaml `ocio.config`).
Its two conversions are a Nuke Write and Read set to "default (color_picking)" in Rodeo's
published show config; the names below are its roles. See ocio/README.md."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # type hints only: fileio never imports pydantic
    from ..config import OcioConfig, PathsConfig

from .errors import SequenceError
from .paths import normalize, to_posix

__all__ = ["REPO", "LINEAR", "IMAGE", "DATA", "NUKE_RAMP", "DEFAULT_CONFIG", "ocio_config_path", "ocio_setup",
           "ocio_roles", "nuke_ramp"]

DEFAULT_CONFIG = "ocio/config.ocio"   # ocio.config's default: the repo's own config


# The farm's OCIO config lives in the repo (ocio/config.ocio; config.yaml `ocio.config`).
# Its two conversions are a Nuke Write and Read set to "default (color_picking)" in Rodeo's
# published show config; the names below are its roles.
REPO = Path(__file__).resolve().parents[2]

LINEAR = "scene_linear"     # what EXRs are in (ACEScg)

IMAGE = "color_picking"     # what ComfyUI's PNGs are in (ACES Rec.709 output transform)

DATA = "data"               # tagged on data maps; their values are never converted

# Linear grey -> 8-bit PNG code through LINEAR -> IMAGE, measured in Nuke 16 with bri3's
# config_1_0_v1 (ocio/README.md). `check` and the tests hold the config to these.
NUKE_RAMP = {0.02: 23, 0.05: 43, 0.18: 99, 0.5: 170, 1.0: 209}


def ocio_config_path(oc: OcioConfig | None) -> str:
    """ocio.config as a full path: relative paths are inside the repo. Built-in configs
    (ocio://...) are passed through."""
    config = oc.config if oc is not None else DEFAULT_CONFIG   # None: ocio.config's default
    if config.startswith("ocio://"):
        return config
    path = Path(config)
    return normalize(str(path if path.is_absolute() else REPO / path))


def ocio_setup(oc: OcioConfig | None, pc: PathsConfig) -> dict:
    """The conversion settings a job is frozen with: the config file and colour spaces."""
    config = ocio_config_path(oc)
    if pc.validate_inputs and not config.startswith("ocio://") and not os.path.isfile(config):
        raise SequenceError(f"No OCIO config at {config} (ocio.config in config.yaml). It comes with the repo: "
                            f"git pull on the farm machine.")
    return {"mode": "colorconvert", "config": config, "from": LINEAR, "to": IMAGE, "linear": LINEAR,
            "image": IMAGE, "data": DATA, "notes": []}


def _config_display(config: str, pc: PathsConfig) -> str:
    """ocio/config.ocio for the repo's own config, else the path as artists see it."""
    if config.startswith("ocio://"):
        return config
    try:
        return Path(config).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return to_posix(config, pc.mappings) or config


def ocio_roles(config: str, names: list[str]) -> dict[str, str | None]:
    """What each name (role or colour space) resolves to in the config, or None if it
    isn't there. Empty if PyOpenColorIO isn't installed."""
    try:
        import PyOpenColorIO as OCIO
    except ImportError:
        return {}
    try:
        cfg = OCIO.Config.CreateFromFile(config)
    except Exception as e:  # noqa: BLE001
        return {"__error__": str(e).splitlines()[0][:200]}
    out = {}
    for n in names:
        cs = cfg.getColorSpace(n)
        out[n] = cs.getName() if cs else None
    return out


def nuke_ramp(config: str) -> dict[float, int] | None:
    """Linear grey -> PNG code through scene_linear -> color_picking in the config, for
    comparing with NUKE_RAMP (measured in Nuke). None if PyOpenColorIO isn't installed."""
    try:
        import PyOpenColorIO as OCIO
    except ImportError:
        return None
    cpu = OCIO.Config.CreateFromFile(config).getProcessor(LINEAR, IMAGE).getDefaultCPUProcessor()
    return {v: round(min(max(cpu.applyRGB([v, v, v])[1], 0.0), 1.0) * 255) for v in NUKE_RAMP}
