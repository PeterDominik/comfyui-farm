"""python -m comfyfarm standalone | serve | manager | agent | check | comfyui

  standalone   the manager and an agent for each ComfyUI in config.yaml's workers, in one
               process, as the farm has always run (serve is the same, under its old name)
  manager      the manager only: agents on other machines check in (comfyfarm agent)
  agent        one agent: runs the manager's jobs on this machine's ComfyUI (agent.yaml);
               agent --check checks this worker and exits
  check        preflight: workflows, tools, OCIO, storage, the shared folder, and config.yaml's
               workers
  comfyui      comfyui snapshot: write the farm's ComfyUI environment file from this machine;
               comfyui verify: list how this machine's ComfyUI differs from it (comfyenv)"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .fileio import farmnodes
from .fileio.ocio import REPO, nuke_ramp, ocio_roles
from .logs import json_handler
from .workflows import WorkflowRegistry


def setup_logging(cfg, verbose: bool = False, filename: str = "comfyfarm.log") -> None:
    """Console and a rotating text log; with log_json, the same lines as JSON beside it
    (comfyfarm.jsonl, agent-<name>.jsonl) for a log collector."""
    log_dir = cfg.resolve(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    fileh = logging.handlers.RotatingFileHandler(log_dir / filename, maxBytes=5_000_000,
                                                 backupCount=5, encoding="utf-8")
    fileh.setFormatter(fmt)
    handlers = [console, fileh]
    if getattr(cfg, "log_json", False):
        agent = getattr(cfg, "name", None) if filename.startswith("agent-") else None
        handlers.append(json_handler(log_dir / (Path(filename).stem + ".jsonl"), "agent" if agent else "manager", agent))
    for old in root.handlers:           # a second call (standalone: manager, then agents) must not leak files
        old.close()
    root.handlers[:] = handlers
    logging.getLogger("httpx").setLevel(logging.WARNING)


def check_ocio(cfg: Config) -> int:
    """The repo's OCIO config: its roles, the values Nuke gives (OC.NUKE_RAMP), and the exact
    oiiotool commands jobs run with it, on a test image. A missing config or LUT, or a
    config that no longer matches Nuke, shows up here, not in a job."""
    import tempfile

    from .fileio import convert as C
    from .fileio import ocio as OC
    from .fileio import outputs as O
    from .fileio.errors import SequenceError
    from .fileio.tools import find_tool
    try:
        ocio = OC.ocio_setup(cfg.ocio, cfg.paths)
    except SequenceError as e:
        print(f"  [FAIL] OCIO: {e}")
        return 1
    shown_config = OC._config_display(ocio["config"], cfg.paths)
    roles = ocio_roles(ocio["config"], [OC.LINEAR, OC.IMAGE, OC.DATA])
    if roles.get("__error__"):
        print(f"  [FAIL] OCIO config {shown_config}: {roles['__error__']}")
        return 1
    missing = [n for n, v in roles.items() if v is None]
    if missing:
        print(f"  [FAIL] OCIO config {shown_config} has no {', '.join(missing)} role")
        return 1
    shown = ", ".join(f"{n} = {v}" for n, v in roles.items()) if roles else f"{OC.LINEAR} -> {OC.IMAGE}"
    try:
        ramp = nuke_ramp(ocio["config"])
    except Exception as e:  # noqa: BLE001  (a LUT file missing or unreadable)
        print(f"  [FAIL] OCIO config {shown_config}: {str(e).splitlines()[0][:300]}")
        return 1
    if ramp is not None and ramp != OC.NUKE_RAMP:
        off = ", ".join(f"{v}: {ramp[v]} (Nuke {OC.NUKE_RAMP[v]})" for v in OC.NUKE_RAMP if ramp[v] != OC.NUKE_RAMP[v])
        print(f"  [FAIL] OCIO config {shown_config} no longer matches Nuke: linear grey -> PNG code {off}. "
              f"See ocio/README.md")
        return 1
    cmd = find_tool("oiiotool", cfg.convert.tools.get("oiiotool"))
    if not cmd:
        print(f"  [WARN] OCIO config {shown_config} ({shown}): oiiotool not found, so EXR inputs and texture "
              f"maps can't be converted (install the requirements: this machine's setup script in deploy/)")
        return 0
    conv = dict(ocio, extra_args=list(cfg.convert.exr.extra_args))
    with tempfile.TemporaryDirectory() as d:
        png, exr = f"{d}/check.png", f"{d}/check.exr"
        steps = [
            ("make a test image", [*cmd, "--create", "4x4", "3", "-d", "uint8", "-o", png]),
            (f"texture colour map {OC.IMAGE} -> {OC.LINEAR}",
             O.map_command(cmd, ocio, {"color": True, "bits": "half"}, png, exr)),
            (f"texture data map tagged {OC.DATA}",
             O.map_command(cmd, ocio, {"color": False, "bits": "float"}, png, f"{d}/data.exr")),
            (f"EXR input {OC.LINEAR} -> {OC.IMAGE}", C.exr_command(cmd, conv, exr, f"{d}/back.png")),
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
    print(f"  [ ok ] OCIO config {shown_config} ({shown}): {matches}oiiotool converts {OC.LINEAR} <-> {OC.IMAGE}")
    return 0


def _line(status: str, text: str) -> None:
    print(f"  [{status}] {text}")


def check_workflows(reg: WorkflowRegistry) -> int:
    bad = 0
    for name, err in reg.load_errors.items():
        _line("FAIL", f"workflow {name}: {err}")
        bad += 1
    for wf in reg.workflows.values():
        if wf.problems:
            bad += 1
            _line("FAIL", f"workflow {wf.name}:")
            for p in wf.problems:
                print(f"         - {p}")
        else:
            _line(" ok ", f"workflow {wf.name} ({len(wf.params)} params)")
    return bad


def check_tools(convert) -> int:
    """oiiotool, ffmpeg and ffprobe, as conversions will run them (convert.tools)."""
    from .fileio.tools import find_tool
    bad = 0
    for tool, args, use in (("oiiotool", ["--buildinfo"], "EXR inputs"),
                            ("ffmpeg", ["-version"], "movie inputs and preview movies"),
                            ("ffprobe", ["-version"], "movie inputs")):
        cmd = find_tool(tool, convert.tools.get(tool))
        if not cmd and tool == "ffprobe":
            _line(" ok ", "ffprobe: not installed; movie info is read with ffmpeg instead")
            continue
        if not cmd:
            _line("WARN", f"{tool} not found: {use} will be refused (install the requirements: this machine's setup script in deploy/)")
            continue
        try:
            out = subprocess.run([*cmd, *args], capture_output=True, text=True, timeout=30)
            text = (out.stdout or out.stderr).strip()
            first = text.splitlines()[0] if text else f"exit {out.returncode}"
            ocio = ""
            if tool == "oiiotool":
                m = re.search(r"OpenColorIO[^\n,;]*", text)
                ocio = f"  ({m.group(0).strip()})" if m else "  (no OpenColorIO support found!)"
            if tool == "ffmpeg" and "--enable-libx264" not in text:    # preview movies are H.264
                ocio = "  (no libx264: preview movies will fail)"
            _line(" ok ", f"{tool}: {first[:70]}{ocio}")
        except Exception as e:  # noqa: BLE001
            bad += 1
            _line("FAIL", f"{tool} ({' '.join(cmd)}): {e}")
    return bad


async def check_comfyui(name: str, url: str, reg: WorkflowRegistry) -> int:
    """A ComfyUI answers, and has every node, input and model each workflow needs."""
    from .agent.comfy import ComfyClient   # here, so check_ocio and --help don't need httpx
    bad = 0
    c = ComfyClient(url)
    try:
        stats = await c.system_stats()
        dev = (stats.get("devices") or [{}])[0]
        ver = (stats.get("system") or {}).get("comfyui_version", "?")
        vram = f" ({dev['vram_total'] / 2**30:.0f} GB)" if dev.get("vram_total") else ""
        _line(" ok ", f"worker {name} {url}  ComfyUI {ver}  {dev.get('name', '')}{vram}")
        oi = await c.object_info()
        why = farmnodes.stale(oi, REPO)
        if why:
            bad += 1
            _line("FAIL", f"{name}: {why} Restart ComfyUI (an agent that starts ComfyUI restarts it itself once "
                          f"it holds no jobs).")
        elif farmnodes.loaded(oi):
            _line(" ok ", f"{name} runs this checkout's farm nodes ({farmnodes.version(REPO)})")
        for wf in reg.workflows.values():
            probs = wf.check_object_info(oi)
            if probs:
                bad += 1
                _line("FAIL", f"{name} can't run {wf.name}:")
                for p in probs:
                    print(f"         - {p}")
            else:
                _line(" ok ", f"{name} has every node, input and model {wf.name} needs")
    except Exception as e:  # noqa: BLE001
        bad += 1
        _line("FAIL", f"worker {name} {url}: {e}")
    finally:
        await c.close()
    return bad


def check_folder(label: str, path: str, min_free_gb: float = 0) -> int:
    """A folder this machine writes into: there (or creatable), writable, with room."""
    import shutil
    import tempfile
    try:
        os.makedirs(path, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".comfyfarm-check-"):
            pass
        free = shutil.disk_usage(path).free / 2**30
    except OSError as e:
        _line("FAIL", f"{label} {path}: can't write there: {e.strerror or e}")
        return 1
    if min_free_gb and free < min_free_gb:
        _line("WARN", f"{label} {path}: writable, but only {free:.0f} GB free")
        return 0
    _line(" ok ", f"{label} {path}: writable, {free:.0f} GB free")
    return 0


async def run_check(cfg: Config) -> int:
    """Preflight: workflows load, workers reachable, node packs/models present, storage visible."""
    print(f"comfyfarm {__version__}  config: {cfg.base_dir}")
    for note in cfg.obsolete_keys:
        _line("WARN", f"config.yaml: {note}")
    reg = WorkflowRegistry(cfg.resolve(cfg.workflows_dir))
    bad = check_workflows(reg)
    bad += check_tools(cfg.convert)
    bad += check_ocio(cfg)
    for m in cfg.paths.mappings:
        ok = os.path.isdir(m.windows)
        _line(" ok " if ok else "WARN", f"storage {m.posix} -> {m.windows}"
              + ("" if ok else "  (not reachable from this machine/account)"))
    if cfg.share.dir:
        from .manager.share import manager_url, share_dir
        bad += check_folder("shared folder", share_dir(cfg))
        print(f"         artists and agents will reach the manager at {manager_url(cfg)}")
    for w in cfg.workers:
        if w.enabled:
            bad += await check_comfyui(w.name, w.url, reg)
    print("All good." if not bad else f"{bad} problem(s).")
    return 1 if bad else 0


async def run_agent_check(cfg, path) -> int:
    """`comfyfarm agent --check`: everything this worker needs, before it takes a job."""
    from .agent.link import HttpLink
    from .config import OcioConfig
    from .fileio import paths as P
    from .protocol import LinkError, build
    print(f"comfyfarm agent {cfg.name} ({build()})  config: {path}")
    bad = 0
    link = HttpLink(cfg.manager, cfg.token)
    storage: list[str] = []
    try:
        r = await link.ping({"name": cfg.name})
        storage = list(r.get("storage") or [])
        _line(" ok ", f"manager {link.shown}: reachable, token accepted")
        if r.get("manager") != build():
            _line("WARN", f"the manager runs {r.get('manager')}, this agent {build()}: run the same commit on both")
    except LinkError as e:
        bad += 1
        _line("FAIL", f"manager {cfg.manager}: {e}")
    finally:
        await link.close()
    reg = WorkflowRegistry(Path(__file__).resolve().parent.parent / "workflows")
    bad += await check_comfyui(cfg.name, cfg.comfyui, reg)
    lc = cfg.comfyui_launch
    if lc.command:
        exe = lc.command[0]
        found = os.path.isfile(exe) or shutil.which(exe)
        cwd_ok = not lc.cwd or os.path.isdir(cfg.resolve(lc.cwd))
        if found and cwd_ok:
            _line(" ok ", f"ComfyUI is started with: {' '.join(lc.command)}" + (f"  (in {lc.cwd})" if lc.cwd else ""))
        else:
            bad += 1
            _line("FAIL", f"comfyui_launch: {'no ' + exe if not found else 'no folder ' + lc.cwd}")
    from .comfyenv import cli as envcli
    for tag, text in envcli.check_lines(cfg):       # differences are warnings: they never stop a machine
        _line(tag, text)
    for root in storage or [m.posix for m in cfg.paths.mappings]:
        local = P.to_farm(root, cfg.paths.mappings)
        ok = os.path.isdir(local)
        bad += 0 if ok else 1
        _line(" ok " if ok else "FAIL", f"storage {root}" + (f" -> {local}" if local != root else "")
              + ("" if ok else "  (not reachable from this machine/account: add it to paths.mappings)"))
    bad += check_folder("scratch", str(cfg.resolve(cfg.convert.scratch_dir)), min_free_gb=20)
    bad += check_tools(cfg.convert)
    bad += check_ocio(Config.model_validate({"workers": [], "ocio": OcioConfig().model_dump(),
                                             "convert": cfg.convert.model_dump(),
                                             "paths": {"mappings": [m.model_dump() for m in cfg.paths.mappings]}}))
    print("All good." if not bad else f"{bad} problem(s).")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="comfyfarm", description="ComfyUI farm")
    ap.add_argument("--config", help="config.yaml path (default: repo root, or $COMFYFARM_CONFIG)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("standalone", help="run the manager and an agent per configured ComfyUI, in one process")
    sub.add_parser("serve", help="the same as standalone (its old name)")
    sub.add_parser("manager", help="run the manager only; agents on other machines check in")
    agent = sub.add_parser("agent", help="run an agent for this machine's ComfyUI")
    agent.add_argument("--config", dest="agent_config",
                       help="agent.yaml path (default: repo root, or $COMFYFARM_AGENT_CONFIG)")
    agent.add_argument("--check", action="store_true",
                       help="check this worker (manager, token, ComfyUI, nodes, storage, scratch, tools) and exit")
    sub.add_parser("check", help="preflight: workflows, workers, node packs, models, storage")
    from .comfyenv import cli as envcli
    envcli.add_parser(sub)
    args = ap.parse_args(argv)

    # Node titles contain emoji (VHS nodes); a service's redirected stdout on Windows
    # defaults to cp1252 and would choke on them.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if args.cmd == "comfyui":
        return envcli.run(args)
    if args.cmd == "agent":
        if args.check:
            from .config import default_agent_config_path, load_agent_config
            path = args.agent_config or args.config or default_agent_config_path()
            return asyncio.run(run_agent_check(load_agent_config(path), path))
        return run_agent(args.agent_config or args.config, args.verbose)

    cfg = load_config(args.config)
    if args.cmd == "check":
        return asyncio.run(run_check(cfg))

    setup_logging(cfg, args.verbose)
    for note in cfg.obsolete_keys:
        logging.getLogger("comfyfarm").warning("config.yaml: %s", note)
    import uvicorn

    from .manager.app import create_app
    uvicorn.run(create_app(cfg, local_agents=args.cmd != "manager"), host=cfg.server.host,
                port=cfg.server.port, log_config=None, ws_ping_interval=20)
    return 0


def run_agent(path: str | None, verbose: bool = False) -> int:
    """`comfyfarm agent`: this machine's agent, until it's stopped."""
    from .agent.agent import Agent
    from .agent.link import HttpLink
    from .config import load_agent_config

    cfg = load_agent_config(path)
    setup_logging(cfg, verbose, f"agent-{cfg.name}.log")
    agent = Agent(cfg, HttpLink(cfg.manager, cfg.token))
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
