"""The ComfyUI runner's sync when the manager says (runners/comfyui.py, decision 0033): this
checkout's environment file must be the version the manager names; the hold in place,
ComfyUI stopped, the steps done in order until one fails, the hold gone, ComfyUI started
again and waited for, then compared again. The steps and the launcher are stand-ins here:
comfyenv.sync's own steps have their tests (test_comfyenv_sync), the launcher its own
(test_agent_host).

    python -m unittest tests.test_comfyui_sync -v
"""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from comfyfarm.comfyenv import spec as ES
from comfyfarm.comfyenv import sync as Y
from comfyfarm.config import AgentConfig

try:
    from comfyfarm.runners.comfyui import ComfyUIRunner
except ImportError:   # pragma: no cover
    ComfyUIRunner = None

DIFFERS = [{"what": "comfyui", "message": "ComfyUI v0.3.75 (1a2b3c4), the farm's v0.3.77 (5d6e7f8)"}]


class Fingerprint(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.spec, self.paths = self.tmp / "environment.yaml", self.tmp / "extra_model_paths.yaml"

    def test_the_version_both_files_make_whatever_the_line_endings(self):
        self.assertIsNone(ES.fingerprint(self.spec, self.paths))            # no environment file: nothing to sync to
        self.spec.write_bytes(b"comfyui:\n  version: v0.3.77\n")
        self.paths.write_bytes(b"farm:\n  base_path: /rdo/bank/comfyui\n")
        linux = ES.fingerprint(self.spec, self.paths)
        self.assertEqual(len(linux), 16)
        self.spec.write_bytes(b"comfyui:\r\n  version: v0.3.77\r\n")       # a Windows checkout's
        self.assertEqual(ES.fingerprint(self.spec, self.paths), linux)
        self.paths.write_bytes(b"farm:\n  base_path: /rdo/bank/comfy_farm/comfyui\n")
        self.assertNotEqual(ES.fingerprint(self.spec, self.paths), linux)


class Client:
    def __init__(self):
        self.queued = (set(), set())

    async def queue(self):
        return self.queued

    async def close(self):
        pass


class Host:
    name, registered, tags = "gpu1", False, ["linux"]

    def __init__(self):
        self.held: list = []

    def runs(self):
        return list(self.held)


class Launcher:
    """ComfyUI as far as stopping and starting it goes: it comes back as soon as it starts."""

    def __init__(self, tmp: Path, runner):
        self.enabled, self.runner, self.pid = True, runner, 4242
        self.hold_file, self.log_file = tmp / "comfyui-gpu1.hold", tmp / "comfyui-gpu1.log"
        self.stops = self.starts = 0

    def held(self):
        return self.hold_file.read_text().partition(" ")[2].strip() if self.hold_file.exists() else None

    def running_pid(self):
        return self.pid

    def stop(self):
        self.stops += 1
        pid, self.pid = self.pid, None
        return pid

    def start(self):
        self.starts += 1
        self.pid = 5000
        r = self.runner
        r.online = r.ws_connected = r.checked = True
        r.object_info = {"KSampler": {}}


@unittest.skipIf(ComfyUIRunner is None, "needs the agent's dependencies (httpx, websockets)")
class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runner = self.make()
        self.said: list[dict] = []
        self.ran: list[str] = []

    def make(self, **settings):
        settings = {"envs": {"managed": True}, "comfyui_launch": {"command": ["python", "main.py"]}, **settings}
        cfg = AgentConfig(name="gpu1", comfyui="http://gpu1:8188", base_dir=self.tmp, **settings)
        r = ComfyUIRunner(cfg, client=Client())
        r.attach(Host())
        r.launcher = Launcher(self.tmp, r)
        r.env = ES.Cached(self.tmp / "environment.yaml", self.tmp / "extra_model_paths.yaml")
        r.env.path.write_text("comfyui:\n  version: v0.3.77\n", encoding="utf-8")
        r.env.model_paths_path.write_text("farm:\n  base_path: /rdo/bank/comfyui\n", encoding="utf-8")
        r.online = r.ws_connected = r.checked = True
        r.object_info = {"KSampler": {}}
        return r

    def file(self):
        return ES.fingerprint(self.runner.env.path, self.runner.env.model_paths_path)

    def step(self, title, fails=None):
        def run():
            # Done with ComfyUI down and held: the agent takes no jobs, the launcher doesn't start it.
            self.assertEqual((self.runner.launcher.stops, self.runner.launcher.starts), (1, 0))
            self.assertTrue(self.runner.launcher.hold_file.exists())
            self.assertEqual(self.runner.farm_nodes["state"], "syncing")
            self.ran.append(title)
            if fails:
                raise Y.SyncError(fails)
            return f"{title}: done"
        return Y.Step(title, run)

    def sync(self, *plans, **order):
        self.runner._sync_plan = mock.AsyncMock(side_effect=list(plans))

        async def say(rec):
            self.said.append(rec)
        return asyncio.run(self.runner.sync({"id": "s1", "file": self.file(), **order}, say))

    def test_stopped_updated_started_again_and_compared(self):
        before = Y.Plan([self.step("ComfyUI v0.3.75 → v0.3.77"), self.step("pip install")], [], DIFFERS)
        after = Y.Plan([], ["torch: sync keeps it as it is"], [{"what": "package", "message": "torch 2.8.0"}])
        rec = self.sync(before, after)
        self.assertEqual((rec["state"], rec["error"]), ("done", None))
        self.assertEqual(self.ran, ["ComfyUI v0.3.75 → v0.3.77", "pip install"])
        self.assertEqual(rec["steps"], [["stop ComfyUI", "stopped (pid 4242)"],
                                        ["ComfyUI v0.3.75 → v0.3.77", "ComfyUI v0.3.75 → v0.3.77: done"],
                                        ["pip install", "pip install: done"]])
        self.assertEqual((rec["differs"], rec["manual"]), (["torch 2.8.0"], ["torch: sync keeps it as it is"]))
        self.assertEqual(rec["note"], "synced; what is left is by hand")
        self.assertEqual([s["state"] for s in self.said],
                         ["checking", "stopping", "applying", "applying", "starting", "done"])
        self.assertFalse(self.runner.launcher.hold_file.exists())
        self.assertEqual((self.runner.launcher.starts, self.runner.farm_nodes), (1, None))

    def test_a_step_that_fails_stops_the_rest_and_comfyui_is_started_again(self):
        before = Y.Plan([self.step("ComfyUI → v0.3.77", fails="commit 5d6e7f8 isn't in its origin"),
                         self.step("pip install")], [], DIFFERS)
        rec = self.sync(before, Y.Plan([], [], DIFFERS))
        self.assertEqual(rec["state"], "failed")
        self.assertEqual(self.ran, ["ComfyUI → v0.3.77"])
        self.assertIn("ComfyUI → v0.3.77 failed:\ncommit 5d6e7f8 isn't in its origin", rec["error"])
        self.assertIn("ComfyUI was started again", rec["error"])
        self.assertEqual(rec["differs"], [DIFFERS[0]["message"]])
        self.assertFalse(self.runner.launcher.hold_file.exists())
        self.assertEqual(self.runner.launcher.starts, 1)

    def test_another_version_of_the_file_is_refused_untouched(self):
        self.runner._sync_plan = mock.AsyncMock()
        rec = asyncio.run(self.runner.sync({"id": "s1", "file": "0123456789abcdef"}, mock.AsyncMock()))
        self.assertEqual(rec["state"], "failed")
        self.assertIn(f"another version of the farm's ComfyUI environment ({self.file()}) than the manager's host "
                      f"(0123456789abcdef): update its checkout (git pull)", rec["error"])
        self.assertEqual((rec["steps"], self.runner.launcher.stops), ([], 0))
        self.runner._sync_plan.assert_not_called()

    def test_nothing_to_do_leaves_comfyui_running(self):
        rec = self.sync(Y.Plan([], [], []))
        self.assertEqual((rec["state"], rec["note"]), ("done", "it matches the farm's ComfyUI: nothing to do"))
        rec = self.sync(Y.Plan([], ["NVIDIA driver 550: the farm's torch needs 570 or newer"], DIFFERS))
        self.assertEqual((rec["state"], rec["manual"]), ("done", ["NVIDIA driver 550: the farm's torch needs 570 or "
                                                                  "newer"]))
        self.assertEqual(self.runner.launcher.stops, 0)

    def test_what_it_refuses(self):
        plan = Y.Plan([self.step("pip install")], [], DIFFERS)
        cases = []
        self.runner = self.make(envs={"managed": False})
        cases.append(("envs.managed is off in this machine's agent.yaml", self.sync(plan)))
        self.runner = self.make()
        self.runner.launcher.enabled = False                                # no comfyui_launch.command
        cases.append(("this agent doesn't start ComfyUI (comfyui_launch in agent.yaml)", self.sync(plan)))
        self.runner = self.make()
        self.runner.host.held = [mock.Mock(runner=self.runner)]
        cases.append(("it holds 1 ComfyUI job(s): drain it and wait for them first", self.sync(plan)))
        self.runner = self.make()
        self.runner.client.queued = ({"p1"}, set())
        cases.append(("ComfyUI has 1 prompt(s) running or queued that aren't the farm's", self.sync(plan)))
        self.runner = self.make()
        self.runner.launcher.pid = None
        cases.append(("ComfyUI runs here, but this agent didn't start it, so it won't stop it", self.sync(plan)))
        for why, rec in cases:
            with self.subTest(why):
                self.assertEqual(rec["state"], "failed")
                self.assertIn(why, rec["error"])
                self.assertEqual(rec["steps"], [])
        self.assertEqual(self.ran, [])

    def test_comfyui_that_doesnt_come_back_fails_it(self):
        self.runner.launcher.start = lambda: None                          # it never answers
        self.runner._back = mock.AsyncMock(return_value=False)
        rec = self.sync(Y.Plan([self.step("pip install")], [], DIFFERS))
        self.assertEqual(rec["state"], "failed")
        self.assertIn("ComfyUI didn't come back within 360 s after the sync: see", rec["error"])
        self.assertFalse(self.runner.launcher.hold_file.exists())


if __name__ == "__main__":
    unittest.main()
