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
    def load(self, nodes, **kw):
        fake = fake_nuke(nodes, **kw)
        patcher = mock.patch.dict(sys.modules, {"nuke": fake})
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

    def test_submit_many_keeps_going_after_a_bad_input(self):
        res = self.m.submit_many("wf", {"seed": "3"}, [("R1", "/a.%04d.png 1-2"), ("R2", "/bad.png"), ("R3", "/c.png")])
        self.assertEqual([(n, bool(j), e) for n, j, e in res], [("R1", True, None), ("R2", False, "No frames"),
                                                               ("R3", True, None)])
        self.assertEqual([s[1]["input"] for s in self.client.submitted], ["/a.%04d.png 1-2", "/bad.png", "/c.png"])
        self.assertEqual(self.m.sources, {"job1": "R1", "job3": "R3"})

    def test_rows(self):
        now = 10_000.0
        q = self.panel.PanelModel.row({"id": "abcdef1234", "status": "queued", "user": "jane", "name": "abc_0010",
                                       "queue_position": 2, "eta_seconds": 125, "created_at": now - 30,
                                       "info": {"version": 3}}, now)
        self.assertEqual(q, ["abcdef12", "abc_0010  v003", "jane", "queued", "", "#2 · ~2m 05s", "just now", ""])
        f = self.panel.PanelModel.row({"id": "x" * 10, "short_id": "xxxxxxxx", "status": "failed", "user": "j",
                                       "error": "CUDA out of memory", "created_at": now - 7200,
                                       "started_at": now - 100, "finished_at": now - 40}, now)
        self.assertEqual((f[4], f[6], f[7]), ("CUDA out of memory", "2 h ago", "1m 00s"))

    def test_destination_text(self):
        html = self.panel.PanelModel.destination_html({
            "frames": 12, "output_dir_posix": "/rdo/shows/x/out/<shot>", "first_file": "a.1001.png",
            "last_file": "a.1012.png", "version": 2, "notes": ["No shot & no seq"], "picked": True,
            "convert": {"kind": "movie", "codec": "prores", "width": 1920, "height": 1080, "fps": 23.976,
                        "timecode": "00:00:41:16", "start": 1001, "end": 1012, "slate_frame": 1000}})
        for part in ("12 frames", "/rdo/shows/x/out/&lt;shot&gt;/", "a.1001.png</code> … <code>a.1012.png",
                     "so this run is v002", "No shot &amp; no seq", "23.976 fps", "first frame (1000) is a slate"):
            self.assertIn(part, html)


if __name__ == "__main__":
    unittest.main()
