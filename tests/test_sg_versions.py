"""A ShotGrid Version link as a job's input (comfyfarm.shotgrid.versions, ShotGrid.version,
Farm._version_inputs): recognising links, ShotGrid's lone # padding, the Version lookup
and its cache, and jobs planned and submitted from a link (frames first, the movie when
they're gone, the Version's show and shot). Against a stand-in ShotGrid only."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.fileio.errors import SequenceError
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.shotgrid import ShotGrid
from comfyfarm.shotgrid import versions as V
from comfyfarm.shotgrid.lookup import NotFound, Unavailable
from comfyfarm.workflows import WorkflowRegistry

REPO = Path(__file__).resolve().parent.parent
SITE = "https://rodeofx.shotgrid.autodesk.com/"
LINK = "https://rodeofx.shotgrid.autodesk.com/detail/Version/4865327"
HI = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi"
MOVIE = "/rdo/shows/xyz/abc/abc_0010/movhistory/abc_0010.pre.v1.mov"


def version(**over):
    v = {"type": "Version", "id": 4865327, "code": "abc_0010.pre.v1", "sg_status_list": "rev",
         "sg_path_to_frames": f"{HI}/abc_0010.pre.v1.#.png", "sg_path_to_movie": MOVIE,
         "sg_first_frame": 1001, "sg_last_frame": 1003,
         "entity": {"type": "Shot", "id": 77, "name": "abc_0010"}, "project": {"type": "Project", "id": 12}}
    v.update(over)
    return v


class FakeShotGrid:
    def __init__(self, v=None):
        self.v = v if v is not None else version()
        self.calls: list[tuple] = []
        self.down = False

    def find_one(self, entity, filters, fields=None, **kw):
        self.calls.append(("find_one", entity, fields))
        if self.down:
            raise ConnectionError("Connection refused")
        if entity == "Version":
            return dict(self.v) if self.v and filters[0][2] == self.v["id"] else None
        if entity == "Shot":
            return {"type": "Shot", "id": 77, "code": "abc_0010", "sg_sequence": {"type": "Sequence", "name": "abc"}}
        if entity == "Project":
            return {"type": "Project", "id": 12, "tank_name": "xyz"}
        raise AssertionError(entity)

    def find(self, entity, filters, fields=None, **kw):
        self.calls.append(("find", entity, fields))
        return [{"type": "Project", "id": 12, "name": "Xyz", "tank_name": "xyz"}]


class Links(unittest.TestCase):
    def test_links_and_frames(self):
        self.assertEqual(V.parse(LINK), {"id": 4865327, "link": LINK, "host": "rodeofx.shotgrid.autodesk.com",
                                         "first": None, "last": None})
        page = "https://rodeofx.shotgrid.autodesk.com/page/9921#Version_4865327_abc_0010"
        self.assertEqual(V.parse(page)["id"], 4865327)
        self.assertEqual((V.parse(f"{LINK} 1001-1050")["first"], V.parse(f"{LINK} 1001-1050")["last"]), (1001, 1050))
        self.assertEqual((V.parse(f"  {LINK}  1050 ")["first"], V.parse(f"{LINK} 1050")["last"]), (1050, 1050))
        for not_one in ("/rdo/shows/xyz/Version_12/a.%04d.png", f"{HI}/x.#.exr 1001-1002",
                        "https://rodeofx.shotgrid.autodesk.com/detail/Shot/77", "", None, 12):
            self.assertIsNone(V.parse(not_one), not_one)
        self.assertTrue(V.same_site(V.parse(LINK), SITE))
        self.assertFalse(V.same_site(V.parse("https://other.shotgunstudio.com/detail/Version/1"), SITE))

    def test_input_text(self):
        self.assertEqual([V.input_text("/a/x.#.png", *r) for r in ((None, None), (1001, 1003), (1050, 1050))],
                         ["/a/x.#.png", "/a/x.#.png 1001-1003", "/a/x.#.png 1050"])


class LoneHash(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.pc = Config.model_validate({"workers": [], "paths": {"mappings": [
            {"posix": "/rdo/shows", "windows": self.dir.as_posix()}]}}).paths

    def test_padding_from_the_files(self):
        for f in (977, 1001):
            (self.dir / f"x.{f:04d}.exr").write_bytes(b"")
        (self.dir / "x.v2.0001.exr").write_bytes(b"")                 # another name: not counted
        self.assertEqual(V.pad_hash("/rdo/shows/x.#.exr", self.pc), "/rdo/shows/x.####.exr")
        self.assertEqual(V.pad_hash("/rdo/shows/x.####.exr", self.pc), "/rdo/shows/x.####.exr")   # already said
        self.assertEqual(V.pad_hash("/rdo/shows/x.%04d.exr", self.pc), "/rdo/shows/x.%04d.exr")
        self.assertEqual(V.pad_hash("/rdo/shows/gone/x.#.exr", self.pc), "/rdo/shows/gone/x.#.exr")

    def test_unpadded_numbers_stay_one_hash(self):
        for f in (1001, 1002):
            (self.dir / f"y.{f}.exr").write_bytes(b"")
        self.assertEqual(V.pad_hash("/rdo/shows/y.#.exr", self.pc), "/rdo/shows/y.#.exr")   # 1001 is 1001 either way


def lookups(v=None, **cfg):
    fake = FakeShotGrid(v)
    conf = Config.model_validate({"workers": [], "shotgrid": {"enabled": True, **cfg}}).shotgrid
    return ShotGrid(conf, connect=lambda: fake), fake


class Lookup(unittest.TestCase):
    def test_a_version(self):
        sg, fake = lookups()
        v = sg.version(4865327)
        self.assertEqual(v, {"id": 4865327, "code": "abc_0010.pre.v1", "url": f"{SITE}detail/Version/4865327",
                             "frames": f"{HI}/abc_0010.pre.v1.#.png", "movie": MOVIE, "first": 1001, "last": 1003,
                             "status": "rev", "entity_type": "Shot", "entity": "abc_0010", "show": "xyz",
                             "sequence": "abc", "shot": "abc_0010"})
        n = len(fake.calls)
        self.assertEqual(sg.version(4865327)["code"], "abc_0010.pre.v1")       # cached
        self.assertEqual(len(fake.calls), n)
        with self.assertRaises(NotFound):
            sg.version(1)

    def test_the_show_from_the_list_of_shows(self):
        sg, fake = lookups()
        sg.projects()
        sg.version(4865327)
        self.assertNotIn(("find_one", "Project"), [c[:2] for c in fake.calls])   # known from the list of shows
        self.assertEqual(sg.version(4865327)["show"], "xyz")

    def test_fields_of_the_sites_own(self):
        sg, fake = lookups(version(sg_path_to_frames=None, rdo_frames="/rdo/a.#.exr", sg_first_frame=None),
                           version_fields={"frames": "rdo_frames", "movie": "sg_path_to_movie"})
        v = sg.version(4865327)
        self.assertEqual((v["frames"], v["first"], v["last"]), ("/rdo/a.#.exr", None, None))
        self.assertIn("rdo_frames", fake.calls[0][2])                           # asked for by name

    def test_an_asset_and_no_paths(self):
        sg, _ = lookups(version(entity={"type": "Asset", "id": 5, "name": "dragon"}, sg_path_to_frames="",
                                sg_path_to_movie=None))
        v = sg.version(4865327)
        self.assertEqual((v["entity_type"], v["entity"], v["shot"], v["frames"], v["movie"]),
                         ("Asset", "dragon", None, None, None))

    def test_not_answering_or_off(self):
        sg, fake = lookups()
        fake.down = True
        with self.assertRaises(Unavailable):
            sg.version(4865327)
        off = ShotGrid(Config.model_validate({"workers": []}).shotgrid, connect=lambda: fake)
        with self.assertRaisesRegex(Unavailable, "lookups are off"):
            off.version(4865327)


class Jobs(unittest.TestCase):
    """Planned and submitted from a link, on frames laid out like a published render."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = self.tmp.replace("\\", "/")
        self.hi = Path(root, "shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi")
        self.hi.mkdir(parents=True)
        for f in range(1001, 1004):
            (self.hi / f"abc_0010.pre.v1.{f}.png").write_bytes(b"x")
        self.cfg = Config.model_validate({
            "database": f"{root}/farm.db", "workers": [],
            "paths": {"mappings": [{"posix": "/rdo/shows", "windows": f"{root}/shows"}],
                      "output_root": f"{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"},
            "shotgrid": {"enabled": True}, "preview": {"enabled": False}})
        store = JobStore(Path(self.cfg.database))
        self.addCleanup(store.close)
        self.farm = Farm(self.cfg, store, WorkflowRegistry(REPO / "workflows"), EventBus())
        self.use(version())

    def use(self, v):
        self.fake = FakeShotGrid(v)
        self.farm.shotgrid = ShotGrid(self.cfg.shotgrid, connect=lambda: self.fake)

    def plan(self, value, wf="ltx25_alpha_gen"):
        return self.farm.plan_summary(self.farm.plan(wf, {"input": value}))

    def test_its_frames_and_its_shot(self):
        r = self.plan(LINK)
        self.assertEqual((r["frames"], r["first"], r["last"], r["show"], r["seq"], r["shot"]),
                         (3, 1001, 1003, "xyz", "abc", "abc_0010"))
        src = r["sources"]["input"]
        self.assertEqual((src["code"], src["kind"], src["url"], src["path"]),
                         ("abc_0010.pre.v1", "frames", f"{SITE}detail/Version/4865327", f"{HI}/abc_0010.pre.v1.#.png"))
        self.assertIn("/shows/xyz/_project_ref/_cg/comfyui/renders/abc_0010", r["output_dir"])
        # Frames after the link: those frames.
        self.assertEqual(self.plan(f"{LINK} 1002-1003")["first"], 1002)

    def test_the_version_decides_the_shot(self):
        self.use(version(entity={"type": "Shot", "id": 78, "name": "abc_0020"}))
        self.fake.find_one = lambda e, f, fields=None, **k: (
            dict(self.fake.v) if e == "Version" else
            {"code": "abc_0020", "sg_sequence": {"name": "abc"}} if e == "Shot" else {"tank_name": "xyz"})
        r = self.plan(LINK)
        self.assertEqual(r["shot"], "abc_0020")                                  # not the path's abc_0010
        self.assertTrue(r["output_dir"].endswith("/abc_0020"), r["output_dir"])

    def test_the_movie_when_the_frames_are_gone(self):
        shutil.rmtree(self.hi)
        wf = self.farm.registry.get("ltx25_alpha_gen")
        use, sources, notes = self.farm._version_inputs(wf, {"input": LINK})
        self.assertEqual((use["input"], sources["input"]["kind"]), (MOVIE, "movie"))   # the whole movie
        self.assertIn("so its movie is", notes[0])
        use, _, _ = self.farm._version_inputs(wf, {"input": f"{LINK} 1010-1020"})
        self.assertEqual(use["input"], f"{MOVIE} 1010-1020")

    def test_refused(self):
        cases = [(version(sg_path_to_frames=None, sg_path_to_movie=None), LINK, "has no frames or movie path"),
                 (None, LINK, "There's no Version 4865327"),
                 (version(), "https://other.shotgunstudio.com/detail/Version/4865327", "not to the farm's ShotGrid")]
        for v, value, why in cases:
            self.use(v if v is not None else {})
            with self.assertRaises(SequenceError) as e:
                self.plan(value)
            self.assertIn(why, str(e.exception))
        self.use(version())
        self.fake.down = True
        with self.assertRaisesRegex(SequenceError, "needs the manager's ShotGrid lookups.*Paste the frames' path"):
            self.plan(LINK)

    def test_a_problem_with_its_frames_names_the_version(self):
        self.use(version(sg_path_to_movie=None, sg_last_frame=1010))           # frames 1004-1010 aren't there
        with self.assertRaisesRegex(SequenceError, "^ShotGrid Version abc_0010.pre.v1 \\(frames "):
            self.plan(LINK)

    def test_submitted_the_link_stays(self):
        job = self.farm.submit("ltx25_alpha_gen", {"input": LINK}, "ann")
        self.assertEqual(job["params"]["input"], LINK)                        # Run again asks ShotGrid again
        self.assertEqual(job["info"]["sources"]["input"]["id"], 4865327)
        self.assertEqual((job["info"]["shot"], job["info"]["input"]["first"]), ("abc_0010", 1001))
        self.assertTrue(job["info"]["input_posix"].endswith("/hi/abc_0010.pre.v1.#.png"))
        plain = self.farm.submit("ltx25_alpha_gen", {"input": f"{HI}/abc_0010.pre.v1.%04d.png 1001-1003"}, "ann")
        self.assertEqual(plain["info"]["sources"], {})


if __name__ == "__main__":
    unittest.main()
