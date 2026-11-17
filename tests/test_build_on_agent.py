"""farm.build_on_agent: the manager stores a request, and the machine that runs the job
builds its ComfyUI graph from it (docs/decisions/0013).

The whole agent suite runs again with the switch on: whatever it checks about the graph
that reaches ComfyUI (paths on a Windows agent, loaders, outputs, texture maps) must hold
when the agent built that graph itself.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_agent as TA      # the module, not its classes: the loader must not collect them twice

from comfyfarm.fileio import wire as W
from comfyfarm.workflows import ParamError, WorkflowRegistry

REPO = Path(__file__).resolve().parent.parent


@unittest.skipIf(TA.Agent is None, "needs the agent's dependencies (httpx, websockets)")
class BuiltOnTheAgent(TA.AgentTests):
    def setUp(self):
        super().setUp()
        self.cfg.farm.build_on_agent = True

    def agent(self, *args, **kwargs):
        a = super().agent(*args, **kwargs)
        a.runner._registry = TA.REGISTRY        # the suite's registry: some tests add workflows of their own to it
        return a

    def test_the_manager_stores_a_request_and_no_graph(self):
        a = self.gpu("gpu1", 32)
        job = self.submit()
        stored = self.store.get(job["id"], with_prompt=True)
        self.assertIsNone(stored["prompt"])
        wf = TA.REGISTRY.workflows["ltx25_alpha_gen"]
        self.assertEqual(stored["request"]["definition"], wf.digest)
        self.assertEqual(self.farm.prompt_of(stored), wf.build_from(stored["request"]))   # the dashboard's API JSON
        a.runner._registry = None               # as a real agent: its own checkout's workflows/, read now
        self.beat(a)
        self.until(lambda: a.runner.client.prompts, what="the graph in ComfyUI")
        self.assertEqual(sorted(a.runner._registry.workflows), sorted(WorkflowRegistry(REPO / "workflows").workflows))
        (sent,) = a.runner.client.prompts.values()
        self.assertEqual(sent, W.localize(W.portable(wf.build_from(stored["request"]), self.desk.view), a.view))

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
        self.assertEqual(a.wants, "processes")
        self.assertEqual([sorted(m) for m in sent], [["name", "processes"]])
        told = sent[0]["processes"]
        self.assertEqual(sorted(told), sorted(TA.REGISTRY.workflows))
        wf = TA.REGISTRY.workflows["wan22_i2i"]
        self.assertEqual(told["wan22_i2i"], {"digest": wf.digest, "problems": [],
                                             "expected_nodes": wf.expected_nodes(a.runner.output_classes)})
        state = self.desk.agents["gpu1"]
        self.assertEqual((state.problems, state.checked, state.output_classes), ({}, True, None))
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

    def test_old_jobs_with_a_stored_graph_still_run(self):
        self.cfg.farm.build_on_agent = False
        job = self.submit()                                         # queued before the switch
        self.cfg.farm.build_on_agent = True
        a = self.gpu("gpu1", 32)
        self.beat(a)
        self.until(lambda: a.runner.client.prompts, what="the graph in ComfyUI")
        stored = self.store.get(job["id"], with_prompt=True)
        self.assertIsNone(stored["request"])
        self.assertEqual(list(a.runner.client.prompts.values()),
                         [W.localize(W.portable(stored["prompt"], self.desk.view), a.view)])


class RequestTests(unittest.TestCase):
    """Without a farm: a request is plain data, and building from it on another machine
    gives the graph the manager would have sent there."""

    @classmethod
    def setUpClass(cls):
        cls.registry = WorkflowRegistry(REPO / "workflows")

    def test_the_fingerprint_is_of_the_content_not_the_file(self):
        again = WorkflowRegistry(REPO / "workflows")
        self.assertEqual({n: w.digest for n, w in again.workflows.items()},
                         {n: w.digest for n, w in self.registry.workflows.items()})
        self.assertEqual(len({w.digest for w in self.registry.workflows.values()}), len(self.registry.workflows))
        self.assertRegex(self.registry.workflows["wan22_i2i"].digest, r"^[0-9a-f]{16}$")

    def test_a_request_from_another_definition_is_refused(self):
        wf = self.registry.workflows["wan22_i2i"]
        request = wf.request({"prompt": "x"})
        self.assertEqual(wf.build_from(request), wf.build({"prompt": "x"}))
        with self.assertRaisesRegex(ParamError, "Update the farm's code"):
            wf.build_from({**request, "definition": "feedfacefeedface"})

    def test_built_on_a_windows_machine_it_is_the_graph_the_manager_would_have_sent(self):
        from comfyfarm.config import PathsConfig
        from comfyfarm.fileio.inputs import Sequence
        maps = PathsConfig(mappings=[{"posix": "/rdo/shows", "windows": "S:/shows"}]).mappings
        manager = W.View(mappings=maps, scratch="/farm/scratch", repo="/farm/repo")
        agent = W.View(mappings=maps, scratch="D:/cf/scratch", repo="D:/cf/repo")
        seq = Sequence(raw="x", farm_path="/rdo/shows/xyz/abc/abc_0010/plate/abc_0010.plate.%04d.png",
                       directory="/rdo/shows/xyz/abc/abc_0010/plate/", filename="abc_0010.plate.%04d.png",
                       first=1001, last=1003, frame_count=3, skip=0, cap=3, show="xyz", shot="abc_0010",
                       source="abc_0010.plate", load_dir="/farm/scratch/job1/in/")
        for name in ("ltx25_alpha_gen", "wan22_i2i", "sam3_ltx_alpha", "ltx25_layout_render"):
            wf = self.registry.workflows[name]
            with self.subTest(workflow=name):
                args = ({}, seq, "/rdo/shows/xyz/_cg/comfyui/renders/abc_0010/v003", "abc_0010.v003", "_cfpreview")
                sent = W.localize(W.portable(wf.build(*args), manager), agent)                # today
                built = W.localize(wf.build_from(W.localize(W.portable(wf.request(*args), manager), agent)), agent)
                self.assertEqual(built, sent)
                self.assertIn("S:", str(built))


if __name__ == "__main__":
    unittest.main()
