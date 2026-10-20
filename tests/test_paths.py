import os
import tempfile
import unittest

from comfyfarm.config import PathMapping, PathsConfig
from comfyfarm.paths import (SequenceError, default_output_root_display, frame_pattern, output_dir,
                             output_root, parse_input, resolve_output_root, resolve_sequence, to_farm,
                             to_posix)

MAP = [PathMapping(posix="/rdo/shows", windows="//rodeofx.com/smb/shows")]


def touch(d, *names):
    for n in names:
        open(os.path.join(d, n), "wb").close()


class MappingTests(unittest.TestCase):
    def test_posix_to_farm_and_back(self):
        p = "/rdo/shows/xyz/.published/abc/abc_0010/x.%04d.png"
        farm = to_farm(p, MAP)
        self.assertEqual(farm, "//rodeofx.com/smb/shows/xyz/.published/abc/abc_0010/x.%04d.png")
        self.assertEqual(to_posix(farm, MAP), p)

    def test_backslash_unc_and_case(self):
        self.assertEqual(to_posix(r"\\RODEOFX.COM\smb\shows\xyz\a.png", MAP), "/rdo/shows/xyz/a.png")

    def test_prefix_boundary(self):
        self.assertEqual(to_farm("/rdo/showsX/a", MAP), "/rdo/showsX/a")

    def test_parse_range(self):
        self.assertEqual(parse_input("/a/b.%d.png 1001-1158"), ("/a/b.%d.png", 1001, 1158))
        self.assertEqual(parse_input("/a/b.####.png"), ("/a/b.####.png", None, None))
        with self.assertRaises(SequenceError):
            parse_input("/a/b.%d.png 20-10")

    def test_frame_pattern(self):
        self.assertEqual(frame_pattern("{shot}_v{version:03d}.{frame:04d}.{ext}"), "{shot}_v{version:03d}.%04d.{ext}")


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name.replace("\\", "/")
        self.shot_dir = f"{self.root}/shows/xyz/.published/abc/abc_0010/hi"
        os.makedirs(self.shot_dir)
        self.cfg = PathsConfig(mappings=[PathMapping(posix="/rdo/shows", windows=f"{self.root}/shows")],
                               output_root=self.root + "/shows/{show}/renders")

    def tearDown(self):
        self.tmp.cleanup()

    def frames(self, a, b, pat="abc_0010.pre.v1.{:04d}.png"):
        touch(self.shot_dir, *[pat.format(f) for f in range(a, b + 1)])

    def test_full_folder_range(self):
        self.frames(1001, 1010)
        s = resolve_sequence("/rdo/shows/xyz/.published/abc/abc_0010/hi/abc_0010.pre.v1.%d.png 1001-1010", self.cfg)
        self.assertEqual((s.show, s.shot, s.skip, s.cap, s.frame_count, s.first), ("xyz", "abc_0010", 0, 0, 10, 1001))
        self.assertTrue(s.directory.endswith("/hi/"))

    def test_subrange_skip_cap(self):
        self.frames(1001, 1020)
        s = resolve_sequence(f"{self.shot_dir}/abc_0010.pre.v1.####.png 1005-1010", self.cfg)
        self.assertEqual((s.skip, s.cap, s.frame_count), (4, 6, 6))

    def test_no_range_uses_whole_sequence_and_ignores_sorted_before(self):
        touch(self.shot_dir, "aaa_thumb.png")  # sorts before the frames
        self.frames(1001, 1005)
        s = resolve_sequence(f"{self.shot_dir}/abc_0010.pre.v1.%04d.png", self.cfg)
        self.assertEqual((s.skip, s.cap, s.first, s.last), (1, 5, 1001, 1005))

    def test_missing_frames_refused(self):
        self.frames(1001, 1005)
        os.remove(f"{self.shot_dir}/abc_0010.pre.v1.1003.png")
        with self.assertRaisesRegex(SequenceError, "Missing frames.*1003"):
            resolve_sequence(f"{self.shot_dir}/abc_0010.pre.v1.%04d.png 1001-1005", self.cfg)

    def test_interleaved_foreign_images_refused(self):
        self.frames(1001, 1005)
        touch(self.shot_dir, "abc_0010.pre.v1.1002_alt.png")   # sorts between 1002 and 1003
        with self.assertRaisesRegex(SequenceError, "sort between"):
            resolve_sequence(f"{self.shot_dir}/abc_0010.pre.v1.%04d.png 1001-1005", self.cfg)

    def test_exr_refused(self):
        with self.assertRaisesRegex(SequenceError, "can't read '.exr'"):
            resolve_sequence(f"{self.shot_dir}/abc_0010.%04d.exr 1-2", self.cfg)

    def test_missing_folder(self):
        with self.assertRaisesRegex(SequenceError, "can't open the input folder"):
            resolve_sequence(f"{self.root}/nope/a.%04d.png 1-2", self.cfg)

    def test_picked_output_folder(self):
        os.makedirs(f"{self.root}/shows/xyz/my_renders")
        self.assertEqual(resolve_output_root("/rdo/shows/xyz/my_renders/", self.cfg),
                         f"{self.root}/shows/xyz/my_renders")
        with self.assertRaisesRegex(SequenceError, "doesn't exist"):
            resolve_output_root("/rdo/shows/xyz/typo_renders", self.cfg)
        with self.assertRaisesRegex(SequenceError, "must be under /rdo/shows"):
            resolve_output_root("/home/peter/renders", self.cfg)
        with self.assertRaisesRegex(SequenceError, "must be under"):
            resolve_output_root("C:/Windows/Temp", self.cfg)
        with self.assertRaisesRegex(SequenceError, r"'\.\.'"):
            resolve_output_root("/rdo/shows/xyz/../../etc", self.cfg)
        with self.assertRaisesRegex(SequenceError, "not a frame pattern"):
            resolve_output_root("/rdo/shows/xyz/my_renders/a.%04d.png 1-5", self.cfg)
        self.cfg.allowed_output_roots = ["/rdo/shows/xyz/my_renders"]
        with self.assertRaisesRegex(SequenceError, "must be under /rdo/shows/xyz/my_renders"):
            resolve_output_root("/rdo/shows/xyz", self.cfg)
        self.assertEqual(default_output_root_display(self.cfg), "/rdo/shows/{show}/renders")

    def test_output_dirs(self):
        root = output_root(self.cfg, "xyz", "abc_0010")
        self.assertEqual(output_dir(self.cfg, root, "xyz", "abc_0010", "alpha", 3),
                         f"{self.root}/shows/xyz/renders/abc_0010/abc_0010_alpha_v003")
        with self.assertRaises(SequenceError):
            output_root(self.cfg, None, "abc_0010")


if __name__ == "__main__":
    unittest.main()
