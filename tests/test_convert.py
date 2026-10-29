import os
import shutil
import subprocess
import tempfile
import unittest

import sys
import types
from pathlib import Path
from unittest import mock

from comfyfarm import convert
from comfyfarm.config import ConvertConfig, OcioConfig, PathMapping, PathsConfig
from comfyfarm.convert import (exr_command, find_tool, ocio_config_path, ocio_setup, parse_ffmpeg_info,
                               parse_probe, point_loader_at_scratch, probe_movie, probe_movie_ffmpeg,
                               read_movie_info, resolve_input, summary, timecode_to_frame)
from comfyfarm.paths import SequenceError

REPO = Path(__file__).resolve().parent.parent
CONFIG = ("ocio_profile_version: 2\nroles:\n  scene_linear: ACES - ACEScg\n  color_picking: Output - sRGB\n"
          "colorspaces:\n  - !<ColorSpace>\n    name: ACES - ACEScg\n  - !<ColorSpace>\n    name: Output - sRGB\n")


class TimecodeTests(unittest.TestCase):
    def test_non_drop(self):
        self.assertEqual(timecode_to_frame("00:00:41:17", 24), 1001)
        self.assertEqual(timecode_to_frame("01:00:00:00", 25), 90000)
        self.assertEqual(timecode_to_frame("00:00:00:00", 23.976), 0)

    def test_drop_frame(self):
        fps = 30000 / 1001
        self.assertEqual(timecode_to_frame("00:01:00;02", fps), 1800)   # first frame after the drop
        self.assertEqual(timecode_to_frame("00:10:00;00", fps), 17982)
        self.assertEqual(timecode_to_frame("00:01:00;04", 60000 / 1001), 3600)

    def test_bad(self):
        with self.assertRaises(ValueError):
            timecode_to_frame("41:17", 24)


class ProbeTests(unittest.TestCase):
    def test_parse(self):
        data = {"streams": [{"codec_type": "video", "codec_name": "prores", "avg_frame_rate": "24000/1001",
                             "nb_frames": "158", "width": 4096, "height": 2160, "color_space": "bt709"},
                            {"codec_type": "data", "codec_tag_string": "tmcd", "tags": {"timecode": "01:00:00:00"}}]}
        p = parse_probe(data)
        self.assertEqual((p["frames"], p["timecode"], p["fps_str"], p["codec"]), (158, "01:00:00:00", "24000/1001", "prores"))

    def test_frames_from_duration(self):
        p = parse_probe({"streams": [{"codec_type": "video", "r_frame_rate": "25/1", "avg_frame_rate": "0/0"}],
                         "format": {"duration": "4.0", "tags": {"timecode": "00:00:40:00"}}})
        self.assertEqual((p["frames"], p["timecode"]), (100, "00:00:40:00"))

    def test_no_video(self):
        with self.assertRaisesRegex(SequenceError, "no video stream"):
            parse_probe({"streams": [{"codec_type": "audio"}]})


FFMPEG_INFO = """Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'df.mov':
  Metadata:
    major_brand     : qt
  Duration: 00:00:01.33, start: 0.000000, bitrate: 6108 kb/s
  Stream #0:0[0x1]: Video: prores (4444) (ap4h / 0x68347061), yuv444p12le(bt709, progressive), 4096x2160, 6098 kb/s, SAR 1:1 DAR 16:9, 29.97 fps, 29.97 tbr, 30k tbn (default)
    Metadata:
      handler_name    : VideoHandler
      timecode        : 00:01:00;02
  Stream #0:1[0x2](eng): Data: none (tmcd / 0x64636D74), 0 kb/s
At least one output file must be specified
"""


class FfmpegOnlyProbeTests(unittest.TestCase):
    """pip's ffmpeg (imageio-ffmpeg) has no ffprobe; movie info is read with ffmpeg."""

    def test_parse_info(self):
        p = parse_ffmpeg_info(FFMPEG_INFO, 40)
        self.assertEqual((p["fps_str"], p["frames"], p["timecode"], p["width"], p["height"], p["codec"],
                          p["color_space"]), ("30000/1001", 40, "00:01:00;02", 4096, 2160, "prores", "bt709"))

    def test_frames_from_duration_and_plain_rates(self):
        text = FFMPEG_INFO.replace("29.97 fps, 29.97 tbr", "25 fps, 25 tbr").replace("yuv444p12le(bt709, progressive)", "yuv422p10le")
        p = parse_ffmpeg_info(text, None)
        self.assertEqual((p["fps_str"], p["frames"], p["color_space"]), ("25/1", 33, None))

    def test_no_video(self):
        with self.assertRaisesRegex(SequenceError, "no video stream"):
            parse_ffmpeg_info("Input #0 ...\n  Stream #0:0: Audio: aac\n", 10)

    def test_ffmpeg_used_when_no_ffprobe(self):
        with mock.patch.object(convert, "find_tool", side_effect=lambda n, c=None: None if n == "ffprobe" else ["ffmpeg"]), \
                mock.patch.object(convert, "probe_movie_ffmpeg", return_value={"via": "ffmpeg"}) as m:
            self.assertEqual(read_movie_info("x.mov", ConvertConfig()), {"via": "ffmpeg"})
            m.assert_called_once_with("x.mov", ["ffmpeg"])

    def test_find_tool_uses_pip_ffmpeg(self):
        exe = tempfile.NamedTemporaryFile(delete=False)
        exe.close()
        fake = types.SimpleNamespace(get_ffmpeg_exe=lambda: exe.name)
        try:
            with mock.patch.dict(sys.modules, {"imageio_ffmpeg": fake}), \
                    mock.patch.object(convert.sys, "executable", os.path.join(tempfile.gettempdir(), "venv", "python")):
                self.assertEqual(find_tool("ffmpeg"), [exe.name])
                self.assertEqual(find_tool("ffmpeg", "C:/tools/ffmpeg.exe"), ["C:/tools/ffmpeg.exe"])   # config wins
        finally:
            os.remove(exe.name)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    def test_matches_ffprobe_on_real_files(self):
        tmp = tempfile.mkdtemp()
        try:
            for name, rate, tc, extra in (("a.mov", "24", "00:00:41:17", []),
                                          ("b.mov", "30000/1001", "00:01:00;02", ["-colorspace", "bt709"])):
                path = os.path.join(tmp, name)
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=64x36:rate={rate}",
                                "-frames:v", "17", "-timecode", tc, "-c:v", "prores_ks", *extra, path], check=True)
                a, b = probe_movie_ffmpeg(path, ["ffmpeg"]), probe_movie(path, ["ffprobe"])
                for key in ("fps_str", "frames", "timecode", "width", "height", "codec", "color_space"):
                    self.assertEqual(a[key], b[key], f"{name} {key}")
        finally:
            shutil.rmtree(tmp)


class OcioTests(unittest.TestCase):
    """EXRs are converted with the repo's OCIO config: linear -> color_picking, as a Nuke
    Write set to "default (color_picking)" does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = self.tmp.replace("\\", "/")
        os.makedirs(f"{self.root}/ocio")
        with open(f"{self.root}/ocio/config.ocio", "w") as fh:
            fh.write(CONFIG)
        self.oc = OcioConfig(config=f"{self.root}/ocio/config.ocio")
        self.pc = PathsConfig(mappings=[PathMapping(posix="/rdo/shows", windows=f"{self.root}/shows")])
        self.cc = ConvertConfig(tools={"oiiotool": "oiiotool-not-needed", "ffmpeg": "", "ffprobe": ""})
        self.plates = f"{self.root}/shows/raa/.published/bpash/bpash_1545/plate/v1"
        os.makedirs(self.plates)
        for f in (1001, 1002):
            open(f"{self.plates}/bpash_1545.plate.v1.{f}.exr", "wb").close()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def exr(self, rng="1001-1002"):
        return resolve_input(f"/rdo/shows/raa/.published/bpash/bpash_1545/plate/v1/bpash_1545.plate.v1.%04d.exr {rng}",
                             self.pc, self.cc, ocio=self.oc)

    def test_color_picking_like_a_nuke_write(self):
        c = self.exr().convert
        self.assertEqual((c["config"], c["from"], c["to"]),
                         (f"{self.root}/ocio/config.ocio", "scene_linear", "color_picking"))
        self.assertEqual(exr_command(["oiiotool"], c, "in.exr", "out.png"),
                         ["oiiotool", "--colorconfig", f"{self.root}/ocio/config.ocio", "in.exr", "--ch", "R,G,B",
                          "--croptofull", "--colorconvert", "scene_linear", "color_picking", "-d", "uint8",
                          "-o", "out.png"])
        self.assertEqual(summary(self.exr(), self.pc)["to"], "color_picking")

    def test_repo_relative_config(self):
        self.assertEqual(ocio_config_path(OcioConfig()), (REPO / "ocio/config.ocio").as_posix())
        setup = ocio_setup(self.oc, self.pc)
        self.assertEqual((setup["linear"], setup["image"], setup["data"]), ("scene_linear", "color_picking", "data"))

    def test_old_ocio_keys_are_reported(self):
        from comfyfarm.config import load_config
        path = f"{self.root}/config.yaml"
        with open(path, "w") as fh:
            fh.write("ocio:\n  config: ocio/config.ocio\n  linear: scene_linear\n  image: color_picking\n"
                     "  data: data\n  context: {}\n")
        cfg = load_config(path)
        self.assertEqual(cfg.ocio.config, "ocio/config.ocio")
        self.assertTrue(any(n.startswith("ocio: context, data, image, linear no longer used") for n in cfg.obsolete_keys),
                        cfg.obsolete_keys)

    def test_missing_config(self):
        os.remove(f"{self.root}/ocio/config.ocio")
        with self.assertRaisesRegex(SequenceError, "No OCIO config at .*git pull"):
            self.exr()

    def test_jobs_queued_before_the_switch_keep_their_settings(self):
        old = {"config": "/x/config.ocio", "extra_args": ["--ch", "R,G,B"], "input_colorspace": "scene_linear",
               "display": "sRGB-monitor", "view": "Client-look"}
        self.assertIn("--ociodisplay:from=scene_linear", exr_command(["oiiotool"], old, "a.exr", "a.png"))

    def test_scratch_loader(self):
        seq = self.exr()
        point_loader_at_scratch(seq, "C:/farm/scratch", "job1")
        self.assertEqual((seq.load_dir, seq.skip, seq.cap), ("C:/farm/scratch/job1/", 0, 0))
        self.assertEqual(seq.convert["dst_pattern"], "bpash_1545.plate.v1.%04d.png")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class MovieTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = self.tmp.replace("\\", "/")
        self.dir = f"{self.root}/shows/raa/.published/bpash/bpash_1545/edit"
        os.makedirs(self.dir)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x36:rate=24", "-frames:v", "10",
                        "-timecode", "00:00:41:17", "-c:v", "prores_ks", f"{self.dir}/ref.mov"], check=True)
        self.pc = PathsConfig(mappings=[PathMapping(posix="/rdo/shows", windows=f"{self.root}/shows")])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def make(self, path, tc=None, frames=10):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=64x36:rate=24", "-frames:v", str(frames)]
        codec = "mpeg4" if path.endswith(".mp4") else "prores_ks"   # mp4 can't hold ProRes
        cmd += (["-timecode", tc] if tc else []) + ["-c:v", codec, path]
        subprocess.run(cmd, check=True)

    def test_movhistory_slate_skipped(self):
        root = f"{self.root}/shows/raa/bpash/1545/movhistory"
        self.make(f"{root}/bpash_1545.ingest.compPlate.v2.mov", "00:00:41:16")   # slate = 1000
        posix = "/rdo/shows/raa/bpash/1545/movhistory/bpash_1545.ingest.compPlate.v2.mov"
        seq = resolve_input(posix, self.pc, ConvertConfig())
        c = seq.convert
        self.assertEqual((seq.first, seq.last, seq.frame_count, seq.shot), (1001, 1009, 9, "bpash_1545"))
        self.assertEqual((c["slate_frame"], c["start"], c["end"], c["index_first"], c["index_last"]), (1000, 1001, 1009, 1, 9))
        seq = resolve_input(posix + " 1003-1004", self.pc, ConvertConfig())
        self.assertEqual((seq.convert["index_first"], seq.convert["index_last"]), (3, 4))
        with self.assertRaisesRegex(SequenceError, "plate runs 1001-1009 \\(frame 1000 is the slate\\)"):
            resolve_input(posix + " 1000-1004", self.pc, ConvertConfig())

    def test_slate_only_for_mov_in_movhistory(self):
        self.make(f"{self.root}/shows/raa/bpash/1545/movhistory/clip.mp4", "00:00:41:16")
        seq = resolve_input("/rdo/shows/raa/bpash/1545/movhistory/clip.mp4", self.pc, ConvertConfig())
        self.assertEqual((seq.first, seq.convert["slate_frame"]), (1000, None))     # .mp4: no slate
        seq = resolve_input("/rdo/shows/raa/.published/bpash/bpash_1545/edit/ref.mov", self.pc, ConvertConfig())
        self.assertEqual((seq.first, seq.convert["slate_frame"]), (1001, None))     # not movhistory

    def test_movhistory_without_timecode(self):
        self.make(f"{self.root}/shows/raa/bpash/1545/movhistory/notc.mov", None, 5)
        seq = resolve_input("/rdo/shows/raa/bpash/1545/movhistory/notc.mov", self.pc, ConvertConfig())
        self.assertEqual((seq.first, seq.last, seq.convert["slate_frame"]), (1001, 1004, 1000))

    def test_slate_frame_not_converted(self):
        import asyncio
        root = f"{self.root}/shows/raa/bpash/1545/movhistory"
        self.make(f"{root}/plate.mov", "00:00:41:16", 6)
        seq = resolve_input("/rdo/shows/raa/bpash/1545/movhistory/plate.mov", self.pc, ConvertConfig())
        point_loader_at_scratch(seq, f"{self.root}/scratch", "job")
        os.makedirs(seq.convert["dst_dir"])
        asyncio.run(convert.convert_movie(seq.convert, ConvertConfig(), seq.first, seq.frame_count, lambda *a: None))
        self.assertEqual(sorted(os.listdir(seq.convert["dst_dir"])), [f"plate.{f}.png" for f in range(1001, 1006)])
        # Frame 1001 is the movie's second frame (the first after the slate).
        full = f"{self.root}/full"
        os.makedirs(full)
        subprocess.run(["ffmpeg", "-v", "error", "-i", f"{root}/plate.mov", "-vf", "scale=in_color_matrix=bt709",
                        "-fps_mode", "passthrough", "-start_number", "1000", "-pix_fmt", "rgb24", f"{full}/f.%04d.png"],
                       check=True)
        with open(f"{full}/f.1001.png", "rb") as a, open(seq.convert["dst_dir"] + "plate.1001.png", "rb") as b:
            self.assertEqual(a.read(), b.read())
        with open(f"{full}/f.1000.png", "rb") as a, open(seq.convert["dst_dir"] + "plate.1001.png", "rb") as b:
            self.assertNotEqual(a.read(), b.read())

    def test_resolve(self):
        seq = resolve_input("/rdo/shows/raa/.published/bpash/bpash_1545/edit/ref.mov 1003-1005", self.pc, ConvertConfig())
        self.assertEqual((seq.kind, seq.first, seq.last, seq.frame_count, seq.shot, seq.source),
                         ("movie", 1003, 1005, 3, "bpash_1545", "ref"))
        self.assertEqual((seq.convert["start"], seq.convert["index_first"], seq.convert["index_last"]), (1001, 2, 4))
        with self.assertRaisesRegex(SequenceError, "doesn't exist"):
            resolve_input("/rdo/shows/raa/nope.mov", self.pc, ConvertConfig())


if __name__ == "__main__":
    unittest.main()
