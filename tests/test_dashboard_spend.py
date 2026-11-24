"""What jobs at outside providers cost on the dashboard (static/spend.js, decisions 0059,
0060): the money in a job's Queue row and details, the question asked before a job of
budgets.confirm_above or more is queued, each show's spending on the Policy tab; and the page
using them.

The functions touch no page, so node runs them alone. Skipped where node isn't installed;
GitHub's runners have it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_envs import HARNESS, NODE, STATIC   # noqa: E402

SCRIPT = STATIC / "spend.js"
COST = {"amount": 0.12, "text": "About $0.12 at fal.ai: 1 image at 2K, $0.08 an image, ×1.5 at 2K (fal.ai's price, "
                                "read 24 Nov 11:37).",
        "budget": {"text": "Show rnd has $49.80 left of its $50.00 cap after it."}}


def job(state=None, amount=0.12):
    j = {"id": "j1", "info": {"cost": COST}}
    if state:
        j["spend"] = {"state": state, "amount": 0.0 if state == "released" else amount, "estimate": 0.12}
    return j


@unittest.skipUnless(NODE, "node isn't installed")
class Says(unittest.TestCase):
    def run_js(self, *cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness, data = Path(tmp) / "harness.js", Path(tmp) / "cases.json"
            harness.write_text(HARNESS, encoding="utf-8")
            data.write_text(json.dumps(list(cases)), encoding="utf-8")
            out = subprocess.run([NODE, str(harness), str(SCRIPT), str(data)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60, env={**os.environ, "TZ": "UTC"})
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_money_as_the_manager_says_it(self):
        said = self.run_js(*[["money", [x]] for x in (0.08, 1.5, 0.0075, 0.1425, 41.2, 1234.5, 0)])
        self.assertEqual(said, ["$0.08", "$1.50", "$0.0075", "$0.1425", "$41.20", "$1,234.50", "$0.00"])

    def test_a_jobs_row_and_details(self):
        short = self.run_js(["spendShort", [job()]], ["spendShort", [job("held")]], ["spendShort", [job("charged")]],
                            ["spendShort", [job("unsure")]], ["spendShort", [job("released")]],
                            ["spendShort", [{"info": {}}]])
        self.assertEqual(short, ["$0.12", "$0.12", "$0.12", "$0.12?", "released", ""])
        line, unsure, none = self.run_js(["spendLine", [job("charged")]], ["spendLine", [job("unsure")]],
                                         ["spendLine", [{"info": {}}]])
        self.assertEqual(line, COST["text"] + " Charged: it reached the provider. The estimate stands until the "
                                              "provider's bill is read.")
        self.assertIn("Held: it may have reached the provider, and the farm can't tell.", unsure)
        self.assertEqual(none, "")

    def test_the_question_before_a_costly_job(self):
        q, = self.run_js(["yesQuestion", [COST]])
        self.assertEqual(q, COST["text"] + "\n\n" + COST["budget"]["text"] + "\n\nSubmit it for about $0.12?")

    def test_each_shows_spending(self):
        spend = [{"show": "rnd", "amount": 41.2, "unsure": 0.08, "jobs": 515, "cap": 50, "rule": "pilot-rnd",
                  "warn_at": 0.8},
                 {"show": "ret2", "amount": 5, "unsure": 0, "jobs": 1, "cap": 5, "rule": "pilot-rnd", "warn_at": 0.8},
                 {"show": "bri3", "amount": 0.08, "unsure": 0, "jobs": 1, "cap": 0, "rule": None, "warn_at": 0.8}]
        rule, others, nothing = self.run_js(["budgetNow", ["pilot-rnd", spend]], ["budgetNow", [None, spend]],
                                            ["budgetNow", ["pin1-none", spend]])
        self.assertEqual(rule, [{"text": "rnd: $41.20 of $50.00 (515 jobs, $0.08 of it unsure)", "cls": "warn"},
                                {"text": "ret2: $5.00 of $5.00 (1 job)", "cls": "err"}])
        self.assertEqual(others, [{"text": "bri3: $0.08 of $0.00 (1 job)", "cls": "err"}])
        self.assertEqual(nothing, [{"text": "nothing spent", "cls": "muted"}])


class ThePage(unittest.TestCase):
    def test_the_queue_and_the_submit_use_it(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<script src="/spend.js"></script>', html)
        self.assertLess(html.index('<script src="/spend.js"></script>'), html.index("function render()"))
        self.assertIn('title="${esc(spendLine(j))}">· ${esc(spendShort(j))}</span>', html)      # the row
        self.assertIn("${i.cost ? `<div>Cost</div><div>${esc(spendLine(j))}</div>` : \"\"}", html)  # the details
        # A 409 with `confirm` is asked about and sent again, from Submit and from Run again.
        self.assertIn("{ body });", html)
        self.assertIn("const job = await withYes(yes => api(\"/api/v1/jobs\"", html)
        self.assertIn("{ ...body, confirm_cost: yes }) }), confirm);", html)
        self.assertIn('const j = await withYes(yes => api(`/api/v1/jobs/${t.dataset.retry}/retry${yes == null ? "" : '
                      '"?confirm_cost=" + yes}`', html)
        policy = (STATIC / "policy.js").read_text(encoding="utf-8")
        self.assertIn("renderBudgets(f?.budgets, p?.spend_now);", policy)
        self.assertIn("budgetNow(rule, spendNow)", policy)


if __name__ == "__main__":
    unittest.main()
