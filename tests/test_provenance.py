"""A job's provenance file (agent/provenance.py, decision 0038): its name for each kind of
output, the files each kind says it wrote, and the file written whole or not at all. A job
writing one through the manager and an agent is in test_agent (ComfyUI) and
test_process_runner (a process); each runner's part (about) in test_runner_contract.

    python -m unittest tests.test_provenance -v
"""

import asyncio
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from comfyfarm import artifacts as A
from comfyfarm.agent import gpus as G
from comfyfarm.agent import provenance as PR
from comfyfarm.config import PathsConfig

FRAME_NAME = PathsConfig().frame_name           # {source}.{tag}_v{version:03d}.{frame:04d}.{ext}
TOKENS = {"source": "plate", "tag": "wan", "version": 1, "ext": "png", "show": "xyz", "seq": "abc", "shot": "abc_0010"}
D = "/rdo/shows/xyz/out/v001"


def frames_job(**info) -> dict:
    return {"job": "j1", "collect": {"frames": {"ext": "png"}},
            "info": {"output_dir": D, "name_tokens": TOKENS, "frame_name": FRAME_NAME, **info}}


TEXTURES = {"name": "{asset}_{channel}_{colorspace}.{udim}.exr", "tokens": {"asset": "rock"},
            "maps": [{"channel": "BaseColor", "colorspace": "srgb"}, {"channel": "Normal", "colorspace": "raw"}],
            "items": [[1001, 1], [1002, 1]]}


class Names(unittest.TestCase):
    """Beside the outputs, named like them, so it sorts with them and says whose it is."""

    def test_beside_frames_named_like_them(self):
        self.assertEqual(PR.name_for(frames_job()), "plate.wan_v001.provenance.json")

    def test_beside_other_kinds_named_like_their_files(self):
        job = {"info": {"output_dir": D, "writes": {"model": {"dir": D, "base": "plate.lora_v001"}}}}
        self.assertEqual(PR.name_for(job), "plate.lora_v001.provenance.json")

    def test_frames_name_it_when_a_job_writes_other_kinds_too(self):
        job = frames_job(writes={"file": {"dir": D, "base": "plate.wan_v001"}})
        self.assertEqual([h.kind for h in A.for_job(job)], ["frames", "file"])
        self.assertEqual(PR.name_for(job), "plate.wan_v001.provenance.json")

    def test_a_texture_folder_and_each_batch_of_a_set(self):
        """A texture set's folder is its own: one file for a run, one per batch for a set."""
        job = {"info": {"output_dir": D, "textures": TEXTURES}}
        self.assertEqual(PR.name_for(job), "provenance.json")
        job["info"]["batch"] = {"set": "s1", "index": 2, "of": 3}
        self.assertEqual(PR.name_for(job), "provenance.002.json")

    def test_none_for_a_job_that_writes_nothing_the_farm_collects(self):
        self.assertIsNone(PR.name_for({"info": {"output_dir": D}}))
        self.assertIsNone(PR.name_for(frames_job(name_tokens=None)))      # queued before names were frozen


class Written(unittest.TestCase):
    """Each kind lists every file it wrote: what the provenance lists, and all a sweeper may delete."""

    def test_frames_and_their_preview_movie(self):
        outputs = {"frame_pattern": f"{D}/plate.wan_v001.%04d.png", "first": 1001, "last": 1003,
                   "preview": f"{D}/plate.wan_v001_preview.mp4"}
        self.assertEqual(A.by_kind("frames").written(frames_job()["info"], outputs),
                         [f"{D}/plate.wan_v001.{n}.png" for n in (1001, 1002, 1003)] + [outputs["preview"]])
        wide = dict(outputs, frame_pattern=f"{D}/x.%06d.png", first=1, last=1, preview=None)
        self.assertEqual(A.by_kind("frames").written({}, wide), [f"{D}/x.000001.png"])
        self.assertEqual(A.by_kind("frames").written({}, {}), [])

    def test_each_map_of_each_tile(self):
        info = {"output_dir": D, "textures": TEXTURES}
        self.assertEqual(A.by_kind("textures").written(info, {"maps": {"BaseColor": {}, "Normal": {}}}),
                         [f"{D}/rock_BaseColor_srgb.1001.exr", f"{D}/rock_BaseColor_srgb.1002.exr",
                          f"{D}/rock_Normal_raw.1001.exr", f"{D}/rock_Normal_raw.1002.exr"])
        self.assertEqual(A.by_kind("textures").written(info, {}), [])

    def test_the_generic_route_lists_its_files(self):
        files = [f"{D}/plate.lora_v001.safetensors"]
        self.assertEqual(A.by_kind("model").written({}, {"model": {"files": files, "bytes": [9]}}), files)
        self.assertEqual(A.by_kind("file").written({}, {}), [])


class TheFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_files_are_named_in_the_output_folder_once_each(self):
        wrote = [f"{D}/a.1001.png", f"{D}/a.1001.png", f"{D}/sub/b.json", "/elsewhere/c.mov"]
        self.assertEqual(PR.files(D, wrote), ["a.1001.png", "sub/b.json", "/elsewhere/c.mov"])
        self.assertEqual(PR.files(D + "/", wrote[:1]), ["a.1001.png"])
        self.assertEqual(PR.files(None, wrote[:1]), [f"{D}/a.1001.png"])

    def test_what_the_document_says(self):
        job = frames_job(input={"first": 1001, "last": 1003}, input_posix="/rdo/shows/xyz/in.%04d.exr",
                         sources={"input": {"id": 7}}, show="xyz", shot="abc_0010", version=1, provenance=True)
        job.update(attempt="a1", name="abc_0010", user="ann", workflow="wan22_i2i", dispatched_at=1_700_000_000,
                   request={"definition": "0123456789abcdef", "values": {"seed": 4}})
        doc = PR.document(job, machine="gpu1", gpu={"name": "RTX", "driver": "570"}, farm="0.1.0+abc",
                          runner={"backend": "ComfyUI"}, started=1_700_000_010, finished=1_700_000_070.04,
                          outputs={"artifacts": [{"kind": "frames"}]}, wrote=[f"{D}/plate.wan_v001.1001.png"],
                          warnings=["w"])
        self.assertEqual(doc["comfyfarm_provenance"], PR.VERSION)
        self.assertEqual((doc["job"], doc["attempt"], doc["user"]), ("j1", "a1", "ann"))
        self.assertEqual(doc["definition"], {"name": "wan22_i2i", "fingerprint": "0123456789abcdef"})
        self.assertEqual((doc["shot"]["show"], doc["shot"]["shot"], doc["shot"]["version"]), ("xyz", "abc_0010", 1))
        self.assertEqual((doc["ran"]["machine"], doc["ran"]["seconds"]), ("gpu1", 60.0))
        self.assertRegex(doc["ran"]["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertEqual(doc["inputs"], {"input": "/rdo/shows/xyz/in.%04d.exr", "range": [1001, 1003], "others": {},
                                         "shotgrid": {"input": {"id": 7}}})
        self.assertEqual((doc["params"], doc["folder"], doc["files"]), ({"seed": 4}, D, ["plate.wan_v001.1001.png"]))
        self.assertEqual((doc["artifacts"], doc["warnings"]), ([{"kind": "frames"}], ["w"]))
        json.dumps(doc)                                                     # all of it is JSON

    def test_written_whole_and_readable_by_everyone(self):
        path = os.path.join(self.dir, "x.provenance.json")
        os.chmod(self.dir, 0o755)
        PR.write(path, {"comfyfarm_provenance": 1, "name": "plaque façade"})
        self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")),
                         {"comfyfarm_provenance": 1, "name": "plaque façade"})
        self.assertEqual(os.listdir(self.dir), ["x.provenance.json"])        # no temporary file left
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode) & 0o444, 0o444)
        PR.write(path, {"comfyfarm_provenance": 1})                          # a second attempt's replaces it
        self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8")), {"comfyfarm_provenance": 1})

    def test_one_that_fails_leaves_nothing_behind(self):
        path = os.path.join(self.dir, "x.provenance.json")
        with self.assertRaises(TypeError):
            PR.write(path, {"not json": object()})
        self.assertEqual(os.listdir(self.dir), [])
        with mock.patch("os.replace", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(PermissionError):
                PR.write(path, {})
        self.assertEqual(os.listdir(self.dir), [])


class Stamp(unittest.TestCase):
    """Each texture map's EXR carries its job and its provenance file's name (decision 0039)."""

    def test_for_a_job_that_writes_a_provenance_file(self):
        textures = A.by_kind("textures")
        job = {"job": "0123abcd", "info": {"output_dir": D, "textures": TEXTURES, "provenance": True}}
        self.assertEqual(textures.stamp(job), {"comfyfarm:job": "0123abcd", "comfyfarm:provenance": "provenance.json"})
        job["info"]["batch"] = {"set": "s1", "index": 2, "of": 3}
        self.assertEqual(textures.stamp(job)["comfyfarm:provenance"], "provenance.002.json")
        job["info"]["provenance"] = False
        self.assertIsNone(textures.stamp(job))
        self.assertIsNone(textures.stamp({"job": "0123abcd", "info": {"output_dir": D, "textures": TEXTURES}}))

    def test_the_maps_are_written_with_it(self):
        job = {"job": "0123abcd", "info": {"output_dir": D, "textures": TEXTURES, "provenance": True}}
        cfg = SimpleNamespace(textures="tc", convert="cc", paths=SimpleNamespace(mappings=[]))
        ctx = A.RunContext(job=job, cfg=cfg, report=None, spawn=None)
        with mock.patch("comfyfarm.fileio.outputs.write_maps", mock.AsyncMock(return_value={"maps": {}})) as write:
            self.assertEqual(asyncio.run(A.by_kind("textures").collect(ctx)), ({"maps": {}}, []))
        self.assertEqual(write.call_args.args[:5], (TEXTURES, D, "tc", "cc", []))
        self.assertEqual(write.call_args.args[6], {"comfyfarm:job": "0123abcd",
                                                   "comfyfarm:provenance": "provenance.json"})


class GPU(unittest.TestCase):
    def test_a_runners_device_name_without_its_number_and_allocator(self):
        self.assertEqual(G.device_name("cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync"), "NVIDIA GeForce RTX 5090")
        self.assertEqual(G.device_name("cuda:1 NVIDIA RTX A6000"), "NVIDIA RTX A6000")
        self.assertIsNone(G.device_name(None))
        self.assertIsNone(G.device_name(""))


if __name__ == "__main__":
    unittest.main()
