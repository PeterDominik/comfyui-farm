"""Turning a workflow off on a farm (decision 0066): the manager's disabled_workflows. A
definition named there is still read, and kept aside: in no menu or list, refused at submit
with a reason an artist can read, never handed out or evaluated, while the policy still knows
its name and the farm's ComfyUI environment still counts what it needs.

    python -m unittest tests.test_workflows_off -v
"""

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from comfyfarm import catalog as C
from comfyfarm.config import Config
from comfyfarm.definitions import DefinitionRegistry, ParamError

REPO = Path(__file__).resolve().parent.parent
OFF = ["ltx25_alpha_gen", "ltx25_layout_render", "sam3_ltx_alpha"]     # the pilot's, 25 November 2026

try:
    from comfyfarm.manager.db import JobStore
    from comfyfarm.manager.events import EventBus
    from comfyfarm.manager.farm import Farm
except ImportError:   # pragma: no cover
    Farm = None


class TheRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = DefinitionRegistry(REPO / "workflows", off=OFF)

    def test_they_are_read_and_kept_aside(self):
        self.assertEqual(sorted(self.reg.disabled), OFF)
        self.assertFalse(set(OFF) & set(self.reg.workflows))
        everything = DefinitionRegistry(REPO / "workflows")
        self.assertEqual(set(self.reg.workflows) | set(self.reg.disabled), set(everything.workflows))
        self.assertEqual(self.reg.load_errors, {})

    def test_asking_for_one_says_it_is_off(self):
        with self.assertRaises(ParamError) as e:
            self.reg.get("sam3_ltx_alpha")
        self.assertEqual(str(e.exception), "SAM3 x LTX alpha (one subject) (sam3_ltx_alpha) is turned off on this "
                                           "farm for now. Ask the farm's admins: it is in disabled_workflows in the "
                                           "manager's config.yaml.")
        self.assertEqual(self.reg.get("wan22_i2i").name, "wan22_i2i")
        with self.assertRaisesRegex(ParamError, "Unknown workflow 'nope'"):
            self.reg.get("nope")

    def test_in_no_menu(self):
        cat = C.current()
        for hidden in (False, True):        # artists' menus, and the dashboard's with the hidden ones
            names = {w["name"] for p in cat.tree(self.reg.workflows.values(), hidden=hidden)
                     for t in p["tasks"] for w in t["workflows"]}
            self.assertFalse(names & set(OFF), hidden)
            self.assertIn("wan22_i2i", names)

    def test_a_name_that_isnt_a_definition_is_said(self):
        with self.assertLogs("comfyfarm", "WARNING") as logs:
            reg = DefinitionRegistry(REPO / "workflows", off=["ltx_alpha"])
        self.assertEqual(reg.disabled, {})
        self.assertIn("disabled_workflows names ltx_alpha, which isn't a definition in", logs.output[0])

    def test_comfyfarm_check_lists_them(self):
        from comfyfarm.cli import check_workflows
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            check_workflows(self.reg)
        for name in OFF:
            self.assertIn(f"[ off] workflow {name}: turned off on this farm (disabled_workflows in config.yaml)",
                          out.getvalue())
        self.assertNotIn("workflow ltx25_alpha_gen (", out.getvalue())

    def test_the_config(self):
        self.assertEqual(Config.model_validate({}).disabled_workflows, [])          # off by default: none
        self.assertEqual(Config.model_validate({"disabled_workflows": OFF}).disabled_workflows, OFF)


@unittest.skipIf(Farm is None, "needs the manager's dependencies")
class TheFarm(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        cfg = Config.model_validate({"database": str(self.tmp / "farm.db"), "workflows_dir": str(REPO / "workflows"),
                                     "workers": [], "disabled_workflows": OFF})
        self.store = JobStore(self.tmp / "farm.db")
        self.addCleanup(self.store.close)
        self.farm = Farm(cfg, self.store, DefinitionRegistry(REPO / "workflows", off=cfg.disabled_workflows),
                         EventBus())

    def test_a_job_of_one_is_refused_saying_why(self):
        with self.assertRaisesRegex(ParamError, r"LTX 2\.5 alpha generation \(ltx25_alpha_gen\) is turned off on "
                                                r"this farm for now"):
            self.farm.plan("ltx25_alpha_gen", {"input": "/rdo/shows/rnd/x/plate.%04d.exr 1001-1010"}, "ann")

    def test_the_policy_still_knows_its_name(self):
        """A rule naming it stays valid while it is off, ready for when it comes back."""
        self.assertLessEqual(set(OFF), self.farm.policy.known())


if __name__ == "__main__":
    unittest.main()
