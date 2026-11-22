"""Typed artifacts (comfyfarm/artifacts, decisions 0034-0036): the seam between the farm and a
kind of output, which handlers a definition and a job get, and the generic route a new kind
takes (file). Frames' and texture sets' own work (folders, names, renaming frames, writing
maps, the preview movie) is tested where it always was: test_agent, test_textures,
test_sets, test_e2e.

    python -m unittest tests.test_artifacts -v
"""

import asyncio
import inspect
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from comfyfarm import artifacts as A
from comfyfarm.artifacts import base
from comfyfarm.config import PathsConfig
from comfyfarm.definitions import DefinitionRegistry, from_process

REPO = Path(__file__).resolve().parent.parent


def members(protocol) -> set[str]:
    return set(getattr(protocol, "__annotations__", {})) | {n for n, v in vars(protocol).items()
                                                             if callable(v) and not n.startswith("_")}


class Seam(unittest.TestCase):
    def test_every_handler_is_a_file_named_by_its_kind(self):
        """A new kind of output is one module here, named by its key under writes:."""
        found = {h.kind: h for h in A.handlers()}
        self.assertEqual(sorted(found), ["file", "frames", "textures"])
        for kind, h in found.items():
            self.assertEqual(type(h).__module__, f"comfyfarm.artifacts.{kind}")

    def test_every_handler_has_what_the_manager_and_the_agent_call(self):
        for h in A.handlers():
            for name in sorted(members(base.Handler) - {"kind"}):
                want, have = getattr(base.Handler, name), getattr(h, name, None)
                with self.subTest(kind=h.kind, member=name):
                    self.assertIsNotNone(have)
                    self.assertEqual(inspect.iscoroutinefunction(have), inspect.iscoroutinefunction(want))
                    self.assertEqual(list(inspect.signature(have).parameters),
                                     [p for p in inspect.signature(want).parameters if p != "self"])

    def test_the_definitions_in_the_repo_get_their_kinds(self):
        registry = DefinitionRegistry(REPO / "workflows")
        kinds = {name: [h.kind for h in A.for_definition(wf)] for name, wf in registry.workflows.items()}
        self.assertEqual(kinds["chord_pbr"], ["textures"])
        for name in ("ltx25_alpha_gen", "ltx25_layout_render", "sam3_ltx_alpha", "wan22_i2i"):
            self.assertEqual(kinds[name], ["frames"], name)
        self.assertEqual(kinds["gpu_test"], ["frames", "file"])     # frames first: they choose the folder
        self.assertEqual(A.for_definition(SimpleNamespace(frames_ext=None, textures=None, writes={})), [])

    def test_a_job_gets_its_kinds_from_the_job_alone(self):
        def kinds(job):
            return [h.kind for h in A.for_job(job)]
        self.assertEqual(kinds({"info": {"textures": {"raw_dir": "/s/j/maps"}}, "collect": {}}), ["textures"])
        self.assertEqual(kinds({"info": {}, "collect": {"frames": {"ext": "png"}}}), ["frames"])
        self.assertEqual(kinds({"info": {}, "collect": {"preview": True}}), ["frames"])        # a video only
        self.assertEqual(kinds({"info": {"writes": {"file": {}}}, "collect": {"frames": {"ext": "png"}}}),
                         ["frames", "file"])
        self.assertEqual(kinds({"info": {}, "collect": {"frames": None, "preview": False, "movie": None}}), [])
        self.assertEqual(kinds({"info": None}), [])

    def test_what_several_kinds_give_goes_together(self):
        out = A.merge({}, {"frames_prefix": "_raw", "writes": {"file": {"dir": "/o"}}, "artifacts": [{"kind": "a"}]})
        A.merge(out, {"writes": {"movie": {"dir": "/o"}}, "artifacts": [{"kind": "b"}]})
        self.assertEqual(out, {"frames_prefix": "_raw", "writes": {"file": {"dir": "/o"}, "movie": {"dir": "/o"}},
                               "artifacts": [{"kind": "a"}, {"kind": "b"}]})


class TheCore(unittest.TestCase):
    def test_the_planner_leaves_a_frame_sequence_to_its_handler(self):
        """Decision 0035: the job's info, what the runner is told and the summary come from the
        handler of its kind. Texture sets (batches across jobs) are the core's still."""
        src = (REPO / "comfyfarm" / "manager" / "farm.py").read_text(encoding="utf-8")
        for name in ("frames_prefix", "preview_prefix", "name_tokens", "frame_name", "preview_name", ".collect(",
                     "_movie", "_plan_textures"):
            self.assertFalse(name in src, f"farm.py names {name}")

    def test_each_kind_says_what_the_job_keeps_and_the_runner_is_told(self):
        frames, textures = A.by_kind("frames"), A.by_kind("textures")
        wf = SimpleNamespace(collect=lambda movie: {"frames": {"ext": "png"}, "preview": False, "movie": movie},
                             makes_preview=False)
        plan = {"wf": wf, "seq": None, "frames_prefix": "_comfyfarm_1234abcd_raw",
                "preview_prefix": "comfyfarm/1234abcd_plate_wan", "name_tokens": {"version": 3}}
        ctx = A.PlanContext(cfg=SimpleNamespace(paths=PathsConfig(), preview=SimpleNamespace(enabled=True)),
                            job_id="1234abcd", scratch_root="/s", reserved=lambda key: False)
        info = frames.info(plan, ctx)
        self.assertEqual((info["frames_prefix"], info["name_tokens"], info["collect"]["frames"]),
                         ("_comfyfarm_1234abcd_raw", {"version": 3}, {"ext": "png"}))
        self.assertEqual(frames.request(plan), {"frames_prefix": "_comfyfarm_1234abcd_raw",
                                                "preview_prefix": "comfyfarm/1234abcd_plate_wan"})
        self.assertEqual(frames.summary(plan), {})
        tex = {"raw_dir": "/s/1234abcd/maps", "maps": [{"channel": "color01"}, {"channel": "nrm02"}]}
        plan = {"textures": tex}
        self.assertEqual((textures.info(plan, ctx), textures.request(plan)), ({"textures": tex}, {"textures": tex}))
        self.assertEqual(textures.summary(plan), {"maps": ["color01", "nrm02"]})


class Errors(unittest.TestCase):
    """What the handlers say when they can't: the manager answers with PlanError's status,
    the agent fails the job with PrepareError's message."""

    def frames(self, frame_name, reserved):
        wf = SimpleNamespace(output_folder_param=None, tag="wan", frames_ext="png", textures=None)
        plan = {"wf": wf, "values": {}, "seq": None, "show": "xyz", "shot": "sh010", "seq_name": "sq01",
                "source": "plate", "notes": []}
        ctx = A.PlanContext(cfg=SimpleNamespace(paths=PathsConfig(frame_name=frame_name, validate_inputs=False)),
                            job_id="preview", scratch_root="/scratch", reserved=reserved)
        return A.by_kind("frames").plan(plan, ctx)

    def test_frames(self):
        plan = self.frames("{source}.{tag}_v{version:03d}.{frame:04d}.{ext}", lambda key: False)
        self.assertEqual((plan["out_dir"], plan["version"], plan["first_file"]),
                         ("//server/share/shows/xyz/comfyui/renders/sh010", 1, "plate.wan_v001.0001.png"))
        with self.assertRaises(A.PlanError) as e:
            self.frames("{source}.{tag}.{frame:04d}.{ext}", lambda key: True)
        self.assertEqual(e.exception.status, 409)
        self.assertIn("already exists. Add {version} to paths.frame_name", str(e.exception))
        with self.assertRaises(A.PlanError) as e:
            self.frames("{source}.{tag}_v{version:03d}.{frame:04d}.{ext}", lambda key: True)
        self.assertEqual((e.exception.status, str(e.exception)),
                         (400, "No free version in //server/share/shows/xyz/comfyui/renders/sh010 (999 tried)."))

    def test_textures(self):
        textures = A.by_kind("textures")
        plan = {"wf": SimpleNamespace(textures=object()), "values": {}, "seq": None}
        with self.assertRaisesRegex(A.PlanError, "This workflow needs an input texture."):
            textures.plan(plan, A.PlanContext(cfg=SimpleNamespace(paths=PathsConfig()), job_id="j",
                                              scratch_root="/s", reserved=lambda key: False))
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "a-file").write_text("")
        raw = f"{tmp}/a-file/maps"                              # a folder can't go inside a file
        ctx = A.RunContext(job={"info": {"textures": {"raw_dir": raw}}}, cfg=None)
        with self.assertRaises(A.PrepareError) as e:
            asyncio.run(textures.prepare(ctx))
        self.assertIn(f"Can't create the scratch folder {raw}: ", str(e.exception))   # a Windows path: no regex
        ctx = A.RunContext(job={"info": {"textures": {"raw_dir": f"{tmp}/maps"}}}, cfg=None)
        (tmp / "maps").mkdir()
        (tmp / "maps" / "old.png").write_text("")             # a first attempt's: emptied
        asyncio.run(textures.prepare(ctx))
        self.assertEqual(list((tmp / "maps").iterdir()), [])


class GenericRoute(unittest.TestCase):
    """A kind the core knows nothing about (decision 0036), as file takes it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = A.by_kind("file")

    def ctx(self, reserved=lambda key: False, frame_name="{source}.{tag}_v{version:03d}.{frame:04d}.{ext}"):
        paths = PathsConfig(frame_name=frame_name, validate_inputs=True,
                            output_root=str(self.tmp).replace("\\", "/") + "/{show}")
        return A.PlanContext(cfg=SimpleNamespace(paths=paths), job_id="1234abcd-ee", scratch_root="/s",
                             reserved=reserved)

    def plan(self, **more):
        wf = SimpleNamespace(output_folder_param=None, tag="gputest", writes={"file": {"ext": "json"}})
        return {"wf": wf, "values": {}, "seq": None, "show": "xyz", "shot": "sh010", "seq_name": "sq01",
                "source": "plate", "notes": [], "out_dir": None, "version": None, **more}

    def test_the_head(self):
        self.assertEqual((self.file.head(None), self.file.head({"ext": "json"})), ({}, {"ext": "json"}))
        for bad, why in (({"ext": ".json"}, "a file extension without its dot"), ({"name": "x"}, "unknown key name"),
                         ("json", "needs to be a section")):
            with self.assertRaisesRegex(ValueError, why):
                self.file.head(bad)

    def test_its_name_follows_the_frames(self):
        tokens = {"source": "plate", "shot": "sh010", "tag": "wan", "version": 3, "ext": "json"}
        for frame_name, want in (("{source}.{tag}_v{version:03d}.{frame:04d}.{ext}", "plate.wan_v003"),
                                 ("{shot}_{tag}_v{version:03d}.{frame:04d}.{ext}", "sh010_wan_v003"),
                                 ("{source}_{frame}.{ext}", "plate"),
                                 ("{source}.{tag}_v{version:03d}.{ext}", "plate.wan_v003")):
            self.assertEqual(base.base_name(frame_name, tokens), want, frame_name)

    def test_beside_the_frames_in_their_folder_and_version(self):
        plan = self.file.plan(self.plan(out_dir="/out/sh010", version=4, first_file="plate.gputest_v004.1001.png",
                                        name_tokens={"source": "plate", "tag": "gputest", "version": 4}), self.ctx())
        self.assertEqual(plan["writes"]["file"], {"dir": "/out/sh010", "prefix": "_comfyfarm_1234abcd_file",
                                                  "ext": "json", "base": "plate.gputest_v004"})
        self.assertEqual(plan["first_file"], "plate.gputest_v004.1001.png")        # the frames' own line
        self.assertEqual(self.file.info(plan, self.ctx()), {"writes": {"file": plan["writes"]["file"]}})
        self.assertEqual(self.file.request(plan), {"writes": {"file": {"dir": "/out/sh010", "ext": "json",
                                                                       "prefix": "_comfyfarm_1234abcd_file"}}})
        self.assertEqual(self.file.summary(plan), {"artifacts": [{"kind": "file", "name": "plate.gputest_v004*.json"}]})

    def test_alone_it_takes_the_first_free_version(self):
        folder = self.tmp / "xyz" / "sh010"
        folder.mkdir(parents=True)
        (folder / "plate.gputest_v001.json").write_text("{}")                  # v1 is there
        plan = self.file.plan(self.plan(), self.ctx(reserved=lambda key: key.endswith("_v002")))   # v2 is queued
        self.assertEqual((plan["version"], plan["out_dir"]), (3, str(folder).replace("\\", "/")))
        self.assertEqual((plan["first_file"], plan["writes"]["file"]["base"]), ("plate.gputest_v003.json",
                                                                                "plate.gputest_v003"))
        with self.assertRaises(A.PlanError) as e:                              # no {version}: never overwritten
            self.file.plan(self.plan(), self.ctx(frame_name="{source}.{tag}.{frame:04d}.{ext}",
                                                 reserved=lambda key: True))
        self.assertEqual(e.exception.status, 409)

    def collect(self, wrote, kind_spec=None):
        spec = kind_spec or {"dir": str(self.tmp), "prefix": "_comfyfarm_1234abcd_file", "ext": "json",
                             "base": "plate.gputest_v003"}
        for n in wrote:
            (self.tmp / n).write_text("{}")

        class Runner:
            label = "a process"

            def files(self, run, result, kind, s):
                return sorted(str(self_tmp / n) for n in os.listdir(self_tmp) if n.startswith(s["prefix"]))

            async def fetch(self, entry, dest):
                os.replace(entry, dest)
        self_tmp = self.tmp
        ctx = A.RunContext(job={"info": {"writes": {"file": spec}}}, cfg=SimpleNamespace(paths=PathsConfig()),
                           runner=Runner())
        return asyncio.run(self.file.collect(ctx)), ctx

    def test_what_the_run_wrote_named_after_the_job(self):
        (outputs, warnings), ctx = self.collect(["_comfyfarm_1234abcd_file.json", "_comfyfarm_1234abcd_file_gpu.json"])
        names = sorted(os.listdir(self.tmp))
        self.assertEqual(names, ["plate.gputest_v003.json", "plate.gputest_v003_gpu.json"])
        self.assertEqual([os.path.basename(f) for f in outputs["file"]["files"]], names)
        self.assertEqual(warnings, [])
        record = self.file.record(outputs)
        self.assertEqual((record["kind"], len(record["files"])), ("file", 2))
        self.assertIsNone(self.file.record({}))

    def test_a_run_that_wrote_none_fails(self):
        with self.assertRaisesRegex(RuntimeError, "the run wrote no file .*though its definition says it writes"):
            self.collect([])

    def test_an_earlier_attempts_files_are_cleared(self):
        (self.tmp / "_comfyfarm_1234abcd_file.json").write_text("{}")
        (self.tmp / "plate.gputest_v002.json").write_text("{}")
        spec = {"dir": str(self.tmp), "prefix": "_comfyfarm_1234abcd_file"}
        ctx = A.RunContext(job={"info": {"writes": {"file": spec}}}, cfg=None)
        asyncio.run(self.file.prepare(ctx))
        self.assertEqual(os.listdir(self.tmp), ["plate.gputest_v002.json"])


class ComfyUI(unittest.TestCase):
    """A ComfyUI graph writing a kind on the generic route: the node its runner block names gets
    the job's prefix, and its files are what ComfyUI lists for that node (decision 0036)."""

    def workflow(self, prefix_input="filename_prefix"):
        from comfyfarm.runners.comfyui_workflow import Workflow
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "workflow_api.json").write_text(json.dumps({"12": {"class_type": "SaveText",
                                                                  "inputs": {"filename_prefix": "x", "text": "hi"}}}))
        (tmp / "process.yaml").write_text(json.dumps({
            "id": "notes", "params": {}, "writes": {"file": {"ext": "txt"}},
            "runner": {"type": "comfyui", "outputs": {"file": {"node": "12", "prefix_input": prefix_input}}}}))
        return Workflow(tmp)

    def test_the_node_gets_the_jobs_prefix(self):
        wf = self.workflow()
        self.assertEqual(wf.writes, {"file": {"ext": "txt"}})
        prompt = wf.build({}, writes={"file": {"dir": "/out", "prefix": "_comfyfarm_1234abcd_file", "ext": "txt"}})
        self.assertEqual(prompt["12"]["inputs"]["filename_prefix"], "_comfyfarm_1234abcd_file")
        self.assertNotIn("outputs.file", " ".join(wf.check_wiring()))
        self.assertIn("outputs.file: node 12 has no input 'name'", self.workflow("name").check_wiring())

    def test_its_files_are_what_comfyui_lists_for_the_node(self):
        try:
            from comfyfarm.runners.comfyui import ComfyUIRunner
        except ImportError:   # pragma: no cover
            self.skipTest("needs the agent's dependencies (httpx, websockets)")
        from comfyfarm.config import AgentConfig
        runner = ComfyUIRunner(AgentConfig(name="gpu1", comfyui="http://gpu1:8188"))
        runner._workflows = lambda: {"notes": self.workflow()}
        hist = {"outputs": {"12": {"text": ["hi"], "files": [{"filename": "_comfyfarm_1234abcd_file_00001_.txt",
                                                              "subfolder": "", "type": "output"}]},
                            "3": {"images": [{"filename": "other.png"}]}}}
        run = SimpleNamespace(job={"workflow": "notes"})
        self.assertEqual([e["filename"] for e in runner.files(run, hist, "file", {})],
                         ["_comfyfarm_1234abcd_file_00001_.txt"])
        self.assertEqual(runner.files(run, hist, "movie", {}), [])           # no node for it


class Head(unittest.TestCase):
    """writes.<kind> in a process.yaml, read by the handler of that kind (decision 0036)."""

    def process(self, writes, runner=None):
        return from_process({"id": "x", "params": {}, "writes": writes,
                             "runner": runner or {"type": "process", "environment": "gpu-test",
                                                  "command": ["python", "{definition}/run.py"]}})[0]

    def test_a_process(self):
        m = self.process({"frames": {"ext": "png"}, "file": {"ext": "json"}})
        self.assertEqual((m["writes"], m["outputs"]), ({"file": {"ext": "json"}}, {"frames": {"ext": "png"}}))
        self.assertNotIn("writes", self.process({"frames": {"ext": "png"}}))       # no new kind: as before
        with self.assertRaisesRegex(ValueError, r"writes: unknown key sound \(it takes frames, preview, file"):
            self.process({"sound": {}})
        with self.assertRaisesRegex(ValueError, "writes.file.ext: a file extension without its dot"):
            self.process({"file": {"ext": ".json"}})

    def test_a_comfyui_workflow_names_the_node(self):
        runner = {"type": "comfyui", "outputs": {"file": {"node": "12", "prefix_input": "filename_prefix"}}}
        m = self.process({"file": {"ext": "txt"}}, runner)
        self.assertEqual((m["writes"], m["outputs"]["file"]), ({"file": {"ext": "txt"}},
                                                               {"node": "12", "prefix_input": "filename_prefix"}))
        with self.assertRaisesRegex(ValueError, "writes.file needs runner.outputs.file"):
            self.process({"file": {}}, {"type": "comfyui"})
        with self.assertRaisesRegex(ValueError, "runner.outputs.file: needs the node that writes it"):
            self.process({"file": {}}, {"type": "comfyui", "outputs": {"file": {"prefix_input": "p"}}})
        tex = {"name": "{channel}.exr", "maps": [{"channel": "color01"}]}
        with self.assertRaisesRegex(ValueError, "writes.file: a texture set writes only its maps"):
            self.process({"textures": tex, "file": {}},
                         {"type": "comfyui", "textures": {"maps": {"color01": {"node": "3"}}},
                          "outputs": {"file": {"node": "12"}}})


if __name__ == "__main__":
    unittest.main()
