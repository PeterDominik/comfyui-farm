"""Texture sets: a texture with more tiles than the manifest's textures.batch (or with
tiles of different sizes) is split into batches that each run through the workflow on
their own and fill one output folder.

    python -m unittest tests.test_sets -v
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.fileio import convert as C
from comfyfarm.fileio.paths import frames_text
from comfyfarm.fileio import media as M
from comfyfarm.fileio import textures as T
from comfyfarm.manager.db import JobStore, now

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_textures import FAKE_OIIOTOOL, OCIO_CONFIG, exr_header, exr_sized, png_bytes

REPO = Path(__file__).resolve().parent.parent
try:   # the manager's farm needs pydantic and yaml (not the web stack)
    from comfyfarm.manager.events import EventBus
    from comfyfarm.manager.farm import Farm, FarmError
    from comfyfarm.workflows import WorkflowRegistry
except ImportError:   # pragma: no cover
    Farm = None


class SplitTests(unittest.TestCase):
    def test_runs_of_one_size_cut_to_the_batch(self):
        a, b = (1024, 1024), (1024, 512)
        self.assertEqual(T.split_batches([a] * 10, 4), [(0, 4), (4, 8), (8, 10)])
        self.assertEqual(T.split_batches([a] * 4, 4), [(0, 4)])
        # The loader would crop a 1024×512 tile to 1024² in a batch of 1024² tiles.
        self.assertEqual(T.split_batches([a, a, b, b, b, a], 4), [(0, 2), (2, 5), (5, 6)])
        self.assertEqual(T.split_batches([a, a, b], None), [(0, 2), (2, 3)])
        self.assertEqual(T.split_batches([None] * 5, 2), [(0, 2), (2, 4), (4, 5)])   # sizes unknown
        self.assertEqual(T.split_batches([a] * 7, None), [(0, 7)])
        self.assertEqual(T.split_batches([], 4), [])

    def test_labels(self):
        udims = [(1001, 0), (1002, 0), (1003, 0), (1004, 0)]
        self.assertEqual(T.batch_label(udims, True), "UDIMs 1001–1004")
        self.assertEqual(T.batch_label([(1001, 0), (1011, 0)], True), "UDIMs 1001–1011 (2)")
        self.assertEqual(T.batch_label([(1011, 0)], True), "UDIM 1011")
        self.assertEqual(T.batch_label([(1001, 1), (1001, 2)], False), "frames 1–2")

    def test_outputs_of_the_finished_tiles(self):
        t = {"name": T.DEFAULT_NAME, "tokens": {"uvset": "map1", "tile": "noTile", "state": "defState",
                                                "description": ""},
             "maps": [{"name": "basecolor", "channel": "color01", "colorspace": "acescg", "color": True},
                      {"name": "height", "channel": "bmp02", "colorspace": "acescg", "color": False}]}
        o = T.map_outputs(t, "/out", [], [[1005, 0], [1001, 0], [1002, 0]])
        self.assertEqual((o["files"], o["frames"], o["first"], o["last"]), (6, 3, 1001, 1005))
        self.assertEqual(o["frame_pattern"], "/out/acescg_map1_noTile_defState_color01.%04d.0000.exr")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = JobStore(Path(self.tmp) / "farm.db")
        self.t = now()

    def tearDown(self):
        self.store.close()   # Windows can't delete an open database
        shutil.rmtree(self.tmp)

    def add(self, jid, user, status="queued", kind="job", parent=None, dt=0.0, info=None):
        self.store.insert({"id": jid, "workflow": "chord_pbr", "user": user, "status": status, "kind": kind,
                           "parent_id": parent, "created_at": self.t + dt, "info": info or {}})

    def test_sets_let_other_jobs_in_between_batches(self):
        self.add("set1", "ann", status="running", kind="set")
        self.add("b0", "ann", status="running", kind="batch", parent="set1")
        for i in (1, 2, 3):   # same created_at: the order they were added in
            self.add(f"b{i}", "ann", kind="batch", parent="set1")
        self.add("ann-single", "ann", dt=1)
        self.add("bob-job", "bob", dt=2)
        order = [j["id"] for j in self.store.queued()]
        # bob has nothing on the GPU; ann's own small job beats her set's next batch.
        self.assertEqual(order, ["bob-job", "ann-single", "b1", "b2", "b3"])
        self.assertNotIn("prompt", self.store.queued()[0])   # ordering doesn't load prompts

    def test_listing(self):
        self.add("set1", "ann", kind="set", status="running")
        for i in range(3):
            self.add(f"b{i}", "ann", kind="batch", parent="set1",
                     info={"batch": {"tiles": 4, "label": f"UDIMs {i}"}, "textures": {"items": [[1001 + i, 0]]}})
        self.add("job1", "bob", dt=1)
        self.assertEqual([j["id"] for j in self.store.list()], ["job1", "set1"])          # what artists submitted
        self.assertEqual([j["id"] for j in self.store.list(parent="set1")], ["b0", "b1", "b2"])
        self.assertEqual(sorted(j["id"] for j in self.store.list(top_level=False, work_only=True)),
                         ["b0", "b1", "b2", "job1"])                                          # what the farm runs
        states = self.store.batch_states("set1")
        self.assertEqual([(r["tiles"], r["label"], json.loads(r["items"])) for r in states][1],
                         (4, "UDIMs 1", [[1002, 0]]))
        self.assertEqual(self.store.queued_batches(), {"set1": 3})

    def test_old_database_gets_the_new_columns(self):
        import sqlite3
        path = Path(self.tmp) / "old.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, workflow TEXT NOT NULL, user TEXT NOT NULL, name TEXT, "
                   "status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, params TEXT, info TEXT, prompt TEXT, "
                   "worker TEXT, prompt_id TEXT, progress REAL NOT NULL DEFAULT 0, stage TEXT, error TEXT, "
                   "warnings TEXT, outputs TEXT, output_dir TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
                   "cancel_requested INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, dispatched_at REAL, "
                   "started_at REAL, finished_at REAL)")
        db.execute("INSERT INTO jobs (id, workflow, user, status, created_at) VALUES ('old', 'x', 'ann', 'queued', 1)")
        db.commit()
        db.close()
        store = JobStore(path)
        job = store.get("old")
        self.assertEqual((job["kind"], job["parent_id"]), ("job", None))
        self.assertEqual([j["id"] for j in store.queued()], ["old"])
        store.close()


@unittest.skipIf(Farm is None, "needs the manager's dependencies (pydantic, pyyaml)")
class FarmSetTests(unittest.TestCase):
    """Planning, submitting and finishing a set, without ComfyUI: batches are moved through
    their states by hand, as the farm's loops would."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = self.tmp.replace("\\", "/")
        os.makedirs(f"{self.root}/ocio")
        Path(f"{self.root}/ocio/config.ocio").write_text(OCIO_CONFIG)
        cfg = Config.model_validate({
            "database": f"{self.root}/farm.db", "workflows_dir": str(REPO / "workflows"), "workers": [],
            "paths": {"mappings": [{"posix": "/rdo/shows", "windows": f"{self.root}/shows"}],
                      "output_root": f"{self.root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"},
            "ocio": {"config": f"{self.root}/ocio/config.ocio"},
            "convert": {"scratch_dir": f"{self.root}/scratch", "tools": {"oiiotool": FAKE_OIIOTOOL}},
        })
        self.store = JobStore(Path(f"{self.root}/farm.db"))
        self.farm = Farm(cfg, self.store, WorkflowRegistry(REPO / "workflows"), EventBus())
        self.asset = f"{self.root}/shows/xyz/_asset/rockA/cg/textures"

    def tearDown(self):
        self.store.close()   # Windows can't delete an open database
        shutil.rmtree(self.tmp)

    def tiles(self, folder, name, udims, data, ext="png"):
        """Tile files (only their headers are read); `data` per UDIM if it's a function."""
        os.makedirs(f"{self.asset}/{folder}", exist_ok=True)
        for u in udims:
            Path(f"{self.asset}/{folder}/{name}.{u}.{ext}").write_bytes(data(u) if callable(data) else data)
        return f"/rdo/shows/xyz/_asset/rockA/cg/textures/{folder}"

    def finish(self, batch, status="done", error=None):
        """A batch's run ends, as _finalize or _fail would leave it."""
        b = self.store.get(batch["id"])
        fields = {"status": status, "finished_at": now(), "started_at": now() - 10, "progress": 1.0, "error": error}
        if status == "done":
            fields["outputs"] = {"files": 5 * b["info"]["batch"]["tiles"]}
        self.farm._emit(self.store.update(b["id"], **fields))

    def test_nothing_stored_for_a_set_names_a_node(self):
        """Maps are planned by channel; which node saves each is the agent's copy's to say."""
        def keys(x):
            if isinstance(x, dict):
                return set(x) | {k for v in x.values() for k in keys(v)}
            return {k for v in x for k in keys(v)} if isinstance(x, list) else set()
        src = self.tiles("src", "rockA_diffuse", range(1001, 1011), png_bytes(2, (2048, 2048)))
        s = self.farm.submit("chord_pbr", {"input": src}, "ann")
        wf = self.farm.registry.workflows["chord_pbr"]
        for job in [s] + self.store.list(parent=s["id"]):
            full = self.store.get(job["id"], with_prompt=True)
            self.assertNotIn("node", keys(full["info"]) | keys(full["request"]))
            if full["request"]:
                graph = wf.build_from(full["request"])
                for m in wf.textures.maps:
                    self.assertTrue(graph[m.node]["inputs"]["filename_prefix"].endswith(m.channel))

    def test_timeline_of_a_set_and_its_batches(self):
        src = self.tiles("src", "rockA_diffuse", range(1001, 1011), png_bytes(2, (2048, 2048)))
        s = self.farm.submit("chord_pbr", {"input": src}, "ann")
        batches = self.store.list(parent=s["id"])
        waiting = self.farm.timeline(s["id"])
        self.assertEqual([e["event"] for e in waiting["events"]], ["submitted"])
        self.assertIsNotNone(waiting["events"][-1]["seconds"])           # still queued: counted up to now
        self.finish(batches[0])
        self.finish(batches[1], "failed", "CUDA out of memory.")
        self.finish(batches[2])
        t = self.farm.timeline(s["id"])
        self.assertEqual([e["event"] for e in t["events"]], ["submitted", "running", "failed"])
        self.assertIn("1 of 3 batches didn't finish", t["events"][-1]["detail"]["error"])
        self.assertIsNone(t["events"][-1]["seconds"])                    # over: the last line has no length
        self.assertTrue(all(e["seconds"] >= 0 for e in t["events"][:-1]))
        one = self.farm.timeline(batches[1]["id"])
        self.assertEqual([(e["event"], e["detail"]) for e in one["events"]],
                         [("submitted", {"kind": "batch"}), ("failed", {"error": "CUDA out of memory."})])
        with self.assertRaisesRegex(FarmError, "Job not found"):
            self.farm.timeline("nope-nope")

    def test_large_texture_becomes_a_set(self):
        src = self.tiles("src", "rockA_diffuse", range(1001, 1011), png_bytes(2, (2048, 2048)))
        preview = self.farm.plan_summary(self.farm.plan("chord_pbr", {"input": src}))
        self.assertEqual((preview["batches"], preview["what"]),
                         (3, "50 EXRs (5 maps × 10 UDIM tiles) in 3 batches of up to 4"))

        s = self.farm.submit("chord_pbr", {"input": src}, "ann")
        self.assertEqual((s["kind"], s["status"], s["stage"], s["info"]["set"]),
                         ("set", "queued", "0/10 UDIMs", {"batches": 3, "tiles": 10, "batch": 4}))
        out = s["output_dir"]
        self.assertTrue(out.endswith("/rockA/rockA_diffuse.chord_v001"), out)
        self.assertTrue(self.store.output_reserved(out + "/"))          # one folder, reserved by the set
        self.assertEqual(self.store.list(), [self.store.get(s["id"])])  # artists see the set only
        batches = self.store.list(parent=s["id"])
        self.assertEqual([b["info"]["batch"]["label"] for b in batches],
                         ["UDIMs 1001–1004", "UDIMs 1005–1008", "UDIMs 1009–1010"])
        for b in batches:
            full = self.store.get(b["id"], with_prompt=True)
            self.assertIsNone(full["prompt"])                                   # built by the machine that runs it
            graph = self.farm.registry.workflows["chord_pbr"].build_from(full["request"])
            c = b["info"]["input"]["convert"]
            self.assertEqual((b["kind"], b["status"], b["output_dir"], b["output_key"]),
                             ("batch", "queued", out, None))
            self.assertEqual(c["dst_dir"], f"{self.root}/scratch/{b['id']}/")   # its own scratch folder
            self.assertEqual(graph["17"]["inputs"]["path"], c["dst_dir"] + c["dst_pattern"])
            self.assertEqual(graph["17"]["inputs"]["frames"], frames_text(c["frames"]))
            self.assertEqual(graph["28"]["inputs"]["output_path"], f"{self.root}/scratch/{b['id']}/maps")
            self.assertEqual(len(b["info"]["textures"]["items"]), len(c["names"]))
        self.assertEqual(batches[2]["info"]["input"]["convert"]["names"],
                         ["rockA_diffuse.1009.png", "rockA_diffuse.1010.png"])
        self.assertEqual(batches[2]["info"]["input"]["convert"]["dst_pattern"], "rockA_diffuse.%04d.png")
        # The next run of the same texture gets the next folder.
        self.assertTrue(self.farm.plan("chord_pbr", {"input": src})["out_dir"].endswith("chord_v002"))
        with self.assertRaises(FarmError):                               # a batch alone isn't rerun
            self.farm.retry(batches[0]["id"], "ann")

        # The first batch's conversion makes its four tiles.
        asyncio.run(C.convert(batches[0]["info"]["input"], self.farm.cfg.convert, lambda d, n: None))
        self.assertEqual(sorted(os.listdir(f"{self.root}/scratch/{batches[0]['id']}")),
                         [f"rockA_diffuse.{u}.png" for u in range(1001, 1005)])

        self.finish(batches[0])
        self.assertEqual((self.store.get(s["id"])["status"], self.store.get(s["id"])["stage"]),
                         ("running", "4/10 UDIMs"))
        self.finish(batches[1], "failed", "ChordMaterialEstimation (node 3): CUDA out of memory")
        self.finish(batches[2])
        s = self.store.get(s["id"])
        self.assertEqual((s["status"], s["stage"]), ("failed", "6/10 UDIMs, 1 batch failed"))
        self.assertIn("1 of 3 batches didn't finish (UDIMs 1005–1008): ChordMaterialEstimation", s["error"])
        o = s["outputs"]
        self.assertEqual((o["frames"], o["first"], o["last"], o["files"]), (6, 1001, 1010, 30))
        self.assertTrue(o["maps"]["bmp02"]["pattern"].endswith("acescg_map1_noTile_defState_bmp02.%04d.0000.exr"))
        self.assertFalse(self.store.output_reserved(out + "/"))

        # Retry failed: only that batch, back to converting; the set is running again.
        s = self.farm.retry_failed(s["id"])
        self.assertEqual((s["status"], s["error"]), ("running", None))
        self.assertEqual([b["status"] for b in self.store.list(parent=s["id"])], ["done", "queued", "done"])
        self.assertTrue(self.store.output_reserved(out + "/"))
        self.finish(self.store.get(batches[1]["id"]))
        s = self.store.get(s["id"])
        self.assertEqual((s["status"], s["stage"], s["progress"]), ("done", "10/10 UDIMs", 1.0))
        self.assertEqual((s["outputs"]["frames"], s["outputs"]["files"]), (10, 50))
        with self.assertRaisesRegex(FarmError, "No failed"):
            self.farm.retry_failed(s["id"])
        self.assertEqual(self.farm.annotate([s])[0]["eta_seconds"], None)

    def test_cancel_a_set(self):
        src = self.tiles("src", "rockA_diffuse", range(1001, 1011), png_bytes(2, (2048, 2048)))
        s = self.farm.submit("chord_pbr", {"input": src}, "ann")
        s = asyncio.run(self.farm.cancel(s["id"]))
        self.assertEqual((s["status"], s["stage"]), ("cancelled", "cancelled: 0/10 UDIMs written"))
        self.assertEqual({b["status"] for b in self.store.list(parent=s["id"])}, {"cancelled"})
        with self.assertRaisesRegex(FarmError, "already cancelled"):
            asyncio.run(self.farm.cancel(s["id"]))
        s = self.farm.retry_failed(s["id"])     # cancelled batches can be finished later too
        self.assertEqual((s["status"], len(self.store.list(parent=s["id"], statuses=("queued",)))), ("queued", 3))

    def test_tiles_of_another_size_get_their_own_batch(self):
        """Even under the batch size: the loader would crop them to the others' size."""
        sizes = {1001: (2048, 2048), 1002: (2048, 2048), 1003: (2048, 1024)}
        src = self.tiles("mixed", "rockA_diffuse", sizes, lambda u: png_bytes(2, sizes[u]))
        s = self.farm.submit("chord_pbr", {"input": src}, "ann")
        self.assertEqual([b["info"]["batch"]["label"] for b in self.store.list(parent=s["id"])],
                         ["UDIMs 1001–1002", "UDIM 1003"])
        self.assertEqual(s["info"]["what"], "15 EXRs (5 maps × 3 UDIM tiles) in 2 batches of up to 2")

    def test_small_tiles_read_directly_in_batches(self):
        src = self.tiles("small", "rockA_diffuse", range(1001, 1007), png_bytes(2, (800, 800)))
        s = self.farm.submit("chord_pbr", {"input": src}, "ann")
        batches = [self.store.get(b["id"], with_prompt=True) for b in self.store.list(parent=s["id"])]
        wf = self.farm.registry.workflows["chord_pbr"]
        loaders = [wf.build_from(b["request"])["17"]["inputs"] for b in batches]
        self.assertEqual([(x["path"], x["frames"]) for x in loaders],
                         [(f"{self.asset}/small/rockA_diffuse.%04d.png", "1001-1004"),
                          (f"{self.asset}/small/rockA_diffuse.%04d.png", "1005-1006")])
        self.assertEqual([b["status"] for b in batches], ["queued", "queued"])   # nothing to convert

    def test_exr_set(self):
        good = exr_sized(4096, 4096)
        layered = exr_header(["diffuse.B", "diffuse.G", "diffuse.R"], (0, 0, 4095, 4095))
        src = self.tiles("exr", "rockA_color", range(1001, 1007), lambda u: layered if u == 1005 else good, "exr")
        s = self.farm.submit("chord_pbr", {"input": src}, "ann")
        b1, b2 = self.store.list(parent=s["id"])
        self.assertEqual([b1["info"]["input"]["convert"]["frames"], b2["info"]["input"]["convert"]["frames"]],
                         [[1001, 1002, 1003, 1004], [1005, 1006]])
        self.assertEqual(b2["info"]["convert_summary"]["scaled"], "4096×4096 → 1024×1024")
        asyncio.run(C.convert(b1["info"]["input"], self.farm.cfg.convert, lambda d, n: None))
        self.assertEqual(M.image_size(f"{self.root}/scratch/{b1['id']}/rockA_color.1004.png"), (1024, 1024))
        with self.assertRaisesRegex(C.ConvertError, "rockA_color.1005.exr has no R, G, B"):
            asyncio.run(C.convert(b2["info"]["input"], self.farm.cfg.convert, lambda d, n: None))


if __name__ == "__main__":
    unittest.main()
