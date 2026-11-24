"""The dashboard's Policy tab (static/policy.js, decision 0047): what the banner, each rule,
each quota's use and each definition's models say, from data shaped as GET /api/v1/policy
gives it; and the tab's panels in the page.

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
from test_dashboard_envs import HARNESS, NODE, STATIC, Panels   # noqa: E402

SCRIPT = STATIC / "policy.js"
QUOTA = {"name": "guem-share", "shows": ["guem*"], "except": [], "per": "show", "max_jobs": 3, "max_frames": 1000,
         "reason": "r"}


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

    def test_the_banner(self):
        file = "/rdo/bank/comfy_farm/policy.yaml"
        ok, off, never, broken = self.run_js(
            ["policyBanner", [{"enabled": True, "file": file, "error": None, "in_force": {"shows": []}}]],
            ["policyBanner", [{"enabled": False, "file": file, "error": None, "in_force": None}]],
            ["policyBanner", [{"enabled": True, "file": file, "error": "isn't taken: x.", "in_force": None}]],
            ["policyBanner", [{"enabled": True, "file": file, "error": f"{file} isn't taken: shows.0.deny: deny is "
                               "all, or a list. The last valid version stays in force.", "in_force": {}}]])
        self.assertIsNone(ok)
        self.assertEqual(off["cls"], "muted")
        self.assertEqual(never, {"cls": "err", "text": f"No valid policy has been read, so no job can be planned "
                                                      f"until {file} is fixed. isn't taken: x."})
        self.assertEqual(broken["cls"], "warn")
        self.assertIn("The last valid version stays in force.", broken["text"])

    def test_where_the_rules_came_from(self):
        fresh, kept = self.run_js(
            ["policyNote", [{"file": "/b/policy.yaml",
                             "in_force": {"taken_at": "2026-11-22T21:00:00-05:00", "fingerprint": "0123456789abcdef",
                                          "source": "file"}}]],
            ["policyNote", [{"file": "/b/policy.yaml", "in_force": {"taken_at": "t", "fingerprint": "ab",
                                                                     "source": "saved copy"}}]])
        self.assertEqual(fresh, "From /b/policy.yaml, taken 2026-11-22T21:00:00-05:00 (01234567)")
        self.assertIn("from the copy the manager kept", kept)

    def test_what_each_rule_does(self):
        said = self.run_js(
            ["ruleShows", [{"shows": ["guem2609", "ret*"], "except": []}]],
            ["ruleShows", [{"shows": ["*"], "except": ["rnd*"]}]],
            ["ruleDoes", [{"deny": "all"}, "show"]],
            ["ruleDoes", [{"deny": ["wan22_i2i"]}, "show"]],
            ["ruleDoes", [{"allow": ["ltx25_alpha_gen", "chord_pbr"]}, "show"]],
            ["ruleDoes", [{"deny": ["cc-by-nc-4.0"]}, "licence"]],
            ["ruleDoes", [{"allow": ["apache-2.0"]}, "licence"]],
            ["ruleDoes", [QUOTA, "quota"]],
            ["ruleDoes", [{"per": "user", "max_jobs": 1}, "quota"]],
            ["ruleDoes", [{"per": "all", "max_frames": 500}, "quota"]],
            ["ruleDoes", [{"allow": ["fal"]}, "provider"]],
            ["ruleDoes", [{"deny": ["python-local", "fal"]}, "provider"]])
        self.assertEqual(said, [
            "guem2609, ret*", "every show but rnd*", "Refuses every definition", "Refuses wan22_i2i",
            "Only ltx25_alpha_gen, chord_pbr", "Refuses licences cc-by-nc-4.0", "Only licences apache-2.0",
            "At most 3 jobs and 1000 frames queued or running, for each show",
            "At most 1 job queued or running, for each user, over these shows",
            "At most 500 frames queued or running, for these shows together",
            "May send work to fal", "May not use python-local, fal"])

    def test_what_is_queued_against_a_quota(self):
        empty, two, together = self.run_js(
            ["quotaNow", [QUOTA, []]],
            ["quotaNow", [QUOTA, [{"for": "guem2609", "jobs": 3, "frames": 400},
                                  {"for": "guem2610", "jobs": 1, "frames": 1000}]]],
            ["quotaNow", [{"max_jobs": 5}, [{"for": "together", "jobs": 2, "frames": 9}]]])
        self.assertEqual(empty, [{"text": "nothing queued", "full": False}])
        self.assertEqual(two, [{"text": "guem2609: 3 of 3 jobs, 400 of 1000 frames", "full": True},
                               {"text": "guem2610: 1 of 3 jobs, 1000 of 1000 frames", "full": True}])
        self.assertEqual(together, [{"text": "2 of 5 jobs", "full": False}])

    def test_what_each_definition_lists(self):
        unlisted, none, chord = self.run_js(
            ["modelsText", [None]], ["modelsText", [[]]],
            ["modelsText", [[{"file": "chord_v1.safetensors", "licence": "ubisoft-research"}]]])
        self.assertEqual((unlisted["cls"], none["text"]), ("muted", "no model weights"))
        self.assertIn("not listed", unlisted["text"])
        self.assertEqual(chord, {"text": "chord_v1.safetensors (ubisoft-research)", "cls": ""})


class PolicyTab(unittest.TestCase):
    def test_a_tab_of_its_own_shown_when_policy_is_on(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        parser = Panels("tab-policy")
        parser.feed(html)
        self.assertEqual([(p["h2"], p["tables"]) for p in parser.panels], [
            ("Policy", 0), ("Show rules", 1), ("Licence rules", 1), ("Provider rules", 1), ("Quotas", 1),
            ("Models and licences", 1)])
        self.assertIn('<button data-tab="policy" id="policyTab" hidden>Policy</button>', html)
        self.assertIn('<script src="/policy.js"></script>', html)
        script = SCRIPT.read_text(encoding="utf-8")
        for el in ("policyTab", "policyBanner", "policyNote", "policyDefaults", "policyShows", "policyLicences",
                   "policyProviders", "policyQuotas", "policyModels"):
            self.assertIn(f'id="{el}"', html)
            self.assertIn(f'"#{el}"', script)


if __name__ == "__main__":
    unittest.main()
