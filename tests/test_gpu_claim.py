"""The GPU claim (decision 0026): the runs of one runner at a time hold the machine's card,
first come first served between runners, and the runner that used the card before frees
what it keeps there (ComfyUI's models) before another runner's run starts.

The runners here are stand-ins that count what the agent asks of them.

    python -m unittest tests.test_gpu_claim -v
"""

import asyncio
import unittest

from comfyfarm.agent.agent import Agent, Held
from comfyfarm.config import AgentConfig


class Link:
    """The manager, as far as these runs go: it takes every report."""

    def __init__(self):
        self.reports: list[dict] = []

    async def report(self, msg):
        self.reports.append(msg)
        return {"ok": True}

    async def status(self, msg):
        return {}


class Runner:
    """A runner that knows some definitions and frees the card when asked (or doesn't)."""
    object_info = farm_nodes = environment = None

    def __init__(self, label, names, max_runs=None, online=True, busy=0):
        self.label, self.names, self.max_runs, self.online = label, set(names), max_runs, online
        self.ready = True
        self.busy = busy                # this many release() calls are refused first (prompts not the farm's)
        self.releases = 0

    def attach(self, host):
        self.host = host

    def start(self):
        return []

    async def close(self):
        pass

    def status(self):
        return {"online": self.online}

    def processes(self):
        return {}

    def knows(self, process):
        return process in self.names

    async def release(self):
        self.releases += 1
        if self.busy:
            self.busy -= 1
            return {"ok": False, "before": 12 * 2**30, "after": 12 * 2**30,
                    "error": "ComfyUI has 1 prompt(s) running or queued: free its VRAM once they're done"}
        return {"ok": True, "before": 12 * 2**30, "after": 0, "error": None}


class Claim(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.addCleanup(self.cancel_all)
        self.comfy = Runner("ComfyUI", {"ltx", "wan"})
        self.proc = Runner("process", {"copy"}, max_runs=1)
        self.link = Link()
        self.n = 0

    def agent(self, max_inflight=2, **kw):
        a = Agent(AgentConfig(name="gpu1", max_inflight=max_inflight, claim_wait_seconds=kw.pop("wait", 30)),
                  self.link, runners=[self.comfy, self.proc])
        a.CLAIM_RETRY_SECONDS = 0.05
        self.wait(self._sync(a))
        return a

    @staticmethod
    async def _sync(a):
        a._ensure_sync()            # its locks belong to this loop

    def wait(self, coro):
        return self.loop.run_until_complete(coro)

    def cancel_all(self):
        tasks = asyncio.all_tasks(self.loop)
        for t in tasks:
            t.cancel()
        if tasks:
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))

    def settle(self):
        self.wait(asyncio.sleep(0.05))

    def ask(self, a, workflow):
        """A run of `workflow` asks for the card: its task ends once it has it."""
        self.n += 1
        h = Held(job={"job": f"j{self.n}", "workflow": workflow, "info": {}}, attempt=f"a{self.n}",
                 runner=a.runner_for(workflow))
        a.held[h.id] = h
        t = self.loop.create_task(a._take_slot(h))
        self.settle()
        return h, t

    def done(self, a, h):
        async def go():
            a._release(h)
            await asyncio.sleep(0.05)
        self.wait(go())

    def test_comfyui_alone_never_frees_the_card(self):
        a = self.agent()
        (x, tx), (y, ty) = self.ask(a, "ltx"), self.ask(a, "wan")
        self.assertTrue(tx.done() and ty.done())         # max_inflight 2: both in ComfyUI at once
        _, tz = self.ask(a, "ltx")
        self.assertFalse(tz.done())
        self.done(a, x)
        self.assertTrue(tz.done())
        self.assertEqual((self.comfy.releases, a.card_owner), (0, self.comfy))

    def test_a_process_waits_for_comfyuis_runs_and_comfyui_frees_the_card_first(self):
        a = self.agent()
        x, _ = self.ask(a, "ltx")
        p, tp = self.ask(a, "copy")
        self.assertFalse(tp.done())                      # the card is ComfyUI's while its run lasts
        self.done(a, x)
        self.assertTrue(tp.done())
        self.assertEqual((self.comfy.releases, a.card_owner), (1, self.proc))
        self.assertEqual((a.released["ok"], a.released["after"]), (True, 0))
        y, ty = self.ask(a, "wan")
        self.assertFalse(ty.done())                      # and the process's while it runs
        self.done(a, p)
        self.assertTrue(ty.done())
        self.assertEqual((self.proc.releases, self.comfy.releases, a.card_owner), (1, 1, self.comfy))

    def test_first_come_first_served_between_runners(self):
        a = self.agent(max_inflight=2)
        x, _ = self.ask(a, "ltx")
        p, tp = self.ask(a, "copy")
        y, ty = self.ask(a, "wan")
        self.assertFalse(ty.done())                      # ComfyUI has room, but the process came first
        self.done(a, x)
        self.assertTrue(tp.done())
        self.assertFalse(ty.done())
        self.done(a, p)
        self.assertTrue(ty.done())

    def test_the_process_runner_runs_one_at_a_time(self):
        a = self.agent(max_inflight=4)
        p1, _ = self.ask(a, "copy")
        _, t2 = self.ask(a, "copy")
        self.assertFalse(t2.done())
        self.done(a, p1)
        self.assertTrue(t2.done())
        self.assertEqual(self.comfy.releases, 1)        # once, for the first: the card was the process's after

    def test_it_waits_for_comfyui_to_free_the_card_and_says_why(self):
        self.comfy.busy = 2
        a = self.agent(wait=30)
        p, tp = self.ask(a, "copy")
        self.wait(asyncio.wait_for(tp, 60))
        self.assertEqual(self.comfy.releases, 3)
        stages = [r["data"].get("stage") for r in self.link.reports if r["event"] == "progress"]
        self.assertIn("waiting for ComfyUI to free the GPU: ComfyUI has 1 prompt(s) running or queued: free its "
                      "VRAM once they're done", stages)
        self.assertEqual(p.notes, [])

    def test_after_claim_wait_it_starts_anyway_and_the_job_says_so(self):
        self.comfy.busy = 1000
        a = self.agent(wait=0.3)
        p, tp = self.ask(a, "copy")
        self.wait(asyncio.wait_for(tp, 10))
        self.assertEqual(a.card_owner, self.proc)
        self.assertEqual(len(p.notes), 1)
        self.assertIn("ComfyUI didn't free the GPU before this job started", p.notes[0])
        self.wait(a.run_failed(p, "CUDA out of memory"))
        failed = [r for r in self.link.reports if r["event"] == "failed"][-1]
        self.assertTrue(failed["data"]["error"].startswith("CUDA out of memory (ComfyUI didn't free the GPU"))

    def test_a_comfyui_that_isnt_running_holds_nothing(self):
        self.comfy.online = False
        a = self.agent()
        _, tp = self.ask(a, "copy")
        self.assertTrue(tp.done())
        self.assertEqual(self.comfy.releases, 0)

    def test_a_run_dropped_while_waiting_leaves_the_card_to_comfyui(self):
        self.comfy.busy = 1000
        a = self.agent(wait=30)
        p, tp = self.ask(a, "copy")
        p.dropped = True
        self.wait(asyncio.wait_for(tp, 30))
        self.assertIs(a.card_owner, self.comfy)          # it never freed it: the next process run asks again

    def test_a_cancelled_wait_lets_the_others_on(self):
        a = self.agent(max_inflight=2)
        x, _ = self.ask(a, "ltx")
        p, tp = self.ask(a, "copy")
        _, ty = self.ask(a, "wan")
        self.assertFalse(ty.done())                      # behind the process
        tp.cancel()                                      # the process job was cancelled while it waited
        self.settle()
        self.assertTrue(ty.done())
        self.assertEqual(a._waiting, [])

    def test_free_vram_counts_only_comfyuis_runs(self):
        a = self.agent()
        self.ask(a, "copy")                              # a process run on the card
        self.assertTrue(self.wait(a.release_vram())["ok"])
        x, _ = self.ask(self.agent(), "ltx")
        b = x.runner.host
        said = self.wait(b.release_vram())
        self.assertEqual((said["ok"], said["error"]), (False, "1 job(s) in ComfyUI: drain the machine and wait for "
                                                              "them, then free its VRAM"))


if __name__ == "__main__":
    unittest.main()
