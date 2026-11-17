"""ShotGrid's right-click menu as the farm builds it (comfyfarm.shotgrid.menu, chunk 13c):
which workflow goes in which folder (Submit, Submit + Publish, Open) from its manifest,
the shows from deploy/shotgrid/menu.yaml, and the items."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from comfyfarm.shotgrid import menu as M
from comfyfarm.workflows import Workflow, WorkflowRegistry

REPO = Path(__file__).resolve().parent.parent
REGISTRY = WorkflowRegistry(REPO / "workflows")
FARM = "http://wrk1125l.rodeofx.com:9090"


def publishes(wf):          # as the publisher says while publishing is on: not textures
    return None if wf.textures or (wf.publish and "off" in wf.publish) else {"step": "lay"}


class Fit(unittest.TestCase):
    def test_the_farms_workflows(self):
        got = {wf.name: M.fit(wf, publishes(wf) is not None) for wf in REGISTRY.workflows.values()}
        self.assertEqual({n: (f["open"], f["submit"], f["publish"]) for n, f in got.items()},
                         {"ltx25_alpha_gen": (True, True, True),          # one input, frames, nothing else needed
                          "chord_pbr": (True, False, False),              # Open only: says its manifest
                          "ltx25_layout_render": (True, False, False),    # two inputs and a prompt
                          "sam3_ltx_alpha": (True, False, False),         # the subject has to be named
                          "wan22_i2i": (True, False, False)})             # one image: its frame has to be picked
        self.assertEqual(got["ltx25_layout_render"]["why"], "it also needs Reference frame; it needs Prompt")
        self.assertEqual(got["sam3_ltx_alpha"]["why"], "it needs Subject")
        self.assertEqual(got["wan22_i2i"]["why"], "it takes one image, so a Version's frame has to be picked")
        self.assertEqual(got["chord_pbr"]["why"], "shotgrid: {submit: false} in its manifest")   # plates aren't UDIMs
        self.assertFalse(M.fit(REGISTRY.workflows["ltx25_alpha_gen"], False)["publish"])    # publishing off

    def manifest(self, extra: str) -> Workflow:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        shutil.copytree(REPO / "workflows" / "ltx25_alpha_gen", d / "wf")
        m = d / "wf" / "manifest.yaml"
        m.write_bytes(m.read_bytes() + b"\n" + extra.encode() + b"\n")
        return Workflow(d / "wf")

    def test_the_manifest_says(self):
        out = M.fit(self.manifest("shotgrid: false"), True)
        self.assertEqual((out["open"], out["submit"], out["why"]), (False, False, "shotgrid: false in its manifest"))
        wf = self.manifest("shotgrid: {submit: false, title: LTX alpha}")
        self.assertEqual((M.fit(wf, True)["open"], M.fit(wf, True)["submit"]), (True, False))
        self.assertEqual(M.build([wf], publishes, {"raa": "all"}, FARM)["items"][0]["title"], "LTX alpha")
        for bad in ("shotgrid: yes please", "shotgrid: {submit: 'no'}", "shotgrid: {menu: false}"):
            self.assertIn("shotgrid: needs to be false", " ".join(self.manifest(bad).problems), bad)
        self.assertEqual(M.fit(self.manifest("shotgrid: {submit: false, title: x}\nrequires: 3"), True)["open"], False)


class Shows(unittest.TestCase):
    def write(self, text: str) -> Path:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "menu.yaml").write_bytes(text.encode())
        return d / "menu.yaml"

    def test_the_repos_own(self):
        # Whichever shows it lists today (it is edited as shows come and go): it must load clean.
        s = M.load(REPO / "deploy" / "shotgrid" / "menu.yaml")
        self.assertEqual({k: v for k, v in s.items() if k != "shows"},
                         {"farm_url": FARM, "menus": ["open"], "publish": True, "problems": []})
        self.assertIn("raa", s["shows"])
        self.assertTrue(all(v == "all" or isinstance(v, list) for v in s["shows"].values()), s["shows"])

    def test_forms_and_problems(self):
        s = M.load(self.write("shows: [raa, bri3]"))
        self.assertEqual((s["shows"], s["farm_url"], s["menus"], s["publish"]), ({"raa": "all", "bri3": "all"}, None, ["open"], True))
        s = M.load(self.write("farm_url: http://x:9090/\nshows: {raa: all, bri3: [ltx25_alpha_gen]}\n"
                              "menus: [publish, open, submit]\npublish: false"))
        self.assertEqual((s["shows"], s["farm_url"], s["menus"], s["publish"]),
                         ({"raa": "all", "bri3": ["ltx25_alpha_gen"]}, "http://x:9090", ["submit", "publish", "open"], False))
        s = M.load(self.write("farm_url: wrk1125l\nshows: {'bad code': all, raa: 3}\nmenus: [open, delete]\npublish: yes please"))
        self.assertEqual(len(s["problems"]), 5, s["problems"])
        self.assertEqual(s["menus"], ["open"])                                    # what it was given doesn't count
        self.assertIn("isn't there", M.load(self.write("").parent / "nope.yaml")["problems"][0])


class Items(unittest.TestCase):
    def test_the_menu(self):
        wfs = list(REGISTRY.workflows.values())
        m = M.build(wfs, publishes, {"raa": "all", "bri3": ["ltx25_alpha_gen", "nope"]}, FARM + "/",
                    menus=("open", "submit", "publish"))
        self.assertEqual(m["problems"], ["show bri3: there's no workflow nope"])
        rows = [(i["folder"], i["title"], i["entity_type"], ",".join(i["shows"])) for i in m["items"]]
        self.assertEqual(rows, [
            ("ComfyFarm Submit", "LTX 2.5 alpha generation", "Version", "raa,bri3"),
            ("ComfyFarm Submit + Publish", "LTX 2.5 alpha generation", "Version", "raa,bri3"),
            ("ComfyFarm Open", "CHORD PBR maps from a diffuse texture", "Version", "raa"),
            ("ComfyFarm Open", "CHORD PBR maps from a diffuse texture", "Playlist", "raa"),
            ("ComfyFarm Open", "LTX 2.5 alpha generation", "Version", "raa,bri3"),
            ("ComfyFarm Open", "LTX 2.5 alpha generation", "Playlist", "raa,bri3"),
            ("ComfyFarm Open", "LTX 2.5 layout to render", "Version", "raa"),
            ("ComfyFarm Open", "LTX 2.5 layout to render", "Playlist", "raa"),
            ("ComfyFarm Open", "SAM3 x LTX alpha (one subject)", "Version", "raa"),
            ("ComfyFarm Open", "SAM3 x LTX alpha (one subject)", "Playlist", "raa"),
            ("ComfyFarm Open", "Wan 2.2 image to image", "Version", "raa"),
            ("ComfyFarm Open", "Wan 2.2 image to image", "Playlist", "raa")])
        self.assertEqual(m["items"][1]["url"], FARM + "/api/v1/shotgrid/ami?action=publish&workflow=ltx25_alpha_gen")
        self.assertEqual([i["list_order"] for i in m["items"][:3]], [101, 201, 300])          # Submit, + Publish, Open
        self.assertEqual(m["folders"], ["ComfyFarm Submit", "ComfyFarm Submit + Publish", "ComfyFarm Open"])
        self.assertEqual(M.build(wfs, publishes, {}, FARM)["items"], [])                      # no shows: no menu
        only = M.build(wfs, publishes, {"raa": "all"}, FARM)                                 # the default: Open only
        self.assertEqual((only["folders"], len(only["items"])), (["ComfyFarm Open"], 10))
        self.assertEqual([i["list_order"] for i in only["items"]][:2], [300, 300])          # as with the other folders on


if __name__ == "__main__":
    unittest.main()
