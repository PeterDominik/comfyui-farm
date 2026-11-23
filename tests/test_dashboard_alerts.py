"""The dashboard's alerts (static/alerts.js, decision 0052): the header's chip, the rules as
they read, the banner, when each was raised, from data shaped as GET /api/v1/alerts gives
it; and the chip, tab and panels in the page.

The functions that decide what to show touch no page, so node runs them alone, in UTC so
the times read the same anywhere. Skipped where node isn't installed; GitHub's runners
have it."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_envs import HARNESS, NODE, STATIC, Panels   # noqa: E402

SCRIPT = STATIC / "alerts.js"
T = datetime(2026, 11, 22, 23, 10, tzinfo=timezone.utc).timestamp()     # a Sunday
RULES = {"machine_silent_minutes": 10, "job_waiting_minutes": 60, "failures_in_a_row": 3, "sweep_problems": True,
         "rollout_problems": True}


def view(**kw):
    return {"enabled": True, "check_seconds": 60, "checked_at": T, "rules": RULES, "google_chat": False,
            "post_error": None, "active": [], "cleared": [], **kw}


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

    def test_the_headers_chip(self):
        two = [{"message": "wrk59467w-5090 hasn't checked in"}, {"message": "A job has waited"}]
        said = self.run_js(["alertsChip", [view(active=two)]], ["alertsChip", [view(active=two[:1])]],
                           ["alertsChip", [view()]], ["alertsChip", [view(enabled=False, active=two)]],
                           ["alertsChip", [None]])
        self.assertEqual(said[0], {"text": "⚠ 2 alerts",
                                   "title": "wrk59467w-5090 hasn't checked in\nA job has waited"})
        self.assertEqual(said[1]["text"], "⚠ 1 alert")
        self.assertEqual(said[2:], [None, None, None])

    def test_the_rules_as_they_read(self):
        on, some, none = self.run_js(["alertsRules", [RULES]],
                                     ["alertsRules", [{**RULES, "machine_silent_minutes": 0, "sweep_problems": False}]],
                                     ["alertsRules", [{k: 0 for k in RULES}]])
        self.assertEqual(on, ["a machine (not drained) hasn't checked in for 10 min",
                              "jobs have waited for a machine for 60 min", "a machine's last 3 jobs all failed",
                              "the retention sweeper's latest report lists problems",
                              "a rollout or a ComfyUI sync has stopped, waiting for a person"])
        self.assertEqual(len(some), 3)
        self.assertEqual(none, [])

    def test_the_banner_and_the_note(self):
        off, ok, unposted = self.run_js(["alertsBanner", [view(enabled=False)]], ["alertsBanner", [view()]],
                                        ["alertsBanner", [view(post_error="Google Chat answered 401 Unauthorized")]])
        self.assertEqual(off["cls"], "muted")
        self.assertIsNone(ok)
        self.assertEqual(unposted, {"cls": "warn", "text": "The last alerts weren't posted to Google Chat: Google Chat "
                                                           "answered 401 Unauthorized. They are here and in the "
                                                           "manager's log."})
        here, chat, never = self.run_js(["alertsNote", [view(), T + 90]], ["alertsNote", [view(google_chat=True), T]],
                                        ["alertsNote", [view(checked_at=None), T]])
        self.assertEqual(here, "Checked every minute, last Sun 22 Nov 23:10 (1 min ago). Not posted anywhere else: "
                               "alerts.google_chat isn't set.")
        self.assertTrue(chat.endswith("New and cleared alerts are posted to Google Chat."), chat)
        self.assertIn("not yet since the manager started", never)

    def test_when(self):
        said = self.run_js(["alertsWhen", [T, T + 20]], ["alertsWhen", [T, T + 7200]],
                           ["alertsWhen", [T, T + 3 * 86400]],
                           ["alertsWhen", [None, T]])
        self.assertEqual(said, ["Sun 22 Nov 23:10 (just now)", "Sun 22 Nov 23:10 (2 h ago)",
                                "Sun 22 Nov 23:10 (3 days ago)", ""])


class AlertsTab(unittest.TestCase):
    def test_a_chip_on_every_tab_and_a_tab_while_alerts_are_on(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<a id="alertChip" class="chip alertchip" href="#alerts" hidden></a>', html)
        self.assertIn('<button data-tab="alerts" id="alertsTab" hidden>', html)
        self.assertIn('<script src="/alerts.js"></script>', html)
        parser = Panels("tab-alerts")
        parser.feed(html)
        self.assertEqual([(p["h2"], p["tables"]) for p in parser.panels], [("Alerts", 1), ("Cleared", 1)])
        script = SCRIPT.read_text(encoding="utf-8")
        for el in ("alertChip", "alertsTab", "alertCount", "alertsBanner", "alertsNote", "alertsRules", "alertsActive",
                   "alertsCleared"):
            self.assertIn(f'id="{el}"', html)
            self.assertIn(f'"#{el}"', script)


if __name__ == "__main__":
    unittest.main()
