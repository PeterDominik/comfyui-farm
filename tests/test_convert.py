import os
import shutil
import subprocess
import tempfile
import unittest

from comfyfarm.config import ConvertConfig, PathMapping, PathsConfig
import sys
import types
from unittest import mock

from comfyfarm import convert
from comfyfarm.convert import (_shot_short, find_tool, parse_ffmpeg_info, parse_probe, point_loader_at_scratch,
                               probe_movie, probe_movie_ffmpeg, read_movie_info, resolve_input, timecode_to_frame)
from comfyfarm.paths import SequenceError

RODEO_SEARCH_PATH = ('search_path: "../../_color_bundle/${SEQ}_${SHOT}:../../_color_bundle/${SEQ}:'
                     '../../_color_bundle/${SHOT}:project_luts:rodeo_luts:ACES_luts"\n')
CONFIG = ("ocio_profile_version: 1\n" + RODEO_SEARCH_PATH +
          "displays:\n  sRGB-monitor:\n    - !<View> {name: Client-look, colorspace: Client-look}\n")


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
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = self.tmp.replace("\\", "/")
        self.lut = f"{self.root}/shows/raa/_project_ref/_lut"
        os.makedirs(f"{self.lut}/_ocio/current")
        with open(f"{self.lut}/_ocio/current/config.ocio", "w") as fh:
            fh.write(CONFIG)
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
                             self.pc, self.cc)

    def test_shot_short(self):
        self.assertEqual(_shot_short("bpash", "bpash_1545"), "1545")
        self.assertEqual(_shot_short("15454", "15454_656551"), "656551")
        self.assertEqual(_shot_short("abc", "xyz_0010"), "xyz_0010")
        self.assertEqual(_shot_short(None, "bpash_1545"), "bpash_1545")

    def test_shot_bundle_found_through_search_path(self):
        os.makedirs(f"{self.lut}/_color_bundle/bpash_1545")
        os.makedirs(f"{self.lut}/_color_bundle/bpash")          # seq bundle: shot one wins
        c = self.exr().convert
        self.assertEqual(c["context"], {"SEQ": "bpash", "SHOT": "1545"})
        self.assertEqual(c["bundle"], f"{self.lut}/_color_bundle/bpash_1545")
        self.assertEqual(c["config"], f"{self.lut}/_ocio/current/config.ocio")
        self.assertEqual((c["display"], c["view"], c["input_colorspace"]), ("sRGB-monitor", "Client-look", "scene_linear"))
        self.assertEqual(c["notes"], [])

    def test_seq_bundle_then_show_default(self):
        os.makedirs(f"{self.lut}/_color_bundle/bpash")
        self.assertEqual(self.exr().convert["bundle"], f"{self.lut}/_color_bundle/bpash")
        shutil.rmtree(f"{self.lut}/_color_bundle")
        c = self.exr().convert
        self.assertIsNone(c["bundle"])
        self.assertIn("show's default LUTs", c["notes"][0])

    def test_config_folder_and_missing_view(self):
        self.cc.exr.ocio_config = "/rdo/shows/{show}/_project_ref/_lut/_ocio/current"   # a folder
        self.assertTrue(self.exr().convert["config"].endswith("/current/config.ocio"))
        self.cc.exr.view = "Nope-look"
        with self.assertRaisesRegex(SequenceError, "view 'Nope-look' isn't in"):
            self.exr()

    def test_missing_config(self):
        os.remove(f"{self.lut}/_ocio/current/config.ocio")
        with self.assertRaisesRegex(SequenceError, "No OCIO config at /rdo/shows/raa/"):
            self.exr()

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

    def test_resolve(self):
        seq = resolve_input("/rdo/shows/raa/.published/bpash/bpash_1545/edit/ref.mov 1003-1005", self.pc, ConvertConfig())
        self.assertEqual((seq.kind, seq.first, seq.last, seq.frame_count, seq.shot, seq.source),
                         ("movie", 1003, 1005, 3, "bpash_1545", "ref"))
        self.assertEqual((seq.convert["start"], seq.convert["index_first"], seq.convert["index_last"]), (1001, 2, 4))
        with self.assertRaisesRegex(SequenceError, "doesn't exist"):
            resolve_input("/rdo/shows/raa/nope.mov", self.pc, ConvertConfig())


if __name__ == "__main__":
    unittest.main()
