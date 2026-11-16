"""A job's timeline (job_events): the store writes a line for every change worth one, in the
same breath as the change, so the farm's callers can't forget to."""

import shutil
import tempfile
import unittest
from pathlib import Path

from comfyfarm.manager.db import JobStore, now, stage_label


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = JobStore(Path(self.tmp) / "farm.db")
        self.store.insert({"id": "job1", "workflow": "wan22_i2i", "user": "ann", "status": "queued",
                           "created_at": now(), "info": {}})

    def tearDown(self):
        self.store.close()   # Windows can't delete an open database
        shutil.rmtree(self.tmp)

    def names(self, job="job1"):
        return [e["event"] for e in self.store.events(job)]

    def run_to(self, *steps):
        """Walk job1 through the statuses an agent reports, as the desk does."""
        s = self.store
        if "preparing" in steps:
            s.transition("job1", ("queued",), status="preparing", worker="gpu-a", prompt_id="try1", stage="on gpu-a")
        if "dispatched" in steps:
            s.transition("job1", ("preparing",), only_prompt="try1", status="dispatched", stage="waiting for GPU")
        if "running" in steps:
            s.transition("job1", ("dispatched",), only_prompt="try1", status="running", stage="starting")

    def test_a_run_from_submit_to_done(self):
        s = self.store
        self.run_to("preparing", "dispatched", "running")
        s.transition("job1", ("running",), only_prompt="try1", status="postprocessing", stage="collecting outputs")
        s.transition("job1", ("postprocessing",), only_prompt="try1", status="done", stage="done", finished_at=now())
        events = s.events("job1")
        self.assertEqual([e["event"] for e in events],
                         ["submitted", "preparing", "dispatched", "running", "postprocessing", "done"])
        self.assertEqual([e["status"] for e in events],
                         ["queued", "preparing", "dispatched", "running", "postprocessing", "done"])
        self.assertEqual([e["worker"] for e in events], [None] + ["gpu-a"] * 5)
        self.assertEqual({e["attempt"] for e in events[1:]}, {"try1"})
        self.assertEqual(events[2]["detail"], {"stage": "waiting for GPU"})
        self.assertEqual(events[5]["detail"], {})                 # "done" says it already
        self.assertEqual(sorted(e["at"] for e in events), [e["at"] for e in events])

    def test_a_refused_change_leaves_no_line(self):
        self.run_to("preparing")
        self.assertIsNone(self.store.transition("job1", ("preparing",), only_prompt="another-try", status="dispatched"))
        self.assertIsNone(self.store.transition("job1", ("running",), status="done"))
        self.assertEqual(self.names(), ["submitted", "preparing"])

    def test_progress_is_not_a_line_but_a_new_step_is(self):
        s = self.store
        self.run_to("preparing")
        for n in (1, 2, 3):
            s.transition("job1", ("preparing",), stage=f"converting EXR to PNG {n}/3", progress=n / 3)
        s.transition("job1", ("preparing",), stage="extracting movie frames")
        self.run_to("dispatched", "running")
        for node in ("Load Checkpoint", "KSampler 3/20", "KSampler 4/20", "VAE Decode"):   # ComfyUI's nodes: too many
            s.transition("job1", ("running",), stage=node, progress=0.5)
        steps = [(e["event"], e["detail"].get("stage")) for e in s.events("job1")]
        self.assertEqual(steps, [("submitted", None), ("preparing", "on gpu-a"), ("stage", "converting EXR to PNG"),
                                 ("stage", "extracting movie frames"), ("dispatched", "waiting for GPU"),
                                 ("running", "starting")])
        self.assertEqual(stage_label("writing EXR maps 12/240"), "writing EXR maps")
        self.assertEqual(stage_label("3/10 UDIMs"), "3/10 UDIMs")

    def test_a_lost_run_is_queued_again_with_the_reason_and_where_it_was(self):
        self.run_to("preparing", "dispatched", "running")
        self.store.transition("job1", ("running",), only_prompt="try1", status="queued", worker=None, prompt_id=None,
                              stage="requeued", warnings=["gpu-a stopped checking in."])
        e = self.store.events("job1")[-1]
        self.assertEqual((e["event"], e["status"], e["worker"], e["attempt"]), ("requeued", "queued", "gpu-a", "try1"))
        self.assertEqual(e["detail"], {"reason": "gpu-a stopped checking in."})

    def test_failure_carries_the_error_and_a_retry_is_named(self):
        self.run_to("preparing")
        self.store.transition("job1", ("preparing",), status="failed", stage="failed", error="No frames at /in/a.####.exr.")
        self.store.transition("job1", ("failed", "cancelled"), status="queued", stage="queued", error=None, worker=None)
        last = self.store.events("job1")[-2:]
        self.assertEqual((last[0]["event"], last[0]["detail"]), ("failed", {"error": "No frames at /in/a.####.exr."}))
        self.assertEqual((last[1]["event"], last[1]["detail"]), ("retried", {}))

    def test_asking_to_cancel_is_a_line_once(self):
        self.run_to("preparing", "dispatched", "running")
        self.store.transition("job1", ("running",), cancel_requested=1, stage="cancelling")
        self.store.update("job1", cancel_requested=1)
        self.store.transition("job1", ("running",), status="cancelled", stage="cancelled", finished_at=now())
        self.assertEqual(self.names()[-2:], ["cancel_requested", "cancelled"])

    def test_a_sets_batches_each_get_a_line_and_update_logs_a_status_change(self):
        rows = [{"id": f"b{i}", "workflow": "chord_pbr", "user": "ann", "status": "queued", "kind": "batch",
                 "parent_id": "set1", "created_at": now(), "info": {}} for i in range(3)]
        self.store.insert_many([{**rows[0], "id": "set1", "kind": "set", "parent_id": None}, *rows])
        self.assertEqual(self.store.events("set1")[0]["detail"], {"kind": "set"})
        self.assertEqual(self.names("b2"), ["submitted"])
        self.store.update("set1", status="running", stage="0/12 UDIMs", progress=0.1)
        self.store.update("set1", status="running", stage="4/12 UDIMs", progress=0.4)     # same status: no line
        self.assertEqual(self.names("set1"), ["submitted", "running"])

    def test_deleted_jobs_take_their_timeline_with_them(self):
        self.store.insert({"id": "old", "workflow": "wan22_i2i", "user": "ann", "status": "done",
                           "created_at": 1.0, "finished_at": 2.0, "info": {}})
        self.assertEqual(self.store.delete_finished_before(10.0), 1)
        self.assertEqual(self.store.events("old"), [])
        self.assertEqual(self.names(), ["submitted"])

    def test_a_database_from_before_gets_the_table(self):
        self.store.close()
        self.store = JobStore(Path(self.tmp) / "farm.db")        # opened again: the schema is applied twice
        self.assertEqual(self.names(), ["submitted"])


if __name__ == "__main__":
    unittest.main()
