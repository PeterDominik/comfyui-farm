"""Agents report the runner environments they have built (decision 0029): what is on disk
(envs/build.report), sent with the agent's status and read again every ENVS_SECONDS, kept by
the manager for each agent, and listed beside the bank's releases.

    python -m unittest tests.test_env_report -v
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_envs import UV, make  # noqa: E402

from comfyfarm.agent.agent import Agent  # noqa: E402
from comfyfarm.config import AgentConfig, Config  # noqa: E402
from comfyfarm.envs import build as B  # noqa: E402
from comfyfarm.envs import release as R  # noqa: E402
from comfyfarm.envs import spec as S  # noqa: E402
from comfyfarm.manager import share  # noqa: E402


class Machine(unittest.TestCase):
    """A machine's builds folder with environments built by the stand-in uv."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.envs = self.tmp / "repo" / "environments"
        self.builds = self.tmp / "machine" / "data" / "envs"
        for patch in (mock.patch.object(S, "ENVS", self.envs), mock.patch.dict(os.environ, {"FAKE_BUILDER_LOG": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def build(self, name="demo", deps=("numpy==2.5.2",), origin=None):
        make(self.envs, name, deps=deps, check=["python", "-c", "pass"])
        B.lock(S.load(name), UV, self.builds)
        spec = S.load(name)
        B.install(spec, self.builds, UV, log=lambda _m: None, origin=origin)
        return spec


class Report(Machine):
    def test_what_is_built_with_its_current_and_previous(self):
        self.assertEqual(B.report(self.builds), {})                  # nothing built (no folder yet)
        one = self.build()
        two = self.build(deps=("numpy==2.5.2", "pillow==12.3.0"), origin={"release": "x", "commit": "abc"})
        other = self.build("tools")
        got = B.report(self.builds)
        self.assertEqual(sorted(got), ["demo", "tools"])                # not .uv-cache, .uv-python, .uv-tool
        demo = got["demo"]
        self.assertEqual((demo["current"], demo["previous"]), (two.fingerprint, one.fingerprint))
        self.assertEqual(sorted(demo["builds"]), sorted([one.fingerprint, two.fingerprint]))
        b = demo["builds"][two.fingerprint]
        self.assertEqual(sorted(b), ["build_seconds", "built_at", "copied", "ok", "origin"])
        self.assertEqual((b["ok"], b["copied"], b["origin"]), (True, False, {"release": "x", "commit": "abc"}))
        self.assertEqual(demo["builds"][one.fingerprint]["origin"], {"checkout": str(self.envs / "demo")})
        self.assertEqual(got["tools"]["current"], other.fingerprint)
        B.remove(self.builds, "tools")
        self.assertEqual(sorted(B.report(self.builds)), ["demo"])


class Link:
    def __init__(self):
        self.statuses: list[dict] = []

    async def status(self, msg):
        self.statuses.append(msg)
        return {}


class Runner:
    label, ready, max_runs = "ComfyUI", True, None
    object_info = farm_nodes = environment = None

    def attach(self, host):
        self.host = host

    def start(self):
        return []

    async def close(self):
        pass

    def status(self):
        return {"online": True, "connected": True}

    def processes(self):
        return {}

    def knows(self, process):
        return False


class AgentSends(Machine):
    def test_it_sends_what_it_has_built_with_its_status(self):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        link = Link()
        a = Agent(AgentConfig(name="gpu1", base_dir=self.tmp / "machine"), link, runners=[Runner()])
        a.registered = True
        self.assertTrue(loop.run_until_complete(a.refresh_environments()))     # None -> {}: a change
        self.assertEqual(a.environments, {})
        self.assertFalse(loop.run_until_complete(a.refresh_environments()))
        spec = self.build()
        self.assertTrue(loop.run_until_complete(a.refresh_environments()))
        loop.run_until_complete(a.send_status())
        sent = link.statuses[-1]["environments"]
        self.assertEqual(sent["demo"]["current"], spec.fingerprint)
        # The loop sends it once it changes, without waiting for anything else.
        a.ENVS_SECONDS = 0.05
        self.build(deps=("numpy==2.5.2", "pillow==12.3.0"))

        async def loop_a_while():
            task = asyncio.ensure_future(a._envs_loop())
            await asyncio.sleep(0.5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        n = len(link.statuses)
        loop.run_until_complete(loop_a_while())
        self.assertEqual(len(link.statuses), n + 1)                      # once: it changed once
        self.assertEqual(link.statuses[-1]["environments"]["demo"]["previous"], spec.fingerprint)


class ManagerKeeps(Machine):
    def test_the_manager_keeps_each_agents_and_lists_the_banks_releases(self):
        from comfyfarm.manager.desk import Desk
        from comfyfarm.manager.db import JobStore
        from comfyfarm.manager.events import EventBus
        from comfyfarm.manager.farm import Farm
        from comfyfarm.definitions import DefinitionRegistry
        spec = self.build()
        bank = self.tmp / "bank"
        R.publish(spec, bank, commit="abc123")
        cfg = Config.model_validate({"database": str(self.tmp / "farm.db"), "workers": [],
                                     "workflows_dir": str(self.tmp / "no-workflows"), "share": {"dir": str(bank)}})
        (self.tmp / "no-workflows").mkdir()
        store = JobStore(self.tmp / "farm.db")
        self.addCleanup(store.close)
        desk = Desk(Farm(cfg, store, DefinitionRegistry(self.tmp / "no-workflows"), EventBus()))
        desk.hello({"name": "gpu1", "comfyui": "http://gpu1:8188", "max_inflight": 1, "jobs": []})
        report = B.report(self.builds)
        desk.status({"name": "gpu1", "online": True, "connected": True, "environments": report})
        self.assertEqual(desk.environments(), {"gpu1": report})
        self.assertEqual(desk.summary(desk.agents["gpu1"])["environments"], report)
        desk.status({"name": "gpu1", "online": True, "connected": True})     # an older agent: kept as it was
        self.assertEqual(desk.environments(), {"gpu1": report})
        listed = share.releases(cfg)
        self.assertEqual([(r["name"], r["fingerprint"], r["commit"]) for r in listed],
                         [("demo", spec.fingerprint, "abc123")])
        cfg.share.dir = ""
        self.assertEqual(share.releases(cfg), [])


if __name__ == "__main__":
    unittest.main()
