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
        if not cmd:
            print(f"  [WARN] {tool} not found: {use} will be refused (see README, 'Input formats')")
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
