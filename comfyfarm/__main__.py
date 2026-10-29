"""python -m comfyfarm serve | check"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import subprocess
import sys

from . import __version__
from .comfy import ComfyClient
from .config import Config, load_config
from .workflows import WorkflowRegistry


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    log_dir = cfg.resolve(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    fileh = logging.handlers.RotatingFileHandler(log_dir / "comfyfarm.log", maxBytes=5_000_000,
                                                 backupCount=5, encoding="utf-8")
    fileh.setFormatter(fmt)
    root.handlers[:] = [console, fileh]
    logging.getLogger("httpx").setLevel(logging.WARNING)


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
    comparing with C.NUKE_RAMP (measured in Nuke). None if PyOpenColorIO isn't installed."""
    from . import convert as C
    try:
        import PyOpenColorIO as OCIO
    except ImportError:
        return None
    cpu = OCIO.Config.CreateFromFile(config).getProcessor(C.LINEAR, C.IMAGE).getDefaultCPUProcessor()
    return {v: round(min(max(cpu.applyRGB([v, v, v])[1], 0.0), 1.0) * 255) for v in C.NUKE_RAMP}


def check_ocio(cfg: Config) -> int:
    """The repo's OCIO config: its roles, the values Nuke gives (C.NUKE_RAMP), and the exact
    oiiotool commands jobs run with it, on a test image. A missing config or LUT, or a
    config that no longer matches Nuke, shows up here, not in a job."""
    import tempfile

    from . import convert as C
    from . import textures as T
    from .paths import SequenceError
    try:
        ocio = C.ocio_setup(cfg.ocio, cfg.paths)
    except SequenceError as e:
        print(f"  [FAIL] OCIO: {e}")
        return 1
    shown_config = C._config_display(ocio["config"], cfg.paths)
    roles = ocio_roles(ocio["config"], [C.LINEAR, C.IMAGE, C.DATA])
    if roles.get("__error__"):
        print(f"  [FAIL] OCIO config {shown_config}: {roles['__error__']}")
        return 1
    missing = [n for n, v in roles.items() if v is None]
    if missing:
        print(f"  [FAIL] OCIO config {shown_config} has no {', '.join(missing)} role")
        return 1
    shown = ", ".join(f"{n} = {v}" for n, v in roles.items()) if roles else f"{C.LINEAR} -> {C.IMAGE}"
    try:
        ramp = nuke_ramp(ocio["config"])
    except Exception as e:  # noqa: BLE001  (a LUT file missing or unreadable)
        print(f"  [FAIL] OCIO config {shown_config}: {str(e).splitlines()[0][:300]}")
        return 1
    if ramp is not None and ramp != C.NUKE_RAMP:
        off = ", ".join(f"{v}: {ramp[v]} (Nuke {C.NUKE_RAMP[v]})" for v in C.NUKE_RAMP if ramp[v] != C.NUKE_RAMP[v])
        print(f"  [FAIL] OCIO config {shown_config} no longer matches Nuke: linear grey -> PNG code {off}. "
              f"See ocio/README.md")
        return 1
    cmd = C.find_tool("oiiotool", cfg.convert.tools.get("oiiotool"))
    if not cmd:
        print(f"  [WARN] OCIO config {shown_config} ({shown}): oiiotool not found, so EXR inputs and texture "
              f"maps can't be converted (run scripts\\setup.ps1)")
        return 0
    conv = dict(ocio, extra_args=list(cfg.convert.exr.extra_args))
    with tempfile.TemporaryDirectory() as d:
        png, exr = f"{d}/check.png", f"{d}/check.exr"
        steps = [
            ("make a test image", [*cmd, "--create", "4x4", "3", "-d", "uint8", "-o", png]),
            (f"texture colour map {C.IMAGE} -> {C.LINEAR}",
             T.map_command(cmd, ocio, {"color": True, "bits": "half"}, png, exr)),
            (f"texture data map tagged {C.DATA}",
             T.map_command(cmd, ocio, {"color": False, "bits": "float"}, png, f"{d}/data.exr")),
            (f"EXR input {C.LINEAR} -> {C.IMAGE}", C.exr_command(cmd, conv, exr, f"{d}/back.png")),
        ]
        for label, step in steps:
            try:
                out = subprocess.run(step, capture_output=True, text=True, timeout=60)
                ok, text = out.returncode == 0, (out.stderr or out.stdout).strip()
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, text = False, str(e)
            if not ok:
                print(f"  [FAIL] OCIO: {label} with {shown_config}: {text[-300:] or 'failed'}")
                return 1
    matches = "matches Nuke, " if ramp is not None else ""
    print(f"  [ ok ] OCIO config {shown_config} ({shown}): {matches}oiiotool converts {C.LINEAR} <-> {C.IMAGE}")
    return 0


async def run_check(cfg: Config) -> int:
    """Preflight: workflows load, workers reachable, node packs/models present, storage visible."""
    bad = 0
    print(f"comfyfarm {__version__}  config: {cfg.base_dir}")
    for note in cfg.obsolete_keys:
        print(f"  [WARN] config.yaml: {note}")
    reg = WorkflowRegistry(cfg.resolve(cfg.workflows_dir))
    for name, err in reg.load_errors.items():
        print(f"  [FAIL] workflow {name}: {err}")
        bad += 1
    for wf in reg.workflows.values():
        if wf.problems:
            bad += 1
            print(f"  [FAIL] workflow {wf.name}:")
            for p in wf.problems:
                print(f"         - {p}")
        else:
            print(f"  [ ok ] workflow {wf.name} ({len(wf.params)} params)")

    from . import convert as C
    for tool, args, use in (("oiiotool", ["--buildinfo"], "EXR inputs"), ("ffmpeg", ["-version"], "movie inputs"),
                            ("ffprobe", ["-version"], "movie inputs")):
        cmd = C.find_tool(tool, cfg.convert.tools.get(tool))
        if not cmd and tool == "ffprobe":
            print("  [ ok ] ffprobe: not installed; movie info is read with ffmpeg instead")
            continue
        if not cmd:
            print(f"  [WARN] {tool} not found: {use} will be refused (run scripts\\setup.ps1)")
            continue
        try:
            out = subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=30)
            text = (out.stdout or out.stderr).strip()
            first = text.splitlines()[0] if text else f"exit {out.returncode}"
            ocio = ""
            if tool == "oiiotool":
                m = re.search(r"OpenColorIO[^\n,;]*", text)
                ocio = f"  ({m.group(0).strip()})" if m else "  (no OpenColorIO support found!)"
            print(f"  [ ok ] {tool}: {first[:70]}{ocio}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  [FAIL] {tool} ({' '.join(cmd)}): {e}")

    bad += check_ocio(cfg)

    for m in cfg.paths.mappings:
        ok = os.path.isdir(m.windows)
        print(f"  [{' ok ' if ok else 'WARN'}] storage {m.posix} -> {m.windows}"
              + ("" if ok else "  (not reachable from this machine/account)"))

    for w in cfg.workers:
        if not w.enabled:
            continue
        c = ComfyClient(w.url)
        try:
            stats = await c.system_stats()
            dev = (stats.get("devices") or [{}])[0]
            ver = (stats.get("system") or {}).get("comfyui_version", "?")
            print(f"  [ ok ] worker {w.name} {w.url}  ComfyUI {ver}  {dev.get('name', '')}")
            oi = await c.object_info()
            for wf in reg.workflows.values():
                probs = wf.check_object_info(oi)
                if probs:
                    bad += 1
                    print(f"  [FAIL] {w.name} can't run {wf.name}:")
                    for p in probs:
                        print(f"         - {p}")
                else:
                    print(f"  [ ok ] {w.name} has every node, input and model {wf.name} needs")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  [FAIL] worker {w.name} {w.url}: {e}")
        finally:
            await c.close()
    print("All good." if not bad else f"{bad} problem(s).")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="comfyfarm", description="ComfyUI farm gateway")
    ap.add_argument("--config", help="config.yaml path (default: repo root, or $COMFYFARM_CONFIG)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the gateway")
    sub.add_parser("check", help="preflight: workflows, workers, node packs, models, storage")
    args = ap.parse_args(argv)

    # Node titles contain emoji (VHS nodes); a service's redirected stdout on Windows
    # defaults to cp1252 and would choke on them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    cfg = load_config(args.config)
    if args.cmd == "check":
        return asyncio.run(run_check(cfg))

    setup_logging(cfg, args.verbose)
    for note in cfg.obsolete_keys:
        logging.getLogger("comfyfarm").warning("config.yaml: %s", note)
    import uvicorn

    from .api import create_app
    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port,
                log_config=None, ws_ping_interval=20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
