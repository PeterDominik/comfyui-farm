"""python -m comfyfarm env list | lock | install | remove | publish | releases
(docs/decisions/0024, 0028).

    env list                    the repo's environments, and what this machine has built of each
    env lock <name>             write its uv.lock (needs pypi.org; commit the file)
    env install <name>          build this machine's copy, check it, make it current
    env install <name> --release <fingerprint|latest>
                                the same, from a release in the bank, checked against its hashes
    env remove <name>           remove what this machine has built of it
    env publish <name>          on the manager's host: the environment as committed, as a release
                                in the bank (share.dir), for agents to install
    env releases [<name>]       what the bank has

Builds go into agent.yaml's envs.dir (default data/envs in this checkout); --dir and --uv
override it, so these run on a machine without an agent too. The bank is --bank, else the
folder of agent.yaml's manager.json, else config.yaml's share.dir."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import build as B
from . import release as R
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
        lock = "LOCK OUT OF DATE" if spec.stale else "locked" if spec.locked else "NOT LOCKED"
        print(f"{name}  {fp}  Python {spec.python}  {lock}  {state}")
        if spec.stale:
            print(f"    uv.lock doesn't match pyproject.toml: {spec.stale}")
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


def _manager_bank(args) -> Path | None:
    from ..config import default_config_path, load_config
    path = getattr(args, "manager_config", None) or default_config_path()
    if not Path(path).exists():
        return None
    share = load_config(path).share.dir
    return Path(share) if share else None


def _bank(args, manager_only: bool = False) -> Path:
    """The farm's shared folder, where releases are: --bank; else, on an agent, the folder
    its manager.json is in (agent.yaml's manager); else config.yaml's share.dir."""
    if args.bank:
        return Path(args.bank)
    if not manager_only:
        from ..config import default_agent_config_path, load_agent_config
        path = args.agent_config or default_agent_config_path()
        if args.agent_config or Path(path).exists():
            manager = load_agent_config(path).manager.strip()
            if manager and not manager.startswith(("http://", "https://")):
                return Path(manager).parent
    bank = _manager_bank(args)
    if bank is None:
        raise S.SpecError("say where the bank is: --bank <the farm's shared folder> (config.yaml's share.dir, where "
                          "manager.json is)")
    return bank


def cmd_install(args) -> int:
    root, uv = _where(args)
    origin = None
    if args.release:
        rel = R.find(_bank(args), args.name, args.release)
        print(f"{rel.name} {rel.fingerprint}: published {rel.published_at} by {rel.meta.get('published_by')}"
              f"{', commit ' + rel.meta['commit'][:10] if rel.meta.get('commit') else ''}")
        stage = root / ".releases" / rel.name / rel.fingerprint
        spec = R.fetch(rel, stage)
        origin = {"release": rel.fingerprint, "published_at": rel.published_at, "commit": rel.meta.get("commit")}
    else:
        stage, spec = None, S.load(args.name)
    try:
        meta = B.install(spec, root, B.get_uv(root, uv), force=args.force, origin=origin)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
    if meta.get("copied"):
        print("Note: uv copied packages instead of linking them from its cache (another disk, or one that can't "
              "hard-link), so each environment takes its full size.")
    print(f"Python: {B.python_of(root / spec.name / meta['fingerprint'])}")
    return 0


def cmd_publish(args) -> int:
    spec = S.load(args.name)
    commit, changed = R.checkout(S.ENVS.parent, spec.folder)
    if changed:
        raise R.ReleaseError(f"{spec.name} has changes that aren't committed ({', '.join(changed)}): a release is "
                             f"the reviewed repo's, so commit them (through a pull request) first")
    if commit is None:
        print("Note: this isn't a git checkout, so the release names no commit.")
    rel, new = R.publish(spec, _bank(args, manager_only=True), commit=commit)
    print(f"{'Published' if new else 'Already published:'} {rel.name} {rel.fingerprint} in {rel.folder}")
    return 0


def cmd_releases(args) -> int:
    bank = _bank(args)
    rows = R.listed(bank, args.name)
    if not rows:
        print(f"No releases in {R.root(bank)}.")
    for r in rows:
        if "error" in r:
            print(f"{r['name']}  {r['fingerprint']}  {r['error']}")
            continue
        commit = f"  commit {r['commit'][:10]}" if r.get("commit") else ""
        print(f"{r['name']}  {r['fingerprint']}  Python {r.get('python')}  published {r.get('published_at')} by "
              f"{r.get('published_by')}{commit}")
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
                           ("remove", "remove what this machine has built of an environment"),
                           ("publish", "on the manager's host: publish an environment as committed, as a release in "
                                       "the bank"),
                           ("releases", "the releases in the bank")):
        c = cmds.add_parser(name, help=helptext)
        if name == "releases":
            c.add_argument("name", nargs="?", help="one environment's (default: all)")
        elif name != "list":
            c.add_argument("name", help="its folder in environments/")
        if name == "install":
            c.add_argument("--force", action="store_true", help="build it again even if this version is built")
            c.add_argument("--release", metavar="FINGERPRINT", help="from this release in the bank (or latest), "
                                                                     "not from this checkout")
        if name in ("install", "publish", "releases"):
            c.add_argument("--bank", help="the farm's shared folder (default: where agent.yaml's manager.json is, "
                                          "else config.yaml's share.dir)")
            c.add_argument("--manager-config", dest="manager_config",
                           help="the manager's config.yaml (default: repo root, or $COMFYFARM_CONFIG)")
        c.add_argument("--config", dest="agent_config",
                       help="agent.yaml (default: repo root, or $COMFYFARM_AGENT_CONFIG)")
        c.add_argument("--dir", help="where builds go (default: agent.yaml's envs.dir, else data/envs)")
        c.add_argument("--uv", nargs="+",
                       help="a uv to use (default: agent.yaml's envs.uv, else one installed with pip)")


def run(args) -> int:
    try:
        return {"list": cmd_list, "lock": cmd_lock, "install": cmd_install, "remove": cmd_remove,
                "publish": cmd_publish, "releases": cmd_releases}[args.env_cmd](args)
    except (S.SpecError, B.BuildError, R.ReleaseError) as e:
        print(f"STOPPED: {e}")
        return 1
