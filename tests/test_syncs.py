"""Syncs the manager runs (manager/syncs.py, decision 0033): a runner's environment brought to
the farm's file on the machines one at a time, the canary first; each drained, synced by
its agent, checked, then resumed. First the sync alone, with what machines report set by
hand; then three real agents, through the real desk, with stand-in runners.

    python -m unittest tests.test_syncs -v
"""

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_envman import Runner  # noqa: E402

from comfyfarm.agent.agent import Agent  # noqa: E402
from comfyfarm.agent.link import LocalLink  # noqa: E402
from comfyfarm.config import AgentConfig, Config  # noqa: E402
from comfyfarm.definitions import DefinitionRegistry  # noqa: E402
from comfyfarm.manager import syncs as SY  # noqa: E402
from comfyfarm.manager.db import JobStore  # noqa: E402
from comfyfarm.manager.desk import Desk  # noqa: E402
from comfyfarm.manager.events import EventBus  # noqa: E402
from comfyfarm.manager.farm import Farm  # noqa: E402
from comfyfarm.protocol import now  # noqa: E402

ORDER = ["tor-flame02-a6000", "wrk59467w-5090", "wrk1125l-3090"]
CANARY = ORDER[0]
FILE = "3f2a9c41d0b7e655"


class Manager(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "workflows").mkdir()
        self.cfg = Config.model_validate({"database": str(self.tmp / "farm.db"), "workers": [],
                                          "workflows_dir": str(self.tmp / "workflows"),
                                          "rollouts": {"order": ORDER, "sync_minutes": 60}})
        self.store = JobStore(self.tmp / "farm.db")
        self.addCleanup(self.store.close)
        self.desk = self.new_desk()

    def new_desk(self):
        return Desk(Farm(self.cfg, self.store, DefinitionRegistry(self.tmp / "workflows"), EventBus()))

    def up(self, m, desk=None):
        """Checking in, its ComfyUI up and checked: it takes jobs unless drained."""
        desk = desk or self.desk
        desk.status({"name": m, "online": True, "connected": True})
        desk.nodes({"name": m, "processes": {}})


class Alone(Manager):
    """The sync with what the machines' agents say set by hand."""

    def setUp(self):
        super().setUp()
        for m in ("wrk1125l-3090", "wrk59467w-5090", "tor-flame02-a6000"):
            self.desk.hello({"name": m, "comfyui": "x", "max_inflight": 1, "jobs": []})
            self.up(m)

    def says(self, m, state, **more):
        self.desk.status({"name": m, "online": True, "connected": True,
                          "sync": {"id": self.desk.syncs.running()["id"], "state": state, **more}})

    def told(self, m):
        """What the machine's next check-in reply tells it about a sync."""
        return self.desk._instructions(self.desk.agents[m], {}).get("sync")

    def now(self):
        return self.desk.syncs.recent(1)[0]

    def test_one_machine_at_a_time_the_canary_first(self):
        r = self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.assertEqual((r["machines"], r["state"], r["phase"]), (ORDER, "running", "draining"))
        self.assertEqual(set(self.desk.drained), {CANARY})                  # the canary alone is drained
        self.desk.syncs.tick()                                              # it holds no jobs: told
        self.assertEqual(self.told(CANARY), {"id": r["id"], "runner": "comfyui", "file": FILE, "force": False})
        self.assertIsNone(self.told(CANARY))                                # once
        self.assertIsNone(self.told("wrk59467w-5090"))
        self.says(CANARY, "applying", step="1/2: pip install", steps=[["stop ComfyUI", "stopped (pid 7)"]])
        self.desk.syncs.tick()
        self.assertEqual((self.now()["phase"], self.now()["results"][CANARY]["step"]), ("syncing", "1/2: pip install"))
        self.desk.status({"name": CANARY, "online": True, "connected": False})   # ComfyUI restarting: unchecked
        self.says(CANARY, "done", note="synced")
        self.desk.syncs.tick()
        self.assertEqual(self.now()["phase"], "checking")                   # until its ComfyUI is checked again
        self.assertEqual(set(self.desk.drained), {CANARY})
        self.up(CANARY)
        self.desk.syncs.tick()
        self.assertEqual(set(self.desk.drained), {"wrk59467w-5090"})        # resumed; the next one's turn
        for m in ORDER[1:]:
            self.desk.syncs.tick()
            self.assertIsNotNone(self.told(m))
            self.says(m, "done", note="synced")
            self.desk.syncs.tick()
        r = self.now()
        self.assertEqual((r["state"], r["error"], self.desk.drained), ("done", None, {}))
        self.assertEqual(set(r["results"]), set(ORDER))

    def test_its_jobs_end_first(self):
        self.desk.syncs.start("comfyui", FILE, [CANARY], False, "peter")
        with mock.patch.object(self.store, "held_by", return_value=[{"id": "j1"}]):
            r = self.desk.syncs.running()
            r["step_started"] = now() - 3 * 3600                            # however long they take
            self.store.save_sync(r)
            self.desk.syncs.tick()
        self.assertEqual((self.now()["phase"], self.now()["state"]), ("draining", "running"))
        self.assertIsNone(self.told(CANARY))
        self.desk.syncs.tick()
        self.assertIsNotNone(self.told(CANARY))

    def test_a_failed_sync_stops_there_and_the_machine_stays_drained(self):
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.desk.syncs.tick()
        self.says(CANARY, "failed", error="pip install … failed:\nNo matching distribution for foo==9",
                  steps=[["stop ComfyUI", "stopped (pid 7)"], ["pip install", "failed: …"]])
        self.desk.syncs.tick()
        r = self.now()
        self.assertEqual((r["state"], r["step"]), ("failed", 0))
        self.assertIn(f"{CANARY}: pip install … failed:\nNo matching distribution", r["error"])
        self.assertEqual(set(self.desk.drained), {CANARY})                  # for a person to look at
        self.assertIsNone(self.told("wrk59467w-5090"))                      # the others never touched
        self.assertIsNone(self.desk.syncs.running())

    def test_a_refusal_before_touching_comfyui_resumes_the_machine(self):
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.desk.syncs.tick()
        self.says(CANARY, "failed", steps=[], error="this machine's checkout has another version of the farm's "
                                                    "ComfyUI environment: update its checkout (git pull)")
        self.desk.syncs.tick()
        r = self.now()
        self.assertEqual(r["state"], "failed")
        self.assertIn(f"(its ComfyUI wasn't touched: {CANARY} takes jobs again)", r["error"])
        self.assertEqual(self.desk.drained, {})

    def test_a_workflow_it_could_run_before_and_cant_after_stops_it(self):
        self.desk.agents[CANARY].problems = {"wan": ["model x isn't in ComfyUI"]}      # couldn't before: fine
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.desk.syncs.tick()
        self.says(CANARY, "done")
        a = self.desk.agents[CANARY]
        a.problems = {"wan": ["model x isn't in ComfyUI"], "ltx25": ["node LTXVAddGuide isn't in ComfyUI"]}
        self.desk.syncs.tick()
        r = self.now()
        self.assertEqual(r["state"], "failed")
        self.assertEqual(r["error"], f"{CANARY}: after the sync it can't run ltx25: node LTXVAddGuide isn't in "
                                     f"ComfyUI")
        self.assertEqual(set(self.desk.drained), {CANARY})

    def test_a_machine_drained_before_stays_drained(self):
        self.desk.set_drained(CANARY, True, "peter")
        self.desk.syncs.start("comfyui", FILE, [CANARY, "wrk59467w-5090"], False, "peter")
        self.desk.syncs.tick()
        self.says(CANARY, "done")
        self.desk.syncs.tick()
        self.assertEqual(self.desk.drained[CANARY]["by"], "peter")
        self.assertIn("wrk59467w-5090", self.desk.drained)

    def test_told_and_silent_is_told_again_then_too_slow_stops_it(self):
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.desk.syncs.tick()
        self.told(CANARY)
        r = self.desk.syncs.running()
        r["asked_at"] = now() - 31                                          # a lost reply, or its agent restarted
        self.store.save_sync(r)
        self.desk.syncs.tick()
        self.assertIsNotNone(self.told(CANARY))
        r = self.desk.syncs.running()
        r["told_at"] = now() - 61 * 60
        self.store.save_sync(r)
        self.desk.syncs.tick()
        self.assertEqual(self.now()["error"],
                         f"{CANARY} didn't finish within 60 minutes (its agent says nothing of it)")

    def test_an_agent_that_restarted_mid_sync_is_told_again(self):
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.desk.syncs.tick()
        self.told(CANARY)
        self.says(CANARY, "applying", steps=[["stop ComfyUI", "stopped (pid 7)"]])
        self.desk.syncs.tick()
        self.desk.hello({"name": CANARY, "comfyui": "x", "max_inflight": 1, "jobs": []})   # it forgot
        r = self.desk.syncs.running()
        r["asked_at"] = now() - 31
        self.store.save_sync(r)
        self.desk.syncs.tick()
        self.assertEqual(self.told(CANARY)["id"], r["id"])

    def test_cancel(self):
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        with mock.patch.object(self.store, "held_by", return_value=[{"id": "j1"}]):
            self.desk.syncs.tick()
        r = self.desk.syncs.cancel("peter")                                 # not told yet: resumed
        self.assertEqual((r["state"], self.desk.drained), ("cancelled", {}))
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.desk.syncs.tick()
        r = self.desk.syncs.cancel("peter")                                 # told: it finishes, and stays drained
        self.assertIn("finishes its sync and stays drained", r["error"])
        self.assertEqual(set(self.desk.drained), {CANARY})
        with self.assertRaises(SY.SyncError):
            self.desk.syncs.cancel("peter")

    def test_one_sync_at_a_time_and_what_it_needs(self):
        with self.assertRaisesRegex(SY.SyncError, "say which version of the farm's environment file"):
            self.desk.syncs.start("comfyui", "", None, False, "peter")
        with self.assertRaisesRegex(SY.SyncError, "no machine gpu9"):
            self.desk.syncs.start("comfyui", FILE, ["gpu9"], False, "peter")
        r = self.desk.syncs.start("comfyui", FILE, ["wrk1125l-3090", CANARY], True, "peter")
        self.assertEqual(r["machines"], [CANARY, "wrk1125l-3090"])          # the canary first, as ordered
        with self.assertRaisesRegex(SY.SyncError, "one machine out of service at a time"):
            self.desk.syncs.start("comfyui", FILE, None, False, "peter")

    def test_it_outlives_a_manager_restart(self):
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.desk.syncs.tick()
        again = self.new_desk()                                             # the manager restarted
        for m in ORDER:
            again.hello({"name": m, "comfyui": "x", "max_inflight": 1, "jobs": []})
            self.up(m, again)
        self.assertEqual(set(again.drained), {CANARY})
        again.status({"name": CANARY, "online": True, "connected": True,
                      "sync": {"id": again.syncs.running()["id"], "state": "done"}})
        again.syncs.tick()
        self.assertEqual((again.syncs.running()["step"], set(again.drained)), (1, {"wrk59467w-5090"}))


class Syncing(Runner):
    """A machine's resident runner that syncs as told: in order, and how, kept in `done`."""

    def __init__(self, done: list, fail: str | None = None):
        super().__init__()
        self.done, self.fail = done, fail

    async def sync(self, order, say):
        self.done.append((self.host.name, order["file"]))
        await say({"id": order["id"], "state": "applying", "step": "1/1: pip install", "steps": []})
        if self.fail:
            rec = {"id": order["id"], "state": "failed", "steps": [["pip install", "failed"]], "error": self.fail}
        else:
            rec = {"id": order["id"], "state": "done", "steps": [["pip install", "done"]], "note": "synced"}
        await say(rec)
        return rec


class ThreeMachines(Manager):
    """Three agents with stand-in runners, through the real desk."""

    def setUp(self):
        super().setUp()
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.done: list = []
        self.agents = {}

    def agent(self, m, runner):
        a = Agent(AgentConfig(name=m, base_dir=self.tmp / m, heartbeat_wait=0), LocalLink(self.desk), runners=[runner])
        self.loop.run_until_complete(a._hello())
        self.up(m)
        self.agents[m] = a

    def run_until(self, done, rounds=30):
        async def go():
            for _ in range(rounds):
                for a in self.agents.values():
                    await a._obey(await a._heartbeat())
                    for _ in range(5):
                        await asyncio.sleep(0)          # the sync it spawned
                    await a.send_status()
                self.desk.syncs.tick()
                if done():
                    return
            self.fail(f"not done: {self.desk.syncs.recent(1)}")
        self.loop.run_until_complete(go())

    def test_every_machine_in_turn_the_canary_first(self):
        for m in ORDER:
            self.agent(m, Syncing(self.done))
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.run_until(lambda: self.desk.syncs.recent(1)[0]["state"] == "done")
        self.assertEqual(self.done, [(m, FILE) for m in ORDER])             # once each, in order
        self.assertEqual(self.desk.drained, {})
        self.assertEqual(self.desk.summary(self.desk.agents[CANARY])["sync"]["note"], "synced")

    def test_a_failure_at_the_canary_stops_it_there(self):
        self.agent(CANARY, Syncing(self.done, fail="pip install … failed"))
        for m in ORDER[1:]:
            self.agent(m, Syncing(self.done))
        self.desk.syncs.start("comfyui", FILE, None, False, "peter")
        self.run_until(lambda: self.desk.syncs.recent(1)[0]["state"] == "failed")
        self.assertEqual(self.done, [(CANARY, FILE)])
        self.assertEqual(set(self.desk.drained), {CANARY})

    def test_a_runner_that_cant_be_synced_says_so(self):
        self.agent(CANARY, Runner())
        self.desk.syncs.start("comfyui", FILE, [CANARY], False, "peter")
        self.run_until(lambda: self.desk.syncs.recent(1)[0]["state"] == "failed")
        self.assertIn("ComfyUI on tor-flame02-a6000 can't be synced by the manager",
                      self.desk.syncs.recent(1)[0]["error"])
        self.assertEqual(self.desk.drained, {})                             # nothing was touched


if __name__ == "__main__":
    unittest.main()
