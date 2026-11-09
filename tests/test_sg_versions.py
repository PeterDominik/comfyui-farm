"""A ShotGrid Version link as a job's input (comfyfarm.shotgrid.versions, ShotGrid.version,
Farm._version_inputs): recognising links, ShotGrid's lone # padding, the Version lookup
and its cache, and jobs planned and submitted from a link (frames first, the movie when
they're gone, the Version's show and shot); playlists and picked Versions as Batch
Submit's rows, and where ShotGrid's right-click goes. Against a stand-in ShotGrid only."""

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
from comfyfarm.shotgrid.lookup import NotFound, Refused, Unavailable
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


class Fault(Exception):
    """shotgun_api3's: ShotGrid answered with an error."""


class AuthenticationFault(Fault):
    """shotgun_api3's: the script or key is wrong."""


# Version 4865327 as the probe of Rodeo's site showed it (tools/sg_probe.py, 2026-11-08).
RODEO = {"type": "Version", "id": 4865327, "code": "yht_1040.lineup.autoLineup.v1", "sg_status_list": "pdlvr",
         "sg_path_to_frames": "/rdo/shows/bc1/.published/yht/yht_1040/render.comp/lineup.autoLineup/v1/main/hi/"
                              "yht_1040.lineup.autoLineup.v1.#.exr",
         "sg_path_to_movie": "/rdo/shows/bc1/yht/1040/movhistory/yht_1040.lineup.autoLineup.v1.mov",
         "sg_first_frame": 1001, "sg_last_frame": 1101,
         "entity": {"id": 258776, "name": "yht_1040", "type": "Shot"},
         "project": {"id": 7344, "name": "bc1", "type": "Project"}}


class FakeShotGrid:
    def __init__(self, v=None):
        self.v = v if v is not None else version()
        self.calls: list[tuple] = []
        self.down = False

    def find_one(self, entity, filters, fields=None, **kw):
        self.calls.append(("find_one", entity, fields))
        if self.down:
            raise self.down if isinstance(self.down, Exception) else ConnectionError("Connection refused")
        if entity == "Version":
            return dict(self.v) if self.v and filters[0][2] == self.v["id"] else None
        if entity == "Playlist":
            if filters[0][2] != 265342:
                return None
            return {"type": "Playlist", "id": 265342, "code": "XYZ - Client Submission 001", "project": {"type": "Project", "id": 12},
                    "versions": [{"type": "Version", "id": i} for i in (12, 11, 13)]}
        if entity == "Shot":
            # ShotGrid's own sequence can be an editorial one (yht_1040's is "child"): not asked for
            return {"type": "Shot", "id": 77, "code": "abc_0010", "sg_sequence": {"type": "Sequence", "name": "child"}}
        if entity == "Project":
            return {"type": "Project", "id": 12, "tank_name": "bc1" if filters[0][2] == 7344 else "xyz"}
        raise AssertionError(entity)

    def find(self, entity, filters, fields=None, **kw):
        self.calls.append(("find", entity, fields))
        if entity == "Project":
            return [{"type": "Project", "id": 12, "name": "Xyz", "tank_name": "xyz"}]
        if entity == "PlaylistVersionConnection":
            if self.no_order:
                raise Fault("API read() PlaylistVersionConnection.sg_sort_order doesn't exist")
            self.order_asked = kw.get("order")
            return sorted(self.links, key=lambda c: c["sg_sort_order"])     # as ShotGrid sorts them
        if entity == "Version":
            ids = filters[0][2]
            return [dict(v) for v in self.playlist_versions if v["id"] in ids]   # in no particular order
        raise AssertionError(entity)

    # A playlist of three Versions; its order is the connections' sg_sort_order.
    playlist_versions = [version(id=11, code="abc_0010.lineup.v1"),
                         version(id=12, code="abc_0020.lineup.v1", entity={"type": "Shot", "id": 78, "name": "abc_0020"}),
                         version(id=13, code="abc_0030.lineup.v2", entity={"type": "Shot", "id": 79, "name": "abc_0030"})]
    links = [{"version": {"type": "Version", "id": 13}, "sg_sort_order": 1},
             {"version": {"type": "Version", "id": 11}, "sg_sort_order": 2},
             {"version": {"type": "Version", "id": 12}, "sg_sort_order": 3}]
    no_order = False


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

    def test_rodeos_version(self):
        sg, fake = lookups(RODEO)
        v = sg.version(4865327)
        self.assertEqual((v["show"], v["sequence"], v["shot"], v["first"], v["last"]),
                         ("bc1", "yht", "yht_1040", 1001, 1101))        # the pipeline's sequence, not "child"
        self.assertTrue(v["frames"].endswith("/hi/yht_1040.lineup.autoLineup.v1.#.exr"))
        self.assertNotIn("Shot", [c[1] for c in fake.calls])             # the shot's name is enough

    def test_a_refused_request_isnt_shotgrid_being_down(self):
        sg, fake = lookups(version_fields={"frames": "sg_no_such_field"})
        fake.down = Fault("API read() Version.sg_no_such_field doesn't exist")
        with self.assertRaises(Refused) as e:
            sg.version(4865327)
        self.assertIn("sg_no_such_field", str(e.exception))
        self.assertIsNone(sg.why())                                      # the show and shot lists carry on
        fake.down = AuthenticationFault("Authentication failed")         # a wrong key fails everything: wait
        sg.cfg.version_fields = {"frames": "sg_path_to_frames"}
        with self.assertRaises(Unavailable) as e:
            sg.version(4865327)
        self.assertNotIsInstance(e.exception, Refused)
        self.assertIn("didn't answer", sg.why())

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


class Playlists(unittest.TestCase):
    def test_its_versions_in_its_order(self):
        sg, fake = lookups()
        pl = sg.playlist(265342)
        self.assertEqual((pl["code"], pl["url"], pl["show"]),
                         ("XYZ - Client Submission 001", f"{SITE}detail/Playlist/265342", "xyz"))
        self.assertEqual([v["code"] for v in pl["versions"]], ["abc_0030.lineup.v2", "abc_0010.lineup.v1", "abc_0020.lineup.v1"])
        self.assertEqual(fake.order_asked[0], {"field_name": "sg_sort_order", "direction": "asc"})
        self.assertEqual(sum(1 for c in fake.calls if c[:2] == ("find_one", "Project")), 1)   # one show, asked once
        n = len(fake.calls)
        self.assertEqual(sg.version(12)["shot"], "abc_0020")                     # the rows' checks: cached
        self.assertEqual(sg.playlist(265342)["id"], 265342)
        self.assertEqual(len(fake.calls), n)

    def test_without_the_playlists_own_order(self):
        sg, fake = lookups()
        fake.no_order = True
        self.assertEqual([v["id"] for v in sg.playlist(265342)["versions"]], [12, 11, 13])   # as the Playlist lists them
        self.assertIsNone(sg.why())

    def test_not_there(self):
        sg, _ = lookups()
        with self.assertRaises(NotFound):
            sg.playlist(1)

    def test_links(self):
        self.assertEqual(V.parse_playlist(f"{SITE}detail/Playlist/265342")["id"], 265342)
        self.assertEqual(V.parse_playlist(f"{SITE}page/9921#Playlist_265342")["id"], 265342)
        self.assertEqual(V.parse_playlist(" 265342 "), {"id": 265342, "link": "265342", "host": ""})
        for not_one in (LINK, "/rdo/shows/xyz", "", None, "Playlist 265342"):
            self.assertIsNone(V.parse_playlist(not_one), not_one)


class Picked(unittest.TestCase):
    """Versions picked in ShotGrid (its right-click on a selection, chunk 13): where the
    right-click opens the dashboard, and the Versions looked up together."""

    def test_where_the_right_click_goes(self):
        go = lambda **f: V.action_target(f, SITE)      # noqa: E731
        self.assertEqual(go(entity_type="Version", selected_ids="4865327", server_hostname="rodeofx.shotgrid.autodesk.com"),
                         "#submit?input=" + LINK.replace(":", "%3A").replace("/", "%2F"))
        self.assertEqual(go(entity_type="Version", selected_ids="13,11,12,11", workflow="ltx25_alpha_gen"),
                         "#batch?workflow=ltx25_alpha_gen&versions=13,11,12")      # in ShotGrid's order, each once
        self.assertEqual(go(entity_type="Playlist", selected_ids="265346,265342"),
                         "#batch?playlist=https%3A%2F%2Frodeofx.shotgrid.autodesk.com%2Fdetail%2FPlaylist%2F265346")
        self.assertEqual(go(entity_type="Version", ids="7"), "#submit?input=" + f"{SITE}detail/Version/7".replace(
            ":", "%3A").replace("/", "%2F"))                                     # selection not required: the page's
        self.assertNotIn("workflow", go(entity_type="Version", selected_ids="7", workflow="../x y"))
        for form, why in (({"entity_type": "Shot", "selected_ids": "258776"}, "that was a Shot"),
                          ({"entity_type": "Version"}, "Nothing was selected: select a Version"),
                          ({"entity_type": "Version", "selected_ids": "1", "server_hostname": "x.shotgunstudio.com"},
                           "came from x.shotgunstudio.com; this farm uses https://rodeofx.shotgrid.autodesk.com"),
                          ({"entity_type": "Playlist", "selected_ids": "a1"}, "'a1' isn't a Playlist id"),
                          ({"entity_type": "Version", "selected_ids": ",".join(map(str, range(1, 502)))}, "up to 500")):
            with self.assertRaises(ValueError) as e:
                V.action_target(form, SITE)
            self.assertIn(why, str(e.exception))

    def test_the_menus_actions(self):
        """The item's own action (comfyfarm.shotgrid.menu) and ShotGrid's user (chunk 13c)."""
        p = V.picked({"entity_type": "Version", "selected_ids": "11,12", "action": "publish", "workflow": "ltx25_alpha_gen",
                      "user_login": "jminett"}, SITE)
        self.assertEqual((p["action"], p["user"], p["links"][1]), ("publish", "jminett", f"{SITE}detail/Version/12"))
        self.assertEqual(V.deep_link(p), "#batch?workflow=ltx25_alpha_gen&user=jminett&publish=1&send=1&versions=11,12")
        self.assertEqual(V.deep_link({**p, "action": "submit"}),
                         "#batch?workflow=ltx25_alpha_gen&user=jminett&send=1&versions=11,12")
        self.assertEqual(V.deep_link({**p, "action": "open"}), "#batch?workflow=ltx25_alpha_gen&user=jminett&versions=11,12")
        self.assertEqual(V.deep_link({**p, "action": "open", "publish": True}),            # Open, Publish ticked
                         "#batch?workflow=ltx25_alpha_gen&user=jminett&publish=1&versions=11,12")
        self.assertFalse(p["publish"])                                         # the manager decides that
        one = V.picked({"entity_type": "Version", "selected_ids": "11", "action": "publish", "workflow": "x"}, SITE)
        self.assertEqual(V.deep_link(one), "#submit?workflow=x&publish=1&input=" + f"{SITE}detail/Version/11".replace(
            ":", "%3A").replace("/", "%2F"))                                   # the form, when the manager can't send it
        pl = V.picked({"entity_type": "Playlist", "selected_ids": "265346", "action": "submit"}, SITE)
        self.assertEqual(pl["action"], "open")                                # a Playlist only opens
        self.assertNotIn("send", V.deep_link(pl))
        odd = V.picked({"entity_type": "Version", "selected_ids": "1", "action": "delete", "user_login": "a b"}, SITE)
        self.assertEqual((odd["action"], odd["user"]), ("open", None))

    def test_ids(self):
        self.assertEqual(V.version_ids("3, 1 2,3"), [3, 1, 2])
        self.assertEqual(V.version_ids([5, "6"]), [5, 6])
        for bad in ("", "1,x", "0", None):
            with self.assertRaises(ValueError):
                V.version_ids(bad)

    def test_the_form_shotgrid_posts(self):
        body = b"user_login=pdominik&entity_type=Version&selected_ids=11%2C12&cols=code&cols=entity&project_name=bc1"
        self.assertEqual(V.form_fields("application/x-www-form-urlencoded", body),
                         {"user_login": "pdominik", "entity_type": "Version", "selected_ids": "11,12", "cols": "entity",
                          "project_name": "bc1"})
        multi = (b"--XyZ\r\nContent-Disposition: form-data; name=\"entity_type\"\r\n\r\nPlaylist\r\n"
                 b"--XyZ\r\nContent-Disposition: form-data; name=\"selected_ids\"\r\n\r\n265346\r\n--XyZ--\r\n")
        self.assertEqual(V.form_fields("multipart/form-data; boundary=XyZ", multi),
                         {"entity_type": "Playlist", "selected_ids": "265346"})
        self.assertEqual(V.form_fields("text/plain", b""), {})

    def test_looked_up_together(self):
        sg, fake = lookups()
        got = sg.versions([13, 99, 11])
        self.assertEqual([v["code"] for v in got], ["abc_0030.lineup.v2", "abc_0010.lineup.v1"])    # in the order asked
        self.assertEqual([c[:2] for c in fake.calls if c[1] == "Version"], [("find", "Version")])  # one request
        n = len(fake.calls)
        self.assertEqual(sg.version(13)["shot"], "abc_0030")                     # the rows' checks: cached
        self.assertEqual([v["id"] for v in sg.versions([11, 13])], [11, 13])
        self.assertEqual(len(fake.calls), n)
        sg.versions([12, 11])
        self.assertEqual(fake.calls[-1][:2], ("find", "Version"))               # only 12 asked for
        self.assertEqual(sg.versions([99]), [])


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

    def test_a_refused_request(self):
        self.fake.down = Fault("API read() Version.sg_path_to_framez doesn't exist")
        with self.assertRaisesRegex(SequenceError, "ShotGrid couldn't give Version 4865327's paths.*version_fields"):
            self.plan(LINK)

    def test_a_problem_with_its_frames_names_the_version(self):
        self.use(version(sg_path_to_movie=None, sg_last_frame=1010))           # frames 1004-1010 aren't there
        with self.assertRaisesRegex(SequenceError, "^ShotGrid Version abc_0010.pre.v1 \\(frames "):
            self.plan(LINK)

    def test_a_playlist_as_batch_rows(self):
        from comfyfarm.manager.farm import FarmError
        out = self.farm.batch_playlist(f"{SITE}detail/Playlist/265342")
        self.assertEqual(out["playlist"]["code"], "XYZ - Client Submission 001")
        self.assertEqual([r["input"] for r in out["rows"]],
                         [f"{SITE}detail/Version/{i}" for i in (13, 11, 12)])     # each row: its Version's link
        self.fake.v = None                                                      # the row checks use the cache
        self.assertEqual(self.plan(out["rows"][1]["input"])["sources"]["input"]["code"], "abc_0010.lineup.v1")
        cases = [("/rdo/shows/xyz", "not a ShotGrid Playlist link", 400),
                 ("https://other.shotgunstudio.com/detail/Playlist/265342", "not to the farm's ShotGrid", 400),
                 ("999", "There's no Playlist 999", 404)]
        for link, why, status in cases:
            with self.assertRaises(FarmError) as e:
                self.farm.batch_playlist(link)
            self.assertIn(why, str(e.exception))
            self.assertEqual(e.exception.status, status)
        self.farm.shotgrid = ShotGrid(Config.model_validate({"workers": []}).shotgrid, connect=lambda: self.fake)
        with self.assertRaises(FarmError) as e:
            self.farm.batch_playlist("265342")
        self.assertEqual(e.exception.status, 503)
        self.assertIn("lookups are off", str(e.exception))

    def test_picked_versions_as_batch_rows(self):
        from comfyfarm.manager.farm import FarmError
        out = self.farm.batch_versions("13, 99,11")
        self.assertEqual(out["source"], {"code": "2 Versions picked in ShotGrid", "show": "xyz", "ids": [13, 11], "missing": [99]})
        self.assertEqual([r["input"] for r in out["rows"]], [f"{SITE}detail/Version/{i}" for i in (13, 11)])
        self.assertEqual(self.plan(out["rows"][0]["input"])["shot"], "abc_0030")
        for ids, why, status in (("x", "isn't a Version id", 400), ("", "No Versions given", 400),
                                 ("98,99", "There's no Version 98, 99", 404)):
            with self.assertRaises(FarmError) as e:
                self.farm.batch_versions(ids)
            self.assertIn(why, str(e.exception))
            self.assertEqual(e.exception.status, status)
        self.farm.shotgrid = ShotGrid(Config.model_validate({"workers": []}).shotgrid, connect=lambda: self.fake)
        with self.assertRaises(FarmError) as e:
            self.farm.batch_versions("13")
        self.assertEqual(e.exception.status, 503)

    def test_submitted_from_shotgrids_menu(self):
        """Its right-click Submit on one Version (chunk 13c): as the ShotGrid user, defaults."""
        from comfyfarm.manager.farm import FarmError
        job = self.farm.shotgrid_submit("ltx25_alpha_gen", LINK, "jminett")
        self.assertEqual((job["user"], job["params"]["input"], job["info"]["shot"]), ("jminett", LINK, "abc_0010"))
        self.assertIsNone(job["info"].get("publish_request"))
        self.assertEqual(self.farm.shotgrid_submit("ltx25_alpha_gen", LINK, None)["user"], "anonymous")
        for wf, why in (("ltx25_layout_render", "can't be submitted from a Version alone: it also needs Reference"),
                        ("wan22_i2i", "it takes one image, so a Version's frame has to be picked")):
            with self.assertRaisesRegex(FarmError, why):
                self.farm.shotgrid_submit(wf, LINK, "jminett")
        with self.assertRaisesRegex(FarmError, "publishing isn't switched on"):        # Submit + Publish, publishing off
            self.farm.shotgrid_submit("ltx25_alpha_gen", LINK, "jminett", publish=True)
        self.use({})                                                            # a Version ShotGrid doesn't have
        with self.assertRaisesRegex(SequenceError, "There's no Version 4865327"):     # the job's own refusals
            self.farm.shotgrid_submit("ltx25_alpha_gen", LINK, "jminett")

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
