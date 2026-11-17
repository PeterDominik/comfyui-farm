"""The seam between the agent and its runner (runners/base.py): each side has everything
the other may call, and a runner that isn't ComfyUI can be put in."""

import asyncio
import inspect
import unittest

try:
    from comfyfarm.agent.agent import Agent
    from comfyfarm.config import AgentConfig
    from comfyfarm.runners import base
    from comfyfarm.runners.comfyui import ComfyUIRunner, Runtime
except ImportError:   # pragma: no cover  (no httpx or websockets here)
    Agent = None


def members(protocol) -> set[str]:
    names = set(getattr(protocol, "__annotations__", {})) | {n for n, v in vars(protocol).items() if callable(v)}
    return {n for n in names if not n.startswith("_")}


class NullRunner:
    """The smallest runner: nothing to run on. Enough for an agent to exist."""
    label, ready = "Nothing", False
    object_info = farm_nodes = environment = None

    def attach(self, host):
        self.host = host

    def start(self):
        return []

    async def close(self):
        self.closed = True

    def status(self):
        return {"online": False}


@unittest.skipIf(Agent is None, "the agent needs httpx and websockets")
class ContractTests(unittest.TestCase):
    def test_the_comfyui_runner_has_all_the_agent_may_call(self):
        for name in sorted(members(base.Runner)):
            self.assertTrue(hasattr(ComfyUIRunner, name) or name in ("label", "ready"), name)
        self.assertEqual(ComfyUIRunner.label, "ComfyUI")
        self.assertIsInstance(inspect.getattr_static(ComfyUIRunner, "ready"), property)

    def test_the_agent_has_all_a_runner_may_call(self):
        instance_attrs = {"name", "registered"}
        for name in sorted(members(base.Host) - instance_attrs):
            want = getattr(base.Host, name)
            have = getattr(Agent, name, None)
            self.assertIsNotNone(have, name)
            self.assertEqual(inspect.iscoroutinefunction(have), inspect.iscoroutinefunction(want), name)
            self.assertEqual(len(inspect.signature(have).parameters), len(inspect.signature(want).parameters), name)

    def test_live_progress_has_what_the_agent_reads(self):
        rt = Runtime(job_id="j", expected=4, titles={})
        self.assertEqual((rt.progress(), rt.nodes(), rt.last_push), (0.0, None, 0.0))
        rt.done.update({"1", "2"})
        rt.current, rt.frac = "3", 0.5
        self.assertEqual((rt.progress(), rt.nodes()), (0.625, 3))
        self.assertLessEqual(members(base.RunState), set(dir(rt)))

    def test_an_agent_takes_any_runner(self):
        async def go():
            runner = NullRunner()
            agent = Agent(AgentConfig(name="gpu-x"), link=None, runner=runner)
            self.assertIs(runner.host, agent)
            self.assertEqual((agent.runs(), agent.by_pid("nope")), ([], None))
            await agent.runner.close()
            return runner
        self.assertTrue(asyncio.run(go()).closed)


if __name__ == "__main__":
    unittest.main()
