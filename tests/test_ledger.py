"""The spend ledger and the caps (decision 0060): policy.yaml's budgets, a cap per show for
its life; a job that doesn't fit refused before it is queued, and racing submits never
spending more than a cap (step 4's gate); a yes asked for at budgets.confirm_above; and each
job's money held when it is queued, then charged, released or unsure when it ends, from what
its gateway says reached the provider. A paid job is never queued again once it may have
reached its provider.

What the gateway says (the API runner's submitted(), the agent's reports) is in
test_api_runner; the dashboard's side in test_dashboard_spend; across processes in
test_e2e_gateway.

    python -m unittest tests.test_ledger -v
"""

import asyncio
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from comfyfarm.manager import ledger as L
from comfyfarm.manager import policy as PO
from comfyfarm.manager.farm import FarmError
from comfyfarm.protocol import duration, now

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gateway import GATEWAY, PLATE, Manager   # noqa: E402

PROVIDERS = """version: 1
contact: the CG supervisors
providers:
  - {name: pilot-fal-on-rnd, shows: [rnd, bri3, pin1], allow: [fal], reason: The fal.ai pilot}
"""
BUDGETS = PROVIDERS + """budgets:
  confirm_above: 5
  rules:
    - name: pilot-rnd
      shows: [rnd]
      cap: CAP
      reason: Peter's fal.ai pilot, on his own account
    - name: pin1-none
      shows: [pin1]
      cap: 0
      reason: Its client hasn't agreed to outside providers
"""


def budgets(cap) -> str:
    return BUDGETS.replace("CAP", str(cap))


SENT = {"provider": "fal", "account": "peter-pilot", "endpoint": "fal-ai/nano-banana-2/edit",
        "body": {"prompt": "rain"}, "request_id": "req-0001"}


class TheBudgets(unittest.TestCase):
    """policy.yaml's budgets: section, read and checked."""

    def test_its_defaults(self):
        b = PO.parse(budgets(50)).budgets
        self.assertEqual((b.currency, b.other_shows, b.confirm_above, b.warn_at), ("USD", 0, 5, 0.8))
        self.assertEqual([(r.name, r.shows, r.cap, r.warn_at) for r in b.rules],
                         [("pilot-rnd", ["rnd"], 50, None), ("pin1-none", ["pin1"], 0, None)])
        self.assertIsNone(PO.parse(PROVIDERS).budgets)

    def test_what_is_wrong_is_said(self):
        cases = {
            "budgets.rules.0.cap: Input should be greater than or equal to 0":
                "budgets:\n  rules:\n  - {name: a, shows: [rnd], cap: -1, reason: r}\n",
            "budgets.rules.0.cap: Field required": "budgets:\n  rules:\n  - {name: a, shows: [rnd], reason: r}\n",
            "budgets.currency: Input should be 'USD'": "budgets:\n  currency: EUR\n",
            "budgets.rules.0.limit: Extra inputs are not permitted":
                "budgets:\n  rules:\n  - {name: a, shows: [rnd], cap: 5, limit: 3, reason: r}\n",
            "budgets.warn_at: Input should be less than or equal to 1": "budgets:\n  warn_at: 1.5\n",
            "the file: two rules are named pilot-fal-on-rnd: each needs its own name":
                "budgets:\n  rules:\n  - {name: pilot-fal-on-rnd, shows: [rnd], cap: 5, reason: r}\n",
        }
        for said, text in cases.items():
            with self.subTest(said=said):
                with self.assertRaises(ValueError) as e:
                    PO.parse(PROVIDERS + text)
                self.assertEqual(str(e.exception), said)

    def policy(self, text, enabled=True):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "policy.yaml").write_text(text, encoding="utf-8")
        return PO.Policy(enabled, str(d / "policy.yaml"), d / "policy.json", known=lambda: set())

    def test_each_shows_cap(self):
        p = self.policy(budgets(50))
        self.assertEqual(p.budget("rnd"), {"cap": 50, "rule": "pilot-rnd", "warn_at": 0.8, "confirm_above": 5,
                                           "reason": "Peter's fal.ai pilot, on his own account",
                                           "contact": "the CG supervisors"})
        self.assertEqual(p.budget("RND")["rule"], "pilot-rnd")
        self.assertEqual((p.budget("bri3")["cap"], p.budget("bri3")["rule"]), (0, None))      # other_shows
        self.assertEqual(p.budget("pin1")["rule"], "pin1-none")
        p = self.policy(budgets(50).replace("confirm_above: 5", "other_shows: 10\n  warn_at: 0.5"))
        self.assertEqual((p.budget("bri3")["cap"], p.budget("bri3")["warn_at"]), (10, 0.5))

    def test_no_section_or_no_policy_spends_nothing(self):
        with self.assertRaises(PO.Refused) as e:
            self.policy(PROVIDERS).budget("rnd")
        self.assertEqual(str(e.exception), "No show may spend money at outside providers yet: the farm's policy has "
                                           "no budgets: section, which gives each show its cap. (Policy: budgets.) "
                                           "Ask the CG supervisors for one.")
        with self.assertRaisesRegex(PO.Refused, r"its cap, is off \(policy.enabled"):
            self.policy(budgets(50), enabled=False).budget("rnd")


class TheRule(unittest.TestCase):
    """What a held row becomes when its job ends (ledger.settled)."""

    def test_it_never_guesses_down(self):
        cases = [  # (row, status before, status) -> state
            ({}, "running", "done", "charged"),
            ({"request_id": "r1"}, "dispatched", "failed", "charged"),
            ({"request_id": "r1", "reached": "yes"}, "running", "cancelled", "charged"),
            ({}, "queued", "cancelled", "released"),
            ({}, "queued", "failed", "released"),
            ({"reached": "no"}, "preparing", "failed", "released"),
            ({"reached": "no"}, "preparing", "cancelled", "released"),
            ({"reached": "maybe"}, "preparing", "failed", "unsure"),
            ({}, "preparing", "failed", "unsure"),          # its gateway said nothing: gone silent
            ({}, "dispatched", "cancelled", "unsure"),
        ]
        for row, before, status, state in cases:
            with self.subTest(row=row, before=before, status=status):
                self.assertEqual(L.settled(row, before, status), state)


class Ledger(Manager):
    def setUp(self, cap=50, extra=""):
        super().setUp()
        self.set_policy(budgets(cap) + extra)
        self.up(GATEWAY, lanes={"fal": 2})

    def set_policy(self, text):
        (self.tmp / "policy.yaml").write_text(text, encoding="utf-8")
        self.farm.policy.refresh(force=True)
        self.assertIsNone(self.farm.policy.error)

    def claim(self) -> list[dict]:
        return self.desk._claim(self.desk.agents[GATEWAY])

    def report(self, jid, event, **data):
        j = self.store.get(jid)
        return self.desk.report({"name": GATEWAY, "job": jid, "attempt": j["prompt_id"], "event": event,
                                 "data": data})

    def spend(self, jid) -> dict:
        return self.store.spend_of(jid)

    def jobs(self) -> int:
        return len(self.store.list(limit=1000))


class TheCap(Ledger):
    def setUp(self):
        super().setUp(cap=0.2)

    def test_the_plan_says_what_is_left(self):
        cost = self.farm.plan_summary(self.farm.plan("fal_edit", {"input": PLATE, "prompt": "rain",
                                                                  "output_path": str(self.tmp / "shows")},
                                                     show="rnd"))["cost"]
        self.assertEqual(cost["budget"], {"show": "rnd", "cap": 0.2, "spent": 0.0, "left": 0.12, "rule": "pilot-rnd",
                                          "warn_at": 0.8, "text": "Show rnd has $0.12 left of its $0.20 cap after it."})
        self.assertFalse(cost["confirm"])

    def test_a_job_over_its_cap_is_refused_before_it_is_queued(self):
        """Step 4's gate, first half."""
        a, b = self.submit(), self.submit()
        self.assertEqual([self.spend(j["id"])["state"] for j in (a, b)], ["held", "held"])
        self.assertAlmostEqual(self.store.spent("rnd"), 0.16)
        with self.assertRaises(FarmError) as e:
            self.submit()
        self.assertEqual(e.exception.status, 403)
        self.assertEqual(str(e.exception), "This job's $0.08 at fal.ai doesn't fit in show rnd's cap: $0.16 of its "
                                           "$0.20 is spent or held, so $0.04 is left. Peter's fal.ai pilot, on his own "
                                           "account. (Policy rule pilot-rnd.) The cap is raised in policy.yaml "
                                           "(budgets). Ask the CG supervisors if it should.")
        self.assertEqual(self.jobs(), 2)                                    # nothing queued
        self.farm._finish_cancel(a["id"])                                   # never left the queue: released
        self.assertEqual(self.submit(resolution="1K")["status"], "queued")
        with self.assertRaisesRegex(FarmError, r"\$0.12 at fal.ai doesn't fit"):
            self.submit(resolution="2K")

    def test_a_show_without_a_cap(self):
        with self.assertRaises(FarmError) as e:
            self.submit(show="bri3")
        self.assertEqual(str(e.exception), "Show bri3 can't spend money at outside providers: no budgets rule gives "
                                           "it a cap. (Policy: budgets.other_shows is $0.00.) Ask the CG supervisors "
                                           "if it should.")
        with self.assertRaisesRegex(FarmError, r"^Show pin1 can't spend money at outside providers: Its client "
                                               r"hasn't agreed to outside providers. \(Policy rule pin1-none.\)"):
            self.submit(show="pin1")
        self.set_policy(PROVIDERS)
        with self.assertRaisesRegex(FarmError, "the farm's policy has no budgets: section"):
            self.submit()
        self.assertEqual(self.jobs(), 0)
        self.assertEqual(self.submit("gpu_test")["status"], "queued")       # the farm's own GPUs cost nothing
        self.assertIsNone(self.store.spend_of(self.store.list()[0]["id"]))

    def test_racing_submits_never_overspend(self):
        """Step 4's gate, second half: 12 submits at once for a cap that holds 2 of them."""
        results, start = [], threading.Barrier(12)

        def one():
            start.wait()
            try:
                results.append(self.submit()["id"])
            except FarmError as e:
                results.append(e)
        threads = [threading.Thread(target=one) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        queued = [r for r in results if isinstance(r, str)]
        self.assertEqual(len(queued), 2)
        self.assertTrue(all(isinstance(r, FarmError) and r.status == 403 for r in results if r not in queued))
        self.assertAlmostEqual(self.store.spent("rnd"), 0.16)
        self.assertEqual(self.jobs(), 2)

    def test_the_cap_is_checked_where_the_job_is_queued(self):
        """The plan's check is the artist's warning; the store's, in the job's own transaction,
        is the one that holds: a refusal there leaves neither the job nor its row."""
        with mock.patch.object(self.farm.store, "spent", return_value=0.0):     # the plan sees an empty cap
            self.submit()
            self.submit()
            with self.assertRaisesRegex(FarmError, r"\$0.16 of its \$0.20 is spent or held"):
                self.submit()
        self.assertEqual(self.jobs(), 2)
        self.assertEqual(len(self.store.spend_for([j["id"] for j in self.store.list()])), 2)


class Confirming(Ledger):
    def setUp(self):
        super().setUp(cap=50)
        self.set_policy(budgets(50).replace("confirm_above: 5", "confirm_above: 0.1"))

    def test_a_job_at_confirm_above_waits_for_a_yes(self):
        self.assertEqual(self.submit()["status"], "queued")                 # $0.08: below it
        with self.assertRaises(FarmError) as e:
            self.submit(resolution="2K")
        self.assertEqual((e.exception.status, str(e.exception)),
                         (409, "This job costs about $0.12 at fal.ai, and a job of $0.10 or more is submitted once you "
                               "say yes: submit it again, confirming. (Policy: budgets.confirm_above.)"))
        cost = e.exception.extra["confirm"]
        self.assertEqual((cost["amount"], cost["confirm"], cost["budget"]["left"]), (0.12, True, 49.8))
        self.assertEqual(self.jobs(), 1)
        with self.assertRaises(FarmError):                                  # a yes to less than it costs
            self.farm.submit("fal_edit", {"input": PLATE, "prompt": "rain", "resolution": "2K",
                                          "output_path": str(self.tmp / "shows")}, "ann", show="rnd",
                             confirm_cost=0.1)
        job = self.farm.submit("fal_edit", {"input": PLATE, "prompt": "rain", "resolution": "2K",
                                            "output_path": str(self.tmp / "shows")}, "ann", show="rnd",
                               confirm_cost=0.12)
        self.assertEqual(self.spend(job["id"])["estimate"], 0.12)
        self.assertTrue(job["info"]["cost"]["confirm"])

    def test_run_again_asks_too(self):
        job = self.farm.submit("fal_edit", {"input": PLATE, "prompt": "rain", "resolution": "2K",
                                            "output_path": str(self.tmp / "shows")}, "ann", show="rnd",
                               confirm_cost=0.12)
        self.farm._finish_cancel(job["id"])
        with self.assertRaises(FarmError) as e:
            self.farm.retry(job["id"], "ann")
        self.assertIn("confirm", e.exception.extra)
        again = self.farm.retry(job["id"], "ann", confirm_cost=0.12)
        self.assertEqual(self.spend(again["id"])["state"], "held")          # a new job, a new row


class Settling(Ledger):
    """What becomes of a job's money as it ends, from what its gateway said."""

    def taken(self, **params) -> dict:
        job = self.submit(**params)
        self.assertEqual([p["job"] for p in self.claim()], [job["id"]])
        return self.store.get(job["id"])

    def test_it_never_left_the_queue(self):
        job = self.submit()
        self.run_async(self.farm.cancel(job["id"]))
        self.assertEqual((self.spend(job["id"])["state"], self.spend(job["id"])["amount"]), ("released", 0.0))
        self.assertEqual(self.store.spent("rnd"), 0.0)

    def test_charged_once_its_provider_has_it(self):
        job = self.taken()
        self.report(job["id"], "dispatched", at=now(), prompt_id=job["prompt_id"], submitted=SENT)
        s = self.spend(job["id"])
        self.assertEqual((s["state"], s["request_id"], s["account"], s["reached"]),
                         ("held", "req-0001", "peter-pilot", "yes"))
        self.report(job["id"], "running", at=now())
        self.report(job["id"], "done", outputs={}, seconds=3)
        s = self.spend(job["id"])
        self.assertEqual((s["state"], s["amount"], s["estimate"]), ("charged", 0.08, 0.08))
        self.assertAlmostEqual(self.store.spent("rnd"), 0.08)
        self.assertIsNotNone(s["settled_at"])

    def test_a_failure_after_it_was_sent_is_charged(self):
        job = self.taken()
        self.report(job["id"], "dispatched", at=now(), prompt_id=job["prompt_id"], submitted=SENT)
        self.report(job["id"], "failed", error="fal.ai couldn't run it", submitted=SENT)
        self.assertEqual(self.spend(job["id"])["state"], "charged")

    def test_a_failure_before_it_was_sent_is_released(self):
        job = self.taken()
        self.report(job["id"], "failed", error="Uploading plate.png to fal.ai failed")     # nothing left the gateway
        s = self.spend(job["id"])
        self.assertEqual((s["state"], s["reached"], s["amount"]), ("released", "no", 0.0))

    def test_a_lost_answer_is_unsure(self):
        job = self.taken()
        maybe = {**SENT, "request_id": None}
        self.report(job["id"], "failed", error="The gateway lost fal.ai's answer while sending the job", submitted=maybe)
        s = self.spend(job["id"])
        self.assertEqual((s["state"], s["reached"], s["amount"]), ("unsure", "maybe", 0.08))
        self.assertAlmostEqual(self.store.spent("rnd"), 0.08)               # held until the bill says
        self.assertEqual(self.farm.submitted(self.store.get(job["id"], with_prompt=True)), maybe)   # its API JSON

    def test_a_gateway_gone_silent_never_queues_it_again(self):
        prepared, sent = self.taken(), self.taken()
        self.report(sent["id"], "dispatched", at=now(), prompt_id=sent["prompt_id"], submitted=SENT)
        self.desk.check_leases(now() + 10 * self.desk.lease_seconds)
        for job, state in ((prepared, "unsure"), (sent, "charged")):
            j = self.store.get(job["id"])
            self.assertEqual((j["status"], self.spend(job["id"])["state"]), ("failed", state))
            self.assertEqual(j["error"], f"{GATEWAY} stopped checking in for over {self.lease_text()}. It costs money "
                                         f"at fal.ai, where it may have run, so it isn't queued again (it would be "
                                         f"paid for twice): look for it in fal.ai's request history, and submit it "
                                         f"again if it isn't there.")
        self.assertEqual(self.store.queued(), [])

    def lease_text(self):
        return duration(self.desk.lease_seconds)

    def test_a_cancel_while_it_is_prepared_goes_to_its_gateway(self):
        job = self.taken()
        j = self.run_async(self.farm.cancel(job["id"]))
        self.assertEqual((j["status"], j["stage"], j["cancel_requested"]), ("preparing", "cancelling", 1))
        held = {job["id"]: {"job": job["id"], "attempt": job["prompt_id"], "status": "preparing"}}
        self.assertEqual(self.desk._instructions(self.desk.agents[GATEWAY], held)["cancel"],
                         [{"job": job["id"], "attempt": job["prompt_id"]}])
        self.report(job["id"], "cancelled")                                 # stopped before it was sent
        self.assertEqual((self.store.get(job["id"])["status"], self.spend(job["id"])["state"]),
                         ("cancelled", "released"))
        other = self.taken()
        self.run_async(self.farm.cancel(other["id"]))
        self.report(other["id"], "dispatched", at=now(), prompt_id=other["prompt_id"], submitted=SENT)
        self.report(other["id"], "cancelled", submitted=SENT)               # it had reached fal.ai
        self.assertEqual(self.spend(other["id"])["state"], "charged")

    def test_the_farms_own_jobs_are_cancelled_at_once(self):
        self.up("wrk59467w-5090")
        job = self.submit("gpu_test")
        self.desk._claim(self.desk.agents["wrk59467w-5090"])
        self.assertEqual(self.run_async(self.farm.cancel(job["id"]))["status"], "cancelled")

    @staticmethod
    def run_async(coro):
        return asyncio.run(coro)


class Showing(Ledger):
    def test_jobs_carry_their_money(self):
        job = self.submit()
        said = []
        with mock.patch.object(self.farm.events, "publish", said.append):
            self.farm._finish_cancel(job["id"])
        self.assertEqual(said[-1]["job"]["spend"]["state"], "released")
        j = self.farm.annotate([self.store.get(job["id"])])[0]
        self.assertEqual((j["spend"]["state"], j["spend"]["estimate"]), ("released", 0.08))
        g = self.submit("gpu_test")
        self.assertNotIn("spend", self.farm.annotate([self.store.get(g["id"])])[0])

    def test_each_shows_spending_against_its_cap(self):
        self.submit()
        self.submit(resolution="2K")
        self.assertEqual(self.farm.policy_view()["spend_now"], [
            {"show": "rnd", "amount": 0.2, "held": 0.2, "charged": 0.0, "unsure": 0.0, "jobs": 2, "cap": 50,
             "rule": "pilot-rnd", "warn_at": 0.8}])

    def test_an_alert_near_the_cap_and_at_it(self):
        self.set_policy(budgets(0.2))
        self.submit()
        self.assertEqual(self.desk.alerts.conditions(now()), {})            # 40%
        self.submit()
        said = self.desk.alerts.conditions(now())
        self.assertEqual(said, {"budget:rnd": ("budget", "rnd", (
            "Show rnd has spent or holds $0.16 of its $0.20 cap at outside providers (policy rule pilot-rnd), 80%: "
            "raise the cap in policy.yaml (budgets) before it runs out, if it should go on."))})
        self.set_policy(budgets(0.16))
        self.assertIn("its jobs there are refused until the cap is raised", self.desk.alerts.conditions(now())
                      ["budget:rnd"][2])
        self.desk.cfg.alerts.budgets = False
        self.assertEqual(self.desk.alerts.conditions(now()), {})


if __name__ == "__main__":
    unittest.main()
