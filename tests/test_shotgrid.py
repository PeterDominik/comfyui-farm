"""ShotGrid lookups (comfyfarm.shotgrid) against a stand-in ShotGrid: the shows and shots
asked for, caching and refreshing, ShotGrid not answering, the script's key from an env
file, and the check publishing makes. Nothing here talks to the real ShotGrid."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.shotgrid import ShotGrid
from comfyfarm.shotgrid.lookup import read_env_file

PROJECTS = [{"type": "Project", "id": 11, "name": "Bridgerton 3", "tank_name": "bri3", "code": "Bridgerton3"},
            {"type": "Project", "id": 12, "name": "Black Canary", "tank_name": "bc1", "code": "bc1"},
            {"type": "Project", "id": 13, "name": "No code", "tank_name": None, "code": None}]
SHOTS = {11: [{"code": "305046_0020", "sg_sequence": {"type": "Sequence", "id": 1, "name": "305046"},
               "sg_status_list": "ip"},
              {"code": "305046_0010", "sg_sequence": {"type": "Sequence", "id": 1, "name": "305046"},
               "sg_status_list": "wtg"},
              {"code": "301011_0140", "sg_sequence": {"type": "Sequence", "id": 2, "name": "301011"},
               "sg_status_list": "ip"},
              {"code": "loose_0010", "sg_sequence": None, "sg_status_list": "ip"}],
         12: [{"code": "mis_0040", "sg_sequence": {"type": "Sequence", "id": 3, "name": "mis"}, "sg_status_list": "ip"},
              # bc1's ShotGrid Sequences are editorial ones: the pipeline's sequence is yht
              {"code": "yht_1040", "sg_sequence": {"type": "Sequence", "id": 4, "name": "child"}, "sg_status_list": "turn"},
              {"code": "nonumber", "sg_sequence": {"type": "Sequence", "id": 5, "name": "misc"}, "sg_status_list": "ip"}]}
OMITTED = {11: [{"type": "Shot", "id": 99, "code": "305046_0030", "sg_status_list": "omt"}]}
ARCHIVED = [{"type": "Project", "id": 14, "tank_name": "old1"}]


class FakeShotGrid:
    """Answers find/find_one like shotgun_api3 for the filters the lookups use; records them."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.down = False
        self.shots = {k: list(v) for k, v in SHOTS.items()}

    def find(self, entity, filters, fields=None, **kw):
        self.calls.append(("find", entity, filters, fields))
        if self.down:
            raise ConnectionError("Connection refused")
        if entity == "Project":
            return [dict(p) for p in PROJECTS]
        if entity == "Shot":
            pid = filters[0][2]["id"]
            return [dict(s) for s in self.shots.get(pid, [])]
        raise AssertionError(entity)

    def find_one(self, entity, filters, fields=None, **kw):
        self.calls.append(("find_one", entity, filters, fields))
        if self.down:
            raise ConnectionError("Connection refused")
        if entity == "Project":
            code = filters[0][2]
            return next((p for p in ARCHIVED if p["tank_name"] == code), None)
        if entity == "Shot":
            pid, code = filters[0][2]["id"], filters[1][2]
            return next((s for s in OMITTED.get(pid, []) if s["code"] == code), None)
        raise AssertionError(entity)

    def count(self, entity):
        return sum(1 for c in self.calls if c[1] == entity)


def lookups(**cfg) -> tuple[ShotGrid, FakeShotGrid]:
    fake = FakeShotGrid()
    conf = Config.model_validate({"workers": [], "shotgrid": {"enabled": True, "refresh_hold": 0, **cfg}}).shotgrid
    return ShotGrid(conf, connect=lambda: fake), fake


class LookupTests(unittest.TestCase):
    def test_shows(self):
        sg, fake = lookups()
        got = sg.projects()
        self.assertIsNone(got["why"])
        self.assertEqual(got["projects"], [{"code": "bc1", "name": "Black Canary"},
                                           {"code": "bri3", "name": "Bridgerton 3"}])
        [(_, _, filters, fields)] = fake.calls
        self.assertEqual(filters, [["sg_status", "is", "Active"]])            # the active ones
        self.assertIn("tank_name", fields)
        self.assertEqual(sg.status()["enabled"], True)

    def test_only_shows_that_can_be_published_to(self):
        fake = FakeShotGrid()
        conf = Config.model_validate({"workers": [], "shotgrid": {"enabled": True}}).shotgrid
        sg = ShotGrid(conf, connect=lambda: fake, keep=lambda code: code == "bri3")   # the farm: a show folder
        self.assertEqual([p["code"] for p in sg.projects()["projects"]], ["bri3"])
        self.assertFalse(sg.shots("bc1")["known_show"])
        from comfyfarm.manager.farm import publish_target_ok
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "bri3").mkdir()
        pub = Config.model_validate({"workers": [], "publish": {"show_folder": str(tmp / "{show}")}}).publish
        self.assertEqual((publish_target_ok(pub, "bri3"), publish_target_ok(pub, "bizdev audio")), (True, False))
        pub.show_folder = ""
        self.assertTrue(publish_target_ok(pub, "anything"))

    def test_shots_and_sequences(self):
        sg, fake = lookups()
        got = sg.shots("BRI3")
        self.assertEqual((got["show"], got["why"], got["known_show"]), ("bri3", None, True))
        self.assertEqual([s["code"] for s in got["shots"]], ["301011_0140", "305046_0010", "305046_0020", "loose_0010"])
        self.assertEqual(got["shots"][1], {"code": "305046_0010", "sequence": "305046", "status": "wtg"})
        self.assertEqual(got["sequences"], ["301011", "305046", "loose"])          # from the shot names
        bc1 = sg.shots("bc1")
        self.assertEqual({s["code"]: s["sequence"] for s in bc1["shots"]},
                         {"mis_0040": "mis", "yht_1040": "yht", "nonumber": "misc"})   # not ShotGrid's "child"
        self.assertEqual(bc1["sequences"], ["mis", "misc", "yht"])
        shot_call = next(c for c in fake.calls if c[1] == "Shot")
        self.assertEqual(shot_call[2], [["project", "is", {"type": "Project", "id": 11}],
                                        ["sg_status_list", "not_in", ["hld", "omt", "hdn", "void"]]])
        unknown = sg.shots("nope")
        self.assertEqual((unknown["known_show"], unknown["shots"]), (False, []))
        self.assertIn("isn't one of the active shows", unknown["why"])

    def test_cached_until_refreshed_or_old(self):
        sg, fake = lookups(shots_ttl=600)
        sg.shots("bri3")
        sg.shots("bri3")
        sg.projects()
        self.assertEqual((fake.count("Project"), fake.count("Shot")), (1, 1))
        fake.shots[11].append({"code": "305046_0040", "sg_sequence": None, "sg_status_list": "wtg"})
        got = sg.shots("bri3", refresh=True)                       # the refresh button: shows and shots
        self.assertIn("305046_0040", [s["code"] for s in got["shots"]])
        self.assertEqual((fake.count("Project"), fake.count("Shot")), (2, 2))
        sg._shots["bri3"] = (time.time() - 601, sg._shots["bri3"][1])  # too old
        sg.shots("bri3")
        self.assertEqual(fake.count("Shot"), 3)

    def test_refresh_is_held_back(self):
        sg, fake = lookups(refresh_hold=30)
        sg.projects()
        sg.projects(refresh=True)
        sg.projects(refresh=True)                                  # clicked again straight away
        self.assertEqual(fake.count("Project"), 2)

    def test_shotgrid_not_answering(self):
        sg, fake = lookups(retry_seconds=60)
        sg.shots("bri3")
        fake.down = True
        got = sg.shots("bri3", refresh=True)
        self.assertIn("ShotGrid didn't answer (ConnectionError: Connection refused)", got["why"])
        self.assertTrue(got["set_up"])                             # set up, just not answering
        self.assertEqual(len(got["shots"]), 4)                     # the last list, said to be stale
        before = len(fake.calls)
        self.assertIn("didn't answer", sg.projects(refresh=True)["why"])
        self.assertEqual(len(fake.calls), before)                  # left alone for a minute
        self.assertIsNone(sg.check("bri3", "nothing_0010"))        # not checked: Daiquiri decides
        fake.down = False
        sg._down_until = 0
        self.assertIsNone(sg.projects()["why"])

    def test_off_or_not_set_up(self):
        conf = Config.model_validate({"workers": []}).shotgrid
        sg = ShotGrid(conf)
        self.assertIn("shotgrid.enabled", sg.why())
        self.assertEqual(sg.projects(), {"projects": [], "fetched_at": None, "why": sg.why(), "set_up": False})
        self.assertEqual(sg.shots("bri3")["shots"], [])
        self.assertIsNone(sg.check("bri3", "305046_0010"))
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        conf.enabled, conf.env_file = True, str(tmp / "none.env")
        conf.script_name_env, conf.script_key_env = "CF_TEST_SG_NAME", "CF_TEST_SG_KEY"
        why = ShotGrid(conf).why()
        self.assertTrue("shotgun_api3 isn't installed" in why or "has no ShotGrid script" in why, why)

    def test_key_from_env_file(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        f = tmp / "shotgrid.env"
        f.write_text("# ComfyFarm's ShotGrid script\nexport CF_TEST_SG_NAME=comfyfarm\nCF_TEST_SG_KEY='s3cr=t'\n\nnoise\n")
        if os.name != "nt":
            os.chmod(f, 0o600)
        self.assertEqual(read_env_file(f), {"CF_TEST_SG_NAME": "comfyfarm", "CF_TEST_SG_KEY": "s3cr=t"})
        conf = Config.model_validate({"workers": [], "shotgrid": {
            "enabled": True, "env_file": str(f), "script_name_env": "CF_TEST_SG_NAME",
            "script_key_env": "CF_TEST_SG_KEY"}}).shotgrid
        self.assertEqual(ShotGrid(conf).credentials(), ("comfyfarm", "s3cr=t"))
        os.environ["CF_TEST_SG_NAME"] = "from-env"
        self.addCleanup(os.environ.pop, "CF_TEST_SG_NAME", None)
        self.assertEqual(ShotGrid(conf).credentials(), ("from-env", "s3cr=t"))
        self.assertEqual(read_env_file(tmp / "missing.env"), {})

    def test_publish_check(self):
        sg, fake = lookups()
        self.assertIsNone(sg.check("bri3", "305046_0010"))
        self.assertIsNone(sg.check("bri3", "305046_0030"))          # omitted, but there: typed on purpose
        self.assertEqual(sg.check("bri3", "305046_0099"), "There's no shot 305046_0099 in bri3 in ShotGrid.")
        self.assertIsNone(sg.check("old1", "x_0010"))                # in ShotGrid, not active: Daiquiri decides
        self.assertEqual(sg.check("zzz9", "x_0010"), "There's no show zzz9 in ShotGrid.")
        # A shot made since the list was loaded is found by reloading it once.
        sg.shots("bc1")
        fake.shots[12].append({"code": "mis_0050", "sg_sequence": None, "sg_status_list": "wtg"})
        n = fake.count("Shot")
        self.assertIsNone(sg.check("bc1", "mis_0050"))
        self.assertEqual(fake.count("Shot"), n + 1)


class PublishCheckTests(unittest.TestCase):
    """The publisher refuses a shot ShotGrid doesn't have, before anything is staged."""

    def test_check_refuses_unknown_shot(self):
        from comfyfarm.fileio.ocio import REPO
        from comfyfarm.manager.db import JobStore
        from comfyfarm.manager.events import EventBus
        from comfyfarm.manager.farm import Farm
        from comfyfarm.workflows import ParamError, WorkflowRegistry
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        templates = tmp / "Nuke" / "ltx25_alpha_gen"
        templates.mkdir(parents=True)
        shutil.copy(Path(__file__).resolve().parent / "templates" / "publish_default.nk", templates / "default.nk")
        cfg = Config.model_validate({"workers": [], "database": str(tmp / "f.db"),
                                     "publish": {"enabled": True, "templates": str(templates.parent),
                                                 "show_folder": ""}})
        store = JobStore(tmp / "f.db")
        self.addCleanup(store.close)
        farm = Farm(cfg, store, WorkflowRegistry(REPO / "workflows"), EventBus())
        farm.shotgrid, _ = lookups()
        self.assertEqual(farm.publisher.check("ltx25_alpha_gen", {}, show="bri3", shot="305046_0010")["shot"],
                         "305046_0010")
        with self.assertRaises(ParamError) as e:
            farm.publisher.check("ltx25_alpha_gen", {"shot": "305046_0099"}, show="bri3", shot="305046_0010")
        self.assertIn("no shot 305046_0099 in bri3", str(e.exception))


if __name__ == "__main__":
    unittest.main()
