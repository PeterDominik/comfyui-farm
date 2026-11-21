"""The process runner (runners/process.py, decision 0025): a process definition's half,
the supervisor that runs each command (runners/process_run.py) and keeps its time limit,
the runner the agent talks to, and a process job through the manager and an agent.

Commands are small Python scripts run in a real runner environment, built with the
stand-in uv (tests/fake_builder.py): nothing is downloaded.

    python -m unittest tests.test_process_runner -v
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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_envs import UV, make  # noqa: E402

from comfyfarm.agent.launch import pid_alive  # noqa: E402
from comfyfarm.config import AgentConfig  # noqa: E402
from comfyfarm.definitions import DefinitionRegistry  # noqa: E402
from comfyfarm.envs import build as EB  # noqa: E402
from comfyfarm.envs import spec as ES  # noqa: E402
from comfyfarm.fileio import wire as W  # noqa: E402
from comfyfarm.runners import process_run as PR  # noqa: E402
from comfyfarm.runners.base import RunRejected  # noqa: E402
from comfyfarm.runners.process import ProcessDefinition, ProcessRegistry, ProcessRunner  # noqa: E402

try:
    from comfyfarm.agent.agent import Agent, Held
except ImportError:   # pragma: no cover
    Agent = Held = None

PYTHON = getattr(sys, "_base_executable", None) or sys.executable

# The command the test definitions run: what it does is its `mode` parameter.
RUN_PY = r'''
import json, os, shutil, subprocess, sys, time
job = json.load(open(os.environ["COMFYFARM_JOB"], encoding="utf-8"))
mode = job["params"].get("mode", "ok")
here = os.path.dirname(os.path.abspath(__file__))

def event(**e):
    with open(job["events"], "a", encoding="utf-8") as f:
        f.write(json.dumps(e) + "\n")

with open("seen.json", "w", encoding="utf-8") as f:
    json.dump({"python": sys.executable, "prefix": sys.prefix, "cwd": os.getcwd(), "here": here,
               "pythonpath": os.environ.get("PYTHONPATH"), "job": job}, f)
open("command.pid", "w").write(str(os.getpid()))
event(progress=0.1, stage="starting")
if mode == "fail":
    print("something broke on the GPU", flush=True)
    sys.exit(3)
if mode == "error":
    json.dump({"error": "the model said no"}, open(job["result"], "w"))
    sys.exit(1)
if mode == "hang":                      # ignores the cancel file: stopped by signal, then killed
    while True:
        time.sleep(0.05)
if mode == "spawn":                     # starts a child that would outlive it
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open("child.pid", "w").write(str(child.pid))
    while True:
        time.sleep(0.05)
if mode == "wait":                      # until the test says go, or the farm says stop
    while not os.path.exists("go"):
        if os.path.exists(job["cancel"]):
            sys.exit(0)
        time.sleep(0.05)
seq = job.get("input")
frames = range(seq["first"], seq["last"] + 1) if seq else range(1, 4)
for i, n in enumerate(frames, 1):
    dst = os.path.join(job["output_dir"], f"{job['frames_prefix']}_{i:05d}.png")
    if seq:
        shutil.copyfile(os.path.join(seq["directory"], seq["filename"] % n), dst)
    else:
        open(dst, "wb").write(b"png")
    event(progress=i / len(frames), stage=f"frame {i}/{len(frames)}")
'''


def definition(root: Path, name: str, **extra) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    p = {"id": name, "label": name.title(),
         "params": {"mode": {"type": "choice", "choices": ["ok", "fail", "error", "hang", "spawn", "wait"],
                             "default": "ok"}},
         "writes": {"frames": {"ext": "png"}},
         "runner": {"type": "process", "environment": "py", "command": ["python", "{definition}/run.py"]}}
    for k, v in extra.items():
        if k == "runner":
            p["runner"].update(v)
        else:
            p[k] = v
    (folder / "process.yaml").write_text(json.dumps(p, indent=2), encoding="utf-8")
    (folder / "run.py").write_text(RUN_PY, encoding="utf-8")
    return folder


def gone(pid: int) -> bool:
    """Not running (a zombie waiting to be collected counts as gone)."""
    stat = Path(f"/proc/{pid}/stat")
    if sys.platform.startswith("linux"):
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return True
    return not pid_alive(pid)


class Environment(unittest.TestCase):
    """A temp repo's environments/ and workflows/, and a machine with environment `py` built."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.envs = cls.tmp / "environments"
        cls.builds = cls.tmp / "machine" / "envs"
        make(cls.envs, "py", deps=("numpy==2.5.2",), check=["python", "-c", "import numpy"])
        patch = mock.patch.object(ES, "ENVS", cls.envs)
        patch.start()
        cls.addClassCleanup(patch.stop)
        with mock.patch.dict(os.environ, {"FAKE_BUILDER_LOG": ""}):
            spec = ES.load("py")
            EB.lock(spec, UV, cls.builds)
            EB.install(ES.load("py"), cls.builds, UV, log=lambda _m: None)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.workflows = self.root / "workflows"
        definition(self.workflows, "demo")
        definition(self.workflows, "slow", limits={"max_minutes": 0.02})       # 1.2 s


class Definitions(Environment):
    def test_it_loads_with_its_runner_block_and_a_fingerprint_of_its_code(self):
        wf = ProcessDefinition(self.workflows / "demo")
        self.assertEqual((wf.runner_type, wf.environment, wf.template), ("process", "py", None))
        self.assertEqual((wf.frames_ext, wf.wiring_problems, wf.problems), ("png", [], []))
        run = self.workflows / "demo" / "run.py"
        run.write_bytes(RUN_PY.encode())                                # bytes: write_text on Windows writes CRLF
        first = ProcessDefinition(self.workflows / "demo").digest
        run.write_bytes(RUN_PY.encode().replace(b"\n", b"\r\n"))       # a Windows checkout
        self.assertEqual(ProcessDefinition(self.workflows / "demo").digest, first)
        run.write_text(RUN_PY + "\n# changed\n", encoding="utf-8")
        self.assertNotEqual(ProcessDefinition(self.workflows / "demo").digest, first)
        (self.workflows / "demo" / "__pycache__").mkdir()
        (self.workflows / "demo" / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"x")
        self.assertEqual(ProcessDefinition(self.workflows / "demo").digest,
                         ProcessDefinition(self.workflows / "demo").digest)

    def test_its_command_is_filled_in_for_this_machine(self):
        definition(self.workflows, "args", runner={"command": ["python", "{definition}/run.py", "--job", "{job}",
                                                               "--out={run}/x"]})
        wf = ProcessDefinition(self.workflows / "args")
        run = self.root / "runs" / "a1"
        self.assertEqual(wf.argv(Path("/env/bin/python"), run),
                         [str(Path("/env/bin/python")), f"{self.workflows / 'args'}/run.py", "--job",
                          str(run / "job.json"), f"--out={run}/x"])
        self.assertEqual(wf.seconds(60), 3600)
        self.assertAlmostEqual(ProcessDefinition(self.workflows / "slow").seconds(60), 1.2)

    def test_what_is_wrong_with_its_block_is_the_same_everywhere(self):
        definition(self.workflows, "noenv", runner={"environment": "nope"})
        definition(self.workflows, "badcmd", runner={"command": "python run.py"})
        definition(self.workflows, "badtoken", runner={"command": ["python", "{script}"]})
        for name, said in (("noenv", "runner.environment: no environment nope"),
                           ("badcmd", "runner.command: needs to be a list of words"),
                           ("badtoken", "{script} isn't something the farm fills in")):
            with self.subTest(name):
                self.assertIn(said, " | ".join(ProcessDefinition(self.workflows / name).wiring_problems))

    def test_each_registry_reads_its_own(self):
        repo = Path(__file__).resolve().parent.parent
        shutil.copytree(repo / "workflows" / "wan22_i2i", self.workflows / "wan22_i2i")
        self.assertEqual(sorted(ProcessRegistry(self.workflows).workflows), ["demo", "slow"])
        heads = DefinitionRegistry(self.workflows)              # the manager's: every runner's
        self.assertEqual(sorted(heads.workflows), ["demo", "slow", "wan22_i2i"])
        self.assertEqual((heads.workflows["demo"].runner_type, heads.workflows["wan22_i2i"].runner_type),
                         ("process", "comfyui"))
        self.assertEqual(heads.workflows["demo"].digest, ProcessRegistry(self.workflows).workflows["demo"].digest)


class Supervisor(unittest.TestCase):
    def setUp(self):
        self.run = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run, True)

    def supervise(self, command, seconds=None, wait=True):
        (self.run / "run.json").write_text(json.dumps({"command": command, "seconds": seconds, "grace": 0.5}))
        p = subprocess.Popen([PYTHON, "-I", str(PR.__file__), str(self.run)], cwd=str(self.run))
        self.addCleanup(lambda: p.poll() is None and p.kill())
        if wait:
            p.wait(30)
        return p

    def test_it_runs_the_command_and_says_how_it_ended(self):
        self.supervise([PYTHON, "-c", "print('hi'); raise SystemExit(4)"])
        ended = json.loads((self.run / "exit.json").read_text())
        self.assertEqual((ended["reason"], ended["code"]), ("exited", 4))
        self.assertIn("hi", (self.run / "log.txt").read_text())
        self.assertEqual(json.loads((self.run / "state.json").read_text())["state"], "running")
        self.assertFalse(PR.alive(self.run))

    def test_a_command_that_wont_start(self):
        self.supervise([str(self.run / "no-such-program")])
        ended = json.loads((self.run / "exit.json").read_text())
        self.assertEqual(ended["reason"], "not_started")
        self.assertIn("no-such-program", ended["error"])

    def test_one_supervisor_a_run_and_its_lock_shows_it_lives(self):
        p = self.supervise([PYTHON, "-c", "import time; time.sleep(30)"], wait=False)
        end = time.time() + 10
        while not (self.run / "state.json").exists() and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(PR.alive(self.run))
        second = subprocess.run([PYTHON, "-I", str(PR.__file__), str(self.run)], capture_output=True, text=True)
        self.assertEqual(second.returncode, 3, second.stderr)
        (self.run / "cancel").touch()
        p.wait(30)
        self.assertEqual(json.loads((self.run / "exit.json").read_text())["reason"], "cancelled")
        self.assertFalse(PR.alive(self.run))

    def test_the_time_limit_is_kept_here(self):
        t0 = time.time()
        self.supervise([PYTHON, "-c", "import time; time.sleep(60)"], seconds=0.5)
        ended = json.loads((self.run / "exit.json").read_text())
        self.assertEqual(ended["reason"], "timeout")
        self.assertLess(time.time() - t0, 20)


class FakeHost:
    """What the agent does for a runner (base.Host), written down."""
    name, registered, tags = "gpu1", True, ["linux"]

    def __init__(self, root: Path):
        self.view = W.View(mappings=[], scratch=str(root / "scratch"), repo="")
        self.said: list = []
        self.held: list = []

    def runs(self):
        return list(self.held)

    def spawn(self, coro):
        asyncio.ensure_future(coro)

    async def send_nodes(self):
        self.said.append(("nodes",))

    async def run_started(self, h):
        h.status = "running"
        self.said.append(("started",))

    async def run_progress(self, h, stage=None, force=False):
        self.said.append(("progress", round(h.rt.progress(), 2), stage))

    async def run_succeeded(self, h, result=None):
        h.status = "postprocessing"
        self.said.append(("done", result))

    async def run_failed(self, h, message, traceback=None):
        h.finished = True
        self.said.append(("failed", message, traceback))

    async def run_interrupted(self, h):
        h.finished = True
        self.said.append(("interrupted",))

    async def run_lost(self, h, reason):
        h.finished = True
        self.said.append(("lost", reason))


@unittest.skipIf(Held is None, "the agent needs httpx and websockets")
class Runs(Environment):
    def setUp(self):
        super().setUp()
        self.out = self.root / "out"
        self.out.mkdir()
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.addCleanup(self.drain)
        self.host = FakeHost(self.root)
        self.runner = self.make_runner()

    def make_runner(self, enabled=True):
        cfg = AgentConfig(name="gpu1", base_dir=self.root, envs={"dir": str(self.builds)},
                          process_runner={"enabled": enabled, "runs_dir": str(self.root / "runs"),
                                          "grace_seconds": 0.5})
        r = ProcessRunner(cfg, registry=ProcessRegistry(self.workflows), tick=0.05)
        r.attach(self.host)
        return r

    def wait(self, coro):
        return self.loop.run_until_complete(coro)

    def drain(self):
        """What the runner left going (collecting its supervisors), before the loop closes."""
        for h in self.host.held:
            if h.rt is not None and h.rt.proc is not None and h.rt.proc.poll() is None:
                h.rt.proc.kill()
        pending = asyncio.all_tasks(self.loop)
        if pending:
            self.loop.run_until_complete(asyncio.wait(pending, timeout=30))

    def held(self, process="demo", mode="ok", digest=None, runner=None):
        runner = runner or self.runner
        wf = runner._definitions()[process]
        job = {"job": f"job-{process}-{mode}", "attempt": f"att-{process}-{mode}", "workflow": process,
               "request": {"definition": digest or wf.digest, "values": {"mode": mode}, "seq": None, "inputs": {},
                           "out_dir": str(self.out), "frames_prefix": "_raw"}}
        h = Held(job=job, attempt=job["attempt"], status="dispatched", pid=job["attempt"], runner=runner)
        self.host.held.append(h)
        return h

    def pump(self, h, until, timeout=20.0, runner=None):
        """Let the runner look at its runs until `until()` holds."""
        runner = runner or self.runner

        async def go():
            end = time.time() + timeout
            while time.time() < end:
                await runner._watch(h)
                if until():
                    return
                await asyncio.sleep(0.05)
            self.fail(f"timed out; the runner said {self.host.said}")
        self.wait(go())

    def kinds(self):
        return [s[0] for s in self.host.said]

    def test_a_run_goes_well(self):
        h = self.held()
        self.wait(self.runner.submit(h))
        self.assertEqual(h.pid, h.attempt)
        self.pump(h, lambda: "done" in self.kinds())
        self.assertEqual(self.kinds()[0], "started")
        self.assertIn(("progress", 0.99, "frame 3/3"), self.host.said)
        self.assertEqual(self.host.said[-1], ("done", {"reason": "exited", "code": 0,
                                                       "seconds": self.host.said[-1][1]["seconds"]}))
        self.assertEqual(sorted(os.listdir(self.out)), ["_raw_00001.png", "_raw_00002.png", "_raw_00003.png"])
        run = self.root / "runs" / h.attempt
        seen = json.loads((run / "seen.json").read_text())
        # In the environment's own Python, in the run's folder, without the pipeline's settings.
        build = EB.current_python(self.builds, "py")
        self.assertEqual(Path(seen["prefix"]).resolve(), build.parent.parent.resolve())
        self.assertEqual((Path(seen["cwd"]).resolve(), seen["pythonpath"]), (run.resolve(), None))
        self.assertEqual(Path(seen["here"]).resolve(), (self.workflows / "demo").resolve())
        job = seen["job"]
        self.assertEqual((job["contract"], job["process"], job["params"], job["frames_prefix"]),
                         (1, "demo", {"mode": "ok"}, "_raw"))
        self.assertEqual(Path(job["output_dir"]).resolve(), self.out.resolve())
        self.assertEqual(job["writes"], {"frames": {"ext": "png"}})
        self.assertEqual(self.runner.submitted(h)["command"][1], f"{self.workflows / 'demo'}/run.py")
        self.wait(self.runner.forget(h.pid))
        self.assertFalse(run.exists())

    def test_a_failed_command_says_why(self):
        h = self.held(mode="fail")
        self.wait(self.runner.submit(h))
        self.pump(h, lambda: "failed" in self.kinds())
        _, message, log = self.host.said[-1]
        self.assertEqual(message, "demo failed: it exited with code 3: something broke on the GPU")
        self.assertIn("something broke on the GPU", log)
        self.wait(self.runner.forget(h.pid))
        self.assertTrue((self.root / "runs" / h.attempt).exists())        # kept to look at

    def test_a_command_can_say_what_went_wrong_itself(self):
        h = self.held(mode="error")
        self.wait(self.runner.submit(h))
        self.pump(h, lambda: "failed" in self.kinds())
        self.assertEqual(self.host.said[-1][1], "demo failed: the model said no")

    def test_its_time_limit(self):
        h = self.held("slow", mode="hang")
        self.wait(self.runner.submit(h))
        self.pump(h, lambda: "failed" in self.kinds())
        self.assertIn("slow was stopped after 0.02 minutes, its time limit (limits.max_minutes", self.host.said[-1][1])
        pid = int((self.root / "runs" / h.attempt / "command.pid").read_text())
        self.assertTrue(gone(pid))

    def test_a_cancel_stops_it_and_what_it_started(self):
        h = self.held(mode="spawn")
        self.wait(self.runner.submit(h))
        run = self.root / "runs" / h.attempt
        self.pump(h, lambda: (run / "child.pid").exists())
        self.assertFalse(self.wait(self.runner.cancel(h)))         # it ends as interrupted
        self.pump(h, lambda: "interrupted" in self.kinds())
        for f in ("command.pid", "child.pid"):
            pid = int((run / f).read_text())
            end = time.time() + 10
            while not gone(pid) and time.time() < end:
                time.sleep(0.05)
            self.assertTrue(gone(pid), f)

    def test_runs_outlive_the_agent(self):
        h = self.held(mode="wait")
        self.wait(self.runner.submit(h))
        run = self.root / "runs" / h.attempt
        self.pump(h, lambda: "started" in self.kinds())
        # The agent restarts: a new runner, handed the run back by the manager.
        again = self.make_runner()
        h2 = Held(job=dict(h.job), attempt=h.attempt, status="running", pid=h.pid, runner=again)
        self.host.held.append(h2)       # h stays: its supervisor is collected when the test ends
        self.assertEqual(self.wait(again.locate(h2)), ("running", None))
        again.follow(h2)
        (run / "go").touch()
        self.pump(h2, lambda: "done" in self.kinds(), runner=again)
        self.assertEqual(self.wait(again.locate(h2))[0], "finished")
        missing = Held(job=dict(h.job), attempt="never", status="running", pid="never", runner=again)
        self.assertEqual(self.wait(again.locate(missing)), ("absent", None))

    def test_a_run_whose_supervisor_died_is_lost(self):
        h = self.held(mode="wait")
        self.wait(self.runner.submit(h))
        self.pump(h, lambda: "started" in self.kinds())
        again = self.make_runner()
        h2 = Held(job=dict(h.job), attempt=h.attempt, status="running", pid=h.pid, runner=again)
        h.rt.proc.kill()                # the supervisor and, on Linux, its command with it
        h.rt.proc.wait(10)
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(int((self.root / "runs" / h.attempt /
                                                                     "command.pid").read_text()))], capture_output=True)
        # Windows lets go of a dead process's lock when it gets round to it.
        end = time.time() + 15
        while self.wait(again.locate(h2)) != ("absent", None) and time.time() < end:
            time.sleep(0.1)
        self.assertEqual(self.wait(again.locate(h2)), ("absent", None))
        again.follow(h2)
        self.pump(h2, lambda: "lost" in self.kinds(), runner=again)

    def test_what_stops_a_run_from_starting(self):
        with self.assertRaises(RunRejected) as e:
            self.wait(self.runner.submit(self.held(digest="0000000000000000")))
        self.assertIn("differ. Update the farm's code on both", str(e.exception))
        off = self.make_runner(enabled=False)
        with self.assertRaises(RunRejected) as e:
            self.wait(off.submit(self.held(runner=off)))
        self.assertIn("The process runner is off on gpu1", str(e.exception))
        self.assertEqual(off.processes()["demo"]["problems"],
                         ["the process runner is off on gpu1 (process_runner.enabled in agent.yaml)"])

    def test_what_it_says_it_can_run(self):
        said = self.runner.processes()
        self.assertEqual(sorted(said), ["demo", "slow"])
        self.assertEqual(said["demo"], {"digest": self.runner._definitions()["demo"].digest, "problems": [],
                                        "broken": [], "expected_nodes": None})
        self.assertTrue(self.runner.knows("demo"))
        self.assertFalse(self.runner.knows("wan22_i2i"))
        # This machine without a build of the environment.
        bare = AgentConfig(name="gpu1", base_dir=self.root, envs={"dir": str(self.root / "no-envs")},
                           process_runner={"enabled": True})
        r = ProcessRunner(bare, registry=ProcessRegistry(self.workflows))
        r.attach(self.host)
        self.assertEqual(r.processes()["demo"]["problems"],
                         ["the environment py isn't built on gpu1: run `python -m comfyfarm env install py` there"])
        with self.assertRaises(RunRejected) as e:
            self.wait(r.submit(self.held(runner=r)))
        self.assertIn("The environment py isn't built on gpu1", str(e.exception))



@unittest.skipIf(Agent is None, "the agent needs httpx and websockets")
class ThroughTheFarm(Environment):
    """A process job submitted to the manager, placed on an agent that has ComfyUI and the
    process runner, run there and its frames collected, as a workflow's are."""

    def setUp(self):
        super().setUp()
        from test_agent import SEQ, FakeComfy, FlakyLink
        from test_textures import OCIO_CONFIG
        from comfyfarm.config import Config
        from comfyfarm.manager.db import JobStore
        from comfyfarm.manager.desk import Desk
        from comfyfarm.manager.events import EventBus
        from comfyfarm.manager.farm import Farm
        from comfyfarm.runners.comfyui import ComfyUIRunner
        from comfyfarm.runners.comfyui_workflow import WorkflowRegistry
        self.SEQ = SEQ
        root = str(self.root).replace("\\", "/")
        hi = Path(root, "shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi")
        hi.mkdir(parents=True)
        for f in range(1001, 1004):
            (hi / f"abc_0010.pre.v1.{f}.png").write_bytes(f"frame {f}".encode())
        os.makedirs(f"{root}/ocio")
        Path(f"{root}/ocio/config.ocio").write_text(OCIO_CONFIG)
        definition(self.workflows, "copy", params={
            "input": {"type": "input", "takes": "images", "label": "Frames", "required": True},
            "mode": {"type": "choice", "choices": ["ok", "fail", "hang"], "default": "ok"}})
        mappings = [{"posix": "/rdo/shows", "windows": f"{root}/shows"}]
        cfg = Config.model_validate({
            "database": f"{root}/farm.db", "workflows_dir": str(self.workflows), "workers": [],
            "paths": {"mappings": mappings, "output_root": f"{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"},
            "ocio": {"config": f"{root}/ocio/config.ocio"}, "convert": {"scratch_dir": f"{root}/scratch"},
            "preview": {"enabled": False}})
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.store = JobStore(Path(f"{root}/farm.db"))
        self.farm = Farm(cfg, self.store, DefinitionRegistry(self.workflows), EventBus())
        self.desk = Desk(self.farm)
        self.wait(self.farm.start())
        acfg = AgentConfig(name="gpu1", comfyui="http://gpu1:8188", max_inflight=1, base_dir=root,
                           paths={"mappings": mappings}, convert={"scratch_dir": f"{root}/scratch-gpu1"},
                           heartbeat_wait=0, retry_seconds=0.05, health_seconds=999, reconcile_seconds=999,
                           workflows_dir=str(self.workflows), envs={"dir": str(self.builds)},
                           process_runner={"enabled": True, "runs_dir": f"{root}/runs", "grace_seconds": 0.5})
        comfy = ComfyUIRunner(acfg, client=FakeComfy())
        comfy._registry = WorkflowRegistry(self.workflows)          # no ComfyUI workflow among them
        self.process = ProcessRunner(acfg, registry=ProcessRegistry(self.workflows), tick=0.05)
        self.agent = Agent(acfg, FlakyLink(self.desk), runners=[comfy, self.process])
        self.addCleanup(self.stop)

        async def ready():
            await self.agent._hello()
            comfy.stats = await comfy.client.system_stats()
            comfy.online = comfy.ws_connected = True
            await comfy.refresh_object_info()
            self.watching = asyncio.ensure_future(self.process._watch_loop())
        self.wait(ready())

    def stop(self):
        self.watching.cancel()
        self.wait(asyncio.gather(self.watching, return_exceptions=True))
        pending = [t for t in self.agent._bg if not t.done()]   # collecting supervisors that just ended
        if pending:
            self.wait(asyncio.wait(pending, timeout=30))
        self.wait(self.agent.stop())
        self.wait(self.farm.stop())
        self.store.close()

    def wait(self, coro):
        return self.loop.run_until_complete(coro)

    def until(self, pred, timeout=30.0, what="condition"):
        async def go():
            end = time.time() + timeout
            while time.time() < end:
                if pred():
                    return
                await asyncio.sleep(0.02)
            self.fail(f"timed out waiting for {what}")
        self.wait(go())

    def beat(self):
        async def go():
            await self.agent._obey(await self.agent._heartbeat())
        self.wait(go())

    def test_a_process_job_runs_and_its_frames_are_collected(self):
        self.assertEqual(self.desk.agents["gpu1"].problems, {})        # it said it can run them
        job = self.farm.submit("copy", {"input": f"{self.SEQ} 1001-1003"}, "ann")
        self.beat()
        self.until(lambda: self.store.get(job["id"])["status"] == "done", what="done")
        row = self.store.get(job["id"])
        out = row["outputs"]
        self.assertEqual((out["frames"], out["first"], out["last"]), (3, 1001, 1003))
        frames = sorted(n for n in os.listdir(row["output_dir"]) if n.endswith(".png"))
        self.assertEqual(len(frames), 3)
        self.assertEqual(Path(row["output_dir"], frames[0]).read_bytes(), b"frame 1001")
        self.assertEqual(self.agent.held, {})
        self.assertEqual(self.agent._in_comfy, 0)

    def test_a_process_job_that_fails_and_one_cancelled(self):
        job = self.farm.submit("copy", {"input": f"{self.SEQ} 1001-1003", "mode": "fail"}, "ann")
        self.beat()
        self.until(lambda: self.store.get(job["id"])["status"] == "failed", what="failed")
        self.assertIn("copy failed: it exited with code 3: something broke on the GPU",
                      self.store.get(job["id"])["error"])
        job = self.farm.submit("copy", {"input": f"{self.SEQ} 1001-1003", "mode": "hang"}, "ann")
        self.beat()
        self.until(lambda: self.store.get(job["id"])["status"] == "running", what="running")
        self.wait(self.farm.cancel(job["id"]))
        self.beat()
        self.until(lambda: self.store.get(job["id"])["status"] == "cancelled", what="cancelled")
        self.until(lambda: self.agent._in_comfy == 0, what="its place given back")


if __name__ == "__main__":
    unittest.main()
