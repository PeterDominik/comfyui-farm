"""GET /metrics in Prometheus' text format (manager/prom.py, decision 0048): every family
declared once with its HELP and TYPE, labels escaped, and the numbers from the queue,
job_metrics, the machines and their GPUs, the policy and the sweeper. The endpoint is in
test_e2e.

    python -m unittest tests.test_prom -v
"""

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.manager import prom as PM
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.desk import AgentState, Desk
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.definitions import DefinitionRegistry
from comfyfarm.protocol import build, now

REPO = Path(__file__).resolve().parent.parent
SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})? (-?[0-9.e+]+|NaN|[+-]Inf)$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse(text: str) -> dict:
    """{(name, ((label, value), ...)): number}, after checking the format: each family's HELP
    and TYPE once, before its samples, and every other line a sample."""
    declared, typed, out = set(), set(), {}
    assert text.endswith("\n")
    for line in text.splitlines():
        if line.startswith("# HELP "):
            name = line.split(" ")[2]
            assert name not in declared, f"{name} declared twice"
            declared.add(name)
        elif line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert name in declared and kind == "gauge", line
            typed.add(name)
        else:
            m = SAMPLE.match(line)
            assert m, f"not a sample: {line!r}"
            name = m.group(1)
            assert name in typed, f"{name} has no HELP and TYPE before its samples"
            labels = tuple(sorted((k, v.encode().decode("unicode_escape")) for k, v in LABEL.findall(m.group(3) or "")))
            out[(name, labels)] = float(m.group(4))
    return out


def value(samples: dict, name: str, /, **labels):
    return samples.get((name, tuple(sorted(labels.items()))))


class Format(unittest.TestCase):
    def test_labels_are_escaped_and_families_declared_once(self):
        x = PM.Exposition()
        x.gauge("comfyfarm_test", "A test", 2, path='a "quoted"\\name\nnext')
        x.gauge("comfyfarm_test", "", 3.5, path="b")
        x.gauge("comfyfarm_empty", "Declared, no samples")
        text = x.text()
        self.assertEqual(text, '# HELP comfyfarm_test A test\n# TYPE comfyfarm_test gauge\n'
                               'comfyfarm_test{path="a \\"quoted\\"\\\\name\\nnext"} 2\n'
                               'comfyfarm_test{path="b"} 3.5\n'
                               '# HELP comfyfarm_empty Declared, no samples\n# TYPE comfyfarm_empty gauge\n')
        self.assertEqual(len(parse(text)), 2)


class Numbers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = self.tmp.replace("\\", "/")
        cfg = Config.model_validate({"database": f"{root}/data/farm.db", "workers": [],
                                     "workflows_dir": str(REPO / "workflows")})
        self.store = JobStore(Path(f"{root}/data/farm.db"))
        self.farm = Farm(cfg, self.store, DefinitionRegistry(REPO / "workflows"), EventBus())
        self.desk = Desk(self.farm)
        self.t = now()

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, jid, workflow="chord_pbr", status="queued", **kw):
        self.store.insert({"id": jid, "workflow": workflow, "user": "ann", "status": status,
                           "created_at": kw.pop("created_at", self.t - 100), "info": {"show": "xyz"}, **kw})

    def ran(self, jid, workflow, outcome, gpu, frames, seconds, waited=10):
        """A job through the desk's states to `outcome`, `seconds` on the GPU."""
        self.job(jid, workflow, created_at=self.t - 1000, info={"show": "xyz", "expected_frames": frames})
        s = self.store
        s.transition(jid, ("queued",), status="preparing", worker="gpu1", gpu=gpu, prompt_id="a")
        s._db.execute("UPDATE job_events SET at=? WHERE job_id=? AND event='preparing'", (self.t - 1000 + waited, jid))
        s.transition(jid, ("preparing",), status="dispatched", dispatched_at=self.t - 500, attempts=1)
        s.transition(jid, ("dispatched",), status="running", started_at=self.t - 500)
        s.transition(jid, ("running",), status=outcome, finished_at=self.t - 500 + seconds)

    def render(self) -> dict:
        return parse(PM.render(self.farm, self.desk, self.t - 3600, self.t))

    def test_the_manager_and_the_queue(self):
        self.job("q1", created_at=self.t - 300)
        self.job("q2", created_at=self.t - 60)
        self.job("q3", workflow="wan22_i2i")
        self.job("r1", status="running", worker="gpu1")
        self.job("s1", kind="set")                                       # its batches are the runs
        self.job("b1", kind="batch", parent_id="s1")
        self.job("d1", status="done")
        m = self.render()
        self.assertEqual(value(m, "comfyfarm_manager_info", build=build()), 1)
        self.assertEqual(value(m, "comfyfarm_manager_start_time_seconds"), round(self.t - 3600, 3))
        self.assertEqual(value(m, "comfyfarm_jobs", status="queued", workflow="chord_pbr"), 3)   # q1, q2, b1
        self.assertEqual(value(m, "comfyfarm_jobs", status="queued", workflow="wan22_i2i"), 1)
        self.assertEqual(value(m, "comfyfarm_jobs", status="running", workflow="chord_pbr"), 1)
        self.assertIsNone(value(m, "comfyfarm_jobs", status="done", workflow="chord_pbr"))       # active only
        self.assertEqual(value(m, "comfyfarm_queue_oldest_seconds"), 300)

    def test_finished_jobs_from_job_metrics(self):
        self.ran("a", "chord_pbr", "done", "NVIDIA RTX A6000", frames=4, seconds=60)
        self.ran("b", "chord_pbr", "done", "NVIDIA RTX A6000", frames=6, seconds=90, waited=30)
        self.ran("c", "chord_pbr", "failed", "NVIDIA RTX A6000", frames=4, seconds=5)
        self.ran("d", "wan22_i2i", "done", "NVIDIA GeForce RTX 5090", frames=81, seconds=300)
        self.store.delete_finished_before(self.t + 1)                   # the jobs go; their numbers stay
        m = self.render()
        a6000 = {"workflow": "chord_pbr", "gpu": "NVIDIA RTX A6000"}
        self.assertEqual(value(m, "comfyfarm_jobs_finished", outcome="done", **a6000), 2)
        self.assertEqual(value(m, "comfyfarm_jobs_finished", outcome="failed", **a6000), 1)
        self.assertEqual(value(m, "comfyfarm_job_run_seconds_sum", **a6000), 150)
        self.assertEqual(value(m, "comfyfarm_job_run_seconds_count", **a6000), 2)
        self.assertEqual(value(m, "comfyfarm_job_frames_sum", **a6000), 10)
        self.assertEqual(value(m, "comfyfarm_job_wait_seconds_sum", workflow="chord_pbr", outcome="done"), 40)
        self.assertEqual(value(m, "comfyfarm_job_run_seconds_sum", workflow="wan22_i2i",
                               gpu="NVIDIA GeForce RTX 5090"), 300)

    def test_machines_and_their_gpus(self):
        probed = AgentState(name="wrk1125l-3090", version="0.9.0+abc", last_seen=now(), online=True,
                            gpus=[{"index": 0, "name": "NVIDIA GeForce RTX 3090", "utilization": 87,
                                   "vram_used": 20 * 2**30, "vram_total": 24 * 2**30}],
                            stats={"devices": [{"name": "cuda:0 NVIDIA GeForce RTX 3090 : cudaMallocAsync"}]})
        comfy = AgentState(name="tor-flame02-a6000", last_seen=now(), online=False,
                           stats={"devices": [{"index": 0, "name": "cuda:0 NVIDIA RTX A6000 : cudaMallocAsync",
                                               "vram_total": 48 * 2**30, "vram_free": 40 * 2**30}]})
        gone = AgentState(name="wrk59467w-5090", last_seen=now() - 3600)
        for a in (probed, comfy, gone):
            self.desk.agents[a.name] = a
        self.desk.drained["tor-flame02-a6000"] = {"by": "peter", "since": now()}
        self.job("r1", status="running", worker="wrk1125l-3090")
        m = self.render()
        self.assertEqual([value(m, "comfyfarm_machine_up", machine=n) for n in (probed.name, comfy.name, gone.name)],
                         [1, 1, 0])
        self.assertEqual(value(m, "comfyfarm_machine_runner_up", machine=comfy.name), 0)
        self.assertEqual(value(m, "comfyfarm_machine_drained", machine=comfy.name), 1)
        self.assertEqual(value(m, "comfyfarm_machine_jobs", machine=probed.name), 1)
        self.assertEqual(value(m, "comfyfarm_machine_info", machine=probed.name, agent="0.9.0+abc",
                               gpu="NVIDIA GeForce RTX 3090"), 1)
        at = {"machine": probed.name, "gpu": "0", "name": "NVIDIA GeForce RTX 3090"}
        self.assertEqual(value(m, "comfyfarm_gpu_utilization_ratio", **at), 0.87)
        self.assertEqual(value(m, "comfyfarm_gpu_memory_used_bytes", **at), 20 * 2**30)
        at = {"machine": comfy.name, "gpu": "0", "name": "NVIDIA RTX A6000"}       # ComfyUI's: no use
        self.assertEqual(value(m, "comfyfarm_gpu_memory_used_bytes", **at), 8 * 2**30)
        self.assertIsNone(value(m, "comfyfarm_gpu_utilization_ratio", **at))
        self.assertFalse([k for k in m if k[0].startswith("comfyfarm_gpu") and dict(k[1])["machine"] == gone.name])

    def test_policy_and_the_sweeper(self):
        m = self.render()
        self.assertEqual(value(m, "comfyfarm_policy_enabled"), 0)
        self.assertIsNone(value(m, "comfyfarm_sweep_files"))                    # no report yet
        self.farm.sweeper.run(self.t)
        m = self.render()
        self.assertEqual((value(m, "comfyfarm_sweep_expired"), value(m, "comfyfarm_sweep_problems")), (0, 0))
        self.assertEqual(value(m, "comfyfarm_sweep_last_run_timestamp_seconds"), int(self.t))


if __name__ == "__main__":
    unittest.main()
