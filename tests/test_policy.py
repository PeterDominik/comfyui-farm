"""The farm's policy (manager/policy.py, decision 0043): policy.yaml validated, re-read when it
changes, the last valid one kept in force (across a restart too), show rules checked when a
job is planned, and the refusal naming its rule. The endpoint is in test_e2e.

    python -m unittest tests.test_policy -v
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from comfyfarm.config import Config
from comfyfarm.definitions import DefinitionRegistry
from comfyfarm.manager import policy as PO

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_textures import FAKE_OIIOTOOL, OCIO_CONFIG, png_bytes   # noqa: E402

REPO = Path(__file__).resolve().parent.parent
KNOWN = {"ltx25_alpha_gen", "sam3_ltx_alpha", "wan22_i2i", "ltx25_layout_render", "chord_pbr", "gpu_test"}
try:   # the manager's farm needs pydantic and yaml (not the web stack)
    from comfyfarm.manager.events import EventBus
    from comfyfarm.manager.farm import Farm, FarmError
    from comfyfarm.manager.db import JobStore
    from comfyfarm.runners.comfyui_workflow import WorkflowRegistry
except ImportError:   # pragma: no cover
    Farm = None

RULES = """
version: 1
contact: the CG supervisors (#comfyfarm)
shows:
  - name: guem-no-ai
    shows: [guem2609]
    deny: all
    reason: The client's contract rules out generative AI on this show.
  - name: bri3-alpha-only
    shows: [BRI*]
    allow: [ltx25_alpha_gen, chord_pbr]
    reason: Only alpha generation is cleared with the client
  - name: unlp-no-wan
    shows: [unlp]
    deny: [wan22_i2i]
    reason: No generated imagery in the frame
"""


class TheFile(unittest.TestCase):
    def test_the_example_is_one_the_manager_takes(self):
        known = set(DefinitionRegistry(REPO / "workflows").workflows)
        pf = PO.parse((REPO / "policy.example.yaml").read_text(encoding="utf-8"), known)
        self.assertEqual((pf.version, pf.other_shows, pf.unknown_show, len(pf.shows)), (1, "allow", "allow", 3))

    def test_what_is_wrong_is_said(self):
        for text, says in (
                ("version: 1\nshows: [\n", "not valid YAML"),
                ("- 1\n", "a mapping, starting with `version: 1`"),
                ("shows: []\n", "version: Field required"),
                ("version: 2\n", "version: Input should be 1"),
                ("version: 1\nquota: 3\n", "quota: Extra inputs are not permitted"),
                ("version: 1\nother_shows: maybe\n", "other_shows: Input should be 'allow' or 'deny'"),
                ("version: 1\nshows:\n  - {name: a, shows: [x], reason: r}\n", "rule a: give either deny"),
                ("version: 1\nshows:\n  - {name: a, shows: [x], deny: all, allow: [wan22_i2i], reason: r}\n",
                 "rule a: give either deny"),
                ("version: 1\nshows:\n  - {name: a, shows: [], deny: all, reason: r}\n", "shows.0.shows"),
                ("version: 1\nshows:\n  - {name: a, shows: [x], deny: all}\n", "shows.0.reason: Field required"),
                ("version: 1\nshows:\n  - {name: a, shows: [x], deny: all, reason: r}\n"
                 "  - {name: a, shows: [y], deny: all, reason: r}\n", "two rules are named a"),
                ("version: 1\nshows:\n  - {name: a, shows: [x], deny: [wan22_i2v], reason: r}\n",
                 "rule a names wan22_i2v, which this farm doesn't have")):
            with self.assertRaisesRegex(ValueError, f"^.*{__import__('re').escape(says)}", msg=text):
                PO.parse(text, KNOWN)

    def test_rules(self):
        pf = PO.parse(RULES, KNOWN)
        guem, bri, unlp = pf.shows
        self.assertTrue(guem.refuses("ltx25_alpha_gen"))
        self.assertEqual((bri.matches("bri3"), bri.matches("Bri3b"), bri.matches("abri3")), (True, True, False))
        self.assertEqual((bri.refuses("ltx25_alpha_gen"), bri.refuses("wan22_i2i")), (False, True))
        self.assertEqual((unlp.refuses("wan22_i2i"), unlp.refuses("ltx25_alpha_gen")), (True, False))


class InForce(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = self.tmp / "bank" / "policy.yaml"
        self.file.parent.mkdir()
        self.saved = self.tmp / "data" / "policy.json"
        stat = mock.patch.object(PO, "STAT_SECONDS", 0)
        stat.start()
        self.addCleanup(stat.stop)

    def policy(self, enabled=True) -> PO.Policy:
        return PO.Policy(enabled, str(self.file), self.saved, lambda: KNOWN, "/rdo/bank/comfy_farm/policy.yaml")

    def write(self, text: str) -> None:
        self.file.write_text(text, encoding="utf-8")
        st = self.file.stat()                                 # seen as changed, even within the same second
        os.utime(self.file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    def test_off_allows_everything_and_reads_nothing(self):
        p = self.policy(enabled=False)
        p.check("wan22_i2i", "guem2609")
        self.assertEqual(p.status(), {"enabled": False, "file": "/rdo/bank/comfy_farm/policy.yaml", "error": None,
                                      "in_force": None})

    def test_a_barred_show_is_refused_with_the_rule_named(self):
        self.write(RULES)
        p = self.policy()
        with self.assertRaises(PO.Refused) as e:
            p.check("ltx25_alpha_gen", "guem2609")
        self.assertEqual(str(e.exception), "ltx25_alpha_gen can't run for show guem2609: The client's contract rules "
                                           "out generative AI on this show. (Policy rule guem-no-ai.) Ask the CG "
                                           "supervisors (#comfyfarm) if this is wrong.")
        self.assertEqual(e.exception.rule, "guem-no-ai")
        with self.assertRaisesRegex(PO.Refused, "Policy rule bri3-alpha-only"):
            p.check("wan22_i2i", "bri3")
        p.check("ltx25_alpha_gen", "bri3")
        p.check("wan22_i2i", "xyz")                           # no rule names it: other_shows is allow
        p.check("wan22_i2i", None)                            # unknown_show is allow

    def test_only_cleared_shows_and_known_shows(self):
        self.write("version: 1\nother_shows: deny\nunknown_show: deny\nshows:\n"
                   "  - {name: bri3-ok, shows: [bri3], deny: [], reason: cleared}\n")
        p = self.policy()
        p.check("wan22_i2i", "bri3")
        with self.assertRaisesRegex(PO.Refused, r"Show xyz isn't cleared to use the farm: no policy rule allows it"):
            p.check("wan22_i2i", "xyz")
        with self.assertRaisesRegex(PO.Refused, "needs the show to be known"):
            p.check("wan22_i2i", "")

    def test_a_broken_file_leaves_the_last_valid_one_in_force(self):
        self.write(RULES)
        p = self.policy()
        first = p.status()["in_force"]
        self.write(RULES.replace("deny: all", "deny: everything"))
        with self.assertRaisesRegex(PO.Refused, "guem-no-ai"):
            p.check("ltx25_alpha_gen", "guem2609")                       # still in force
        s = p.status()
        self.assertEqual(s["in_force"]["fingerprint"], first["fingerprint"])
        self.assertIn("/rdo/bank/comfy_farm/policy.yaml isn't taken: shows.0.deny", s["error"])
        self.assertIn("The last valid version stays in force.", s["error"])
        self.write(RULES.replace("deny: all", "deny: [wan22_i2i]"))     # fixed: taken
        p.check("ltx25_alpha_gen", "guem2609")
        s = p.status()
        self.assertEqual((s["error"], s["in_force"]["source"]), (None, "file"))
        self.assertNotEqual(s["in_force"]["fingerprint"], first["fingerprint"])

    def test_the_last_valid_one_stays_in_force_across_a_restart(self):
        self.write(RULES)
        self.policy()
        self.write("version: 1\nshows: [\n")                               # broken while the manager was down
        p = self.policy()
        with self.assertRaisesRegex(PO.Refused, "guem-no-ai"):
            p.check("ltx25_alpha_gen", "guem2609")
        s = p.status()
        self.assertEqual(s["in_force"]["source"], "saved copy")
        self.assertIn("not valid YAML", s["error"])

    def test_a_definition_removed_from_the_repo_leaves_the_last_valid_one(self):
        self.write(RULES)
        self.policy()
        known = KNOWN - {"wan22_i2i"}                                      # gone after a git pull
        p = PO.Policy(True, str(self.file), self.saved, lambda: known, "/rdo/bank/comfy_farm/policy.yaml")
        with self.assertRaisesRegex(PO.Refused, "guem-no-ai"):
            p.check("ltx25_alpha_gen", "guem2609")
        self.assertIn("rule unlp-no-wan names wan22_i2i, which this farm doesn't have", p.status()["error"])

    def test_a_file_that_goes_missing_leaves_the_last_valid_one(self):
        self.write(RULES)
        p = self.policy()
        self.file.unlink()
        with self.assertRaisesRegex(PO.Refused, "guem-no-ai"):
            p.check("ltx25_alpha_gen", "guem2609")
        self.assertIn("can't be read", p.status()["error"])

    def test_never_a_valid_file_means_nothing_is_planned(self):
        self.write("version: 1\nother_shows: maybe\n")
        p = self.policy()
        with self.assertRaisesRegex(PO.Refused, "The farm's policy can't be read, so no job can be planned until it "
                                                "is fixed: .*other_shows"):
            p.check("ltx25_alpha_gen", "xyz")
        self.assertIsNone(p.status()["in_force"])


class Paths(unittest.TestCase):
    def test_in_the_bank_folder_or_a_path_of_its_own(self):
        cfg = Config.model_validate({"workers": [], "share": {"dir": "/rdo/bank/comfy_farm"},
                                     "paths": {"mappings": [{"posix": "/rdo/bank", "windows": "B:/bank"}]}})
        self.assertEqual(PO.policy_path(cfg)[1], "/rdo/bank/comfy_farm/policy.yaml")
        cfg.policy.file = "/rdo/bank/elsewhere/rules.yaml"
        self.assertEqual(PO.policy_path(cfg)[1], "/rdo/bank/elsewhere/rules.yaml")
        cfg.policy.file = ""
        self.assertEqual(PO.policy_path(cfg), (None, None))


class Check(unittest.TestCase):
    def test_comfyfarm_policy_check(self):
        from comfyfarm.cli import main
        with tempfile.TemporaryDirectory() as d:
            good, bad = Path(d, "good.yaml"), Path(d, "bad.yaml")
            good.write_text(RULES, encoding="utf-8")
            bad.write_text(RULES.replace("wan22_i2i", "wan22_i2v"), encoding="utf-8")
            config = Path(d, "config.yaml")
            config.write_text(f"workers: []\nworkflows_dir: {(REPO / 'workflows').as_posix()}\n", encoding="utf-8")
            for f, code, says in ((good, 0, "guem-no-ai: guem2609 denies everything: The client's contract"),
                                  (bad, 1, "[FAIL]"), (bad, 1, "rule unlp-no-wan names wan22_i2v")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(main(["--config", str(config), "policy", "check", str(f)]), code)
                self.assertIn(says, out.getvalue())


@unittest.skipIf(Farm is None, "needs the manager's dependencies (pydantic, pyyaml)")
class WhenPlanned(unittest.TestCase):
    """The gate: a barred show is refused at plan, with the rule named."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = self.tmp.replace("\\", "/")
        os.makedirs(f"{self.root}/ocio")
        os.makedirs(f"{self.root}/bank")
        Path(f"{self.root}/ocio/config.ocio").write_text(OCIO_CONFIG)
        Path(f"{self.root}/bank/policy.yaml").write_text(RULES.replace("guem2609", "xyz"))
        cfg = Config.model_validate({
            "database": f"{self.root}/farm.db", "workflows_dir": str(REPO / "workflows"), "workers": [],
            "share": {"dir": f"{self.root}/bank"}, "policy": {"enabled": True},
            "paths": {"mappings": [{"posix": "/rdo/shows", "windows": f"{self.root}/shows"}],
                      "output_root": f"{self.root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"},
            "ocio": {"config": f"{self.root}/ocio/config.ocio"},
            "convert": {"scratch_dir": f"{self.root}/scratch", "tools": {"oiiotool": FAKE_OIIOTOOL}},
        })
        self.store = JobStore(Path(f"{self.root}/farm.db"))
        self.farm = Farm(cfg, self.store, WorkflowRegistry(REPO / "workflows"), EventBus())
        self.src = f"{self.root}/shows/xyz/_asset/rockA/cg/textures/src"
        os.makedirs(self.src)
        Path(f"{self.src}/rockA_diffuse.1001.png").write_bytes(png_bytes(2, (64, 64)))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp)

    def test_a_barred_show_is_refused_at_plan_with_the_rule_named(self):
        with self.assertRaises(FarmError) as e:
            self.farm.plan("chord_pbr", {"input": "/rdo/shows/xyz/_asset/rockA/cg/textures/src"})
        self.assertEqual(e.exception.status, 403)
        self.assertIn("chord_pbr can't run for show xyz", str(e.exception))
        self.assertIn("(Policy rule guem-no-ai.)", str(e.exception))
        with self.assertRaisesRegex(FarmError, "guem-no-ai"):
            self.farm.submit("chord_pbr", {"input": "/rdo/shows/xyz/_asset/rockA/cg/textures/src"}, "ann")
        self.assertEqual(self.store.list(), [])                               # nothing queued


if __name__ == "__main__":
    unittest.main()
