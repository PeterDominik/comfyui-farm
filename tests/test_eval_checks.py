"""Evals (decision 0053), the agent's part (agent/evals.py): its copies of the eval files
checked against the manager's, the eval's folder emptied (and nothing outside the evals
folder), and the checks on what the run wrote; with the standard-library PNG reader
(fileio/media.py) that compares PNGs where OpenImageIO's Python module isn't installed.

    python -m unittest tests.test_eval_checks -v
"""

import hashlib
import importlib.util
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from comfyfarm.agent import evals as EV
from comfyfarm.fileio.media import png_pixels

REPO = Path(__file__).resolve().parent.parent

OIIO = importlib.util.find_spec("OpenImageIO") is not None


def png(path: Path, w: int, h: int, samples: list[int], channels: int = 3, depth: int = 8, filters=(0,)) -> Path:
    """A PNG with each row filtered in turn by `filters` (0 none, 1 sub, 2 up, 3 average, 4 Paeth)."""
    nbytes = depth // 8
    bpp = channels * nbytes
    stride = w * bpp
    data = b"".join(struct.pack(">H" if depth == 16 else "B", v) for v in samples)
    rows, prev = [], bytes(stride)
    for y in range(h):
        line = data[y * stride:(y + 1) * stride]
        kind = filters[y % len(filters)]
        out = bytearray()
        for i, v in enumerate(line):
            a = line[i - bpp] if i >= bpp else 0
            b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
            if kind == 1:
                p = a
            elif kind == 2:
                p = b
            elif kind == 3:
                p = (a + b) // 2
            elif kind == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                p = a if pa <= pb and pa <= pc else b if pb <= pc else c
            else:
                p = 0
            out.append((v - p) & 255)
        rows.append(bytes([kind]) + bytes(out))
        prev = line
    colour = {1: 0, 2: 4, 3: 2, 4: 6}[channels]

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, colour, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
    return path


def ramp(w, h, channels=3, full=255):
    return [((x * 7 + y * 13 + c * 50) * (full // 255)) % (full + 1) for y in range(h) for x in range(w)
            for c in range(channels)]


class Pixels(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_every_filter_and_depth_reads_back(self):
        for channels in (1, 2, 3, 4):
            for depth in (8, 16):
                with self.subTest(channels=channels, depth=depth):
                    full = 255 if depth == 8 else 65535
                    want = ramp(9, 7, channels, full)
                    p = png(self.tmp / f"x{channels}{depth}.png", 9, 7, want, channels, depth, filters=(0, 1, 2, 3, 4))
                    w, h, ch, scale, got = png_pixels(str(p))
                    self.assertEqual((w, h, ch, scale, list(got)), (9, 7, channels, full, want))

    def test_what_it_doesnt_read(self):
        self.assertIsNone(png_pixels(str(REPO / "workflows/chord_pbr/process.yaml")))     # not a PNG
        p = png(self.tmp / "a.png", 2, 2, [0] * 12)
        b = bytearray(p.read_bytes())
        b[28] = 1                                                           # interlaced
        p.write_bytes(bytes(b))
        self.assertIsNone(png_pixels(str(p)))


class Before(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_its_eval_files_are_the_managers(self):
        f = self.tmp / "workflows/gpu_test/eval/input.png"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"plate")
        good = [[str(f), hashlib.sha256(b"plate").hexdigest()]]
        self.assertIsNone(EV.verify(good))
        self.assertEqual(EV.verify([[str(f), "0" * 64]]), "its checkout's gpu_test/eval/input.png isn't the manager's")
        why = EV.verify([[str(self.tmp / "gone.png"), "0" * 64]])
        self.assertTrue(why.endswith(f"{self.tmp.name}/gone.png isn't in its checkout"), why)
        self.assertIsNone(EV.verify([]))

    def test_its_folder_emptied_and_nothing_else(self):
        scratch = self.tmp / "scratch"
        folder = scratch / "evals" / "gpu_test"
        (folder / "old_v001").mkdir(parents=True)
        (folder / "old_v001" / "x.png").write_bytes(b"x")
        EV.clear(str(folder), str(scratch))
        self.assertFalse(folder.exists())
        EV.clear(str(folder), str(scratch))                               # not there: fine
        for outside in (scratch / "evals", scratch / "job1", self.tmp / "shows", scratch / "evals" / ".." / "x"):
            with self.subTest(folder=outside), self.assertRaisesRegex(ValueError, "isn't a folder in this machine"):
                EV.clear(str(outside), str(scratch))


class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ref = png(self.tmp / "expected.png", 16, 8, ramp(16, 8))

    def wrote(self, *names, like=None):
        out = []
        for n in names:
            p = self.tmp / "out" / n
            p.parent.mkdir(exist_ok=True)
            if n.endswith(".png"):
                png(p, 16, 8, like if like is not None else ramp(16, 8), filters=(1, 4))
            else:
                p.write_text("{}")
            out.append(str(p))
        return out

    def test_files_size_and_like_pass(self):
        files = self.wrote("plate.gputest_v001.0001.png", "plate.gputest_v001.json", "plate.gputest_v001_preview.mp4")
        r = EV.check({"files": {"png": 1, "json": 1}, "size": [16, 8],
                      "like": [{"output": "*.png", "expected": str(self.ref), "max_error": 0.002}]}, files, oiio=False)
        self.assertEqual(r["ok"], True, r)
        self.assertEqual([(c["check"], c["said"]) for c in r["checks"]], [
            ("files .png", "1 written, 1 wanted"), ("files .json", "1 written, 1 wanted"),
            ("size", "1 image(s), each 16 x 8"),
            ("like *.png", "plate.gputest_v001.0001.png against expected.png: mean error 0.0000, at most 0.002")])

    def test_what_fails_and_how_it_says_so(self):
        off = [min(255, v + 51) for v in ramp(16, 8)]                       # about 0.2 brighter
        files = self.wrote("a.0001.png", "a.0002.png", like=off)
        r = EV.check({"files": {"exr": 5}, "size": [32, 32],
                      "like": [{"output": "a.0001.png", "expected": str(self.ref), "max_error": 0.01},
                               {"output": "*.png", "expected": str(self.ref), "max_error": 0.01}]}, files, oiio=False)
        self.assertFalse(r["ok"])
        said = {c["check"]: c["said"] for c in r["checks"]}
        self.assertEqual(said["files .exr"], "0 written, 5 wanted")
        self.assertEqual(said["size"], "a.0001.png is 16 x 8, wanted 32 x 32 (and 1 more)")
        self.assertRegex(said["like a.0001.png"],
                         r"^a\.0001\.png against expected\.png: mean error 0\.1\d+, at most 0\.01$")
        self.assertEqual(said["like *.png"], "2 files it wrote match *.png: it needs to match one")
        text = EV.failure(r)
        self.assertTrue(text.startswith("The eval failed: files .exr: 0 written, 5 wanted; size: a.0001.png is 16 x 8"),
                        text)
        self.assertNotIn("Passed:", text)
        r = EV.check({"files": {"png": 2}, "size": [32, 32]}, files)
        self.assertIn("Passed: files .png: 2 written, 2 wanted.", EV.failure(r))

    def test_images_it_cant_compare(self):
        small = png(self.tmp / "small.png", 4, 4, ramp(4, 4))
        grey = png(self.tmp / "grey.png", 16, 8, ramp(16, 8, 1), channels=1)
        self.assertEqual(EV.mean_error(str(small), str(self.ref), oiio=False),
                         (None, "it is 4 x 4, the reference 16 x 8"))
        self.assertEqual(EV.mean_error(str(grey), str(self.ref), oiio=False),
                         (None, "one is grey and the other colour"))
        exr = self.tmp / "a.exr"
        exr.write_bytes(b"v/1\x01")
        err, why = EV.mean_error(str(exr), str(self.ref), oiio=False)
        self.assertIsNone(err)
        self.assertIn("OpenImageIO's Python module isn't installed on this machine", why)
        rgb = ramp(16, 8)
        rgba = png(self.tmp / "rgba.png", 16, 8, [v for i in range(16 * 8) for v in (*rgb[i * 3:i * 3 + 3], 255)],
                   channels=4)
        self.assertEqual(EV.mean_error(str(rgba), str(self.ref), oiio=False), (0.0, None))   # the channels both have
        r = EV.check({"size": [16, 8]}, [str(self.tmp / "out.json")])
        self.assertEqual(r["checks"][0]["said"], "it wrote no image")

    @unittest.skipUnless(OIIO, "OpenImageIO's Python module isn't installed")
    def test_openimageio_measures_the_same(self):
        off = png(self.tmp / "off.png", 16, 8, [min(255, v + 51) for v in ramp(16, 8)])
        by_oiio, _ = EV.mean_error(str(off), str(self.ref))
        by_hand, _ = EV.mean_error(str(off), str(self.ref), oiio=False)
        self.assertAlmostEqual(by_oiio, by_hand, places=4)
        self.assertEqual(EV.mean_error(str(self.ref), str(self.ref)), (0.0, None))

    def test_the_farms_gpu_test_reference_is_its_input_inverted(self):
        e = REPO / "workflows/gpu_test/eval"
        w, h, ch, full, a = png_pixels(str(e / "input.png"))
        _, _, _, _, b = png_pixels(str(e / "expected.png"))
        self.assertEqual((w, h, ch), (64, 48, 3))
        self.assertTrue(all(x + y == full for x, y in zip(a, b)))


if __name__ == "__main__":
    unittest.main()
