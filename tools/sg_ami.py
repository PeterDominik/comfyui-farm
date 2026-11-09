"""Puts ComfyFarm into ShotGrid's right-click menu ("Send to ComfyFarm", an Action Menu
Item; README: ShotGrid right-click), or takes it out again. It changes ShotGrid, so it only
shows what it would do until it's run with --apply.

Run it once, where shotgun_api3 is (the manager), with a ShotGrid script that may create
Action Menu Items (an admin script: the manager's own read-only script can't, and doesn't
need to):

    cd /mntx/raid/pdominik/comfyui-farm
    export SG_ADMIN_SCRIPT_NAME=<the admin script's name>
    PYTHONPATH=. $(cat run/python) tools/sg_ami.py --farm http://wrk1125l:9090 --show bri3
    PYTHONPATH=. $(cat run/python) tools/sg_ami.py --farm http://wrk1125l:9090 --show bri3 --apply

The script's key is SG_ADMIN_SCRIPT_KEY if it's set, else it's asked for (not shown as it's
typed). It's used for this run only: never written anywhere, never printed.

What it makes: an item on Versions and one on Playlists ("Send to ComfyFarm"), each
POSTing the selection to <farm>/api/v1/shotgrid/ami, which opens the dashboard (one
Version: the Submit form; a Playlist or several Versions: Batch Submit). Limited to
--show's projects (their tank_name) and/or --group's people (a permission group);
neither means everyone on the site, which it refuses without --everywhere.

Running it again changes the items it made (found by entity type and URL) rather than
making more; --remove deletes them (with --apply). --workflow <name> makes items that open
that workflow (URL …/ami?workflow=<name>), alongside the plain ones.

Before writing, it reads ActionMenuItem's fields from ShotGrid and sets only those the site
has; a limit it can't set (no projects field, say) stops it."""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from urllib.parse import urlencode, urlsplit

ENTITY = "ActionMenuItem"
TYPES = ("Version", "Playlist")
NAME_ENV, KEY_ENV = "SG_ADMIN_SCRIPT_NAME", "SG_ADMIN_SCRIPT_KEY"


class Stop(Exception):
    """Why it can't go on (printed, exit 1)."""


def parse_args(argv):
    p = argparse.ArgumentParser(prog="sg_ami.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--farm", required=True, help="the manager's address as people's browsers reach it (http://wrk1125l:9090)")
    p.add_argument("--show", action="append", default=[], help="only in this show's project (its tank_name); repeat for more")
    p.add_argument("--group", help="only for this permission group (its name in ShotGrid)")
    p.add_argument("--everywhere", action="store_true", help="every project, everyone: needed when neither --show nor --group is given")
    p.add_argument("--workflow", help="items that open this workflow (its name, as in workflows/)")
    p.add_argument("--title", help='the menu entry (default "Send to ComfyFarm"; with --workflow, "Send to ComfyFarm: <name>")')
    p.add_argument("--folder", help="a folder in the menu to put them in")
    p.add_argument("--types", default=",".join(TYPES), help="the entity types to add it to (default Version,Playlist)")
    p.add_argument("--remove", action="store_true", help="delete the items instead")
    p.add_argument("--apply", action="store_true", help="do it (without: only show what it would do)")
    p.add_argument("--site", help="the ShotGrid site (default: shotgrid.url in the manager's config.yaml)")
    return p.parse_args(argv)


def item_url(farm: str, workflow: str | None) -> str:
    u = urlsplit(farm)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise Stop(f"--farm {farm!r}: give the manager's address, like http://wrk1125l:9090")
    url = f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}/api/v1/shotgrid/ami"
    if workflow:
        if not re.match(r"^[\w.-]{1,80}$", workflow):
            raise Stop(f"--workflow {workflow!r} isn't a workflow name")
        url += "?" + urlencode({"workflow": workflow})
    return url


def credentials(env, ask_name, ask_key) -> tuple[str, str]:
    name = env.get(NAME_ENV) or ask_name("ShotGrid admin script name: ").strip()
    key = env.get(KEY_ENV) or ask_key(f"Key of the ShotGrid script {name} (not shown): ").strip()
    if not (name and key):
        raise Stop(f"No ShotGrid admin script: set {NAME_ENV} (and {KEY_ENV}, or type the key when asked).")
    return name, key


def valid_types(schema: dict, field: str) -> list[str]:
    return ((schema.get(field) or {}).get("properties", {}).get("valid_types", {}) or {}).get("value") or []


def links(rows) -> set:
    return {(r.get("type"), r.get("id")) for r in rows or []}


def as_written(values: dict) -> dict:
    """Entity lists as ShotGrid takes them: [{type, id}]."""
    return {k: ([{"type": r["type"], "id": r["id"]} for r in v] if isinstance(v, list) else v) for k, v in values.items()}


def describe(value) -> str:
    if isinstance(value, list):
        return ", ".join(f"{r.get('name') or r.get('code') or ''} ({r['type']} {r['id']})".strip() for r in value) or "(none)"
    return repr(value)


def run(args, sg, show_field: str, out) -> int:
    say = lambda *a: print(*a, file=out)      # noqa: E731
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    bad = [t for t in types if t not in TYPES]
    if bad or not types:
        raise Stop(f"--types: ComfyFarm takes {' and '.join(TYPES)}, not {', '.join(bad) or 'nothing'}")
    url = item_url(args.farm, args.workflow)
    title = args.title or ("Send to ComfyFarm" + (f": {args.workflow}" if args.workflow else ""))
    try:
        schema = sg.schema_field_read(ENTITY)
    except Exception as e:  # noqa: BLE001 - say what ShotGrid said
        raise Stop(f"ShotGrid wouldn't show the {ENTITY} fields to this script: {type(e).__name__}: {e}") from None
    for f in ("title", "url", "entity_type"):
        if f not in schema:
            raise Stop(f"This site's {ENTITY} has no {f} field: it can't be made from here.")
    if args.folder and "folder" not in schema:
        raise Stop(f"This site's {ENTITY} has no folder field.")
    fields = [f for f in ("title", "url", "entity_type", "selection_required", "folder", "projects", "permissions_groups")
              if f in schema]

    # The existing ones: this farm's URL, on these types.
    existing = {t: [] for t in types}
    for row in sg.find(ENTITY, [["url", "is", url]], fields) or []:
        if row.get("entity_type") in existing:
            existing[row["entity_type"]].append(row)

    if args.remove:
        doomed = [r for t in types for r in existing[t]]
        if not doomed:
            say(f"There's no {ENTITY} posting to {url} on {', '.join(types)}: nothing to remove.")
            return 0
        for r in doomed:
            say(f"{'Deleting' if args.apply else 'Would delete'} {ENTITY} {r['id']}: {r.get('title')!r} on {r.get('entity_type')}")
            if args.apply:
                sg.delete(ENTITY, r["id"])
        say("Done." if args.apply else "\nNothing was changed in ShotGrid. Run it again with --apply to do it.")
        return 0

    # Who sees them.
    limit = {}
    if args.show:
        if "projects" not in schema:
            raise Stop(f"This site's {ENTITY} has no projects field, so it can't be limited to {', '.join(args.show)}.")
        rows = sg.find("Project", [[show_field, "in", args.show]], ["name", show_field]) or []
        found = {str(r.get(show_field)): r for r in rows}
        missing = [s for s in args.show if s not in found]
        if missing:
            raise Stop(f"No ShotGrid project has {show_field} {', '.join(missing)}.")
        limit["projects"] = [{"type": "Project", "id": found[s]["id"], "name": found[s].get("name") or s} for s in args.show]
    if args.group:
        if "permissions_groups" not in schema:
            raise Stop(f"This site's {ENTITY} has no permissions_groups field, so it can't be limited to {args.group!r}.")
        kind = (valid_types(schema, "permissions_groups") or ["PermissionRuleSet"])[0]
        row = None
        for name_field in ("display_name", "code"):
            try:
                row = sg.find_one(kind, [[name_field, "is", args.group]], ["display_name", "code"])
            except Exception:  # noqa: BLE001 - a field this site doesn't have: try the other
                row = None
            if row:
                break
        if not row:
            raise Stop(f"No ShotGrid permission group is called {args.group!r}.")
        limit["permissions_groups"] = [{"type": kind, "id": row["id"], "name": row.get("display_name") or row.get("code")}]
    if not limit and not args.everywhere:
        raise Stop("Neither --show nor --group: that's everyone in every project. Say --everywhere if that's meant.")

    say(f"ShotGrid {ENTITY}s posting to {url}")
    if not limit:
        say("  for everyone, in every project (--everywhere)")
    changes = 0
    for t in types:
        want = {"title": title, "url": url, "entity_type": t}
        if "selection_required" in schema:
            want["selection_required"] = True
        if args.folder:
            want["folder"] = args.folder
        for f in ("projects", "permissions_groups"):
            if f in schema:
                want[f] = limit.get(f, [])
        have = existing[t][0] if existing[t] else None
        if have is None:
            say(f"\n{'Creating' if args.apply else 'Would create'} one on {t}:")
            for k, v in want.items():
                say(f"  {k}: {describe(v)}")
            if args.apply:
                made = sg.create(ENTITY, as_written(want))
                say(f"  made: {ENTITY} {made['id']}")
            changes += 1
            continue
        diff = {k: v for k, v in want.items()
                if (links(v) != links(have.get(k)) if isinstance(v, list) else v != have.get(k))}
        if not diff:
            say(f"\nOn {t}: {ENTITY} {have['id']} is already so.")
        else:
            say(f"\n{'Changing' if args.apply else 'Would change'} {ENTITY} {have['id']} on {t}:")
            for k, v in diff.items():
                say(f"  {k}: {describe(have.get(k))} -> {describe(v)}")
            if args.apply:
                sg.update(ENTITY, have["id"], as_written(diff))
            changes += 1
        for extra in existing[t][1:]:
            say(f"  (another one, {ENTITY} {extra['id']}, posts there too: --remove takes them all away)")
    if not args.apply and changes:
        say("\nNothing was changed in ShotGrid. Run it again with --apply to do it.")
    elif changes:
        say("\nDone. It's in the right-click menu of the Versions and Playlists it's for (reload ShotGrid's page).")
    return 0


def connect(site: str, name: str, key: str):
    import shotgun_api3
    return shotgun_api3.Shotgun(site, script_name=name, api_key=key)


def main(argv=None, env=None, ask_name=input, ask_key=getpass.getpass, connect=connect, out=sys.stdout) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    site, show_field = args.site, "tank_name"
    if not site:
        try:
            from comfyfarm.config import load_config
            sgc = load_config().shotgrid
            site, show_field = sgc.url, sgc.show_field or show_field
        except Exception as e:  # noqa: BLE001
            print(f"No --site, and the manager's config.yaml couldn't be read ({e}).", file=sys.stderr)
            return 1
    key = ""
    scrub = lambda text: text.replace(key, "<key>") if key else text     # noqa: E731 - ShotGrid's errors can quote it
    try:
        name, key = credentials(env, ask_name, ask_key)
        print(f"ShotGrid {site.rstrip('/')} as the script {name}", file=out)
        return run(args, connect(site, name, key), show_field, out)
    except Stop as e:
        print(scrub(str(e)), file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - ShotGrid's own errors
        print(scrub(f"ShotGrid said: {type(e).__name__}: {e}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
