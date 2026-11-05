"""Nuke helpers (with a stand-in `nuke` module) and the panel's logic (with a stand-in
farm). The Qt widgets themselves only run inside Nuke.

    python -m unittest tests.test_nuke -v
"""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for sub in ("client", "client/nuke"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))
farm = importlib.import_module("comfyfarm_client")


# ---------------------------------------------------------------- a small fake Nuke
class Knob:
    def __init__(self, value):
        self._v = value

    def value(self):
        return self._v

    def setValue(self, v):  # noqa: N802 (Nuke naming)
        self._v = v


class Node:
    def __init__(self, cls, name, x=0, y=0, **knobs):
        self._cls, self._name, self._x, self._y = cls, name, x, y
        self._knobs = {k: Knob(v) for k, v in knobs.items()}
        self._knobs.setdefault("label", Knob(""))
        self.selected = False

    def Class(self):  # noqa: N802
        return self._cls

    def name(self):
        return self._name

    def knobs(self):
        return self._knobs

    def __getitem__(self, k):
        return self._knobs[k]

    def xpos(self):
        return self._x

    def ypos(self):
        return self._y

    def setXYpos(self, x, y):  # noqa: N802
        self._x, self._y = x, y

    def setSelected(self, v):  # noqa: N802
        self.selected = v


def fake_nuke(nodes, first=1001, last=1100):
    n = types.ModuleType("nuke")
    n.messages = []
    root = types.SimpleNamespace(firstFrame=lambda: first, lastFrame=lambda: last)
    n.root = lambda: root
    n.frame = lambda: 1050.0                   # the viewer's current frame
    n.selectedNodes = lambda: [x for x in nodes if x.selected]
    n.allNodes = lambda cls=None, recurseGroups=False: [x for x in nodes if cls is None or x.Class() == cls]
    n.toNode = lambda name: next((x for x in nodes if x.name() == name), None)
    n.filename = lambda node: node["file"].value()
    n.message = n.messages.append

    def read(**kw):
        node = Node("Read", f"Read{len(nodes) + 1}", **kw)
        nodes.append(node)
        return node
    n.nodes = types.SimpleNamespace(Read=read)
    return n


SEQ = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/abc_0010.pre.v1.%04d.png"


def done_job(**kw):
    job = {"id": "a1b2c3d4-0000", "short_id": "a1b2c3d4", "status": "done", "workflow": "ltx25_alpha_gen",
           "info": {"input": {"raw": f"{SEQ} 1001-1012"}, "input_posix": SEQ},
           "outputs": {"frame_pattern": "//srv/shows/xyz/out/abc_0010.pre.v1.alpha_v002.%04d.png",
                       "frame_pattern_posix": "/rdo/shows/xyz/out/abc_0010.pre.v1.alpha_v002.%04d.png",
                       "first": 1001, "last": 1012}}
    job.update(kw)
    return job


class NukeHelpers(unittest.TestCase):
    def load(self, nodes, posix=True, **kw):
        """The helpers with a stand-in Nuke, on a machine that sees show storage as Linux
        paths (as artists' workstations do) unless posix=False (a Windows machine)."""
        fake = fake_nuke(nodes, **kw)
        for patcher in (mock.patch.dict(sys.modules, {"nuke": fake}), mock.patch.object(farm, "POSIX", posix)):
            patcher.start()
            self.addCleanup(patcher.stop)
        sys.modules.pop("comfyfarm_nuke", None)
        self.addCleanup(sys.modules.pop, "comfyfarm_nuke", None)
        return importlib.import_module("comfyfarm_nuke"), fake

    def test_inputs_from_nodes(self):
        nodes = [
            Node("Read", "Read1", y=50, file=SEQ, first=1003, last=1010),
            Node("Read", "Read2", y=10, file="R:\\shows\\x\\plate.####.png", first=1, last=5),
            Node("Read", "Mov", y=20, file="/rdo/shows/xyz/abc/0010/movhistory/a.v2.mov", first=1, last=40),
            Node("Write", "WriteAll", y=30, file="/rdo/shows/xyz/work/pre.%04d.png", use_limit=False, first=1, last=2),
            Node("Write", "WriteLimit", y=40, file="/rdo/shows/xyz/work/pre.%04d.png", use_limit=True, first=1010, last=1020),
            Node("Read", "Still", y=60, file="/rdo/shows/xyz/work/still.png", first=1, last=1),
            Node("Blur", "Blur1", y=0),
        ]
        nk, _ = self.load(nodes)
        for n in nodes:
            n.selected = True
        self.assertEqual(nk.selected_sequences(), [
            ("Read2", "R:/shows/x/plate.####.png 1-5"),           # top of the DAG first
            ("Mov", "/rdo/shows/xyz/abc/0010/movhistory/a.v2.mov"),   # movies: whole file
            ("WriteAll", "/rdo/shows/xyz/work/pre.%04d.png 1001-1100"),   # script range
            ("WriteLimit", "/rdo/shows/xyz/work/pre.%04d.png 1010-1020"),
            ("Read1", f"{SEQ} 1003-1010"),
            ("Still", "/rdo/shows/xyz/work/still.png"),
        ])

    def test_one_image_workflows_take_the_current_frame(self):
        nodes = [Node("Read", "Plate", file=SEQ, first=1003, last=1100),
                 Node("Read", "Mov", y=20, file="/rdo/shows/xyz/abc/0010/movhistory/a.v2.mov", first=1, last=40),
                 Node("Read", "Still", y=60, file="/rdo/shows/xyz/work/still.png", first=1, last=1)]
        nk, _ = self.load(nodes)
        for n in nodes:
            n.selected = True
        self.assertEqual(nk.selected_sequences(one_frame=True), [
            ("Plate", f"{SEQ} 1050"),
            ("Mov", "/rdo/shows/xyz/abc/0010/movhistory/a.v2.mov"),   # timecode numbering: the farm asks for it
            ("Still", "/rdo/shows/xyz/work/still.png"),
        ])

    def test_create_read_next_to_its_source_once(self):
        src = Node("Read", "Plate", x=100, y=200, file=SEQ.replace("%04d", "####"), first=1001, last=1012)
        nodes = [src]
        nk, _ = self.load(nodes)
        read = nk.create_read(done_job())
        self.assertEqual(read["file"].value(), farm.output_pattern(done_job()))
        self.assertEqual((read["first"].value(), read["last"].value()), (1001, 1012))
        self.assertEqual((read.xpos(), read.ypos()), (220, 200))   # found by its path
        self.assertIn("a1b2c3d4", read["label"].value())
        self.assertTrue(read.selected)
        self.assertIs(nk.create_read(done_job()), read)          # not a second one
        self.assertEqual(len(nodes), 2)
        v3 = done_job(short_id="ffff0000", outputs=dict(done_job()["outputs"], frame_pattern_posix="/rdo/v3.%04d.png"))
        self.assertEqual(nk.create_read(v3).xpos(), 340)        # next to the earlier result, not on it

    def test_texture_job_gets_a_read_per_map(self):
        tex = "/rdo/shows/xyz/_asset/rockA/cg/textures/src/rockA_diffuse.%04d.png"
        src = Node("Read", "Diffuse", x=0, y=50, file=tex.replace("%04d", "####"), first=1001, last=1011)
        nodes = [src]
        nk, _ = self.load(nodes)
        base = "/rdo/shows/xyz/out/rockA_diffuse.chord_v001/acescg_map1_noTile_defState_"
        maps = {c: {"pattern": "//srv" + base[4:] + f"{c}.%04d.0000.exr", "pattern_posix": f"{base}{c}.%04d.0000.exr",
                    "first": 1001, "last": 1011, "files": 3} for c in ("color01", "nrm02", "bmp02")}
        maps["nrm02"].update(first=1001, last=1003, files=3)   # no gaps: no black frames needed
        job = done_job(info={"input": {"raw": tex}, "input_posix": tex},
                       outputs={"maps": maps, "frame_pattern_posix": maps["color01"]["pattern_posix"], "files": 9})
        first = nk.create_read(job)
        reads = [n for n in nodes if n is not src]
        self.assertEqual([r["file"].value() for r in reads], [m["pattern_posix"] for m in maps.values()])
        self.assertIs(first, reads[0])
        self.assertEqual([r.xpos() for r in reads], [120, 240, 360])     # in a row next to the diffuse Read
        self.assertEqual(reads[0]["on_error"].value(), "black")           # tiles 1003-1010 don't exist
        self.assertNotIn("on_error", reads[1].knobs())
        self.assertIn("bmp02", reads[2]["label"].value())
        self.assertTrue(all(r.selected for r in reads))
        nk.create_read(job)
        self.assertEqual(len(nodes), 4)                                   # already read in: no duplicates

    def test_windows_reads_use_the_farm_paths(self):
        src = Node("Read", "Plate", x=100, y=200, file=SEQ.replace("%04d", "####"), first=1001, last=1012)
        nodes = [src]
        nk, _ = self.load(nodes, posix=False)
        read = nk.create_read(done_job())
        self.assertEqual(read["file"].value(), done_job()["outputs"]["frame_pattern"])
        base = "/rdo/shows/xyz/out/rockA_diffuse.chord_v001/acescg_map1_noTile_defState_"
        maps = {"color01": {"pattern": "//srv" + base[4:] + "color01.%04d.0000.exr",
                            "pattern_posix": f"{base}color01.%04d.0000.exr", "first": 1001, "last": 1001, "files": 1}}
        reads = nk.create_map_reads(done_job(outputs={"maps": maps}))
        self.assertEqual([r["file"].value() for r in reads], [maps["color01"]["pattern"]])

    def test_panel_expression_needs_no_imports(self):
        # Nuke builds the panel by evaluating this text in __main__, which hasn't imported
        # anything of ours (a bare "comfyfarm_panel.ComfyFarmPanel" gave a NameError there).
        nk, _ = self.load([])
        widget_class = type("ComfyFarmPanel", (), {})
        with mock.patch.dict(sys.modules, {"comfyfarm_panel": types.SimpleNamespace(ComfyFarmPanel=widget_class)}):
            self.assertIs(eval(nk.PANEL_WIDGET, {"__builtins__": __builtins__}), widget_class)

    def test_create_read_near_named_node_and_unfinished_job(self):
        other = Node("Write", "WritePre", x=-50, y=10, file="/x/y.%04d.png")
        nodes = [other]
        nk, fake = self.load(nodes)
        read = nk.create_read(done_job(info={}), near="WritePre")
        self.assertEqual((read.xpos(), read.ypos()), (70, 10))
        self.assertIsNone(nk.create_read(done_job(status="failed", outputs={})))
        self.assertTrue(fake.messages)


class FakeFarm:
    FarmError = farm.FarmError
    FINISHED = farm.FINISHED

    def __init__(self):
        self.listed, self.by_id, self.submitted = [], {}, []

    def submit(self, workflow, params, name=None):
        self.submitted.append((workflow, params, name))
        if "bad" in params.get("input", ""):
            raise farm.FarmError("No frames")
        job = {"id": f"job{len(self.submitted)}", "status": "queued"}
        self.by_id[job["id"]] = job
        return job

    def jobs(self, user=None, active=False, limit=50):
        return list(self.listed)

    def get(self, job_id):
        return self.by_id[job_id]


WF = {"name": "wf", "params": [
    {"name": "input", "type": "sequence"}, {"name": "output_path", "type": "output_folder"},
    {"name": "seed", "type": "int"}, {"name": "prompt", "type": "text"},
    {"name": "flip", "type": "bool"}, {"name": "size", "type": "choice", "choices": [512, 768]}]}


class PanelLogic(unittest.TestCase):
    def setUp(self):
        self.panel = importlib.import_module("comfyfarm_panel")
        self.client = FakeFarm()
        self.m = self.panel.PanelModel(client=self.client, user="peter")
        self.m.workflows = [WF]

    def test_one_image_workflows(self):
        PM = self.panel.PanelModel
        self.assertTrue(PM.takes_one({"params": [{"name": "input", "type": "sequence", "takes": "image"}]}))
        self.assertFalse(PM.takes_one({"params": [{"name": "input", "type": "sequence", "takes": "images"}]}))
        self.assertFalse(PM.takes_one({"params": [{"name": "input", "type": "sequence"}]}))   # an older farm

    def test_form_to_params(self):
        params = self.m.params_from_form(WF, {"input": " /a.%04d.png 1-2 ", "output_path": "   ", "seed": " 7 ",
                                              "prompt": "  keep  spaces ", "flip": 0, "size": "768", "extra": "x"})
        self.assertEqual(params, {"input": "/a.%04d.png 1-2", "seed": "7", "prompt": "  keep  spaces ",
                                  "flip": False, "size": 768})

    def test_poll_reports_each_finished_job_once(self):
        a = self.m.submit("wf", {"input": "/a.%04d.png 1-2"}, source_node="Read1")
        b = self.m.submit("wf", {"input": "/b.%04d.png 1-2"})
        self.client.listed = [dict(a, status="running"), dict(b, status="queued")]
        self.assertEqual(self.m.poll()[1], [])
        self.client.listed = [dict(b, status="running")]               # a left the active list...
        self.client.by_id[a["id"]] = dict(a, status="done")             # ...because it finished
        self.assertEqual([j["id"] for j in self.m.poll()[1]], [a["id"]])
        self.client.listed = [dict(b, status="failed")]
        self.assertEqual([j["status"] for j in self.m.poll()[1]], ["failed"])
        self.assertEqual((self.m.poll()[1], self.m.session_jobs, self.m.sources), ([], {}, {a["id"]: "Read1"}))

    def test_rows(self):
        now = 10_000.0
        labels = {"ltx25_alpha_gen": "LTX 2.5 alpha generation"}
        q = self.panel.PanelModel.row({"id": "abcdef1234", "status": "queued", "user": "jane", "name": "abc_0010",
                                       "workflow": "ltx25_alpha_gen", "queue_position": 2, "eta_seconds": 125,
                                       "created_at": now - 30, "info": {"version": 3}}, now, labels)
        self.assertEqual(q, ["abcdef12", "abc_0010  v003", "LTX 2.5 alpha generation", "", "jane", "queued", "",
                             "#2 · ~2m 05s", "just now", ""])        # queued: no machine yet
        f = self.panel.PanelModel.row({"id": "x" * 10, "short_id": "xxxxxxxx", "status": "failed", "user": "j",
                                       "workflow": "chord_pbr", "worker": "wrk1125l-3090",
                                       "error": "CUDA out of memory", "created_at": now - 7200,
                                       "started_at": now - 100, "finished_at": now - 40}, now, labels)
        self.assertEqual(f[2:4], ["chord_pbr", "wrk1125l-3090"])     # no label known: the workflow's name
        cols = self.panel.COLUMNS
        self.assertEqual((f[cols.index("Progress")], f[cols.index("Submitted")], f[cols.index("Took")]),
                         ("CUDA out of memory", "2 h ago", "1m 00s"))
        s = self.panel.PanelModel.row({"id": "s" * 10, "status": "running", "kind": "set", "workflow": "chord_pbr"}, now)
        self.assertEqual(s[3], "per batch")                           # a texture set's batches run anywhere
        self.assertEqual(self.m.labels(), {"wf": "wf"})

    def test_machines(self):
        now = 10_000.0
        PM = self.panel.PanelModel
        w = {"name": "wrk59467w-5090", "rank": 1, "online": True, "connected": True, "checked": True, "inflight": 1,
             "max_inflight": 2, "last_seen": now - 2, "comfyui_version": "0.33.0", "agent_version": "0.1.0+590a1d1",
             "gpu": {"name": "cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync", "vram_free": 21 * 2**30,
                     "vram_total": 32 * 2**30}, "problems": {}}
        jobs = [{"id": "a", "name": "abc_0010", "worker": "wrk59467w-5090", "status": "running"},
                {"id": "b", "name": "old", "worker": "wrk59467w-5090", "status": "done"},
                {"id": "c", "name": "elsewhere", "worker": "wrk1125l-3090", "status": "running"}]
        self.assertEqual(PM.machine_row(w, jobs, now),
                         ["1", "wrk59467w-5090", "busy", "NVIDIA GeForce RTX 5090", "21.0 GB of 32.0 GB", "1/2",
                          "abc_0010", "0.33.0", "590a1d1", "just now"])
        ranked = PM.machines([dict(w, name="c", rank=None), dict(w, name="b", rank=2), w])
        self.assertEqual([m["name"] for m in ranked], ["wrk59467w-5090", "b", "c"])   # in the order they get jobs
        self.assertEqual(PM.machine_state(dict(w, inflight=0))[0], "ready")
        self.assertEqual(PM.machine_state(dict(w, online=False))[0], "offline")
        self.assertEqual(PM.machine_state(dict(w, checked=False))[0], "checking nodes")
        self.assertEqual(PM.machine_state(dict(w, problems={"chord_pbr": ["x"]}))[0], "can't run chord_pbr")
        # After a pull changed the farm nodes: no new jobs until ComfyUI restarts.
        for state, shown in (("waiting", "new farm nodes: restarting ComfyUI when idle"),
                             ("manual", "farm nodes out of date")):
            self.assertEqual(PM.machine_state(dict(w, farm_nodes={"state": state, "message": "m"}))[0], shown)
        restarting = dict(w, online=False, farm_nodes={"state": "restarting", "message": "m"})
        self.assertEqual(PM.machine_state(restarting)[0], "restarting ComfyUI (new farm nodes)")
        self.assertEqual(PM.online([w, dict(w, farm_nodes={"state": "waiting"})]), 1)
        line = PM.summary({"queued": 3}, [w, dict(w, name="b", online=False), dict(w, problems={"x": ["y"]})], "peter")
        self.assertIn("2 of 3 machines online", line)      # one lacking a workflow's nodes still takes jobs
        self.assertIn("3 queued", line)

    def test_destination_text(self):
        html = self.panel.PanelModel.destination_html({
            "frames": 12, "output_dir_posix": "/rdo/shows/x/out/<shot>", "first_file": "a.1001.png",
            "last_file": "a.1012.png", "version": 2, "notes": ["No shot & no seq"], "picked": True,
            "convert": {"kind": "movie", "codec": "prores", "width": 1920, "height": 1080, "fps": 23.976,
                        "timecode": "00:00:41:16", "start": 1001, "end": 1012, "slate_frame": 1000}})
        for part in ("12 frames", "/rdo/shows/x/out/&lt;shot&gt;/", "a.1001.png</code> … <code>a.1012.png",
                     "so this run is v002", "No shot &amp; no seq", "23.976 fps", "first frame (1000) is a slate"):
            self.assertIn(part, html)
        html = self.panel.PanelModel.destination_html({
            "frames": 3, "what": "15 EXRs (5 maps × 3 UDIM tiles)", "maps": ["color01", "nrm02"],
            "output_dir_posix": "/rdo/x/rockA_diffuse.chord_v001", "first_file": "acescg_map1_noTile_defState_color01.1001.0000.exr",
            "version": 1, "picked": False, "asset": "rockA", "show": "xyz",
            "convert": {"kind": "exr", "mode": "colorconvert", "from": "scene_linear", "to": "color_picking",
                        "config": "ocio/config.ocio"}})
        for part in ("15 EXRs (5 maps × 3 UDIM tiles)", "<code>nrm02</code>",
                     "<code>scene_linear</code> → <code>color_picking</code> with <code>ocio/config.ocio</code>",
                     "Asset <code>rockA</code>"):
            self.assertIn(part, html)


if __name__ == "__main__":
    unittest.main()
