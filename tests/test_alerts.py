"""Alerts (manager/alerts.py, decision 0052): each rule raised when its condition starts,
followed while it holds, cleared when it ends; the rules off; the dashboard's view, which
never gives the webhook; and posting to Google Chat, against a local stand-in. The
endpoint and the Alerts tab are in test_e2e and test_dashboard_alerts.

    python -m unittest tests.test_alerts -v
"""

import asyncio
import contextlib
import io
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.definitions import DefinitionRegistry
from comfyfarm.manager import alerts as AL
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.desk import AgentState, Desk
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.protocol import now

REPO = Path(__file__).resolve().parent.parent


class Rules(unittest.TestCase):
    def setUp(self, **alerts):
        self.tmp = tempfile.mkdtemp()
        root = self.tmp.replace("\\", "/")
        self.cfg = Config.model_validate({"database": f"{root}/data/farm.db", "workers": [],
                                          "share": {"url": "http://wrk1125l.rodeofx.com:9090"},
                                          "alerts": {"enabled": True, **alerts}})
        self.store = JobStore(Path(f"{root}/data/farm.db"))
        self.farm = Farm(self.cfg, self.store, DefinitionRegistry(REPO / "workflows"), EventBus())
        self.desk = Desk(self.farm)
        self.alerts = self.desk.alerts
        self.t = now()
        self.n = 0

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self, dt: float = 0):
        return self.alerts.check(self.t + dt)

    def keys(self) -> list[str]:
        return [a["key"] for a in self.store.open_alerts()]

    def machine(self, name: str, seen_ago: float) -> None:
        self.desk.agents[name] = AgentState(name=name, last_seen=self.t - seen_ago)

    def ran(self, worker: str, outcome: str, ago: float, workflow: str = "chord_pbr", error: str | None = None):
        """A finished job on `worker`, `ago` seconds ago: its job_metrics row, and the job."""
        self.n += 1
        jid = f"job{self.n:05d}abcdef"
        self.store.insert({"id": jid, "workflow": workflow, "user": "ann", "status": outcome, "worker": worker,
                           "created_at": self.t - ago - 60, "finished_at": self.t - ago, "error": error})
        self.store._db.execute("INSERT INTO job_metrics (job_id, kind, workflow, user, worker, outcome, created_at, "
                               "started_at, finished_at) VALUES (?, 'job', ?, 'ann', ?, ?, ?, ?, ?)",
                               (jid, workflow, worker, outcome, self.t - ago - 60, self.t - ago - 30, self.t - ago))
        return jid

    # ------------------------------------------------------------ machines
    def test_a_silent_machine(self):
        self.machine("wrk1125l-3090", 5)
        self.machine("wrk59467w-5090", 11 * 60)
        self.machine("tor-flame02-a6000", 3600)
        self.desk.drained["tor-flame02-a6000"] = {"by": "peter", "since": self.t}      # being worked on
        self.store.add_gpu_samples([("wrk9999l-old", 0, self.t - 7200, 1, 1, 2, 0)])   # before a restart
        raised, cleared = self.check()
        self.assertEqual([a["key"] for a in raised], ["machine_silent:wrk59467w-5090", "machine_silent:wrk9999l-old"])
        self.assertIn("wrk59467w-5090 hasn't checked in since ", raised[0]["message"])
        self.assertIn("Check that the machine is on and its agent running", raised[0]["message"])
        self.assertEqual(self.check(30), ([], []))                                     # raised once
        self.desk.agents["wrk59467w-5090"].last_seen = self.t + 60                    # back
        raised, cleared = self.check(60)
        self.assertEqual((raised, [a["key"] for a in cleared]), ([], ["machine_silent:wrk59467w-5090"]))
        self.assertEqual(self.keys(), ["machine_silent:wrk9999l-old"])

    # ------------------------------------------------------------ the queue
    def queued(self, jid: str, ago: float, workflow: str = "ltx25_alpha_gen") -> None:
        self.store.insert({"id": jid, "workflow": workflow, "user": "ann", "name": f"sh{jid}", "status": "queued",
                           "created_at": self.t - ago, "info": {"show": "unlp"}})

    def test_jobs_waiting(self):
        self.queued("aaaa1111", 2 * 3600)
        self.queued("bbbb2222", 10 * 60)                                   # not yet
        [a], _ = self.check()
        label = self.farm.registry.workflows["ltx25_alpha_gen"].label
        self.assertEqual(a["key"], "job_waiting")
        self.assertTrue(a["message"].startswith(f"A job has waited for a machine for over 60 min: shaaaa1111 "
                                                f"({label}, ann, unlp), queued since "), a["message"])
        self.assertIn("busy, drained or offline", a["message"])            # no machine says otherwise
        self.queued("cccc3333", 3 * 3600)
        self.assertEqual(self.check(30), ([], []))                         # followed, not raised again
        [held] = self.store.open_alerts()
        self.assertTrue(held["message"].startswith("2 jobs have waited for a machine for over 60 min. The oldest: "
                                                   "shcccc3333"), held["message"])
        self.farm.policy.enabled, self.farm.policy.rules = True, None      # the policy can't be read
        self.check(40)
        self.assertIn("No valid policy is in force", self.store.open_alerts()[0]["message"])
        for jid in ("aaaa1111", "cccc3333"):
            self.store.transition(jid, ("queued",), status="cancelled", finished_at=self.t)
        self.assertEqual([a["key"] for a in self.check(60)[1]], ["job_waiting"])

    # ------------------------------------------------------------ failures
    def test_failures_in_a_row(self):
        self.ran("wrk1125l-3090", "done", 900)
        self.ran("wrk1125l-3090", "failed", 800)
        self.ran("wrk1125l-3090", "cancelled", 700)                        # says nothing of the machine
        self.ran("wrk1125l-3090", "failed", 600, workflow="ltx25_alpha_gen")
        latest = self.ran("wrk1125l-3090", "failed", 500, error="CUDA error: out of memory\nTraceback ...")
        self.ran("tor-flame02-a6000", "failed", 900)
        self.ran("tor-flame02-a6000", "failed", 800)
        self.ran("tor-flame02-a6000", "done", 700)                         # the latest went
        [a], _ = self.check()
        self.assertEqual(a["key"], "failures:wrk1125l-3090")
        chord, ltx = (self.farm.registry.workflows[w].label for w in ("chord_pbr", "ltx25_alpha_gen"))
        self.assertEqual(a["message"], f"The last 3 jobs on wrk1125l-3090 failed ({chord}, {ltx}). The latest, "
                                       f"{latest[:8]}: CUDA error: out of memory. Look at them on the Queue tab; "
                                       f"if the machine is the problem, drain it on the Machines tab.")
        self.ran("wrk1125l-3090", "done", 10)
        self.assertEqual([x["key"] for x in self.check(60)[1]], ["failures:wrk1125l-3090"])

    # ------------------------------------------------------------ the sweeper, rollouts, syncs
    def test_a_sweep_with_problems(self):
        d = self.farm.sweeper.dir
        d.mkdir(parents=True)
        (d / "2026-11-24.json").write_text(json.dumps({"at": "2026-11-24T03:00:00", "problems": 0}))
        self.assertEqual(self.check(), ([], []))
        (d / "2026-11-25.json").write_text(json.dumps({"at": "2026-11-25T03:00:00", "problems": 4}))
        [a], _ = self.check(10)
        self.assertEqual(a["message"], "The retention sweep of 2026-11-25 lists 4 problem(s). Read its report "
                                       "(/api/v1/sweeps/2026-11-25?format=text, or data/sweeps/2026-11-25.json on "
                                       "the manager) before deletion is turned on.")
        (d / "2026-11-26.json").write_text(json.dumps({"at": "2026-11-26T03:00:00", "problems": 0}))
        self.assertEqual([x["key"] for x in self.check(20)[1]], ["sweep"])

    def test_a_stopped_rollout_or_sync(self):
        r = {"id": "r1", "name": "gpu-test", "release": "c817ab555ebac60a", "machines": ["tor-flame02-a6000", "x"],
             "before": {}, "step": 0, "state": "failed", "error": "tor-flame02-a6000: uv sync failed",
             "started_at": self.t}
        self.store.save_rollout(r)
        s = {"id": "s1", "runner": "comfyui", "machines": ["tor-flame02-a6000"], "step": 0, "state": "failed",
             "error": "a workflow it could run before can't run now", "started_at": self.t}
        self.store.save_sync(s)
        self.store.save_sync({**s, "id": "s0", "started_at": self.t - 9})  # an older one, its machine resumed since
        self.desk.drained["tor-flame02-a6000"] = {"by": "sync s1", "since": self.t}
        raised, _ = self.check()
        self.assertEqual([a["key"] for a in raised], ["rollout:r1", "sync:s1"])
        self.assertEqual(raised[0]["message"], "The rollout of gpu-test c817ab55 stopped at tor-flame02-a6000: "
                                               "tor-flame02-a6000: uv sync failed. On the manager's host: comfyfarm "
                                               "env rollout gpu-test --rollback (or --cancel).")
        self.assertIn("tor-flame02-a6000 stays drained: look at it, then resume it", raised[1]["message"])
        self.store.save_rollout({**r, "state": "rolled_back"})
        del self.desk.drained["tor-flame02-a6000"]                          # resumed by a person
        self.assertEqual(sorted(a["key"] for a in self.check(10)[1]), ["rollout:r1", "sync:s1"])

    # ------------------------------------------------------------ off, kept, shown
    def test_the_rules_off(self):
        self.tearDown()
        self.setUp(machine_silent_minutes=0, job_waiting_minutes=0, failures_in_a_row=0, sweep_problems=False,
                   rollout_problems=False)
        self.machine("wrk59467w-5090", 3600)
        self.queued("aaaa1111", 5 * 3600)
        for ago in (300, 200, 100):
            self.ran("wrk1125l-3090", "failed", ago)
        self.assertEqual(self.alerts.conditions(self.t), {})

    def test_the_dashboards_view_and_what_is_kept(self):
        self.machine("wrk59467w-5090", 3600)
        self.check()
        self.alerts.cfg.google_chat = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=k&token=secret"
        v = self.alerts.view()
        self.assertEqual((v["enabled"], v["google_chat"], len(v["active"]), v["cleared"]), (True, True, 1, []))
        self.assertEqual(v["rules"]["failures_in_a_row"], 3)
        self.assertNotIn("secret", json.dumps(v))                          # the webhook is never given
        self.desk.agents["wrk59467w-5090"].last_seen = self.t + 100
        self.check(100)
        self.assertEqual(len(self.alerts.view()["cleared"]), 1)
        self.alerts._pruned = 0
        self.alerts.check(self.t + 31 * 86400)                             # cleared over keep_days ago: gone
        self.assertEqual(self.store.cleared_alerts(), [])
        self.alerts.cfg.enabled = False
        self.assertEqual((self.alerts.view()["active"], self.alerts.view()["enabled"]), ([], False))
        self.assertFalse(Config().alerts.enabled)                          # off until switched on


class Chat(BaseHTTPRequestHandler):
    """A stand-in for Google Chat's webhook: keeps what it's sent, answers `status`."""
    got: list = []
    status = 200

    def do_POST(self):
        Chat.got.append((self.path, self.headers["Content-Type"],
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(Chat.status)
        self.end_headers()
        self.wfile.write(b'{"name": "spaces/x/messages/y"}' if Chat.status == 200 else b'{"error": "bad token"}')

    def log_message(self, *a):
        pass


class GoogleChat(unittest.TestCase):
    def setUp(self):
        Chat.got, Chat.status = [], 200
        self.server = HTTPServer(("127.0.0.1", 0), Chat)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/spaces/AAA/messages?key=k&token=secret"

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def test_posted(self):
        self.assertIsNone(AL.post_google_chat(self.url, "hello"))
        self.assertEqual(Chat.got, [("/v1/spaces/AAA/messages?key=k&token=secret", "application/json; charset=UTF-8",
                                     {"text": "hello"})])

    def test_refused_or_unreachable_without_the_webhook_in_the_reason(self):
        Chat.status = 401
        why = AL.post_google_chat(self.url, "hello")
        self.assertTrue(why.startswith("Google Chat answered 401"), why)
        self.assertIn("bad token", why)
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        why = AL.post_google_chat(self.url, "hello", timeout=2)
        self.assertTrue(why.startswith("can't reach Google Chat"), why)
        self.assertNotIn("secret", why)

    def test_one_message_a_check(self):
        rules = Rules()
        rules.setUp()
        try:
            rules.machine("wrk59467w-5090", 3600)
            rules.machine("tor-flame02-a6000", 3600)
            rules.alerts.cfg.google_chat = self.url
            asyncio.run(rules.alerts.post(*rules.check()))
            [(_, _, body)] = Chat.got
            lines = body["text"].splitlines()
            self.assertEqual(lines[0], "*ComfyFarm* (wrk1125l.rodeofx.com:9090)")
            self.assertTrue(lines[1].startswith("⚠️ tor-flame02-a6000 hasn't checked in since"), lines)
            self.assertTrue(lines[2].startswith("⚠️ wrk59467w-5090"), lines)
            self.assertEqual(lines[-1], "<http://wrk1125l.rodeofx.com:9090/#alerts|Alerts tab>")
            rules.desk.agents["tor-flame02-a6000"].last_seen = rules.t + 600
            asyncio.run(rules.alerts.post(*rules.check(600)))
            self.assertTrue(Chat.got[1][2]["text"].splitlines()[1].startswith("✅ Cleared after 10 min: "
                                                                             "tor-flame02-a6000 hasn't"))
            asyncio.run(rules.alerts.post(*rules.check(660)))              # nothing changed: nothing posted
            self.assertEqual(len(Chat.got), 2)
            self.assertIsNone(rules.alerts.post_error)
        finally:
            rules.tearDown()


class TestCommand(unittest.TestCase):
    """`comfyfarm alerts test`: a test message to config.yaml's webhook, or why it didn't go."""

    def run_it(self, cfg, answer):
        from comfyfarm import cli
        sent, real = [], AL.post_google_chat
        AL.post_google_chat = lambda url, text: sent.append((url, text)) or answer
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.run_alerts_test(cfg)
        finally:
            AL.post_google_chat = real
        return code, sent, out.getvalue()

    def test_posts_or_says_why_not(self):
        url = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=k&token=secret"
        cfg = Config.model_validate({"workers": [], "share": {"url": "http://farm.test:9090"},
                                     "alerts": {"google_chat": url}})
        code, [(to, text)], out = self.run_it(cfg, None)
        self.assertEqual((code, to), (0, url))
        self.assertTrue(text.startswith("*ComfyFarm* (farm.test:9090)\nA test message"), text)
        self.assertIn("alerts.enabled is off", out)
        code, _, out = self.run_it(cfg, "Google Chat answered 404 Not Found")
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] Google Chat answered 404 Not Found", out)
        self.assertNotIn("secret", out)
        code, sent, out = self.run_it(Config.model_validate({"workers": []}), None)
        self.assertEqual((code, sent), (1, []))
        self.assertIn("alerts.google_chat isn't set", out)


if __name__ == "__main__":
    unittest.main()
