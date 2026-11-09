"""Makes ShotGrid's right-click menu match the farm's (README: ShotGrid right-click):

    ComfyFarm Open ▸ <workflow>          (and Submit ▸ / Submit + Publish ▸ when menu.yaml has them)

on the Versions and Playlists of the shows in deploy/shotgrid/menu.yaml. The farm says what
the menu should be (GET <farm>/api/v1/shotgrid/menu: which workflow goes in which folder,
worked out from their manifests); this makes ShotGrid's Action Menu
Items match it: it creates the missing ones, changes those that differ, and deletes the
ones the farm doesn't have any more (any item posting to the farm). It changes ShotGrid, so
it only shows what it would do until it's run with --apply. Run it again after adding a
workflow or a show (and deploying the manager).

It needs a ShotGrid connection that may create Action Menu Items (the manager's own
read-only script can't, and doesn't need to). Two ways to get one:

The pipeline's own (--pipeline): rdo_publish_pipeline's client, with its permissions, so no
script or key is handled here. Run it where that module is: a rez environment that has it,
or Maya. This file is standalone (standard library only, Python 3.7+): run it from the
farm's checkout, or copy it anywhere.

    # Maya's Script Editor (Python):
    import sys; sys.path.insert(0, "/mntx/raid/pdominik/comfyui-farm/tools")
    import sg_ami
    sg_ami.main(["--pipeline", "--farm", "http://wrk1125l.rodeofx.com:9090"])
    # then the same list with "--apply" added (after changing this file: importlib.reload(sg_ami))

    cd /mntx/raid/pdominik/comfyui-farm
    rez env <the package with it> -- python tools/sg_ami.py --pipeline --farm http://wrk1125l.rodeofx.com:9090

--pipeline-path <folder> imports the module from that folder instead (outside rez; its own
dependencies have to be importable too).

An admin script (the manager, where shotgun_api3 is):

    cd /mntx/raid/pdominik/comfyui-farm
    export SG_ADMIN_SCRIPT_NAME=<the admin script's name>
    PYTHONPATH=. $(cat run/python) tools/sg_ami.py --farm http://wrk1125l.rodeofx.com:9090 [--apply]

The script's key is SG_ADMIN_SCRIPT_KEY if it's set, else it's asked for (not shown as it's
typed). It's used for this run only: never written anywhere, never printed.

--remove deletes every item posting to the farm instead (with --apply).

Before writing, it reads ActionMenuItem's fields from ShotGrid and stops if the site can't
hold what the menu needs (folders, projects)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ENTITY = "ActionMenuItem"
NAME_ENV, KEY_ENV = "SG_ADMIN_SCRIPT_NAME", "SG_ADMIN_SCRIPT_KEY"
PIPELINE = "rdo_publish_pipeline"
AMI_PATH = "/api/v1/shotgrid/ami"
FIELDS = ("title", "url", "entity_type", "folder", "list_order", "selection_required", "projects")


class Stop(Exception):
    """Why it can't go on (printed, exit 1)."""


def parse_args(argv):
    p = argparse.ArgumentParser(prog="sg_ami.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--farm", required=True, help="the manager's address (http://wrk1125l.rodeofx.com:9090): its menu is read "
                                                 "from there")
    p.add_argument("--remove", action="store_true", help="delete every item posting to the farm instead")
    p.add_argument("--apply", action="store_true", help="do it (without: only show what it would do)")
    p.add_argument("--pipeline", action="store_true",
                   help=f"use the pipeline's ShotGrid client ({PIPELINE}: in rez or Maya) instead of an admin script")
    p.add_argument("--pipeline-path", action="append", default=[], metavar="FOLDER",
                   help=f"import {PIPELINE} from this folder (implies --pipeline)")
    p.add_argument("--site", help="the ShotGrid site for an admin script (default: shotgrid.url in the manager's "
                                  "config.yaml); with --pipeline, a check that the pipeline's client is on it")
    args = p.parse_args(argv)
    args.pipeline = args.pipeline or bool(args.pipeline_path)
    return args


def farm_base(farm: str) -> str:
    u = urlsplit(farm)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise Stop(f"--farm {farm!r}: give the manager's address, like http://wrk1125l.rodeofx.com:9090")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


def fetch_menu(farm: str) -> dict:
    """The farm's menu (GET /api/v1/shotgrid/menu), straight (no proxy: it's on the studio's network)."""
    url = farm_base(farm) + "/api/v1/shotgrid/menu"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Stop(f"{url} isn't there: the farm doesn't have the menu yet (deploy the manager first).") from None
        raise Stop(f"{url} answered {e.code}: {e.read()[:300].decode('utf-8', 'replace')}") from None
    except (OSError, ValueError) as e:
        raise Stop(f"The farm didn't answer at {url}: {e}") from None


def credentials(env, ask_name, ask_key) -> tuple[str, str]:
    name = env.get(NAME_ENV) or ask_name("ShotGrid admin script name: ").strip()
    key = env.get(KEY_ENV) or ask_key(f"Key of the ShotGrid script {name} (not shown): ").strip()
    if not (name and key):
        raise Stop(f"No ShotGrid admin script: set {NAME_ENV} (and {KEY_ENV}, or type the key when asked).")
    return name, key


def url_of(item: dict) -> str:
    u = item.get("url")
    return (u.get("url") if isinstance(u, dict) else u) or ""


def links(rows) -> set:
    return {(r.get("type"), r.get("id")) for r in rows or []}


def same(want, have) -> bool:
    if isinstance(want, list):
        return links(want) == links(have)
    return want == have


def as_written(values: dict) -> dict:
    """Entity lists as ShotGrid takes them: [{type, id}]."""
    return {k: ([{"type": r["type"], "id": r["id"]} for r in v] if isinstance(v, list) else v) for k, v in values.items()}


def describe(value) -> str:
    if isinstance(value, list):
        return ", ".join(r.get("name") or f"{r['type']} {r['id']}" for r in value) or "(none)"
    return repr(value)


def ours(sg, prefixes, fields) -> list[dict]:
    """The items posting to the farm (any of its addresses), whatever made them."""
    return sorted((r for r in sg.find(ENTITY, [], list(fields)) or []
                   if any(url_of(r).startswith(p) for p in prefixes)), key=lambda r: r["id"])


def run(args, sg, menu: dict | None, out) -> int:
    say = lambda *a: print(*a, file=out)      # noqa: E731
    base = farm_base(args.farm)
    farm_url = (menu or {}).get("farm_url") or base
    prefixes = sorted({farm_url + AMI_PATH, base + AMI_PATH})
    try:
        schema = sg.schema_field_read(ENTITY)
    except Exception as e:  # noqa: BLE001 - say what ShotGrid said
        raise Stop(f"ShotGrid wouldn't show the {ENTITY} fields to this script: {type(e).__name__}: {e}") from None
    for f in ("title", "url", "entity_type"):
        if f not in schema:
            raise Stop(f"This site's {ENTITY} has no {f} field: it can't be made from here.")
    fields = [f for f in FIELDS if f in schema]
    existing = ours(sg, prefixes, fields)

    if args.remove:
        if not existing:
            say(f"No {ENTITY} posts to {' or '.join(prefixes)}: nothing to remove.")
            return 0
        for r in existing:
            say(f"{'Deleting' if args.apply else 'Would delete'} {ENTITY} {r['id']}: "
                f"{(r['folder'] + ' ▸ ') if r.get('folder') else ''}{r.get('title')!r} on {r.get('entity_type')}")
            if args.apply:
                sg.delete(ENTITY, r["id"])
        say("Done." if args.apply else "\nNothing was changed in ShotGrid. Run it again with --apply to do it.")
        return 0

    if menu.get("problems"):
        raise Stop("The farm's menu has problems; fix them first (deploy/shotgrid/menu.yaml, the manifests):\n  "
                   + "\n  ".join(menu["problems"]))
    items = menu.get("items") or []
    for f, why in (("folder", "its folders"), ("projects", "the shows it's for")):
        if items and f not in schema:
            raise Stop(f"This site's {ENTITY} has no {f} field, so it can't hold {why}.")

    # The shows' projects (their tank_name: the menu's show codes).
    codes = sorted({c for it in items for c in it["shows"]})
    projects = {}
    if codes:
        rows = sg.find("Project", [["tank_name", "in", codes]], ["name", "tank_name"]) or []
        projects = {str(r.get("tank_name")): {"type": "Project", "id": r["id"], "name": r.get("tank_name") or r.get("name")}
                    for r in rows}
        missing = [c for c in codes if c not in projects]
        if missing:
            raise Stop(f"No ShotGrid project has tank_name {', '.join(missing)} (deploy/shotgrid/menu.yaml).")

    say(f"ShotGrid's ComfyFarm menu, posting to {farm_url}{AMI_PATH}")
    by_key = {}
    for r in existing:
        by_key.setdefault((r.get("entity_type"), url_of(r)), []).append(r)
    counts = {"create": 0, "change": 0, "delete": 0, "keep": 0}
    folder = None
    for it in sorted(items, key=lambda i: (i["list_order"], i["entity_type"] != "Version")):
        want = {"title": it["title"], "url": it["url"], "entity_type": it["entity_type"], "folder": it["folder"],
                "list_order": it["list_order"], "selection_required": True,
                "projects": [projects[c] for c in it["shows"]]}
        want = {k: v for k, v in want.items() if k in schema}
        if it["folder"] != folder:
            folder = it["folder"]
            say(f"\n{folder} ▸")
        have = by_key.pop((it["entity_type"], it["url"]), [])
        label = f"  {it['title']}  on {it['entity_type']}  [{', '.join(it['shows'])}]"
        if not have:
            counts["create"] += 1
            say(f"{label}: {'creating' if args.apply else 'would create'}")
            if args.apply:
                made = sg.create(ENTITY, as_written(want))
                say(f"    made {ENTITY} {made['id']}")
        else:
            diff = {k: v for k, v in want.items() if not same(v, have[0].get(k))}
            if diff:
                counts["change"] += 1
                say(f"{label}: {'changing' if args.apply else 'would change'} {ENTITY} {have[0]['id']} ("
                    + "; ".join(f"{k} {describe(have[0].get(k))} -> {describe(v)}" for k, v in diff.items()) + ")")
                if args.apply:
                    sg.update(ENTITY, have[0]["id"], as_written(diff))
            else:
                counts["keep"] += 1
                say(f"{label}: as it is ({ENTITY} {have[0]['id']})")
            for extra in have[1:]:                     # a second one for the same: one is enough
                by_key.setdefault(("", ""), []).append(extra)
    stale = sorted((r for rows in by_key.values() for r in rows), key=lambda r: r["id"])
    if stale:
        say("\nNot in the farm's menu (any more):")
    for r in stale:
        counts["delete"] += 1
        say(f"  {(r['folder'] + ' ▸ ') if r.get('folder') else ''}{r.get('title')!r} on {r.get('entity_type')}: "
            f"{'deleting' if args.apply else 'would delete'} {ENTITY} {r['id']}")
        if args.apply:
            sg.delete(ENTITY, r["id"])
    left = [w for w in menu.get("workflows") or [] if not w.get("submit") and w.get("open")]
    if left and any(f != "ComfyFarm Open" for f in menu.get("folders") or []):       # the Submit folders are on
        say("\nOnly in Open: "
            + "; ".join(f"{w['label']} ({w['why']})" for w in left))
    out_ = [w for w in menu.get("workflows") or [] if not w.get("open")]
    if out_:
        say("Not in the menu: " + "; ".join(f"{w['label']} ({w['why']})" for w in out_))
    changes = counts["create"] + counts["change"] + counts["delete"]
    say(f"\n{counts['create']} to create, {counts['change']} to change, {counts['delete']} to delete, "
        f"{counts['keep']} as they are." if not args.apply else
        f"\nDone: {counts['create']} created, {counts['change']} changed, {counts['delete']} deleted. Reload "
        "ShotGrid's page to see them.")
    if not args.apply and changes:
        say("Nothing was changed in ShotGrid. Run it again with --apply to do it.")
    return 0


def connect(site: str, name: str, key: str):
    import shotgun_api3
    return shotgun_api3.Shotgun(site, script_name=name, api_key=key)


def pipeline_client(paths=()):
    """The pipeline's own ShotGrid connection, as its tools get it:
    rdo_publish_pipeline.manager.ClientRegistry().get_client().sg. `paths`: folders to
    import it from first (outside rez)."""
    for folder in reversed(list(paths)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    try:
        from rdo_publish_pipeline import manager
    except ImportError as e:
        raise Stop(f"{PIPELINE} can't be imported here ({e}). Run it in a rez environment that has it, or in "
                   "Maya, or give --pipeline-path <the folder holding it>.") from None
    try:
        sg = manager.ClientRegistry().get_client().sg
    except Exception as e:  # noqa: BLE001 - whatever the pipeline says
        raise Stop(f"{PIPELINE} couldn't make its ShotGrid client: {type(e).__name__}: {e}") from None
    if sg is None:
        raise Stop(f"{PIPELINE}'s client has no ShotGrid connection (its sg is None).")
    return sg


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def main(argv=None, env=None, ask_name=input, ask_key=getpass.getpass, connect=connect, pipeline=pipeline_client,
         fetch=fetch_menu, out=None) -> int:
    """The command line; from Maya: main(["--pipeline", "--farm", …]). 0 when done, 1 when not."""
    out = out or sys.stdout
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    secret = []                               # the key, to keep it out of ShotGrid's errors
    scrub = lambda text: text.replace(secret[0], "<key>") if secret and secret[0] else text     # noqa: E731
    try:
        menu = None if args.remove else fetch(args.farm)
        if args.pipeline:
            sg = pipeline(args.pipeline_path)
            conf = getattr(sg, "config", None)
            secret.append(str(getattr(conf, "api_key", None) or ""))
            site = str(getattr(sg, "base_url", None) or "")
            if args.site and _host(site) != _host(args.site):
                raise Stop(f"{PIPELINE}'s ShotGrid is {site or 'unknown'}, not {args.site}: nothing done.")
            who = getattr(conf, "script_name", None) or getattr(conf, "user_login", None)
            print(f"ShotGrid {site.rstrip('/') or '(its site)'} through {PIPELINE}"
                  + (f", as {who}" if who else ""), file=out)
            return run(args, sg, menu, out)
        site = args.site
        if not site:
            try:
                from comfyfarm.config import load_config
                site = load_config().shotgrid.url
            except Exception as e:  # noqa: BLE001
                raise Stop(f"No --site, and the manager's config.yaml couldn't be read ({e}).") from None
        name, key = credentials(env, ask_name, ask_key)
        secret.append(key)
        print(f"ShotGrid {site.rstrip('/')} as the script {name}", file=out)
        return run(args, connect(site, name, key), menu, out)
    except Stop as e:
        print(scrub(str(e)), file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - ShotGrid's own errors
        print(scrub(f"ShotGrid said: {type(e).__name__}: {e}"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
