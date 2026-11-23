"""The Machines tab's GPU history graphs (static/gpu_history.js, decision 0050): each GPU's
samples, its lines broken where the machine wasn't checking in, the times it held farm jobs,
the times along the bottom, and what pointing at a time says, from data shaped as
GET /api/v1/workers/{name}/gpu-history gives it.

The functions that decide what to draw touch no page, so node runs them alone, in UTC so
the times read the same anywhere. Skipped where node isn't installed; GitHub's runners
have it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_envs import HARNESS, NODE, STATIC   # noqa: E402

SCRIPT = STATIC / "gpu_history.js"
GB = 10**9
T = datetime(2026, 11, 22, 21, 45, tzinfo=timezone.utc).timestamp()     # a Sunday
BOX = {"t0": 0, "t1": 1000, "x0": 0, "x1": 1000, "y0": 0, "y1": 100}       # a second a pixel, 1% a pixel


def sample(at, util=None, used=None, total=None, jobs=0, gpu=0):
    return {"gpu": gpu, "at": at, "util": util, "used": used, "total": total, "jobs": jobs}


@unittest.skipUnless(NODE, "node isn't installed")
class Draws(unittest.TestCase):
    def run_js(self, *cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness, data = Path(tmp) / "harness.js", Path(tmp) / "cases.json"
            harness.write_text(HARNESS, encoding="utf-8")
            data.write_text(json.dumps(list(cases)), encoding="utf-8")
            out = subprocess.run([NODE, str(harness), str(SCRIPT), str(data)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60, env={**os.environ, "TZ": "UTC"})
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_each_gpu_oldest_first(self):
        [series] = self.run_js(["gpuSeries", [[sample(120, gpu=1), sample(60), sample(0, gpu=1)]]])
        self.assertEqual([(s["gpu"], [x["at"] for x in s["samples"]]) for s in series], [(0, [60]), (1, [0, 120])])

    def test_lines_break_where_the_machine_was_silent_or_nothing_was_read(self):
        ss = [sample(0, 0, 6 * GB, 24 * GB), sample(60, 50, 12 * GB, 24 * GB), sample(120, 100, 24 * GB, 24 * GB),
              sample(400, 20, None, None),                                  # 280 s later: a gap (over 150 s)
              sample(460, None, 6 * GB, 24 * GB),                           # use not read
              sample(520, 40, 6 * GB, 24 * GB)]
        use, mem, averaged = self.run_js(["gpuPath", [ss, "util", BOX, 60]], ["gpuPath", [ss, "mem", BOX, 60]],
                                         ["gpuPath", [ss[:4], "util", BOX, 600]])  # 10-minute steps: no gap there
        self.assertEqual(use, "M0.0 100.0L60.0 50.0L120.0 0.0M400.0 80.0l0 0M520.0 60.0l0 0")   # lone points: dots
        self.assertEqual(mem, "M0.0 75.0L60.0 50.0L120.0 0.0M460.0 75.0L520.0 75.0")
        self.assertEqual(averaged, "M0.0 100.0L60.0 50.0L120.0 0.0L400.0 80.0")

    def test_the_times_it_held_farm_jobs(self):
        ss = [sample(0, jobs=1), sample(60, jobs=2), sample(120), sample(600, jobs=1), sample(900, jobs=1)]
        [bands] = self.run_js(["gpuBands", [ss, BOX, 60]])
        self.assertEqual(bands, [{"x": 0, "w": 120}, {"x": 600, "w": 60}, {"x": 900, "w": 60}])

    def test_the_times_along_the_bottom(self):
        hour, day, week = self.run_js(["gpuTicks", [T - 3600, T]], ["gpuTicks", [T, T + 86400]],
                                      ["gpuTicks", [T - 7 * 86400, T]])
        self.assertEqual([k["label"] for k in hour], ["20:45", "21:00", "21:15", "21:30", "21:45"])
        self.assertEqual([k["label"] for k in day], ["Mon 23", "06:00", "12:00", "18:00"])   # a day's start: the day
        self.assertEqual([k["label"] for k in week], ["Mon 16", "Tue 17", "Wed 18", "Thu 19", "Fri 20", "Sat 21",
                                                      "Sun 22"])

    def test_pointing_at_a_time(self):
        ss = [sample(0), sample(60), sample(120), sample(600)]
        near = self.run_js(["gpuNearest", [ss, 70, 60]], ["gpuNearest", [ss, 95, 60]], ["gpuNearest", [ss, 400, 60]],
                           ["gpuNearest", [ss, -10, 60]], ["gpuNearest", [[], 10, 60]])
        self.assertEqual(near, [1, 2, -1, 0, -1])                           # 400: nothing close enough
        probed = sample(T, 87.4, 20 * GB, 24 * GB, jobs=1)
        said = self.run_js(["gpuReadout", [probed, 0]], ["gpuReadout", [probed, 20]],
                           ["gpuReadout", [sample(T, None, 8 * GB, 48 * GB), 0]])
        self.assertEqual(said, [
            "Sun 22 Nov 21:45: use 87%, memory 20.0 of 24.0 GB, 1 farm job",
            "Sun 22 Nov 21:45, 20 min: use 87% on average, memory up to 20.0 of 24.0 GB, up to 1 farm job",
            "Sun 22 Nov 21:45: use not read, memory 8.0 of 48.0 GB, no farm job"])

    def test_the_line_above_each_graph(self):
        ss = [sample(0, 90, 20 * GB, 24 * GB, 1), sample(60, 10, 22 * GB, 24 * GB, 1), sample(120, 50, 2 * GB, 24 * GB)]
        busy, comfy, none, long = self.run_js(
            ["gpuSummary", [ss, 60]], ["gpuSummary", [[sample(0, None, 8 * GB, 48 * GB)], 60]],
            ["gpuSummary", [[], 60]], ["gpuSummary", [[sample(i * 1210, 1, 1, 2, 1) for i in range(5)], 1210]])
        self.assertEqual(busy, "use 50% on average · memory up to 22.0 of 24.0 GB · farm jobs 2 min")
        self.assertEqual(comfy, "use not read (gpu_probe is off) · memory up to 8.0 of 48.0 GB · no farm jobs")
        self.assertEqual(none, "no samples in these hours")
        self.assertTrue(long.endswith("farm jobs 1.7 h"), long)

    def test_a_graph(self):
        ss = [sample(T - 120, 80, 12 * GB, 24 * GB, 1), sample(T - 60, 60, 12 * GB, 24 * GB, 1)]
        drawn, empty = self.run_js(["gpuSvg", [ss, 600, T - 3600, T, 60]], ["gpuSvg", [[], 600, T - 3600, T, 60]])
        self.assertIn('class="gline guse" d="M', drawn)
        self.assertIn('class="gline gmem" d="M', drawn)
        self.assertEqual(drawn.count('class="gjobs"'), 1)
        self.assertIn(">100%</text>", drawn)
        self.assertIn(">21:00</text>", drawn)
        self.assertIn('class="gline guse" d=""', empty)                     # nothing drawn, nothing broken


if __name__ == "__main__":
    unittest.main()
