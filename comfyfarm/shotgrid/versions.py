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
from urllib.parse import urlsplit

from ..fileio import paths as P

__all__ = ["parse", "same_site", "pad_hash", "input_text"]

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


def same_site(ref: dict, site_url: str) -> bool:
    """The link is to the farm's ShotGrid site (shotgrid.url)."""
    return ref["host"].lower() == (urlsplit(site_url).hostname or "").lower()


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
