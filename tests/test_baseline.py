"""tools/baseline.py: the steps of a job's run read from its timeline, cold and warm told
apart, and the report it writes."""

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("baseline_tool", REPO / "tools" / "baseline.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)

T0 = 1_800_000_000.0


def job(jid, wf, worker, start, run=60.0, frames=None, **k):
    """A done job: 5 s queued, 10 s prepared, 2 s behind another, `run` s on the GPU, 8 s collected."""
    return {"id": jid, "workflow": wf, "worker": worker, "status": "done", "kind": "job", "created_at": start,
            "dispatched_at": start + 15, "started_at": start + 17, "finished_at": start + 25 + run,
            "info": {"expected_frames": frames}, **k}


def timeline(j, run=60.0):
    s = j["created_at"]
    marks = [("submitted", 0), ("preparing", 5), ("stage", 6), ("dispatched", 15), ("running", 17),
             ("postprocessing", 17 + run), ("stage", 18 + run), ("done", 25 + run)]
    return [{"event": e, "at": s + dt} for e, dt in marks]


class PhaseTests(unittest.TestCase):
    def test_steps_from_a_timeline(self):
        j = job("a", "wan22_i2i", "gpu-a", T0)
        self.assertEqual(B.phases(j, timeline(j)),
                         {"wait": 5, "prepare": 10, "gpu_wait": 2, "run": 60, "collect": 8, "total": 85})

    def test_only_the_last_attempt_counts_but_the_total_is_from_submit(self):
        j = job("a", "wan22_i2i", "gpu-b", T0, finished_at=T0 + 300)
        lost = [{"event": "submitted", "at": T0}, {"event": "preparing", "at": T0 + 5},
                {"event": "dispatched", "at": T0 + 9}, {"event": "running", "at": T0 + 10},
                {"event": "requeued", "at": T0 + 100}]
        again = [{"event": "preparing", "at": T0 + 200}, {"event": "dispatched", "at": T0 + 210},
                 {"event": "running", "at": T0 + 212}, {"event": "done", "at": T0 + 300}]     # no postprocessing line
        self.assertEqual(B.phases(j, lost + again),
                         {"wait": 200, "prepare": 10, "gpu_wait": 2, "run": 88, "collect": 0, "total": 300})

    def test_a_job_from_before_the_timeline_uses_its_own_times(self):
        j = job("a", "wan22_i2i", "gpu-a", T0)
        self.assertEqual(B.phases(j, []),
                         {"wait": 15, "prepare": None, "gpu_wait": 2, "run": 68, "collect": None, "total": 85})

    def test_unfinished_and_failed_jobs_are_left_out(self):
        self.assertIsNone(B.phases({**job("a", "w", "g", T0), "status": "failed"}, []))
        self.assertIsNone(B.phases({**job("a", "w", "g", T0), "finished_at": None}, []))
        self.assertIsNone(B.phases({**job("a", "w", "g", T0), "dispatched_at": None}, []))

    def test_size_is_frames_or_tiles(self):
        self.assertEqual(B.units(job("a", "w", "g", T0, frames=81)), 81)
        self.assertEqual(B.units({"info": {"batch": {"tiles": 4}, "expected_frames": 9}}), 4)
        self.assertEqual(B.units({"info": None}), 1)


class ColdTests(unittest.TestCase):
    def test_cold_is_per_machine_after_another_workflow(self):
        jobs = [job("1", "wan", "gpu-a", T0), job("2", "wan", "gpu-a", T0 + 100), job("3", "ltx", "gpu-a", T0 + 200),
                job("4", "wan", "gpu-a", T0 + 300), job("5", "wan", "gpu-b", T0 + 150),
                {**job("6", "wan", None, T0 + 400), "started_at": None}]
        B.mark_cold(list(reversed(jobs)))
        self.assertEqual([j.get("cold") for j in jobs], [True, False, True, True, True, None])


class FakeFarm(ModuleType):
    """The client module, answering from lists."""
    class FarmError(Exception):
        pass

    def __init__(self, jobs, sets=None, with_timeline=()):
        super().__init__("comfyfarm_client")
        self.jobs_, self.sets, self.tl = jobs, sets or {}, set(with_timeline)

    def set_url(self, url):
        self.url = url

    def base_url(self):
        return getattr(self, "url", "http://farm:8000")

    def health(self):
        return {"ok": True, "version": "1.2.3"}

    def workers(self):
        return [{"name": "gpu-a", "gpu": {"name": "cuda:0 NVIDIA RTX A6000"}}, {"name": "gpu-b", "gpu": None}]

    def _request(self, method, path):
        assert path.startswith("/api/v1/jobs?status=done&limit="), path
        return {"jobs": self.jobs_}

    def batches(self, set_id):
        return self.sets[set_id]

    def events(self, job_id):
        j = next(x for x in self.jobs_ + sum(self.sets.values(), []) if x["id"] == job_id)
        return timeline(j, j["finished_at"] - j["created_at"] - 25) if job_id in self.tl else []


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.now = B.time.time()
        self.tmp = tempfile.mkdtemp()
        self.saved = sys.modules.get("comfyfarm_client")

    def tearDown(self):
        shutil.rmtree(self.tmp)
        sys.modules.pop("comfyfarm_client", None)
        if self.saved:
            sys.modules["comfyfarm_client"] = self.saved

    def farm(self):
        t = self.now - 3600
        batches = [job("b1", "chord_pbr", "gpu-a", t + 1000, run=30, kind="batch", info={"batch": {"tiles": 4}}),
                   job("b2", "chord_pbr", "gpu-a", t + 1100, run=20, kind="batch", info={"batch": {"tiles": 2}}),
                   {**job("b3", "chord_pbr", "gpu-a", t + 1200), "status": "failed"}]
        jobs = [job("j1", "wan22_i2i", "gpu-a", t, run=100, frames=81), job("j2", "wan22_i2i", "gpu-a", t + 200, run=60, frames=81),
                job("j3", "wan22_i2i", "gpu-a", t + 400, run=62, frames=41), job("j4", "wan22_i2i", "gpu-b", t + 500, run=90),
                job("old", "ltx25_alpha_gen", "gpu-a", self.now - 40 * 86400), {"id": "s1", "kind": "set", "workflow": "chord_pbr"}]
        return FakeFarm(jobs, {"s1": batches}, with_timeline={"j1", "j2", "j3", "b1", "b2"})

    def test_rows_and_summary(self):
        rows, counts = B.collect(self.farm(), days=14, limit=100)
        self.assertEqual(counts, {"coarse": 1, "jobs": 6})                         # j4 has no timeline; "old" is too old
        self.assertEqual({r["job"]: (r["gpu"], r["cold"]) for r in rows}["j4"], ("gpu-b", True))
        lines = B.summarise(rows)
        self.assertEqual([(x["workflow"], x["gpu"], x["start"], x["runs"]) for x in lines],
                         [("chord_pbr", "cuda:0 NVIDIA RTX A6000", "cold", 1), ("chord_pbr", "cuda:0 NVIDIA RTX A6000", "warm", 1),
                          ("wan22_i2i", "cuda:0 NVIDIA RTX A6000", "cold", 1), ("wan22_i2i", "cuda:0 NVIDIA RTX A6000", "warm", 2),
                          ("wan22_i2i", "gpu-b", "cold", 1)])
        warm = lines[3]
        self.assertEqual((warm["run"], warm["size"], warm["prepare"], warm["total"], warm["total_min"], warm["total_max"]),
                         (61.0, 61.0, 10.0, 86.0, 85.0, 87.0))
        self.assertEqual((lines[4]["prepare"], lines[4]["collect"], lines[4]["run"]), (None, None, 98.0))
        only = B.collect(self.farm(), days=14, limit=100, workflow="chord_pbr")[0]
        self.assertEqual([(r["job"], r["cold"], r["units"]) for r in only], [("b1", True, 4), ("b2", False, 2)])

    def test_the_command_writes_a_table_and_the_numbers(self):
        sys.modules["comfyfarm_client"] = self.farm()
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(B.main(["--out", self.tmp, "--url", "http://farm-host:8000"]), 0)
        date = B.time.strftime("%Y-%m-%d")
        md = (Path(self.tmp) / f"baseline-{date}.md").read_text(encoding="utf-8")
        self.assertIn("| wan22_i2i | cuda:0 NVIDIA RTX A6000 | warm | 2 | 61 | 5 | 10 | 2 | 61 | 8 | 86 | 85 to 87 |", md)
        self.assertIn("| wan22_i2i | gpu-b | cold | 1 | 1 | 15 |  | 2 | 98 |  | 115 | 115 to 115 |", md)
        self.assertIn("from 6 finished jobs of the last 14 days on http://farm-host:8000 (farm code 1.2.3)", md)
        self.assertIn("1 job(s) from before the farm kept a timeline", md)
        self.assertIn(md, out.getvalue())
        data = json.loads((Path(self.tmp) / f"baseline-{date}.json").read_text(encoding="utf-8"))
        self.assertEqual((len(data["jobs"]), len(data["summary"]), data["meta"]["version"]), (6, 5, "1.2.3"))

    def test_an_empty_farm_still_gives_a_report(self):
        md = B.markdown(B.summarise([]), {"date": "2026-11-16", "days": 14, "manager": "m", "version": "1", "jobs": 0,
                                          "coarse": 0})
        self.assertIn("no finished jobs in this period", md)
        self.assertIn("Every job had a timeline.", md)


if __name__ == "__main__":
    unittest.main()
