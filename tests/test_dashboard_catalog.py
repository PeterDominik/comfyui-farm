"""The dashboard's Submit pickers, Provider > Task > Workflow (static/catalog.js, decision 0054):
what each picker lists and picks, from data shaped as GET /api/v1/catalog gives it; and the
pickers in the page.

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

from comfyfarm import catalog as C                     # noqa: E402
from comfyfarm.definitions import DefinitionRegistry   # noqa: E402

SCRIPT = STATIC / "catalog.js"
REPO = Path(__file__).resolve().parent.parent


def farm_tree() -> list:
    """The farm's own catalogue, hidden ones too, as the dashboard asks for it with no outside
    provider switched on (providers.enabled, decision 0055): the farm's GPUs only."""
    cat = C.load()
    own = [wf for wf in DefinitionRegistry(REPO / "workflows").workflows.values()
           if cat.providers[wf.provider].target == "pool"]
    return json.loads(json.dumps(cat.tree(own, hidden=True)))


@unittest.skipUnless(NODE, "node isn't installed")
class Picks(unittest.TestCase):
    def run_js(self, *cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness, data = Path(tmp) / "harness.js", Path(tmp) / "cases.json"
            harness.write_text(HARNESS, encoding="utf-8")
            data.write_text(json.dumps(list(cases)), encoding="utf-8")
            out = subprocess.run([NODE, str(harness), str(SCRIPT), str(data)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_first_of_each_when_nothing_is_picked(self):
        pick, = self.run_js(["catalogPick", [farm_tree(), None, None, None]])
        self.assertEqual([o["label"] for o in pick["providers"]], ["ComfyUI Local", "Python Local"])
        self.assertEqual([o["label"] for o in pick["tasks"]], ["Masking", "Video", "Image", "Textures"])
        self.assertEqual([o["value"] for o in pick["workflows"]], ["ltx25_alpha_gen", "sam3_ltx_alpha"])
        self.assertEqual((pick["provider"], pick["task"], pick["workflow"]),
                         ("comfyui-local", "masking", "ltx25_alpha_gen"))

    def test_a_provider_then_its_tasks(self):
        tree = farm_tree()
        python, textures, gone = self.run_js(["catalogPick", [tree, "python-local", None, None]],
                                             ["catalogPick", [tree, "comfyui-local", "textures", None]],
                                             ["catalogPick", [tree, "comfyui-local", "relight", "nope"]])
        self.assertEqual(([o["label"] for o in python["tasks"]], python["workflow"]), (["Tests"], "batch_test"))
        self.assertEqual([o["label"] for o in python["workflows"]],
                         ["Batch test (process runner) (hidden from artists)",
                          "GPU test (process runner) (hidden from artists)"])
        self.assertEqual((textures["task"], textures["workflow"]), ("textures", "chord_pbr"))
        self.assertEqual((gone["task"], gone["workflow"]), ("masking", "ltx25_alpha_gen"))   # gone: the first

    def test_a_workflow_by_name(self):
        tree = farm_tree()
        where = self.run_js(["catalogWhere", [tree, "wan22_i2i"]], ["catalogWhere", [tree, "gpu_test"]],
                            ["catalogWhere", [tree, "nope"]], ["catalogWhere", [None, "wan22_i2i"]])
        self.assertEqual(where, [{"provider": "comfyui-local", "task": "image"},
                                 {"provider": "python-local", "task": "tests"}, None, None])
        pick, = self.run_js(["catalogPick", [tree, "comfyui-local", "masking", "sam3_ltx_alpha"]])
        self.assertEqual(pick["workflow"], "sam3_ltx_alpha")

    def test_no_task_and_what_an_artists_menu_wouldnt_show(self):
        tree = [{"name": "comfyui-local", "label": "ComfyUI Local", "offsite": False, "tasks": [
                    {"name": "video", "label": "Video", "workflows": [
                        {"name": "a", "label": "A", "description": "Makes A.", "hidden": False, "enabled": False}]},
                    {"name": None, "label": "Other", "workflows": [{"name": "b", "label": "B", "hidden": False}]}]},
                {"name": "fal", "label": "fal.ai", "offsite": True, "tasks": []}]
        other, where, first = self.run_js(["catalogPick", [tree, "comfyui-local", "", None]],
                                          ["catalogWhere", [tree, "b"]],
                                          ["catalogPick", [tree, "comfyui-local", None, None]])
        self.assertEqual((other["task"], other["workflow"]), ("", "b"))
        self.assertEqual(other["tasks"], [{"value": "video", "label": "Video"}, {"value": "", "label": "Other"}])
        self.assertEqual(where, {"provider": "comfyui-local", "task": ""})
        self.assertEqual(first["workflows"], [{"value": "a", "label": "A (turned off)", "title": "Makes A."}])
        self.assertEqual(first["providers"][1], {"value": "fal", "label": "fal.ai (off-site)"})
        empty, = self.run_js(["catalogPick", [tree, "fal", None, None]])
        self.assertEqual((empty["tasks"], empty["workflows"], empty["task"], empty["workflow"]), ([], [], None, None))
        nothing, = self.run_js(["catalogPick", [None, None, None, None]])
        self.assertEqual((nothing["providers"], nothing["provider"]), ([], None))


class SubmitTab(unittest.TestCase):
    def test_the_pickers_in_the_page(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")
        # Loaded before the page's own script, so the pickers are there when it boots.
        self.assertLess(html.index('<script src="/catalog.js"></script>'), html.index("const $ = (s, el = document)"))
        self.assertIn('<div class="pickers plain" id="catalogPickers">', html)
        for el in ("provSelect", "taskSelect", "wfSelect", "catalogPickers"):
            self.assertIn(f'id="{el}"', html)
            self.assertIn(f'"#{el}"', script)
        self.assertIn("await catalogInit();", html)
        self.assertIn("catalogShow(d.workflow); renderForm();", html)
        self.assertIn('api("/api/v1/catalog?hidden=true")', script)


if __name__ == "__main__":
    unittest.main()
