"""The providers' bills, read into the spend ledger (decision 0061): a gateway asked once for
each read wanted, for the time back to the oldest row not settled by a bill; each row the
bill charges billed at what it charged; an unsure row placed with a charge for its endpoint
and time; a row the bill should show by now and doesn't billed $0, or released; a bill that
couldn't be read changing nothing; the nightly time; top-ups; the Spend tab's view and CSV;
the alerts; and `comfyfarm spend`.

The gateway reading fal.ai's stand-in is in test_api_runner; across processes in
test_e2e_gateway.

    python -m unittest tests.test_bills -v
"""

import argparse
import contextlib
import io
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from comfyfarm.manager import bills as BL
from comfyfarm.protocol import now

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gateway import GATEWAY   # noqa: E402
from test_ledger import SENT, Ledger   # noqa: E402

ENDPOINT = SENT["endpoint"]
DAY = 86400


class Bills(Ledger):
    def ended(self, state="charged", request_id=None, ago_days=0.0) -> dict:
        """A paid job that ended `ago_days` ago, its money `state`."""
        job = self.submit()
        self.claim()
        jid = job["id"]
        if state == "charged":
            sent = {**SENT, "request_id": request_id or f"req-{jid[:8]}"}
            self.report(jid, "dispatched", at=now(), prompt_id=self.store.get(jid)["prompt_id"], submitted=sent)
            self.report(jid, "running", at=now())
            self.report(jid, "done", outputs={}, seconds=2)
        elif state == "unsure":
            self.report(jid, "failed", error="answer lost", submitted={**SENT, "request_id": None})
        elif state != "held":
            raise ValueError(state)
        if ago_days:
            back = ago_days * DAY
            self.store._db.execute("UPDATE spend SET created_at=created_at-?, settled_at=settled_at-? WHERE job_id=?",
                                   (back + 60, back, jid))
        return self.spend(jid)

    def bill(self, *charges, since=None, until=None, account="peter-pilot", error=None, balance=None) -> dict:
        t = now()
        msg = {"name": GATEWAY, "provider": "fal", "account": account, "since": since or t - 5 * DAY,
               "until": until or t, "stand_in": False, "balance": balance, "error": error,
               "events": [{"request_id": c[0], "endpoint": c[3] if len(c) > 3 else ENDPOINT, "at": c[2] or t,
                           "quantity": 1, "unit": "image", "unit_price": c[1], "cost": c[1]} for c in charges]}
        said = self.desk.bill(msg)
        self.assertTrue(said["ok"], said)
        return said["read"]


class TheBillApplied(Bills):
    def test_a_charged_job_is_billed_at_what_the_bill_says(self):
        s = self.ended()
        at = now()
        read = self.bill((s["request_id"], 0.075, at))
        row = self.spend(s["job_id"])
        self.assertEqual((row["state"], row["actual"], row["amount"]), ("billed", 0.075, 0.075))
        self.assertAlmostEqual(self.store.spent("rnd"), 0.075)
        self.assertEqual((read["events"], read["matched"], read["zeroed"], read["released"]), (1, 1, 0, 0))
        again = self.bill((s["request_id"], 0.075, at))                    # read again: the same charge
        self.assertEqual(again["matched"], 0)
        self.assertEqual(self.spend(s["job_id"])["actual"], 0.075)

    def test_two_charges_for_one_request_add_up(self):
        s = self.ended()
        self.bill((s["request_id"], 0.05, now() - 10), (s["request_id"], 0.03, now() - 5))
        self.assertEqual(self.spend(s["job_id"])["actual"], 0.08)

    def test_a_running_job_is_billed_once_it_ends(self):
        job = self.submit()
        self.claim()
        jid = job["id"]
        self.report(jid, "dispatched", at=now(), prompt_id=self.store.get(jid)["prompt_id"],
                    submitted={**SENT, "request_id": "req-run"})
        self.bill(("req-run", 0.07, None))
        self.assertEqual(self.spend(jid)["state"], "held")                 # the job hasn't ended
        self.report(jid, "running", at=now())
        self.report(jid, "done", outputs={}, seconds=2)
        self.assertEqual(self.spend(jid)["state"], "charged")
        self.bill()                                                       # the next read places it
        self.assertEqual((self.spend(jid)["state"], self.spend(jid)["actual"]), ("billed", 0.07))

    def test_an_unsure_job_takes_a_charge_it_can_be_placed_with(self):
        s = self.ended("unsure")
        t = now()
        self.bill(("other-endpoint", 0.5, t, "fal-ai/flux/dev"), ("too-late", 0.06, t + 2 * 3600))   # neither fits
        self.assertEqual(self.spend(s["job_id"])["state"], "unsure")
        self.bill(("its-own", 0.08, t))
        row = self.spend(s["job_id"])
        self.assertEqual((row["state"], row["request_id"], row["actual"]), ("billed", "its-own", 0.08))
        self.assertEqual(self.store.bill_totals("fal", "peter-pilot")["other_charges"], 2)

    def test_what_the_bill_should_show_and_doesnt(self):
        charged, unsure = self.ended(ago_days=3), self.ended("unsure", ago_days=3)
        recent = self.ended()                                              # inside the 48 hours' grace
        before = self.ended(ago_days=10)                                   # queued before the time read
        other = self.ended(ago_days=3)
        self.store._db.execute("UPDATE spend SET account='rodeo' WHERE job_id=?", (other["job_id"],))
        read = self.bill(since=now() - 5 * DAY)
        self.assertEqual((read["zeroed"], read["released"]), (1, 1))
        self.assertEqual([(self.spend(r["job_id"])["state"], self.spend(r["job_id"])["amount"])
                          for r in (charged, unsure, recent, before, other)],
                         [("billed", 0.0), ("released", 0.0), ("charged", 0.08), ("charged", 0.08), ("charged", 0.08)])
        self.assertIsNotNone(self.spend(unsure["job_id"])["billed_at"])

    def test_a_late_charge_still_bills_it(self):
        s = self.ended(ago_days=3)
        self.bill(since=now() - 5 * DAY)
        self.assertEqual(self.spend(s["job_id"])["actual"], 0.0)          # billed $0: not on the bill
        self.bill((s["request_id"], 0.08, now() - DAY))                    # then on it, a day late
        self.assertEqual((self.spend(s["job_id"])["state"], self.spend(s["job_id"])["amount"]), ("billed", 0.08))

    def test_a_bill_that_couldnt_be_read_changes_nothing(self):
        s = self.ended(ago_days=3)
        read = self.bill(error="fal.ai refused the gateway's key while reading its bill (403)")
        self.assertEqual(read["error"], "fal.ai refused the gateway's key while reading its bill (403)")
        self.assertEqual(self.spend(s["job_id"])["state"], "charged")
        self.assertEqual(self.store.bill_reads("fal", 1)[0]["id"], read["id"])

    def test_what_it_takes_from_a_gateway(self):
        self.assertEqual(self.desk.bill({"name": GATEWAY, "provider": "comfyui-local", "since": 0, "until": 1}),
                         {"ok": False, "error": "no outside provider 'comfyui-local'"})
        self.assertFalse(self.desk.bill({"name": GATEWAY, "provider": "fal"})["ok"])
        self.assertEqual(self.desk.bill({"name": "nobody", "provider": "fal"}), {"ok": False, "hello": True})
        said = self.desk.bill({"name": GATEWAY, "provider": "fal", "account": "peter-pilot", "since": 0,
                               "until": now(), "events": [{"request_id": "r1", "at": now(), "cost": 0.1},
                                                          {"request_id": "r2", "at": "x", "cost": 0.1},
                                                          {"request_id": "r3", "at": now(), "cost": -1}, "junk"]})
        self.assertEqual(said["read"]["events"], 1)


class Asking(Bills):
    def test_a_gateway_is_asked_once_for_each_read_wanted(self):
        a = self.desk.agents[GATEWAY]
        self.assertIsNone(self.desk.bills.instructions(a))
        self.assertEqual(self.desk.bills.want("a test"), [GATEWAY])
        ask = self.desk._instructions(a, {})["bill"]
        self.assertAlmostEqual(ask["until"], now(), delta=5)
        self.assertIsNone(self.desk.bills.instructions(a))                 # once
        self.up("wrk59467w-5090")
        self.assertIsNone(self.desk.bills.instructions(self.desk.agents["wrk59467w-5090"]))   # no gateway
        self.desk.bills.want("again")
        self.assertIsNotNone(self.desk.bills.instructions(a))

    def test_two_reads_wanted_in_the_same_tick(self):
        """Windows' clock can give two asks the same time: each is still asked for."""
        a = self.desk.agents[GATEWAY]
        with mock.patch("comfyfarm.manager.bills.now", return_value=1795582800.0):
            self.desk.bills.want("one")
            self.assertIsNotNone(self.desk.bills.instructions(a))
            self.desk.bills.want("two")
            self.assertIsNotNone(self.desk.bills.instructions(a))
            self.assertIsNone(self.desk.bills.instructions(a))

    def test_the_time_read(self):
        t = now()
        since, until = self.desk.bills.window(["fal"])
        self.assertAlmostEqual(since, t - DAY, delta=5)                    # a day, at least
        s = self.ended(ago_days=3)
        since, _ = self.desk.bills.window(["fal"])
        self.assertAlmostEqual(since, s["created_at"] - 3600, delta=5)     # back to the oldest not billed
        self.ended(ago_days=200)
        self.assertAlmostEqual(self.desk.bills.window(["fal"])[0], t - 89 * DAY, delta=5)    # 90 days at most

    def test_the_nightly_time(self):
        b = self.desk.bills
        b.cfg.at = "05:00"
        day = datetime(2026, 11, 24)
        at = datetime(2026, 11, 24, 5).timestamp()
        self.assertEqual(b.next_run(datetime(2026, 11, 24, 4).timestamp()), at)
        late = datetime(2026, 11, 24, 9).timestamp()
        self.assertEqual(b.next_run(late), late)                           # today's was missed: now
        b.wanted_at = datetime(2026, 11, 24, 5, 1).timestamp()
        self.assertEqual(b.next_run(late), datetime(day.year, day.month, 25, 5).timestamp())


class TopUps(Bills):
    def test_recorded_on_the_account_the_ledger_knows(self):
        with self.assertRaisesRegex(BL.BillError, r"name the fal.ai account the money went on \(--account"):
            self.desk.bills.topup("fal", 50, None, None, "peter")
        self.ended()
        rec = self.desk.bills.topup("fal", 50, None, "IT, PO 1234", "peter")
        self.assertEqual((rec["provider"], rec["account"], rec["amount"], rec["note"], rec["by"]),
                         ("fal", "peter-pilot", 50.0, "IT, PO 1234", "peter"))
        with self.assertRaisesRegex(BL.BillError, "comfyui-local isn't an outside provider"):
            self.desk.bills.topup("comfyui-local", 50, "x", None, "peter")
        with self.assertRaisesRegex(BL.BillError, "a top-up is an amount"):
            self.desk.bills.topup("fal", 0, "peter-pilot", None, "peter")
        a = {x["account"]: x for x in self.desk.bills.accounts()}["peter-pilot"]
        self.assertEqual((a["topped_up"], a["spent"], a["label"]), (50.0, 0.08, "fal.ai"))


class TheSpendTab(Bills):
    def test_its_view(self):
        s = self.ended()
        self.bill((s["request_id"], 0.075, None), balance={"amount": 41.2, "currency": "USD"})
        v = self.desk.bills.view()
        self.assertEqual(set(v), {"enabled", "reading", "shows", "accounts", "rows", "topups", "reads"})
        self.assertTrue(v["enabled"])
        self.assertEqual((v["reading"]["nightly"], v["reading"]["at"]), (False, "05:00"))
        a = v["accounts"][0]
        self.assertEqual((a["account"], a["balance"], a["bill"]["farm"], a["bill"]["other"]),
                         ("peter-pilot", 41.2, 0.075, 0.0))
        self.assertEqual(v["rows"][0]["job_name"], self.store.get(s["job_id"])["name"])
        self.assertEqual(v["shows"][0]["billed"], 0.075)
        self.assertEqual(self.desk.bills.view(show="bri3")["rows"], [])

    def test_the_csv(self):
        s = self.ended()
        text = BL.csv_text(self.store.spend_rows())
        head, row = text.splitlines()
        self.assertEqual(head, ",".join(BL.CSV_COLUMNS))
        self.assertIn(f",rnd,ann,{s['job_id']},", row)
        self.assertIn(",charged,", row)

    def test_dates(self):
        t0, t1 = BL.days("2026-11-01", "2026-11-24")
        self.assertEqual((t0, t1), (datetime(2026, 11, 1).timestamp(), datetime(2026, 11, 25).timestamp()))
        self.assertEqual(BL.days(None, None), (None, None))
        with self.assertRaisesRegex(ValueError, "until is a date like 2026-11-24, not '24/11'"):
            BL.days(None, "24/11")


class Alerting(Bills):
    def test_a_bill_not_read_and_a_low_balance(self):
        self.bill(error="no billing key")
        said = self.desk.alerts.conditions(now())
        self.assertIn("couldn't read fal.ai's account peter-pilot's bill", said["bill:fal:peter-pilot"][2])
        self.desk.cfg.ledger.balance_below = 10
        self.bill(balance={"amount": 8.2, "currency": "USD"})
        said = self.desk.alerts.conditions(now())
        self.assertNotIn("bill:fal:peter-pilot", said)                    # read since
        self.assertEqual(said["balance:fal:peter-pilot"][2],
                         "fal.ai's account peter-pilot has $8.20 left, below ledger.balance_below ($10.00): top it "
                         "up before its jobs are refused there, then record it on the manager's host: comfyfarm "
                         "spend topup fal <amount>.")
        self.desk.cfg.alerts.bills = False
        self.assertEqual(self.desk.alerts.conditions(now()), {})


class TheCommand(Bills):
    """`comfyfarm spend`, with the manager's answers as it would give them."""

    def run_it(self, action, answers, **kw):
        from comfyfarm import cli
        args = argparse.Namespace(action=action, provider=kw.get("provider"), amount=kw.get("amount"),
                                  account=kw.get("account"), note=kw.get("note"))
        calls = []

        def call(cfg, method, path, body=None):
            calls.append((method, path, body))
            return answers(method, path, body)
        out = io.StringIO()
        with mock.patch.object(cli, "_host_call", call), contextlib.redirect_stdout(out):
            code = cli.run_spend(self.cfg, args, wait=1, poll=0.01)
        return code, out.getvalue(), calls

    def test_show(self):
        s = self.ended()
        self.bill((s["request_id"], 0.075, None), balance={"amount": 41.2, "currency": "USD"})
        code, out, _ = self.run_it("show", lambda *a: self.desk.bills.view(limit=1))
        self.assertEqual(code, 0)
        self.assertIn("  rnd: $0.075 of $50.00 (1 job(s); billed $0.075, charged at estimates $0.00", out)
        self.assertIn("  fal.ai (peter-pilot): topped up $0.00, the farm's jobs $0.075, balance $41.20", out)
        self.assertIn("Bills: read when asked (ledger.nightly is off): comfyfarm spend reconcile", out)

    def test_topup_and_reconcile(self):
        code, out, calls = self.run_it("topup", lambda *a: {"provider": "fal", "account": "peter-pilot",
                                                            "amount": 50.0, "note": "IT"},
                                       provider="fal", amount=50, note="IT")
        self.assertEqual((code, out.strip()), (0, "Recorded: $50.00 on fal (peter-pilot), IT."))
        self.assertEqual(calls[0], ("POST", "/api/v1/spend/topups",
                                    {"provider": "fal", "amount": 50, "account": None, "note": "IT"}))
        self.assertEqual(self.run_it("topup", lambda *a: {})[0], 2)        # no provider or amount

        def answers(method, path, body):
            if method == "POST":
                return {"wanted_at": 100.0, "gateways": [GATEWAY]}
            return {"reads": [{"at": 101.0, "provider": "fal", "account": "peter-pilot", "stand_in": True,
                               "error": None, "events": 3, "matched": 2, "zeroed": 1, "released": 0,
                               "balance": 50.0}]}
        code, out, _ = self.run_it("reconcile", answers)
        self.assertEqual(code, 0)
        self.assertIn("fal (peter-pilot), the stand-in: 3 charge(s) read; 2 job(s) billed, 1 billed nothing, "
                      "0 released; balance $50.00", out)
        code, out, _ = self.run_it("reconcile", lambda m, p, b: {"wanted_at": 100.0, "gateways": [GATEWAY]}
                                   if m == "POST" else {"reads": []})
        self.assertEqual(code, 1)
        self.assertIn("No bill came back within 1 s", out)


class TheMessage(unittest.TestCase):
    """`bill` (protocol.py): both links carry it, and the manager takes it over HTTP."""

    def test_both_links_and_the_endpoint(self):
        try:
            from comfyfarm.agent.link import HttpLink, LocalLink
        except ImportError:
            self.skipTest("the agent needs httpx")
        for link in (LocalLink, HttpLink):
            self.assertTrue(callable(getattr(link, "bill", None)), link)
        app = (Path(__file__).resolve().parent.parent / "comfyfarm/manager/app.py").read_text(encoding="utf-8")
        self.assertIn('@app.post("/api/v1/agent/bill", include_in_schema=False, dependencies=[Depends(agent_auth)])',
                      app)
        protocol = (Path(__file__).resolve().parent.parent / "comfyfarm/protocol.py").read_text(encoding="utf-8")
        self.assertIn("  bill       the gateway, as asked:", protocol)


if __name__ == "__main__":
    unittest.main()
