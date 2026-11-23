"""python -m comfyfarm standalone | serve | manager | agent | check | comfyui

  standalone   the manager and an agent for each ComfyUI in config.yaml's workers, in one
               process, as the farm has always run (serve is the same, under its old name)
  manager      the manager only: agents on other machines check in (comfyfarm agent)
  agent        one agent: runs the manager's jobs on this machine's ComfyUI (agent.yaml);
               agent --check checks this worker and exits
  check        preflight: workflows, tools, OCIO, storage, the shared folder, and config.yaml's
               workers
  comfyui      comfyui snapshot: write the farm's ComfyUI environment file from this machine;
               comfyui verify: list how this machine's ComfyUI differs from it (comfyenv)
  alerts       alerts test: post a test message to config.yaml's alerts.google_chat
  eval         a definition's eval on the machines (all that may run it, or --machine), through
               the manager on this host, and how each check went (README: Evals)"""

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
from .runners.comfyui_workflow import WorkflowRegistry


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
            # With the stamp a job's maps carry (decision 0039), so this oiiotool is known to take it.
            (f"texture colour map {OC.IMAGE} -> {OC.LINEAR}",
             O.map_command(cmd, ocio, {"color": True, "bits": "half"}, png, exr,
                           stamp={"comfyfarm:job": "check", "comfyfarm:provenance": "provenance.json"})),
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


def check_workflows(reg, what: str = "workflow") -> int:
    from . import catalog as C
    cat = C.current()
    bad = 0
    for name, err in reg.load_errors.items():
        _line("FAIL", f"{what} {name}: {err}")
        bad += 1
    for wf in reg.workflows.values():
        if wf.problems:
            bad += 1
            _line("FAIL", f"{what} {wf.name}:")
            for p in wf.problems:
                print(f"         - {p}")
        else:
            _line(" ok ", f"{what} {wf.name} ({len(wf.params)} params), in the menus under "
                          f"{cat.place(wf.task, wf.provider, wf.hidden is True)}")
    return bad


def check_catalog() -> int:
    """The menus' providers and tasks (catalog.py, decision 0054)."""
    from . import catalog as C
    cat = C.current()
    for e in cat.errors:
        _line("FAIL", f"menus: {e}")
    if not cat.errors:
        _line(" ok ", f"menus: {cat.summary()}")
    return len(cat.errors)


def check_process_runner(cfg) -> int:
    """`agent --check`: whether this machine can run each process definition (the process
    runner on, its environment built here at this checkout's version)."""
    from .runners.process import ProcessRunner
    said = ProcessRunner(cfg).processes()
    if not said:
        return 0
    if not cfg.process_runner.enabled:
        _line(" ok ", f"process runner: off (process_runner.enabled in agent.yaml), so {len(said)} process "
                      f"definition(s) aren't run here")
        return 0
    bad = 0
    for name, v in sorted(said.items()):
        probs = list(v["broken"]) + list(v["problems"])
        if probs:
            bad += 1
            _line("FAIL", f"process {name}:")
            for p in probs:
                print(f"         - {p}")
        else:
            _line(" ok ", f"process {name}: its environment is built here")
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


def check_gpus(command: list[str]) -> int:
    """This machine's GPUs as the agent's gpu_probe reads them: a warning when it can't (it's optional)."""
    from .agent import gpus as G
    found, error = G.probe(command)
    if error:
        _line("WARN", f"GPUs: {error}")
    for g in found:
        size = f"{g['vram_total'] / 2**30:.0f} GB" if g["vram_total"] else "memory unknown"
        _line(" ok ", f"GPU {g['index']}: {g['name']}, {size}, driver {g['driver']}")
    return 0


async def check_comfyui(name: str, url: str, reg: WorkflowRegistry) -> int:
    """A ComfyUI answers, and has every node, input and model each workflow needs."""
    from .runners.comfy import ComfyClient   # here, so check_ocio and --help don't need httpx
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
    bad = check_catalog()
    bad += check_workflows(reg)
    from .runners.process import ProcessRegistry
    bad += check_workflows(ProcessRegistry(cfg.resolve(cfg.workflows_dir)), "process")
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
    bad += check_process_runner(cfg)
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
    bad += check_gpus(cfg.gpu_probe.command)
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
    pol = sub.add_parser("policy", help="the farm's policy.yaml (README: Policy)")
    pol.add_argument("action", choices=["check"], help="check: validate a policy file before it goes in the bank")
    pol.add_argument("file", nargs="?", help="the file to check (default: the one config.yaml's policy.file names)")
    al = sub.add_parser("alerts", help="the manager's alerts (README: Alerts)")
    al.add_argument("action", choices=["test"], help="test: post a test message to alerts.google_chat")
    ev = sub.add_parser("eval", help="run a definition's eval on machines, through the manager on this host "
                                     "(README: Evals)")
    ev.add_argument("definition", help="a definition with eval: in its process.yaml, e.g. gpu_test")
    ev.add_argument("--machine", action="append", help="a machine to run it on (repeat for more; default: every "
                                                       "machine checking in that may run it)")
    from .comfyenv import cli as envcli
    envcli.add_parser(sub)
    from .envs import cli as runnerenvs
    runnerenvs.add_parser(sub)
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
    if args.cmd == "env":
        return runnerenvs.run(args)
    if args.cmd == "agent":
        if args.check:
            from .config import default_agent_config_path, load_agent_config
            path = args.agent_config or args.config or default_agent_config_path()
            return asyncio.run(run_agent_check(load_agent_config(path), path))
        return run_agent(args.agent_config or args.config, args.verbose)

    cfg = load_config(args.config)
    if args.cmd == "check":
        return asyncio.run(run_check(cfg))
    if args.cmd == "policy":
        return run_policy_check(cfg, args.file)
    if args.cmd == "alerts":
        return run_alerts_test(cfg)
    if args.cmd == "eval":
        return run_eval(cfg, args.definition, args.machine)

    setup_logging(cfg, args.verbose)
    for note in cfg.obsolete_keys:
        logging.getLogger("comfyfarm").warning("config.yaml: %s", note)
    import uvicorn

    from .manager.app import create_app
    uvicorn.run(create_app(cfg, local_agents=args.cmd != "manager"), host=cfg.server.host,
                port=cfg.server.port, log_config=None, ws_ping_interval=20)
    return 0


def _name(f: str) -> str:
    return f.replace("\\", "/").rsplit("/", 1)[-1].lower()


def model_notes(definitions: dict, env: dict | None, pf=None) -> list[str]:
    """What an admin should know before trusting licence rules (decision 0044): definitions
    that don't list their models, files the farm's ComfyUI environment gives a definition
    that its models: doesn't list, and licences a rule names that no definition carries
    (a typo would refuse or allow nothing)."""
    notes = []
    unlisted = sorted(n for n, wf in definitions.items() if not wf.models_listed)
    if unlisted:
        notes.append(f"these don't list their models (licence rules treat them as unlisted_models says): "
                     f"{', '.join(unlisted)}")
    for file, entry in sorted(((env or {}).get("models") or {}).items()):
        for n in entry.get("for") or []:
            wf = definitions.get(n)
            if wf is not None and wf.models_listed and _name(file) not in {_name(m["file"]) for m in wf.models}:
                notes.append(f"{n} loads {file} (deploy/comfyui/environment.yaml), which its models: doesn't list")
    if pf is not None:
        carried = {m["licence"].strip().lower() for wf in definitions.values() for m in wf.models}
        for r in pf.licences:
            for lic in r.licences():
                if lic.strip().lower() not in carried:
                    notes.append(f"rule {r.name} names licence {lic}, which no definition's models carry")
    return notes


def run_eval(cfg: Config, definition: str, machines: list[str] | None, poll: float = 3.0) -> int:
    """`comfyfarm eval`: a definition's eval on machines (decision 0053), through the manager on
    this host (it starts them from its own host only), followed until each ends. 0 when all
    passed."""
    import getpass
    import json
    import textwrap
    import time
    import urllib.error
    import urllib.request

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(f"http://127.0.0.1:{cfg.server.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", "X-User": getpass.getuser()})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except ValueError:
                detail = None
            raise SystemExit(f"The manager said no: {detail or e}") from None
        except urllib.error.URLError as e:
            raise SystemExit(f"No manager on 127.0.0.1:{cfg.server.port} ({e.reason}): run this on the manager's "
                             f"host, with its config.yaml.") from None

    rows = call("POST", "/api/v1/evals", {"definition": definition, "machines": machines or None})["evals"]
    for r in rows:
        print(f"{r['definition']} on {r['machine']}: queued (job {r['job'][:8]})", flush=True)
    said = {r["job"]: r["stage"] for r in rows}
    end = time.monotonic() + cfg.evals.minutes * 60
    while any(r["status"] not in ("done", "failed", "cancelled", "gone") for r in rows):
        if time.monotonic() > end:
            print(f"Not over after {cfg.evals.minutes:g} minutes (evals.minutes): see the jobs on the Queue tab.")
            return 1
        time.sleep(poll)
        rows = call("GET", "/api/v1/evals?jobs=" + ",".join(r["job"] for r in rows))["evals"]
        for r in rows:
            if r["stage"] != said.get(r["job"]) and r["status"] not in ("done", "failed", "cancelled"):
                print(f"{r['definition']} on {r['machine']}: {r['stage']}", flush=True)
                said[r["job"]] = r["stage"]
    ok = True
    for r in rows:
        if r["status"] == "done":
            print(f"\n{r['definition']} on {r['machine']}: passed")
            for c in r.get("checks") or []:
                print(f"  [{' ok ' if c['ok'] else 'FAIL'}] {c['check']}: {c['said']}")
        else:
            ok = False
            print(f"\n{r['definition']} on {r['machine']}: {r['status'].upper()}")
            print(textwrap.indent(r.get("error") or "no reason given", "  "))
    print(f"\nThe outputs are in each machine's scratch folder, in evals/{definition}, until its next eval.")
    return 0 if ok else 1


def run_alerts_test(cfg: Config) -> int:
    """`comfyfarm alerts test`: post a test message to the Google Chat space config.yaml's
    alerts.google_chat names, to see the webhook works before an alert needs it."""
    import socket

    from .manager.alerts import post_google_chat
    from .manager.share import manager_url
    if not cfg.alerts.google_chat:
        print("alerts.google_chat isn't set in config.yaml: alerts go to the log and the dashboard only.")
        return 1
    error = post_google_chat(cfg.alerts.google_chat, f"*ComfyFarm* ({manager_url(cfg).split('://')[-1]})\n"
                             f"A test message: `comfyfarm alerts test` on {socket.gethostname()}. Alerts will "
                             f"come here.\n<{manager_url(cfg)}/#alerts|Alerts tab>")
    if error:
        print(f"  [FAIL] {error}. Check the webhook in the space (Apps & integrations > Webhooks), and that "
              f"this machine reaches chat.googleapis.com (HTTPS_PROXY, if the studio needs one).")
        return 1
    print("  [ ok ] posted: look for it in the space.")
    if not cfg.alerts.enabled:
        print("         (alerts.enabled is off in config.yaml: the manager raises no alerts yet)")
    return 0


def run_policy_check(cfg: Config, file: str | None) -> int:
    """`comfyfarm policy check [file]`: is this policy file one the manager would take? Its
    definitions are checked against this checkout's workflows/, and its licence rules
    against the licences their models carry."""
    from .comfyenv import spec as ES
    from .definitions import DefinitionRegistry
    from .manager.policy import parse, policy_path
    path = file or policy_path(cfg)[0]
    if not path:
        print("No policy file: give one, or set policy.file in config.yaml.")
        return 1
    definitions = DefinitionRegistry(cfg.resolve(cfg.workflows_dir)).workflows
    try:
        pf = parse(Path(path).read_text(encoding="utf-8"), set(definitions))
    except (OSError, ValueError) as e:
        print(f"  [FAIL] {path}: {getattr(e, 'strerror', None) or e}")
        return 1
    print(f"  [ ok ] {path}: {len(pf.shows)} show rule(s), {len(pf.licences)} licence rule(s), "
          f"{len(pf.quotas)} quota(s); shows no rule "
          f"names: {pf.other_shows}; a job whose show isn't known: {pf.unknown_show}; a definition that doesn't "
          f"list its models: {pf.unlisted_models}")

    def shows(r) -> str:
        return ", ".join(r.shows) + (f" (not {', '.join(r.except_)})" if r.except_ else "")

    for r in pf.shows:
        what = ("denies everything" if r.deny == "all" else f"denies {', '.join(r.deny)}" if r.deny
                else f"allows only {', '.join(r.allow or [])}")
        print(f"         {r.name}: {shows(r)} {what}: {r.reason}")
    for r in pf.licences:
        what = f"denies licences {', '.join(r.deny)}" if r.deny is not None else \
            f"allows only licences {', '.join(r.allow or [])}"
        print(f"         {r.name}: {shows(r)} {what}: {r.reason}")
    for r in pf.quotas:
        most = " and ".join(x for x in (f"{r.max_jobs} job(s)" if r.max_jobs else "",
                                        f"{r.max_frames} frame(s)" if r.max_frames else "") if x)
        each = {"show": "each show", "user": "each user, over them all", "all": "all of them together"}[r.per]
        print(f"         {r.name}: {shows(r)}, {each}: at most {most} queued or running: {r.reason}")
    for n, wf in sorted(definitions.items()):
        if wf.models_listed:
            print(f"         {n} uses " + (", ".join(f"{m['file']} ({m['licence']})" for m in wf.models)
                                          or "no model weights"))
    for note in model_notes(definitions, ES.load(), pf):
        print(f"  [note] {note}")
    if not cfg.policy.enabled:
        print("         (policy.enabled is off in config.yaml: the manager doesn't read it yet)")
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
