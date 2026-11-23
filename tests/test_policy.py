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
from types import SimpleNamespace
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
        self.assertEqual((pf.version, pf.other_shows, pf.unknown_show, len(pf.shows), len(pf.licences),
                          pf.unlisted_models, len(pf.quotas)), (1, "allow", "allow", 3, 1, "allow", 2))

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


RESEARCH = "ubisoft-machine-learning-license-research-only-copyleft"
LICENCES = """
version: 1
licences:
  - name: no-research-only
    shows: ["*"]
    except: [rnd*]
    deny: [Ubisoft-Machine-Learning-License-Research-Only-Copyleft]
    reason: Research-only weights can't be used on client work
  - name: bri3-apache-only
    shows: [bri3]
    allow: [apache-2.0]
    reason: Only Apache-licensed weights are cleared on this show
"""
CHORD = [{"file": "chord_v1.safetensors", "licence": RESEARCH}]
WAN = [{"file": "WAN/wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors", "licence": "Apache-2.0"},
       {"file": "umt5_xxl_fp8_e4m3fn_scaled.safetensors", "licence": "apache-2.0"}]


class Licences(unittest.TestCase):
    """Decision 0044: the licences of the models a definition lists, by show."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.file = tmp / "policy.yaml"
        self.saved = tmp / "policy.json"

    def policy(self, text: str) -> PO.Policy:
        self.file.write_text(text, encoding="utf-8")
        return PO.Policy(True, str(self.file), self.saved, lambda: KNOWN)

    def test_a_licence_a_rule_denies_is_refused_with_the_file_named(self):
        p = self.policy(LICENCES)
        with self.assertRaises(PO.Refused) as e:
            p.check("chord_pbr", "guem2609", CHORD)
        self.assertEqual(str(e.exception), f"chord_pbr can't run for show guem2609: it uses chord_v1.safetensors, "
                                           f"licensed {RESEARCH}. Research-only weights can't be used on client "
                                           f"work. (Policy rule no-research-only.)")
        self.assertEqual(e.exception.rule, "no-research-only")
        p.check("chord_pbr", "rnd2601", CHORD)                              # except: [rnd*]
        p.check("wan22_i2i", "guem2609", WAN)
        with self.assertRaisesRegex(PO.Refused, "chord_pbr can't run without a known show: it uses chord_v1"):
            p.check("chord_pbr", None, CHORD)                               # * is every job, a known show or not

    def test_a_rule_that_allows_only_some_licences(self):
        p = self.policy(LICENCES)
        p.check("wan22_i2i", "bri3", WAN)                                   # case doesn't matter
        with self.assertRaisesRegex(PO.Refused, r"it uses chord_v1.safetensors, licensed .*\(Policy rule "
                                                r"no-research-only\.\)"):
            p.check("chord_pbr", "bri3", CHORD)                             # the first rule that refuses is named
        with self.assertRaisesRegex(PO.Refused, r"it uses x.safetensors, licensed cc-by-nc-4.0\. Only Apache.*"
                                                r"bri3-apache-only"):
            p.check("wan22_i2i", "bri3", [*WAN, {"file": "x.safetensors", "licence": "cc-by-nc-4.0"}])
        p.check("gpu_test", "bri3", [])                                     # models: [] uses no weights

    def test_a_definition_that_doesnt_list_its_models(self):
        p = self.policy(LICENCES)
        p.check("ltx25_alpha_gen", "guem2609", None)                        # unlisted_models: allow
        p = self.policy(LICENCES + "unlisted_models: deny\n")
        with self.assertRaisesRegex(PO.Refused, "ltx25_alpha_gen can't run for show guem2609: its process.yaml "
                                                "doesn't list its models and their licences"):
            p.check("ltx25_alpha_gen", "guem2609", None)
        p.check("ltx25_alpha_gen", "rnd2601", None)                         # no licence rule for it: not asked

    def test_what_is_wrong_is_said(self):
        for text, says in (
                ("version: 1\nlicences:\n  - {name: a, shows: [x], reason: r}\n", "rule a: give either deny"),
                ("version: 1\nlicences:\n  - {name: a, shows: [x], deny: all, reason: r}\n",
                 "licences.0.deny: Input should be a valid list"),
                ("version: 1\nshows:\n  - {name: a, shows: [x], deny: all, reason: r}\n"
                 "licences:\n  - {name: a, shows: [x], deny: [mit], reason: r}\n", "two rules are named a"),
                ("version: 1\nunlisted_models: maybe\n", "unlisted_models: Input should be 'allow' or 'deny'")):
            with self.assertRaisesRegex(ValueError, __import__("re").escape(says), msg=text):
                PO.parse(text, KNOWN)

    def test_except_works_for_show_rules_too_and_is_shown_as_written(self):
        p = self.policy("version: 1\nshows:\n  - {name: all-but-rnd, shows: ['*'], except: [rnd*], deny: all, "
                        "reason: Closed}\n")
        p.check("wan22_i2i", "rnd2601", WAN)
        with self.assertRaisesRegex(PO.Refused, "all-but-rnd"):
            p.check("wan22_i2i", "bri3", WAN)
        self.assertEqual(p.status()["in_force"]["shows"][0]["except"], ["rnd*"])

    def test_the_definitions_say_what_they_use(self):
        wfs = DefinitionRegistry(REPO / "workflows").workflows
        self.assertEqual((wfs["chord_pbr"].models_listed, wfs["chord_pbr"].models), (True, CHORD))
        self.assertEqual((wfs["gpu_test"].models_listed, wfs["gpu_test"].models), (True, []))
        self.assertFalse(wfs["wan22_i2i"].models_listed)

    def test_notes_for_the_admin(self):
        from comfyfarm.cli import model_notes
        defs = {"chord_pbr": SimpleNamespace(models_listed=True, models=CHORD),
                "wan22_i2i": SimpleNamespace(models_listed=True, models=WAN[:1]),
                "ltx": SimpleNamespace(models_listed=False, models=[])}
        env = {"models": {"chord_v1.safetensors": {"for": ["chord_pbr"]},
                          "wan2.2_t2v_low_noise_14B_fp8_scaled.safetensors": {"for": ["wan22_i2i"]},
                          "umt5_xxl_fp8_e4m3fn_scaled.safetensors": {"for": ["wan22_i2i"]},
                          "ltx.safetensors": {"for": ["ltx"]}}}
        typo = PO.parse(LICENCES.replace("allow: [apache-2.0]", "allow: [apache2]"))
        self.assertEqual(model_notes(defs, env, typo), [
            "these don't list their models (licence rules treat them as unlisted_models says): ltx",
            "wan22_i2i loads umt5_xxl_fp8_e4m3fn_scaled.safetensors (deploy/comfyui/environment.yaml), which its "
            "models: doesn't list",
            "rule bri3-apache-only names licence apache2, which no definition's models carry"])
        self.assertEqual(len(model_notes(defs, env, PO.parse(LICENCES))), 2)   # Apache-2.0 is carried, any case


QUOTAS = """
version: 1
contact: the CG supervisors (#comfyfarm)
quotas:
  - name: guem-20-a-show
    shows: [guem*]
    max_jobs: 3
    max_frames: 1000
    reason: The show's share of the pilot's GPUs
  - name: two-each
    shows: ["*"]
    except: [rnd*]
    per: user
    max_jobs: 2
    reason: Leave room for everyone
  - name: cold-and-ret-together
    shows: [cold, ret*]
    per: all
    max_frames: 500
    reason: Their client's budget
"""


class Quotas(unittest.TestCase):
    """Decision 0045: how much a show, a user on it, or some shows together may have queued or
    running at once, checked at plan with what is queued now."""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.file = tmp / "policy.yaml"
        self.file.write_text(QUOTAS, encoding="utf-8")
        self.p = PO.Policy(True, str(self.file), tmp / "policy.json", lambda: KNOWN)

    @staticmethod
    def queued(*rows) -> mock.Mock:
        return mock.Mock(return_value=[{"show": s, "user": u, "frames": f} for s, u, f in rows])

    def test_jobs_a_show_has_queued(self):
        q = self.queued(("guem2609", "ann", 10), ("guem2609", "bob", 10), ("bri3", "ann", 10))
        self.p.quota("guem2609", "cid", 10, q)                                  # 2 of 3
        q = self.queued(*[("guem2609", u, 10) for u in ("ann", "bob", "cid")], ("bri3", "ann", 10))
        with self.assertRaises(PO.Refused) as e:
            self.p.quota("guem2609", "dee", 10, q)
        self.assertEqual(str(e.exception), "There are already 3 job(s) queued or running for show guem2609, and the "
                                           "most is 3: The show's share of the pilot's GPUs. (Policy rule "
                                           "guem-20-a-show.) Submit again when some have finished. Ask the CG "
                                           "supervisors (#comfyfarm) if this is wrong.")
        self.assertEqual(e.exception.rule, "guem-20-a-show")
        self.p.quota("guem2610", "dee", 10, q)                                  # each show its own count

    def test_frames_a_show_has_queued(self):
        q = self.queued(("guem2609", "ann", 900))
        self.p.quota("guem2609", "bob", 100, q)                                 # 1000 of 1000
        with self.assertRaisesRegex(PO.Refused, r"This job's 101 frame\(s\) would take show guem2609 to 1001 "
                                                r"queued or running, and the most is 1000: .*or with fewer frames"):
            self.p.quota("guem2609", "bob", 101, q)
        with self.assertRaisesRegex(PO.Refused, "to 1200 queued or running"):
            self.p.quota("guem2610", "bob", 1200, self.queued())               # one job bigger than the quota

    def test_per_user_and_except(self):
        q = self.queued(("bri3", "ann", 1), ("unlp", "ann", 1), ("bri3", "bob", 1))
        with self.assertRaisesRegex(PO.Refused, "2 job\\(s\\) queued or running for ann on every show but rnd\\*, "
                                                "and the most is 2.*two-each"):
            self.p.quota("bri3", "ann", 1, q)                                   # counted over every show it's for
        self.p.quota("bri3", "bob", 1, q)
        self.p.quota("rnd2601", "ann", 1, q)                                    # except: [rnd*]
        self.p.quota("bri3", None, 1, q)                                        # no user: per user isn't asked

    def test_shows_together(self):
        q = self.queued(("cold", "ann", 300), ("retkmc2610", "bob", 150), ("bri3", "ann", 400))
        self.p.quota("ret1", "cid", 50, q)
        with self.assertRaisesRegex(PO.Refused, r"would take shows cold, ret\* to 501 .*cold-and-ret-together"):
            self.p.quota("cold", "cid", 51, q)

    def test_what_is_queued_against_each_quota(self):
        """Decision 0047: the Policy tab's counts, as each quota counts them, the fullest first."""
        queued = [{"show": "guem2609", "user": "ann", "frames": 10}, {"show": "guem2609", "user": "bob", "frames": 5},
                  {"show": "guem2610", "user": "ann", "frames": 900}, {"show": "cold", "user": "bob", "frames": 300},
                  {"show": "rnd2601", "user": "ann", "frames": 1}]
        self.assertEqual(self.p.usage(queued), {
            "guem-20-a-show": [{"for": "guem2609", "jobs": 2, "frames": 15},
                               {"for": "guem2610", "jobs": 1, "frames": 900}],
            "two-each": [{"for": "ann", "jobs": 2, "frames": 910}, {"for": "bob", "jobs": 2, "frames": 305}],
            "cold-and-ret-together": [{"for": "together", "jobs": 1, "frames": 300}]})
        self.assertEqual(PO.Policy(False, str(self.file), self.file.with_suffix(".json"), lambda: KNOWN).usage(queued),
                         {})

    def test_whats_queued_is_only_asked_when_a_quota_is_for_the_show(self):
        self.file.write_text(QUOTAS.replace('["*"]', "[bri3]"), encoding="utf-8")
        p = PO.Policy(True, str(self.file), self.file.with_suffix(".json"), lambda: KNOWN)
        q = self.queued()
        p.quota("unlp", "ann", 1, q)
        q.assert_not_called()
        PO.Policy(False, str(self.file), self.file.with_suffix(".json"), lambda: KNOWN).quota("guem1", "a", 9999, q)
        q.assert_not_called()

    def test_what_is_wrong_is_said(self):
        for text, says in (
                ("version: 1\nquotas:\n  - {name: a, shows: [x], reason: r}\n", "rule a: give max_jobs, max_frames"),
                ("version: 1\nquotas:\n  - {name: a, shows: [x], max_jobs: 0, reason: r}\n",
                 "quotas.0.max_jobs: Input should be greater than or equal to 1"),
                ("version: 1\nquotas:\n  - {name: a, shows: [x], per: team, max_jobs: 1, reason: r}\n",
                 "quotas.0.per: Input should be 'show', 'user' or 'all'"),
                ("version: 1\nshows:\n  - {name: a, shows: [x], deny: all, reason: r}\n"
                 "quotas:\n  - {name: a, shows: [x], max_jobs: 1, reason: r}\n", "two rules are named a")):
            with self.assertRaisesRegex(ValueError, __import__("re").escape(says), msg=text):
                PO.parse(text, KNOWN)


class Paths(unittest.TestCase):
    def test_in_the_bank_folder_or_a_path_of_its_own(self):
        cfg = Config.model_validate({"workers": [], "share": {"dir": "/rdo/bank/comfy_farm"},
                                     "paths": {"mappings": [{"posix": "/rdo/bank", "windows": "B:/bank"}]}})
        self.assertEqual(PO.policy_path(cfg)[1], "/rdo/bank/comfy_farm/policy.yaml")
        cfg.policy.file = "/rdo/bank/elsewhere/rules.yaml"
        self.assertEqual(PO.policy_path(cfg)[1], "/rdo/bank/elsewhere/rules.yaml")
        cfg.share.dir = ""                                          # no bank folder: beside config.yaml
        cfg.base_dir = REPO
        cfg.policy.file = "policy.yaml"
        local, shown = PO.policy_path(cfg)
        self.assertTrue(local.endswith("/policy.yaml") and "\\" not in local, local)     # forward slashes on Windows
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

    def test_a_licence_a_show_doesnt_take_is_refused_at_plan(self):
        """Decision 0044: CHORD's own models: list, against the show's licence rules."""
        Path(f"{self.root}/bank/policy.yaml").write_text(LICENCES.replace('["*"]', "[xyz]"))
        self.farm.policy.refresh(force=True)
        with self.assertRaisesRegex(FarmError, "chord_pbr can't run for show xyz: it uses chord_v1.safetensors, "
                                               f"licensed {RESEARCH}.*no-research-only"):
            self.farm.plan("chord_pbr", {"input": "/rdo/shows/xyz/_asset/rockA/cg/textures/src"})

    def test_a_barred_show_is_refused_at_plan_with_the_rule_named(self):
        with self.assertRaises(FarmError) as e:
            self.farm.plan("chord_pbr", {"input": "/rdo/shows/xyz/_asset/rockA/cg/textures/src"})
        self.assertEqual(e.exception.status, 403)
        self.assertIn("chord_pbr can't run for show xyz", str(e.exception))
        self.assertIn("(Policy rule guem-no-ai.)", str(e.exception))
        with self.assertRaisesRegex(FarmError, "guem-no-ai"):
            self.farm.submit("chord_pbr", {"input": "/rdo/shows/xyz/_asset/rockA/cg/textures/src"}, "ann")
        self.assertEqual(self.store.list(), [])                               # nothing queued

    def test_the_menus_keep_what_a_show_may_not_use_and_say_why(self):
        """Decision 0054: GET /api/v1/catalog marks a workflow the show's rules refuse, with
        the reason and the rule, instead of leaving it out."""
        Path(f"{self.root}/bank/policy.yaml").write_text(RULES)
        self.farm.policy.refresh(force=True)
        flat = {w["name"]: w for p in self.farm.catalogue("bri3")["providers"] for t in p["tasks"]
                for w in t["workflows"]}
        self.assertEqual(sorted(n for n, w in flat.items() if w["allowed"]), ["chord_pbr", "ltx25_alpha_gen"])
        wan = flat["wan22_i2i"]
        self.assertEqual((wan["allowed"], wan["rule"]), (False, "bri3-alpha-only"))
        self.assertIn("wan22_i2i can't run for show bri3: Only alpha generation is cleared with the client. "
                      "(Policy rule bri3-alpha-only.)", wan["reason"])
        cat = self.farm.catalogue("guem2609")
        self.assertEqual(cat["show"], "guem2609")
        self.assertFalse(any(w["allowed"] for p in cat["providers"] for t in p["tasks"] for w in t["workflows"]))
        self.assertTrue(all(w["allowed"] for p in self.farm.catalogue("unlp")["providers"] for t in p["tasks"]
                            for w in t["workflows"] if w["name"] != "wan22_i2i"))

    def test_a_quota_is_checked_at_plan_with_what_is_queued(self):
        """Decision 0045: the second job over a show's quota is refused, in the preview too."""
        Path(f"{self.root}/bank/policy.yaml").write_text(
            "version: 1\nquotas:\n  - {name: one-at-a-time, shows: [xyz], max_jobs: 1, reason: Testing}\n")
        self.farm.policy.refresh(force=True)
        src = {"input": "/rdo/shows/xyz/_asset/rockA/cg/textures/src"}
        self.farm.submit("chord_pbr", src, "ann")
        with self.assertRaises(FarmError) as e:
            self.farm.plan("chord_pbr", src, user="bob")
        self.assertEqual(e.exception.status, 403)
        self.assertIn("There are already 1 job(s) queued or running for show xyz, and the most is 1", str(e.exception))
        with self.assertRaisesRegex(FarmError, "one-at-a-time"):
            self.farm.submit("chord_pbr", src, "bob")
        self.assertEqual(len(self.store.list()), 1)

    def test_what_the_policy_tab_reads(self):
        """Decision 0047: GET /api/v1/policy's view, with what is queued and each definition's models."""
        Path(f"{self.root}/bank/policy.yaml").write_text(
            "version: 1\nquotas:\n  - {name: three, shows: [xyz], max_jobs: 3, reason: Testing}\n")
        self.farm.policy.refresh(force=True)
        self.farm.submit("chord_pbr", {"input": "/rdo/shows/xyz/_asset/rockA/cg/textures/src"}, "ann")
        v = self.farm.policy_view()
        self.assertEqual((v["enabled"], v["error"], [q["name"] for q in v["in_force"]["quotas"]]),
                         (True, None, ["three"]))
        self.assertEqual(v["quotas_now"], {"three": [{"for": "xyz", "jobs": 1, "frames": 1}]})
        models = {d["name"]: d["models"] for d in v["definitions"]}
        self.assertEqual(models["chord_pbr"][0]["file"], "chord_v1.safetensors")
        self.assertIsNone(models["wan22_i2i"])                              # doesn't list them

    def test_what_is_queued_one_entry_per_submission(self):
        info = {"show": "xyz"}
        self.store.insert({"id": "j1", "workflow": "wan22_i2i", "user": "ann", "status": "running",
                           "created_at": 1, "info": {**info, "input": {"frame_count": 48}}})
        self.store.insert({"id": "s1", "kind": "set", "workflow": "chord_pbr", "user": "bob", "status": "running",
                           "created_at": 2, "info": info})
        for bid, status, tiles in (("b1", "done", 4), ("b2", "running", 4), ("b3", "queued", 2)):
            self.store.insert({"id": bid, "kind": "batch", "parent_id": "s1", "workflow": "chord_pbr", "user": "bob",
                               "status": status, "created_at": 3, "info": {**info, "batch": {"tiles": tiles}}})
        self.store.insert({"id": "j2", "workflow": "wan22_i2i", "user": "ann", "status": "done", "created_at": 4,
                           "info": info})
        self.assertEqual(sorted(self.farm.queued_now(), key=lambda q: q["user"]),
                         [{"show": "xyz", "user": "ann", "frames": 48}, {"show": "xyz", "user": "bob", "frames": 6}])


if __name__ == "__main__":
    unittest.main()
