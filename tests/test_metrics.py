"""Job metrics (decision 0040): one small row per finished job, written where every change to
a job passes (db.JobStore._changed), kept when the job is deleted, and read as JSON or CSV
(manager/metrics.py). A set and its batches are in test_sets; a job run by an agent, with its
GPU, in test_agent; the endpoint in test_e2e.

    python -m unittest tests.test_metrics -v
"""

import csv
import io
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from comfyfarm.manager import metrics as MX
from comfyfarm.manager.db import METRICS_COLUMNS, JobStore
from comfyfarm.protocol import now

SECRET = "a red fox at dawn"                    # a prompt: never in a metrics row
PATH = "/rdo/shows/xyz/seq/abc_0010/plate.%04d.exr"


def job(jid: str = "j1", **kw) -> dict:
    return {"id": jid, "workflow": "wan22_i2i", "user": "ann", "status": "queued", "created_at": now() - 100,
            "params": {"prompt": SECRET, "input": PATH},
            "info": {"show": "xyz", "shot": "abc_0010", "input": {"frame_count": 3, "farm_path": PATH},
                     "output_dir": "/rdo/shows/xyz/out/v001"},
            "request": {"definition": "0123456789abcdef", "values": {"prompt": SECRET}}, **kw}


class Rows(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "farm.db"
        self.store = JobStore(self.path)

    def tearDown(self):
        self.store.close()                       # Windows can't delete an open database
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_job(self, jid="j1", outcome="done", attempt="a1", **fields):
        """Through the states the desk moves a job through, to `outcome`."""
        s = self.store
        s.transition(jid, ("queued",), status="preparing", worker="gpu1", gpu="NVIDIA GeForce RTX 3090",
                     prompt_id=attempt)
        s.transition(jid, ("preparing",), status="dispatched", dispatched_at=now(), attempts=fields.pop("attempts", 1))
        s.transition(jid, ("dispatched",), status="running", started_at=now())
        end = {"done": {"outputs": {"output_dir": "/rdo/shows/xyz/out/v001"}},
               "failed": {"error": f"oiiotool failed writing {PATH}"}}.get(outcome, {})
        return s.transition(jid, ("running",), status=outcome, finished_at=now(), **end, **fields)

    def test_a_finished_job_leaves_one_row(self):
        self.store.insert(job())
        self.assertEqual(self.store.job_metrics(), [])                    # not until it finishes
        self.run_job()
        [row] = self.store.job_metrics()
        self.assertEqual(set(row), set(METRICS_COLUMNS))
        self.assertEqual({k: row[k] for k in ("job_id", "kind", "parent_id", "workflow", "fingerprint", "user", "show",
                                              "worker", "gpu", "outcome", "attempts", "units")},
                         {"job_id": "j1", "kind": "job", "parent_id": None, "workflow": "wan22_i2i",
                          "fingerprint": "0123456789abcdef", "user": "ann", "show": "xyz", "worker": "gpu1",
                          "gpu": "NVIDIA GeForce RTX 3090", "outcome": "done", "attempts": 1, "units": 3})
        times = [row[k] for k in ("created_at", "assigned_at", "dispatched_at", "started_at", "finished_at")]
        self.assertEqual(times, sorted(times))
        self.assertIsNotNone(row["assigned_at"])

    def test_no_params_prompts_paths_or_errors(self):
        """Decision 0006: kept after the job, so nothing in it an artist typed or a path."""
        self.store.insert(job("j1"))
        self.run_job("j1", "failed")
        [row] = self.store.job_metrics()
        self.assertEqual(row["outcome"], "failed")
        text = " ".join(str(v) for v in row.values())
        for word in (SECRET, "/rdo", "oiiotool", "abc_0010"):
            self.assertNotIn(word, text)

    def test_cancelled_before_it_ran(self):
        self.store.insert(job())
        self.store.transition("j1", ("queued",), status="cancelled", finished_at=now(), cancel_requested=1)
        [row] = self.store.job_metrics()
        self.assertEqual((row["outcome"], row["worker"], row["gpu"], row["assigned_at"], row["started_at"]),
                         ("cancelled", None, None, None, None))

    def test_a_job_that_finishes_again_replaces_its_row(self):
        self.store.insert(job())
        self.run_job(outcome="failed")
        self.store.transition("j1", ("failed",), status="queued", error=None, finished_at=None)      # retried
        self.assertEqual(self.store.job_metrics()[0]["outcome"], "failed")       # until it finishes again
        self.run_job(outcome="done", attempt="a2", attempts=2)
        [row] = self.store.job_metrics()
        self.assertEqual((row["outcome"], row["attempts"]), ("done", 2))

    def test_kept_when_the_job_is_deleted(self):
        self.store.insert(job("j1"))
        self.store.insert(job("j2"))
        self.run_job("j1")
        self.assertEqual(self.store.delete_finished_before(now() + 1), 1)
        self.assertIsNone(self.store.get("j1"))
        self.assertEqual([r["job_id"] for r in self.store.job_metrics()], ["j1"])
        self.assertIsNotNone(self.store.get("j2"))                             # still queued: no row, not deleted

    def test_between_two_times_oldest_first(self):
        for jid in ("j1", "j2", "j3"):
            self.store.insert(job(jid))
            self.run_job(jid)
        for jid, t in (("j1", 1_000), ("j2", 2_000), ("j3", 3_000)):
            self.store._db.execute("UPDATE job_metrics SET finished_at=? WHERE job_id=?", (t, jid))
        self.assertEqual([r["job_id"] for r in self.store.job_metrics(since=1_500)], ["j2", "j3"])
        self.assertEqual([r["job_id"] for r in self.store.job_metrics(until=3_000)], ["j1", "j2"])
        self.assertEqual([r["job_id"] for r in self.store.job_metrics(limit=1)], ["j1"])

    def test_a_row_that_cant_be_written_never_stops_the_job(self):
        self.store.insert(job())
        with mock.patch("comfyfarm.manager.db.units", side_effect=RuntimeError("boom")), \
                self.assertLogs("comfyfarm", "WARNING") as logs:
            done = self.run_job()
        self.assertEqual(done["status"], "done")
        self.assertEqual(self.store.job_metrics(), [])
        self.assertIn("metrics row for job j1: boom", logs.output[0])

    def test_jobs_finished_before_there_were_rows_get_theirs(self):
        self.store.insert(job("j1"))
        self.run_job("j1")
        self.store._db.execute("DELETE FROM job_metrics")
        self.store.close()
        self.store = JobStore(self.path)
        self.assertEqual([r["job_id"] for r in self.store.job_metrics()], ["j1"])

    def test_an_old_database_gets_the_table_and_its_finished_jobs_rows(self):
        path = Path(self.tmp) / "old.db"
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, workflow TEXT NOT NULL, user TEXT NOT NULL, name TEXT, "
                   "status TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, params TEXT, info TEXT, prompt TEXT, "
                   "worker TEXT, prompt_id TEXT, progress REAL NOT NULL DEFAULT 0, stage TEXT, error TEXT, "
                   "warnings TEXT, outputs TEXT, output_dir TEXT, attempts INTEGER NOT NULL DEFAULT 0, "
                   "cancel_requested INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, dispatched_at REAL, "
                   "started_at REAL, finished_at REAL)")
        db.execute("INSERT INTO jobs (id, workflow, user, status, worker, attempts, created_at, finished_at) "
                   "VALUES ('old', 'ltx25_alpha_gen', 'ann', 'done', 'gpu1', 1, 1, 61)")
        db.execute("INSERT INTO jobs (id, workflow, user, status, created_at) VALUES ('q', 'x', 'ann', 'queued', 1)")
        db.commit()
        db.close()
        store = JobStore(path)
        try:
            [row] = store.job_metrics()
            self.assertEqual((row["job_id"], row["worker"], row["gpu"], row["units"], row["finished_at"]),
                             ("old", "gpu1", None, 1, 61))
        finally:
            store.close()


class Read(unittest.TestCase):
    STORED = {"job_id": "j1", "kind": "batch", "parent_id": "s1", "workflow": "chord_pbr", "fingerprint": "0123",
              "user": "ann", "show": "xyz", "worker": "gpu1", "gpu": "NVIDIA RTX A6000", "outcome": "done",
              "attempts": 1, "units": 4, "created_at": 1_700_000_000, "assigned_at": 1_700_000_030,
              "dispatched_at": 1_700_000_042.5, "started_at": 1_700_000_045, "finished_at": 1_700_000_105}

    def test_local_times_and_how_long_each_part_took(self):
        [r] = MX.rows([self.STORED])
        self.assertEqual(tuple(r), MX.COLUMNS)
        self.assertEqual((r["job"], r["set"], r["gpu"], r["units"]), ("j1", "s1", "NVIDIA RTX A6000", 4))
        self.assertEqual(datetime.fromisoformat(r["finished"]).timestamp(), 1_700_000_105)
        self.assertEqual((r["wait_seconds"], r["prepare_seconds"], r["gpu_wait_seconds"], r["run_seconds"],
                          r["total_seconds"]), (30.0, 12.5, 2.5, 60.0, 105.0))
        [never] = MX.rows([dict(self.STORED, assigned_at=None, dispatched_at=None, started_at=None)])
        self.assertEqual((never["assigned"], never["wait_seconds"], never["run_seconds"], never["total_seconds"]),
                         (None, None, None, 105.0))

    def test_csv_for_a_spreadsheet(self):
        text = MX.to_csv(MX.rows([self.STORED, dict(self.STORED, job_id="j2", gpu=None)]))
        table = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(text.splitlines()[0], ",".join(MX.COLUMNS))
        self.assertEqual([(r["job"], r["gpu"], r["run_seconds"]) for r in table],
                         [("j1", "NVIDIA RTX A6000", "60.0"), ("j2", "", "60.0")])

    def test_times_asked_for(self):
        self.assertIsNone(MX.parse_time(None))
        self.assertIsNone(MX.parse_time(" "))
        self.assertEqual(MX.parse_time("1700000000"), 1_700_000_000)
        self.assertEqual(MX.parse_time("2026-11-01"), datetime(2026, 11, 1).timestamp())
        self.assertEqual(MX.parse_time("2026-11-01T09:30"), datetime(2026, 11, 1, 9, 30).timestamp())
        with self.assertRaisesRegex(ValueError, "'last week' isn't a time: give a date"):
            MX.parse_time("last week")


if __name__ == "__main__":
    unittest.main()
