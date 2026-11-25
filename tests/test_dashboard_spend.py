"""What jobs at outside providers cost on the dashboard (static/spend.js, decisions 0059 to
0061): the money in a job's Queue row and details, the question asked before a job of
budgets.confirm_above or more is queued, each show's spending on the Policy tab, the Spend
tab's rows; and the page using them.

The functions touch no page, so node runs them alone. Skipped where node isn't installed;
GitHub's runners have it."""

from __future__ import annotations

import json
import os
import re
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

    def test_what_the_bill_said(self):
        billed, nothing, gone = self.run_js(
            ["rowState", [{"state": "billed", "actual": 0.075}]], ["rowState", [{"state": "billed", "actual": 0}]],
            ["rowState", [{"state": "released", "billed_at": 1.0}]])
        self.assertEqual(billed, "Billed $0.075 on the provider's bill.")
        self.assertEqual(nothing, "Billed $0.00: the provider's bill charges nothing for it.")
        self.assertEqual(gone, "Released: the provider's bill shows no request of it.")
        line, = self.run_js(["spendLine", [{"info": {"cost": COST}, "spend": {"state": "billed", "actual": 0.12}}]])
        self.assertEqual(line, COST["text"] + " Billed $0.12 on the provider's bill.")

    def test_the_spend_tab(self):
        t = 1795582800          # Wed 25 Nov 2026 05:00 UTC; node runs in UTC
        show, full = self.run_js(
            ["showCells", [{"show": "rnd", "cap": 50, "amount": 41.2, "billed": 40, "charged": 1, "unsure": 0.08,
                            "held": 0.12, "warn_at": 0.8}]],
            ["showCells", [{"show": "ret2", "cap": 5, "amount": 5, "warn_at": 0.8}]])
        self.assertEqual(show, {"show": "rnd", "cap": "$50.00", "amount": "$41.20", "billed": "$40.00",
                                "charged": "$1.00", "unsure": "$0.08", "held": "$0.12", "left": "$8.80",
                                "cls": "warn"})
        self.assertEqual((full["left"], full["cls"]), ("$0.00", "err"))
        read = {"at": t, "stand_in": 1, "error": None, "events": 3, "matched": 2, "zeroed": 1, "released": 0}
        ok, bad, never = self.run_js(["readText", [read]], ["readText", [{**read, "error": "no billing key"}]],
                                     ["readText", [None]])
        self.assertEqual(ok, "25 Nov 05:00 (the stand-in's bill, which takes no money): 3 charges; 2 jobs billed, "
                             "1 billed nothing, 0 released")
        self.assertEqual((bad, never), ("25 Nov 05:00: not read: no billing key", "not read yet"))
        acct, = self.run_js(["accountCells", [{"label": "fal.ai", "account": "peter-pilot", "balance": 41.2,
                                               "balance_at": t, "stand_in": False, "topped_up": 50, "spent": 8.8,
                                               "bill": {"other": 0.5, "other_charges": 2}, "last_read": read}]])
        self.assertEqual({k: acct[k] for k in ("account", "balance", "topped", "spent", "other", "cls")},
                         {"account": "fal.ai · peter-pilot", "balance": "$41.20", "topped": "$50.00",
                          "spent": "$8.80", "other": "$0.50 (2 charges no job of the farm's is for)", "cls": ""})
        row, = self.run_js(["ledgerCells", [{"created_at": t, "show": "rnd", "job_id": "abcdef1234", "job_name": None,
                                             "user": "ann", "estimate": 0.08, "actual": None, "amount": 0.08,
                                             "state": "charged"}]])
        self.assertEqual((row["when"], row["job"], row["estimate"], row["billed"]), ("25 Nov 05:00", "abcdef12",
                                                                                    "$0.08", ""))
        href, every = self.run_js(["csvHref", ["rnd", "2026-11-01", ""]], ["csvHref", ["", "", ""]])
        self.assertEqual((href, every), ("/api/v1/spend?format=csv&show=rnd&since=2026-11-01",
                                         "/api/v1/spend?format=csv"))
        note, = self.run_js(["readingNote", [{"reading": {"nightly": True, "at": "05:00", "grace_hours": 48}}]])
        self.assertTrue(note.startswith("The providers' bills are read each night at 05:00 (ledger.at): comfyfarm "
                                        "spend reconcile"), note)

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


def page_function(name: str) -> str:
    """One of index.html's own functions that touch no page, as a script node can run."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    start = re.search(rf"\n(async )?function {name}\(", html).start() + 1
    return html[start:html.index("\n}\n", start) + 3]


@unittest.skipUnless(NODE, "node isn't installed")
class ThePreview(unittest.TestCase):
    """The Submit tab's preview (where it writes, what it costs) and the fields it waits for."""
    WF = {"params": [{"name": "input", "type": "sequence", "primary": True, "required": True},
                     {"name": "prompt", "type": "text", "label": "Prompt", "required": True},
                     {"name": "resolution", "type": "choice", "label": "Resolution", "choices": ["1K", "2K"]},
                     {"name": "output_path", "type": "output_folder"}]}

    def test_it_waits_for_a_required_field(self):
        """Nano Banana 2's prompt is required: the preview said "Prompt is required." while it was
        being typed (the pilot, 24 November). It now says what to fill in, and asks nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "preview.js"
            script.write_text(page_function("previewWaitsFor"), encoding="utf-8")
            harness, data = Path(tmp) / "harness.js", Path(tmp) / "cases.json"
            harness.write_text(HARNESS, encoding="utf-8")
            ref = {"name": "ref", "type": "sequence", "label": "Reference", "required": True}
            two = {"params": self.WF["params"] + [ref]}
            data.write_text(json.dumps([["previewWaitsFor", [self.WF, {"input": "/x/test.png"}]],
                                        ["previewWaitsFor", [self.WF, {"input": "/x/test.png", "prompt": " \n "}]],
                                        ["previewWaitsFor", [self.WF, {"input": "/x/test.png", "prompt": "a face"}]],
                                        ["previewWaitsFor", [self.WF, {}]],
                                        ["previewWaitsFor", [two, {"input": "/x/test.png"}]]]), encoding="utf-8")
            out = subprocess.run([NODE, str(harness), str(script), str(data)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        fill = "Fill in Prompt to check the job and see where it writes."
        self.assertEqual(json.loads(out.stdout), [fill, fill, "", fill, "Fill in Prompt and Reference to check the "
                                                                         "job and see where it writes."])

    def test_it_sends_every_field_and_follows_each(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        update = page_function("updateDest")
        self.assertIn("const params = formParams(wf), waits = previewWaitsFor(wf, params);", update)
        self.assertLess(update.index("waits"), update.index('api("/api/v1/plan"'))
        self.assertIn("const params = formParams(wf);", html)                 # Submit sends the same
        self.assertIn("for (const p of wf.params)\n    $(\"#p_\" + p.name)?.addEventListener(", html)


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
        # The Spend tab: its button, its section and the elements spend.js draws.
        self.assertIn('<button data-tab="spend" id="spendTab" hidden>Spend</button>', html)
        self.assertIn('"policy", "spend"];', html)
        self.assertIn('if (tab === "spend") window.spendShown?.();', html)
        script = SCRIPT.read_text(encoding="utf-8")
        for el in ("tab-spend", "spendBanner", "spendNote", "spendShows", "spendAccounts", "spendShow", "spendSince",
                   "spendUntil", "spendCsv", "spendRows", "spendTopups"):
            self.assertIn(f'id="{el}"', html)
            self.assertIn(f'"{el}"', script.replace("#", ""))
        policy = (STATIC / "policy.js").read_text(encoding="utf-8")
        self.assertIn("renderBudgets(f?.budgets, p?.spend_now);", policy)
        self.assertIn("budgetNow(rule, spendNow)", policy)


if __name__ == "__main__":
    unittest.main()
