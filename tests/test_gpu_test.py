"""The farm's test process, workflows/gpu_test (decision 0027): its definition as the repo
has it, and its script run as the process runner runs it, with a stand-in torch backed by
numpy (tests/fake_torch), so blur, invert, grade and holding the card are checked without
a GPU. And what job.json gives a command of its inputs (runners/process.with_files).

    python -m unittest tests.test_gpu_test -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from comfyfarm.definitions import DefinitionRegistry
from comfyfarm.envs import spec as ES
from comfyfarm.runners.process import ProcessRegistry, with_files

try:
    import numpy as np
    from PIL import Image
except ImportError:   # pragma: no cover
    np = None

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / "workflows" / "gpu_test" / "run.py"
FAKE_TORCH = Path(__file__).resolve().parent / "fake_torch"


class Definition(unittest.TestCase):
    def test_it_is_in_the_repo_and_has_no_problems(self):
        wf = ProcessRegistry(REPO / "workflows").workflows["gpu_test"]
        self.assertEqual((wf.problems, wf.wiring_problems), ([], []))
        self.assertEqual((wf.runner_type, wf.environment, wf.frames_ext), ("process", "gpu-test", "png"))
        self.assertEqual(wf.seconds(60), 600)                       # limits: {max_minutes: 10}
        self.assertEqual(wf.port.takes, "images")
        self.assertEqual(sorted(wf.params), ["hold_gb", "input", "operation", "output_path", "pause", "strength"])
        self.assertEqual((wf.publish, wf.shotgrid, wf.makes_preview), ({"off": True}, False, True))
        self.assertTrue(ES.load(wf.environment).locked)

    def test_the_manager_reads_it_and_comfyui_does_not(self):
        heads = DefinitionRegistry(REPO / "workflows")
        self.assertIn("gpu_test", heads.workflows)
        self.assertEqual(heads.workflows["gpu_test"].digest,
                         ProcessRegistry(REPO / "workflows").workflows["gpu_test"].digest)
        self.assertEqual(heads.workflows["gpu_test"].schema()["enabled"], True)


class InputFiles(unittest.TestCase):
    """What job.json gives a command for an input: its files in order, as the loaders read them."""

    def seq(self, **kw):
        d = {"raw": "", "farm_path": "/s/plate.%04d.png", "directory": "/s/", "filename": "plate.%04d.png",
             "first": 1001, "last": 1003, "frame_count": 3, "skip": 0, "cap": 0, "show": None, "shot": None}
        d.update(kw)
        return d

    def test_a_sequence(self):
        got = with_files(self.seq())
        self.assertEqual(got["files"], ["/s/plate.1001.png", "/s/plate.1002.png", "/s/plate.1003.png"])
        self.assertEqual(got["numbers"], [1001, 1002, 1003])
        self.assertEqual(got["directory"], "/s/")                   # as the farm resolved it, too

    def test_a_converted_input_is_read_from_its_pngs(self):
        got = with_files(self.seq(filename="plate.%04d.exr", kind="exr",
                                  convert={"dst_dir": "/scratch/j1/", "dst_pattern": "plate.%04d.png"}))
        self.assertEqual(got["files"], ["/scratch/j1/plate.1001.png", "/scratch/j1/plate.1002.png",
                                        "/scratch/j1/plate.1003.png"])

    def test_one_image_and_nothing(self):
        got = with_files(self.seq(filename="still.png", first=None, last=None, frame_count=1))
        self.assertEqual((got["files"], got["numbers"]), (["/s/still.png"], None))
        self.assertIsNone(with_files(None))
        self.assertEqual(with_files({"odd": 1}), {"odd": 1})       # a shape this farm doesn't know


@unittest.skipIf(np is None, "needs numpy and Pillow (requirements-dev.txt)")
class Script(unittest.TestCase):
    """run.py as the process runner runs it: in its run folder, with $COMFYFARM_JOB."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"
        self.out.mkdir()
        rng = np.random.default_rng(7)
        self.frames = []
        for n in (1001, 1002):
            a = rng.integers(0, 256, size=(12, 16, 3), dtype=np.uint8)
            Image.fromarray(a).save(self.tmp / f"plate.{n}.png")
            self.frames.append(a)

    def run_it(self, env=None, **params):
        job = {"contract": 1, "job": "0123456789ab", "params": {"operation": "blur", "strength": 4, "hold_gb": 0,
                                                                "pause": 0, **params},
               "input": with_files({"raw": "", "farm_path": "", "directory": f"{self.tmp}/",
                                    "filename": "plate.%04d.png", "first": 1001, "last": 1002, "frame_count": 2,
                                    "skip": 0, "cap": 0, "show": None, "shot": None}),
               "output_dir": str(self.out), "frames_prefix": "_raw", "events": str(self.tmp / "events.jsonl"),
               "result": str(self.tmp / "result.json"), "cancel": str(self.tmp / "cancel")}
        (self.tmp / "job.json").write_text(json.dumps(job), encoding="utf-8")
        path = os.pathsep.join([str(FAKE_TORCH), *filter(None, [os.environ.get("PYTHONPATH")])])
        r = subprocess.run([sys.executable, str(RUN)], cwd=self.tmp, capture_output=True, text=True, timeout=120,
                           env={**os.environ, "PYTHONPATH": path, "COMFYFARM_JOB": str(self.tmp / "job.json"),
                                **(env or {})})
        result = json.loads((self.tmp / "result.json").read_text()) if (self.tmp / "result.json").exists() else None
        return r, result

    def written(self, i):
        return np.asarray(Image.open(self.out / f"_raw_{i:05d}.png"))

    def test_invert_and_grade(self):
        r, result = self.run_it(operation="invert")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(result, {"warnings": []})
        self.assertTrue(np.array_equal(self.written(1), 255 - self.frames[0]))
        self.assertTrue(np.array_equal(self.written(2), 255 - self.frames[1]))
        self.assertEqual(sorted(os.listdir(self.out)), ["_raw_00001.png", "_raw_00002.png",
                                                        "gpu_test.01234567.json"])   # no half-written frame
        r, _ = self.run_it(operation="grade", strength=2)
        expected = np.clip(np.round(self.frames[0].astype(np.float32) / 255 * 2 * 255), 0, 255)
        self.assertLessEqual(np.abs(self.written(1).astype(int) - expected.astype(int)).max(), 1)

    def test_blur_is_a_gaussian_that_keeps_flat_areas(self):
        flat = np.full((10, 10, 3), 128, dtype=np.uint8)
        Image.fromarray(flat).save(self.tmp / "plate.1001.png")
        r, _ = self.run_it(operation="blur", strength=2)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(np.array_equal(self.written(1), flat))     # weights add to 1: flat stays flat
        out = self.written(2).astype(float)
        self.assertLess(out.std(), self.frames[1].astype(float).std())   # noise is smoothed
        r, _ = self.run_it(operation="blur", strength=0)
        self.assertTrue(np.array_equal(self.written(2), self.frames[1]))

    def test_it_reports_progress_and_the_card(self):
        r, _ = self.run_it(hold_gb=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        events = [json.loads(line) for line in (self.tmp / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events[0]["stage"], "holding 10 GB of the GPU (23.0 GB free)")
        self.assertEqual(events[-1], {"progress": 1.0, "stage": "frame 2/2"})
        report = json.loads((self.out / "gpu_test.01234567.json").read_text())
        self.assertEqual((report["gpu"], report["total_gb"], report["free_gb_at_start"], report["held_gb"],
                          report["frames"]), ("Fake GPU", 24.0, 23.0, 10.0, 2))
        self.assertGreaterEqual(report["peak_gb"], 10.0)

    def test_a_hold_that_does_not_fit_says_what_was_free(self):
        r, result = self.run_it(hold_gb=20, env={"FAKE_TORCH_FREE_GB": "9.5"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(result["error"], "couldn't hold 20 GB of the GPU: 9.5 of 24.0 GB were free when it started. "
                                          "Something else holds the card: see the job's warnings and the Machines tab")
        self.assertFalse((self.out / "_raw_00001.png").exists())

    def test_a_card_that_was_not_free_is_a_warning(self):
        r, result = self.run_it(env={"FAKE_TORCH_FREE_GB": "10"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(result["warnings"], ["Only 10.0 of 24.0 GB of the GPU were free when the test started: "
                                              "something else held the card"])

    def test_no_gpu(self):
        r, result = self.run_it(env={"FAKE_TORCH_GPU": "0"})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(result["error"], "torch sees no GPU on this machine: check its NVIDIA driver (nvidia-smi)")

    def test_it_stops_when_asked(self):
        (self.tmp / "cancel").touch()
        r, _ = self.run_it()
        self.assertEqual(r.returncode, 0)
        self.assertFalse((self.out / "_raw_00001.png").exists())


if __name__ == "__main__":
    unittest.main()
