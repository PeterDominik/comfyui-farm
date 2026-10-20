import os
import tempfile
import unittest

from comfyfarm.config import PathMapping, PathsConfig
from comfyfarm.paths import (SequenceError, context_from_path, default_output_dir_display,
                             default_output_root_display, folder_has_frames, frame_pattern, output_dir,
                             output_root, parse_input, render, resolve_output_folder, resolve_sequence,
                             source_stem, to_farm, to_posix, version_candidates)

PATTERNS = ["/shows/{show}/.published/{seq}/{shot}/", "/shows/{show}/"]

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

    def test_picked_output_folder_is_the_destination(self):
        os.makedirs(f"{self.root}/shows/xyz/comfy")
        # The folder itself may not exist yet; its parent must.
        self.assertEqual(resolve_output_folder("/rdo/shows/xyz/comfy/test/", self.cfg),
                         f"{self.root}/shows/xyz/comfy/test")
        with self.assertRaisesRegex(SequenceError, "comfyy doesn't exist"):
            resolve_output_folder("/rdo/shows/xyz/comfyy/test", self.cfg)
        with self.assertRaisesRegex(SequenceError, "must be under /rdo/shows"):
            resolve_output_folder("/home/peter/renders", self.cfg)
        with self.assertRaisesRegex(SequenceError, "must be under"):
            resolve_output_folder("C:/Windows/Temp", self.cfg)
        with self.assertRaisesRegex(SequenceError, r"'\.\.'"):
            resolve_output_folder("/rdo/shows/xyz/../../etc", self.cfg)
        with self.assertRaisesRegex(SequenceError, "not a file, frame pattern"):
            resolve_output_folder("/rdo/shows/xyz/comfy/a.%04d.png 1-5", self.cfg)
        with self.assertRaisesRegex(SequenceError, "Can't write into .published"):
            resolve_output_folder("/rdo/shows/xyz/.published/abc/abc_0010/comfy", self.cfg)
        self.cfg.allowed_output_roots = ["/rdo/shows/xyz/comfy"]
        with self.assertRaisesRegex(SequenceError, "must be under /rdo/shows/xyz/comfy"):
            resolve_output_folder("/rdo/shows/xyz/other", self.cfg)
        self.assertEqual(default_output_root_display(self.cfg), "/rdo/shows/{show}/renders")
        self.assertEqual(default_output_dir_display(self.cfg, "alpha"),
                         "/rdo/shows/{show}/renders/{shot}/{shot}_alpha_v###")

    def test_version_candidates(self):
        def names(f, n=3):
            it = version_candidates(f)
            return [next(it)[0].rpartition("/")[2] for _ in range(n)]
        self.assertEqual(names("//s/comfy/test"), ["test", "test_v002", "test_v003"])
        self.assertEqual(names("//s/comfy/test_v003"), ["test_v003", "test_v004", "test_v005"])
        self.assertEqual(names("//s/comfy/alpha.V01"), ["alpha.V01", "alpha.V02", "alpha.V03"])
        self.assertEqual(names("//s/comfy/v7"), ["v7", "v8", "v9"])
        self.assertEqual(names("//s/comfy/dev2"), ["dev2", "dev2_v002", "dev2_v003"])   # not a version
        self.assertEqual([v for _, v in list(version_candidates("//s/test"))[:2]], [1, 2])

    def test_folder_has_frames(self):
        d = f"{self.root}/out"
        self.assertFalse(folder_has_frames(d))          # missing
        os.makedirs(d)
        touch(d, "notes.txt", "comp.nk")
        self.assertFalse(folder_has_frames(d))          # no frames
        touch(d, "old.1001.exr")
        self.assertTrue(folder_has_frames(d))

    def test_context_by_position_any_shot_naming(self):
        self.assertEqual(context_from_path("/rdo/shows/raa/.published/bpash/bpash_1545/render.comp/x.%d.png", PATTERNS),
                         {"show": "raa", "seq": "bpash", "shot": "bpash_1545"})
        self.assertEqual(context_from_path("//rodeofx.com/smb/shows/ply/.published/15454/15454_656551/hi/a.png", PATTERNS),
                         {"show": "ply", "seq": "15454", "shot": "15454_656551"})
        self.assertEqual(context_from_path("/rdo/shows/ply/_project_ref/_cg/comfy/test/a.png", PATTERNS), {"show": "ply"})
        self.assertEqual(context_from_path("/home/peter/a.png", PATTERNS), {})

    def test_numeric_shot_and_no_guessing(self):
        d = f"{self.root}/shows/ply/.published/15454/15454_656551/render.comp/pre/v1/main/hi"
        os.makedirs(d)
        touch(d, *[f"15454_656551.pre.v1.{f}.png" for f in (1001, 1002)])
        s = resolve_sequence(f"/rdo/shows/ply/.published/15454/15454_656551/render.comp/pre/v1/main/hi/"
                             f"15454_656551.pre.v1.%d.png", self.cfg)
        self.assertEqual((s.show, s.seq_name, s.shot, s.source), ("ply", "15454", "15454_656551", "15454_656551.pre.v1"))
        os.makedirs(f"{self.root}/shows/ply/work/hi")
        touch(f"{self.root}/shows/ply/work/hi", "plate.1001.png")
        s = resolve_sequence("/rdo/shows/ply/work/hi/plate.%04d.png", self.cfg)
        self.assertEqual((s.show, s.shot), ("ply", None))   # not 'hi'
        with self.assertRaisesRegex(SequenceError, "no shot from the input path.*Give an Output folder"):
            output_dir(self.cfg, "//r", s.show, s.seq_name, s.shot, "alpha", 1)

    def test_source_stem(self):
        self.assertEqual(source_stem("bpash_1545.preprocess.mp01_png.v1.%d.png", ""), "bpash_1545.preprocess.mp01_png.v1")
        self.assertEqual(source_stem("name.%04d_beauty.png", ""), "name_beauty")
        self.assertEqual(source_stem("still.png", ""), "still")
        self.assertEqual(source_stem("", "//s/plates/"), "plates")
        self.assertEqual(render("{source}.{tag}_v{version:03d}.{frame:04d}.{ext}", "x", source="a.v1", tag="alpha",
                                version=2, frame=1001, ext="png"), "a.v1.alpha_v002.1001.png")

    def test_output_dirs(self):
        root = output_root(self.cfg, "xyz", "abc", "abc_0010")
        self.assertEqual(output_dir(self.cfg, root, "xyz", "abc", "abc_0010", "alpha", 3),
                         f"{self.root}/shows/xyz/renders/abc_0010/abc_0010_alpha_v003")
        with self.assertRaises(SequenceError):
            output_root(self.cfg, None, "abc", "abc_0010")


if __name__ == "__main__":
    unittest.main()
