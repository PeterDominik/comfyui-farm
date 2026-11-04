"""The manager and its agents in one process (manager.desk, agent.agent, agent.link.LocalLink),
against an in-memory ComfyUI per agent: taking jobs, converting inputs on the agent,
running and finishing them, cancels in each state, lost runs, restarts of either side,
leases, several agents with their own path mappings.

The end-to-end suites (test_e2e.py, test_e2e_agents.py) do the same over real HTTP
against tools/mock_comfyui.py.

    python -m unittest tests.test_agent -v
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_textures import FAKE_OIIOTOOL, OCIO_CONFIG
from test_workflows import fake_object_info

from comfyfarm.config import AgentConfig, Config
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.desk import Desk
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.protocol import LinkError
from comfyfarm.workflows import WorkflowRegistry

try:   # the agent talks to ComfyUI with httpx and websockets
    from comfyfarm.agent.agent import Agent
    from comfyfarm.agent.link import LocalLink
except ImportError:   # pragma: no cover
    Agent = None

REPO = Path(__file__).resolve().parent.parent
SEQ = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/abc_0010.pre.v1.%04d.png"
EXR = "/rdo/shows/xyz/.published/abc/abc_0010/plate/v1/abc_0010.plate.v1.%04d.exr"
REGISTRY = WorkflowRegistry(REPO / "workflows")
INSTALLED = {}   # every node pack and model the workflows use
for _wf in REGISTRY.workflows.values():
    INSTALLED.update(fake_object_info(_wf.template))


class FakeComfy:
    """ComfyUI's HTTP API as the agent uses it, in memory. Tests move prompts along by
    hand: running/pending, history, websocket messages."""

    def __init__(self, object_info: dict | None = None):
        self.oi = dict(object_info or INSTALLED)
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
        await asyncio.sleep(0)
        self.prompts[prompt_id] = prompt
        self.pending.add(prompt_id)
        return prompt_id, {}

    async def queue(self):
        await asyncio.sleep(0)   # a real request lets other tasks run meanwhile
        return set(self.running), set(self.pending)

    async def history(self, prompt_id):
        await asyncio.sleep(0)
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


class FlakyLink(LocalLink if Agent else object):
    """The desk in this process, which can be made unreachable."""

    def __init__(self, desk):
        super().__init__(desk)
        self.down = False

    def _check(self):
        if self.down:
            raise LinkError("manager down (test)")

    async def hello(self, msg):
        self._check()
        return await super().hello(msg)

    async def status(self, msg):
        self._check()
        return await super().status(msg)

    async def nodes(self, msg):
        self._check()
        return await super().nodes(msg)

    async def heartbeat(self, msg):
        self._check()
        return await super().heartbeat(msg)

    async def report(self, msg):
        self._check()
        return await super().report(msg)


def link_dir(target: Path, link: Path) -> None:
    """The same folder under a second path: what a machine with another mapping sees."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@unittest.skipIf(Agent is None, "needs the agent's dependencies (httpx, websockets)")
class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = root = self.tmp.replace("\\", "/")
        hi = Path(root, "shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi")
        hi.mkdir(parents=True)
        for f in range(1001, 1004):
            (hi / f"abc_0010.pre.v1.{f}.png").write_bytes(b"x")
        exr = Path(root, "shows/xyz/.published/abc/abc_0010/plate/v1")
        exr.mkdir(parents=True)
        for f in range(1001, 1004):
            (exr / f"abc_0010.plate.v1.{f}.exr").write_bytes(b"x")
            (exr / f"abc_0010.slow.v1.{f}.exr").write_bytes(b"x")
        os.makedirs(f"{root}/ocio")
        Path(f"{root}/ocio/config.ocio").write_text(OCIO_CONFIG)
        self.mappings = [{"posix": "/rdo/shows", "windows": f"{root}/shows"}]
        self.cfg = Config.model_validate({
            "database": f"{root}/farm.db", "workflows_dir": str(REPO / "workflows"), "workers": [],
            "paths": {"mappings": self.mappings,
                      "output_root": f"{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"},
            "ocio": {"config": f"{root}/ocio/config.ocio"},
            "convert": {"scratch_dir": f"{root}/scratch", "tools": {"oiiotool": FAKE_OIIOTOOL}},
        })
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.store = None
        self.start_manager()
        self.agents: list = []
        self.addCleanup(self.stop_all)
        self.oiiolog = f"{root}/oiiotool.log"
        os.environ["FAKE_OIIOTOOL_LOG"] = self.oiiolog
        self.addCleanup(os.environ.pop, "FAKE_OIIOTOOL_LOG", None)

    # ------------------------------------------------------------ harness
    def start_manager(self):
        """The manager, on the farm's database (a restart, if it was running)."""
        if self.store is not None:
            self.wait(self.farm.stop())
            self.store.close()
        self.store = JobStore(Path(f"{self.root}/farm.db"))
        self.farm = Farm(self.cfg, self.store, REGISTRY, EventBus())
        self.desk = Desk(self.farm)
        self.wait(self.farm.start())
        for a in getattr(self, "agents", []):
            a.link.desk = self.desk     # its next call reaches the new manager

    def stop_all(self):
        for a in self.agents:
            self.wait(a.stop())
        self.wait(self.farm.stop())
        self.store.close()

    def wait(self, coro):
        return self.loop.run_until_complete(coro)

    def agent(self, name="gpu1", mappings=None, scratch=None, max_inflight=2, comfy=None, **settings):
        settings = {"heartbeat_wait": 0, "retry_seconds": 0.05, "health_seconds": 999, "reconcile_seconds": 999,
                    **settings}
        cfg = AgentConfig(name=name, comfyui=f"http://{name}:8188", max_inflight=max_inflight, base_dir=self.root,
                          paths={"mappings": self.mappings if mappings is None else mappings},
                          convert={"scratch_dir": scratch or f"{self.root}/scratch-{name}",
                                   "tools": {"oiiotool": FAKE_OIIOTOOL}}, **settings)
        a = Agent(cfg, FlakyLink(self.desk), client=comfy or FakeComfy())
        self.agents.append(a)
        return a

    def ready(self, a):
        """Checked in, its ComfyUI up and its node definitions checked."""
        async def go():
            await a._hello()
            a.stats = await a.client.system_stats()
            a.online = a.ws_connected = True
            await a.refresh_object_info()
        self.wait(go())
        return a

    def beat(self, a):
        async def go():
            reply = await a._heartbeat()
            await a._obey(reply)
            return reply
        return self.wait(go())

    def until(self, pred, timeout=10.0, what="condition"):
        async def go():
            end = time.time() + timeout
            while time.time() < end:
                if pred():
                    return
                await asyncio.sleep(0.01)
            self.fail(f"timed out waiting for {what}")
        self.wait(go())

    def msg(self, a, kind, **data):
        self.wait(a._on_message({"type": kind, "data": data}))

    def job(self, jid):
        return self.store.get(jid)

    def submit(self, inp=f"{SEQ} 1001-1003", **params):
        return self.farm.submit("ltx25_alpha_gen", dict(params, input=inp), "ann")

    def dispatched(self, a, job=None):
        """A job taken by `a` and sent to its ComfyUI; its prompt id."""
        job = job or self.submit()
        self.beat(a)
        self.until(lambda: self.job(job["id"])["status"] == "dispatched", what="dispatched")
        return self.job(job["id"])["prompt_id"]

    def finish(self, a, jid, pid):
        """ComfyUI runs the prompt: the save node writes its frames, then success."""
        self.msg(a, "execution_start", prompt_id=pid)
        h = a.held[jid]
        info = h.job["info"]
        for i in range(1, (info.get("expected_frames") or 1) + 1):
            Path(info["output_dir"], f"{info['frames_prefix']}_{i:04d}.png").write_bytes(b"png")
        a.client.hist[pid] = {"outputs": {}, "status": {"status_str": "success", "completed": True}}
        self.msg(a, "execution_success", prompt_id=pid)
        self.until(lambda: self.job(jid)["status"] in ("done", "failed"), what="done")
        return self.job(jid)

    # ------------------------------------------------------------ tests
    def test_job_taken_run_and_collected(self):
        a = self.ready(self.agent())
        job = self.submit()
        self.assertEqual(job["status"], "queued")
        self.beat(a)
        j = self.job(job["id"])
        self.assertEqual((j["status"], j["worker"], j["attempts"]), ("preparing", "gpu1", 0))
        self.until(lambda: self.job(job["id"])["status"] == "dispatched")
        j = self.job(job["id"])
        pid = j["prompt_id"]
        self.assertEqual(j["attempts"], 1)
        prompt = a.client.prompts[pid]
        self.assertEqual(prompt["9104"]["inputs"]["directory"],
                         f"{self.root}/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/")
        self.msg(a, "execution_start", prompt_id=pid)
        self.until(lambda: self.job(job["id"])["status"] == "running")
        done = self.finish(a, job["id"], pid)
        self.assertEqual(done["status"], "done", done["error"])
        out = done["outputs"]
        self.assertEqual((out["frames"], out["first"], out["last"]), (3, 1001, 1003))
        self.assertEqual(out["frame_pattern"], f"{done['output_dir']}/abc_0010.pre.v1.alpha_v001.%04d.png")
        self.assertTrue(out["frame_pattern_posix"].startswith("/rdo/shows/xyz/_project_ref/"))
        self.assertEqual(sorted(os.listdir(done["output_dir"])),
                         [f"abc_0010.pre.v1.alpha_v001.{f}.png" for f in (1001, 1002, 1003)])
        self.assertEqual(a.held, {})
        self.assertNotIn(pid, a.client.hist)                    # ComfyUI's history entry dropped
        self.assertEqual(self.store.stats("ltx25_alpha_gen")["runs"], 1)
        w = self.desk.summaries()[0]
        self.assertEqual((w["name"], w["online"], w["connected"], w["inflight"], w["gpu"]["name"]),
                         ("gpu1", True, True, 0, "cuda:0 fake"))
        self.assertEqual(self.farm.online_workers(), 1)

    def test_an_agent_sees_the_job_through_its_own_paths(self):
        # gpu2 sees show storage under another folder (like a Windows machine's UNC path).
        link_dir(Path(self.root, "shows"), Path(self.root, "other/mount/shows"))
        mine = [{"posix": "/rdo/shows", "windows": f"{self.root}/other/mount/shows"}]
        a = self.ready(self.agent("gpu2", mappings=mine))
        job = self.submit()
        pid = self.dispatched(a, job)
        prompt = a.client.prompts[pid]
        self.assertTrue(prompt["9104"]["inputs"]["directory"].startswith(f"{self.root}/other/mount/shows/"))
        self.assertTrue(prompt["9109"]["inputs"]["output_path"].startswith(f"{self.root}/other/mount/shows/"))
        done = self.finish(a, job["id"], pid)
        self.assertEqual(done["status"], "done", done["error"])
        # The manager's view: its own paths, and the studio paths.
        self.assertTrue(done["outputs"]["frame_pattern"].startswith(f"{self.root}/shows/"))
        self.assertTrue(done["outputs"]["frame_pattern_posix"].startswith("/rdo/shows/"))
        self.assertTrue(Path(done["output_dir"], "abc_0010.pre.v1.alpha_v001.1002.png").is_file())

    def test_exr_converted_into_the_agents_scratch(self):
        a = self.ready(self.agent(scratch=f"{self.root}/agent-scratch"))
        job = self.farm.submit("ltx25_alpha_gen", {"input": f"{EXR} 1001-1003"}, "ann")
        self.assertEqual((job["status"], job["info"]["input"]["convert"]["dst_dir"]),
                         ("queued", f"{self.root}/scratch/{job['id']}/"))   # as the manager planned it
        pid = self.dispatched(a, job)
        loader = a.client.prompts[pid]["9104"]["inputs"]["directory"]
        self.assertEqual(loader, f"{self.root}/agent-scratch/{job['id']}/")
        self.assertEqual(sorted(os.listdir(loader)), [f"abc_0010.plate.v1.{f}.png" for f in (1001, 1002, 1003)])
        done = self.finish(a, job["id"], pid)
        self.assertEqual(done["status"], "done", done["error"])
        self.assertFalse(os.path.exists(loader))               # scratch cleaned up
        self.assertFalse(os.path.exists(f"{self.root}/scratch/{job['id']}"))

    def test_cancel_queued_and_preparing_at_once(self):
        a = self.ready(self.agent())
        queued = self.submit()
        self.assertEqual(self.wait(self.farm.cancel(queued["id"]))["status"], "cancelled")
        a.online = False                                       # ComfyUI down: the next job waits in preparing
        job = self.submit()
        self.beat(a)
        self.assertEqual(self.job(job["id"])["status"], "preparing")
        self.assertEqual(self.wait(self.farm.cancel(job["id"]))["status"], "cancelled")
        self.beat(a)                                           # told to drop it
        self.assertEqual(a.held, {})
        self.assertEqual(a._in_comfy, 0)
        self.assertEqual(a.client.prompts, {})

    def test_cancel_in_comfyui_waiting_and_running(self):
        a = self.ready(self.agent())
        waiting = self.submit()
        pid = self.dispatched(a, waiting)
        j = self.wait(self.farm.cancel(waiting["id"]))
        self.assertEqual((j["status"], j["stage"], j["cancel_requested"]), ("dispatched", "cancelling", 1))
        self.beat(a)
        self.until(lambda: self.job(waiting["id"])["status"] == "cancelled")
        self.assertEqual(a.client.deleted, [pid])

        running = self.submit()
        pid = self.dispatched(a, running)
        a.client.pending.discard(pid)
        a.client.running.add(pid)
        self.msg(a, "execution_start", prompt_id=pid)
        self.wait(self.farm.cancel(running["id"]))
        self.beat(a)
        self.assertEqual(a.client.interrupted, [pid])
        self.assertEqual(self.job(running["id"])["status"], "running")   # until ComfyUI says it stopped
        self.msg(a, "execution_interrupted", prompt_id=pid)
        self.until(lambda: self.job(running["id"])["status"] == "cancelled")
        self.assertEqual(a.held, {})

    def test_node_error_fails_the_job_with_its_traceback(self):
        a = self.ready(self.agent())
        job = self.submit()
        pid = self.dispatched(a, job)
        self.msg(a, "execution_start", prompt_id=pid)
        self.msg(a, "execution_error", prompt_id=pid, node_id="9104", node_type="VHS_LoadImagesPath",
                 exception_message="No images", traceback=["Traceback: boom\n"])
        self.until(lambda: self.job(job["id"])["status"] == "failed")
        failed = self.job(job["id"])
        self.assertIn("VHS_LoadImagesPath (node 9104", failed["error"])
        self.assertEqual(failed["info"]["traceback"], "Traceback: boom\n")

    def test_a_run_comfyui_lost_is_requeued_then_given_up(self):
        a = self.ready(self.agent())
        job = self.submit()
        pid = self.dispatched(a, job)
        a.held[job["id"]].dispatched_at -= 10
        a.client.pending.clear()                               # ComfyUI restarted: no queue, no history
        self.wait(a._reconcile())
        j = self.job(job["id"])
        self.assertEqual((j["status"], j["worker"], j["attempts"]), ("queued", None, 1))
        self.assertIn("lost the job", j["warnings"][-1])
        self.assertIn(pid, a.abandoned)
        pid2 = self.dispatched(a, j)                           # taken again: a new run
        self.assertNotEqual(pid2, pid)
        self.assertEqual(self.job(job["id"])["attempts"], 2)
        a.held[job["id"]].dispatched_at -= 10
        a.client.pending.clear()
        self.wait(a._reconcile())
        self.until(lambda: self.job(job["id"])["status"] == "failed")   # farm.max_attempts is 2
        self.assertIn("Gave up after 2 attempts", self.job(job["id"])["error"])

    def test_overlapping_checks_requeue_a_lost_run_once(self):
        a = self.ready(self.agent())
        job = self.submit()
        self.dispatched(a, job)
        a.held[job["id"]].dispatched_at -= 10
        a.client.pending.clear()
        calls, lost = [], self.farm.lost
        self.farm.lost = lambda j, reason: (calls.append(j["id"]), lost(j, reason))[1]

        async def both():
            await asyncio.gather(a._reconcile(), a._reconcile())
        self.wait(both())
        self.assertEqual(calls, [job["id"]])
        self.assertEqual(self.job(job["id"])["status"], "queued")

    def test_a_report_about_an_earlier_run_is_ignored(self):
        a = self.ready(self.agent())
        job = self.submit()
        pid = self.dispatched(a, job)
        stale = {"name": "gpu1", "job": job["id"], "attempt": "not-this-run", "event": "done", "data": {}}
        self.assertEqual(self.desk.report(stale), {"ok": False, "status": "dispatched"})
        self.farm.lost(self.job(job["id"]), "ComfyUI lost the job.")   # requeued...
        self.assertEqual(self.desk.report(dict(stale, attempt=pid)), {"ok": False, "status": "queued"})
        self.beat(a)                                           # ...and taken again as a new run
        self.assertNotEqual(self.job(job["id"])["prompt_id"], pid)
        self.assertNotEqual(a.held[job["id"]].attempt, pid)
        self.assertIn(pid, a.client.deleted)                   # the old run taken out of ComfyUI

    def test_agent_restart_picks_its_run_up_again(self):
        comfy = FakeComfy()
        a = self.ready(self.agent(comfy=comfy))
        job = self.submit()
        pid = self.dispatched(a, job)
        comfy.pending.discard(pid)
        comfy.running.add(pid)
        self.agents.remove(a)                                  # the agent stops; ComfyUI carries on
        b = self.agent(comfy=comfy)
        self.ready(b)
        self.until(lambda: job["id"] in b.held and b.held[job["id"]].status == "running")
        self.until(lambda: self.job(job["id"])["status"] == "running")
        comfy.running.discard(pid)
        done = self.finish(b, job["id"], pid)
        self.assertEqual((done["status"], done["attempts"]), ("done", 1), done["error"])

    def test_agent_restart_collects_a_run_that_finished_meanwhile(self):
        comfy = FakeComfy()
        a = self.ready(self.agent(comfy=comfy))
        job = self.submit()
        pid = self.dispatched(a, job)
        info = a.held[job["id"]].job["info"]
        self.agents.remove(a)                                  # the agent stops; ComfyUI finishes the job
        comfy.pending.discard(pid)
        for i in range(1, 4):
            Path(info["output_dir"], f"{info['frames_prefix']}_{i:04d}.png").write_bytes(b"png")
        comfy.hist[pid] = {"outputs": {}, "status": {"status_str": "success", "completed": True}}
        b = self.ready(self.agent(comfy=comfy))
        self.until(lambda: self.job(job["id"])["status"] == "done", what="done")
        done = self.job(job["id"])
        self.assertEqual((done["attempts"], done["outputs"]["frames"]), (1, 3))
        self.assertEqual(b.held, {})

    def test_agent_restart_before_submitting_prepares_again(self):
        comfy = FakeComfy()
        a = self.ready(self.agent(comfy=comfy))
        a.online = False
        job = self.submit()
        self.beat(a)
        self.assertEqual(self.job(job["id"])["status"], "preparing")
        for t in [h.task for h in a.held.values()]:
            t.cancel()
        self.agents.remove(a)
        b = self.ready(self.agent(comfy=comfy))
        self.until(lambda: self.job(job["id"])["status"] == "dispatched")
        self.assertEqual(self.job(job["id"])["worker"], "gpu1")
        self.assertEqual(list(comfy.prompts), [b.held[job["id"]].pid])

    def test_manager_restart_and_reports_that_wait(self):
        a = self.ready(self.agent())
        job = self.submit()
        pid = self.dispatched(a, job)
        self.start_manager()                                   # the manager restarts mid-run
        self.assertEqual(self.beat(a).get("hello"), True)      # it doesn't know the agent yet
        self.assertFalse(a.registered)
        self.wait(a._hello())                                  # checks in again with its job
        self.assertEqual(self.job(job["id"])["status"], "dispatched")
        a.link.down = True                                     # and goes away again
        self.msg(a, "execution_start", prompt_id=pid)
        info = a.held[job["id"]].job["info"]
        for i in range(1, 4):
            Path(info["output_dir"], f"{info['frames_prefix']}_{i:04d}.png").write_bytes(b"png")
        a.client.hist[pid] = {"outputs": {}, "status": {"status_str": "success", "completed": True}}
        self.msg(a, "execution_success", prompt_id=pid)
        self.until(lambda: a.held[job["id"]].finished, what="collected")
        self.assertEqual(self.job(job["id"])["status"], "dispatched")   # nothing delivered yet
        a.link.down = False
        self.wait(a._flush_all())                              # delivered in order once it's back
        j = self.job(job["id"])
        self.assertEqual((j["status"], j["outputs"]["frames"]), ("done", 3))
        self.assertEqual(a.held, {})

    def test_jobs_of_an_agent_that_stopped_checking_in_go_back(self):
        a = self.ready(self.agent())
        job = self.submit()
        pid = self.dispatched(a, job)
        self.desk.check_leases(time.time() + self.cfg.farm.lost_after_seconds + 1)
        j = self.job(job["id"])
        self.assertEqual((j["status"], j["worker"]), ("queued", None))
        self.assertIn("stopped checking in", j["warnings"][-1])
        b = self.ready(self.agent("gpu2"))
        self.beat(b)                                           # another agent takes it
        self.assertEqual(self.job(job["id"])["worker"], "gpu2")
        self.beat(a)                                           # the first comes back: told to drop its run
        self.assertNotIn(job["id"], a.held)
        self.assertIn(pid, a.client.deleted)

    def test_a_job_the_agent_never_got_goes_back(self):
        a = self.ready(self.agent())
        job = self.submit()
        reply = self.wait(self.desk.heartbeat({"name": "gpu1", "seq": a._seq + 1, "held": [], "wait": 0}))
        a._seq += 1
        self.assertEqual([j["job"] for j in reply["jobs"]], [job["id"]])   # the reply is lost
        first = reply["jobs"][0]["attempt"]
        self.assertEqual(self.job(job["id"])["status"], "preparing")
        reply = self.wait(a._heartbeat())                      # its next check-in doesn't list it:
        j = self.job(job["id"])                                # requeued, and offered again as a new run
        self.assertIn("doesn't have the job", j["warnings"][-1])
        self.assertEqual((j["status"], j["attempts"]), ("preparing", 0))
        self.assertNotEqual(j["prompt_id"], first)
        self.assertEqual([x["attempt"] for x in reply["jobs"]], [j["prompt_id"]])
        self.wait(a._obey(reply))
        self.until(lambda: self.job(job["id"])["status"] == "dispatched")

    def test_agents_take_what_they_have_room_for(self):
        a = self.ready(self.agent("gpu1", max_inflight=2))
        b = self.ready(self.agent("gpu2", max_inflight=1))
        jobs = [self.submit() for _ in range(6)]
        self.beat(a)
        self.beat(b)
        by = {j["id"]: self.job(j["id"])["worker"] for j in jobs}
        self.assertEqual(sorted(w or "" for w in by.values()), ["", "gpu1", "gpu1", "gpu1", "gpu2", "gpu2"])
        self.until(lambda: a._in_comfy == 2 and b._in_comfy == 1, what="ComfyUI places taken")
        statuses = sorted(self.job(j)["status"] for j, w in by.items() if w == "gpu1")
        self.assertEqual(statuses, ["dispatched", "dispatched", "preparing"])   # one prepared ahead

    def test_jobs_go_only_to_agents_that_can_run_them(self):
        missing = {k: v for k, v in INSTALLED.items() if k != "VHS_LoadImagesPath"}
        a = self.ready(self.agent("gpu1", comfy=FakeComfy(missing)))
        self.assertIn("ltx25_alpha_gen", self.desk.summaries()[0]["problems"])
        job = self.submit()
        self.beat(a)
        failed = self.job(job["id"])                           # no worker can: failed, saying why
        self.assertEqual(failed["status"], "failed")
        self.assertIn("VHS_LoadImagesPath is not installed", failed["error"])
        b = self.ready(self.agent("gpu2"))
        job = self.submit()
        self.beat(a)
        self.assertEqual(self.job(job["id"])["status"], "queued")   # gpu2 can: it waits for gpu2
        self.beat(b)
        self.assertEqual(self.job(job["id"])["worker"], "gpu2")

    def test_the_agent_on_its_own(self):
        """Its loops as `comfyfarm agent` runs them: it checks in, takes and runs a job, and
        keeps its reports while the manager is away."""
        a = self.agent(heartbeat_wait=1, health_seconds=0.05, reconcile_seconds=0.2)

        async def ws_loop():            # ComfyUI's websocket: up; its messages come from the test
            a.ws_connected = True
            await a.refresh_object_info()
            await asyncio.Event().wait()
        a._ws_loop = ws_loop

        async def start():
            a.start()
        self.wait(start())
        self.until(lambda: self.desk.summaries() and self.desk.summaries()[0]["checked"]
                   and self.desk.summaries()[0]["online"], what="agent ready")
        job = self.submit()
        self.until(lambda: self.job(job["id"])["status"] == "dispatched", what="dispatched")
        pid = self.job(job["id"])["prompt_id"]
        a.link.down = True                                     # the manager goes away
        self.msg(a, "execution_start", prompt_id=pid)
        info = a.held[job["id"]].job["info"]
        for i in range(1, 4):
            Path(info["output_dir"], f"{info['frames_prefix']}_{i:04d}.png").write_bytes(b"png")
        a.client.pending.discard(pid)
        a.client.hist[pid] = {"outputs": {}, "status": {"status_str": "success", "completed": True}}
        self.msg(a, "execution_success", prompt_id=pid)
        self.until(lambda: a.held[job["id"]].finished, what="collected")
        self.until(lambda: a.manager_down, what="manager seen down")
        self.assertEqual(self.job(job["id"])["status"], "dispatched")
        a.link.down = False                                    # back: the reports go, in order
        self.until(lambda: self.job(job["id"])["status"] == "done", what="done")
        self.assertEqual(self.job(job["id"])["outputs"]["frames"], 3)
        self.until(lambda: not a.manager_down and a.held == {}, what="manager back")

    def test_a_check_in_waits_for_work(self):
        self.ready(self.agent())

        async def go():
            t0 = time.time()
            hb = asyncio.ensure_future(self.desk.heartbeat({"name": "gpu1", "seq": 99, "held": [], "wait": 5}))
            await asyncio.sleep(0.2)
            self.assertFalse(hb.done())                        # nothing to do: held
            job = self.submit()                                # wakes it
            reply = await asyncio.wait_for(hb, 2)
            self.assertEqual([j["job"] for j in reply["jobs"]], [job["id"]])
            self.assertLess(time.time() - t0, 2)
        self.wait(go())


    def test_a_check_in_the_agent_gave_up_on_gives_nothing_out(self):
        self.ready(self.agent())

        async def go():
            first = asyncio.ensure_future(self.desk.heartbeat({"name": "gpu1", "seq": 50, "held": [], "wait": 5}))
            await asyncio.sleep(0.1)
            await self.desk.heartbeat({"name": "gpu1", "seq": 51, "held": [], "wait": 0})   # the agent retried
            job = self.submit()
            self.assertEqual(await asyncio.wait_for(first, 2), {"drop": [], "cancel": [], "jobs": []})
            self.assertEqual(self.job(job["id"])["status"], "queued")
            reply = await self.desk.heartbeat({"name": "gpu1", "seq": 52, "held": [], "wait": 0})
            self.assertEqual([j["job"] for j in reply["jobs"]], [job["id"]])
        self.wait(go())


if __name__ == "__main__":
    unittest.main()
