"""A ShotGrid Version link as a job's input.

Anywhere a job takes an input path (the dashboard, the Nuke panel, the client), it also
takes a Version's link, as the browser shows it:

  https://rodeofx.shotgrid.autodesk.com/detail/Version/4865327
  https://rodeofx.shotgrid.autodesk.com/page/9921#Version_4865327      (a page with it open)

optionally followed by frames, as after a path: `<link> 1001-1050`, or `<link> 1050` for
one frame. The manager asks ShotGrid for the Version (read only, ShotGrid.version) and
the job reads:

- its frames (shotgrid.version_fields.frames: the published images), over the Version's
  frame range unless frames were given;
- else its movie (…movie): when it has no frames, or they aren't on disk.

The Version decides the show, sequence and shot (its entity), not the path: the output
folder and the publish fields follow it. The job keeps the link as its parameter (Run
again asks ShotGrid again) and records the Version in info.sources."""

from __future__ import annotations

import os
import re
from email.policy import default as email_default
from email.parser import BytesParser
from urllib.parse import parse_qs, urlencode, urlsplit

from ..fileio import paths as P

__all__ = ["parse", "parse_playlist", "version_ids", "picked", "deep_link", "action_target", "form_fields", "same_site",
           "pad_hash", "input_text"]

# http(s)://<site>/…/detail/Version/<id> or …#Version_<id>, then optional frames
_LINK = re.compile(r"^(?P<link>https?://\S*?(?:/detail/Version/|#Version_)(?P<id>\d+)\S*?)"
                   r"(?:\s+(?P<first>-?\d+)(?:\s*-\s*(?P<last>-?\d+))?)?\s*$", re.I)


def parse(raw) -> dict | None:
    """{id, link, host, first, last} of a Version link (with frames after it), or None when
    `raw` isn't one (a path, anything else)."""
    if not isinstance(raw, str):
        return None
    m = _LINK.match(raw.strip())
    if not m:
        return None
    first = int(m["first"]) if m["first"] is not None else None
    last = int(m["last"]) if m["last"] is not None else first
    return {"id": int(m["id"]), "link": m["link"], "host": urlsplit(m["link"]).hostname or "",
            "first": first, "last": last}


_PLAYLIST = re.compile(r"^(?P<link>https?://\S*?(?:/detail/Playlist/|#Playlist_)(?P<id>\d+)\S*?)\s*$", re.I)


def parse_playlist(raw) -> dict | None:
    """{id, link, host} of a Playlist link (…/detail/Playlist/<id>, …#Playlist_<id>), or of
    a bare playlist id (host ""), or None."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.isdigit():
        return {"id": int(text), "link": text, "host": ""}
    m = _PLAYLIST.match(text)
    return {"id": int(m["id"]), "link": m["link"], "host": urlsplit(m["link"]).hostname or ""} if m else None


MAX_PICKED = 500                 # Versions picked at once (a right-click on a selection)


def version_ids(raw, what: str = "Version") -> list[int]:
    """Ids as a list ('1,2, 3' or [1, 2, 3]), in order, each once; ValueError when one
    isn't an id, or there are none or too many."""
    parts = raw if isinstance(raw, list) else str(raw or "").replace(",", " ").split()
    ids = []
    for p in parts:
        if not str(p).strip().isdigit() or int(p) <= 0:
            raise ValueError(f"{p!r} isn't a {what} id.")
        ids.append(int(p))
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise ValueError(f"No {what}s given.")
    if len(ids) > MAX_PICKED:
        raise ValueError(f"That's {len(ids)} {what}s; Batch Submit takes up to {MAX_PICKED} at once.")
    return ids


_WORKFLOW = re.compile(r"^[\w.-]{1,80}$")
_LOGIN = re.compile(r"^[\w.@+-]{1,64}$")
ACTIONS = ("open", "submit", "publish")


def picked(form: dict, site_url: str) -> dict:
    """What ShotGrid's right-click (an Action Menu Item posting to /api/v1/shotgrid/ami)
    picked: {kind, ids, links, action, workflow, user, publish}. `form` is what ShotGrid posts
    (entity_type, selected_ids, server_hostname, user_login…; README: ShotGrid right-click),
    plus the item's own URL's action (open, submit, publish: submit and publish when
    done) and workflow (comfyfarm.shotgrid.menu). The links are to the farm's ShotGrid
    (`site_url`). ValueError with why, for the page the person sees instead (another site,
    nothing selected, a Shot)."""
    get = lambda k: str(form.get(k) or "").strip()     # noqa: E731
    host = (urlsplit(site_url).hostname or "").lower()
    posted = get("server_hostname").lower()
    if posted and host and posted != host:
        raise ValueError(f"That came from {posted}; this farm uses {site_url.rstrip('/')}.")
    kind = get("entity_type")
    if kind not in ("Version", "Playlist"):
        raise ValueError(f"ComfyFarm takes Versions and Playlists; that was {'a ' + kind if kind else 'something else'}.")
    ids = get("selected_ids") or get("ids")
    if not ids:
        raise ValueError(f"Nothing was selected: select a {kind}, then right-click it.")
    ids = version_ids(ids, kind)
    if kind == "Playlist":
        ids = ids[:1]                            # one playlist per table
    site = site_url.rstrip("/") or f"https://{posted}"
    action = get("action") if get("action") in ACTIONS else "open"
    return {"kind": kind, "ids": ids, "links": [f"{site}/detail/{kind}/{i}" for i in ids],
            "action": "open" if kind == "Playlist" else action,          # a Playlist only opens
            "workflow": get("workflow") if _WORKFLOW.match(get("workflow")) else None,
            "user": get("user_login") if _LOGIN.match(get("user_login")) else None,
            "publish": False}                    # Publish when done ticked: the manager decides


def deep_link(p: dict) -> str:
    """Where the dashboard opens for what was picked (picked()), as its deep link:

      one Version         #submit?input=<its link>                 the Submit form, its input filled
      several Versions    #batch?versions=<id>,<id>,…              Batch Submit, a row each
      a Playlist          #batch?playlist=<its link>               Batch Submit, its Versions

    with the workflow, and the ShotGrid login as the dashboard's user when it has none.
    publish=1 ticks Publish when done (`publish`, or Submit + Publish). Submit (+ Publish)
    on several Versions also asks to send them (send=1); on one, the manager submits it
    itself (here: when it can't, the form)."""
    q = {"workflow": p["workflow"]} if p.get("workflow") else {}
    if p.get("user"):
        q["user"] = p["user"]
    submit = p.get("action") in ("submit", "publish")
    if p.get("publish") or p.get("action") == "publish":
        q["publish"] = "1"
    if p["kind"] == "Playlist":
        return "#batch?" + urlencode({**q, "playlist": p["links"][0]})
    if len(p["ids"]) == 1:
        return "#submit?" + urlencode({**q, "input": p["links"][0]})
    return "#batch?" + urlencode({**q, **({"send": "1"} if submit else {}),
                                  "versions": ",".join(map(str, p["ids"]))}, safe=",")


def action_target(form: dict, site_url: str) -> str:
    """The deep link for what ShotGrid's right-click posted (picked, then deep_link)."""
    return deep_link(picked(form, site_url))


def form_fields(content_type: str, body: bytes) -> dict[str, str]:
    """A posted form's fields (what ShotGrid's right-click posts): url-encoded, or
    multipart's plain fields, without needing python-multipart. The last value of a
    repeated field."""
    ctype = content_type.split(";")[0].strip().lower()
    if ctype == "multipart/form-data":
        msg = BytesParser(policy=email_default).parsebytes(b"Content-Type: " + content_type.encode("latin-1")
                                                           + b"\r\n\r\n" + body)
        out = {}
        for part in msg.iter_parts() if msg.is_multipart() else []:
            name = part.get_param("name", header="content-disposition")
            if name and not part.get_filename():
                out[name] = part.get_content() if part.get_content_maintype() == "text" else ""
        return out
    return {k: v[-1] for k, v in parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True).items()}


def same_site(ref: dict, site_url: str) -> bool:
    """The link is to the farm's ShotGrid site (shotgrid.url); a bare id is."""
    return not ref["host"] or ref["host"].lower() == (urlsplit(site_url).hostname or "").lower()


def pad_hash(path: str, pc, listdir=os.listdir) -> str:
    """ShotGrid's frame token is a lone `#`, whatever the padding ('x.#.exr'). As a farm
    pattern one # is one digit, so a 4-padded frame 977 would be looked for as x.977.exr:
    it becomes as many #s as the files on disk are padded to (x.####.exr). Unchanged when
    there's nothing to tell (no zero-padded file, nothing listed)."""
    folder, _, name = path.rpartition("/")
    if name.count("#") != 1:
        return path
    pre, post = name.split("#")
    try:
        names = listdir(P.to_farm(folder, pc.mappings) or folder)
    except OSError:
        return path
    widths = set()
    for n in names:
        if n.startswith(pre) and n.endswith(post) and len(n) > len(pre) + len(post):
            digits = n[len(pre):len(n) - len(post)]
            if digits.isdigit() and len(digits) > 1 and digits.startswith("0"):
                widths.add(len(digits))
    return f"{folder}/{pre}{'#' * max(widths)}{post}" if widths else path


def input_text(path: str, first: int | None, last: int | None) -> str:
    """A path and frames as an input field holds them: '<path> 1001-1084', '<path> 1050'."""
    if first is None:
        return path
    return f"{path} {first}" if last in (None, first) else f"{path} {first}-{last}"
