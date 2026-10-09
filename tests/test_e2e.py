"""End-to-end: real gateway + mock ComfyUI as subprocesses, driven through the
artist client. Covers the happy path, cancels, failures, restarts of either side,
and a missing node pack.

    python -m unittest tests.test_e2e -v
"""

import importlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "client"))
farm = importlib.import_module("comfyfarm_client")

SEQ = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/abc_0010.pre.v1.%04d.png"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    raise RuntimeError(f"{url} never came up")


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="comfyfarm-e2e-"))
        plates = cls.tmp / "shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi"
        plates.mkdir(parents=True)
        for f in range(1001, 1013):
            (plates / f"abc_0010.pre.v1.{f}.png").write_bytes(b"x")
        cls.comfy_port, cls.gw_port = free_port(), free_port()
        root = cls.tmp.as_posix()
        (cls.tmp / "config.yaml").write_text(f"""
server: {{host: 127.0.0.1, port: {cls.gw_port}}}
database: {root}/data/farm.db
log_dir: {root}/logs
workflows_dir: {REPO.as_posix()}/workflows
workers: [{{name: mock, url: "http://127.0.0.1:{cls.comfy_port}", max_inflight: 2}}]
paths:
  mappings: [{{posix: /rdo/shows, windows: "{root}/shows"}}]
  output_root: "{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"
farm: {{reconcile_seconds: 1.5, health_seconds: 0.5, lost_after_seconds: 60}}
""")
        os.environ["COMFYFARM_URL"] = f"http://127.0.0.1:{cls.gw_port}"
        cls.comfy = cls.gw = None
        cls.start_comfy()
        cls.start_gateway()

    @classmethod
    def tearDownClass(cls):
        for p in (cls.gw, cls.comfy):
            if p and p.poll() is None:
                p.terminate()
                p.wait(10)
        if os.environ.get("KEEP_E2E"):
            print(f"\nkept {cls.tmp}")
        else:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ process control
    @classmethod
    def start_comfy(cls, *extra):
        with open(cls.tmp / "mock.log", "a") as log:
            cls.comfy = subprocess.Popen(
                [sys.executable, str(REPO / "tools/mock_comfyui.py"), "--port", str(cls.comfy_port),
                 "--output", str(cls.tmp / "comfy_output"), "--delay", "0.06", *extra],
                stdout=log, stderr=subprocess.STDOUT)
        wait_http(f"http://127.0.0.1:{cls.comfy_port}/system_stats")

    @classmethod
    def stop_comfy(cls):
        cls.comfy.send_signal(signal.SIGKILL)   # a crash, not a clean shutdown
        cls.comfy.wait(10)

    @classmethod
    def start_gateway(cls):
        with open(cls.tmp / "gateway.log", "a") as log:
            cls.gw = subprocess.Popen(
                [sys.executable, "-m", "comfyfarm", "--config", str(cls.tmp / "config.yaml"), "serve"],
                cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT)
        wait_http(f"http://127.0.0.1:{cls.gw_port}/api/v1/health")
        cls.wait_worker_ready()

    @classmethod
    def wait_worker_ready(cls, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            ws = farm._request("GET", "/api/v1/workers")["workers"]
            if ws and ws[0]["online"] and ws[0]["connected"]:
                return ws[0]
            time.sleep(0.2)
        raise RuntimeError("worker never became ready")

    # ------------------------------------------------------------ helpers
    def submit(self, rng="1001-1012", **params):
        params.setdefault("input", f"{SEQ} {rng}" if rng else SEQ)
        return farm.submit("ltx25_alpha_gen", params)

    def wait_for(self, job_id, statuses, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            j = farm.get(job_id)
            if j["status"] in statuses:
                return j
            time.sleep(0.15)
        self.fail(f"job {job_id[:8]} stuck in {farm.get(job_id)['status']}, wanted {statuses}")

    def outputs(self, job):
        return sorted(os.listdir(job["info"]["output_dir"]))

    # ------------------------------------------------------------ tests
    def test_01_happy_path(self):
        job = self.submit("1003-1010", positive_prompt="clean alpha", seed=7)
        self.assertEqual(job["status"], "queued")
        self.assertEqual((job["info"]["input"]["skip"], job["info"]["input"]["cap"]), (2, 8))
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        out = done["outputs"]
        self.assertEqual((out["frames"], out["first"], out["last"]), (8, 1003, 1010))
        self.assertTrue(out["frame_pattern_posix"].startswith("/rdo/shows/xyz/_project_ref/"))
        names = self.outputs(done)
        self.assertIn("abc_0010_alpha_v001.1003.png", names)
        self.assertIn("abc_0010_alpha_v001_preview.mp4", names)
        self.assertFalse([n for n in names if "_raw_" in n], "raw frames should be renamed")
        prompt = farm._request("GET", f"/api/v1/jobs/{job['id']}/prompt")
        self.assertEqual(prompt["5516:4832"]["inputs"]["noise_seed"], 7)
        self.assertEqual(done["progress"], 1.0)

    def test_02_versions_and_cancel_waiting_jobs(self):
        a, b, c = self.submit(), self.submit(), self.submit()
        self.assertEqual([j["info"]["version"] for j in (a, b, c)],
                         [a["info"]["version"], a["info"]["version"] + 1, a["info"]["version"] + 2])
        self.wait_for(b["id"], ("dispatched", "running"))     # b sits in ComfyUI's own queue
        cb = farm.cancel(b["id"])
        cc = farm.cancel(c["id"])                             # c still in the gateway queue
        self.assertEqual(cc["status"], "cancelled")
        self.assertEqual(self.wait_for(b["id"], ("cancelled", "done", "failed"))["status"], "cancelled", cb)
        self.assertEqual(self.wait_for(a["id"], ("done", "failed"))["status"], "done")

    def test_03_cancel_running(self):
        job = self.submit()
        self.wait_for(job["id"], ("running",))
        time.sleep(0.5)
        farm.cancel(job["id"])
        self.assertEqual(self.wait_for(job["id"], ("cancelled", "done", "failed"))["status"], "cancelled")

    def test_04_failure_reports_node_and_traceback(self):
        job = self.submit(positive_prompt="MOCK_FAIL")
        failed = self.wait_for(job["id"], ("failed", "done"))
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Mock failure requested", failed["error"])
        self.assertIn("Prompt (positive)", failed["error"])
        self.assertTrue(failed["info"].get("traceback"))

    def test_05_bad_input_rejected_at_submit(self):
        with self.assertRaisesRegex(farm.FarmError, "Missing frames.*1013"):
            self.submit("1001-1014")
        with self.assertRaisesRegex(farm.FarmError, "multiple of 32"):
            self.submit(shorter_side=1000)
        with self.assertRaisesRegex(farm.FarmError, "can't read '.exr'"):
            farm.submit("ltx25_alpha_gen", {"input": "/rdo/shows/xyz/a.%04d.exr 1-2"})

    def test_06_gateway_restart_mid_job(self):
        job = self.submit()
        self.wait_for(job["id"], ("running",))
        self.gw.terminate()
        self.gw.wait(15)
        time.sleep(5)    # the job finishes in ComfyUI while the gateway is down
        self.start_gateway()
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["outputs"]["frames"], 12)

    def test_07_comfyui_crash_mid_job_requeues(self):
        job = self.submit()
        self.wait_for(job["id"], ("running",))
        self.stop_comfy()
        time.sleep(1.5)
        self.start_comfy()
        done = self.wait_for(job["id"], ("done", "failed"), timeout=60)
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["attempts"], 2)
        self.assertTrue(any("lost the job" in w for w in done["warnings"]))

    def test_08_missing_node_pack_fails_fast(self):
        self.stop_comfy()
        self.start_comfy("--drop-class", "VHS_LoadImagesPath")
        try:
            time.sleep(2.5)   # gateway notices the restart and re-reads node definitions
            worker = self.wait_worker_ready()
            self.assertIn("ltx25_alpha_gen", worker["problems"])
            job = self.submit()
            failed = self.wait_for(job["id"], ("failed", "done"))
            self.assertEqual(failed["status"], "failed")
            self.assertIn("VHS_LoadImagesPath is not installed", failed["error"])
        finally:
            self.stop_comfy()
            self.start_comfy()
            time.sleep(2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
