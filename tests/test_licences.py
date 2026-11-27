"""The repo's licences and Rodeo's licence rules (decision 0070). Every definition lists its
models with their licences, every licence is one Rodeo's rules in policy.example.yaml have
decided, and the rules do what Peter said on 26 November 2026: production shows only use
models whose licence is cleared for Rodeo, the rest run on rnd only, and LTX-2.x on no show.

    python -m unittest tests.test_licences -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from comfyfarm.definitions import DefinitionRegistry
from comfyfarm.manager import policy as PO

REPO = Path(__file__).resolve().parent.parent


class TheRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reg = DefinitionRegistry(REPO / "workflows")
        cls.defs = reg.workflows
        cls.rules = PO.parse((REPO / "policy.example.yaml").read_text(encoding="utf-8"), known=set(cls.defs))

    def refused(self, name: str, show: str) -> str | None:
        """The licence rule that refuses `name` on `show`, or None."""
        models = self.defs[name].models
        for r in self.rules.licences:
            if r.matches(show):
                if models is None:
                    return "unlisted_models" if self.rules.unlisted_models == "deny" else None
                if r.offending(models):
                    return r.name
        return None

    def test_every_definition_lists_its_models(self):
        """So a licence rule sees each one (models: [] for one that loads no weights)."""
        unlisted = sorted(n for n, w in self.defs.items() if w.models is None)
        self.assertEqual(unlisted, [])
        self.assertEqual(self.rules.unlisted_models, "deny")

    def test_every_licence_is_one_the_rules_decided(self):
        """A new licence needs a decision first: it is refused on production until a rule names it."""
        named = {x.lower() for r in self.rules.licences for x in r.licences()}
        carried = {m["licence"].lower() for w in self.defs.values() for m in w.models or []}
        cleared = {x.lower() for r in self.rules.licences if r.allow is not None for x in r.allow}
        # rnd only: not cleared, and no rule refuses them on rnd.
        rnd_only = {"ubisoft-machine-learning-license-research-only-copyleft",
                    "apache-2.0-noncommercial-training-data"}
        self.assertLessEqual(carried, named | rnd_only)
        self.assertFalse(rnd_only & cleared)

    def test_on_a_production_show_and_on_rnd(self):
        for show in ("raa", "bri3", "cold"):
            with self.subTest(show=show):
                self.assertEqual(self.refused("chord_pbr", show), "production-cleared-licences")   # research-only
                self.assertEqual(self.refused("sam3_ltx_alpha", show), "production-cleared-licences")
                for ok in ("wan22_i2i", "gpu_test", "batch_test", "nano_banana2_edit_fal", "flux2_pro_fal",
                           "birefnet_v2_fal"):
                    self.assertIsNone(self.refused(ok, show), ok)
        self.assertIsNone(self.refused("chord_pbr", "rnd"))                 # Peter: CHORD on rnd only
        for ltx in ("ltx25_alpha_gen", "ltx25_layout_render", "sam3_ltx_alpha"):
            with self.subTest(definition=ltx):
                self.assertEqual(self.refused(ltx, "rnd"), "no-licence-rodeo-lacks")        # dropped: no show
                self.assertEqual(self.refused(ltx, ""), "production-cleared-licences")      # a show not known

    def test_a_refusal_says_why(self):
        """What the artist reads, through the policy as the manager reads it from the bank."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(REPO / "policy.example.yaml", tmp / "policy.yaml")
        p = PO.Policy(True, str(tmp / "policy.yaml"), tmp / "policy.json", lambda: set(self.defs))
        with self.assertRaises(PO.Refused) as e:
            p.check("chord_pbr", "raa", self.defs["chord_pbr"].models)
        self.assertEqual(str(e.exception), "chord_pbr can't run for show raa: it uses chord_v1.safetensors, licensed "
                                           "ubisoft-machine-learning-license-research-only-copyleft. Production "
                                           "shows use only models whose licence is cleared for Rodeo; the others "
                                           "run on rnd only. (Policy rule production-cleared-licences.) Ask the CG "
                                           "supervisors (#comfyfarm) if this is wrong.")
        p.check("chord_pbr", "rnd", self.defs["chord_pbr"].models)
        with self.assertRaisesRegex(PO.Refused, "LTX-2.x needs a paid licence from Lightricks"):
            p.check("ltx25_alpha_gen", "rnd", self.defs["ltx25_alpha_gen"].models)


if __name__ == "__main__":
    unittest.main()
