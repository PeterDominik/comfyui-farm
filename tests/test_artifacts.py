"""Typed artifacts (comfyfarm/artifacts, decision 0034): the seam between the farm and a kind
of output, and which handler a definition and a job get. The handlers' own work (folders,
names, renaming frames, writing maps, the preview movie) is tested where it always was:
test_agent, test_textures, test_sets, test_e2e.

    python -m unittest tests.test_artifacts -v
"""

import asyncio
import inspect
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from comfyfarm import artifacts as A
from comfyfarm.artifacts import base
from comfyfarm.config import PathsConfig
from comfyfarm.definitions import DefinitionRegistry

REPO = Path(__file__).resolve().parent.parent


def members(protocol) -> set[str]:
    return set(getattr(protocol, "__annotations__", {})) | {n for n, v in vars(protocol).items()
                                                             if callable(v) and not n.startswith("_")}


class Seam(unittest.TestCase):
    def test_every_handler_is_a_file_named_by_its_kind(self):
        """A new kind of output is one module here, named by its key under writes:."""
        found = {h.kind: h for h in A.handlers()}
        self.assertEqual(sorted(found), ["frames", "textures"])
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

    def test_the_definitions_in_the_repo_get_their_kind(self):
        registry = DefinitionRegistry(REPO / "workflows")
        kinds = {name: (A.for_definition(wf) or SimpleNamespace(kind=None)).kind
                 for name, wf in registry.workflows.items()}
        self.assertEqual(kinds["chord_pbr"], "textures")
        for name in ("ltx25_alpha_gen", "ltx25_layout_render", "sam3_ltx_alpha", "wan22_i2i", "gpu_test"):
            self.assertEqual(kinds[name], "frames", name)
        for name, wf in registry.workflows.items():           # one kind each: the handlers don't overlap
            self.assertLessEqual(sum(h.writes(wf) for h in A.handlers()), 1, name)
        self.assertIsNone(A.for_definition(SimpleNamespace(frames_ext=None, textures=None)))   # writes nothing

    def test_a_job_gets_its_kind_from_the_job_alone(self):
        self.assertEqual(A.for_job({"info": {"textures": {"raw_dir": "/s/j/maps"}}, "collect": {}}).kind, "textures")
        self.assertEqual(A.for_job({"info": {}, "collect": {"frames": {"ext": "png"}}}).kind, "frames")
        self.assertEqual(A.for_job({"info": {}, "collect": {"preview": True}}).kind, "frames")   # a video only
        self.assertIsNone(A.for_job({"info": {}, "collect": {"frames": None, "preview": False, "movie": None}}))
        self.assertIsNone(A.for_job({"info": None}))


class Errors(unittest.TestCase):
    """What the handlers say when they can't: the manager answers with PlanError's status,
    the agent fails the job with PrepareError's message."""

    def frames(self, frame_name, reserved):
        wf = SimpleNamespace(output_folder_param=None, tag="wan", frames_ext="png", textures=None)
        plan = {"wf": wf, "values": {}, "seq": None, "show": "xyz", "shot": "sh010", "seq_name": "sq01",
                "source": "plate", "notes": []}
        ctx = A.PlanContext(cfg=SimpleNamespace(paths=PathsConfig(frame_name=frame_name, validate_inputs=False)),
                            job_id="preview", scratch_root="/scratch", reserved=reserved)
        return next(h for h in A.handlers() if h.kind == "frames").plan(plan, ctx)

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
        textures = next(h for h in A.handlers() if h.kind == "textures")
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


if __name__ == "__main__":
    unittest.main()
