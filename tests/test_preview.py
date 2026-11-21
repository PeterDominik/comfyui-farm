"""The preview movie the agent makes from a job's frames (comfyfarm.fileio.preview): its
size, frame rate and timecode, the ffmpeg command, a real encode read back, and which
jobs get one (the workflow, the manager's preview settings, a movie input's rate)."""

from __future__ import annotations

import asyncio
import json
import shutil
import struct
import tempfile
import types
import unittest
import zlib
from pathlib import Path

import yaml

from comfyfarm.config import Config, ConvertConfig
from comfyfarm.fileio import media as M
from comfyfarm.fileio import preview as PV
from comfyfarm.fileio.errors import ConvertError
from comfyfarm.fileio.tools import find_tool
from comfyfarm.workflows import Workflow, WorkflowRegistry, from_process

REPO = Path(__file__).resolve().parent.parent
REGISTRY = WorkflowRegistry(REPO / "workflows")
FFMPEG = find_tool("ffmpeg")


def png(width: int, height: int, grey: int = 128) -> bytes:
    """An RGB PNG of one grey, written by hand (no imaging library needed)."""
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    rows = (b"\x00" + bytes([grey]) * 3 * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


SETTINGS = {"fps": 24.0, "fps_str": "24/1", "max_width": 1920, "crf": 18}


class Helpers(unittest.TestCase):
    def test_timecode_both_ways(self):
        self.assertEqual(M.frame_to_timecode(1001, 24), "00:00:41:17")
        self.assertEqual(M.frame_to_timecode(1001, 25), "00:00:40:01")
        self.assertEqual(M.frame_to_timecode(1001, 24000 / 1001), "00:00:41:17")     # 23.976 isn't drop-frame
        self.assertEqual(M.frame_to_timecode(1800, 30000 / 1001), "00:01:00;02")     # 29.97 is: ;00 and ;01 skipped
        self.assertIsNone(M.frame_to_timecode(-1, 24))
        for fps in (24, 25, 24000 / 1001, 30000 / 1001, 50, 60000 / 1001):
            for frame in (0, 1, 1001, 1799, 1800, 17982, 86399, 123457):
                self.assertEqual(M.timecode_to_frame(M.frame_to_timecode(frame, fps), fps), frame, (fps, frame))

    def test_rate_and_size(self):
        self.assertEqual([PV.rate_text(r) for r in (24, 25, 23.976, 24000 / 1001, 29.97, 59.94, 12.5)],
                         ["24/1", "25/1", "24000/1001", "24000/1001", "30000/1001", "60000/1001", "25/2"])
        self.assertEqual(PV.movie_size((2049, 1081), 1920), (1920, 1012))      # scaled, both sides even
        self.assertEqual(PV.movie_size((3840, 2160), 1920), (1920, 1080))
        self.assertEqual(PV.movie_size((1280, 720), 1920), (1280, 720))        # smaller: as it is
        self.assertEqual(PV.movie_size((1001, 555), 0), (1000, 554))           # no cap: just even
        self.assertEqual(PV.movie_size((1, 1), 1920), (2, 2))
        self.assertIsNone(PV.movie_size(None, 1920))

    def test_the_command(self):
        cmd = PV.movie_command(["ffmpeg"], "/r/x.alpha_v003.%04d.png", 1001, 84, SETTINGS, (1920, 1012), "/r/o.mp4")
        arg = lambda k: cmd[cmd.index(k) + 1]   # noqa: E731
        self.assertEqual((arg("-framerate"), arg("-start_number"), arg("-i"), arg("-frames:v")),
                         ("24/1", "1001", "/r/x.alpha_v003.%04d.png", "84"))
        self.assertTrue(arg("-vf").startswith("scale=1920:1012:"))
        self.assertIn("out_color_matrix=bt709", arg("-vf"))                     # not ffmpeg's BT.601 default
        self.assertEqual((arg("-c:v"), arg("-preset"), arg("-crf"), arg("-pix_fmt")), ("libx264", "veryfast", "18", "yuv420p"))
        self.assertEqual((arg("-timecode"), arg("-colorspace"), cmd[-1]), ("00:00:41:17", "bt709", "/r/o.mp4"))
        self.assertIn("+faststart", cmd)                                        # plays before it's all loaded
        unknown = PV.movie_command(["ffmpeg"], "/r/x.%04d.png", 1, 2, {"fps": 25}, None, "/r/o.mp4")
        self.assertIn("trunc(iw/2)*2", unknown[unknown.index("-vf") + 1])
        self.assertEqual(unknown[unknown.index("-framerate") + 1], "25/1")

    def test_ffmpegs_errors_read_well(self):
        out = ("[png @ 0x55f16a33bfc0] Invalid PNG signature 0x00000000.\n"
               "[vist#0:0/png @ 0x55f1] Decoding error: Invalid data found when processing input\n"
               "    Last message repeated 1 times\n"
               "[vist#0:0/png @ 0x55f1] Decoding error: Invalid data found when processing input\n")
        self.assertEqual(PV.why(out), "Invalid PNG signature 0x00000000. / Decoding error: Invalid data found "
                                      "when processing input")
        self.assertTrue(PV.why("x" * 50 + "\n" + "y" * 400).endswith(" …"))


@unittest.skipUnless(FFMPEG, "no ffmpeg on this machine")
class Encode(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def frames(self, width, height, first=1001, count=5):
        for i in range(count):
            (self.dir / f"sh_010.alpha_v003.{first + i:04d}.png").write_bytes(png(width, height, 40 * i))
        return {"frame_pattern": f"{self.dir.as_posix()}/sh_010.alpha_v003.%04d.png", "first": first, "frames": count}

    def test_a_movie_numbered_like_its_frames(self):
        dst = str(self.dir / "sh_010.alpha_v003_preview.mp4")
        asyncio.run(PV.make_movie(self.frames(2050, 60), dict(SETTINGS, max_width=1024), dst, ConvertConfig()))
        info = M.read_movie_info(dst, ConvertConfig())
        self.assertEqual((info["frames"], info["fps"], info["width"], info["height"], info["codec"]),
                         (5, 24.0, 1024, 30, "h264"))
        # Read back as an input, it's numbered from its first frame, like the frames.
        self.assertEqual(M.timecode_to_frame(info["timecode"], info["fps"]), 1001)
        self.assertEqual([p.name for p in self.dir.glob("*.mp4")], ["sh_010.alpha_v003_preview.mp4"])   # no .part left

    def test_a_movie_at_a_movie_inputs_rate(self):
        dst = str(self.dir / "p.mp4")
        asyncio.run(PV.make_movie(self.frames(64, 36, first=86400), {"fps": 24000 / 1001, "fps_str": "24000/1001",
                                                                     "max_width": 1920, "crf": 18}, dst, ConvertConfig()))
        info = M.read_movie_info(dst, ConvertConfig())
        self.assertAlmostEqual(info["fps"], 23.976, places=3)
        self.assertEqual(M.timecode_to_frame(info["timecode"], info["fps"]), 86400)

    def test_frames_ffmpeg_cant_read(self):
        for f in (1001, 1002):
            (self.dir / f"x.{f}.png").write_bytes(b"not a png")
        dst = str(self.dir / "x_preview.mp4")
        with self.assertRaises(ConvertError) as e:
            asyncio.run(PV.make_movie({"frame_pattern": f"{self.dir.as_posix()}/x.%04d.png", "first": 1001,
                                       "frames": 2}, SETTINGS, dst, ConvertConfig()))
        self.assertIn("ffmpeg couldn't make it", str(e.exception))
        self.assertEqual(list(self.dir.glob("*.mp4")), [])                     # nothing half-written left


class WhichJobs(unittest.TestCase):
    def test_workflows(self):
        wfs = REGISTRY.workflows
        self.assertTrue(all(wfs[n].makes_preview for n in ("ltx25_alpha_gen", "ltx25_layout_render", "wan22_i2i")))
        self.assertFalse(wfs["chord_pbr"].makes_preview)                         # texture maps: no movie
        self.assertTrue(wfs["ltx25_alpha_gen"].schema()["preview"])

    def definition(self, d: Path, preview, as_manifest=False) -> Workflow:
        """wan22_i2i saying `preview`: in its process.yaml's writes, or as a manifest's top-level key."""
        shutil.copytree(REPO / "workflows" / "wan22_i2i", d / "wf")
        p = yaml.safe_load((d / "wf" / "process.yaml").read_text(encoding="utf-8"))
        if as_manifest:
            m, _ = from_process(p)
            (d / "wf" / "process.yaml").unlink()
            (d / "wf" / "manifest.yaml").write_text(json.dumps({**m, "preview": preview}), encoding="utf-8")
        else:
            p["writes"]["preview"] = preview
            (d / "wf" / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        return Workflow(d / "wf")

    def test_a_workflow_can_opt_out(self):
        for as_manifest in (False, True):
            with self.subTest(manifest=as_manifest), tempfile.TemporaryDirectory() as d:
                wf = self.definition(Path(d), False, as_manifest)
                self.assertFalse(wf.makes_preview)
                self.assertEqual([p for p in wf.problems if "preview" in p], [])
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "writes.preview: true .*, false or own"):
                self.definition(Path(d), "no")
        with tempfile.TemporaryDirectory() as d:
            wf = self.definition(Path(d), "no", as_manifest=True)
            self.assertIn("preview: needs to be true or false", " ".join(wf.problems))

    def test_settings_frozen_per_job(self):
        from comfyfarm.manager.db import JobStore
        from comfyfarm.manager.events import EventBus
        from comfyfarm.manager.farm import Farm
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = Config.model_validate({"workers": [], "database": str(tmp / "f.db"),
                                     "preview": {"fps": 25, "max_width": 1280, "crf": 20}})
        store = JobStore(tmp / "f.db")
        self.addCleanup(store.close)
        farm = Farm(cfg, store, REGISTRY, EventBus())
        ltx, chord = REGISTRY.workflows["ltx25_alpha_gen"], REGISTRY.workflows["chord_pbr"]
        images = types.SimpleNamespace(convert=None)
        self.assertEqual(farm._movie(ltx, images), {"fps": 25.0, "fps_str": "25/1", "max_width": 1280, "crf": 20})
        movie = types.SimpleNamespace(convert={"kind": "movie", "fps": 24000 / 1001, "fps_str": "24000/1001"})
        self.assertEqual(farm._movie(ltx, movie)["fps_str"], "24000/1001")     # the input movie's own rate
        self.assertIsNone(farm._movie(chord, images))
        cfg.preview.enabled = False
        self.assertIsNone(farm._movie(ltx, images))


if __name__ == "__main__":
    unittest.main()
