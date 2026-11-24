"""The Machines tab's gateway (static/gateway.js, decisions 0055, 0056): its lanes in place of
a GPU, from workers shaped as GET /api/v1/workers gives them; and the page using it.

The functions touch no page, so node runs them alone. Skipped where node isn't installed;
GitHub's runners have it."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_envs import HARNESS, NODE, STATIC   # noqa: E402

SCRIPT = STATIC / "gateway.js"
GATEWAY = {"name": "wrk1125l-gateway", "lanes": {"fal": 2}, "online": True, "connected": True}
GPU = {"name": "wrk59467w-5090", "lanes": {}, "online": True, "connected": True}


@unittest.skipUnless(NODE, "node isn't installed")
class Lanes(unittest.TestCase):
    def run_js(self, *cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness, data = Path(tmp) / "harness.js", Path(tmp) / "cases.json"
            harness.write_text(HARNESS, encoding="utf-8")
            data.write_text(json.dumps(list(cases)), encoding="utf-8")
            out = subprocess.run([NODE, str(harness), str(SCRIPT), str(data)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_the_gateway_shows_its_lanes(self):
        gw, gpu, old = self.run_js(["isGateway", [GATEWAY]], ["isGateway", [GPU]], ["isGateway", [{"name": "x"}]])
        self.assertEqual((gw, gpu, old), (True, False, False))     # an agent from before lanes: a GPU machine
        text, tip, none = self.run_js(["laneText", [GATEWAY]], ["laneTip", [GATEWAY]], ["laneText", [GPU]])
        self.assertEqual(text, "gateway · fal ×2")
        self.assertEqual(tip, "The farm's gateway: it sends API jobs to outside providers (fal: 2 jobs at once, "
                              "gateway.yaml). It has no GPU and no ComfyUI, and it is left out of rollouts and "
                              "ComfyUI syncs.")
        self.assertEqual(none, "")
        two, = self.run_js(["laneText", [{"lanes": {"fal": 1, "higgsfield": 3}}]])
        self.assertEqual(two, "gateway · fal ×1, higgsfield ×3")


class ThePage(unittest.TestCase):
    def test_the_machines_tab_uses_it(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<script src="/gateway.js"></script>', html)
        self.assertLess(html.index('<script src="/gateway.js"></script>'), html.index("function renderWorkers"))
        self.assertIn("isGateway(w) ? `<td title=\"${esc(laneTip(w))}\">${esc(laneText(w))}</td>", html)
        self.assertIn("w.online && !isGateway(w) ? `<button", html)                  # no Free VRAM: no GPU
        envs = (STATIC / "environments.js").read_text(encoding="utf-8")
        self.assertIn('filter(w => !(typeof isGateway === "function" && isGateway(w)))', envs)


if __name__ == "__main__":
    unittest.main()
