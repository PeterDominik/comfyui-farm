import contextlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from comfyfarm import colordebug
from comfyfarm.config import Config, ConvertConfig, OcioConfig, PathMapping, PathsConfig

REPO = Path(__file__).resolve().parent.parent
CONFIG = """ocio_profile_version: 2
roles:
  scene_linear: ACES - ACEScg
  color_picking: Output - sRGB
colorspaces:
  - !<ColorSpace>
    name: ACES - ACEScg
  - !<ColorSpace>
    name: ACES - ACES2065-1
  - !<ColorSpace>
    name: Output - sRGB
  - !<ColorSpace>
    name: Utility - sRGB - Texture
"""


def png(grey: int, size: int = 8, chunks: list[tuple[bytes, bytes]] = ()) -> bytes:
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + bytes([grey]) * size for _ in range(size))
    extra = b"".join(chunk(t, d) for t, d in chunks)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)) + extra
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class ColorDebugTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        root = self.tmp.replace("\\", "/")
        os.makedirs(f"{root}/ocio")
        Path(f"{root}/ocio/config.ocio").write_text(CONFIG)
        plates = f"{root}/shows/raa/.published/bpash/bpash_1545/plate/v1"
        os.makedirs(plates)
        for f in (1001, 1002):
            Path(f"{plates}/bpash_1545.plate.v1.{f}.exr").write_bytes(b"x")
        Path(f"{root}/ref.1001.png").write_bytes(png(100))
        self.cfg = Config(paths=PathsConfig(mappings=[PathMapping(posix="/rdo/shows", windows=f"{root}/shows")]),
                          ocio=OcioConfig(config=f"{root}/ocio/config.ocio"),
                          convert=ConvertConfig(tools={"oiiotool": [sys.executable, str(REPO / "tests/fake_oiiotool.py")],
                                                       "ffmpeg": "", "ffprobe": ""}))
        self.out = f"{root}/out"
        # The job's settings come out lifted (140); ACES2065-1 input matches the reference (100).
        os.environ["FAKE_OIIOTOOL_LEVELS"] = json.dumps({"scene_linear|color_picking": 140,
                                                          "ACES - ACES2065-1|color_picking": 101,
                                                          "ACES - ACEScg|color_picking": 120,
                                                          "scene_linear|Utility - sRGB - Texture": 150})

    def tearDown(self):
        os.environ.pop("FAKE_OIIOTOOL_LEVELS", None)
        shutil.rmtree(self.tmp)

    def run_debug(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = colordebug.run(self.cfg, "/rdo/shows/raa/.published/bpash/bpash_1545/plate/v1/"
                                            "bpash_1545.plate.v1.%04d.exr", 1001,
                                  f"{self.tmp}/ref.1001.png", self.out, **kw)
        return code, buf.getvalue()

    def test_finds_the_matching_settings(self):
        code, text = self.run_debug()
        self.assertEqual(code, 0)
        self.assertIn("job uses:    from=scene_linear  to=color_picking", text)
        self.assertIn("median +40.0", text)                     # job settings: lifted by 40/255
        self.assertIn("from=scene_linear | to=Utility - sRGB - Texture", text)   # other outputs tried too
        self.assertIn("Best match:", text)
        self.assertIn("from=ACES - ACES2065-1 | to=color_picking", text.split("Best match:")[1].splitlines()[0])
        self.assertIn("ocio:\n    linear: 'ACES - ACES2065-1'", text)
        self.assertTrue(os.path.isfile(f"{self.out}/00_vs_reference.png"))
        self.assertTrue(os.path.isfile(f"{self.out}/report.txt"))

    def test_png_tags(self):
        p = f"{self.tmp}/tagged.png"
        Path(p).write_bytes(png(10, chunks=[(b"gAMA", struct.pack(">I", 100000)), (b"cICP", bytes([1, 8, 0, 1]))]))
        self.assertEqual(colordebug.png_color_tags(p), ["gAMA 1.00000", "cICP primaries=1 transfer=8 matrix=0"])
        self.assertEqual(colordebug.png_color_tags(f"{self.tmp}/ref.1001.png"), [])


if __name__ == "__main__":
    unittest.main()
