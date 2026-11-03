"""The manager's Farm and a WorkerRunner together, in one process, against an in-memory
stand-in for ComfyUI: what passes between them (protocol.JobHost) when a job is
dispatched, runs, finishes, is cancelled, fails, or is lost by ComfyUI.

The end-to-end suite (test_e2e.py) does the same over real HTTP against tools/mock_comfyui.py.

    python -m unittest tests.test_worker -v
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_workflows import fake_object_info

from comfyfarm.config import Config, WorkerConfig
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.workflows import WorkflowRegistry

try:   # the worker talks to ComfyUI with httpx and websockets
    from comfyfarm.agent.worker import WorkerRunner
except ImportError:   # pragma: no cover
    WorkerRunner = None

REPO = Path(__file__).resolve().parent.parent
SEQ = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/abc_0010.pre.v1.%04d.png"


class FakeComfy:
    """ComfyUI's HTTP API as the worker uses it, in memory. Tests move prompts along by
    hand: put them in running/pending, write history, send websocket messages."""

    def __init__(self, object_info: dict):
        self.oi = object_info
        self.prompts: dict[str, dict] = {}
        self.running: set[str] = set()
        self.pending: set[str] = set()
        self.hist: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.interrupted: list[str] = []

    async def system_stats(self):
        return {"system": {"comfyui_version": "test"},
                "devices": [{"name": "cuda:0 fake", "vram_total": 1, "vram_free": 1}]}

    async def object_info(self):
        return self.oi

    async def submit(self, prompt, prompt_id, client_id, extra=None):
        self.prompts[prompt_id] = prompt
        self.pending.add(prompt_id)
        return prompt_id, {}

    async def queue(self):
        return set(self.running), set(self.pending)

    async def history(self, prompt_id):
        return self.hist.get(prompt_id)

    async def delete_history(self, prompt_ids):
        for p in prompt_ids:
            self.hist.pop(p, None)

    async def delete_queued(self, prompt_ids):
        self.deleted += prompt_ids
        self.pending -= set(prompt_ids)

    async def interrupt(self, prompt_id):
        self.interrupted.append(prompt_id)

    async def download(self, filename, subfolder, kind, dest):
        Path(dest).write_bytes(b"mp4")

    async def close(self):
        pass


@unittest.skipIf(WorkerRunner is None, "needs the worker's dependencies (httpx, websockets)")
class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        root = self.tmp.replace("\\", "/")
        plates = Path(root, "shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi")
        plates.mkdir(parents=True)
        for f in range(1001, 1004):
            (plates / f"abc_0010.pre.v1.{f}.png").write_bytes(b"x")
        cfg = Config.model_validate({
            "database": f"{root}/farm.db", "workflows_dir": str(REPO / "workflows"), "workers": [],
            "paths": {"mappings": [{"posix": "/rdo/shows", "windows": f"{root}/shows"}],
                      "output_root": f"{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"},
            "convert": {"scratch_dir": f"{root}/scratch"},
        })
        registry = WorkflowRegistry(REPO / "workflows")
        self.store = JobStore(Path(f"{root}/farm.db"))
        self.farm = Farm(cfg, self.store, registry, EventBus())
        self.worker = WorkerRunner(WorkerConfig(name="gpu1", url="http://fake:8188"), cfg, self.farm)
        installed = {}   # every node pack and model both workflows use
        for wf in registry.workflows.values():
            installed.update(fake_object_info(wf.template))
        self.comfy = FakeComfy(installed)
        self.worker.client = self.comfy
        self.farm.attach(self.worker)
        self.worker.online = self.worker.ws_connected = True
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.wait(self.worker.refresh_object_info())   # the preflight check runs against the manager's manifests

    def wait(self, coro):
        return self.loop.run_until_complete(coro)

    def message(self, kind: str, **data):
        self.wait(self.worker._on_message({"type": kind, "data": data}))

    def settle(self):
        """Let the worker's background tasks (collecting outputs) finish."""
        while self.worker._bg:
            self.wait(asyncio.gather(*list(self.worker._bg)))

    def dispatched(self, rng="1001-1003"):
        job = self.farm.submit("ltx25_alpha_gen", {"input": f"{SEQ} {rng}"}, "ann")
        self.wait(self.farm._dispatch_once())
        job = self.store.get(job["id"])
        self.assertEqual((job["status"], job["worker"]), ("dispatched", "gpu1"))
        return job

    def test_preflight_passes_with_everything_installed(self):
        self.assertEqual(self.worker.problems, {})
        self.assertTrue(self.worker.checked)
        self.worker.stats = self.wait(self.comfy.system_stats())   # what the health loop reads
        self.assertEqual(self.worker.summary()["gpu"]["name"], "cuda:0 fake")

    def test_preflight_names_a_missing_node_pack(self):
        del self.comfy.oi["ChordLoadModel"]
        self.wait(self.farm.check_workers())
        self.assertEqual(list(self.worker.problems), ["chord_pbr"])
        self.assertIn("ChordLoadModel is not installed", self.worker.problems["chord_pbr"][0])

    def test_job_runs_and_frames_are_renamed(self):
        job = self.dispatched()
        pid = job["prompt_id"]
        self.assertEqual(self.worker.summary()["inflight"], 1)
        prompt = self.comfy.prompts[pid]
        self.assertTrue(prompt["9104"]["inputs"]["directory"].endswith("/main/hi/"))   # the loader reads the plate

        self.message("execution_start", prompt_id=pid)
        self.assertEqual(self.store.get(job["id"])["status"], "running")
        self.message("executing", node="9104", prompt_id=pid)
        self.assertIn("stage", self.store.get(job["id"]))

        info = self.store.get(job["id"])["info"]
        for i in range(1, 4):   # what the save node writes
            Path(info["output_dir"], f"{info['frames_prefix']}_{i:04d}.png").write_bytes(b"png")
        self.comfy.hist[pid] = {"outputs": {}, "status": {"status_str": "success", "completed": True}}
        self.message("execution_success", prompt_id=pid)
        self.settle()

        done = self.store.get(job["id"])
        self.assertEqual(done["status"], "done", done["error"])
        out = done["outputs"]
        self.assertEqual((out["frames"], out["first"], out["last"]), (3, 1001, 1003))
        self.assertEqual(sorted(os.listdir(info["output_dir"])),
                         [f"abc_0010.pre.v1.alpha_v001.{f}.png" for f in (1001, 1002, 1003)])
        self.assertTrue(done["outputs"]["frame_pattern_posix"].startswith("/rdo/shows/xyz/_project_ref/"))
        self.assertNotIn(pid, self.comfy.hist)      # ComfyUI's history entry dropped
        self.assertNotIn(pid, self.worker.runtime)

    def test_cancel_while_waiting_in_comfyui(self):
        job = self.dispatched()
        j = self.wait(self.farm.cancel(job["id"]))
        self.assertEqual(j["status"], "cancelled")
        self.assertEqual(self.comfy.deleted, [job["prompt_id"]])

    def test_cancel_while_running_waits_for_the_interrupt(self):
        job = self.dispatched()
        pid = job["prompt_id"]
        self.comfy.pending.discard(pid)
        self.comfy.running.add(pid)
        self.message("execution_start", prompt_id=pid)
        j = self.wait(self.farm.cancel(job["id"]))
        self.assertEqual((j["status"], j["stage"], self.comfy.interrupted), ("running", "cancelling", [pid]))
        self.message("execution_interrupted", prompt_id=pid)
        self.assertEqual(self.store.get(job["id"])["status"], "cancelled")

    def test_node_error_fails_the_job_with_its_traceback(self):
        job = self.dispatched()
        pid = job["prompt_id"]
        self.message("execution_start", prompt_id=pid)
        self.message("execution_error", prompt_id=pid, node_id="9104", node_type="VHS_LoadImagesPath",
                     exception_message="No images", traceback=["Traceback: boom\n"])
        self.settle()
        failed = self.store.get(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("VHS_LoadImagesPath (node 9104", failed["error"])
        self.assertEqual(failed["info"]["traceback"], "Traceback: boom\n")
        self.assertNotIn(pid, self.worker.runtime)

    def test_job_comfyui_lost_is_requeued_then_given_up(self):
        job = self.dispatched()
        self.store.update(job["id"], dispatched_at=job["dispatched_at"] - 10)
        self.comfy.pending.clear()                   # ComfyUI restarted: no queue, no history
        self.wait(self.worker._reconcile())
        j = self.store.get(job["id"])
        self.assertEqual((j["status"], j["worker"], j["attempts"]), ("queued", None, 1))
        self.assertIn("lost the job", j["warnings"][-1])
        self.assertIn(job["prompt_id"], self.worker.abandoned)

        self.wait(self.farm._dispatch_once())         # second attempt, new prompt id
        j = self.store.get(job["id"])
        self.assertEqual((j["status"], j["attempts"]), ("dispatched", 2))
        self.assertNotEqual(j["prompt_id"], job["prompt_id"])
        self.store.update(job["id"], dispatched_at=j["dispatched_at"] - 10)
        self.comfy.pending.clear()
        self.wait(self.worker._reconcile())
        self.assertEqual(self.store.get(job["id"])["status"], "failed")   # farm.max_attempts is 2


if __name__ == "__main__":
    unittest.main()
