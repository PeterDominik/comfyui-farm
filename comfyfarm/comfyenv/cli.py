"""comfyfarm comfyui snapshot | verify: the farm's ComfyUI environment file, from and
against this machine.

Both read this machine's agent.yaml (--config) for where ComfyUI and its Python are
(comfyui_dir / comfyui_python, else comfyui_launch) and ComfyUI's address; --comfyui,
--python and --url override it, so they also run on a machine without an agent."""

from __future__ import annotations

import json
import os
import socket
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


def _check(folder: str, python: str, url: str, command: list[str], tags=()) -> tuple[dict | None, dict, dict]:
    """(the environment file, this machine's state, how it differs). Packs and models of
    workflows this machine never gets (requires: VRAM, tags) don't count, as on the dashboard."""
    from ..workflows import WorkflowRegistry

    spec, farm_paths = S.load(S.SPEC), S.farm_model_paths(S.FARM_MODEL_PATHS)
    if spec is None:
        return None, {}, {"differs": [], "notes": []}
    stats = _get(url + "/system_stats", 5)
    state = M.collect(folder, python, stats, argv=command[1:] if command else None, **S.wanted(spec))
    if stats is None:
        state["notes"].append(f"ComfyUI isn't answering at {url}: its version and flags were read from its folder "
                              f"and agent.yaml")
    devices = (stats or {}).get("devices") or [{}]
    skip = S.excluded(WorkflowRegistry(REPO / "workflows"), devices[0].get("vram_total"), {*tags, M.OS})
    return spec, state, S.compare(spec, state, farm_paths, skip=skip)


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
    from ..workflows import WorkflowRegistry

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


def add_parser(sub) -> None:
    p = sub.add_parser("comfyui", help="the farm's ComfyUI environment: snapshot it from this machine, or verify "
                                       "this machine against it")
    cmds = p.add_subparsers(dest="comfyui_cmd", required=True)
    for name, helptext in (("snapshot", f"write {_shown(S.SPEC)} from this machine's ComfyUI"),
                           ("verify", "list how this machine's ComfyUI differs from the farm's")):
        c = cmds.add_parser(name, help=helptext)
        c.add_argument("--config", dest="agent_config", help="agent.yaml (default: repo root, or $COMFYFARM_AGENT_CONFIG)")
        c.add_argument("--comfyui", help="ComfyUI's folder (default: from agent.yaml)")
        c.add_argument("--python", help="ComfyUI's Python (default: from agent.yaml)")
        c.add_argument("--url", help="ComfyUI's address (default: from agent.yaml)")
        if name == "snapshot":
            c.add_argument("--out", help="where to write it (default: the repo's)")
            c.add_argument("--name", help="this machine's name in the file (default: the agent's)")
        else:
            c.add_argument("--json", action="store_true", help="print the machine's state and differences as JSON")


def run(args) -> int:
    return snapshot(args) if args.comfyui_cmd == "snapshot" else verify(args)
