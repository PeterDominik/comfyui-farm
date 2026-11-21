"""A request (docs/decisions/0013): plain data the manager stores, from which the machine
that runs the job builds its ComfyUI graph with its own copy of the workflow."""

import unittest
from pathlib import Path

from comfyfarm.fileio import wire as W
from comfyfarm.definitions import ParamError
from comfyfarm.runners.comfyui_workflow import WorkflowRegistry

REPO = Path(__file__).resolve().parent.parent


class RequestTests(unittest.TestCase):
    """Building from a request on another machine gives the graph that machine needs: the one
    built on the manager and mapped to its paths."""

    @classmethod
    def setUpClass(cls):
        cls.registry = WorkflowRegistry(REPO / "workflows")

    def test_the_fingerprint_is_of_the_content_not_the_file(self):
        again = WorkflowRegistry(REPO / "workflows")
        self.assertEqual({n: w.digest for n, w in again.workflows.items()},
                         {n: w.digest for n, w in self.registry.workflows.items()})
        self.assertEqual(len({w.digest for w in self.registry.workflows.values()}), len(self.registry.workflows))
        self.assertRegex(self.registry.workflows["wan22_i2i"].digest, r"^[0-9a-f]{16}$")

    def test_a_request_from_another_definition_is_refused(self):
        wf = self.registry.workflows["wan22_i2i"]
        request = wf.request({"prompt": "x"})
        self.assertEqual(wf.build_from(request), wf.build({"prompt": "x"}))
        with self.assertRaisesRegex(ParamError, "Update the farm's code"):
            wf.build_from({**request, "definition": "feedfacefeedface"})

    def test_built_on_a_windows_machine_its_paths_are_that_machines(self):
        from comfyfarm.config import PathsConfig
        from comfyfarm.fileio.inputs import Sequence
        maps = PathsConfig(mappings=[{"posix": "/rdo/shows", "windows": "S:/shows"}]).mappings
        manager = W.View(mappings=maps, scratch="/farm/scratch", repo="/farm/repo")
        agent = W.View(mappings=maps, scratch="D:/cf/scratch", repo="D:/cf/repo")
        seq = Sequence(raw="x", farm_path="/rdo/shows/xyz/abc/abc_0010/plate/abc_0010.plate.%04d.png",
                       directory="/rdo/shows/xyz/abc/abc_0010/plate/", filename="abc_0010.plate.%04d.png",
                       first=1001, last=1003, frame_count=3, skip=0, cap=3, show="xyz", shot="abc_0010",
                       source="abc_0010.plate", load_dir="/farm/scratch/job1/in/")
        for name in ("ltx25_alpha_gen", "wan22_i2i", "sam3_ltx_alpha", "ltx25_layout_render"):
            wf = self.registry.workflows[name]
            with self.subTest(workflow=name):
                args = ({}, seq, "/rdo/shows/xyz/_cg/comfyui/renders/abc_0010/v003", "abc_0010.v003", "_cfpreview")
                sent = W.localize(W.portable(wf.build(*args), manager), agent)
                built = W.localize(wf.build_from(W.localize(W.portable(wf.request(*args), manager), agent)), agent)
                self.assertEqual(built, sent)
                self.assertIn("S:", str(built))

    def test_a_config_that_still_sets_the_old_switch_loads_and_says_so(self):
        import tempfile
        from comfyfarm.config import load_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("farm:\n  build_on_agent: true\n", encoding="utf-8")
            cfg = load_config(str(path))
        self.assertFalse(hasattr(cfg.farm, "build_on_agent"))
        self.assertTrue(any(n.startswith("farm.build_on_agent is no longer used") for n in cfg.obsolete_keys))


if __name__ == "__main__":
    unittest.main()
