"""Batch Submit's ShotGrid query (comfyfarm.shotgrid.query): what it offers for a show
(sequences, shots, the filters and their values, from the site's schema and ShotGrid's
summarize), the filters it builds from names and picked values only, refusing what it
didn't offer, "latest per shot and name", and the rows it gives (Farm.batch_query).
Against a stand-in ShotGrid that evaluates the filters it's sent, like ShotGrid would."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm, FarmError
from comfyfarm.shotgrid import ShotGrid
from comfyfarm.shotgrid import query as Q
from comfyfarm.workflows import WorkflowRegistry

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
FRAMES = "/rdo/shows/bri3/.published/x/hi/x.#.exr"
MOVIE = "/rdo/shows/bri3/x/movhistory/x.mov"


class Fault(Exception):
    """shotgun_api3's: ShotGrid answered with an error."""


def ago(days):
    return NOW - timedelta(days=days)


PROJECTS = [{"type": "Project", "id": 11, "name": "Bridgerton 3", "tank_name": "bri3"},
            {"type": "Project", "id": 12, "name": "Black Canary", "tank_name": "bc1"}]
SHOTS = {1: {"code": "bpa_1430", "sg_sequence": {"type": "Sequence", "id": 1, "name": "bpa"}, "sg_status_list": "ip", "project": 11},
         2: {"code": "bpa_1440", "sg_sequence": {"type": "Sequence", "id": 1, "name": "bpa"}, "sg_status_list": "ip", "project": 11},
         3: {"code": "yht_1040", "sg_sequence": {"type": "Sequence", "id": 2, "name": "yht"}, "sg_status_list": "ip", "project": 11},
         4: {"code": "yht_1050", "sg_sequence": {"type": "Sequence", "id": 2, "name": "yht"}, "sg_status_list": "omt", "project": 11},
         5: {"code": "mis_0040", "sg_sequence": {"type": "Sequence", "id": 3, "name": "mis"}, "sg_status_list": "ip", "project": 12}}
TASKS = {501: {"content": "LINEUP (AUTO)"}, 502: {"content": "LINEUP (AUTO)"}, 503: {"content": "tech"}}
STEPS = {1: {"code": "Lighting"}, 2: {"code": "Lineup"}}
LINKED = {"Shot": SHOTS, "Task": TASKS, "Step": STEPS}


def shot(i):
    return {"type": "Shot", "id": i, "name": SHOTS[i]["code"]}


def task(i):
    return {"type": "Task", "id": i, "name": TASKS[i]["content"]}


def step(i):
    return {"type": "Step", "id": i, "name": STEPS[i]["code"]}


def ver(vid, code, ent, created, project=11, frames=FRAMES, movie=MOVIE, **f):
    return {"type": "Version", "id": vid, "code": code, "entity": ent, "project": {"type": "Project", "id": project},
            "sg_path_to_frames": frames, "sg_path_to_movie": movie, "sg_first_frame": 1001, "sg_last_frame": 1010,
            "created_at": created, "sg_status_list": "rev", "sg_task": None, "sg_pipeline_step": None,
            "sg_version_type": None, "sg_department": None, "sg_version_number": None, "sg_layer": None, **f}


VERSIONS = [
    ver(101, "yht_1040.lineup.autoLineup.v1", shot(3), ago(10), sg_task=task(501), sg_pipeline_step=step(2),
        sg_version_type="render", sg_department="Lighting", sg_version_number=1, sg_status_list="apr"),
    ver(102, "yht_1040.lineup.autoLineup.v2", shot(3), ago(5), sg_task=task(501), sg_pipeline_step=step(2),
        sg_version_type="render", sg_department="Lighting", sg_version_number=2),
    ver(103, "bpa_1430_mp01_compressed.mov", shot(1), ago(30), frames=None, sg_task=task(502),
        sg_version_type="plate", sg_department="Reference", sg_status_list="na"),
    ver(104, "bpa_1430.lig.tech.cs_layers.defPart.v26", shot(1), ago(2), sg_task=task(503), sg_pipeline_step=step(1),
        sg_version_type="render", sg_department="Lighting", sg_version_number="26"),
    ver(105, "bpa_1430.lig.tech.cs_layers.defPart.v25", shot(1), ago(3), sg_task=task(503), sg_pipeline_step=step(1),
        sg_version_type="render", sg_department="Lighting", sg_version_number=25),
    ver(106, "bpa_1440.lig.tech.v1", shot(2), ago(1), frames=None, movie=None),            # nothing to run on
    ver(107, "chairA.model.v3", {"type": "Asset", "id": 9, "name": "chairA"}, ago(1)),     # not a shot
    ver(108, "yht_1050.comp.v1", shot(4), ago(1)),                                         # an omitted shot
    ver(109, "mis_0040.comp.v1", shot(5), ago(1), project=12),                             # another show
]

# Rodeo's Version fields as the chunk 11 probe found them (Version 4865327, the project's
# claude/sg_probe-version-4865327.json): Pipeline Step links to a Step, Task to a Task,
# Type and Department are lists, Version Number a number, Layer text.
SCHEMA = {
    "code": {"data_type": {"value": "text"}, "name": {"value": "Version Name"}},
    "sg_status_list": {"data_type": {"value": "status_list"}, "name": {"value": "Status"},
                       "properties": {"valid_values": {"value": ["na", "rev", "apr"]},
                                      "display_values": {"value": {"na": "N/A", "rev": "Pending Review", "apr": "Approved"}}}},
    "sg_task": {"data_type": {"value": "entity"}, "name": {"value": "Task"}, "properties": {"valid_types": {"value": ["Task"]}}},
    "sg_pipeline_step": {"data_type": {"value": "entity"}, "name": {"value": "Pipeline Step"},
                         "properties": {"valid_types": {"value": ["Step"]}}},
    "sg_version_type": {"data_type": {"value": "list"}, "name": {"value": "Type"},
                        "properties": {"valid_values": {"value": ["plate", "render", "precomp"]},
                                       "display_values": {"value": {"plate": "Plate", "render": "Render"}}}},
    "sg_department": {"data_type": {"value": "list"}, "name": {"value": "Department"},
                      "properties": {"valid_values": {"value": ["Editorial", "Lighting", "FX", "Reference"]}}},
    "sg_layer": {"data_type": {"value": "text"}, "name": {"value": "Layer"}},
    "sg_version_number": {"data_type": {"value": "number"}, "name": {"value": "Version Number"}},
    "sg_first_frame": {"data_type": {"value": "number"}}, "created_at": {"data_type": {"value": "date_time"}},
}


def resolve(rec, path):
    """A field of a record, through links (entity.Shot.code)."""
    head, *rest = path.split(".")
    value = rec.get(head)
    while rest:
        if not isinstance(value, dict):
            return None
        typ, field, rest = rest[0], rest[1], rest[2:]
        if value.get("type") != typ:
            return None
        value = LINKED[typ][value["id"]].get(field)
    return value


def matches(rec, f):
    if isinstance(f, dict):
        hits = [matches(rec, x) for x in f["filters"]]
        return any(hits) if f["filter_operator"] == "any" else all(hits)
    path, op, value = f
    have = resolve(rec, path)
    if path == "project" and isinstance(have, dict):
        have = {"type": "Project", "id": have["id"]}
    if op == "is":
        return have == value
    if op == "is_not":
        return have != value
    if op == "in":
        return have in value
    if op == "not_in":
        return have not in value
    if op == "contains":
        return value.lower() in (have or "").lower()
    if op == "type_is":
        return isinstance(have, dict) and have.get("type") == value
    if op == "greater_than":
        return have is not None and have > value
    raise AssertionError(f"an operator the query shouldn't use: {op}")


class FakeShotGrid:
    def __init__(self):
        self.calls = []
        self.refuse_summarize: set[str] = set()
        self.versions = [dict(v) for v in VERSIONS]
        self.schema = dict(SCHEMA)

    def find(self, entity, filters, fields=None, order=None, limit=0, **kw):
        self.calls.append(("find", entity, filters, fields, limit))
        if entity == "Project":
            return [dict(p) for p in PROJECTS]
        if entity == "Shot":
            pid = filters[0][2]["id"]
            hidden = next((f[2] for f in filters if f[0] == "sg_status_list"), [])
            return [{**s, "type": "Shot", "id": i} for i, s in SHOTS.items()
                    if s["project"] == pid and s["sg_status_list"] not in hidden]
        if entity == "Version":
            for f in filters:
                self._check(f)
            rows = [v for v in self.versions if all(matches(v, f) for f in filters)]
            rows.sort(key=lambda v: (v["created_at"], v["id"]), reverse=True)
            rows = [{k: v[k] for k in ["type", "id", *fields] if k in v} for v in rows]
            return rows[:limit] if limit else rows
        raise AssertionError(entity)

    def _check(self, f):
        """Only fields the site has, as ShotGrid would refuse others."""
        if isinstance(f, dict):
            for x in f["filters"]:
                self._check(x)
            return
        head = f[0].split(".")[0]
        if head not in self.schema and head not in ("project", "entity", "id", "code", "created_at",
                                                    "sg_path_to_frames", "sg_path_to_movie"):
            raise Fault(f"API read() Version.{head} doesn't exist")
        kind = ((self.schema.get(f[0]) or {}).get("data_type") or {}).get("value")
        if f[1] == "contains" and kind in ("list", "status_list"):
            raise Fault(f"API read() 'contains' isn't valid for the {kind} field Version.{f[0]}")

    def schema_field_read(self, entity):
        self.calls.append(("schema_field_read", entity))
        assert entity == "Version"
        return dict(self.schema)

    def summarize(self, entity, filters, summary_fields, grouping=None, **kw):
        self.calls.append(("summarize", entity, filters, grouping))
        path = grouping[0]["field"]
        if path.split(".")[0] in self.refuse_summarize:
            raise Fault(f"can't group by {path}")
        groups = {}
        for v in self.versions:
            if all(matches(v, f) for f in filters):
                value = resolve(v, path)
                key = value["id"] if isinstance(value, dict) else value
                g = groups.setdefault(key, {"group_name": value["name"] if isinstance(value, dict) else (value or ""),
                                            "group_value": value, "summaries": {"id": 0}})
                g["summaries"]["id"] += 1
        return {"groups": list(groups.values()), "summaries": {"id": sum(g["summaries"]["id"] for g in groups.values())}}

    def asked(self, method, entity="Version"):
        return [c for c in self.calls if c[0] == method and c[1] == entity]


# Every kind of field the query handles: a link (Pipeline Step), a link listed by name across
# shots (Task), and lists (Type, Department). Rodeo's default is step and department only.
FIELDS = {"step": "sg_pipeline_step", "task": "sg_task", "type": "sg_version_type", "department": "sg_department"}


def lookups(**query):
    fake = FakeShotGrid()
    query = {"fields": FIELDS, **query}
    conf = Config.model_validate({"workers": [], "shotgrid": {"enabled": True, "refresh_hold": 0, "query": query}}).shotgrid
    return ShotGrid(conf, connect=lambda: fake), fake


def codes(r):
    return [v["code"] for v in r["rows"]]


class Offered(unittest.TestCase):
    def test_what_a_show_offers(self):
        sg, fake = lookups()
        o = Q.options(sg, "BRI3")
        self.assertIsNone(o["why"])
        self.assertEqual(o["show"], "bri3")
        self.assertEqual(o["sequences"], [{"code": "bpa", "shots": 2}, {"code": "yht", "shots": 1}])   # yht_1050 omitted
        self.assertEqual([s["code"] for s in o["shots"]], ["bpa_1430", "bpa_1440", "yht_1040"])
        f = {d["key"]: d for d in o["filters"]}
        self.assertEqual(list(f), ["status", "step", "task", "type", "department"])
        # The values the show's Versions have (with frames or a movie), labelled as ShotGrid does.
        self.assertEqual(f["status"]["values"], [{"value": "apr", "label": "Approved", "count": 1},
                                                 {"value": "na", "label": "N/A", "count": 1},
                                                 {"value": "rev", "label": "Pending Review", "count": 3}])
        self.assertEqual(f["step"]["values"], [{"value": "Lighting", "label": "Lighting", "count": 2},
                                               {"value": "Lineup", "label": "Lineup", "count": 2}])
        self.assertEqual(f["task"]["values"], [{"value": "LINEUP (AUTO)", "label": "LINEUP (AUTO)", "count": 3},
                                               {"value": "tech", "label": "tech", "count": 2}])  # two tasks, one name
        self.assertEqual(f["type"]["values"], [{"value": "plate", "label": "Plate", "count": 1},
                                               {"value": "render", "label": "Render", "count": 4}])
        self.assertEqual([v["value"] for v in f["department"]["values"]], ["Lighting", "Reference"])
        self.assertEqual({d["kind"] for d in o["filters"]}, {"choice"})
        self.assertEqual((f["step"]["label"], f["step"]["field"]), ("Pipeline Step", "sg_pipeline_step"))
        # Through the link by name: the summarize grouped on the Task's content.
        self.assertIn([{"field": "sg_task.Task.content", "type": "exact", "direction": "asc"}],
                      [c[3] for c in fake.asked("summarize")])
        n = len(fake.calls)
        Q.options(sg, "bri3")                                                     # cached
        self.assertEqual(len(fake.calls), n)
        Q.options(sg, "bri3", refresh=True)
        self.assertEqual(len(fake.asked("summarize")), 10)

    def test_what_the_site_doesnt_have_or_cant_list(self):
        sg, fake = lookups(fields={"step": "sg_pipeline_step", "frames": "sg_first_frame", "layer": "sg_layer",
                                   "dept": "sg_department", "type": "sg_version_type", "gone": "sg_nope"})
        fake.refuse_summarize = {"sg_layer", "sg_department", "sg_version_type"}
        fake.versions = [v for v in fake.versions if v["id"] != 104 and v["id"] != 105]   # no Lighting step left
        for v in fake.versions:
            v["sg_pipeline_step"] = None
        o = Q.options(sg, "bri3")
        f = {d["key"]: d for d in o["filters"]}
        self.assertEqual(list(f), ["status", "layer", "dept", "type"])
        self.assertEqual((f["layer"]["kind"], f["layer"]["note"]), ("text", "typed: ShotGrid couldn't list its values"))
        for k, first in (("type", {"value": "plate", "label": "Plate", "count": None}),   # a list: all its values,
                         ("dept", {"value": "Editorial", "label": "Editorial", "count": None})):  # without counts
            self.assertEqual((f[k]["kind"], f[k]["values"][0]), ("choice", first))
        self.assertEqual({d["key"]: d["why"] for d in o["left_out"]},
                         {"step": "none of bri3's Versions has one",
                          "frames": "sg_first_frame is a number field: only lists, text and links are offered",
                          "gone": "Version has no field sg_nope on this site (shotgrid.query.fields)"})

    def test_too_many_values_are_typed_but_not_a_list(self):
        sg, fake = lookups(fields={"layer": "sg_layer", "department": "sg_department"})
        fake.versions += [ver(1000 + i, f"yht_1040.x{i}.v1", shot(3), ago(1), sg_layer=f"l{i}", sg_department=f"d{i}")
                          for i in range(Q.MAX_VALUES + 1)]
        f = {d["key"]: d for d in Q.options(sg, "bri3")["filters"]}
        self.assertEqual((f["layer"]["kind"], f["layer"]["note"]), ("text", "typed: 301 values, too many to list"))
        self.assertNotIn("values", f["layer"])
        # ShotGrid takes no "contains" on a list (Rodeo's Department is one): it stays picked from.
        self.assertEqual((f["department"]["kind"], len(f["department"]["values"])), ("choice", 303))

    def test_the_default_filters(self):
        """Rodeo's: status, Pipeline Step and Department (Task and Type weren't useful)."""
        fake = FakeShotGrid()
        sg = ShotGrid(Config.model_validate({"workers": [], "shotgrid": {"enabled": True, "refresh_hold": 0}}).shotgrid,
                      connect=lambda: fake)
        o = Q.options(sg, "bri3")
        self.assertEqual([(f["key"], f["label"]) for f in o["filters"]],
                         [("status", "Status"), ("step", "Pipeline Step"), ("department", "Department")])
        self.assertEqual(o["left_out"], [])
        with self.assertRaises(ValueError) as e:                                 # a saved query naming Task
            Q.run(sg, {"show": "bri3", "filters": {"task": ["LINEUP (AUTO)"]}})
        self.assertEqual(str(e.exception), "There's no filter task.")

    def test_no_show_or_no_shotgrid(self):
        sg, fake = lookups()
        o = Q.options(sg, "nope")
        self.assertEqual((o["why"], o["filters"], o["shots"]), ("nope isn't one of the active shows in ShotGrid.", [], []))
        off = ShotGrid(Config.model_validate({"workers": []}).shotgrid, connect=lambda: fake)
        self.assertIn("ShotGrid lookups are off", Q.options(off, "bri3")["why"])
        self.assertFalse(Q.options(off, "bri3")["set_up"])


class Found(unittest.TestCase):
    def setUp(self):
        self.sg, self.fake = lookups()

    def run_(self, **spec):
        return Q.run(self.sg, {"show": "bri3", **spec}, now=NOW)

    def test_the_latest_of_each_shot_and_name(self):
        r = self.run_()
        # Not the Version without media, the asset's, the omitted shot's or another show's.
        self.assertEqual(codes(r), ["bpa_1430.lig.tech.cs_layers.defPart.v26", "bpa_1430_mp01_compressed.mov",
                                    "yht_1040.lineup.autoLineup.v2"])
        self.assertEqual((r["matched"], r["show"], r["notes"], r["more"]), (5, "bri3", [], False))
        v = r["rows"][0]
        self.assertEqual((v["id"], v["shot"], v["sequence"], v["show"], v["frames"]), (104, "bpa_1430", "bpa", "bri3", FRAMES))
        n = len(self.fake.calls)
        self.assertEqual(self.sg.version(104)["code"], v["code"])                 # cached for the row checks
        self.assertEqual(len(self.fake.calls), n)
        self.assertEqual(codes(self.run_(latest=False)), [
            "bpa_1430.lig.tech.cs_layers.defPart.v25", "bpa_1430.lig.tech.cs_layers.defPart.v26",
            "bpa_1430_mp01_compressed.mov", "yht_1040.lineup.autoLineup.v1", "yht_1040.lineup.autoLineup.v2"])

    def test_filters(self):
        for spec, want in (
                ({"name": "mp01"}, ["bpa_1430_mp01_compressed.mov"]),
                ({"name": "MP01, autolineup"}, ["bpa_1430_mp01_compressed.mov", "yht_1040.lineup.autoLineup.v2"]),
                ({"sequences": ["yht"]}, ["yht_1040.lineup.autoLineup.v2"]),
                ({"shots": "BPA_14*"}, ["bpa_1430.lig.tech.cs_layers.defPart.v26", "bpa_1430_mp01_compressed.mov"]),
                ({"sequences": ["bpa", "yht"], "shots": ["*_1040"]}, ["yht_1040.lineup.autoLineup.v2"]),
                ({"filters": {"status": ["apr"]}, "latest": False}, ["yht_1040.lineup.autoLineup.v1"]),
                ({"filters": {"status": ["apr"]}}, ["yht_1040.lineup.autoLineup.v1"]),   # the latest among those found
                ({"filters": {"step": ["Lighting"]}}, ["bpa_1430.lig.tech.cs_layers.defPart.v26"]),
                ({"filters": {"task": ["LINEUP (AUTO)"], "type": ["plate"]}}, ["bpa_1430_mp01_compressed.mov"]),
                ({"filters": {"department": ["Reference", "Lighting"]}, "since_days": 4},
                 ["bpa_1430.lig.tech.cs_layers.defPart.v26"]),
                ({"since_days": 7, "latest": False}, ["bpa_1430.lig.tech.cs_layers.defPart.v25",
                                                      "bpa_1430.lig.tech.cs_layers.defPart.v26",
                                                      "yht_1040.lineup.autoLineup.v2"]),
                ({"name": "nothing"}, [])):
            with self.subTest(spec):
                self.assertEqual(codes(self.run_(**spec)), want)

    def test_the_filters_sent(self):
        self.run_(sequences=["bpa"], name="mp01", filters={"task": ["LINEUP (AUTO)"]}, since_days=3)
        sent = self.fake.asked("find")[-1]
        self.assertEqual(sent[2], [
            ["project", "is", {"type": "Project", "id": 11}], ["entity", "type_is", "Shot"],
            ["entity.Shot.sg_status_list", "not_in", ["hld", "omt", "hdn", "void"]],
            {"filter_operator": "any", "filters": [["sg_path_to_frames", "is_not", None], ["sg_path_to_movie", "is_not", None]]},
            ["entity.Shot.code", "in", ["bpa_1430", "bpa_1440"]],
            {"filter_operator": "any", "filters": [["code", "contains", "mp01"]]},
            ["sg_task.Task.content", "in", ["LINEUP (AUTO)"]],
            ["created_at", "greater_than", NOW - timedelta(days=3)]])
        self.assertIn("sg_version_number", sent[3])
        self.assertEqual(sent[4], 5001)                                           # scan + 1: are there more?

    def test_a_typed_filter(self):
        sg, fake = lookups(fields={"layer": "sg_layer"})
        fake.refuse_summarize = {"sg_layer"}
        fake.versions[2]["sg_layer"] = "refPass"                                   # bpa_1430_mp01_compressed.mov
        self.assertEqual(codes(Q.run(sg, {"show": "bri3", "filters": {"layer": "refp"}}, now=NOW)),
                         ["bpa_1430_mp01_compressed.mov"])
        self.assertEqual(fake.asked("find")[-1][2][-1], ["sg_layer", "contains", "refp"])

    def test_refused(self):
        for spec, why in (
                ({"show": ""}, "Pick a show first."),
                ({"show": "a b"}, "isn't a show code"),
                ({"show": "nope"}, "nope isn't one of the active shows"),
                ({"sequences": ["zzz"]}, "There's no sequence zzz in bri3."),
                ({"shots": ["yht_1050"]}, "There's no shot yht_1050 in bri3 (or its status hides it)."),
                ({"shots": ["bpa;drop"]}, "isn't a shot code"),
                ({"sequences": ["yht"], "shots": ["bpa_1430"]}, "bpa_1430 isn't in the sequences picked."),
                ({"filters": {"status": ["omt"]}}, "Status: there's no omt among bri3's Versions."),
                ({"filters": {"colour": ["red"]}}, "There's no filter colour."),
                ({"filters": {"sg_status_list": ["rev"]}}, "There's no filter sg_status_list."),
                ({"filters": {"status": [{"$ne": 1}]}}, "status: a list of names"),
                ({"filters": {"status": 3}}, "the values picked, or a text"),
                ({"since_days": 0.5}, "between 1 and 3650"),
                ({"since_days": "soon"}, "a number of days"),
                ({"name": "x" * 101}, "100 characters")):
            with self.subTest(spec), self.assertRaises(ValueError) as e:
                Q.run(self.sg, {"show": "bri3", **spec}, now=NOW)
            self.assertIn(why, str(e.exception))
        self.assertEqual(self.fake.asked("find"), [])                            # nothing asked of ShotGrid

    def test_a_filter_left_out_says_why(self):
        sg, fake = lookups(fields={"gone": "sg_nope"})
        with self.assertRaises(ValueError) as e:
            Q.run(sg, {"show": "bri3", "filters": {"gone": ["x"]}})
        self.assertEqual(str(e.exception), "Gone: Version has no field sg_nope on this site (shotgrid.query.fields).")

    def test_more_than_it_takes(self):
        sg, fake = lookups(scan=3, max_rows=2)
        r = Q.run(sg, {"show": "bri3"}, now=NOW)
        self.assertEqual(r["matched"], 3)                                         # the newest 3: v26, v25, v2
        self.assertEqual(codes(r), ["bpa_1430.lig.tech.cs_layers.defPart.v26", "yht_1040.lineup.autoLineup.v2"])
        self.assertTrue(r["more"])
        self.assertEqual(r["notes"], ["Only the newest 3 Versions were looked at (the latest of each among them): "
                                      "narrow the query to see the rest."])
        r = Q.run(sg, {"show": "bri3", "latest": False}, now=NOW)
        self.assertEqual(r["notes"], ["Only the newest 2 Versions were looked at: narrow the query to see the rest."])
        sg, fake = lookups(max_rows=2)
        self.assertEqual(Q.run(sg, {"show": "bri3"}, now=NOW)["notes"],
                         ["3 found, the first 2 shown: narrow the query to see the rest."])


class Latest(unittest.TestCase):
    def test_names_and_versions(self):
        for code, want in (("yht_1040.lineup.autoLineup.v3", ("yht_1040.lineup.autolineup", 3)),
                           ("bpa_1430_mp01_compressed.mov", ("bpa_1430_mp01_compressed", None)),
                           ("abc_0010_comp_v012.mov", ("abc_0010_comp", 12)),
                           ("abc_0010-plate-V7", ("abc_0010-plate", 7)),
                           ("abc_0010.v2.final", ("abc_0010.v2.final", None))):
            self.assertEqual(Q.name_key(code), want)

    def test_which_is_latest(self):
        e = {"name": "abc_0010"}
        rows = [{"id": 1, "code": "abc_0010.comp.v9", "entity": e, "n": "3"},     # the newest first
                {"id": 2, "code": "abc_0010.comp.v10", "entity": e, "n": 4},
                {"id": 3, "code": "abc_0010.plate.mov", "entity": e},
                {"id": 4, "code": "abc_0010.plate.mov", "entity": e},
                {"id": 5, "code": "abc_0010.comp.v1", "entity": {"name": "abc_0020"}}]
        self.assertEqual([r["id"] for r in Q.latest(rows, "n")], [2, 3, 5])     # the field, then the newest
        self.assertEqual([r["id"] for r in Q.latest(rows)], [2, 3, 5])          # the code's vN


class Rows(unittest.TestCase):
    """Farm.batch_query: Batch Submit's rows, as a playlist's."""

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = Config.model_validate({"database": f"{tmp}/farm.db", "workers": [], "shotgrid": {"enabled": True},
                                     "preview": {"enabled": False}})
        store = JobStore(Path(cfg.database))
        self.addCleanup(store.close)
        self.farm = Farm(cfg, store, WorkflowRegistry(REPO / "workflows"), EventBus())
        self.fake = FakeShotGrid()
        self.farm.shotgrid = ShotGrid(cfg.shotgrid, connect=lambda: self.fake)

    def test_rows(self):
        out = self.farm.batch_query({"show": "bri3", "sequences": ["yht"]})
        self.assertEqual(out["source"], {"code": "1 latest Version in bri3", "show": "bri3", "matched": 2, "latest": True,
                                         "more": False, "notes": [], "query": {
                                             "show": "bri3", "sequences": ["yht"], "shots": [], "name": [], "filters": {},
                                             "since_days": None, "latest": True}})
        self.assertEqual(out["rows"], [{"version": self.farm.shotgrid.version(102),
                                        "input": "https://rodeofx.shotgrid.autodesk.com/detail/Version/102"}])
        self.assertEqual(self.farm.batch_query({"show": "bri3", "latest": False})["source"]["code"], "5 Versions in bri3")
        self.assertEqual(self.farm.batch_query({"show": "bri3", "name": "zz"})["source"]["code"], "No Version in bri3 matches")
        self.assertEqual(self.farm.query_options("bri3")["show"], "bri3")

    def test_refused(self):
        with self.assertRaises(FarmError) as e:
            self.farm.batch_query({"show": "bri3", "sequences": ["zzz"]})
        self.assertEqual((e.exception.status, str(e.exception)), (400, "There's no sequence zzz in bri3."))
        self.fake.schema["sg_version_number"] = SCHEMA["sg_version_number"]
        self.fake.versions[0]["code"] = None                                     # still fine
        self.farm.shotgrid.cfg.query.fields = {"bad": "sg_bad"}
        self.fake.schema["sg_bad"] = {"data_type": {"value": "text"}}            # the site says yes, the find says no
        self.farm.shotgrid.query_options.clear()
        self.fake._check = lambda f: (_ for _ in ()).throw(Fault("API read() Version.sg_bad doesn't exist"))
        with self.assertRaises(FarmError) as e:
            self.farm.batch_query({"show": "bri3"})
        self.assertEqual(e.exception.status, 502)
        self.assertIn("ShotGrid refused the query", str(e.exception))
        off = ShotGrid(Config.model_validate({"workers": []}).shotgrid, connect=lambda: self.fake)
        self.farm.shotgrid = off
        with self.assertRaises(FarmError) as e:
            self.farm.batch_query({"show": "bri3"})
        self.assertEqual(e.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
