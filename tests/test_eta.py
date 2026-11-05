"""Run-time estimates: the fit per workflow and GPU type (manager.eta) and its storage."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from comfyfarm.manager import eta as E
from comfyfarm.manager.db import JobStore


def runs(*pairs):
    row = None
    for size, seconds in pairs:
        row = E.fold(row, size, seconds)
    return row


class EtaTests(unittest.TestCase):
    def test_gpu_type(self):
        self.assertEqual(E.gpu_type("cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync"), "NVIDIA GeForce RTX 5090")
        self.assertEqual(E.gpu_type("cuda:1 NVIDIA GeForce RTX 3090"), "NVIDIA GeForce RTX 3090")
        self.assertEqual((E.gpu_type(None), E.gpu_type("")), ("unknown GPU", "unknown GPU"))

    def test_no_runs(self):
        self.assertIsNone(E.estimate(None, 10))
        self.assertIsNone(E.fit(runs()))

    def test_one_size_so_far_is_proportional(self):
        row = runs((100, 50), (100, 50), (100, 50))
        self.assertEqual(E.fit(row), (0.0, 0.5))
        self.assertAlmostEqual(E.estimate(row, 200), 100)
        self.assertAlmostEqual(E.estimate(runs((3, 30)), 6), 60)       # one run is enough

    def test_sizes_that_vary_give_overhead_plus_time_per_frame(self):
        row = runs((50, 35), (100, 60), (200, 110), (150, 85))         # 10 s + 0.5 s a frame
        a, b = E.fit(row)
        self.assertAlmostEqual(a, 10, places=6)
        self.assertAlmostEqual(b, 0.5, places=6)
        self.assertAlmostEqual(E.estimate(row, 300), 160, places=5)
        self.assertEqual(E.describe(row)["runs"], 4)
        self.assertAlmostEqual(E.describe(row)["overhead"], 10)
        self.assertAlmostEqual(E.describe(row)["per_unit"], 0.5)

    def test_two_runs_of_different_sizes_arent_a_line_yet(self):
        row = runs((50, 35), (100, 60))
        self.assertEqual(E.fit(row)[0], 0.0)

    def test_a_line_that_makes_no_sense_falls_back_to_proportional(self):
        row = runs((50, 100), (100, 60), (200, 40))                    # bigger runs faster: noise
        a, b = E.fit(row)
        self.assertEqual(a, 0.0)
        self.assertGreater(b, 0)

    def test_recent_runs_count_most(self):
        row = runs((10, 100), *[(10, 10)] * 10)                        # slow once, long ago (models loading)
        self.assertLess(E.estimate(row, 10), 13)
        self.assertEqual(row["runs"], 11)

    def test_combine(self):
        a, b = runs((3, 30)), runs((3, 60))
        self.assertAlmostEqual(E.estimate(E.combine([a, b]), 3), 45)
        self.assertIsNone(E.combine([None, {}]))

    def test_units(self):
        self.assertEqual(E.units({"info": {"batch": {"tiles": 4}, "textures": {"items": [1] * 10}}}), 4)
        self.assertEqual(E.units({"info": {"textures": {"items": [1] * 10}}}), 10)
        self.assertEqual(E.units({"info": {"expected_frames": 158, "input": {"frame_count": 3}}}), 158)
        self.assertEqual(E.units({"info": {"input": {"frame_count": 3}}}), 3)
        self.assertEqual((E.units({"info": None}), E.units({})), (1, 1))     # a still, or no input

    def test_stored_per_workflow_and_gpu(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        store = JobStore(Path(tmp, "farm.db"))
        self.addCleanup(store.close)
        for seconds in (30, 30):
            store.record_gpu_run("ltx", "NVIDIA GeForce RTX 5090", 3, seconds)
        store.record_gpu_run("ltx", "NVIDIA GeForce RTX 3090", 3, 60)
        stats = store.run_stats()
        self.assertEqual(sorted(stats), [("ltx", "NVIDIA GeForce RTX 3090"), ("ltx", "NVIDIA GeForce RTX 5090")])
        self.assertEqual(stats[("ltx", "NVIDIA GeForce RTX 5090")]["runs"], 2)
        self.assertAlmostEqual(E.estimate(stats[("ltx", "NVIDIA GeForce RTX 5090")], 6), 60)


if __name__ == "__main__":
    unittest.main()
