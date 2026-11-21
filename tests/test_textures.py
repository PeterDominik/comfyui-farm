"""Texture workflows: TEX naming, UDIM inputs, folder inputs, and the EXR writing step.

    python -m unittest tests.test_textures -v
"""

import asyncio
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from comfyfarm.config import ConvertConfig, OcioConfig, PathMapping, PathsConfig, TexturesConfig
from comfyfarm.fileio import convert as C
from comfyfarm.fileio import inputs as I
from comfyfarm.fileio import media as M
from comfyfarm.fileio import outputs as Out
from comfyfarm.fileio import scratch as S
from comfyfarm.fileio import textures as T
from comfyfarm.fileio.convert import exr_command
from comfyfarm.fileio.errors import SequenceError
from comfyfarm.fileio.inputs import (Sequence, loaded_names, resolve_input, resolve_sequence, summary, texture_input,
                                     texture_numbers)
from comfyfarm.fileio.ocio import ocio_roles
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
# All five, as workflows/chord_pbr/process.yaml has them.
CHORD_MAPS = MAPS + [{"name": n, "channel": c, "colorspace": "acescg", "color": False, "bits": "half"}
                     for n, c in (("normal", "nrm02"), ("roughness", "spcr01"), ("metalness", "metal01"))]
TOKENS = {"uvset": "map1", "tile": "noTile", "state": "defState", "description": ""}
OCIO_CONFIG = ("ocio_profile_version: 2\nroles:\n  default: ACES - ACEScg\n  scene_linear: ACES - ACEScg\n"
               "  color_picking: Output - sRGB\n  data: Raw\n"
               "displays:\n  sRGB:\n    - !<View> {name: Raw, colorspace: Raw}\n"
               "colorspaces:\n  - !<ColorSpace>\n    name: ACES - ACEScg\n  - !<ColorSpace>\n    name: Output - sRGB\n"
               "  - !<ColorSpace>\n    name: Raw\n    isdata: true\n")
# A job's frozen OCIO settings (convert.ocio_setup) with the default names.
JOB_OCIO = {"config": "/farm/ocio/config.ocio", "linear": "scene_linear", "image": "color_picking", "data": "data"}


def png_bytes(color_type: int, size: tuple[int, int] = (1, 1)) -> bytes:
    """A 1x1 8-bit PNG of the given colour type (0 grey, 2 RGB, 4 grey+alpha, 6 RGBA).
    `size` only changes the header (all the farm reads of an input); the pixels stay one."""
    import struct
    import zlib
    n = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", *size, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x80" * n)) + chunk(b"IEND", b""))


def exr_header(channels: list[str], window: tuple[int, int, int, int] | None = None, padding: int = 0) -> bytes:
    """The start of an OpenEXR file: magic, version and a header with these channels, and
    a displayWindow (xmin, ymin, xmax, ymax) if given. `padding` puts a string attribute
    that long before the others, for headers longer than the first read."""
    import struct

    def attr(name, kind, value):
        return name.encode() + b"\0" + kind.encode() + b"\0" + struct.pack("<i", len(value)) + value
    chlist = b"".join(c.encode() + b"\0" + struct.pack("<iB3xii", 1, 0, 1, 1) for c in channels) + b"\0"
    return (b"\x76\x2f\x31\x01" + struct.pack("<I", 2)
            + (attr("comments", "string", b"x" * padding) if padding else b"")
            + attr("compression", "compression", b"\x03") + attr("channels", "chlist", chlist)
            + (attr("displayWindow", "box2i", struct.pack("<iiii", *window)) if window else b"") + b"\0")


def exr_sized(w: int, h: int, channels=("B", "G", "R")) -> bytes:
    return exr_header(list(channels), (0, 0, w - 1, h - 1))


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
        # Maps by channel, as the manager plans them: which node saves each is this copy's to say.
        tex = {"raw_dir": "C:/farm/scratch/j1/maps",
               "maps": [{"channel": m.channel, "prefix": f"_cfj1_{m.channel}"} for m in self.wf.textures.maps]}
        before = copy.deepcopy(self.wf.template)
        p = self.wf.build(v, seq, "//srv/out/rock.chord_v001", textures=tex)
        self.assertEqual(self.wf.template, before)
        # A request planned before that also named the nodes: the same graph.
        old = dict(tex, maps=[dict(mp, node=m.node) for mp, m in zip(tex["maps"], self.wf.textures.maps)])
        self.assertEqual(self.wf.build(v, seq, "//srv/out/rock.chord_v001", textures=old), p)
        for m in self.wf.textures.maps:
            self.assertEqual(p[m.node]["inputs"]["output_path"], "C:/farm/scratch/j1/maps")
            self.assertEqual(p[m.node]["inputs"]["filename_prefix"], f"_cfj1_{m.channel}")
        self.assertEqual(p["17"]["inputs"], {"path": "//srv/t/rock.%04d.png", "frames": "1001-1002"})
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
                               "--colorconvert", "scene_linear", "color_picking", "-d", "uint8", "--compression", "none",
                               "-o", "out.png"])
        self.assertEqual(summary(seq, self.cfg)["to"], "color_picking")

    def test_movies_refused(self):
        with self.assertRaisesRegex(SequenceError, "not movies"):
            resolve_input("/rdo/shows/xyz/a.mov", self.cfg, ConvertConfig(), texture=True, ocio=self.oc)


class ChannelTests(unittest.TestCase):
    """oiiotool fills channels an image doesn't have with 0 (and only warns), so greyscale
    images are copied into R, G, B instead of asked for channels they don't have."""

    def write(self, name, data):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = f"{d}/{name}"
        Path(path).write_bytes(data)
        return path

    def test_png_channels(self):
        for ctype, names, ch in ((0, ["Y"], "R=Y,G=Y,B=Y"), (4, ["Y", "A"], "R=Y,G=Y,B=Y"),
                                 (2, ["R", "G", "B"], "R,G,B"), (6, ["R", "G", "B", "A"], "R,G,B"),
                                 (3, ["R", "G", "B"], "R,G,B")):
            path = self.write(f"map{ctype}.png", png_bytes(ctype))
            self.assertEqual((M.image_channels(path), M.channel_args(path)), (names, ch), ctype)

    def test_exr_channels(self):
        cases = ((["Y"], "R=Y,G=Y,B=Y"), (["B", "G", "R"], "R,G,B"), (["A", "B", "G", "R", "depth.Z"], "R,G,B"),
                 (["BY", "RY", "Y"], "R,G,B"), (["height"], "R=height,G=height,B=height"))
        for names, ch in cases:
            path = self.write("t.exr", exr_header(names))
            self.assertEqual((M.image_channels(path), M.channel_args(path)), (names, ch), names)
        layered = self.write("t.exr", exr_header(["diffuse.B", "diffuse.G", "diffuse.R"]))
        with self.assertRaisesRegex(C.ConvertError, "t.exr has no R, G, B or single grey channel .*diffuse.B"):
            M.channel_args(layered)

    def test_unreadable_files_keep_rgb(self):
        self.assertEqual(M.channel_args(self.write("empty.png", b"")), "R,G,B")
        self.assertEqual(M.channel_args(self.write("odd.tif", b"II*\0")), "R,G,B")
        self.assertEqual(M.channel_args("/nope/missing.exr"), "R,G,B")

    def test_exr_command_uses_the_channels(self):
        c = {"config": "c.ocio", "extra_args": ["--ch", "R,G,B", "--croptofull"], "from": "a", "to": "b"}
        cmd = exr_command(["oiiotool"], c, "in.exr", "out.png", "R=Y,G=Y,B=Y")
        self.assertEqual(cmd[cmd.index("--ch") + 1:cmd.index("--ch") + 3], ["R=Y,G=Y,B=Y", "--croptofull"])
        self.assertEqual(c["extra_args"][1], "R,G,B")                        # the job's settings aren't changed
        self.assertEqual(exr_command(["oiiotool"], dict(c, extra_args=["--croptofull"]), "a", "b", "R=Y,G=Y,B=Y"),
                         ["oiiotool", "--colorconfig", "c.ocio", "a", "--croptofull", "--colorconvert", "a", "b",
                          "-d", "uint8", "--compression", "none", "-o", "b"])   # no --ch configured: none added

    def test_greyscale_exr_input(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        os.makedirs(f"{d}/src")
        for f in (1001, 1002):
            Path(f"{d}/src/grey.{f}.exr").write_bytes(exr_header(["Y"]))
        c = {"kind": "exr", "src_dir": f"{d}/src/", "src_pattern": "grey.%04d.exr", "first": 1001, "last": 1002,
             "frames": [1001, 1002], "dst_dir": f"{d}/out/", "dst_pattern": "grey.%04d.png", "config": "c.ocio",
             "extra_args": ["--ch", "R,G,B", "--croptofull"], "from": "scene_linear", "to": "color_picking"}
        os.makedirs(f"{d}/out")
        log = f"{d}/oiio.log"
        os.environ["FAKE_OIIOTOOL_LOG"] = log
        self.addCleanup(os.environ.pop, "FAKE_OIIOTOOL_LOG", None)
        asyncio.run(C.convert_exr(c, ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}), lambda d_, n: None))
        calls = [json.loads(line)["args"] for line in Path(log).read_text().splitlines()]
        self.assertEqual([a[a.index("--ch") + 1] for a in calls], ["R=Y,G=Y,B=Y"] * 2)
        Path(f"{d}/src/grey.1001.exr").write_bytes(exr_header(["diffuse.R", "diffuse.G", "diffuse.B"]))
        with self.assertRaisesRegex(C.ConvertError, "grey.1001.exr has no R, G, B"):
            asyncio.run(C.convert_exr(c, ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}), lambda d_, n: None))


class WorkResolutionTests(unittest.TestCase):
    """work_resolution (CHORD: 1024): larger inputs are scaled down before the workflow,
    keeping their aspect; inputs that fit are used as they are."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name.replace("\\", "/")
        self.dir = f"{self.root}/shows/xyz/_asset/rockA/cg/textures/src"
        self.posix = "/rdo/shows/xyz/_asset/rockA/cg/textures/src"
        os.makedirs(self.dir)
        self.cfg = PathsConfig(mappings=[PathMapping(posix="/rdo/shows", windows=f"{self.root}/shows")])
        touch(f"{self.root}/ocio")
        Path(f"{self.root}/ocio/config.ocio").write_text(OCIO_CONFIG)
        self.oc = OcioConfig(config=f"{self.root}/ocio/config.ocio")
        self.cc = ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL})

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, data):
        Path(f"{self.dir}/{name}").write_bytes(data)

    def resolve(self, name, limit=1024):
        return resolve_input(f"{self.posix}/{name}", self.cfg, self.cc, texture=True, ocio=self.oc,
                             work_resolution=limit)

    def run_convert(self, seq):
        S.point_loader_at_scratch(seq, f"{self.root}/scratch", "job1")
        log = Path(f"{self.root}/oiio.log")
        log.unlink(missing_ok=True)
        os.environ["FAKE_OIIOTOOL_LOG"] = str(log)
        self.addCleanup(os.environ.pop, "FAKE_OIIOTOOL_LOG", None)
        asyncio.run(C.convert(seq.as_dict(), self.cc, lambda d_, n: None))
        return [json.loads(line)["args"] for line in log.read_text().splitlines()]

    def test_image_size(self):
        self.put("a.png", png_bytes(2, (4096, 2048)))
        self.put("a.exr", exr_sized(4096, 4096))
        self.put("offset.exr", exr_header(["R", "G", "B"], (-10, -10, 1013, 1013)))
        self.put("nowindow.exr", exr_header(["R", "G", "B"]))
        self.put("long.exr", exr_header(["R", "G", "B"], (0, 0, 799, 599), padding=200_000))   # past the first 64 KB
        self.put("empty.png", b"")
        sizes = {n: M.image_size(f"{self.dir}/{n}") for n in os.listdir(self.dir)}
        self.assertEqual(sizes, {"a.png": (4096, 2048), "a.exr": (4096, 4096), "offset.exr": (1024, 1024),
                                 "nowindow.exr": None, "long.exr": (800, 600), "empty.png": None})
        self.assertEqual(M.image_channels(f"{self.dir}/long.exr"), ["R", "G", "B"])
        self.assertIsNone(M.image_size(f"{self.dir}/missing.png"))

    def test_fit_and_resize_args(self):
        cases = {(4096, 4096): ((1024, 1024), ["--resize", "1024x0"]),
                 (4096, 2048): ((1024, 512), ["--resize", "1024x0"]),
                 (2048, 4096): ((512, 1024), ["--resize", "0x1024"]),
                 (1024, 1024): ((1024, 1024), []), (800, 600): ((800, 600), [])}
        for size, (fit, args) in cases.items():
            self.assertEqual((M.fit_size(size, 1024), C.resize_args(size, 1024)), (fit, args), size)
        self.assertEqual((C.resize_args(None, 1024), C.resize_args((4096, 4096), None)), ([], []))
        self.assertEqual(I.scaled_text([(4096, 4096)], 1024), "4096×4096 → 1024×1024")
        self.assertEqual(I.scaled_text([(4096, 4096), (1024, 1024), (2048, 1024)], 1024),
                         "4096×4096 → 1024×1024 (2 of 3 images)")
        self.assertIsNone(I.scaled_text([(800, 800), None], 1024))

    def test_large_png_scaled_into_scratch(self):
        self.put("cliff.png", png_bytes(2, (4096, 4096)))
        seq = self.resolve("cliff.png")
        c = seq.convert
        self.assertEqual((seq.kind, c["kind"], c["names"], c["frames"], c["scaled"]),
                         ("image", "image", ["cliff.png"], [1], "4096×4096 → 1024×1024"))
        self.assertEqual(loaded_names(seq), ["cliff.png"])           # the maps are still named after it
        self.assertEqual(summary(seq, self.cfg), {"kind": "image", "work_resolution": 1024,
                                                  "scaled": "4096×4096 → 1024×1024"})
        calls = self.run_convert(seq)
        self.assertEqual((seq.load_dir, seq.skip, seq.cap), (f"{self.root}/scratch/job1/", 0, 0))
        src, dst = f"{self.dir}/cliff.png", f"{self.root}/scratch/job1/cliff.0001.png"
        self.assertEqual(calls, [["--iconfig", "oiio:UnassociatedAlpha", "1", src, "--reorient",
                                  "--resize", "1024x0", "-d", "uint8", "--compression", "none", "-o", dst]])
        self.assertEqual(os.listdir(f"{self.root}/scratch/job1"), ["cliff.0001.png"])
        self.assertEqual(M.image_size(dst), (1024, 1024))            # the fake oiiotool honours --resize

    def test_small_or_unknown_inputs_read_as_they_are(self):
        self.put("small.png", png_bytes(2, (800, 800)))
        self.put("exact.png", png_bytes(2, (1024, 512)))
        self.put("odd.jpg", b"\xff\xd8 not really a jpg")
        for name in ("small.png", "exact.png", "odd.jpg"):
            seq = self.resolve(name)
            self.assertEqual((seq.kind, seq.convert), ("frames", None), name)
        self.put("big.png", png_bytes(2, (4096, 4096)))
        seq = self.resolve("big.png", limit=None)                    # a workflow without work_resolution
        self.assertEqual((seq.kind, seq.convert), ("frames", None))

    def test_udim_tiles_scaled_each_to_fit(self):
        self.put("rock_diffuse.1001.png", png_bytes(2, (4096, 4096)))
        self.put("rock_diffuse.1002.png", png_bytes(2, (1024, 1024)))
        self.put("rock_diffuse.1011.png", png_bytes(2, (2048, 1024)))
        seq = self.resolve("rock_diffuse.%04d.png")
        c = seq.convert
        self.assertEqual((seq.kind, c["frames"], c["scaled"]), ("image", [1001, 1002, 1011],
                                                                "4096×4096 → 1024×1024 (2 of 3 images)"))
        self.assertEqual(loaded_names(seq), ["rock_diffuse.1001.png", "rock_diffuse.1002.png", "rock_diffuse.1011.png"])
        calls = self.run_convert(seq)
        resize = {a[a.index("-o") + 1].rsplit("/", 1)[1]: a[a.index("--reorient") + 1:a.index("-d")] for a in calls}
        self.assertEqual(resize, {"rock_diffuse.1001.png": ["--resize", "1024x0"], "rock_diffuse.1002.png": [],
                                  "rock_diffuse.1011.png": ["--resize", "1024x0"]})
        self.assertEqual(M.image_size(f"{self.root}/scratch/job1/rock_diffuse.1011.png"), (1024, 512))
        # The tiles keep their UDIMs, so the maps do too.
        items, has_udim = T.items_for(loaded_names(seq), "udim")
        self.assertEqual(([i[0] for i in items], has_udim), ([1001, 1002, 1011], True))

    def test_exr_scaled_in_linear_before_the_colour_conversion(self):
        self.put("rock.1001.exr", exr_sized(4096, 4096))
        self.put("rock.1002.exr", exr_sized(512, 512))
        seq = self.resolve("rock.%04d.exr")
        c = seq.convert
        self.assertEqual((seq.kind, c["work_resolution"], c["scaled"]),
                         ("exr", 1024, "4096×4096 → 1024×1024 (1 of 2 images)"))
        s = summary(seq, self.cfg)
        self.assertEqual((s["to"], s["work_resolution"], s["scaled"]), ("color_picking", 1024, c["scaled"]))
        calls = self.run_convert(seq)
        big = next(a for a in calls if a[a.index("-o") + 1].endswith("rock.1001.png"))
        self.assertEqual(big[big.index("--ch"):big.index("-d")],
                         ["--ch", "R,G,B", "--croptofull", "--resize", "1024x0",
                          "--colorconvert", "scene_linear", "color_picking"])
        small = next(a for a in calls if a[a.index("-o") + 1].endswith("rock.1002.png"))
        self.assertNotIn("--resize", small)
        # Without work_resolution: no sizes read, no resize, as before.
        seq = self.resolve("rock.%04d.exr", limit=None)
        self.assertEqual((seq.convert["work_resolution"], seq.convert["scaled"]), (None, None))
        self.assertFalse([a for a in self.run_convert(seq) if "--resize" in a])
        old = dict(c)
        del old["work_resolution"]                          # a job queued before the setting existed
        self.assertNotIn("--resize", exr_command(["oiiotool"], old, "a.exr", "a.png"))

    def test_files_gone_before_converting(self):
        self.put("cliff.png", png_bytes(2, (4096, 4096)))
        seq = self.resolve("cliff.png")
        os.remove(f"{self.dir}/cliff.png")
        with self.assertRaisesRegex(C.ConvertError, "Files disappeared since submitting: cliff.png"):
            self.run_convert(seq)

    def test_manifest(self):
        wf = Workflow(WF)
        self.assertEqual((wf.work_resolution, wf.problems), (1024, []))
        self.assertEqual(wf.schema()["work_resolution"], 1024)
        for bad in ("1024", 32, True, 1.5):
            wf.work_resolution = bad
            self.assertTrue([p for p in wf.static_check() if p.startswith("work_resolution:")], bad)


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
            res = asyncio.run(Out.write_maps(t, out, TexturesConfig(), ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}),
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

    def chord_maps(self, tmp):
        """CHORD's saves as they really are: RGB base colour and normal, greyscale height,
        roughness and metalness."""
        raw = f"{tmp}/scratch/j1/maps"
        os.makedirs(raw)
        for m in CHORD_MAPS:
            grey = m["channel"] not in ("color01", "nrm02")
            Path(f"{raw}/_cfj1_{m['channel']}_0001.png").write_bytes(png_bytes(0 if grey else 2))
        return {"raw_dir": raw, "name": T.DEFAULT_NAME, "ext": "png", "tokens": TOKENS, "items": [[1001, 0]],
                "maps": [dict(m, prefix=f"_cfj1_{m['channel']}") for m in CHORD_MAPS], "ocio": JOB_OCIO}

    def test_greyscale_maps_are_copied_into_rgb(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = self.chord_maps(tmp)
            log = f"{tmp}/oiio.log"
            os.environ["FAKE_OIIOTOOL_LOG"] = log
            self.addCleanup(os.environ.pop, "FAKE_OIIOTOOL_LOG", None)
            asyncio.run(Out.write_maps(t, tmp, TexturesConfig(), ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}),
                                     [], lambda d, n: None))
            calls = [json.loads(line) for line in Path(log).read_text().splitlines()]
            ch = {next(m["channel"] for m in CHORD_MAPS if f"_{m['channel']}_" in " ".join(c["args"])):
                  c["args"][c["args"].index("--ch") + 1] for c in calls}
            self.assertEqual(ch, {"color01": "R,G,B", "nrm02": "R,G,B", "bmp02": "R=Y,G=Y,B=Y",
                                  "spcr01": "R=Y,G=Y,B=Y", "metal01": "R=Y,G=Y,B=Y"})
            self.assertEqual([c["warnings"] for c in calls], [[]] * len(CHORD_MAPS))

    def test_missing_channels_fail_instead_of_black_maps(self):
        # If a map ever reaches oiiotool asking for channels it doesn't have, the job fails
        # rather than writing an all-black EXR.
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            t = self.chord_maps(tmp)
            with mock.patch.object(M, "channel_args", return_value="R,G,B"), \
                    self.assertRaisesRegex(Exception, "_cfj1_.*_0001.png is missing channels, so the map would be black"):
                asyncio.run(Out.write_maps(t, tmp, TexturesConfig(), ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}),
                                         [], lambda d, n: None))

    def test_missing_images_fail_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = f"{tmp}/maps"
            touch(raw, "_cfj1_color01_0001.png")
            t = {"raw_dir": raw, "name": T.DEFAULT_NAME, "ext": "png", "tokens": TOKENS, "items": [[1001, 0], [1002, 0]],
                 "maps": [dict(MAPS[0], prefix="_cfj1_color01")], "ocio": JOB_OCIO}
            with self.assertRaisesRegex(Exception, r"basecolor map \(color01\) came back as 1 image\(s\) for 2"):
                asyncio.run(Out.write_maps(t, tmp, TexturesConfig(), ConvertConfig(tools={"oiiotool": FAKE_OIIOTOOL}),
                                         [], lambda d, n: None))


class PreflightTests(unittest.TestCase):
    def check(self, oc=None, oiiotool=None):
        from comfyfarm.cli import check_ocio
        from comfyfarm.config import Config
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_ocio(Config(ocio=oc or OcioConfig(),
                                     convert=ConvertConfig(tools={"oiiotool": oiiotool or FAKE_OIIOTOOL})))
        return code, out.getvalue()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.log = f"{self.tmp}/oiio.log"
        os.environ["FAKE_OIIOTOOL_LOG"] = self.log
        self.addCleanup(os.environ.pop, "FAKE_OIIOTOOL_LOG", None)
        self.ocio_lib = bool(ocio_roles(str(REPO / "ocio" / "config.ocio"), ["scene_linear"]))   # PyOpenColorIO

    def test_runs_the_job_commands_with_the_repo_config(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("ocio/config.ocio", out)
        if self.ocio_lib:
            self.assertIn("scene_linear = acescg, color_picking = comfy_png, data = raw): matches Nuke", out)
        calls = [json.loads(line)["args"] for line in Path(self.log).read_text().splitlines()]
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1][calls[1].index("--colorconvert") + 1:][:2], ["color_picking", "scene_linear"])
        self.assertIn("--iscolorspace", calls[2])
        self.assertEqual(calls[3][calls[3].index("--colorconvert") + 1:][:2], ["scene_linear", "color_picking"])

    def test_failures(self):
        self.assertEqual(self.check(oiiotool=[sys.executable, "-c", "import sys; sys.exit(3)"])[0], 1)
        self.assertEqual(self.check(OcioConfig(config=f"{self.tmp}/nope.ocio"))[0], 1)
        # A config without the data role, and one whose transforms don't match Nuke's values;
        # only PyOpenColorIO can tell.
        Path(f"{self.tmp}/no_data.ocio").write_text(OCIO_CONFIG.replace("  data: Raw\n", ""))
        code, out = self.check(OcioConfig(config=f"{self.tmp}/no_data.ocio"))
        self.assertEqual(code, 1 if self.ocio_lib else 0, out)
        Path(f"{self.tmp}/other.ocio").write_text(OCIO_CONFIG)
        code, out = self.check(OcioConfig(config=f"{self.tmp}/other.ocio"))
        self.assertEqual(code, 1 if self.ocio_lib else 0, out)
        if self.ocio_lib:
            self.assertIn("no longer matches Nuke: linear grey -> PNG code 0.02: 5 (Nuke 23)", out)


if __name__ == "__main__":
    unittest.main()
