"""Batch jobs (decision 0063): a process definition's `class: batch`. Its jobs queue after
artists' jobs and take only a free GPU, nothing is prepared ahead behind one, and with
batch.preempt a drained machine's batch job saves what it resumes from in its resume folder,
stops and is queued again, to go on elsewhere. Here: the queue's order, the supervisor's
`preempt` file, the process runner's half, a batch job through the manager and two agents,
the farm's batch test (workflows/batch_test) run with a stand-in torch, and the dashboard's
line for it.

    python -m unittest tests.test_batch -v
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dashboard_envs import HARNESS, NODE  # noqa: E402
from test_dashboard_spend import page_function  # noqa: E402
from test_process_runner import PYTHON, Environment, FakeHost, Farmed, definition  # noqa: E402

from comfyfarm.definitions import DefinitionRegistry  # noqa: E402
from comfyfarm.manager.db import PREEMPTED, JobStore  # noqa: E402
from comfyfarm.protocol import now  # noqa: E402
from comfyfarm.runners import process_run as PR  # noqa: E402
from comfyfarm.runners.process import ProcessRegistry, ProcessRunner  # noqa: E402

try:
    from comfyfarm.agent.agent import Agent, Held
except ImportError:   # pragma: no cover
    Agent = Held = None

REPO = Path(__file__).resolve().parent.parent
RUN = REPO / "workflows" / "batch_test" / "run.py"
FAKE_TORCH = Path(__file__).resolve().parent / "fake_torch"


class TheQueue(unittest.TestCase):
    """Artists' jobs go first, whatever a batch job's priority."""

    def test_batch_jobs_come_after_artists_jobs(self):
        tmp = Path(tempfile.mkdtemp())
        store = JobStore(tmp / "farm.db")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(store.close)            # first: Windows can't delete an open database
        t = now()
        for i, (jid, cls, priority) in enumerate((("train", "batch", 5), ("ann", "interactive", 0),
                                                  ("sweep", "batch", 0), ("old", None, 0), ("bob", "interactive", 1))):
            store.insert({"id": jid, "workflow": "w", "user": jid, "status": "queued", "priority": priority,
                          "created_at": t + i, "info": {"class": cls} if cls else {}})
        order = store.queued()
        self.assertEqual([j["id"] for j in order], ["bob", "ann", "old", "train", "sweep"])
        self.assertNotIn("is_batch", order[0])


class Supervisor(unittest.TestCase):
    """The `preempt` file (runners/process_run.py): saved and stopped, or stopped."""

    def setUp(self):
        self.run = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run, True)

    def supervise(self, command, seconds=None, preempt=None):
        (self.run / "run.json").write_text(json.dumps({"command": command, "seconds": seconds, "grace": 0.5,
                                                       "preempt": preempt}))
        p = subprocess.Popen([PYTHON, "-I", str(PR.__file__), str(self.run)], cwd=str(self.run))
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def started(self):
        end = time.time() + 10
        while not (self.run / "state.json").exists() and time.time() < end:
            time.sleep(0.05)

    def ended(self, p):
        p.wait(30)
        return json.loads((self.run / "exit.json").read_text())

    def test_a_command_that_saves_and_stops_when_asked_was_preempted(self):
        p = self.supervise([PYTHON, "-c", "import os, time\nwhile not os.path.exists('preempt'): time.sleep(0.05)"])
        self.started()
        (self.run / "preempt").touch()
        ended = self.ended(p)
        self.assertEqual((ended["reason"], ended["code"]), ("preempted", 0))

    def test_one_that_doesnt_stop_is_stopped_when_its_time_is_up(self):
        p = self.supervise([PYTHON, "-c", "import time; time.sleep(60)"], preempt=0.5)
        self.started()
        t0 = time.time()
        (self.run / "preempt").touch()
        self.assertEqual(self.ended(p)["reason"], "preempted")
        self.assertLess(time.time() - t0, 20)

    def test_a_cancel_meanwhile_wins(self):
        p = self.supervise([PYTHON, "-c", "import time; time.sleep(60)"], preempt=60)
        self.started()
        (self.run / "preempt").touch()
        time.sleep(0.5)
        (self.run / "cancel").touch()
        self.assertEqual(self.ended(p)["reason"], "cancelled")

    def test_once_asked_its_time_limit_no_longer_counts(self):
        (self.run / "preempt").touch()          # saving may take longer than what was left of its limit
        p = self.supervise([PYTHON, "-c", "import time; time.sleep(1.5)"], seconds=0.5, preempt=30)
        self.assertEqual(self.ended(p)["reason"], "preempted")


@unittest.skipIf(Held is None, "the agent needs httpx and websockets")
class TheRunner(Environment):
    """The process runner's half: job.json says where to save and what to watch; a preempted
    run is reported so, and leaves no run folder (what it did is in its resume folder)."""

    def test_a_preempted_run(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        host = FakeHost(self.root)
        cfg_runs = self.root / "runs"
        from comfyfarm.config import AgentConfig
        runner = ProcessRunner(AgentConfig(name="gpu1", base_dir=self.root, envs={"dir": str(self.builds)},
                                           process_runner={"enabled": True, "runs_dir": str(cfg_runs),
                                                           "grace_seconds": 0.5, "preempt_seconds": 30}),
                               registry=ProcessRegistry(self.workflows), tick=0.05)
        runner.attach(host)
        resume, out = self.root / "out" / "plate.demo_v001.resume", self.root / "out"
        resume.mkdir(parents=True)
        wf = runner._definitions()["demo"]
        job = {"job": "job-1", "attempt": "att-1", "workflow": "demo", "info": {"resume_dir": str(resume)},
               "request": {"definition": wf.digest, "values": {"mode": "batch"}, "seq": None, "inputs": {},
                           "out_dir": str(out), "frames_prefix": "_raw"}}
        h = Held(job=job, attempt="att-1", status="dispatched", pid="att-1", runner=runner)
        host.held.append(h)

        async def until(pred):
            end = time.time() + 20
            while time.time() < end and not pred():
                await runner._watch(h)
                await asyncio.sleep(0.05)
            self.assertTrue(pred(), host.said)

        async def go():
            await runner.submit(h)
            said = json.loads((cfg_runs / "att-1" / "job.json").read_text())
            self.assertEqual((said["resume_dir"], said["preempt"]), (str(resume), str(cfg_runs / "att-1" / "preempt")))
            self.assertEqual(json.loads((cfg_runs / "att-1" / "run.json").read_text())["preempt"], 30)
            await until(lambda: ("started",) in host.said)
            self.assertFalse(await runner.preempt(h))       # it ends as preempted
            await until(lambda: ("preempted",) in host.said)
            await runner.forget(h.pid)
            if h.rt.proc is not None:
                await asyncio.to_thread(h.rt.proc.wait, 30)
        loop.run_until_complete(go())
        self.assertEqual((resume / "saved").read_text(), "att-1")      # what the next run goes on from
        self.assertFalse((cfg_runs / "att-1").exists())


@unittest.skipIf(Agent is None, "the agent needs httpx and websockets")
class ThroughTheFarm(Farmed):
    """A batch job through the manager and two agents: drained on one, it saves, stops and is
    queued again, waits behind an artist's job, and finishes on the other."""

    def definitions(self):
        definition(self.workflows, "copy", params={
            **self.FRAMES, "mode": {"type": "choice", "choices": ["ok", "hang"], "default": "ok"}})
        definition(self.workflows, "train", params={
            **self.FRAMES, "mode": {"type": "choice", "choices": ["batch"], "default": "batch"}},
            writes={"file": {"ext": "json"}}, **{"class": "batch"})

    def setUp(self):
        super().setUp()
        self.second = None

    def add_second(self):
        """gpu2: another machine with ComfyUI and the process runner, as gpu1 is."""
        from test_agent import FakeComfy, FlakyLink
        from comfyfarm.config import AgentConfig
        from comfyfarm.runners.comfyui import ComfyUIRunner
        from comfyfarm.runners.comfyui_workflow import WorkflowRegistry
        root = str(self.root).replace("\\", "/")
        acfg = AgentConfig(name="gpu2", comfyui="http://gpu2:8188", max_inflight=1, base_dir=root,
                           paths={"mappings": [{"posix": "/rdo/shows", "windows": f"{root}/shows"}]},
                           convert={"scratch_dir": f"{root}/scratch-gpu2"}, heartbeat_wait=0, retry_seconds=0.05,
                           health_seconds=999, reconcile_seconds=999, workflows_dir=str(self.workflows),
                           envs={"dir": str(self.builds)},
                           process_runner={"enabled": True, "runs_dir": f"{root}/runs-gpu2", "grace_seconds": 0.5})
        comfy = ComfyUIRunner(acfg, client=FakeComfy())
        comfy._registry = WorkflowRegistry(self.workflows)
        process = ProcessRunner(acfg, registry=ProcessRegistry(self.workflows), tick=0.05)
        agent = Agent(acfg, FlakyLink(self.desk), runners=[comfy, process])

        async def ready():
            await agent._hello()
            comfy.stats = await comfy.client.system_stats()
            comfy.online = comfy.ws_connected = True
            await comfy.refresh_object_info()
            return asyncio.ensure_future(process._watch_loop())
        watching = self.wait(ready())

        def stop():
            watching.cancel()
            self.wait(asyncio.gather(watching, return_exceptions=True))
            pending = [t for t in agent._bg if not t.done()]
            if pending:
                self.wait(asyncio.wait(pending, timeout=30))
            self.wait(agent.stop())
        self.addCleanup(stop)
        self.second = agent
        return agent

    def beat(self, agent=None):
        agent = agent or self.agent

        async def go():
            reply = await agent._heartbeat()
            await agent._obey(reply)
            return reply
        return self.wait(go())

    def status(self, job):
        return self.store.get(job["id"])["status"]

    def test_drained_it_saves_stops_and_goes_on_elsewhere(self):
        self.farm.cfg.batch.preempt = True
        train = self.farm.submit("train", {"input": f"{self.SEQ} 1001"}, "ann")
        resume = train["info"]["resume_dir"]
        self.assertEqual((train["info"]["class"], os.path.basename(resume)),
                         ("batch", os.path.basename(train["info"]["writes"]["file"]["base"]) + ".resume"))
        self.assertEqual(os.path.dirname(resume), train["output_dir"])          # beside its outputs
        self.beat()
        self.until(lambda: self.status(train) == "running", what="the batch job running")
        self.assertTrue(os.path.isdir(resume))
        first = self.store.get(train["id"])["prompt_id"]
        # An artist's job isn't prepared ahead behind it: it waits for a free GPU.
        copy = self.farm.submit("copy", {"input": f"{self.SEQ} 1001-1003"}, "bob")
        self.beat()
        self.assertEqual(self.status(copy), "queued")
        # Drained: it is told to stop, saves, and is queued again, not counted as an attempt.
        self.desk.set_drained("gpu1", True, by="peter")
        reply = self.beat()
        self.assertEqual(reply["preempt"], [{"job": train["id"], "attempt": first, "why": "gpu1 was drained by peter"}])
        self.until(lambda: self.status(train) == "queued", what="the batch job queued again")
        row = self.store.get(train["id"])
        self.assertEqual((row["stage"], row["attempts"], row["warnings"], row["worker"]), (PREEMPTED, 0, [], None))
        self.assertEqual([(p["worker"], p["why"]) for p in row["info"]["preempted"]],
                         [("gpu1", "gpu1 was drained by peter")])
        self.assertEqual(Path(resume, "saved").read_text(), first)
        events = self.farm.timeline(train["id"])["events"]
        said = [(e["event"], (e.get("detail") or {}).get("reason")) for e in events if e["event"] == "preempted"]
        self.assertEqual(said, [("preempted", "gpu1 was drained by peter")])
        self.until(lambda: not (Path(self.process.runs_dir) / first).exists(), what="its run folder gone")
        self.assertEqual(self.agent.held, {})
        # gpu2 takes the artist's job first, then the batch job, which goes on from what it saved.
        gpu2 = self.add_second()
        self.beat(gpu2)
        self.assertEqual((self.store.get(copy["id"])["worker"], self.status(train)), ("gpu2", "queued"))
        self.until(lambda: self.status(copy) == "done", what="the artist's job done")
        self.beat(gpu2)
        self.until(lambda: self.status(train) in ("done", "failed"), what="the batch job to end")
        row = self.store.get(train["id"])
        self.assertEqual((row["status"], row["worker"], row["attempts"]), ("done", "gpu2", 1), row["error"])
        [report] = row["outputs"]["file"]["files"]
        self.assertTrue(os.path.exists(report))
        self.until(lambda: not os.path.exists(resume), what="its resume folder removed")

    def test_without_preempt_a_drained_machines_batch_job_carries_on(self):
        train = self.farm.submit("train", {"input": f"{self.SEQ} 1001"}, "ann")
        self.beat()
        self.until(lambda: self.status(train) == "running", what="running")
        self.desk.set_drained("gpu1", True, by="peter")
        self.assertNotIn("preempt", self.beat())
        self.assertEqual(self.status(train), "running")
        # With it on, a job the agent says it is already stopping isn't asked again.
        self.farm.cfg.batch.preempt = True
        a, row = self.desk.agents["gpu1"], self.store.get(train["id"])
        listed = {train["id"]: {"attempt": row["prompt_id"], "preempting": True}}
        self.assertNotIn("preempt", self.desk._instructions(a, listed))
        listed[train["id"]]["preempting"] = False
        self.assertEqual(len(self.desk._instructions(a, listed)["preempt"]), 1)
        # Cancelled, its resume folder goes with it.
        self.wait(self.farm.cancel(train["id"]))
        self.beat()
        self.until(lambda: self.status(train) == "cancelled", what="cancelled")
        self.until(lambda: not os.path.exists(train["info"]["resume_dir"]), what="its resume folder removed")


class TheBatchTest(unittest.TestCase):
    """workflows/batch_test: its definition as the repo has it, and its script run as the
    process runner runs it, with a stand-in torch (tests/fake_torch)."""

    def test_it_is_in_the_repo_and_has_no_problems(self):
        wf = ProcessRegistry(REPO / "workflows").workflows["batch_test"]
        self.assertEqual((wf.problems, wf.wiring_problems), ([], []))
        self.assertEqual((wf.job_class, wf.environment, wf.port.takes, wf.hidden), ("batch", "gpu-test", "image", True))
        self.assertEqual(sorted(wf.params), ["hold_gb", "input", "save_every", "step_seconds", "steps"])
        self.assertIn("batch_test", DefinitionRegistry(REPO / "workflows").workflows)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.resume, self.out = self.tmp / "plate.batchtest_v001.resume", self.tmp / "out"
        self.resume.mkdir()
        self.out.mkdir()

    def job(self, **params):
        job = {"job": "0123456789", "attempt": "a1", "params": params, "resume_dir": str(self.resume),
               "events": str(self.tmp / "events.jsonl"), "result": str(self.tmp / "result.json"),
               "cancel": str(self.tmp / "cancel"), "preempt": str(self.tmp / "preempt"),
               "writes": {"file": {"dir": str(self.out), "prefix": "_comfyfarm_01234567_file", "ext": "json"}}}
        (self.tmp / "job.json").write_text(json.dumps(job), encoding="utf-8")

    def start(self, env=None):
        path = os.pathsep.join([str(FAKE_TORCH), *filter(None, [os.environ.get("PYTHONPATH")])])
        return subprocess.Popen([sys.executable, str(RUN)], cwd=self.tmp, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                env={**os.environ, "PYTHONPATH": path, "COMFYFARM_JOB": str(self.tmp / "job.json"),
                                     **(env or {})})

    def finish(self, p):
        out, err = p.communicate(timeout=120)
        result = json.loads((self.tmp / "result.json").read_text()) if (self.tmp / "result.json").exists() else None
        return p.returncode, err, result

    def events(self):
        """What it wrote to events.jsonl so far: a line still being written is left for the next look."""
        path, out = self.tmp / "events.jsonl", []
        for line in path.read_text().splitlines() if path.exists() else []:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    def checkpoint(self):
        return json.loads((self.resume / "checkpoint.json").read_text())

    def report(self):
        return json.loads((self.out / "_comfyfarm_01234567_file.json").read_text())

    def test_it_counts_its_steps_and_reports_its_run(self):
        self.job(steps=3, step_seconds=0, save_every=2)
        code, err, result = self.finish(self.start())
        self.assertEqual((code, result), (0, {"warnings": []}), err)
        self.assertEqual(self.events()[-1], {"progress": 1.0, "stage": "step 3/3"})
        report = self.report()
        self.assertEqual((report["steps"], report["preempted"]), (3, 0))
        self.assertEqual([(r["from"], r["to"], r["ended"], r["gpu"]) for r in report["runs"]],
                         [(0, 3, "finished", "Fake GPU")])
        self.assertEqual(self.checkpoint()["step"], 3)

    def test_preempted_it_saves_and_the_next_run_goes_on_from_there(self):
        self.job(steps=40, step_seconds=0.05, save_every=1000)        # never saved but when asked
        p = self.start()
        end = time.time() + 30
        while time.time() < end and not any(e.get("stage") == "step 3/40" for e in self.events()):
            time.sleep(0.02)
        (self.tmp / "preempt").touch()
        code, err, _ = self.finish(p)
        self.assertEqual(code, 0, err)
        at = self.checkpoint()["step"]
        self.assertTrue(3 <= at < 40, at)
        self.assertEqual(self.events()[-1]["stage"], f"preempted at step {at}/40: saved")
        self.assertFalse((self.out / "_comfyfarm_01234567_file.json").exists())     # no report until it finishes
        (self.tmp / "preempt").unlink()                                 # the next run, as the farm starts it
        code, err, _ = self.finish(self.start())
        self.assertEqual(code, 0, err)
        self.assertTrue(any(e.get("stage", "").startswith(f"step {at}/40 (resumed on ") for e in self.events()))
        report = self.report()
        self.assertEqual([(r["from"], r["to"], r["ended"]) for r in report["runs"]],
                         [(0, at, "preempted"), (at, 40, "finished")])
        self.assertEqual(report["preempted"], 1)

    def test_cancelled_it_stops_and_saves_nothing(self):
        self.job(steps=40, step_seconds=0.05)
        (self.tmp / "cancel").touch()
        code, err, _ = self.finish(self.start())
        self.assertEqual(code, 0, err)
        self.assertFalse((self.resume / "checkpoint.json").exists())

    def test_a_resume_folder_from_another_length_of_run_is_refused(self):
        (self.resume / "checkpoint.json").write_text(json.dumps({"step": 4, "steps": 10, "runs": []}))
        self.job(steps=5, step_seconds=0)
        code, _, result = self.finish(self.start())
        self.assertEqual(code, 1)
        self.assertIn("holds a run of 10 steps, not 5", result["error"])

    def test_no_gpu(self):
        self.job(steps=3, step_seconds=0)
        code, _, result = self.finish(self.start(env={"FAKE_TORCH_GPU": "0"}))
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "torch sees no GPU on this machine: check its NVIDIA driver (nvidia-smi)")


@unittest.skipUnless(NODE, "node isn't installed")
class TheDashboard(unittest.TestCase):
    """A batch job's line in its details, and its timeline's word for a preemption."""

    def run_js(self, *cases):
        with tempfile.TemporaryDirectory() as tmp:
            script, harness, data = Path(tmp) / "batch.js", Path(tmp) / "harness.js", Path(tmp) / "cases.json"
            script.write_text(page_function("batchLine"), encoding="utf-8")
            harness.write_text(HARNESS, encoding="utf-8")
            data.write_text(json.dumps(list(cases)), encoding="utf-8")
            out = subprocess.run([NODE, str(harness), str(script), str(data)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_its_line(self):
        said = "Waits behind artists' jobs, and saves where it is and stops when the farm needs its GPU, to go on " \
               "from there on the next free one."
        once = {"info": {"preempted": [{"worker": "a6000", "why": "a6000 was drained by peter"}]}}
        twice = {"info": {"preempted": once["info"]["preempted"] * 2}}
        self.assertEqual(self.run_js(["batchLine", [{"info": {}}]], ["batchLine", [once]], ["batchLine", [twice]]),
                         [said, said + " Preempted once, last on a6000: a6000 was drained by peter.",
                          said + " Preempted 2 times, last on a6000: a6000 was drained by peter."])
        html = (REPO / "comfyfarm" / "manager" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('preempted: "Preempted"', html)
        self.assertIn('pathRow("Resume folder", i.resume_dir_posix || i.resume_dir)', html)


if __name__ == "__main__":
    unittest.main()
