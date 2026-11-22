"""Rollouts (manager/rollouts.py, decision 0031): a release to the machines one at a time, the
canary first, each gated by its install's check, with pause, rollback and cancel. First the
rollout alone, with what machines report set by hand; then three real agents with their
environment managers, through the real desk, where a broken release stops at the canary.

    python -m unittest tests.test_rollouts -v
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_envs import UV, make  # noqa: E402
from test_envman import Runner  # noqa: E402

from comfyfarm.agent.agent import Agent  # noqa: E402
from comfyfarm.agent.link import LocalLink  # noqa: E402
from comfyfarm.config import AgentConfig, Config  # noqa: E402
from comfyfarm.definitions import DefinitionRegistry  # noqa: E402
from comfyfarm.envs import build as B  # noqa: E402
from comfyfarm.envs import release as R  # noqa: E402
from comfyfarm.envs import spec as S  # noqa: E402
from comfyfarm.manager import rollouts as RO  # noqa: E402
from comfyfarm.manager.db import JobStore  # noqa: E402
from comfyfarm.manager.desk import Desk  # noqa: E402
from comfyfarm.manager.events import EventBus  # noqa: E402
from comfyfarm.manager.farm import Farm  # noqa: E402
from comfyfarm.protocol import now  # noqa: E402

ORDER = ["tor-flame02-a6000", "wrk59467w-5090", "wrk1125l-3090"]


class Manager(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "workflows").mkdir()
        self.bank = self.tmp / "bank"
        self.bank.mkdir()
        self.cfg = Config.model_validate({"database": str(self.tmp / "farm.db"), "workers": [],
                                          "workflows_dir": str(self.tmp / "workflows"),
                                          "share": {"dir": str(self.bank)},
                                          "rollouts": {"order": ORDER, "step_minutes": 30}})
        self.store = JobStore(self.tmp / "farm.db")
        self.addCleanup(self.store.close)
        self.desk = self.new_desk()

    def new_desk(self):
        return Desk(Farm(self.cfg, self.store, DefinitionRegistry(self.tmp / "workflows"), EventBus()))


class Alone(Manager):
    """The rollout with what the machines report set by hand."""

    def setUp(self):
        super().setUp()
        for m in ("wrk1125l-3090", "wrk59467w-5090", "tor-flame02-a6000"):
            self.desk.hello({"name": m, "comfyui": "x", "max_inflight": 1, "jobs": []})
            self.has(m, "old")

    def has(self, m, release, ok=True):
        self.desk.agents[m].envs = {"demo": {"current": release, "previous": None,
                                             "builds": {release: {"ok": ok}}}}

    def says(self, m, state, error=None):
        self.desk.agents[m].env_work = {"demo": {"want": "new", "state": state, "error": error}}

    def wanted(self):
        return {m: w.get("demo") for m, w in self.store.env_wanted().items()}

    def test_the_canary_first_then_one_at_a_time(self):
        r = self.desk.rollouts.start("demo", "new", None, "peter")
        self.assertEqual((r["machines"], r["state"], r["step"]), (ORDER, "running", 0))
        self.assertEqual(r["before"], {m: "old" for m in ORDER})
        self.assertEqual(self.wanted(), {"tor-flame02-a6000": "new"})     # the canary alone is told
        self.desk.rollouts.tick()
        self.assertEqual(self.wanted(), {"tor-flame02-a6000": "new"})     # until it has it
        self.has("tor-flame02-a6000", "new")
        self.desk.rollouts.tick()
        self.assertEqual(self.wanted(), {"tor-flame02-a6000": "new", "wrk59467w-5090": "new"})
        self.has("wrk59467w-5090", "new")
        self.desk.rollouts.tick()
        self.has("wrk1125l-3090", "new")
        self.desk.rollouts.tick()
        r = self.desk.rollouts.recent(1)[0]
        self.assertEqual((r["state"], r["step"], r["error"]), ("done", 2, None))
        self.assertEqual(self.wanted(), {m: "new" for m in ORDER})

    def test_a_broken_release_stops_at_the_canary(self):
        self.desk.rollouts.start("demo", "new", None, "peter")
        self.says("tor-flame02-a6000", "failed", "the check failed: torch sees no GPU")
        self.desk.rollouts.tick()
        r = self.desk.rollouts.open("demo")
        self.assertEqual((r["state"], r["step"]), ("failed", 0))
        self.assertEqual(r["error"], "tor-flame02-a6000: the check failed: torch sees no GPU")
        self.desk.rollouts.tick()
        self.assertEqual(self.wanted(), {"tor-flame02-a6000": "new"})     # the others never heard of it
        with self.assertRaises(RO.RolloutError) as e:
            self.desk.rollouts.act("demo", "resume", "peter")
        self.assertIn("a failed one is rolled back or cancelled", str(e.exception))
        r = self.desk.rollouts.act("demo", "rollback", "peter")
        self.assertEqual(r["state"], "rolled_back")
        self.assertEqual(self.wanted(), {"tor-flame02-a6000": "old"})     # back to what it had
        self.assertIsNone(self.desk.rollouts.open("demo"))

    def test_a_machine_with_the_manager_off_stops_it_too(self):
        self.desk.rollouts.start("demo", "new", None, "peter")
        self.says("tor-flame02-a6000", "off", "envs.managed is off in this machine's agent.yaml")
        self.desk.rollouts.tick()
        self.assertIn("envs.managed is off", self.desk.rollouts.open("demo")["error"])

    def test_a_machine_too_slow_stops_it(self):
        r = self.desk.rollouts.start("demo", "new", None, "peter")
        r["step_started"] = now() - 31 * 60
        self.store.save_rollout(r)
        self.desk.rollouts.tick()
        r = self.desk.rollouts.open("demo")
        self.assertEqual(r["state"], "failed")
        self.assertIn("tor-flame02-a6000 didn't have it within 30 minutes (says nothing)", r["error"])

    def test_pause_resume_and_cancel(self):
        self.desk.rollouts.start("demo", "new", None, "peter")
        self.desk.rollouts.act("demo", "pause", "peter")
        self.has("tor-flame02-a6000", "new")
        self.desk.rollouts.tick()
        self.assertEqual(self.wanted(), {"tor-flame02-a6000": "new"})     # paused: no next machine
        self.desk.rollouts.act("demo", "resume", "peter")
        self.desk.rollouts.tick()
        self.assertIn("wrk59467w-5090", self.wanted())
        r = self.desk.rollouts.act("demo", "cancel", "peter")
        self.assertEqual(r["state"], "cancelled")
        self.assertEqual(self.wanted()["wrk59467w-5090"], "new")          # cancelled: kept as it is
        with self.assertRaises(RO.RolloutError):
            self.desk.rollouts.act("demo", "pause", "peter")

    def test_rollback_after_it_is_done_reaches_every_machine(self):
        self.has("wrk1125l-3090", None)                     # the manager's host had none of it
        self.desk.agents["wrk1125l-3090"].envs = {}
        self.desk.rollouts.start("demo", "new", None, "peter")
        for m in ORDER:
            self.has(m, "new")
            self.desk.rollouts.tick()
        self.assertEqual(self.desk.rollouts.recent(1)[0]["state"], "done")
        self.desk.rollouts.act("demo", "rollback", "peter")
        self.assertEqual(self.wanted(), {"tor-flame02-a6000": "old", "wrk59467w-5090": "old", "wrk1125l-3090": None})

    def test_one_open_rollout_an_environment_and_it_outlives_the_manager(self):
        self.desk.rollouts.start("demo", "new", ["wrk1125l-3090", "tor-flame02-a6000"], "peter")
        with self.assertRaises(RO.RolloutError) as e:
            self.desk.rollouts.start("demo", "newer", None, "peter")
        self.assertIn("a rollout of demo is open (running)", str(e.exception))
        with self.assertRaises(RO.RolloutError):
            self.desk.rollouts.start("demo", "x", ["gpu9"], "peter")
        again = self.new_desk()                             # the manager restarted
        for m in ORDER:
            again.hello({"name": m, "comfyui": "x", "max_inflight": 1, "jobs": []})
        r = again.rollouts.open("demo")
        self.assertEqual(r["machines"], ["tor-flame02-a6000", "wrk1125l-3090"])   # the canary first, as ordered
        again.agents["tor-flame02-a6000"].envs = {"demo": {"current": "new", "builds": {"new": {"ok": True}}}}
        again.rollouts.tick()
        self.assertEqual(again.rollouts.open("demo")["step"], 1)


class ThreeMachines(Manager):
    """Three agents with their environment managers, the bank, and the stand-in uv."""

    def setUp(self):
        super().setUp()
        self.envs = self.tmp / "repo" / "environments"
        for patch in (mock.patch.object(S, "ENVS", self.envs), mock.patch.dict(os.environ, {"FAKE_BUILDER_LOG": ""})):
            patch.start()
            self.addCleanup(patch.stop)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.agents = {}
        for m in ORDER:
            cfg = AgentConfig(name=m, base_dir=self.tmp / m, manager=str(self.bank / "manager.json"),
                              heartbeat_wait=0, envs={"dir": "envs", "uv": UV, "managed": True})
            a = Agent(cfg, LocalLink(self.desk), runners=[Runner()])
            self.loop.run_until_complete(a._hello())
            self.agents[m] = a

    def release(self, check=("python", "-c", "pass"), deps=("numpy==2.5.2",)):
        make(self.envs, "demo", deps=deps, check=list(check))
        B.lock(S.load("demo"), UV, self.tmp / "lock-scratch")
        return R.publish(S.load("demo"), self.bank, commit="abc")[0].fingerprint

    def current(self, m):
        return B.builds(self.tmp / m / "envs", "demo").current

    def run_until(self, done, rounds=20):
        async def go():
            for _ in range(rounds):
                for a in self.agents.values():
                    await a._obey(await a._heartbeat())
                    await a.envman.apply()
                    await a.refresh_environments()
                    await a.send_status()
                self.desk.rollouts.tick()
                if done():
                    return
            self.fail(f"not done: {self.desk.rollouts.recent(1)}")
        self.loop.run_until_complete(go())

    def test_a_good_release_reaches_every_machine_in_order(self):
        old = self.release()
        for a in self.agents.values():
            B.install(S.load("demo"), Path(a.cfg.resolve(a.cfg.envs.dir)), UV, log=lambda _m: None)
        self.run_until(lambda: all(self.desk.agents[m].envs for m in ORDER))
        new = self.release(deps=("numpy==2.5.2", "pillow==12.3.0"))
        self.desk.rollouts.start("demo", new, None, "peter")
        self.run_until(lambda: self.desk.rollouts.recent(1)[0]["state"] == "done")
        self.assertEqual({m: self.current(m) for m in ORDER}, {m: new for m in ORDER})
        # The canary had it first, the manager's host last.
        built = {m: B.builds(self.tmp / m / "envs", "demo").info[new]["built_at"] for m in ORDER}
        self.assertLessEqual(built["tor-flame02-a6000"], built["wrk1125l-3090"])
        self.desk.rollouts.act("demo", "rollback", "peter")
        self.run_until(lambda: all(self.current(m) == old for m in ORDER))

    def test_a_broken_release_stops_at_the_canary(self):
        good = self.release()
        for a in self.agents.values():
            B.install(S.load("demo"), Path(a.cfg.resolve(a.cfg.envs.dir)), UV, log=lambda _m: None)
        self.run_until(lambda: all(self.desk.agents[m].envs for m in ORDER))
        broken = self.release(check=("python", "-c", "raise SystemExit('broken on purpose')"))
        self.desk.rollouts.start("demo", broken, None, "peter")
        self.run_until(lambda: self.desk.rollouts.recent(1)[0]["state"] == "failed")
        r = self.desk.rollouts.recent(1)[0]
        self.assertEqual(r["machines"][r["step"]], "tor-flame02-a6000")
        self.assertIn("broken on purpose", r["error"])
        self.assertEqual({m: self.current(m) for m in ORDER}, {m: good for m in ORDER})   # the canary kept its own
        self.assertEqual(self.desk.agents["wrk59467w-5090"].env_wanted, {})               # the rest never heard of it
        self.desk.rollouts.act("demo", "rollback", "peter")
        self.run_until(lambda: not self.desk.agents["tor-flame02-a6000"].env_work)
        self.assertEqual(self.current("tor-flame02-a6000"), good)
        self.assertEqual(self.desk.agents["tor-flame02-a6000"].env_wanted, {"demo": good})


class TheRepos(unittest.TestCase):
    def test_canary_test_is_broken_on_purpose_and_locked(self):
        spec = S.load("canary-test")
        self.assertEqual((spec.locked, spec.stale, spec.dependencies), (True, None, []))
        self.assertIn("broken on purpose", " ".join(spec.check))


if __name__ == "__main__":
    unittest.main()
