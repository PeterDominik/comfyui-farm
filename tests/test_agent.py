"""The manager and its agents in one process (manager.desk, agent.agent, agent.link.LocalLink),
against an in-memory ComfyUI per agent: taking jobs, converting inputs on the agent,
running and finishing them, cancels in each state, lost runs, restarts of either side,
leases, several agents with their own path mappings.

The end-to-end suites (test_e2e.py, test_e2e_agents.py) do the same over real HTTP
against tools/mock_comfyui.py.

    python -m unittest tests.test_agent -v
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_textures import FAKE_OIIOTOOL, OCIO_CONFIG
from test_comfyenv import COMMIT_A
from test_comfyenv import Farm as ComfyInstall
from test_workflows import fake_object_info, plate_matte_workflow
from test_preview import FFMPEG, png

from comfyfarm.comfyenv import spec as ES
from comfyfarm.config import AgentConfig, Config
from comfyfarm.fileio import farmnodes
from comfyfarm.fileio import wire as W
from comfyfarm.fileio.errors import SequenceError
from comfyfarm.fileio.media import read_movie_info
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.desk import STEAL_SECONDS, Desk
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm, FarmError
from comfyfarm.protocol import LinkError
from comfyfarm.definitions import Definition, ParamError
from comfyfarm.runners.comfyui_workflow import Workflow, WorkflowRegistry

try:   # the agent talks to ComfyUI with httpx and websockets
    from comfyfarm.agent.agent import Agent
    from comfyfarm.agent.link import LocalLink
    from comfyfarm.runners.comfyui import ComfyUIRunner
except ImportError:   # pragma: no cover
    Agent = None

REPO = Path(__file__).resolve().parent.parent
SEQ = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/abc_0010.pre.v1.%04d.png"
EXR = "/rdo/shows/xyz/.published/abc/abc_0010/plate/v1/abc_0010.plate.v1.%04d.exr"
REGISTRY = WorkflowRegistry(REPO / "workflows")
INSTALLED = {}   # every node pack and model the workflows use
for _wf in REGISTRY.workflows.values():
    for _cls, _spec in fake_object_info(_wf.template).items():
        _have = INSTALLED.setdefault(_cls, _spec)["input"]["required"]
        for _key, _val in _spec["input"]["required"].items():
            if _key in _have and _have[_key] is not _val and isinstance(_val[0], list):   # models: both lists
                _have[_key][0].extend(o for o in _val[0] if o not in _have[_key][0])
            else:
                _have.setdefault(_key, _val)


class FakeComfy:
    """ComfyUI's HTTP API as the agent uses it, in memory. Tests move prompts along by
    hand: running/pending, history, websocket messages."""

    def __init__(self, object_info: dict | None = None, vram: int = 32 * 2**30, card: str = "fake"):   # a 5090
        self.oi = dict(object_info or INSTALLED)
        self.vram = vram
        self.card = card
        self.prompts: dict[str, dict] = {}
        self.running: set[str] = set()
        self.pending: set[str] = set()
        self.hist: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.interrupted: list[str] = []

    async def system_stats(self):
        return {"system": {"comfyui_version": "test"},
                "devices": [{"name": f"cuda:0 {self.card}", "vram_total": self.vram, "vram_free": self.vram}]}

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


def old_farm_nodes(object_info: dict) -> dict:
    """The same ComfyUI as it runs after a `git pull` changed the farm nodes: the old ones."""
    oi = {k: dict(v) for k, v in object_info.items()}
    for c in farmnodes.CLASSES:
        oi[c]["description"] = farmnodes.marker("0123456789ab", "/old/checkout")
    return oi


class FakeLauncher:
    """Starts nothing; a restart gives the agent's ComfyUI the node definitions `then`."""

    def __init__(self, comfy: "FakeComfy", then: dict | None, enabled: bool = True):
        self.comfy, self.then, self.enabled, self.restarts = comfy, then, enabled, 0

    def tick(self, reachable):
        pass

    def restart(self):
        self.restarts += 1
        if self.then == "not ours":
            return False                  # a ComfyUI this agent didn't start
        if self.then is not None:
            self.comfy.oi = dict(self.then)
        return True


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

    async def environment(self, msg):
        self._check()
        return await super().environment(msg)

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
            "preview": {"enabled": False},      # its own tests switch it on (they need ffmpeg)
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
        a = Agent(cfg, FlakyLink(self.desk), runner=ComfyUIRunner(cfg, client=comfy or FakeComfy()))
        a.runner._registry = REGISTRY           # the suite's: some tests add workflows of their own to it
        self.agents.append(a)
        return a

    def any_gpu(self):
        """LTX without its least VRAM (requires), for tests of the order machines get jobs in."""
        wf = REGISTRY.workflows["ltx25_alpha_gen"]
        self.addCleanup(setattr, wf, "requires", wf.requires)
        wf.requires = {"vram_gb": None, "tags": []}

    def gpu(self, name, gb, object_info=None, card="fake"):
        """An agent taking jobs, on a GPU with this much VRAM."""
        return self.ready(self.agent(name, comfy=FakeComfy(object_info, vram=gb * 2**30, card=card)))

    def ready(self, a):
        """Checked in, its ComfyUI up and its node definitions checked."""
        async def go():
            await a._hello()
            a.runner.stats = await a.runner.client.system_stats()
            a.runner.online = a.runner.ws_connected = True
            await a.runner.refresh_object_info()
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
        self.wait(a.runner._on_message({"type": kind, "data": data}))

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

    def finish(self, a, jid, pid, frame=b"png"):
        """ComfyUI runs the prompt: the save node writes its frames, then success."""
        self.msg(a, "execution_start", prompt_id=pid)
        h = a.held[jid]
        info = h.job["info"]
        for i in range(1, (info.get("expected_frames") or 1) + 1):
            Path(info["output_dir"], f"{info['frames_prefix']}_{i:04d}.png").write_bytes(frame)
        a.runner.client.hist[pid] = {"outputs": {}, "status": {"status_str": "success", "completed": True}}
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
        prompt = a.runner.client.prompts[pid]
        self.assertEqual(prompt["9104"]["inputs"],     # ComfyFarm Load Images: the exact frames
                         {"path": f"{self.root}/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/"
                                  f"abc_0010.pre.v1.%04d.png", "frames": "1001-1003"})
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
        self.assertNotIn(pid, a.runner.client.hist)                    # ComfyUI's history entry dropped
        self.assertEqual(self.store.stats("ltx25_alpha_gen")["runs"], 1)
        w = self.desk.summaries()[0]
        self.assertEqual((w["name"], w["online"], w["connected"], w["inflight"], w["gpu"]["name"]),
                         ("gpu1", True, True, 0, "cuda:0 fake"))
        self.assertEqual(self.farm.online_workers(), 1)

    @unittest.skipUnless(FFMPEG, "no ffmpeg on this machine")
    def test_the_preview_movie_of_the_frames(self):
        self.cfg.preview.enabled = True
        a = self.ready(self.agent())
        job = self.submit()
        self.assertEqual(job["info"]["collect"]["movie"], {"fps": 24.0, "fps_str": "24/1", "max_width": 1920, "crf": 18})
        done = self.finish(a, job["id"], self.dispatched(a, job), frame=png(64, 36))
        self.assertEqual((done["status"], done["warnings"]), ("done", []), done["error"])
        out = done["outputs"]
        self.assertEqual(out["preview"], f"{done['output_dir']}/abc_0010.pre.v1.alpha_v001_preview.mp4")
        self.assertTrue(out["preview_posix"].startswith("/rdo/shows/xyz/_project_ref/"))
        info = read_movie_info(out["preview"], self.cfg.convert)
        self.assertEqual((info["frames"], info["width"], info["height"]), (3, 64, 36))
        self.assertEqual(sorted(os.listdir(done["output_dir"])),
                         [f"abc_0010.pre.v1.alpha_v001.{f}.png" for f in (1001, 1002, 1003)]
                         + ["abc_0010.pre.v1.alpha_v001_preview.mp4"])

    def test_a_preview_that_cant_be_made_is_only_a_warning(self):
        self.cfg.preview.enabled = True
        a = self.ready(self.agent())
        job = self.submit()
        done = self.finish(a, job["id"], self.dispatched(a, job), frame=b"not a png")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["outputs"]["frames"], 3)                      # the frames are what counts
        self.assertNotIn("preview", done["outputs"])
        self.assertTrue(any(w.startswith("Couldn't make the preview movie") for w in done["warnings"]), done["warnings"])

    def test_no_preview_of_one_frame_or_when_switched_off(self):
        self.cfg.preview.enabled = True
        a = self.ready(self.agent())
        one = self.submit(f"{SEQ} 1002")
        done = self.finish(a, one["id"], self.dispatched(a, one), frame=png(8, 8))
        self.assertEqual((done["outputs"]["frames"], done["warnings"]), (1, []))
        self.assertNotIn("preview", done["outputs"])
        self.cfg.preview.enabled = False                                   # frozen at submit
        off = self.submit()
        self.assertIsNone(off["info"]["collect"]["movie"])
        done = self.finish(a, off["id"], self.dispatched(a, off), frame=png(8, 8))
        self.assertNotIn("preview", done["outputs"])

    @unittest.skipIf(os.name == "nt", "publishing runs on the Linux manager (a POSIX shell)")
    def test_publish_when_done(self):
        """Asked for at submit: checked then, run once the job is done."""
        from test_publish import FAKE_DISPATCH, TEMPLATE
        def submit(**publish):
            return self.farm.submit("ltx25_alpha_gen", {"input": f"{SEQ} 1001-1003"}, "ann", publish=publish)
        with self.assertRaises(ParamError) as e:
            submit()
        self.assertIn("isn't switched on", str(e.exception))
        pub = self.cfg.publish
        templates = Path(self.root, "publishTemplates")
        (templates / "ltx25_alpha_gen").mkdir(parents=True)
        shutil.copy(TEMPLATE, templates / "ltx25_alpha_gen" / "default.nk")
        Path(self.root, "dispatch.py").write_text(FAKE_DISPATCH)
        for k, v in {"enabled": True, "dry_run": False, "templates": str(templates),
                     "staging": str(templates / ".staging"), "shell": ["/bin/sh", "-c"], "show_folder": "",
                     "command": f"{sys.executable} {self.root}/dispatch.py --entities {{shot}} --step {{step}} "
                                f"--workfile {{workfile}} --show {{show}} --frameRange {{frames}}"}.items():
            self.addCleanup(setattr, pub, k, getattr(pub, k))
            setattr(pub, k, v)
        with self.assertRaises(ParamError):
            submit(step="nope")
        a = self.ready(self.agent())
        job = submit(step="comp")
        self.assertEqual(job["info"]["publish_request"],
                         {"show": "xyz", "shot": "abc_0010", "step": "comp", "template": "default",
                          "description": "Auto Publish", "user": "ann"})
        done = self.finish(a, job["id"], self.dispatched(a, job))
        self.assertEqual(done["status"], "done", done["error"])
        self.until(lambda: (self.store.publishes(job["id"]) or [{}])[0].get("state") in ("dispatched", "failed"),
                   what="published")
        rec = self.store.publishes(job["id"])[0]
        self.assertEqual((rec["state"], rec["tractor_id"]), ("dispatched", "6501887"), rec["error"])
        self.assertEqual(Path(rec["workfile"]), templates / ".staging" / job["id"] / "default.nk")
        self.assertEqual(rec["frames"], "1001-1003")
        self.assertEqual(self.farm.annotate([self.job(job["id"])])[0]["publish"]["state"], "dispatched")
        # Run again: published when done too; a dry run stays one.
        again = self.farm.retry(job["id"], "ann")
        self.assertEqual(again["info"]["publish_request"]["step"], "comp")
        self.assertTrue(submit(dry_run=True)["info"]["publish_request"]["dry_run"])
        pub.templates = str(Path(self.root, "elsewhere"))                  # can't publish any more: run it anyway
        self.assertNotIn("publish_request", self.farm.retry(job["id"], "ann")["info"])

    @unittest.skipIf(os.name == "nt", "read permissions are set on Linux; on Windows the share decides")
    def test_outputs_are_readable_by_everyone(self):
        old = os.umask(0o077)                                  # an agent or ComfyUI with a private umask
        self.addCleanup(os.umask, old)
        a = self.ready(self.agent())
        job = self.submit()
        done = self.finish(a, job["id"], self.dispatched(a, job))
        self.assertEqual(done["status"], "done", done["error"])
        out = Path(done["output_dir"])
        self.assertEqual(out.stat().st_mode & 0o555, 0o555)
        self.assertEqual(out.parent.stat().st_mode & 0o555, 0o555)   # folders the agent created
        for f in out.iterdir():
            self.assertEqual(f.stat().st_mode & 0o444, 0o444, f.name)

    def test_an_agent_sees_the_job_through_its_own_paths(self):
        # gpu2 sees show storage under another folder (like a Windows machine's UNC path).
        link_dir(Path(self.root, "shows"), Path(self.root, "other/mount/shows"))
        mine = [{"posix": "/rdo/shows", "windows": f"{self.root}/other/mount/shows"}]
        a = self.ready(self.agent("gpu2", mappings=mine))
        job = self.submit()
        pid = self.dispatched(a, job)
        prompt = a.runner.client.prompts[pid]
        self.assertTrue(prompt["9104"]["inputs"]["path"].startswith(f"{self.root}/other/mount/shows/"))
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
        loader = a.runner.client.prompts[pid]["9104"]["inputs"]
        self.assertEqual(loader, {"path": f"{self.root}/agent-scratch/{job['id']}/abc_0010.plate.v1.%04d.png",
                                  "frames": "1001-1003"})
        self.assertEqual(sorted(os.listdir(loader["path"].rpartition("/")[0])),
                         [f"abc_0010.plate.v1.{f}.png" for f in (1001, 1002, 1003)])
        done = self.finish(a, job["id"], pid)
        self.assertEqual(done["status"], "done", done["error"])
        self.assertFalse(os.path.exists(loader["path"].rpartition("/")[0]))   # scratch cleaned up
        self.assertFalse(os.path.exists(f"{self.root}/scratch/{job['id']}"))

    def test_cancel_queued_and_preparing_at_once(self):
        a = self.ready(self.agent())
        queued = self.submit()
        self.assertEqual(self.wait(self.farm.cancel(queued["id"]))["status"], "cancelled")
        a.runner.online = False                                       # ComfyUI down: the next job waits in preparing
        job = self.submit()
        self.beat(a)
        self.assertEqual(self.job(job["id"])["status"], "preparing")
        self.assertEqual(self.wait(self.farm.cancel(job["id"]))["status"], "cancelled")
        self.beat(a)                                           # told to drop it
        self.assertEqual(a.held, {})
        self.assertEqual(a._in_comfy, 0)
        self.assertEqual(a.runner.client.prompts, {})

    def test_cancel_in_comfyui_waiting_and_running(self):
        a = self.ready(self.agent())
        waiting = self.submit()
        pid = self.dispatched(a, waiting)
        j = self.wait(self.farm.cancel(waiting["id"]))
        self.assertEqual((j["status"], j["stage"], j["cancel_requested"]), ("dispatched", "cancelling", 1))
        self.beat(a)
        self.until(lambda: self.job(waiting["id"])["status"] == "cancelled")
        self.assertEqual(a.runner.client.deleted, [pid])

        running = self.submit()
        pid = self.dispatched(a, running)
        a.runner.client.pending.discard(pid)
        a.runner.client.running.add(pid)
        self.msg(a, "execution_start", prompt_id=pid)
        self.wait(self.farm.cancel(running["id"]))
        self.beat(a)
        self.assertEqual(a.runner.client.interrupted, [pid])
        self.assertEqual(self.job(running["id"])["status"], "running")   # until ComfyUI says it stopped
        self.msg(a, "execution_interrupted", prompt_id=pid)
        self.until(lambda: self.job(running["id"])["status"] == "cancelled")
        self.assertEqual(a.held, {})

    def test_node_error_fails_the_job_with_its_traceback(self):
        a = self.ready(self.agent())
        job = self.submit()
        pid = self.dispatched(a, job)
        self.msg(a, "execution_start", prompt_id=pid)
        self.msg(a, "execution_error", prompt_id=pid, node_id="9104", node_type="ComfyFarmLoadImages",
                 exception_message="No images", traceback=["Traceback: boom\n"])
        self.until(lambda: self.job(job["id"])["status"] == "failed")
        failed = self.job(job["id"])
        self.assertIn("ComfyFarmLoadImages (node 9104", failed["error"])
        self.assertEqual(failed["info"]["traceback"], "Traceback: boom\n")

    def test_a_run_comfyui_lost_is_requeued_then_given_up(self):
        a = self.ready(self.agent())
        job = self.submit()
        pid = self.dispatched(a, job)
        a.held[job["id"]].dispatched_at -= 10
        a.runner.client.pending.clear()                               # ComfyUI restarted: no queue, no history
        self.wait(a.runner._reconcile())
        j = self.job(job["id"])
        self.assertEqual((j["status"], j["worker"], j["attempts"]), ("queued", None, 1))
        self.assertIn("lost the job", j["warnings"][-1])
        self.assertIn(pid, a.runner.abandoned)
        pid2 = self.dispatched(a, j)                           # taken again: a new run
        self.assertNotEqual(pid2, pid)
        self.assertEqual(self.job(job["id"])["attempts"], 2)
        a.held[job["id"]].dispatched_at -= 10
        a.runner.client.pending.clear()
        self.wait(a.runner._reconcile())
        self.until(lambda: self.job(job["id"])["status"] == "failed")   # farm.max_attempts is 2
        self.assertIn("Gave up after 2 attempts", self.job(job["id"])["error"])

    def test_overlapping_checks_requeue_a_lost_run_once(self):
        a = self.ready(self.agent())
        job = self.submit()
        self.dispatched(a, job)
        a.held[job["id"]].dispatched_at -= 10
        a.runner.client.pending.clear()
        calls, lost = [], self.farm.lost
        self.farm.lost = lambda j, reason: (calls.append(j["id"]), lost(j, reason))[1]

        async def both():
            await asyncio.gather(a.runner._reconcile(), a.runner._reconcile())
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
        self.assertIn(pid, a.runner.client.deleted)                   # the old run taken out of ComfyUI

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
        a.runner.online = False
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
        a.runner.client.hist[pid] = {"outputs": {}, "status": {"status_str": "success", "completed": True}}
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
        self.desk.agents["gpu1"].last_seen -= 60                # and still hasn't
        b = self.ready(self.agent("gpu2"))
        self.beat(b)                                           # another agent takes it
        self.assertEqual(self.job(job["id"])["worker"], "gpu2")
        self.beat(a)                                           # the first comes back: told to drop its run
        self.assertNotIn(job["id"], a.held)
        self.assertIn(pid, a.runner.client.deleted)

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

    def test_a_second_job_only_when_every_gpu_is_busy(self):
        a = self.ready(self.agent("gpu1", max_inflight=2))
        b = self.ready(self.agent("gpu2", max_inflight=1))
        jobs = [self.submit() for _ in range(6)]
        self.beat(a)
        self.beat(b)
        by = {j["id"]: self.job(j["id"])["worker"] for j in jobs}
        self.assertEqual([by[j["id"]] for j in jobs], ["gpu1", "gpu2", "gpu1", "gpu2", None, None])
        self.until(lambda: a._in_comfy == 2 and b._in_comfy == 1, what="ComfyUI places taken")
        statuses = sorted(self.job(j)["status"] for j, w in by.items() if w == "gpu2")
        self.assertEqual(statuses, ["dispatched", "preparing"])   # one prepared ahead, waiting for ComfyUI

    # ------------------------------------------------------------ which machine gets a job
    def test_the_best_free_gpu_takes_the_job(self):
        self.any_gpu()
        small, mid, big = self.gpu("small", 24), self.gpu("mid", 32), self.gpu("big", 96)
        self.assertEqual({w["name"]: w["rank"] for w in self.desk.summaries()}, {"big": 1, "mid": 2, "small": 3})
        got = []
        for _ in range(6):
            job = self.submit()
            for a in (small, mid, big):        # the worst checks in first
                self.beat(a)
            got.append(self.job(job["id"])["worker"])
        # One per GPU, best first; then, every GPU busy, one prepared ahead on each, best first.
        self.assertEqual(got, ["big", "mid", "small", "big", "mid", "small"])
        job = self.submit()
        for a in (small, mid, big):
            self.beat(a)
        self.assertEqual(self.job(job["id"])["status"], "queued")   # no room: it waits

    def test_collecting_outputs_leaves_the_gpu_free(self):
        self.any_gpu()
        big, small = self.gpu("big", 96), self.gpu("small", 24)
        first = self.submit()
        self.dispatched(big, first)
        self.store.transition(first["id"], ("dispatched",), status="postprocessing")
        job = self.submit()
        self.beat(small)
        self.beat(big)
        self.assertEqual(self.job(job["id"])["worker"], "big")

    def test_agents_order_puts_machines_first(self):
        self.any_gpu()
        self.cfg.agents.order = ["Small"]
        small, big = self.gpu("small", 24), self.gpu("big", 96)
        self.gpu("mid", 32)
        self.assertEqual({w["name"]: w["rank"] for w in self.desk.summaries()}, {"small": 1, "big": 2, "mid": 3})
        job = self.submit()
        self.beat(big)
        self.beat(small)
        self.assertEqual(self.job(job["id"])["worker"], "small")

    def test_the_fastest_gpu_for_the_workflow_takes_the_job(self):
        """Run times decide, not VRAM: LTX goes to the 5090 before the A6000 (48 GB)."""
        self.any_gpu()
        a6000 = self.gpu("a6000", 48, card="NVIDIA RTX A6000")
        r3090 = self.gpu("r3090", 24, card="NVIDIA GeForce RTX 3090")
        r5090 = self.gpu("r5090", 32, card="NVIDIA GeForce RTX 5090")
        ltx = "ltx25_alpha_gen"
        self.store.record_gpu_run(ltx, "NVIDIA GeForce RTX 5090", 3, 10)       # 3.3 s a frame
        self.store.record_gpu_run(ltx, "NVIDIA GeForce RTX 3090", 3, 22)       # 7.3 s
        self.store.record_gpu_run(ltx, "NVIDIA RTX A6000", 3, 36)              # 12 s
        self.assertEqual({w["name"]: w["rank"] for w in self.desk.summaries()},     # without run times: VRAM
                         {"a6000": 1, "r5090": 2, "r3090": 3})
        self.assertEqual([(r["order"], r["gpu"]) for r in self.farm.run_times()],
                         [(1, "NVIDIA GeForce RTX 5090"), (2, "NVIDIA GeForce RTX 3090"), (3, "NVIDIA RTX A6000")])
        jobs = [self.submit() for _ in range(4)]
        # The queue's "→ machine" says the same: fastest free first, then one prepared ahead on the fastest.
        self.assertEqual([j["next_worker"] for j in self.farm.annotate([dict(j) for j in jobs])],
                         ["r5090", "r3090", "a6000", "r5090"])
        for a in (a6000, r3090, r5090):                                       # the slowest checks in first
            self.beat(a)
        self.assertEqual([self.job(j["id"])["worker"] for j in jobs], ["r5090", "r3090", "a6000", "r5090"])
        # A workflow none of them has run goes by VRAM.
        for j in jobs:
            self.store.transition(j["id"], ("preparing", "dispatched"), status="done")
        wan = self.farm.submit("wan22_i2i", {"input": f"{SEQ} 1001"}, "ann")
        for a in (r5090, r3090, a6000):
            self.beat(a)
        self.assertEqual(self.job(wan["id"])["worker"], "a6000")

    def test_gpus_that_havent_run_the_workflow_come_after_timed_ones(self):
        self.any_gpu()
        machines = [self.gpu("a6000", 48, card="NVIDIA RTX A6000"),
                    self.gpu("r5090", 32, card="NVIDIA GeForce RTX 5090"),
                    self.gpu("r3090", 24, card="NVIDIA GeForce RTX 3090")]
        self.store.record_gpu_run("ltx25_alpha_gen", "NVIDIA GeForce RTX 3090", 3, 60)   # only the 3090 has run it
        jobs = [self.submit() for _ in range(3)]
        for a in machines:
            self.beat(a)
        # The timed 3090 first; the others (no runs of their own yet) by VRAM, which times them too.
        self.assertEqual([self.job(j["id"])["worker"] for j in jobs], ["r3090", "a6000", "r5090"])

    def test_the_job_size_decides_between_overhead_and_speed(self):
        """A GPU with a big start-up overhead but fast frames gets long jobs; short ones go
        to the one that starts quickly."""
        self.any_gpu()
        quick = self.gpu("quick", 32, card="NVIDIA GeForce RTX 5090")
        heavy = self.gpu("heavy", 48, card="NVIDIA RTX A6000")
        ltx = "ltx25_alpha_gen"
        for frames in (2, 10, 30):
            self.store.record_gpu_run(ltx, "NVIDIA GeForce RTX 5090", frames, 5 + 4 * frames)    # 5 s + 4 s a frame
            self.store.record_gpu_run(ltx, "NVIDIA RTX A6000", frames, 60 + 1 * frames)          # 60 s + 1 s a frame
        short = self.submit()                                                  # 3 frames: 17 s here, 63 s there
        self.beat(heavy)
        self.beat(quick)
        self.assertEqual(self.job(short["id"])["worker"], "quick")
        self.store.transition(short["id"], ("preparing", "dispatched"), status="done")
        long = dict(self.submit())                                             # 100 frames: 405 s, 160 s
        info = dict(long["info"], expected_frames=100)
        self.store.update(long["id"], info=info)
        self.beat(quick)
        self.beat(heavy)
        self.assertEqual(self.job(long["id"])["worker"], "heavy")

    def test_identical_gpus_warm_first_then_take_turns(self):
        a, b = self.gpu("gpu-a", 32), self.gpu("gpu-b", 32)
        got = []
        for submit in (self.submit,                                                   # LTX: gpu-a (name)
                       lambda: self.farm.submit("wan22_i2i", {"input": f"{SEQ} 1001"}, "ann"),   # cold on both: turns
                       self.submit,                                                   # LTX again: still loaded on gpu-a
                       lambda: self.farm.submit("wan22_i2i", {"input": f"{SEQ} 1002"}, "ann")):  # and Wan on gpu-b
            job = submit()
            self.beat(b)
            self.beat(a)
            got.append(self.job(job["id"])["worker"])
            self.store.transition(job["id"], ("preparing", "dispatched"), status="done")   # its GPU free again
        self.assertEqual(got, ["gpu-a", "gpu-b", "gpu-a", "gpu-b"])
        self.assertEqual({w["name"]: w["last_workflow"] for w in self.desk.summaries()},
                         {"gpu-a": "ltx25_alpha_gen", "gpu-b": "wan22_i2i"})

    # ------------------------------------------------------------ requires and drain
    def test_ltx_needs_32_gb(self):
        small = self.gpu("small", 24)
        self.assertEqual(self.desk.summaries()[0]["skips"]["ltx25_alpha_gen"],
                         "needs a GPU with 32 GB of VRAM (this one has 24)")
        plan = self.farm.plan_summary(self.farm.plan("ltx25_alpha_gen", {"input": f"{SEQ} 1001-1003"}))
        self.assertIn("No machine on the farm can take it now: it needs a GPU with 32 GB of VRAM (this one has 24). "
                      "The job waits until one checks in.", plan["notes"])
        job = self.submit()
        self.assertTrue(job["stage"].startswith("No machine on the farm can take it now"))
        self.beat(small)
        self.assertEqual(self.job(job["id"])["status"], "queued")       # waits: never failed for it
        wan = self.farm.submit("wan22_i2i", {"input": f"{SEQ} 1001"}, "ann")
        self.beat(small)
        self.assertEqual(self.job(wan["id"])["worker"], "small")        # other workflows still run there
        big = self.gpu("big", 32)                                       # 31.8 GiB reported counts as 32
        self.desk.agents["big"].stats["devices"][0]["vram_total"] = int(31.84 * 2**30)
        self.beat(small)
        self.beat(big)
        self.assertEqual(self.job(job["id"])["worker"], "big")
        self.assertEqual(self.job(job["id"])["warnings"], [])           # the wait wasn't a warning

    def test_tags(self):
        osname = "windows" if os.name == "nt" else "linux"
        wf = REGISTRY.workflows["wan22_i2i"]
        self.addCleanup(setattr, wf, "requires", wf.requires)
        wf.requires = {"vram_gb": None, "tags": [osname, "nightly"]}
        a = self.ready(self.agent("tagged", tags=["Nightly"]))
        plain = self.ready(self.agent("plain"))
        self.assertEqual(self.desk.agents["tagged"].tags, sorted({"nightly", osname}))   # its OS added
        self.assertEqual({w["name"]: w["skips"].get("wan22_i2i") for w in self.desk.summaries()},
                         {"tagged": None, "plain": "needs the tag nightly (agent.yaml tags)"})
        job = self.farm.submit("wan22_i2i", {"input": f"{SEQ} 1001"}, "ann")
        self.beat(plain)
        self.beat(a)
        self.assertEqual(self.job(job["id"])["worker"], "tagged")

    def test_drain_and_resume(self):
        a = self.ready(self.agent())
        first = self.submit()
        pid = self.dispatched(a, first)
        self.assertEqual(self.desk.set_drained("gpu1", True, by="peter"), {"worker": "gpu1", "drained": True})
        w = self.desk.summaries()[0]
        self.assertEqual((w["drained"]["by"], w["held"]), ("peter", 1))
        nxt = self.submit()
        self.assertIn("The machines that can run it (gpu1) are drained", nxt["stage"])
        self.beat(a)
        self.assertEqual(self.job(nxt["id"])["status"], "queued")        # no new jobs...
        self.assertEqual(self.finish(a, first["id"], pid)["status"], "done")   # ...but it finishes its own
        self.assertEqual(self.farm.online_workers(), 0)
        self.assertEqual(Desk(self.farm).drained.keys(), {"gpu1"})       # kept across manager restarts
        self.desk.set_drained("gpu1", False, by="peter")
        self.beat(a)
        self.assertEqual(self.job(nxt["id"])["worker"], "gpu1")

    def test_a_drained_machine_doesnt_make_jobs_fail(self):
        """Drained is a pause: jobs only it can run wait for it."""
        missing = {k: v for k, v in INSTALLED.items() if k != "LTXPadFrames"}
        can = self.ready(self.agent("can"))
        cant = self.ready(self.agent("cant", comfy=FakeComfy(missing)))
        self.desk.set_drained("can", True)
        job = self.submit()
        self.beat(cant)
        self.assertEqual(self.job(job["id"])["status"], "queued")
        self.desk.set_drained("can", False)
        self.beat(can)
        self.assertEqual(self.job(job["id"])["worker"], "can")

    def test_warm_models_never_beat_a_bigger_free_gpu(self):
        self.any_gpu()
        big, small = self.gpu("big", 96), self.gpu("small", 32)
        self.desk.agents["small"].last_workflow = "ltx25_alpha_gen"     # loaded on the smaller card
        job = self.submit()
        self.beat(small)
        self.beat(big)
        self.assertEqual(self.job(job["id"])["worker"], "big")

    def test_a_better_gpu_that_cant_run_the_workflow_doesnt_hold_it_up(self):
        self.any_gpu()
        missing = {k: v for k, v in INSTALLED.items() if k != "LTXPadFrames"}
        self.gpu("big", 96, missing)
        small = self.gpu("small", 24)
        job = self.submit()
        self.beat(small)
        self.assertEqual(self.job(job["id"])["worker"], "small")
        self.assertEqual(self.desk._passed, {})

    def test_a_job_the_best_gpu_doesnt_come_for_goes_to_the_next(self):
        self.any_gpu()
        self.gpu("big", 96)                    # checked in, but never comes for jobs
        small = self.gpu("small", 24)
        job = self.submit()
        self.beat(small)
        self.assertEqual(self.job(job["id"])["status"], "queued")   # big's: it waits for big
        name, since = self.desk._passed[job["id"]]
        self.assertEqual(name, "big")
        self.desk._passed[job["id"]] = (name, since - STEAL_SECONDS)   # big hasn't come for it since
        self.beat(small)
        self.assertEqual(self.job(job["id"])["worker"], "small")
        self.assertNotIn(job["id"], self.desk._passed)

    # ------------------------------------------------------------ the farm's ComfyUI
    def test_the_environment_goes_to_the_dashboard(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        inst = ComfyInstall(tmp)
        ES.save({"comfyui": {"version": "0.33.0"}, "libraries": inst.libraries,
                 "node_packs": {"comfyui-kjnodes": {"version": "1.1.9", "hash": "0" * 12, "for": ["ltx25_alpha_gen"]}},
                 "models": {"ltx.safetensors": {"folder": "diffusion_models", "library": str(inst.bank), "size": 100}}},
                tmp / "environment.yaml")
        a = self.ready(self.agent(comfyui_dir=str(inst.comfy), comfyui_python=sys.executable))
        a.runner.env = ES.Cached(tmp / "environment.yaml", tmp / "no-farm-paths.yaml")   # the agent's checkout's
        sent = []
        told = a.link.environment

        async def spy(msg):
            sent.append(msg)
            return await told(msg)
        a.link.environment = spy
        state = self.wait(a.runner.check_environment())
        self.assertEqual([sorted(m) for m in sent], [["name", "report"]])            # what differs, not what it read
        self.assertEqual(sorted(sent[0]["report"]), ["checked_at", "comfyui", "differs", "file", "notes"])
        self.assertEqual(state["comfyui"]["commit"], COMMIT_A)
        self.assertEqual(state["models"]["ltx.safetensors"]["size"], 100)
        env = self.desk.summary(self.desk.agents["gpu1"])["environment"]
        self.assertEqual(env["comfyui"], {"version": "test", "commit": COMMIT_A})    # FakeComfy's version
        kj = os.path.normpath(inst.comfy / "custom_nodes" / "comfyui-kjnodes")
        self.assertEqual(env["differs"], [f"ComfyUI test / {COMMIT_A[:7]}; the farm's is 0.33.0",
                                          f"Node pack comfyui-kjnodes has local changes ({kj})"])
        self.assertEqual(a.environment_differs, env["differs"])
        job = self.submit()                                     # a machine that differs still gets jobs
        self.beat(a)
        self.assertEqual(self.job(job["id"])["worker"], "gpu1")
        # The file changes on the machine (git pull): its next check compares with the new one.
        ES.save({"comfyui": {"version": "test"}, "libraries": inst.libraries}, tmp / "environment.yaml")
        os.utime(tmp / "environment.yaml", (time.time() + 5, time.time() + 5))
        self.wait(a.runner.check_environment())
        self.assertEqual(self.desk.summary(self.desk.agents["gpu1"])["environment"]["differs"], [])
        self.assertEqual(a.environment_differs, [])
        # A manager that restarted gets it again after hello.
        self.desk.agents.clear()
        self.wait(a._hello())
        self.assertEqual(self.desk.agents["gpu1"].environment, a.runner.environment)

    def test_packs_and_models_of_workflows_the_machine_never_runs_dont_count(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        inst = ComfyInstall(tmp)
        ES.save({"comfyui": {"version": "test"}, "libraries": inst.libraries,
                 "node_packs": {"only-for-ltx": {"version": "1.0", "hash": "0" * 12, "for": ["ltx25_alpha_gen"]}}},
                tmp / "environment.yaml")
        need = REGISTRY.workflows["ltx25_alpha_gen"].requires["vram_gb"]
        for name, gb, differs in (("small", need - 8, 0), ("big", need, 1)):
            a = self.ready(self.agent(name, comfy=FakeComfy(vram=gb * 2**30), comfyui_dir=str(inst.comfy),
                                      comfyui_python=sys.executable))
            a.runner.env = ES.Cached(tmp / "environment.yaml", tmp / "no-farm-paths.yaml")
            self.wait(a.runner.check_environment())
            with self.subTest(machine=name):
                self.assertEqual(len(self.desk.agents[name].environment["differs"]), differs,
                                 self.desk.agents[name].environment)

    def test_an_agent_that_sends_what_it_read_is_told_to_update(self):
        a = self.agent("old")
        self.wait(a._hello())
        self.assertEqual(self.desk.environment({"name": "old", "state": {"comfyui": {"version": "1"}}}), {})
        env = self.desk.summary(self.desk.agents["old"])["environment"]
        self.assertEqual((env["differs"], env["comfyui"]), ([], {"version": None, "commit": None}))
        self.assertEqual(env["notes"], ["old runs farm code too old to compare its environment: update the farm's "
                                        "code on it"])

    def test_a_sync_holds_the_machine(self):
        """While comfyfarm comfyui sync updates ComfyUI, the machine takes no jobs and says why."""
        from comfyfarm.agent.launch import release_hold, write_hold
        a = self.ready(self.agent())
        w = self.desk.agents["gpu1"]
        self.assertEqual(self.desk.ping({"name": "gpu1"})["drained"], False)
        self.desk.set_drained("gpu1", True, "ann")
        job = self.submit()
        r = self.desk.ping({"name": "gpu1"})                               # what sync asks before stopping ComfyUI
        self.assertEqual((r["drained"], r["held"]), (True, 0))
        write_hold(a.runner.launcher.hold_file, "sync is updating ComfyUI")
        self.assertTrue(a.runner._hold_tick())
        self.wait(a.send_status())
        self.assertEqual(self.desk.summary(w)["farm_nodes"], {"state": "syncing", "message": "sync is updating ComfyUI"})
        self.desk.set_drained("gpu1", False)
        self.beat(a)
        self.assertEqual(self.job(job["id"])["status"], "queued")           # held: no jobs
        release_hold(a.runner.launcher.hold_file)
        self.assertFalse(a.runner._hold_tick())
        self.assertIsNone(a.runner.farm_nodes)
        self.assertTrue(a.runner._env_stale)                                      # its environment is read again
        self.wait(a.send_status())
        self.beat(a)
        self.assertEqual(self.job(job["id"])["worker"], "gpu1")

    def test_an_agent_that_doesnt_know_its_comfyui_folder(self):
        a = self.ready(self.agent())
        self.wait(a.runner.check_environment())
        env = self.desk.summary(self.desk.agents["gpu1"])["environment"]
        self.assertEqual(env["differs"], [])
        self.assertIn("ComfyUI's folder isn't known", env["notes"][-1])

    # ------------------------------------------------------------ ETAs
    def test_etas_walk_the_queue_per_gpu_type(self):
        self.any_gpu()
        big = self.gpu("big", 32, card="NVIDIA GeForce RTX 5090")
        small = self.gpu("small", 24, card="NVIDIA GeForce RTX 3090")
        jobs = [self.submit() for _ in range(5)]                  # 3 frames each
        got = self.farm.annotate([dict(j) for j in jobs])
        self.assertEqual([j["next_worker"] for j in got], ["big", "small", "big", "small", "big"])
        self.assertEqual({j["eta_seconds"] for j in got}, {None})   # no runs yet: no times
        self.assertEqual([j["queue_position"] for j in got], [1, 2, 3, 4, 5])

        ltx = "ltx25_alpha_gen"
        self.store.record_gpu_run(ltx, "NVIDIA GeForce RTX 5090", 3, 30)     # 10 s a frame
        self.store.record_gpu_run(ltx, "NVIDIA GeForce RTX 3090", 3, 60)     # 20 s a frame
        got = self.farm.annotate([dict(j) for j in jobs])
        # Each free GPU, best first; then one prepared ahead on each; then whichever frees first.
        self.assertEqual([(j["next_worker"], j["eta_seconds"]) for j in got],
                         [("big", 30), ("small", 60), ("big", 60), ("small", 120), ("big", 90)])
        plan = self.farm.plan_summary(self.farm.plan(ltx, {"input": f"{SEQ} 1001-1002"}))
        self.assertEqual(plan["eta"], {"done_in": 110, "run": 20, "worker": "big", "workers": ["big"]})

        for a in (small, big):                                          # the desk hands them out the same way
            self.beat(a)
        self.assertEqual([self.job(j["id"])["worker"] for j in jobs], ["big", "small", "big", "small", None])
        # big's first is half done after 10 s: 15 s left, then the one prepared ahead.
        self.until(lambda: self.job(jobs[2]["id"])["status"] in ("dispatched", "preparing") and
                   self.job(jobs[0]["id"])["status"] == "dispatched", what="dispatched")
        self.store.update(jobs[0]["id"], status="running", started_at=time.time() - 10, progress=0.5)
        fc = self.desk.forecast()
        self.assertAlmostEqual(fc[jobs[0]["id"]]["done_in"], 15, delta=1)
        self.assertAlmostEqual(fc[jobs[2]["id"]]["done_in"], 45, delta=1)
        self.assertEqual(fc[jobs[4]["id"]]["worker"], "big")           # frees at 45, small at 120
        self.assertAlmostEqual(fc[jobs[4]["id"]]["done_in"], 75, delta=1)

        self.desk.set_drained("big", True)                              # finishes its own, takes no more
        fc = self.desk.forecast()
        self.assertAlmostEqual(fc[jobs[2]["id"]]["done_in"], 45, delta=1)
        self.assertEqual(fc[jobs[4]["id"]]["worker"], "small")
        self.store.update(jobs[0]["id"], status="postprocessing")       # collecting its outputs: done any moment
        self.assertEqual(self.desk.forecast()[jobs[0]["id"]]["done_in"], 0)

    def test_a_gpu_without_runs_of_its_own_goes_by_the_others(self):
        self.any_gpu()
        self.gpu("big", 32, card="NVIDIA GeForce RTX 5090")
        job = self.submit()
        self.store.record_gpu_run("ltx25_alpha_gen", "NVIDIA GeForce RTX 3090", 3, 60)
        self.assertEqual(self.farm.annotate([job])[0]["eta_seconds"], 60)
        wan = self.farm.submit("wan22_i2i", {"input": f"{SEQ} 1001"}, "ann")
        self.store.update(wan["id"], priority=5)                            # first in the queue
        self.store.record_run("wan22_i2i", 12, None)                        # only the per-workflow average
        got = self.farm.annotate([self.job(job["id"]), self.job(wan["id"])])
        self.assertEqual([(j["queue_position"], j["eta_seconds"]) for j in got], [(2, 72), (1, 12)])   # behind Wan

    def test_finished_runs_are_recorded_per_gpu_type(self):
        a = self.ready(self.agent(comfy=FakeComfy(card="NVIDIA GeForce RTX 5090")))
        job = self.submit()
        pid = self.dispatched(a, job)
        self.assertEqual(self.finish(a, job["id"], pid)["status"], "done")
        [(key, row)] = self.store.run_stats().items()
        self.assertEqual((key, row["runs"], row["wx"]), (("ltx25_alpha_gen", "NVIDIA GeForce RTX 5090"), 1, 3))
        [rt] = self.farm.run_times()
        self.assertEqual((rt["label"], rt["gpu"], rt["runs"], rt["size"]),
                         (REGISTRY.workflows["ltx25_alpha_gen"].label, "NVIDIA GeForce RTX 5090", 1, 3))

    def test_jobs_go_only_to_agents_that_can_run_them(self):
        missing = {k: v for k, v in INSTALLED.items() if k != "LTXPadFrames"}
        a = self.ready(self.agent("gpu1", comfy=FakeComfy(missing)))
        self.assertIn("ltx25_alpha_gen", self.desk.summaries()[0]["problems"])
        job = self.submit()
        self.beat(a)
        failed = self.job(job["id"])                           # no worker can: failed, saying why
        self.assertEqual(failed["status"], "failed")
        self.assertIn("LTXPadFrames is not installed", failed["error"])
        b = self.ready(self.agent("gpu2"))
        job = self.submit()
        self.beat(a)
        self.assertEqual(self.job(job["id"])["status"], "queued")   # gpu2 can: it waits for gpu2
        self.beat(b)
        self.assertEqual(self.job(job["id"])["worker"], "gpu2")

    # ------------------------------------------------------------ one image (Wan I2I)
    def test_one_frame_of_a_sequence(self):
        a = self.ready(self.agent())
        job = self.farm.submit("wan22_i2i", {"input": f"{SEQ} 1002", "prompt": "fog"}, "ann")
        self.assertEqual((job["info"]["first_frame"], job["info"]["expected_frames"]), (1002, 1))
        pid = self.dispatched(a, job)
        prompt = a.runner.client.prompts[pid]
        self.assertEqual(prompt["254"]["inputs"]["path"],         # ComfyFarm Load Image finds the frame's file
                         f"{self.root}/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/"
                         f"abc_0010.pre.v1.%04d.png 1002")
        self.assertEqual(prompt["255"]["inputs"]["value"], "fog")
        done = self.finish(a, job["id"], pid)
        self.assertEqual(done["status"], "done", done["error"])
        self.assertEqual(os.listdir(done["output_dir"]), ["abc_0010.pre.v1.i2i_v001.1002.png"])   # numbered like it

    def test_one_frame_of_an_exr_sequence_is_converted(self):
        a = self.ready(self.agent())
        job = self.farm.submit("wan22_i2i", {"input": f"{EXR} 1003"}, "ann")
        pid = self.dispatched(a, job)
        path = a.runner.client.prompts[pid]["254"]["inputs"]["path"]
        self.assertEqual(path, f"{self.root}/scratch-gpu1/{job['id']}/abc_0010.plate.v1.1003.png")   # the converted file
        self.assertTrue(os.path.isfile(path))
        done = self.finish(a, job["id"], pid)
        self.assertEqual(os.listdir(done["output_dir"]), ["abc_0010.plate.v1.i2i_v001.1003.png"])

    def test_one_image_workflows_want_the_frame_named(self):
        shown = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/abc_0010.pre.v1.%04d.png"
        for given in (SEQ, f"{SEQ} 1001-1002", SEQ.rpartition("/")[0]):     # the whole sequence, a range, its folder
            with self.assertRaises(SequenceError) as e:
                self.farm.plan("wan22_i2i", {"input": given})
            self.assertIn(f"This workflow takes one image, and {shown} gives more (a sequence of", str(e.exception))
            self.assertIn(f"Put the frame to use after it, e.g. {shown} 1001.", str(e.exception))
        with self.assertRaisesRegex(SequenceError, r"Missing frames .*1004"):
            self.farm.plan("wan22_i2i", {"input": f"{SEQ} 1004"})
        Path(self.root, "shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/photo.png").write_bytes(b"x")
        plan = self.farm.plan("wan22_i2i", {"input": shown.rpartition("/")[0] + "/photo.png"})   # a still: as it is
        self.assertEqual((plan["seq"].frame_count, plan["first_file"].endswith(".0001.png")), (1, True))

    # ------------------------------------------------------------ several inputs
    def plate_matte(self):
        """The sample three-input workflow, on the farm and on the agent's ComfyUI."""
        wf = plate_matte_workflow(Path(self.tmp, "wf-pm"))
        REGISTRY.workflows[wf.name] = wf
        self.addCleanup(REGISTRY.workflows.pop, wf.name, None)
        oi = dict(INSTALLED)
        for cls, spec in fake_object_info(wf.template).items():
            oi.setdefault(cls, spec)
        return self.ready(self.agent(comfy=FakeComfy(oi)))

    # ------------------------------------------------------------ node ids stay on the agent
    def test_nothing_stored_for_a_job_names_a_node(self):
        """The manager plans by what a workflow takes and writes; which node does what is the
        agent's copy's to say (pinned by the job's fingerprint)."""
        def keys(x):
            if isinstance(x, dict):
                return set(x) | {k for v in x.values() for k in keys(v)}
            return {k for v in x for k in keys(v)} if isinstance(x, list) else set()
        job = self.submit()
        self.assertEqual(job["info"]["collect"]["frames"], {"ext": "png"})
        self.assertIs(job["info"]["collect"]["preview"], False)
        full = self.store.get(job["id"], with_prompt=True)
        self.assertNotIn("node", keys(full["info"]) | keys(full["request"]))     # texture jobs: test_sets

    def test_a_definition_an_agent_finds_broken_is_disabled_on_the_manager(self):
        """The manager checks a definition's head; how it wires the graph is checked where
        the graph is, and the agent's report disables it for everyone (decision 0020)."""
        folder = Path(self.tmp, "wf-broken", "wan_broken")
        shutil.copytree(REPO / "workflows/wan22_i2i", folder)
        p = yaml.safe_load((folder / "process.yaml").read_text(encoding="utf-8"))
        p["id"] = "wan_broken"
        p["runner"]["params"]["prompt"]["targets"][0]["node"] = "999"      # a node the graph doesn't have
        (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        why = "param prompt: node 999 is not in the workflow (re-exported? update the manifest)"
        mine = Definition(folder)                                           # the manager's copy: its head
        self.assertEqual((mine.problems, mine.enabled), ([], True))
        self.assertIn(why, Workflow(folder).problems)                       # `comfyfarm check`, CI, the agent
        REGISTRY.workflows["wan_broken"] = mine
        self.addCleanup(REGISTRY.workflows.pop, "wan_broken", None)
        a = self.agent()
        a.runner._registry = types.SimpleNamespace(workflows={**REGISTRY.workflows, "wan_broken": Workflow(folder)})
        self.ready(a)
        self.assertEqual((mine.enabled, mine.reported), (False, [why]))
        self.assertEqual(mine.schema()["problems"], [why])
        self.assertIn(why, self.desk.agents["gpu1"].problems["wan_broken"])  # and the machine is passed over
        with self.assertRaisesRegex(ParamError, "Workflow 'wan_broken' is disabled: param prompt: node 999"):
            self.farm.submit("wan_broken", {"input": f"{SEQ} 1001"}, "ann")
        self.assertEqual(self.desk.agents["gpu1"].problems.get("wan22_i2i"), None)  # the others run as before

    def own_preview(self) -> "Agent":
        """The plate/matte workflow with a video node of its own (outputs.preview)."""
        wf = plate_matte_workflow(Path(self.tmp, "wf-op"))
        m, t = (json.loads((wf.dir / f).read_text(encoding="utf-8")) for f in ("manifest.yaml", "workflow_api.json"))
        t["7"] = {"class_type": "VHS_VideoCombine", "inputs": {"images": ["1", 0], "filename_prefix": "x"}}
        m.update(name="own_preview", tag="op",
                 outputs=dict(m["outputs"], preview={"node": "7", "class": "VHS_VideoCombine",
                                                     "prefix_input": "filename_prefix"}))
        for f, d in (("manifest.yaml", m), ("workflow_api.json", t)):
            (wf.dir / f).write_text(json.dumps(d), encoding="utf-8")
        wf = Workflow(wf.dir)
        self.assertEqual(wf.problems, [])
        REGISTRY.workflows[wf.name] = wf
        self.addCleanup(REGISTRY.workflows.pop, wf.name, None)
        oi = dict(INSTALLED)
        for cls, spec in fake_object_info(wf.template).items():
            oi.setdefault(cls, spec)
        return self.ready(self.agent(comfy=FakeComfy(oi)))

    def test_a_workflows_own_preview_video_is_copied(self):
        a = self.own_preview()
        queued = []
        for old_style in (False, True):
            job = self.farm.submit("own_preview", {"plate": f"{SEQ} 1001-1002"}, "ann")
            self.assertIs(job["info"]["collect"]["preview"], True)
            if old_style:               # as a job queued before agents found their nodes: the section itself
                info = dict(job["info"], collect=dict(job["info"]["collect"], preview={"node": "7"}))
                self.store.update(job["id"], info=info)
            queued.append(job)
        for job in queued:
            pid = self.dispatched(a, job)
            self.msg(a, "execution_start", prompt_id=pid)
            info = a.held[job["id"]].job["info"]
            for i in (1, 2):
                Path(info["output_dir"], f"{info['frames_prefix']}_{i:04d}.png").write_bytes(b"png")
            a.runner.client.hist[pid] = {"outputs": {"7": {"gifs": [{"filename": "x.mp4", "subfolder": "",
                                                                     "type": "output"}]}},
                                         "status": {"status_str": "success", "completed": True}}
            self.msg(a, "execution_success", prompt_id=pid)
            self.until(lambda: self.job(job["id"])["status"] in ("done", "failed"), what="done")
            done = self.job(job["id"])
            with self.subTest(queued_before=job is queued[1]):
                self.assertEqual(done["status"], "done", done.get("error"))
                self.assertEqual(Path(done["outputs"]["preview"]).read_bytes(), b"mp4")
                self.assertEqual(done["warnings"] or [], [])

    def test_several_inputs_each_converted_into_its_own_folder(self):
        a = self.plate_matte()
        # The matte as a pattern alone: read at the plate's frames. An EXR: converted.
        job = self.farm.submit("plate_matte", {"plate": f"{SEQ} 1001-1003", "matte": EXR}, "ann")
        info = job["info"]
        self.assertEqual((info["inputs"]["matte"]["first"], info["inputs"]["matte"]["last"]), (1001, 1003))
        self.assertIsNone(info["inputs"]["reference"])
        self.assertEqual(info["inputs"]["matte"]["convert"]["dst_dir"], f"{self.root}/scratch/{job['id']}/matte/")
        pid = self.dispatched(a, job)
        prompt = a.runner.client.prompts[pid]
        scratch = f"{self.root}/scratch-gpu1/{job['id']}"
        self.assertEqual(prompt["1"]["inputs"]["frames"], "1001-1003")             # the plate, read in place
        self.assertTrue(prompt["1"]["inputs"]["path"].endswith("/hi/abc_0010.pre.v1.%04d.png"))
        self.assertEqual(prompt["2"]["inputs"], {"path": f"{scratch}/matte/abc_0010.plate.v1.%04d.png",
                                                 "frames": "1001-1003"})
        self.assertEqual(sorted(os.listdir(f"{scratch}/matte")), [f"abc_0010.plate.v1.{f}.png" for f in (1001, 1002, 1003)])
        self.assertEqual((prompt["4"]["inputs"]["value"], prompt["5"]["inputs"]["value"]), (True, False))
        self.assertEqual(prompt["3"]["inputs"], {"path": "", "optional": True})   # the reference, left empty
        done = self.finish(a, job["id"], pid)
        self.assertEqual(done["status"], "done", done["error"])
        self.assertEqual(sorted(os.listdir(done["output_dir"])),                  # named after the plate
                         [f"abc_0010.pre.v1.pm_v001.{f}.png" for f in (1001, 1002, 1003)])
        self.assertFalse(os.path.exists(scratch), "the matte's scratch folder is cleaned up too")

    def test_layout_to_render_with_its_reference_frame(self):
        a = self.ready(self.agent())
        with self.assertRaisesRegex(ParamError, "Reference frame is required"):
            self.farm.submit("ltx25_layout_render", {"input": f"{SEQ} 1001-1003", "prompt": "a castle"}, "ann")
        job = self.farm.submit("ltx25_layout_render", {"input": f"{SEQ} 1001-1003", "reference": f"{EXR} 1002",
                                                       "prompt": "a castle at dusk"}, "ann")
        pid = self.dispatched(a, job)
        prompt = a.runner.client.prompts[pid]
        self.assertEqual(prompt["9061"]["inputs"]["frames"], "1001-1003")             # the layout
        self.assertEqual(prompt["9060"]["inputs"]["path"],                           # the reference, converted
                         f"{self.root}/scratch-gpu1/{job['id']}/reference/abc_0010.plate.v1.1002.png")
        self.assertTrue(os.path.isfile(prompt["9060"]["inputs"]["path"]))
        self.assertEqual((prompt["5508"]["inputs"]["value"], prompt["5509"]["inputs"]["value"]), ("a castle at dusk", ""))
        done = self.finish(a, job["id"], pid)
        self.assertEqual(done["status"], "done", done["error"])
        self.assertEqual(sorted(os.listdir(done["output_dir"])),
                         [f"abc_0010.pre.v1.render_v001.{f}.png" for f in (1001, 1002, 1003)])

    def test_a_matte_must_cover_the_plates_frames(self):
        self.plate_matte()
        with self.assertRaisesRegex(SequenceError, r"^Matte has frames 1001-1002, and Plate 1001-1003: they must be "
                                                   r"the same frames\.$"):
            self.farm.plan("plate_matte", {"plate": f"{SEQ} 1001-1003", "matte": f"{EXR} 1001-1002"})
        with self.assertRaisesRegex(SequenceError, r"^Matte: Missing frames .*1004"):
            self.farm.plan("plate_matte", {"plate": f"{SEQ} 1001-1003", "matte": f"{EXR} 1001-1004"})
        with self.assertRaisesRegex(SequenceError, r"^Reference takes one image, and .* gives more \(a sequence of 3 "
                                                   r"frames, 1001-1003\)"):
            self.farm.plan("plate_matte", {"plate": f"{SEQ} 1001-1003", "reference": SEQ})
        plan = self.farm.plan("plate_matte", {"plate": f"{SEQ} 1002", "reference": f"{SEQ} 1001"})
        self.assertEqual(self.farm.plan_summary(plan)["inputs"]["reference"]["first"], 1001)
        self.assertIsNone(self.farm.plan_summary(plan)["inputs"]["matte"])

    # ------------------------------------------------------------ farm nodes after a pull
    def test_old_farm_nodes_restart_comfyui_once_idle(self):
        a = self.ready(self.agent())
        job = self.submit()
        pid = self.dispatched(a, job)
        # A `git pull` changed the farm nodes; ComfyUI still runs the old ones.
        a.runner.client.oi = old_farm_nodes(INSTALLED)
        a.runner.launcher = FakeLauncher(a.runner.client, then=INSTALLED)
        with self.assertLogs("comfyfarm", "WARNING"):
            self.wait(a.runner.refresh_object_info())
        w = self.desk.summaries()[0]
        self.assertEqual(w["farm_nodes"]["state"], "waiting")
        self.assertIn("runs version 0123456789ab from /old/checkout", w["farm_nodes"]["message"])
        self.assertFalse(self.desk.accepting(self.desk.agents["gpu1"]))      # no new jobs
        nxt = self.submit()
        self.beat(a)
        self.assertEqual(self.job(nxt["id"])["status"], "queued")
        self.wait(a.runner._farm_nodes_tick(True))
        self.assertEqual(a.runner.launcher.restarts, 0)                          # it still runs a job
        self.assertEqual(self.finish(a, job["id"], pid)["status"], "done")
        self.wait(a.runner._farm_nodes_tick(True))
        self.assertEqual(a.runner.launcher.restarts, 1)                          # idle: restarted
        self.assertEqual(self.desk.summaries()[0]["farm_nodes"]["state"], "restarting")
        self.wait(a.runner.refresh_object_info())                                # back, with the new nodes
        self.assertIsNone(a.runner.farm_nodes)
        self.assertIsNone(self.desk.summaries()[0]["farm_nodes"])
        self.beat(a)
        self.assertEqual(self.job(nxt["id"])["worker"], "gpu1")           # taking jobs again

    def test_old_farm_nodes_without_comfyui_launch_wait_for_a_restart_by_hand(self):
        comfy = FakeComfy(old_farm_nodes(INSTALLED))
        a = self.agent(comfy=comfy)
        a.runner.launcher = FakeLauncher(comfy, then=INSTALLED, enabled=False)
        with self.assertLogs("comfyfarm", "WARNING"):
            self.ready(a)
        self.wait(a.runner._farm_nodes_tick(True))
        self.assertEqual(a.runner.launcher.restarts, 0)
        w = self.desk.summaries()[0]
        self.assertEqual(w["farm_nodes"]["state"], "manual")
        self.assertTrue(w["farm_nodes"]["message"].endswith("Restart ComfyUI to load them."))
        self.assertEqual(self.farm.online_workers(), 0)

    def test_a_comfyui_the_agent_didnt_start_is_left_to_restart_by_hand(self):
        comfy = FakeComfy(old_farm_nodes(INSTALLED))
        a = self.agent(comfy=comfy)
        a.runner.launcher = FakeLauncher(comfy, then="not ours")
        with self.assertLogs("comfyfarm", "WARNING"):
            self.ready(a)
            self.wait(a.runner._farm_nodes_tick(True))
            self.wait(a.runner._farm_nodes_tick(True))
        self.assertEqual(a.runner.launcher.restarts, 1)
        self.assertEqual(a.runner.farm_nodes["state"], "manual")
        self.assertIn("This agent didn't start ComfyUI, so it can't restart it: Restart ComfyUI to load them.",
                      a.runner.farm_nodes["message"])

    def test_a_restart_that_doesnt_bring_the_checkouts_nodes_says_so(self):
        comfy = FakeComfy(old_farm_nodes(INSTALLED))
        a = self.agent(comfy=comfy)
        a.runner.launcher = FakeLauncher(comfy, then=None)                       # ComfyUI loads them from elsewhere
        with self.assertLogs("comfyfarm", "WARNING"):
            self.ready(a)
            self.wait(a.runner._farm_nodes_tick(True))
            self.wait(a.runner.refresh_object_info())
            self.wait(a.runner._farm_nodes_tick(True))
        self.assertEqual(a.runner.launcher.restarts, 1)                          # once, not in a loop
        self.assertEqual(a.runner.farm_nodes["state"], "manual")
        self.assertIn("Restarting ComfyUI didn't change that: check that ComfyUI's custom_nodes/comfyfarm_nodes "
                      "links to this checkout", a.runner.farm_nodes["message"])

    def test_the_agent_on_its_own(self):
        """Its loops as `comfyfarm agent` runs them: it checks in, takes and runs a job, and
        keeps its reports while the manager is away."""
        a = self.agent(heartbeat_wait=1, health_seconds=0.05, reconcile_seconds=0.2)

        async def ws_loop():            # ComfyUI's websocket: up; its messages come from the test
            a.runner.ws_connected = True
            await a.runner.refresh_object_info()
            await asyncio.Event().wait()
        a.runner._ws_loop = ws_loop

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
        a.runner.client.pending.discard(pid)
        a.runner.client.hist[pid] = {"outputs": {}, "status": {"status_str": "success", "completed": True}}
        self.msg(a, "execution_success", prompt_id=pid)
        self.until(lambda: a.held[job["id"]].finished, what="collected")
        self.until(lambda: a.manager_down, what="manager seen down")
        self.assertEqual(self.job(job["id"])["status"], "dispatched")
        a.link.down = False                                    # back: the reports go, in order
        self.until(lambda: self.job(job["id"])["status"] == "done", what="done")
        self.assertEqual(self.job(job["id"])["outputs"]["frames"], 3)
        self.until(lambda: not a.manager_down and a.held == {}, what="manager back")

    def test_agent_check_lists_each_storage_once(self):
        from comfyfarm.config import PathMapping
        self.cfg.paths.mappings.append(PathMapping(posix="/shows", windows=f"{self.root}/shows"))   # another name
        self.cfg.paths.mappings.append(PathMapping(posix="/rdo/bank", windows=f"{self.root}/bank"))
        self.assertEqual(self.desk.ping({"name": "gpu1"})["storage"], ["/rdo/shows", "/rdo/bank"])

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


    # ---------------------------------------------------------------- requests (decisions 0013, 0014, 0016)
    def test_the_manager_stores_a_request_and_the_agent_builds_the_graph(self):
        a = self.gpu("gpu1", 32)
        job = self.submit()
        stored = self.store.get(job["id"], with_prompt=True)
        self.assertIsNone(stored["prompt"])
        wf = REGISTRY.workflows["ltx25_alpha_gen"]
        self.assertEqual(stored["request"]["definition"], wf.digest)
        with self.assertRaisesRegex(FarmError, "once a machine has started it"):     # the dashboard's API JSON
            self.farm.submitted(stored)
        a.runner._registry = None               # as a real agent: its own checkout's workflows/, read now
        self.beat(a)
        self.until(lambda: a.runner.client.prompts, what="the graph in ComfyUI")
        self.assertEqual(sorted(a.runner._registry.workflows), sorted(WorkflowRegistry(REPO / "workflows").workflows))
        (sent,) = a.runner.client.prompts.values()
        self.assertEqual(sent, W.localize(W.portable(wf.build_from(stored["request"]), self.desk.view), a.view))
        # The agent tells the manager what it queued, and that is what the dashboard hands out.
        self.until(lambda: self.job(job["id"])["status"] == "dispatched", what="the run reported")
        self.assertEqual(self.farm.submitted(self.store.get(job["id"], with_prompt=True)), sent)
        self.assertNotIn("prompt", self.desk._payload(self.store.get(job["id"], with_prompt=True),
                                                      self.desk.agents["gpu1"]))        # never sent back out

    def test_a_set_has_no_api_json_of_its_own(self):
        with self.assertRaisesRegex(FarmError, "each of its batches has one"):
            self.farm.submitted({"kind": "set", "prompt": None})

    def stale(self, a, workflow="ltx25_alpha_gen"):
        """Give the agent a checkout of its own whose copy of the workflow is another one."""
        own = WorkflowRegistry(REPO / "workflows")
        own.workflows[workflow].digest = "0" * 16
        a.runner._registry = own
        return a

    def test_the_agent_says_what_it_can_run_and_sends_no_node_definitions(self):
        sent = []
        a = self.agent("gpu1")
        nodes = a.link.nodes

        async def spy(msg):
            sent.append(msg)
            return await nodes(msg)
        a.link.nodes = spy
        self.ready(a)
        self.assertEqual([sorted(m) for m in sent], [["name", "processes"]])
        told = sent[0]["processes"]
        self.assertEqual(sorted(told), sorted(REGISTRY.workflows))
        wf = REGISTRY.workflows["wan22_i2i"]
        self.assertEqual(told["wan22_i2i"], {"digest": wf.digest, "problems": [], "broken": [],
                                             "expected_nodes": wf.expected_nodes(a.runner.output_classes)})
        state = self.desk.agents["gpu1"]
        self.assertEqual((state.problems, state.checked), ({}, True))
        self.assertEqual(state.expected["wan22_i2i"], told["wan22_i2i"]["expected_nodes"])

    def test_a_machine_with_another_copy_of_the_workflow_is_passed_over(self):
        self.any_gpu()
        old = self.stale(self.agent("old"))
        self.ready(old)
        self.assertRegex(self.desk.agents["old"].problems["ltx25_alpha_gen"][0],
                         r"its copy of the workflow \(0{16}\) and the manager's \([0-9a-f]{16}\) differ: update")
        self.assertEqual(list(self.desk.agents["old"].problems), ["ltx25_alpha_gen"])   # its other workflows are fine
        good = self.gpu("good", 32)
        job = self.submit()
        self.beat(old)
        self.assertEqual(old.held, {})
        self.beat(good)
        self.until(lambda: good.runner.client.prompts, what="the job on the machine that matches")
        self.assertEqual(self.job(job["id"])["worker"], "good")

    def test_a_checkout_that_changed_after_it_was_reported_refuses_the_job(self):
        a = self.gpu("gpu1", 32)
        job = self.submit()
        self.stale(a)                                               # pulled, or edited, since it checked in
        self.beat(a)
        self.until(lambda: self.job(job["id"])["status"] == "failed", what="the job refused")
        self.assertRegex(self.job(job["id"])["error"],
                         r"ltx25_alpha_gen on this machine \(0{16}\) and the one the manager planned the job with")
        self.assertEqual(a.runner.client.prompts, {})

    def test_a_machine_whose_checkout_lacks_the_workflow(self):
        a = self.agent("gpu1")
        own = WorkflowRegistry(REPO / "workflows")
        del own.workflows["wan22_i2i"]
        a.runner._registry = own
        self.ready(a)
        self.assertEqual(self.desk.agents["gpu1"].problems["wan22_i2i"],
                         ["gpu1 has no workflow wan22_i2i: update the farm's code on it"])

    def test_a_job_from_before_requests_is_refused_with_what_to_do(self):
        a = self.gpu("gpu1", 32)
        job = self.submit()
        wf = REGISTRY.workflows["ltx25_alpha_gen"]
        full = self.store.get(job["id"], with_prompt=True)
        self.store.update(job["id"], prompt=wf.build_from(full["request"]), request=None)   # as the manager stored it
        self.beat(a)
        self.until(lambda: self.job(job["id"])["status"] == "failed", what="the job refused")
        self.assertRegex(self.job(job["id"])["error"],
                         "queued before the farm changed how it stores jobs.*Submit it again")
        self.assertEqual(a.runner.client.prompts, {})

    def test_an_agent_too_old_to_say_what_it_can_run_is_passed_over(self):
        a = self.agent("old")
        self.wait(a._hello())
        self.assertEqual(self.desk.nodes({"name": "old", "object_info": {}})["problems"]["wan22_i2i"],
                         ["old runs farm code too old to say what it can run: update the farm's code on it"])
        self.assertEqual(sorted(self.desk.agents["old"].problems), sorted(REGISTRY.workflows))


if __name__ == "__main__":
    unittest.main()
