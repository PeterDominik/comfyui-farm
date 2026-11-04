"""End-to-end with the farm split over processes, as on several machines: the manager
(`comfyfarm manager`) and two agents (`comfyfarm agent`), each with its own mock ComfyUI,
over HTTP with an agent token, driven through the artist client.

gpu-b sees show storage under another folder (a junction on Windows, a symlink elsewhere),
as a machine with another mount would, and each agent has its own scratch folder. Covers
jobs spread over both, inputs converted on the agent that runs them, cancels, an agent
restarting, an agent going away for good, and the manager restarting.

    python -m unittest tests.test_e2e_agents -v
"""

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "client"))
farm = importlib.import_module("comfyfarm_client")
sys.path.insert(0, str(REPO / "tests"))
from test_agent import link_dir
from test_e2e import free_port, wait_http

SEQ = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/abc_0010.pre.v1.%04d.png"
EXR = "/rdo/shows/xyz/.published/abc/abc_0010/plate/v1/abc_0010.plate.v1.%04d.exr"
TOKEN = "e2e-agent-token"
LEASE = 15          # farm.lost_after_seconds: an agent that stops checking in loses its jobs after this
AGENTS = ("gpu-a", "gpu-b")


class AgentsE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="comfyfarm-agents-"))
        root = cls.tmp.as_posix()
        hi = cls.tmp / "shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi"
        hi.mkdir(parents=True)
        for f in range(1001, 1013):
            (hi / f"abc_0010.pre.v1.{f}.png").write_bytes(b"x")
        plate = cls.tmp / "shows/xyz/.published/abc/abc_0010/plate/v1"
        plate.mkdir(parents=True)
        for f in range(1001, 1005):
            (plate / f"abc_0010.plate.v1.{f}.exr").write_bytes(b"x")
        (cls.tmp / "ocio").mkdir()
        (cls.tmp / "ocio/config.ocio").write_text(
            "ocio_profile_version: 2\nroles:\n  scene_linear: ACES - ACEScg\n  color_picking: Output - sRGB\n")
        link_dir(cls.tmp / "shows", cls.tmp / "mnt/shows")
        shows = {"gpu-a": f"{root}/shows", "gpu-b": f"{root}/mnt/shows"}   # each agent's view
        cls.port = free_port()
        cls.comfy_ports = {name: free_port() for name in AGENTS}
        oiiotool = f'["{Path(sys.executable).as_posix()}", "{(REPO / "tests/fake_oiiotool.py").as_posix()}"]'
        (cls.tmp / "config.yaml").write_text(f"""
server: {{host: 127.0.0.1, port: {cls.port}}}
database: {root}/manager/farm.db
log_dir: {root}/logs
workflows_dir: {REPO.as_posix()}/workflows
workers: []
agents: {{token: {TOKEN}}}
paths:
  mappings: [{{posix: /rdo/shows, windows: "{root}/shows"}}]
  output_root: "{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"
farm: {{lost_after_seconds: {LEASE}}}
ocio: {{config: "{root}/ocio/config.ocio"}}
convert: {{scratch_dir: {root}/manager/scratch, tools: {{oiiotool: {oiiotool}}}}}
""")
        for name in AGENTS:
            (cls.tmp / f"{name}.yaml").write_text(f"""
name: {name}
manager: http://127.0.0.1:{cls.port}
token: {TOKEN}
comfyui: http://127.0.0.1:{cls.comfy_ports[name]}
max_inflight: 1
log_dir: {root}/logs
paths:
  mappings: [{{posix: /rdo/shows, windows: "{shows[name]}"}}]
convert: {{scratch_dir: {root}/{name}/scratch, frame_workers: 2, tools: {{oiiotool: {oiiotool}}}}}
heartbeat_wait: 2
retry_seconds: 0.5
health_seconds: 0.5
reconcile_seconds: 1.5
delete_comfy_history: false
""")
        os.environ["FAKE_OIIOTOOL_LOG"] = str(cls.tmp / "oiiotool.log")
        os.environ["COMFYFARM_URL"] = f"http://127.0.0.1:{cls.port}"
        cls.comfy, cls.agents, cls.manager = {}, {}, None
        for name in AGENTS:
            cls.start_comfy(name)
        cls.start_manager()
        for name in AGENTS:
            cls.start_agent(name)
        cls.wait_agents_ready()

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("FAKE_OIIOTOOL_LOG", None)
        for p in [*cls.agents.values(), cls.manager, *cls.comfy.values()]:
            if p and p.poll() is None:
                p.terminate()
                p.wait(10)
        if os.environ.get("KEEP_E2E"):
            print(f"\nkept {cls.tmp}")
        else:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ process control
    @classmethod
    def start_comfy(cls, name):
        with open(cls.tmp / f"mock-{name}.log", "a") as log:
            cls.comfy[name] = subprocess.Popen(
                [sys.executable, str(REPO / "tools/mock_comfyui.py"), "--port", str(cls.comfy_ports[name]),
                 "--output", str(cls.tmp / f"comfy-output-{name}"), "--delay", "0.06"],
                stdout=log, stderr=subprocess.STDOUT)
        wait_http(f"http://127.0.0.1:{cls.comfy_ports[name]}/system_stats")

    @classmethod
    def start_manager(cls):
        with open(cls.tmp / "manager.log", "a") as log:
            cls.manager = subprocess.Popen(
                [sys.executable, "-m", "comfyfarm", "--config", str(cls.tmp / "config.yaml"), "manager"],
                cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT)
        wait_http(f"http://127.0.0.1:{cls.port}/api/v1/health")

    @classmethod
    def start_agent(cls, name):
        with open(cls.tmp / f"agent-{name}.out", "a") as log:
            cls.agents[name] = subprocess.Popen(
                [sys.executable, "-m", "comfyfarm", "agent", "--config", str(cls.tmp / f"{name}.yaml")],
                cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT)

    @classmethod
    def kill_agent(cls, name):
        cls.agents[name].kill()     # a crash, not a clean stop
        cls.agents[name].wait(10)

    @classmethod
    def wait_agents_ready(cls, names=AGENTS, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            ws = {w["name"]: w for w in farm.workers()}
            if all(n in ws and ws[n]["online"] and ws[n]["connected"] and ws[n]["checked"] for n in names):
                return ws
            time.sleep(0.2)
        raise RuntimeError(f"agents never became ready: {farm.workers()}")

    # ------------------------------------------------------------ helpers
    def submit(self, rng="1001-1004", inp=SEQ):
        return farm.submit("ltx25_alpha_gen", {"input": f"{inp} {rng}"})

    def wait_for(self, job_id, statuses, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            j = farm.get(job_id)
            if j["status"] in statuses:
                return j
            time.sleep(0.15)
        self.fail(f"job {job_id[:8]} stuck in {farm.get(job_id)['status']}, wanted {statuses}")

    def comfy_prompt(self, job):
        """The prompt as the agent sent it to its ComfyUI (agents keep ComfyUI's history here)."""
        url = f"http://127.0.0.1:{self.comfy_ports[job['worker']]}/history/{job['prompt_id']}"
        with urllib.request.urlopen(url, timeout=5) as r:
            entry = json.loads(r.read())[job["prompt_id"]]
        return entry["prompt"][2], entry["status"]

    # ------------------------------------------------------------ tests
    def test_01_jobs_spread_over_both_agents_each_with_its_paths(self):
        jobs = [self.submit() for _ in range(4)]
        done = [self.wait_for(j["id"], ("done", "failed")) for j in jobs]
        for d in done:
            self.assertEqual(d["status"], "done", d.get("error"))
            self.assertEqual((d["outputs"]["frames"], d["attempts"]), (4, 1))
            prompt, _ = self.comfy_prompt(d)
            # The agent's own view of show storage in what it gave its ComfyUI (gpu-b's is
            # under mnt/)...
            for path in (prompt["9104"]["inputs"]["directory"], prompt["9109"]["inputs"]["output_path"]):
                self.assertIn("/shows/xyz/", path)
                self.assertEqual("/mnt/shows/" in path, d["worker"] == "gpu-b", path)
            # ...and the manager's in what the artist gets back.
            pattern = d["outputs"]["frame_pattern"]
            self.assertNotIn("/mnt/", pattern)
            self.assertTrue(d["outputs"]["frame_pattern_posix"].startswith("/rdo/shows/xyz/"))
            self.assertTrue(all(os.path.isfile(pattern % f) for f in range(1001, 1005)), pattern)
        self.assertEqual({d["worker"] for d in done}, set(AGENTS))
        ws = {w["name"]: w for w in farm.workers()}
        self.assertEqual(ws["gpu-b"]["agent_version"], ws["gpu-a"]["agent_version"])
        self.assertEqual(farm.health()["workers_online"], 2)

    def test_02_exr_converted_on_the_agent_that_runs_it(self):
        job = self.submit(inp=EXR)
        self.assertEqual(job["status"], "queued")
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        prompt, _ = self.comfy_prompt(done)
        scratch = prompt["9104"]["inputs"]["directory"]
        self.assertTrue(scratch.endswith(f"/{done['worker']}/scratch/{job['id']}/"), scratch)   # its own
        calls = [json.loads(line) for line in (self.tmp / "oiiotool.log").read_text().splitlines()]
        written = [c["args"][-1] for c in calls if job["id"] in c["args"][-1]]
        self.assertEqual(len(written), 4)
        self.assertTrue(all(w.startswith(scratch) for w in written), written)
        self.assertFalse(os.path.exists(scratch), "scratch frames should be cleaned up")
        self.assertFalse((self.tmp / "manager/scratch" / job["id"]).exists())   # nothing on the manager

    def test_03_cancel_running_and_queued(self):
        job = self.submit("1001-1012")
        running = self.wait_for(job["id"], ("running",))
        j = farm.cancel(job["id"])
        self.assertEqual((j["status"], j["stage"]), ("running", "cancelling"))
        self.assertEqual(self.wait_for(job["id"], ("cancelled", "done", "failed"))["status"], "cancelled")
        _, status = self.comfy_prompt(running)
        self.assertEqual(status["messages"][-1][0], "execution_interrupted")
        jobs = [self.submit() for _ in range(5)]
        self.assertEqual(farm.cancel(jobs[-1]["id"])["status"], "cancelled")   # not in ComfyUI yet: at once
        for j in jobs[:-1]:
            self.assertEqual(self.wait_for(j["id"], ("done", "failed"))["status"], "done")
        self.assertEqual(farm.get(jobs[-1]["id"])["status"], "cancelled")

    def test_04_agent_restart_picks_its_run_up_again(self):
        job = self.submit("1001-1012")
        running = self.wait_for(job["id"], ("running",))
        self.kill_agent(running["worker"])      # ComfyUI carries on
        self.start_agent(running["worker"])
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual((done["status"], done["worker"], done["attempts"]), ("done", running["worker"], 1),
                         done.get("error"))
        self.assertEqual(done["outputs"]["frames"], 12)
        self.wait_agents_ready()

    def test_05_jobs_of_an_agent_gone_for_good_go_to_the_other(self):
        job = self.submit("1001-1012")
        running = self.wait_for(job["id"], ("running",))
        gone = running["worker"]
        other = next(n for n in AGENTS if n != gone)
        self.kill_agent(gone)
        try:
            done = self.wait_for(job["id"], ("done", "failed"), timeout=LEASE + 40)
            self.assertEqual((done["status"], done["worker"], done["attempts"]), ("done", other, 2),
                             done.get("error"))
            self.assertTrue(any("stopped checking in" in w for w in done["warnings"]), done["warnings"])
            self.assertEqual(done["outputs"]["frames"], 12)
        finally:
            self.start_agent(gone)
            self.wait_agents_ready()

    def test_06_manager_restart_mid_job(self):
        job = self.submit("1001-1012")
        self.wait_for(job["id"], ("running",))
        self.manager.terminate()
        self.manager.wait(15)
        time.sleep(5)       # the job finishes in ComfyUI meanwhile; its agent keeps the reports
        self.start_manager()
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual((done["status"], done["attempts"]), ("done", 1), done.get("error"))
        self.assertEqual(done["outputs"]["frames"], 12)
        self.wait_agents_ready()

    def test_07_agents_need_the_token(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/v1/agent/hello", method="POST",
                                     data=json.dumps({"name": "intruder"}).encode(),
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 401)
        self.assertNotIn("intruder", {w["name"] for w in farm.workers()})


if __name__ == "__main__":
    unittest.main(verbosity=2)
