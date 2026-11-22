"""comfyfarm comfyui snapshot | verify | sync | syncs: the farm's ComfyUI environment file,
from and against this machine, and bringing this machine to it (comfyenv.sync).

All read this machine's agent.yaml (--config) for where ComfyUI and its Python are
(comfyui_dir / comfyui_python, else comfyui_launch) and ComfyUI's address; --comfyui,
--python and --url override it, so they also run on a machine without an agent.

On the manager's host, `sync --all` (or --machine) asks the manager to sync the machines
instead, one at a time, the canary first, each by its own agent (manager/syncs.py,
decision 0033); `syncs` shows how that goes, and `sync --cancel` stops it."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import socket
import textwrap
import time
from pathlib import Path

from ..fileio.ocio import REPO
from . import machine as M
from . import spec as S


def _where(args) -> tuple[str, str, str, list[str]]:
    """(ComfyUI's folder, its Python, its URL, its launch command) from the arguments and agent.yaml."""
    from ..config import default_agent_config_path, load_agent_config

    cfg = None
    path = args.agent_config or default_agent_config_path()
    if args.agent_config or Path(path).exists():
        cfg = load_agent_config(path)
    folder = args.comfyui or (M.comfyui_folder(cfg) if cfg else "")
    python = args.python or (M.comfyui_python(cfg) if cfg else "")
    url = (args.url or (cfg.comfyui if cfg else "") or "http://127.0.0.1:8188").rstrip("/")
    command = list(cfg.comfyui_launch.command) if cfg else []
    args.tags = [str(t).lower() for t in cfg.tags] if cfg else []
    if folder and cfg and not os.path.isabs(folder):
        folder = str(cfg.resolve(folder))
    args.name = getattr(args, "name", None) or (cfg.name if cfg else socket.gethostname())
    args.cfg = cfg
    return folder, python, url, command


def _get(url: str, timeout: float) -> dict | None:
    import httpx
    try:
        r = httpx.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def _shown(path: Path) -> str:
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def _describe(state: dict) -> str:
    c = state.get("comfyui") or {}
    py = (state.get("python") or {}).get("python")
    torch = ((state.get("python") or {}).get("packages") or {}).get("torch")
    bits = [f"ComfyUI {S._rev(c)} in {c.get('dir')}"]
    if py:
        bits.append(f"Python {py}")
    if torch:
        bits.append(f"torch {torch}")
    if state.get("driver"):
        bits.append(f"driver {state['driver']}")
    return ", ".join(bits)


def _state(folder: str, python: str, url: str, command: list[str], tags=()) -> tuple[dict | None, dict, set[str]]:
    """(the environment file, this machine's state, the workflows it never gets). Packs and
    models of those (requires: VRAM, tags) don't count, as on the dashboard."""
    from ..runners.comfyui_workflow import WorkflowRegistry

    spec = S.load(S.SPEC)
    if spec is None:
        return None, {}, set()
    stats = _get(url + "/system_stats", 5)
    state = M.collect(folder, python, stats, argv=command[1:] if command else None, **S.wanted(spec))
    if stats is None and not state.get("error"):
        state["notes"].append(f"ComfyUI isn't answering at {url}: its version and flags were read from its folder "
                              f"and agent.yaml")
    devices = (stats or {}).get("devices") or [{}]
    skip = S.excluded(WorkflowRegistry(REPO / "workflows"), devices[0].get("vram_total") or state.get("gpu_memory"),
                      {*tags, M.OS})
    return spec, state, skip


def _check(folder: str, python: str, url: str, command: list[str], tags=()) -> tuple[dict | None, dict, dict]:
    """(the environment file, this machine's state, how it differs)."""
    spec, state, skip = _state(folder, python, url, command, tags)
    if spec is None:
        return None, {}, {"differs": [], "notes": []}
    return spec, state, S.compare(spec, state, S.farm_model_paths(S.FARM_MODEL_PATHS), skip=skip)


def check_lines(cfg) -> list[tuple[str, str]]:
    """For `comfyfarm agent --check`: how this machine differs from the farm's ComfyUI, as
    warnings (a difference never stops a machine)."""
    folder = M.comfyui_folder(cfg)
    if folder and not os.path.isabs(folder):
        folder = str(cfg.resolve(folder))
    spec, state, result = _check(folder, M.comfyui_python(cfg), cfg.comfyui.rstrip("/"), list(cfg.comfyui_launch.command),
                                 [str(t).lower() for t in getattr(cfg, "tags", [])])
    if spec is None:
        return [("WARN", f"no farm ComfyUI environment file yet ({_shown(S.SPEC)}): comfyfarm comfyui snapshot")]
    if state.get("error"):
        return [("WARN", f"ComfyUI environment not checked: {state['error']}")]
    lines = [("WARN", f"differs from the farm's ComfyUI: {d['message']}") for d in result["differs"]]
    lines += [("WARN", f"ComfyUI environment, not checked: {n}") for n in result["notes"]]
    return lines or [(" ok ", f"ComfyUI matches the farm's environment ({_shown(S.SPEC)})")]


def verify(args) -> int:
    folder, python, url, command = _where(args)
    spec, state, result = _check(folder, python, url, command, args.tags)
    if spec is None:
        print(f"There's no environment file yet ({S.SPEC}): run `comfyfarm comfyui snapshot` on the reference machine.")
        return 2
    if args.json:
        print(json.dumps({"state": state, **result}, indent=2))
        return 1 if result["differs"] else 0
    if state.get("error"):
        print(state["error"])
        return 2
    print(_describe(state))
    ref = spec.get("reference") or {}
    print(f"Farm environment: {_shown(S.SPEC)} (from {ref.get('machine', '?')}, {ref.get('date', '?')})")
    if result["differs"]:
        print(f"\n{len(result['differs'])} difference(s):")
        for d in result["differs"]:
            print(f"  - {d['message']}")
    else:
        print("\nMatches the farm's ComfyUI.")
    for n in result["notes"]:
        print(f"  note: {n}")
    return 1 if result["differs"] else 0


def snapshot(args) -> int:
    from ..runners.comfyui_workflow import WorkflowRegistry

    folder, python, url, command = _where(args)
    stats = _get(url + "/system_stats", 5)
    object_info = _get(url + "/object_info", 120) if stats is not None else None
    if object_info is None:
        print(f"ComfyUI isn't answering at {url}: start it (snapshot reads which node pack provides each node "
              f"from it), or pass --url.")
        return 2
    out = Path(args.out) if args.out else S.SPEC
    old = S.load(out) or {}
    registry = WorkflowRegistry(REPO / "workflows")
    need = S.needs(registry, object_info)
    state = M.collect(folder, python, stats, argv=command[1:] if command else None,
                      libraries=old.get("libraries") or S.LIBRARIES, packs=list(need["packs"]),
                      models={m: None for m in need["models"]}, packages=M.KEY_PACKAGES)
    if state.get("error"):
        print(state["error"])
        return 2
    spec, warnings = S.snapshot(state, need, args.name, old)
    S.save(spec, out)
    print(_describe(state))
    print(f"Wrote {out}: {len(spec.get('node_packs') or {})} node pack(s), {len(spec.get('models') or {})} model(s), "
          f"{len(spec.get('packages') or {})} package(s). Review the diff (git diff) and commit it.")
    for w in warnings:
        print(f"  warning: {w}")
    return 0


# ================================================================ sync
async def _ping(cfg) -> dict:
    from ..agent.link import HttpLink
    link = HttpLink(cfg.manager, cfg.token)
    try:
        return await link.ping({"name": cfg.name})
    finally:
        await link.close()


def _not_safe(cfg, url: str, ask_manager: bool) -> str | None:
    """Why ComfyUI mustn't be stopped now, or None: a job in it, or (asking the manager) a
    machine that isn't drained or still holds jobs."""
    from ..protocol import LinkError

    q = _get(url + "/queue", 5)
    if q and (q.get("queue_running") or q.get("queue_pending")):
        return "ComfyUI has a job in its queue: wait for it to finish, then run sync again."
    if cfg is None or not ask_manager:
        return None
    try:
        r = asyncio.run(_ping(cfg))
    except LinkError as e:
        return (f"Can't ask the manager whether {cfg.name} is drained ({e}). Drain it on the dashboard, and once it "
                f"holds no jobs run sync again with --no-manager.")
    if "drained" not in r:
        return (f"The manager doesn't say whether {cfg.name} is drained (an older manager): drain it on the dashboard "
                f"and, once it holds no jobs, run sync again with --no-manager.")
    if not r["drained"]:
        return (f"Drain {cfg.name} on the dashboard's Machines tab first (sync stops its ComfyUI), then run sync again "
                f"once it holds no jobs.")
    if r.get("held"):
        return f"{cfg.name} still holds {r['held']} job(s): run sync again once they're done."
    return None


def _stop_comfyui(cfg, url: str) -> str:
    """Stop the ComfyUI the agent started (with the hold in place, it isn't started again)."""
    from ..agent.launch import Launcher, pid_alive, stop_process
    from .sync import SyncError

    if _get(url + "/system_stats", 3) is None:
        return "ComfyUI isn't running"
    pid = None
    if cfg is not None:
        pid = Launcher(cfg.name, cfg.comfyui_launch, cfg.resolve(cfg.log_dir), cfg.base_dir).running_pid()
    if pid is None:
        raise SyncError(f"ComfyUI is running at {url}, but the agent didn't start it, so sync won't stop it: stop it "
                        f"yourself, then run sync again.")
    stop_process(pid)
    end = time.time() + 30
    while pid_alive(pid) and time.time() < end:
        time.sleep(0.3)
    if pid_alive(pid):
        stop_process(pid, force=True)
    end = time.time() + 15
    while _get(url + "/system_stats", 2) is not None and time.time() < end:
        time.sleep(0.5)
    return f"stopped ComfyUI (pid {pid})"


def _print_plan(p) -> None:
    if p.differs:
        print(f"\n{len(p.differs)} difference(s):")
        for d in p.differs:
            print(f"  - {d['message']}")
    if p.steps:
        print("\nsync does:")
        for i, s in enumerate(p.steps, 1):
            print(f"  {i}. {s.title}")
    if p.manual:
        print("\nBy hand (sync doesn't do these):")
        for m in p.manual:
            print(f"  - {m}")


# ================================================================ syncs the manager runs
PHASES = {"draining": "waiting for its jobs to end", "told": "told", "syncing": "syncing",
          "checking": "back: being checked"}


def _sync_line(r: dict) -> str:
    """One sync the manager runs, and how each machine it reached went."""
    at = r["machines"][r["step"]] if r.get("machines") else "?"
    where = {"running": f"at {at} ({PHASES.get(r.get('phase'), r.get('phase'))})", "failed": f"stopped at {at}",
             "cancelled": f"at {at}"}.get(r["state"], "")
    lines = [f"{r['id']}  {r.get('runner')} to the farm's environment {r.get('file')}  {r['state']} {where}".rstrip() +
             f"  [{' > '.join(r.get('machines') or [])}]  (by {r.get('by') or '?'})"]
    for m in r.get("machines") or []:
        res = (r.get("results") or {}).get(m)
        if not res:
            continue
        said = res.get("note") or res.get("step") or res.get("error") or ""
        lines.append(f"    {m}: {res.get('state')}" + (f": {said}" if said and res.get("state") != "failed" else ""))
        lines += [f"      {title}: {how}" for title, how in res.get("steps") or []]
        lines += [f"      still different: {d}" for d in res.get("differs") or []]
        lines += [f"      by hand: {x}" for x in res.get("manual") or []]
    if r.get("error"):
        lines.append(textwrap.indent(r["error"], "    "))
    return "\n".join(lines)


def _sync_farm(args) -> int:
    """Ask the manager on this host to sync the machines (or stop its sync)."""
    from ..envs.cli import _manager
    from ..envs.spec import SpecError

    try:
        if args.cancel:
            print(_sync_line(_manager(args, "POST", "/api/v1/syncs/cancel")))
            return 0
        file = S.fingerprint()
        if file is None:
            print(f"There's no environment file ({S.SPEC}) in this checkout: nothing to sync to.")
            return 2
        r = _manager(args, "POST", "/api/v1/syncs", {"file": file, "runner": "comfyui", "force": args.force,
                                                   "machines": None if args.all else args.machine})
    except SpecError as e:
        print(f"STOPPED: {e}")
        return 1
    print(_sync_line(r))
    print("\nEach machine in turn: drained, then (once it holds no jobs) synced by its agent (ComfyUI stopped, "
          "updated, started again), checked and resumed. `comfyfarm comfyui syncs` and the dashboard's Machines tab "
          "show how it goes.")
    return 0


def syncs(args) -> int:
    from ..envs.cli import _manager
    from ..envs.spec import SpecError

    try:
        rows = _manager(args, "GET", "/api/v1/syncs")["syncs"]
    except SpecError as e:
        print(f"STOPPED: {e}")
        return 1
    if not rows:
        print("No syncs yet.")
    for r in rows:
        print(_sync_line(r))
    return 0


def sync(args) -> int:
    from ..agent import launch as L
    from . import sync as Y

    if args.machine or args.all or args.cancel:
        if args.apply:
            print("--apply syncs this machine; --all, --machine and --cancel ask the manager. Use one or the other.")
            return 2
        return _sync_farm(args)
    folder, python, url, command = _where(args)
    cfg = args.cfg
    spec, state, skip = _state(folder, python, url, command, args.tags)
    if spec is None:
        print(f"There's no environment file yet ({S.SPEC}): run `comfyfarm comfyui snapshot` on the reference machine.")
        return 2
    if state.get("error"):
        print(state["error"])
        return 2
    index = args.index_url if args.index_url is not None else Y.PYPI

    def make_plan(st):
        return Y.plan(spec, st, S.farm_model_paths(S.FARM_MODEL_PATHS), skip, comfyui_dir=folder, python=python,
                      force=args.force, index_url=index or None, farm_file=S.FARM_MODEL_PATHS)

    p = make_plan(state)
    print(_describe(state))
    print(f"Farm environment: {_shown(S.SPEC)}")
    if not p.differs:
        print("\nMatches the farm's ComfyUI: nothing to do.")
        return 0
    _print_plan(p)
    if not args.apply:
        if p.steps:
            print(f"\nNothing changed yet. Run again with --apply to do it: that stops ComfyUI, so first drain "
                  f"{args.name} on the dashboard's Machines tab and wait until it holds no jobs.")
        return 1
    if not p.steps:
        print("\nNothing sync can do itself: the rest is by hand.")
        return 1
    why = _not_safe(cfg, url, ask_manager=not args.no_manager)
    if why:
        print("\n" + why)
        return 2
    hold = L.hold_path(cfg.resolve(cfg.log_dir), cfg.name) if cfg else None
    if hold:
        L.write_hold(hold, f"comfyfarm comfyui sync is updating ComfyUI (since "
                           f"{datetime.datetime.now():%H:%M}): no jobs until it's done.")
    failed = False
    print()
    try:
        try:
            print(f"  {_stop_comfyui(cfg, url)}")
        except Y.SyncError as e:
            print(f"\n{e}")
            return 2
        for i, step in enumerate(p.steps, 1):
            print(f"  [{i}/{len(p.steps)}] {step.title} … ", end="", flush=True)
            try:
                print(step.run() or "done")
            except Y.SyncError as e:
                print(f"failed:\n{e}")
                failed = True
                break
    finally:
        if hold:
            L.release_hold(hold)        # the agent starts ComfyUI again
    spec, state, skip = _state(folder, python, url, command, args.tags)
    after = make_plan(state)
    if after.differs:
        print("\nStill different:" if not failed else "\nAs it is now:")
        for d in after.differs:
            print(f"  - {d['message']}")
        if after.manual:
            print("\nBy hand:")
            for m in after.manual:
                print(f"  - {m}")
    else:
        print("\nMatches the farm's ComfyUI.")
    if cfg and cfg.comfyui_launch.command:
        print(f"\nThe agent starts ComfyUI again within a minute. Then Resume {cfg.name} on the dashboard.")
    else:
        print("\nStart ComfyUI again" + (f", then Resume {cfg.name} on the dashboard." if cfg else "."))
    return 2 if failed else (1 if after.differs else 0)


def add_parser(sub) -> None:
    p = sub.add_parser("comfyui", help="the farm's ComfyUI environment: snapshot it from this machine, verify this "
                                       "machine against it, or sync this machine to it")
    cmds = p.add_subparsers(dest="comfyui_cmd", required=True)
    for name, helptext in (("snapshot", f"write {_shown(S.SPEC)} from this machine's ComfyUI"),
                           ("verify", "list how this machine's ComfyUI differs from the farm's"),
                           ("sync", "bring this machine's ComfyUI to the farm's (prints the plan; --apply does it); "
                                    "on the manager's host, --all has the manager sync every machine, one at a time"),
                           ("syncs", "on the manager's host: the syncs the manager runs, newest first")):
        c = cmds.add_parser(name, help=helptext)
        if name == "syncs":
            c.add_argument("--manager-config", dest="manager_config",
                           help="the manager's config.yaml, for its port (default: repo root, or $COMFYFARM_CONFIG)")
            continue
        c.add_argument("--config", dest="agent_config", help="agent.yaml (default: repo root, or $COMFYFARM_AGENT_CONFIG)")
        c.add_argument("--comfyui", help="ComfyUI's folder (default: from agent.yaml)")
        c.add_argument("--python", help="ComfyUI's Python (default: from agent.yaml)")
        c.add_argument("--url", help="ComfyUI's address (default: from agent.yaml)")
        if name == "snapshot":
            c.add_argument("--out", help="where to write it (default: the repo's)")
            c.add_argument("--name", help="this machine's name in the file (default: the agent's)")
        elif name == "verify":
            c.add_argument("--json", action="store_true", help="print the machine's state and differences as JSON")
        else:
            c.add_argument("--apply", action="store_true",
                           help="do it: stops ComfyUI (drain the machine first), updates it, the agent starts it again")
            c.add_argument("--force", action="store_true", help="discard local changes in ComfyUI's and the packs' git checkouts")
            c.add_argument("--no-manager", action="store_true",
                           help="don't ask the manager whether this machine is drained (it's down; you drained it)")
            c.add_argument("--index-url", help="pip's package index (default: pypi.org; '' for pip's own settings)")
            c.add_argument("--all", action="store_true",
                           help="on the manager's host: the manager syncs every machine, one at a time, the canary "
                                "first: each drained, synced by its agent, checked, resumed")
            c.add_argument("--machine", action="append",
                           help="on the manager's host: the manager syncs this machine (again for more)")
            c.add_argument("--cancel", action="store_true", help="on the manager's host: no next machine for its sync")
            c.add_argument("--manager-config", dest="manager_config",
                           help="the manager's config.yaml, for its port (default: repo root, or $COMFYFARM_CONFIG)")


def run(args) -> int:
    return {"snapshot": snapshot, "verify": verify, "sync": sync, "syncs": syncs}[args.comfyui_cmd](args)
