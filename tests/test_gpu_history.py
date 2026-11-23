"""GPU history (decision 0049): each machine's GPUs sampled from what its agent last reported,
averaged into steps when asked for many hours, and pruned after gpu_history.days. The
endpoint is in test_e2e.

    python -m unittest tests.test_gpu_history -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.definitions import DefinitionRegistry
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.desk import AgentState, Desk
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.protocol import now

GIB = 2**30


class History(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = self.tmp.replace("\\", "/")
        self.cfg = Config.model_validate({"database": f"{root}/farm.db", "workers": [],
                                          "gpu_history": {"sample_seconds": 60, "days": 7}})
        self.store = JobStore(Path(f"{root}/farm.db"))
        self.desk = Desk(Farm(self.cfg, self.store, DefinitionRegistry(Path(root) / "none"), EventBus()))
        self.t = now()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def machines(self):
        probed = AgentState(name="wrk1125l-3090", last_seen=now(), gpus=[
            {"index": 0, "name": "NVIDIA GeForce RTX 3090", "utilization": 90, "vram_used": 20 * GIB,
             "vram_total": 24 * GIB},
            {"index": 1, "name": "NVIDIA GeForce RTX 3090", "utilization": 0, "vram_used": 1 * GIB,
             "vram_total": 24 * GIB}])
        comfy = AgentState(name="tor-flame02-a6000", last_seen=now(), stats={"devices": [
            {"index": 0, "name": "cuda:0 NVIDIA RTX A6000", "vram_total": 48 * GIB, "vram_free": 40 * GIB}]})
        silent = AgentState(name="wrk59467w-5090", last_seen=now() - 3600, gpus=[{"index": 0, "utilization": 50}])
        for a in (probed, comfy, silent):
            self.desk.agents[a.name] = a
        self.store.insert({"id": "j1", "workflow": "x", "user": "ann", "status": "running", "worker": probed.name,
                           "created_at": self.t})

    def test_each_gpu_of_each_machine_checking_in(self):
        self.machines()
        self.assertEqual(self.desk.sample_gpus(self.t), 3)                   # not the silent one
        h = self.desk.gpu_history("wrk1125l-3090", 1, self.t + 1)
        self.assertEqual([(s["gpu"], s["util"], s["used"], s["total"], s["jobs"]) for s in h["samples"]],
                         [(0, 90, 20 * GIB, 24 * GIB, 1), (1, 0, 1 * GIB, 24 * GIB, 1)])
        [a6000] = self.desk.gpu_history("tor-flame02-a6000", 1, self.t + 1)["samples"]
        self.assertEqual((a6000["util"], a6000["used"], a6000["total"], a6000["jobs"]), (None, 8 * GIB, 48 * GIB, 0))
        self.assertEqual(self.desk.gpu_history("wrk59467w-5090", 1, self.t + 1)["samples"], [])

    def test_many_hours_are_averaged_into_steps(self):
        self.machines()
        a = self.desk.agents["wrk1125l-3090"]
        for i in range(120):                                                  # two hours, a sample a minute
            a.gpus[0]["utilization"] = 100 if i % 2 else 0
            a.gpus[0]["vram_used"] = (10 + i % 3) * GIB
            self.desk.sample_gpus(self.t - 7200 + i * 60)
        h = self.desk.gpu_history("wrk1125l-3090", 2, self.t)
        self.assertEqual((h["step_seconds"], h["sample_seconds"], h["days"]), (60, 60, 7))
        self.assertEqual(len([s for s in h["samples"] if s["gpu"] == 0]), 120)    # under 500: every sample
        h = self.desk.gpu_history("wrk1125l-3090", 24 * 7, self.t)
        self.assertEqual(h["step_seconds"], round(24 * 7 * 3600 / 500))           # about 500 a GPU at most
        gpu0 = [s for s in h["samples"] if s["gpu"] == 0]
        self.assertLess(len(gpu0), 120)
        self.assertTrue(all(0 <= s["util"] <= 100 for s in gpu0))
        self.assertTrue(any(0 < s["util"] < 100 for s in gpu0))                  # averaged
        self.assertEqual(max(s["used"] for s in gpu0), 12 * GIB)                  # memory at its highest
        self.assertEqual(self.desk.gpu_history("wrk1125l-3090", 0.5, self.t)["samples"][0]["at"],
                         self.t - 7200 + 90 * 60)                                  # the last half hour only

    def test_old_samples_go(self):
        self.machines()
        self.desk.sample_gpus(self.t - 8 * 86400)
        self.desk.sample_gpus(self.t)
        self.assertEqual(self.store.prune_gpu_samples(self.t - 7 * 86400), 3)
        self.assertEqual(len(self.desk.gpu_history("wrk1125l-3090", 24 * 31, self.t + 1)["samples"]), 2)

    def test_the_settings(self):
        self.assertEqual((Config().gpu_history.sample_seconds, Config().gpu_history.days), (60, 7))
        for bad in ({"sample_seconds": 5}, {"days": -1}):
            with self.assertRaises(ValueError):
                Config.model_validate({"workers": [], "gpu_history": bad})


if __name__ == "__main__":
    unittest.main()
