"""The dashboard's Usage tab (static/usage.js, decision 0050): what each cell, the bar and the
line above the table say, from data shaped as GET /api/v1/usage gives it; and the tab in the
page.

The functions that decide what to show touch no page, so node runs them alone. Skipped
where node isn't installed; GitHub's runners have it."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_envs import HARNESS, NODE, STATIC   # noqa: E402

SCRIPT = STATIC / "usage.js"


def row(key="2026-11-22", label=None, jobs=0, failed=0, gpu=0.0, lost=0.0, **kw):
    return {"key": key, "label": label or key, "jobs": jobs, "done": jobs - failed, "failed": failed, "cancelled": 0,
            "gpu_seconds": gpu, "lost_seconds": lost, "frames": 0, "wait_seconds": None, **kw}


@unittest.skipUnless(NODE, "node isn't installed")
class Says(unittest.TestCase):
    def run_js(self, *cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness, data = Path(tmp) / "harness.js", Path(tmp) / "cases.json"
            harness.write_text(HARNESS, encoding="utf-8")
            data.write_text(json.dumps(list(cases)), encoding="utf-8")
            out = subprocess.run([NODE, str(harness), str(SCRIPT), str(data)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_a_rows_name(self):
        said = self.run_js(["usageLabel", [row("2026-11-22"), "day"]], ["usageLabel", [row("2026-03-01"), "day"]],
                           ["usageLabel", [row("chord_pbr", "CHORD PBR"), "workflow"]],
                           ["usageLabel", [row("", "no show"), "show"]])
        self.assertEqual(said, ["Sun 22 Nov", "Sun 1 Mar", "CHORD PBR", "no show"])

    def test_the_numbers(self):
        said = self.run_js(
            ["usageHours", [0]], ["usageHours", [100]], ["usageHours", [12.5 * 3600]], ["usageHours", [1204.4 * 3600]],
            ["usageFailed", [row(jobs=8, failed=0)]], ["usageFailed", [row(jobs=8, failed=3)]],
            ["usageWait", [None]], ["usageWait", [42.4]], ["usageWait", [250]], ["usageWait", [7200]])
        self.assertEqual(said, ["0", "<0.1", "12.5", "1,204", "0", "3 (38%)", "—", "42s", "4 min", "2.0 h"])

    def test_the_gpu_hours_bar_and_its_tooltip(self):
        rows = [row(gpu=7200, lost=1800), row(gpu=1800), row(gpu=0)]
        bars = self.run_js(*[["usageBar", [r, rows]] for r in rows], ["usageBar", [row(), [row()]]])
        self.assertEqual(bars, [100, 25, 0, 0])
        lost, clean = self.run_js(["usageHoursTip", [rows[0]]], ["usageHoursTip", [rows[1]]])
        self.assertTrue(lost.endswith("; 0.5 of them on runs that failed or were cancelled"), lost)
        self.assertTrue(clean.startswith("0.5 GPU hours, from each run's start on the GPU until it finished"), clean)
        self.assertNotIn("failed", clean)

    def test_the_line_above_the_table(self):
        u = {"since": "2026-10-24", "show": None, "workflow": None, "workflows": [{"name": "chord_pbr",
                                                                                   "label": "CHORD PBR"}],
             "total": {**row(jobs=412, failed=9, gpu=37.5 * 3600), "cancelled": 2, "frames": 18204}}
        every, one, none = self.run_js(["usageTotal", [u]], ["usageTotal", [{**u, "show": "unlp",
                                                                              "workflow": "chord_pbr"}]],
                                       ["usageTotal", [{**u, "show": ""}]])
        self.assertEqual(every, "412 jobs, 37.5 GPU hours, 9 (2%) failed, 2 cancelled, 18,204 frames done, "
                                "since Sat 24 Oct")
        self.assertTrue(one.startswith("show unlp, CHORD PBR: 412 jobs"), one)
        self.assertTrue(none.startswith("show (none): "), none)

    def test_the_request(self):
        said = self.run_js(["usageQuery", [{"days": 30, "by": "day", "show": None, "workflow": None}]],
                           ["usageQuery", [{"days": 7, "by": "workflow", "show": "a b&c", "workflow": "chord_pbr"}]],
                           ["usageQuery", [{"days": 90, "by": "machine", "show": "", "workflow": None}]])
        self.assertEqual(said, ["/api/v1/usage?days=30&by=day",
                                "/api/v1/usage?days=7&by=workflow&show=a%20b%26c&workflow=chord_pbr",
                                "/api/v1/usage?days=90&by=machine&show="])


class UsageTab(unittest.TestCase):
    def test_a_tab_of_its_own(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<button data-tab="usage">Usage</button>', html)
        self.assertIn('<section id="tab-usage" hidden>', html)
        self.assertIn('<script src="/usage.js"></script>', html)
        self.assertIn('"usage"', html.split("const TABS = ")[1].split("\n")[0])
        script = SCRIPT.read_text(encoding="utf-8")
        for el in ("usageDays", "usageBy", "usageShow", "usageWorkflow", "usageCsv", "usageNote", "usageHead",
                   "usageRows", "usageTable"):
            self.assertIn(f'id="{el}"', html)
            self.assertIn(f'"#{el}"', script)


if __name__ == "__main__":
    unittest.main()
