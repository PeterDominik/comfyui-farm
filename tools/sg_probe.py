"""What ShotGrid holds for a few Versions (read only): the fields chunk 11 builds on.

Run on the manager, with its Python and its ShotGrid script (the same SG_SCRIPT_NAME /
SG_SCRIPT_KEY the manager uses: its environment or ~/.config/comfyfarm/shotgrid.env):

    cd /mntx/raid/pdominik/comfyui-farm
    PYTHONPATH=. $(cat run/python) tools/sg_probe.py <Version link or id> [...] > sg_probe.json

It only reads (schema_field_read, find, find_one): nothing in ShotGrid is changed. The
output is JSON: the Version entity's fields and their types, then for each Version every
field that has a value (signed media URLs shortened), its Shot and Project. (Rodeo's
site has TankPublishedFile, not PublishedFile: the Version's tank_published_file field
names it.) The key never appears in it."""

from __future__ import annotations

import json
import re
import sys

from comfyfarm.config import load_config
from comfyfarm.shotgrid.lookup import ShotGrid, Unavailable

LINK = re.compile(r"(?:/detail/Version/|[#&/]Version_)(\d+)", re.I)


def version_id(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        return int(text)
    m = LINK.search(text)
    return int(m.group(1)) if m else None


def tidy(value):
    """Values as JSON can hold them; media URLs (signed, short-lived) cut down."""
    if isinstance(value, dict):
        if value.get("url") and value.get("link_type"):       # a file/link field
            return {k: v for k, v in value.items() if k not in ("url",)} | {"url": "<url>"}
        return {k: tidy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [tidy(v) for v in value]
    if isinstance(value, str) and value.startswith("http") and ("AWSAccessKeyId" in value or "Signature" in value
                                                                or "X-Amz" in value):
        return "<signed url>"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def has_value(v) -> bool:
    return v not in (None, "", [], {}) and v is not False


def also(sg, *args):
    """A lookup that may not work on every site (a field or entity it doesn't have): its
    answer, or why not, without losing the rest."""
    try:
        return tidy(sg._ask(*args))
    except Unavailable as e:
        return {"error": str(e)}


def main(argv: list[str]) -> int:
    ids = [(a, version_id(a)) for a in argv]
    bad = [a for a, i in ids if i is None]
    if not ids or bad:
        print(f"usage: sg_probe.py <Version link or id> [...]  (not a Version: {', '.join(bad) or 'none given'})",
              file=sys.stderr)
        return 2
    cfg = load_config()
    sg = ShotGrid(cfg.shotgrid)
    why = sg.setup_why()
    if why:
        print(why, file=sys.stderr)
        return 1
    out: dict = {"site": cfg.shotgrid.url, "asked": [a for a, _ in ids]}
    try:
        schema = sg._ask("schema_field_read", "Version")
        out["version_fields"] = {name: {"type": f.get("data_type", {}).get("value"),
                                        "name": f.get("name", {}).get("value"),
                                        **({"entity_types": f["properties"]["valid_types"]["value"]}
                                           if f.get("properties", {}).get("valid_types") else {})}
                                 for name, f in sorted(schema.items())}
        out["versions"] = []
        for asked, vid in ids:
            v = sg._ask("find_one", "Version", [["id", "is", vid]], sorted(schema))
            if not v:
                out["versions"].append({"asked": asked, "id": vid, "found": False})
                continue
            entry = {"asked": asked, "id": vid, "found": True,
                     "fields": {k: tidy(x) for k, x in sorted(v.items()) if has_value(x)}}
            ent = v.get("entity") or {}
            if ent.get("type") and ent.get("id"):
                entry["entity"] = also(sg, "find_one", ent["type"], [["id", "is", ent["id"]]],
                                       ["code", "sg_sequence", "sg_status_list", "project", "sg_cut_in", "sg_cut_out",
                                        "sg_head_in", "sg_tail_out", "sg_cut_duration", "sg_frame_rate", "description"])
            proj = v.get("project") or {}
            if proj.get("id"):
                entry["project"] = also(sg, "find_one", "Project", [["id", "is", proj["id"]]],
                                        ["name", "tank_name", "code", "sg_status", "sg_frame_rate"])
            out["versions"].append(entry)
    except Unavailable as e:
        print(str(e), file=sys.stderr)
        return 1
    json.dump(out, sys.stdout, indent=1, sort_keys=False, default=str)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
