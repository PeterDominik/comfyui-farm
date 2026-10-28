"""Texture workflows: TEX naming, UDIM inputs, folder inputs, and the EXR writing step.

    python -m unittest tests.test_textures -v
"""

import asyncio
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from comfyfarm import textures as T
from comfyfarm.__main__ import ocio_roles
from comfyfarm.config import ConvertConfig, OcioConfig, PathMapping, PathsConfig, TexturesConfig
from comfyfarm.convert import REPO, exr_command, resolve_input, summary
from comfyfarm.paths import SequenceError, loaded_names, resolve_sequence, texture_input, texture_numbers
from comfyfarm.paths import Sequence
from comfyfarm.workflows import ParamError, Workflow

REPO = Path(__file__).resolve().parent.parent
WF = REPO / "workflows" / "chord_pbr"
FAKE_OIIOTOOL = [sys.executable, str(REPO / "tests" / "fake_oiiotool.py")]


def touch(d, *names):
    os.makedirs(d, exist_ok=True)
    for n in names:
        open(os.path.join(d, n), "wb").close()


MAPS = [{"name": "basecolor", "channel": "color01", "colorspace": "acescg", "color": True, "bits": "half"},
        {"name": "height", "channel": "bmp02", "colorspace": "acescg", "color": False, "bits": "float"}]
TOKENS = {"uvset": "map1", "tile": "noTile", "state": "defState", "description": ""}
OCIO_CONFIG = ("ocio_profile_version: 2\nroles:\n  default: ACES - ACEScg\n  scene_linear: ACES - ACEScg\n"
               "  color_picking: Output - sRGB\n  data: Raw\n"
               "displays:\n  sRGB:\n    - !<View> {name: Raw, colorspace: Raw}\n"
               "colorspaces:\n  - !<ColorSpace>\n    name: ACES - ACEScg\n  - !<ColorSpace>\n    name: Output - sRGB\n"
               "  - !<ColorSpace>\n    name: Raw\n    isdata: true\n")
# A job's frozen OCIO settings (convert.ocio_setup) with the default names.
JOB_OCIO = {"config": "/farm/ocio/config.ocio", "linear": "scene_linear", "image": "color_picking", "data": "data",
            "context": {}}


class NamingTests(unittest.TestCase):
    def test_numbers_from_names(self):
        self.assertEqual(texture_numbers("rock_diffuse.1002.png"), (1002, 0, True))
        self.assertEqual(texture_numbers("rock_diffuse_1011.tif"), (1011, 0, True))
        self.assertEqual(texture_numbers("acescg_map1_noTile_defState_color01.1003.0007.exr"), (1003, 7, True))
        self.assertEqual(texture_numbers("photo.jpg"), (1001, 0, False))
        self.assertEqual(texture_numbers("photo_2.png"), (1001, 0, False))          # not a UDIM
        self.assertEqual(texture_numbers("lava.1005.png", "frame"), (1001, 1005, False))
        self.assertEqual(texture_numbers("lava.0012.png", "frame"), (1001, 12, False))

    def test_convention_names(self):
        tokens = T.name_tokens({"uvset": "map1", "state": "defState"}, "noTile")
        self.assertEqual(T.file_name(T.DEFAULT_NAME, MAPS[0], tokens, 1001, 0),
                         "acescg_map1_noTile_defState_color01.1001.0000.exr")
        tokens = T.name_tokens({"uvset": "map10-preview", "state": "damaged", "description": "sand"}, "tile")
        self.assertEqual(T.file_name(T.DEFAULT_NAME, MAPS[1], tokens, 1001, 12),
                         "acescg_map10-preview_tile_damaged_bmp02-sand.1001.0012.exr")

    def test_tile_choice(self):
        tiles = [(1001, 0), (1002, 0)]
        self.assertEqual(T.resolve_tile("auto", tiles, True), "noTile")
        self.assertEqual(T.resolve_tile("auto", [(1001, 0)], False), "tile")
        self.assertEqual(T.resolve_tile("noTile", [(1001, 0)], False), "noTile")
        with self.assertRaisesRegex(SequenceError, "one 1001 tile"):
            T.resolve_tile("tile", tiles, True)

    def test_duplicate_output_names_refused(self):
        items, has = T.items_for(["a.1001.png", "a.1002.png"], "udim")
        self.assertEqual((items, has), ([(1001, 0), (1002, 0)], True))
        with self.assertRaisesRegex(SequenceError, "set Numbering to frame"):
            T.items_for(["lava.0001.png", "lava.0002.png"], "udim")
        self.assertEqual(T.items_for(["lava.0001.png", "lava.0002.png"], "frame")[0], [(1001, 1), (1001, 2)])

    def test_patterns_for_reads(self):
        self.assertEqual(T.name_pattern(T.DEFAULT_NAME, MAPS[0], TOKENS, [(1001, 0), (1002, 0), (1011, 0)]),
                         ("acescg_map1_noTile_defState_color01.%04d.0000.exr", 1001, 1011))
        self.assertEqual(T.name_pattern(T.DEFAULT_NAME, MAPS[0], TOKENS, [(1001, 3), (1001, 4)]),
                         ("acescg_map1_noTile_defState_color01.1001.%04d.exr", 3, 4))
        self.assertEqual(T.name_pattern(T.DEFAULT_NAME, MAPS[0], TOKENS, [(1001, 0)]),
                         ("acescg_map1_noTile_defState_color01.1001.0000.exr", None, None))

    def test_output_folder_falls_back(self):
        base = {"root": "//srv/r", "show": "xyz", "seq": None, "shot": None, "source": "rock", "tag": "chord"}
        self.assertEqual(T.output_dir(T.DEFAULT_DIRS, version=2, asset="rockA", **base), "//srv/r/rockA/rock.chord_v002")
        self.assertEqual(T.output_dir(T.DEFAULT_DIRS, version=1, asset=None, **base), "//srv/r/rock.chord_v001")
        self.assertEqual(T.describe(5, [(1001, 0), (1002, 0)], True, "udim"), "10 EXRs (5 maps × 2 UDIM tiles)")
        self.assertEqual(T.describe(5, [(1001, 0)], False, "udim"), "5 EXRs (5 maps × 1 texture)")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.wf = Workflow(WF)

    def test_chord_manifest_is_clean(self):
        self.assertEqual(self.wf.problems, [])
        self.assertEqual([m.channel for m in self.wf.textures.maps], ["color01", "nrm02", "bmp02", "spcr01", "metal01"])
        self.assertEqual({m.channel: m.bits for m in self.wf.textures.maps}["bmp02"], "float")
        self.assertEqual([m.channel for m in self.wf.textures.maps if m.color], ["color01"])

    def test_template_has_no_show_paths(self):
        tpl = json.loads((WF / "workflow_api.json").read_text(encoding="utf-8"))
        self.assertEqual((tpl["14"]["inputs"]["value"], tpl["23"]["inputs"]["value"]), ("", ""))
        self.assertNotIn("pdominik", json.dumps(tpl))

    def test_reserved_and_bad_channels_refused(self):
        wf = Workflow(WF)
        wf.textures.maps[1].channel = "nrm01"
        wf.textures.maps[2].channel = "bump"
        probs = " ".join(wf.static_check())
        self.assertIn("nrm01 is reserved for maps published by modeling", probs)
        self.assertIn("textures map bump: channel should be a TEX channel", probs)
        wf = Workflow(WF)
        wf.textures.name = "{colorspace}_{channel}.exr"
        self.assertIn("needs {udim}", " ".join(wf.static_check()))

    def test_naming_param_validation(self):
        v = self.wf.validate({"input": "x", "uvset": "map10-preview", "state": "burnedFace", "tile": "noTile"})
        self.assertEqual((v["uvset"], v["state"], v["description"], v["numbering"]),
                         ("map10-preview", "burnedFace", "", "udim"))
        with self.assertRaisesRegex(ParamError, "map plus 1 or 2 digits"):
            self.wf.validate({"input": "x", "uvset": "uv1"})
        with self.assertRaisesRegex(ParamError, "one word"):
            self.wf.validate({"input": "x", "state": "def state"})
        with self.assertRaisesRegex(ParamError, "one of"):
            self.wf.validate({"input": "x", "tile": "tiled"})

    def test_build_points_every_map_at_scratch(self):
        v = self.wf.validate({"input": "x"})
        seq = Sequence(raw="x", farm_path="//srv/t/rock.%04d.png", directory="//srv/t/", filename="rock.%04d.png",
                       first=1001, last=1002, frame_count=2, skip=0, cap=0, show="xyz", shot=None)
        tex = {"raw_dir": "C:/farm/scratch/j1/maps",
               "maps": [{"node": m.node, "prefix": f"_cfj1_{m.channel}"} for m in self.wf.textures.maps]}
        before = copy.deepcopy(self.wf.template)
        p = self.wf.build(v, seq, "//srv/out/rock.chord_v001", textures=tex)
        self.assertEqual(self.wf.template, before)
        for m in self.wf.textures.maps:
            self.assertEqual(p[m.node]["inputs"]["output_path"], "C:/farm/scratch/j1/maps")
            self.assertEqual(p[m.node]["inputs"]["filename_prefix"], f"_cfj1_{m.channel}")
        self.assertEqual(p["17"]["inputs"]["directory"], "//srv/t/")
        self.assertEqual(p["23"]["inputs"]["value"], "//srv/out/rock.chord_v001")
        self.assertNotIn("uvset", json.dumps(p))   # naming params never reach ComfyUI


class InputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name.replace("\\", "/")
        self.dir = f"{self.root}/shows/xyz/_asset/rockA/cg/textures/src"
        self.cfg = PathsConfig(mappings=[PathMapping(posix="/rdo/shows", windows=f"{self.root}/shows")])
        self.posix = "/rdo/shows/xyz/_asset/rockA/cg/textures/src"
        touch(f"{self.root}/ocio")
        Path(f"{self.root}/ocio/config.ocio").write_text(OCIO_CONFIG)
        self.oc = OcioConfig(config=f"{self.root}/ocio/config.ocio")

    def tearDown(self):
        self.tmp.cleanup()

    def test_udim_tiles_with_gaps(self):
        touch(self.dir, "rock_diffuse.1001.png", "rock_diffuse.1002.png", "rock_diffuse.1011.png")
        with self.assertRaisesRegex(SequenceError, "Missing frames"):   # plates still need every frame
            resolve_sequence(f"{self.posix}/rock_diffuse.%04d.png", self.cfg)
        seq = resolve_sequence(f"{self.posix}/rock_diffuse.%04d.png", self.cfg, allow_gaps=True)
        self.assertEqual((seq.frames, seq.skip, seq.cap, seq.frame_count), ([1001, 1002, 1011], 0, 0, 3))
        seq = resolve_sequence(f"{self.posix}/rock_diffuse.%04d.png 1002-1020", self.cfg, allow_gaps=True)
        self.assertEqual((seq.frames, seq.skip, seq.cap), ([1002, 1011], 1, 2))
        self.assertEqual(loaded_names(seq), ["rock_diffuse.1002.png", "rock_diffuse.1011.png"])

    def test_folder_means_the_texture_in_it(self):
        touch(self.dir, "rock_diffuse.1001.png", "rock_diffuse.1002.png", "notes.txt")
        self.assertEqual(texture_input(self.posix, self.cfg), f"{self.posix}/rock_diffuse.%04d.png")
        seq = resolve_input(self.posix + "/", self.cfg, ConvertConfig(), texture=True, ocio=self.oc)
        self.assertEqual((seq.source, seq.frames, seq.show), ("rock_diffuse", [1001, 1002], "xyz"))
        # Convention-named tiles: the UDIM varies, the frame stays.
        other = f"{self.root}/shows/xyz/tex2"
        touch(other, "acescg_map1_noTile_defState_color01.1001.0000.exr",
              "acescg_map1_noTile_defState_color01.1002.0000.exr")
        self.assertEqual(texture_input("/rdo/shows/xyz/tex2", self.cfg),
                         "/rdo/shows/xyz/tex2/acescg_map1_noTile_defState_color01.%04d.0000.exr")
        single = f"{self.root}/shows/xyz/photo"
        touch(single, "cliff.jpg")
        self.assertEqual(texture_input("/rdo/shows/xyz/photo 1-1", self.cfg), "/rdo/shows/xyz/photo/cliff.jpg 1-1")

    def test_folder_with_several_textures_refused(self):
        touch(self.dir, "rock_diffuse.1001.png", "rock_normal.1001.png", "moss.jpg")
        with self.assertRaisesRegex(SequenceError, "holds 3 different textures .*one job per texture"):
            texture_input(self.posix, self.cfg)
        touch(f"{self.root}/shows/xyz/empty", "readme.txt")
        with self.assertRaisesRegex(SequenceError, "No texture images"):
            texture_input("/rdo/shows/xyz/empty", self.cfg)

    def test_exr_textures_converted_like_plates(self):
        """Same conversion as every EXR: the repo config, linear -> color_picking."""
        touch(self.dir, "rock.1001.exr", "rock.1003.exr")
        seq = resolve_input(f"{self.posix}/rock.%04d.exr", self.cfg, ConvertConfig(tools={"oiiotool": "x"}),
                            texture=True, ocio=self.oc)
        c = seq.convert
        self.assertEqual((c["from"], c["to"], c["frames"]), ("scene_linear", "color_picking", [1001, 1003]))
        self.assertEqual(loaded_names(seq), ["rock.1001.exr", "rock.1003.exr"])
        cmd = exr_command(["oiiotool"], c, "in.exr", "out.png")
        self.assertEqual(cmd, ["oiiotool", "--colorconfig", self.oc.config, "in.exr", "--ch", "R,G,B", "--croptofull",
                               "--colorconvert", "scene_linear", "color_picking", "-d", "uint8", "-o", "out.png"])
        self.assertEqual(summary(seq, self.cfg)["to"], "color_picking")

    def test_movies_refused(self):
        with self.assertRaisesRegex(SequenceError, "not movies"):
            resolve_input("/rdo/shows/xyz/a.mov", self.cfg, ConvertConfig(), texture=True, ocio=self.oc)


class WriteMapsTests(unittest.TestCase):
    def test_raw_images_become_named_exrs(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw, out = f"{tmp}/scratch/j1/maps", f"{tmp}/out"
            os.makedirs(out)
            touch(raw, *[f"_cfj1_{c}_{i:04d}.png" for c in ("color01", "bmp02") for i in (1, 2, 3)], "other.png")
            t = {"raw_dir": raw, "name": T.DEFAULT_NAME, "ext": "png", "tokens": TOKENS,
                 "items": [[1001, 0], [1002, 0], [1011, 0]],
                 "maps": [dict(m, prefix=f"_cfj1_{m['channel']}") for m in MAPS], "ocio": JOB_OCIO}
            log = f"{tmp}/oiio.log"
            os.environ["FAKE_OIIOTOOL_LOG"] = log
            self.addCleanup(os.environ.pop, "FAKE_OIIOTOOL_LOG", None)
            seen = []
            res = asyncio.run(T.write_maps(t, out, TexturesConfig(), ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}),
                                           [PathMapping(posix="/rdo/x", windows=out)], lambda d, n: seen.append((d, n))))
            names = sorted(os.listdir(out))
            self.assertEqual(names, [f"acescg_map1_noTile_defState_{c}.{u}.0000.exr"
                                     for c in ("bmp02", "color01") for u in (1001, 1002, 1011)])
            self.assertEqual(seen[-1], (6, 6))
            self.assertEqual(res["maps"]["color01"]["pattern_posix"], "/rdo/x/acescg_map1_noTile_defState_color01.%04d.0000.exr")
            self.assertEqual((res["files"], res["frames"], res["first"], res["last"]), (6, 3, 1001, 1011))
            calls = [json.loads(line)["args"] for line in Path(log).read_text().splitlines()]
            color = next(a for a in calls if "_cfj1_color01_0002.png" in " ".join(a))
            # A Nuke Read set to "default (color_picking)": color_picking -> scene_linear.
            self.assertEqual(color[color.index("--colorconvert") + 1:color.index("--colorconvert") + 3],
                             ["color_picking", "scene_linear"])
            self.assertEqual(color[color.index("--colorconfig") + 1], "/farm/ocio/config.ocio")
            self.assertEqual(color[color.index("-d") + 1], "half")
            bump = next(a for a in calls if "_cfj1_bmp02_0001.png" in " ".join(a))
            self.assertNotIn("--colorconvert", bump)
            self.assertEqual((bump[bump.index("-d") + 1], bump[bump.index("--iscolorspace") + 1]), ("float", "data"))
            self.assertTrue(bump[bump.index("-o") + 1].endswith(".part.exr"))   # renamed once complete

    def test_missing_images_fail_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = f"{tmp}/maps"
            touch(raw, "_cfj1_color01_0001.png")
            t = {"raw_dir": raw, "name": T.DEFAULT_NAME, "ext": "png", "tokens": TOKENS, "items": [[1001, 0], [1002, 0]],
                 "maps": [dict(MAPS[0], prefix="_cfj1_color01")], "ocio": JOB_OCIO}
            with self.assertRaisesRegex(Exception, r"basecolor map \(color01\) came back as 1 image\(s\) for 2"):
                asyncio.run(T.write_maps(t, tmp, TexturesConfig(), ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}),
                                         [], lambda d, n: None))


class PreflightTests(unittest.TestCase):
    def test_ocio_check_runs_the_job_commands(self):
        from comfyfarm.__main__ import check_ocio
        from comfyfarm.config import Config
        with tempfile.TemporaryDirectory() as tmp:
            Path(f"{tmp}/config.ocio").write_text(OCIO_CONFIG)
            log = f"{tmp}/oiio.log"
            os.environ["FAKE_OIIOTOOL_LOG"] = log
            self.addCleanup(os.environ.pop, "FAKE_OIIOTOOL_LOG", None)
            oc = OcioConfig(config=f"{tmp}/config.ocio")
            cfg = Config(ocio=oc, convert=ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}))
            self.assertEqual(check_ocio(cfg), 0)
            calls = [json.loads(line)["args"] for line in Path(log).read_text().splitlines()]
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[1][calls[1].index("--colorconvert") + 1:][:2], ["color_picking", "scene_linear"])
            self.assertIn("--iscolorspace", calls[2])
            self.assertEqual(calls[3][calls[3].index("--colorconvert") + 1:][:2], ["scene_linear", "color_picking"])
            broken = Config(ocio=oc, convert=ConvertConfig(tools={"oiiotool": [sys.executable, "-c", "import sys; sys.exit(3)"]}))
            self.assertEqual(check_ocio(broken), 1)
            self.assertEqual(check_ocio(Config(ocio=OcioConfig(config=f"{tmp}/nope.ocio"))), 1)
            self.assertEqual(check_ocio(Config(ocio=OcioConfig(config=f"{tmp}/config.ocio", data="nope"),
                                               convert=ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}))),
                             1 if ocio_roles(f"{tmp}/config.ocio", ["data"]) else 0)

    @unittest.skipUnless(os.path.isfile(REPO / "ocio" / "config.ocio"), "ocio/config.ocio not in this checkout")
    def test_ocio_check_with_the_repo_config(self):
        from comfyfarm.__main__ import check_ocio
        from comfyfarm.config import Config
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FAKE_OIIOTOOL_LOG"] = f"{tmp}/oiio.log"
            self.addCleanup(os.environ.pop, "FAKE_OIIOTOOL_LOG", None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(check_ocio(Config(convert=ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}))), 0)
            self.assertIn("ocio/config.ocio", out.getvalue())
            if ocio_roles(str(REPO / "ocio" / "config.ocio"), ["scene_linear"]):   # PyOpenColorIO installed
                self.assertIn("scene_linear = rdo, color_picking = ACES-look, data = Utility - Raw", out.getvalue())


if __name__ == "__main__":
    unittest.main()
