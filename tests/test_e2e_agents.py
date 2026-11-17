"""End-to-end with the farm split over processes, as on several machines: the manager
(`comfyfarm manager`) and two agents (`comfyfarm agent`), each with its own mock ComfyUI,
over HTTP with an agent token, driven through the artist client.

gpu-b sees show storage under another folder (a junction on Windows, a symlink elsewhere),
as a machine with another mount would, finds the manager through the manager.json in the
farm's shared folder, and starts its own ComfyUI (comfyui_launch). Each agent has its own
scratch folder. Covers jobs spread over both, inputs converted on the agent that runs
them, cancels, an agent restarting, an agent going away for good, the manager
restarting, an agent keeping its ComfyUI running, `comfyfarm agent --check`, and the
shared client finding the manager.

    python -m unittest tests.test_e2e_agents -v
"""

import importlib
import json
import os
import shutil
import signal
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
share: {{dir: "{root}/share"}}
paths:
  mappings: [{{posix: /rdo/shows, windows: "{root}/shows"}}]
  output_root: "{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"
farm: {{lost_after_seconds: {LEASE}}}
ocio: {{config: "{root}/ocio/config.ocio"}}
convert: {{scratch_dir: {root}/manager/scratch, tools: {{oiiotool: {oiiotool}}}}}
""")
        mock = [Path(sys.executable).as_posix(), (REPO / "tools/mock_comfyui.py").as_posix()]
        manager = {"gpu-a": f"http://127.0.0.1:{cls.port}", "gpu-b": f"{root}/share/manager.json"}
        launch = {"gpu-a": "", "gpu-b": f"""
comfyui_launch:
  command: {json.dumps(mock + ["--port", str(cls.comfy_ports["gpu-b"]), "--output",
                              f"{root}/comfy-output-gpu-b", "--delay", "0.06"])}
  restart_after: 2
  startup_seconds: 30"""}
        for name in AGENTS:
            (cls.tmp / f"{name}.yaml").write_text(f"""
name: {name}
manager: "{manager[name]}"
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
delete_comfy_history: false{launch[name]}
""")
        os.environ["FAKE_OIIOTOOL_LOG"] = str(cls.tmp / "oiiotool.log")
        os.environ["COMFYFARM_URL"] = f"http://127.0.0.1:{cls.port}"
        cls.comfy, cls.agents, cls.manager = {}, {}, None
        cls.start_comfy("gpu-a")            # gpu-b's agent starts its own
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
        pid = cls.launched_comfy()
        if pid:
            os.kill(pid, signal.SIGTERM)
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
    def launched_comfy(cls) -> int | None:
        """The pid of the ComfyUI gpu-b's agent started."""
        try:
            return int((cls.tmp / "logs/comfyui-gpu-b.pid").read_text())
        except (OSError, ValueError):
            return None

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
            for path in (prompt["9104"]["inputs"]["path"], prompt["9109"]["inputs"]["output_path"]):
                self.assertIn("/shows/xyz/", path)
                self.assertEqual("/mnt/shows/" in path, d["worker"] == "gpu-b", path)
            # ...and the manager's in what the artist gets back.
            pattern = d["outputs"]["frame_pattern"]
            self.assertNotIn("/mnt/", pattern)
            self.assertTrue(d["outputs"]["frame_pattern_posix"].startswith("/rdo/shows/xyz/"))
            self.assertTrue(all(os.path.isfile(pattern % f) for f in range(1001, 1005)), pattern)
            # The preview movie the agent made, at the manager's path too (what it plays).
            preview = d["outputs"].get("preview")
            self.assertTrue(preview and os.path.isfile(preview) and "/mnt/" not in preview, (preview, d["warnings"]))
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
        scratch = prompt["9104"]["inputs"]["path"].rpartition("/")[0] + "/"
        self.assertTrue(scratch.endswith(f"/{done['worker']}/scratch/{job['id']}/"), scratch)   # its own
        calls = [json.loads(line) for line in (self.tmp / "oiiotool.log").read_text().splitlines()]
        written = [c["args"][-1] for c in calls if job["id"] in c["args"][-1]]
        self.assertEqual(len(written), 4)
        self.assertTrue(all(w.startswith(scratch) for w in written), written)
        self.assertFalse(os.path.exists(scratch), "scratch frames should be cleaned up")
        self.assertFalse((self.tmp / "manager/scratch" / job["id"]).exists())   # nothing on the manager
        # Its timeline: every state it went through, with the conversion as its own step.
        events = farm._request("GET", f"/api/v1/jobs/{job['id']}/events")["events"]
        self.assertEqual(events, farm.events(job["id"]))
        names = [e["event"] for e in events]
        self.assertEqual([n for n in names if n != "stage"],
                         ["submitted", "preparing", "dispatched", "running", "postprocessing", "done"])
        steps = [e["detail"]["stage"] for e in events if e["event"] == "stage" and e["status"] == "preparing"]
        self.assertTrue(any(s_.startswith("converting EXR") for s_ in steps), events)
        self.assertEqual({e["worker"] for e in events[1:]}, {done["worker"]})
        self.assertIsNone(events[-1]["seconds"])
        self.assertTrue(all(e["seconds"] is not None for e in events[:-1]))

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

    def test_08_the_agent_keeps_its_comfyui_running(self):
        log = self.tmp / "logs/comfyui-gpu-b.log"
        self.assertIn("started by agent gpu-b", log.read_text())
        old = self.launched_comfy()
        os.kill(old, getattr(signal, "SIGKILL", signal.SIGTERM))     # ComfyUI crashes
        end = time.time() + 60
        while time.time() < end and self.launched_comfy() == old:
            time.sleep(0.2)
        self.assertNotEqual(self.launched_comfy(), old, "gpu-b's agent didn't start ComfyUI again")
        self.wait_agents_ready()
        self.assertEqual(log.read_text().count("started by agent gpu-b"), 2)
        jobs = [self.submit() for _ in range(4)]
        done = [self.wait_for(j["id"], ("done", "failed")) for j in jobs]
        self.assertEqual([d["status"] for d in done], ["done"] * 4)

    def test_09_agent_check(self):
        def check(name, *extra):
            r = subprocess.run([sys.executable, "-m", "comfyfarm", "agent", "--config", str(self.tmp / f"{name}.yaml"),
                                "--check", *extra], cwd=str(REPO), capture_output=True, text=True, timeout=120)
            return r.returncode, {line.strip() for line in r.stdout.splitlines()}, r.stdout + r.stderr

        for name in AGENTS:
            _, lines, out = check(name)
            ok = [ln for ln in lines if ln.startswith("[ ok ]")]
            self.assertTrue(any("reachable, token accepted" in ln for ln in ok), out)
            self.assertTrue(any(ln.startswith(f"[ ok ] worker {name} http://127.0.0.1:") for ln in ok), out)
            self.assertTrue(any(f"{name} has every node, input and model ltx25_alpha_gen needs" in ln for ln in ok), out)
            self.assertTrue(any(ln.startswith("[ ok ] storage /rdo/shows") for ln in ok), out)
            # [WARN] when the disk has under 20 GB free (a CI runner): the folder is still checked.
            self.assertTrue(any(" scratch " in ln and ": writable" in ln for ln in lines), out)
        self.assertTrue(any("manager.json" in ln and "token accepted" in ln for ln in check("gpu-b")[1]))
        self.assertTrue(any(ln.startswith("[ ok ] ComfyUI is started with:") for ln in check("gpu-b")[1]))
        bad = self.tmp / "bad-token.yaml"
        bad.write_text((self.tmp / "gpu-a.yaml").read_text().replace(TOKEN, "wrong"))
        code, lines, out = check("bad-token")
        self.assertEqual(code, 1, out)
        self.assertTrue(any(ln.startswith("[FAIL] manager") and "refused the agent token" in ln for ln in lines), out)

    def test_10_the_shared_client_finds_the_manager(self):
        share = self.tmp / "share"
        self.assertEqual(json.loads((share / "manager.json").read_text())["url"], f"http://127.0.0.1:{self.port}")
        env = {k: v for k, v in os.environ.items() if k not in ("COMFYFARM_URL", "COMFYFARM_ADDRESS_FILE")}
        r = subprocess.run([sys.executable, str(share / "client/comfyfarm_client.py"), "workflows"], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ltx25_alpha_gen", r.stdout)
        self.assertTrue((share / "client/nuke/install_for_me.py").is_file())

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
