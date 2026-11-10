"""Batch Submit's ShotGrid query (README: Batch Submit): a show's Versions found by a few
named filters, each Version a row. Read only, with the manager's own ShotGrid script.

The browser never sends ShotGrid filters. It names a show and picks among the values the
manager offered for it (options()), and the manager builds the filters itself (build()):
a value it didn't offer is refused, and nothing else reaches ShotGrid.

options(show), what the query panel offers for a show:
  sequences, shots  the show's, as the publish fields list them (the shots' hidden
                    statuses left out)
  filters           status (sg_status_list), then shotgrid.query.fields (step, task, type,
                    department), each a Version field the site has. Each offers the values
                    the show's Versions have, with how many (ShotGrid's summarize), labelled
                    as ShotGrid labels them; a linked field (a Task) by its linked name. A
                    text or linked field ShotGrid can't sum up, or with too many values to
                    list, is typed instead ("contains"); a list (status, Rodeo's type and
                    department) is always picked from, since ShotGrid takes no "contains"
                    on a list. A field the site doesn't have, or of another kind (a number,
                    a date), is left out, and the panel says so.
  cached options_ttl seconds per show (the panel's ↻ asks again).

run(spec), the Versions:
  - in the show, of its Shots (not assets), leaving out the shots whose status the lists
    hide (hld omt hdn void), with frames or a movie (a row needs one);
  - in the sequences and shots asked (a shot must be in the sequences, when both are
    given; globs work: bpa_14*);
  - whose code contains any of the words asked (name: "mp01, plate");
  - with the values picked (any of them, per filter), created in the last N days;
  - with `latest`, only the latest of each shot and name: the name is the code without
    its version (yht_1040.lineup.autoLineup.v3 -> yht_1040.lineup.autoLineup; a movie's
    extension goes too), the latest the highest sg_version_number, else the code's vN,
    else the newest.
  The newest shotgrid.query.scan Versions are looked at, and max_rows given at most, in
  shot and code order; when there are more, it says so."""

from __future__ import annotations

import fnmatch
import re
import time
from datetime import datetime, timedelta, timezone

from .lookup import Refused, Unavailable

__all__ = ["options", "run", "normalize", "build", "latest", "name_key", "describe"]

NAME_FIELDS = {"Task": "content", "HumanUser": "name", "ClientUser": "name", "ApiUser": "firstname"}
MAX_VALUES = 300            # values listed for a filter; more and it's typed instead
MAX_PICKED = 300            # values picked for one filter
MAX_CODES = 2000            # shots a query can name (sequences expand to their shots)
MAX_WORDS = 10
_CODE = re.compile(r"^[\w.*?\[\]-]{1,64}$")
_SHOW = re.compile(r"^[\w.-]{1,64}$")
_KEY = re.compile(r"^\w{1,32}$")
_EXT = re.compile(r"\.(mov|mp4|m4v|avi|mxf|webm|exr|dpx|jpe?g|png|tiff?)$", re.I)
_VER = re.compile(r"[._-]?v(\d+)$", re.I)


# ---------------------------------------------------------------- the fields offered
def describe(key: str, field: str, info: dict | None) -> dict:
    """How a Version field is filtered on: {key, field, label, type, path, kind, schema}
    (path: what the filter names, a linked field's name through the link; schema: a list
    field's values and labels, else None), or {key, field, label, why} when it can't be."""
    label = ((info or {}).get("name") or {}).get("value") or key.capitalize()
    if not info:
        return {"key": key, "field": field, "label": label,
                "why": f"Version has no field {field} on this site (shotgrid.query.fields)"}
    kind = (info.get("data_type") or {}).get("value")
    props = info.get("properties") or {}
    base = {"key": key, "field": field, "label": label, "type": kind, "kind": "choice", "schema": None}
    if kind in ("status_list", "list"):
        shown = (props.get("display_values") or {}).get("value") or {}
        values = (props.get("valid_values") or {}).get("value") or []
        return {**base, "path": field, "schema": [{"value": str(v), "label": str(shown.get(v) or v)} for v in values]}
    if kind == "text":
        return {**base, "path": field}
    if kind == "entity":
        types = (props.get("valid_types") or {}).get("value") or []
        if not types:
            return {"key": key, "field": field, "label": label, "why": f"{field} links to nothing ShotGrid names"}
        return {**base, "path": f"{field}.{types[0]}.{NAME_FIELDS.get(types[0], 'code')}"}
    return {"key": key, "field": field, "label": label,
            "why": f"{field} is a {kind} field: only lists, text and links are offered"}


def _base(project_id: int, cfg) -> list:
    """What every query and its values are within: the show's Shots' Versions, the shots
    the lists hide left out, with frames or a movie."""
    f = [["project", "is", {"type": "Project", "id": project_id}], ["entity", "type_is", "Shot"]]
    if cfg.hidden_statuses:
        f.append(["entity.Shot.sg_status_list", "not_in", list(cfg.hidden_statuses)])
    media = [cfg.version_fields.get(k) for k in ("frames", "movie") if cfg.version_fields.get(k)]
    if media:
        f.append({"filter_operator": "any", "filters": [[m, "is_not", None] for m in media]})
    return f


def _values(sg, d: dict, base: list) -> list[dict] | None:
    """The values a field has among the show's Versions, with how many: [{value, label,
    count}] by label; None when ShotGrid can't sum the field up."""
    try:
        r = sg.ask("summarize", "Version", base, [{"field": "id", "type": "count"}],
                   grouping=[{"field": d["path"], "type": "exact", "direction": "asc"}])
    except Refused:
        return None
    labels = {v["value"]: v["label"] for v in d["schema"] or []}
    out = {}
    for g in (r or {}).get("groups") or []:
        value = g.get("group_value")
        if isinstance(value, dict):
            value = value.get("name") or g.get("group_name")
        if value is None or str(value).strip() == "":
            continue
        value = str(value)
        n = (g.get("summaries") or {}).get("id")
        n = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) else 0
        if value in out:                                   # one name, several linked entities
            out[value]["count"] += n
        else:
            out[value] = {"value": value, "label": labels.get(value) or str(g.get("group_name") or value), "count": n}
    return sorted(out.values(), key=lambda v: v["label"].lower())


def _filters(sg, show: dict, refresh: bool) -> dict:
    """{filters: [as describe() gives, with values], left_out: [{key, label, why}],
    fetched_at}: cached per show."""
    qc = sg.cfg.query
    key = show["code"].lower()
    cached = sg.query_options.get(key)
    if cached and not refresh and time.time() - cached[0] < qc.options_ttl:
        return cached[1]
    schema = sg.schema("Version")
    base = _base(show["id"], sg.cfg)
    found, left = [], []
    for k, field in (("status", "sg_status_list"), *[(k, f) for k, f in qc.fields.items() if k != "status"]):
        d = describe(k, field, schema.get(field))
        if "why" in d:
            left.append({"key": k, "label": d["label"], "why": d["why"]})
            continue
        values = _values(sg, d, base)
        if values is None and d["schema"] is not None:      # a list: all its values, without counts
            values = [{**v, "count": None} for v in d["schema"]]
        if values is None:
            d.update(kind="text", note="typed: ShotGrid couldn't list its values")
        elif len(values) > MAX_VALUES and d["schema"] is None:    # a list stays a list: no "contains" on one
            d.update(kind="text", note=f"typed: {len(values)} values, too many to list")
        elif not values:
            left.append({"key": k, "label": d["label"], "why": f"none of {show['code']}'s Versions has one"})
            continue
        else:
            d["values"] = values
        found.append(d)
    out = {"filters": found, "left_out": left, "fetched_at": time.time()}
    sg.query_options[key] = (time.time(), out)
    return out


def _public(d: dict) -> dict:
    return {k: d[k] for k in ("key", "field", "label", "kind", "values", "note") if k in d}


def options(sg, show: str, refresh: bool = False) -> dict:
    """What the query panel offers for a show: {show, why, sequences: [{code, shots}],
    shots: [{code, sequence}], filters: [{key, field, label, kind, values: [{value, label,
    count}], note}], left_out: [{key, label, why}], fetched_at}. `why` says what's wrong,
    and the rest is empty, when ShotGrid can't be asked or doesn't have the show."""
    out = {"show": show, "why": None, "set_up": sg.setup_why() is None, "sequences": [], "shots": [],
           "filters": [], "left_out": [], "fetched_at": None}
    shots = sg.shots(show, refresh)
    if shots["why"] or not shots["known_show"]:
        out["why"] = shots["why"] or f"{show} isn't one of the active shows in ShotGrid."
        return out
    out["show"] = shots["show"]
    counts: dict[str, int] = {}
    for s in shots["shots"]:
        if s["sequence"]:
            counts[s["sequence"]] = counts.get(s["sequence"], 0) + 1
    out["sequences"] = [{"code": q, "shots": counts.get(q, 0)} for q in shots["sequences"]]
    out["shots"] = [{"code": s["code"], "sequence": s["sequence"]} for s in shots["shots"]]
    try:
        f = _filters(sg, sg.project(shots["show"]), refresh)
    except Unavailable as e:                     # Refused too: the schema
        out["why"] = str(e)
        return out
    out.update(filters=[_public(d) for d in f["filters"]], left_out=f["left_out"], fetched_at=f["fetched_at"])
    return out


# ---------------------------------------------------------------- the query
def _list(value, what: str, limit: int) -> list[str]:
    if value is None or value == "":
        return []
    items = re.split(r"[,\s]+", value) if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or any(isinstance(x, (dict, list, tuple, bool)) for x in items):
        raise ValueError(f"{what}: a list of names, or names separated by commas.")
    out = [str(x).strip() for x in items if str(x).strip()]
    if len(out) > limit:
        raise ValueError(f"{what}: at most {limit}.")
    return list(dict.fromkeys(out))


def normalize(spec: dict) -> dict:
    """The query as asked, checked and in one shape: {show, sequences, shots, name: [words],
    filters: {key: [values] or "text"}, since_days, latest}. ValueError says what's wrong."""
    if not isinstance(spec, dict):
        raise ValueError("The query is a set of named filters.")
    show = str(spec.get("show") or "").strip()
    if not _SHOW.match(show):
        raise ValueError("Pick a show first." if not show else f"{show[:40]!r} isn't a show code.")
    q = {"show": show}
    for k, what in (("sequences", "Sequences"), ("shots", "Shots")):
        q[k] = _list(spec.get(k), what, MAX_CODES)
        bad = [c for c in q[k] if not _CODE.match(c)]
        if bad:
            raise ValueError(f"{what}: {bad[0][:40]!r} isn't a {k[:-1]} code (letters, digits, _ . -, and * ? for patterns).")
    name = spec.get("name") or ""
    words = _list(name, "Name", MAX_WORDS)
    if any(len(w) > 100 for w in words):
        raise ValueError("Name: 100 characters at most.")
    q["name"] = words
    filters = spec.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("filters: {filter: values}.")
    q["filters"] = {}
    for k, v in filters.items():
        if not _KEY.match(str(k)):
            raise ValueError(f"There's no filter {str(k)[:40]!r}.")
        if isinstance(v, str):
            v = v.strip()
            if len(v) > 100:
                raise ValueError(f"{k}: 100 characters at most.")
            if v:
                q["filters"][str(k)] = v
            continue
        vals = _list(v, str(k), MAX_PICKED) if isinstance(v, (list, tuple)) else None
        if vals is None or any(len(x) > 200 for x in vals):
            raise ValueError(f"{k}: the values picked, or a text.")
        if vals:
            q["filters"][str(k)] = vals
    since = spec.get("since_days")
    if since in (None, "", 0):
        q["since_days"] = None
    else:
        try:
            since = int(since)
        except (TypeError, ValueError):
            raise ValueError("Created: a number of days.") from None
        if not 1 <= since <= 3650:
            raise ValueError("Created: between 1 and 3650 days.")
        q["since_days"] = since
    q["latest"] = bool(spec.get("latest", True))
    return q


def _match(patterns: list[str], names: list[str], what: str, show: str, within: set | None = None) -> list[str]:
    """The names matching each pattern (a glob, or a name in any case), only those in
    `within` when given; ValueError for a pattern matching none."""
    out = []
    for pat in patterns:
        p = pat.lower()
        hit = [n for n in names if fnmatch.fnmatchcase(n.lower(), p)]
        if not hit:
            raise ValueError(f"There's no {what} {pat} in {show}" + (" (or its status hides it)." if what == "shot" else "."))
        if within is not None:
            hit = [n for n in hit if n in within]
            if not hit:
                raise ValueError(f"{pat} isn't in the sequences picked.")
        out.extend(hit)
    return list(dict.fromkeys(out))


def build(q: dict, show: dict, opts: dict, shots: list[dict], cfg, now: datetime | None = None) -> list:
    """The ShotGrid filters for a normalized query: `show` {code, id}, `opts` the show's
    filters (_filters), `shots` its shot list. ValueError for a sequence, shot, filter or
    value the show doesn't have."""
    f = _base(show["id"], cfg)
    if q["sequences"] or q["shots"]:
        in_seq = None
        if q["sequences"]:
            seqs = set(_match(q["sequences"], sorted({s["sequence"] for s in shots if s["sequence"]}),
                              "sequence", show["code"]))
            in_seq = [s["code"] for s in shots if s["sequence"] in seqs]
            if not in_seq:
                raise ValueError(f"There's no shot in {', '.join(sorted(seqs))}.")
        if q["shots"]:
            codes = _match(q["shots"], [s["code"] for s in shots], "shot", show["code"],
                           set(in_seq) if in_seq is not None else None)
        else:
            codes = in_seq
        if len(codes) > MAX_CODES:
            raise ValueError(f"That's {len(codes)} shots: pick at most {MAX_CODES}, or no shots for all of them.")
        f.append(["entity.Shot.code", "in", sorted(codes)])
    if q["name"]:
        f.append({"filter_operator": "any", "filters": [["code", "contains", w] for w in q["name"]]})
    by_key = {d["key"]: d for d in opts["filters"]}
    for k, v in q["filters"].items():
        d = by_key.get(k)
        if d is None:
            gone = next((x for x in opts["left_out"] if x["key"] == k), None)
            raise ValueError(f"{gone['label']}: {gone['why']}." if gone else f"There's no filter {k}.")
        if d["kind"] == "text":
            text = v if isinstance(v, str) else " ".join(v)
            f.append([d["path"], "contains", text])
            continue
        vals = [v] if isinstance(v, str) else v
        known = {x["value"] for x in d.get("values") or []}
        bad = [x for x in vals if x not in known]
        if bad:
            raise ValueError(f"{d['label']}: there's no {bad[0]} among {show['code']}'s Versions.")
        f.append([d["path"], "in", vals])
    if q["since_days"]:
        now = now or datetime.now(timezone.utc)
        f.append(["created_at", "greater_than", now - timedelta(days=q["since_days"])])
    return f


def name_key(code: str) -> tuple[str, int | None]:
    """A Version's name without its version, and the version: yht_1040.lineup.autoLineup.v3
    -> ("yht_1040.lineup.autolineup", 3); bpa_1430_mp01_compressed.mov -> (…, None)."""
    c = _EXT.sub("", (code or "").strip())
    m = _VER.search(c)
    return ((c[:m.start()] if m else c).lower(), int(m[1]) if m else None)


def _number(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("vV").isdigit():
        return int(value.strip().lstrip("vV"))
    return None


def latest(rows: list[dict], number_field: str = "") -> list[dict]:
    """The latest Version of each shot and name, in the order given (the newest first):
    the highest number_field, else the code's vN, else the first given."""
    best: dict[tuple, tuple] = {}
    for i, r in enumerate(rows):
        base, n = name_key(r.get("code") or "")
        own = _number(r.get(number_field)) if number_field else None
        rank = (own if own is not None else n if n is not None else -1, -i)
        k = (((r.get("entity") or {}).get("name") or "").lower(), base)
        if k not in best or rank > best[k][0]:
            best[k] = (rank, i)
    keep = {i for _, i in best.values()}
    return [r for i, r in enumerate(rows) if i in keep]


def run(sg, spec: dict, now: datetime | None = None) -> dict:
    """The Versions a query finds: {query (normalized), show, rows: [as ShotGrid.version()],
    matched (before latest), looked_at, more, notes}. ValueError for a query the show can't
    take; Unavailable / Refused when ShotGrid can't be asked or refuses."""
    q = normalize(spec)
    qc = sg.cfg.query
    shots = sg.shots(q["show"])
    if shots["why"] or not shots["known_show"]:
        if shots["known_show"] is False:
            raise ValueError(shots["why"] or f"{q['show']} isn't one of the active shows in ShotGrid.")
        raise Unavailable(shots["why"])
    show = sg.project(shots["show"])
    q["show"] = show["code"]
    opts = _filters(sg, show, False)
    filters = build(q, show, opts, shots["shots"], sg.cfg, now)
    schema = sg.schema("Version")
    fields = sg.version_fields() + ["created_at"]
    number = qc.version_number if qc.version_number in schema else ""
    if number:
        fields.append(number)
    look = max(qc.scan if q["latest"] else qc.max_rows, 1)
    rows = sg.ask("find", "Version", filters, fields, limit=look + 1,
                  order=[{"field_name": "created_at", "direction": "desc"}, {"field_name": "id", "direction": "desc"}]) or []
    notes = []
    scanned_all = len(rows) <= look
    rows = rows[:look]
    matched = len(rows)
    if q["latest"]:
        rows = latest(rows, number)
    rows.sort(key=lambda r: (((r.get("entity") or {}).get("name") or "").lower(), (r.get("code") or "").lower()))
    if not scanned_all:
        notes.append(f"Only the newest {look} Versions were looked at"
                     + (" (the latest of each among them)" if q["latest"] else "") + ": narrow the query to see the rest.")
    if len(rows) > qc.max_rows:
        notes.append(f"{len(rows)} found, the first {qc.max_rows} shown: narrow the query to see the rest.")
        rows = rows[:qc.max_rows]
    found = [sg.version_dict(r) for r in rows]
    sg.remember(found)
    return {"query": q, "show": show["code"], "rows": found, "matched": matched, "looked_at": look,
            "more": bool(notes), "notes": notes}
