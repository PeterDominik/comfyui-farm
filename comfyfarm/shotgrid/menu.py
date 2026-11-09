"""The farm in ShotGrid's right-click menu: a submission form, like the Nuke panel's, shaped
by what a menu can do. A folder of Action Menu Items, a workflow each:

  ComfyFarm Open ▸        on Versions and Playlists: the dashboard with the workflow, and the
                          Version (the Submit form) or the rows (Batch Submit) loaded, Publish
                          when done ticked for a workflow that publishes (menu.yaml publish:)

Two more folders can be switched on (menu.yaml menus:), for when ShotGrid can show the result
over its own page (the manager on https, its modal overlay; until then every click opens a
new tab, so Open is enough):

  ComfyFarm Submit ▸              on Versions: one is submitted at once (the tab then shows
                                  the job in the dashboard); several open Batch Submit, which
                                  asks to send them
  ComfyFarm Submit + Publish ▸    the same, published when done (Daiquiri)

Where a workflow goes (fit), from its manifest:
  Open        it has an image input (a Version's link fills it)
  Submit      a Version is all it needs: its only required input takes frames (not one
              image, which needs its frame picked) and every other field is optional;
              `shotgrid: {submit: false}` keeps it out
  + Publish   one of Submit's that publishes (publishing on)
  `shotgrid: false` keeps a workflow out of the menu altogether.

Which shows have it, and the rest: deploy/shotgrid/menu.yaml (shotgrid.menu_file), each show
with every workflow that fits, or a list of them. GET /api/v1/shotgrid/menu gives the items;
ShotGrid's are made to match by tools/sg_ami.py (it needs a script that may create them)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode

import yaml

__all__ = ["FOLDERS", "fit", "load", "build"]

FOLDERS = (("submit", "ComfyFarm Submit"), ("publish", "ComfyFarm Submit + Publish"), ("open", "ComfyFarm Open"))
_SHOW = re.compile(r"^[A-Za-z0-9_]+$")


def fit(wf, publishes: bool) -> dict:
    """{open, submit, publish, why}: which folders a workflow goes in, and why not the
    others (why it can't submit from a Version alone, or isn't in the menu)."""
    out = {"open": False, "submit": False, "publish": False, "why": ""}
    if not wf.enabled:
        out["why"] = "it's disabled: " + "; ".join(wf.problems)
        return out
    if wf.shotgrid is False:
        out["why"] = "shotgrid: false in its manifest"
        return out
    port = wf.port
    if port is None:
        out["why"] = "it has no image input for a Version"
        return out
    out["open"] = True
    opts = wf.shotgrid if isinstance(wf.shotgrid, dict) else {}
    why = []
    if opts.get("submit") is False:
        why.append("shotgrid: {submit: false} in its manifest")
    others = [p for p in wf.other_ports if p.required]
    if others:
        why.append("it also needs " + ", ".join(wf.params[p.name].label or p.name for p in others))
    if port.takes == "image":
        why.append("it takes one image, so a Version's frame has to be picked")
    needs = [p.label or p.name for n, p in wf.params.items()
             if n not in wf.ports and p.required and p.default in (None, "")]
    if needs:
        why.append("it needs " + ", ".join(needs))
    out["submit"] = not why
    out["publish"] = out["submit"] and bool(publishes)
    out["why"] = "; ".join(why)
    return out


def load(path) -> dict:
    """The menu's settings from its file: {shows: {show: "all" | [workflow, …]}, farm_url (or
    None), menus: the folders it has (open, submit, publish; default open), publish: whether
    Open ticks Publish when done (default true), problems}. A file that isn't there: no shows."""
    p = Path(path)
    out = {"shows": {}, "farm_url": None, "menus": ["open"], "publish": True, "problems": []}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {**out, "problems": [f"{p} isn't there: no show has the menu."]}
    except (OSError, yaml.YAMLError) as e:
        return {**out, "problems": [f"{p} can't be read: {e}"]}
    if not isinstance(data, dict):
        return {**out, "problems": [f"{p}: needs shows: and farm_url:"]}
    problems, shows = out["problems"], out["shows"]
    raw = data.get("shows") or {}
    if isinstance(raw, list):                       # shows: [raa, bri3]
        raw = {s: "all" for s in raw}
    if not isinstance(raw, dict):
        problems.append(f"{p}: shows: needs to be a list of show codes, or show: all | [workflows]")
        raw = {}
    for code, which in raw.items():
        code = str(code)
        if not _SHOW.match(code):
            problems.append(f"{p}: {code!r} isn't a show code")
        elif which in (None, "all", True):
            shows[code] = "all"
        elif isinstance(which, list) and all(isinstance(w, str) for w in which):
            shows[code] = list(which)
        else:
            problems.append(f"{p}: {code}: needs to be all, or a list of workflows")
    url = data.get("farm_url")
    if url is not None and not (isinstance(url, str) and re.match(r"^https?://[^/\s]+", url)):
        problems.append(f"{p}: farm_url {url!r} isn't the manager's address (http://host:port)")
        url = None
    out["farm_url"] = url.rstrip("/") if url else None
    menus = data.get("menus", ["open"])
    known = [a for a, _ in FOLDERS]
    if not (isinstance(menus, list) and menus and all(m in known for m in menus)):
        problems.append(f"{p}: menus: needs to be a list of {', '.join(known)}")
    else:
        out["menus"] = [a for a in known if a in menus]
    if not isinstance(data.get("publish", True), bool):
        problems.append(f"{p}: publish: needs to be true or false")
    else:
        out["publish"] = data.get("publish", True)
    return out


def build(workflows, publish_info, shows: dict, farm_url: str, menus=("open",)) -> dict:
    """The menu: {farm_url, shows, folders, items: [{entity_type, folder, title, url,
    list_order, shows, workflow, action}], workflows: [{name, label, open, submit,
    publish, why}], problems}. `workflows` in the dashboard's order; `publish_info(wf)`
    the publisher's (None: it doesn't publish); `menus` the folders it has (FOLDERS' actions)."""
    base = farm_url.rstrip("/")
    fits, out_wfs, problems = {}, [], []
    for wf in workflows:
        f = fits[wf.name] = fit(wf, publish_info(wf) is not None)
        out_wfs.append({"name": wf.name, "label": wf.label, **f})
    known = {wf.name for wf in workflows}
    for code, which in shows.items():
        for name in [] if which == "all" else which:
            if name not in known:
                problems.append(f"show {code}: there's no workflow {name}")
    items = []
    for fi, (action, folder) in enumerate(FOLDERS):
        if action not in menus:
            continue
        for wi, wf in enumerate(workflows):
            if not fits[wf.name][action]:
                continue
            on = [code for code, which in shows.items() if which == "all" or wf.name in which]
            if not on:
                continue
            opts = wf.shotgrid if isinstance(wf.shotgrid, dict) else {}
            for kind in ("Version", "Playlist") if action == "open" else ("Version",):
                items.append({"entity_type": kind, "folder": folder, "title": opts.get("title") or wf.label,
                              "url": f"{base}/api/v1/shotgrid/ami?" + urlencode({"action": action, "workflow": wf.name}),
                              "list_order": (fi + 1) * 100 + wi, "shows": on, "workflow": wf.name, "action": action})
    return {"farm_url": base, "shows": shows, "folders": [f for a, f in FOLDERS if a in menus], "items": items,
            "workflows": out_wfs, "problems": problems}
