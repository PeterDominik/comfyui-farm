"""The Usage tab's numbers (manager/usage.py, decision 0050): runs, GPU hours and failures
from job_metrics, per day, show, definition or machine, for one show or definition. The
endpoint is in test_e2e; the tab is in test_dashboard_usage.

    python -m unittest tests.test_usage -v
"""

import shutil
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path

from comfyfarm.definitions import DefinitionRegistry
from comfyfarm.manager import usage as US
from comfyfarm.manager.db import JobStore

REPO = Path(__file__).resolve().parent.parent
TODAY = date.today()


def noon(days_ago: int) -> float:
    """Midday, local time, that many days ago."""
    return datetime.combine(TODAY - timedelta(days=days_ago), time(12)).timestamp()


class Usage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = JobStore(Path(self.tmp) / "farm.db")
        self.registry = DefinitionRegistry(REPO / "workflows")
        self.n = 0

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ran(self, days_ago, outcome="done", workflow="chord_pbr", show="xyz", worker="wrk1125l-3090", run=600,
            units=10, wait=30, kind="job", started=True):
        """A job_metrics row: given to `worker` `wait` seconds after it was submitted, then
        `run` seconds on its GPU, finished at midday `days_ago`."""
        self.n += 1
        end = noon(days_ago)
        start = end - run if started else None
        created = (start or end) - 60 - wait
        self.store._db.execute(
            "INSERT INTO job_metrics (job_id, kind, workflow, user, show, worker, outcome, units, created_at, "
            "assigned_at, started_at, finished_at) VALUES (?, ?, ?, 'ann', ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"j{self.n}", kind, workflow, show, worker, outcome, units, created,
             created + wait if worker else None, start, end))

    def report(self, **kw):
        return US.report(self.store, self.registry, t=noon(0) + 3600, **kw)

    def test_per_day(self):
        self.ran(0, run=3600, units=24)
        self.ran(0, "failed", run=600, wait=90)
        self.ran(0, "cancelled", started=False, worker=None)               # never given to a machine
        self.ran(2, run=1800, units=6, kind="batch")                       # a set's batch: a run of its own
        self.ran(2, kind="set", run=99999)                                 # the set isn't: its batches are
        self.ran(40)                                                       # before the span
        r = self.report(days=7)
        self.assertEqual((r["by"], r["days"], r["since"]), ("day", 7, (TODAY - timedelta(days=6)).isoformat()))
        self.assertEqual([x["key"] for x in r["rows"]], [(TODAY - timedelta(days=i)).isoformat() for i in range(7)])
        today, _, two_ago = r["rows"][:3]
        self.assertEqual((today["jobs"], today["done"], today["failed"], today["cancelled"]), (3, 1, 1, 1))
        self.assertEqual(today["gpu_seconds"], 4200)                       # the failed run held the GPU too
        self.assertEqual(today["lost_seconds"], 600)
        self.assertEqual(today["frames"], 24)                              # of those done
        self.assertEqual(today["wait_seconds"], 60)                        # of those given to a machine
        self.assertEqual((two_ago["jobs"], two_ago["gpu_seconds"], two_ago["frames"]), (1, 1800, 6))
        self.assertEqual(r["rows"][1], {"key": r["rows"][1]["key"], "label": r["rows"][1]["key"], "jobs": 0,
                                        "done": 0, "failed": 0, "cancelled": 0, "gpu_seconds": 0, "lost_seconds": 0,
                                        "frames": 0, "wait_seconds": None})      # a day without runs
        self.assertEqual((r["total"]["jobs"], r["total"]["gpu_seconds"], r["total"]["failed"]), (4, 6000, 1))

    def test_per_show_definition_and_machine(self):
        self.ran(1, show="unlp", run=600)
        self.ran(1, show="unlp", workflow="ltx25_alpha_gen", worker="wrk59467w-5090", run=7200)
        self.ran(1, show=None, worker="tor-flame02-a6000", run=60)
        self.ran(3, "cancelled", show="xyz", started=False, worker=None)
        shows = self.report(by="show")["rows"]
        self.assertEqual([(x["key"], x["label"], x["jobs"]) for x in shows],
                         [("unlp", "unlp", 2), ("", "no show", 1), ("xyz", "xyz", 1)])   # most GPU hours first
        defs = self.report(by="workflow")["rows"]
        self.assertEqual([x["key"] for x in defs], ["ltx25_alpha_gen", "chord_pbr"])
        self.assertEqual(defs[0]["label"], self.registry.workflows["ltx25_alpha_gen"].label)
        machines = self.report(by="machine")["rows"]
        self.assertEqual([(x["key"], x["label"]) for x in machines][-1], ("", "not given to a machine"))
        self.assertEqual(machines[0]["key"], "wrk59467w-5090")

    def test_one_show_or_definition(self):
        self.ran(1, show="unlp", run=600)
        self.ran(1, show="unlp", workflow="ltx25_alpha_gen", run=7200)
        self.ran(1, show="xyz", run=60)
        self.ran(1, show=None, run=30)
        r = self.report(by="workflow", show="unlp")
        self.assertEqual([(x["key"], x["jobs"]) for x in r["rows"]], [("ltx25_alpha_gen", 1), ("chord_pbr", 1)])
        self.assertEqual(r["total"]["gpu_seconds"], 7800)
        self.assertEqual(self.report(show="")["total"]["jobs"], 1)          # jobs with no show
        self.assertEqual(self.report(workflow="chord_pbr", show="xyz")["total"]["gpu_seconds"], 60)
        self.assertEqual(r["shows"], ["", "unlp", "xyz"])                   # what the filters offer: all of them
        self.assertEqual([w["name"] for w in r["workflows"]], ["chord_pbr", "ltx25_alpha_gen"])

    def test_the_span(self):
        self.ran(1)
        r = self.report(days=1)
        self.assertEqual((len(r["rows"]), r["total"]["jobs"], r["since"]), (1, 0, TODAY.isoformat()))
        self.assertEqual(r["total"]["wait_seconds"], None)
        self.assertEqual(self.report(days=2)["total"]["jobs"], 1)
        with self.assertRaisesRegex(ValueError, "by is one of day, show, workflow, machine"):
            self.report(by="user")


if __name__ == "__main__":
    unittest.main()
