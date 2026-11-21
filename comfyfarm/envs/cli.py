"""python -m comfyfarm env list | lock | install | remove (docs/decisions/0024).

    env list                    the repo's environments, and what this machine has built of each
    env lock <name>             write its uv.lock (needs pypi.org; commit the file)
    env install <name>          build this machine's copy, check it, make it current
    env remove <name>           remove what this machine has built of it

Builds go into agent.yaml's envs.dir (default data/envs in this checkout); --dir and --uv
override it, so these run on a machine without an agent too."""

from __future__ import annotations

from pathlib import Path

from . import build as B
from . import spec as S


def _where(args) -> tuple[Path, list[str] | None]:
    from ..config import default_agent_config_path, load_agent_config
    path = args.agent_config or default_agent_config_path()
    cfg = load_agent_config(path) if args.agent_config or Path(path).exists() else None
    root = Path(args.dir) if args.dir else (cfg.resolve(cfg.envs.dir) if cfg else S.REPO / "data" / "envs")
    return root, (args.uv or (list(cfg.envs.uv) if cfg and cfg.envs.uv else None))


def cmd_list(args) -> int:
    root, _ = _where(args)
    names = S.names()
    if not names:
        print(f"No environments in {S.ENVS}.")   # read now, so tests can point it elsewhere
    for name in names:
        try:
            spec = S.load(name)
        except S.SpecError as e:
            print(f"{name}: {e}")
            continue
        have = B.builds(root, name)
        fp = spec.fingerprint
        if have.current == fp and have.info.get(fp, {}).get("ok"):
            state = "built, current"
        elif have.current:
            state = f"this machine has {have.current}, an older version: run `env install {name}`"
        else:
            state = "not built here"
        print(f"{name}  {fp}  Python {spec.python}  {'locked' if spec.locked else 'NOT LOCKED'}  {state}")
        if spec.description:
            print(f"    {spec.description}")
        if have.previous:
            print(f"    previous: {have.previous}")
    print(f"(builds in {root})")
    return 0


def cmd_lock(args) -> int:
    root, uv = _where(args)
    spec = S.load(args.name)
    out = B.lock(spec, B.get_uv(root, uv), root)
    print(out.strip()[-800:])
    print(f"Written {spec.folder / 'uv.lock'}: commit it.")
    return 0


def cmd_install(args) -> int:
    root, uv = _where(args)
    spec = S.load(args.name)
    meta = B.install(spec, root, B.get_uv(root, uv), force=args.force)
    if meta.get("copied"):
        print("Note: uv copied packages instead of linking them from its cache (another disk, or one that can't "
              "hard-link), so each environment takes its full size.")
    print(f"Python: {B.python_of(root / spec.name / meta['fingerprint'])}")
    return 0


def cmd_remove(args) -> int:
    root, _ = _where(args)
    print(f"Removed {args.name}'s builds from {root}." if B.remove(root, args.name)
          else f"Nothing of {args.name} is built in {root}.")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("env", help="runner environments: list them, lock one, build or remove this machine's copy")
    cmds = p.add_subparsers(dest="env_cmd", required=True)
    for name, helptext in (("list", "the repo's environments and what is built here"),
                           ("lock", "write an environment's uv.lock (needs pypi.org; commit it)"),
                           ("install", "build this machine's copy of an environment, check it, make it current"),
                           ("remove", "remove what this machine has built of an environment")):
        c = cmds.add_parser(name, help=helptext)
        if name != "list":
            c.add_argument("name", help="its folder in environments/")
        if name == "install":
            c.add_argument("--force", action="store_true", help="build it again even if this version is built")
        c.add_argument("--config", dest="agent_config",
                       help="agent.yaml (default: repo root, or $COMFYFARM_AGENT_CONFIG)")
        c.add_argument("--dir", help="where builds go (default: agent.yaml's envs.dir, else data/envs)")
        c.add_argument("--uv", nargs="+",
                       help="a uv to use (default: agent.yaml's envs.uv, else one installed with pip)")


def run(args) -> int:
    try:
        return {"list": cmd_list, "lock": cmd_lock, "install": cmd_install, "remove": cmd_remove}[args.env_cmd](args)
    except (S.SpecError, B.BuildError) as e:
        print(f"STOPPED: {e}")
        return 1
