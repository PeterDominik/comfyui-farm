import os
import tempfile
import unittest

from comfyfarm.config import PathMapping, PathsConfig
from comfyfarm.fileio.errors import SequenceError
from comfyfarm.fileio.inputs import resolve_sequence
from comfyfarm.fileio.naming import (default_output_dir_display, default_output_root_display, output_dir, output_root,
                                     resolve_output_folder, sequence_exists)
from comfyfarm.fileio.paths import context_from_path, frame_pattern, parse_input, render, source_stem, to_farm, to_posix

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

    def test_a_machine_that_sees_the_studio_path_as_is(self):
        same = PathMapping(posix="/rdo/shows")                     # a Linux manager or agent
        self.assertEqual(same.windows, "/rdo/shows")
        self.assertEqual(to_farm("/rdo/shows/xyz/a.png", [same]), "/rdo/shows/xyz/a.png")
        self.assertEqual(to_posix("/rdo/shows/xyz/a.png", [same]), "/rdo/shows/xyz/a.png")
        other = PathMapping.model_validate({"posix": "/rdo/shows", "local": "/mnt/shows"})   # mounted elsewhere
        self.assertEqual(to_farm("/rdo/shows/xyz/a.png", [other]), "/mnt/shows/xyz/a.png")

    def test_output_root_as_a_studio_path(self):
        cfg = PathsConfig(mappings=MAP, output_root="/rdo/shows/{show}/_project_ref/_cg/comfyui/renders")
        self.assertEqual(output_root(cfg, "xyz"), "//rodeofx.com/smb/shows/xyz/_project_ref/_cg/comfyui/renders")
        self.assertEqual(default_output_root_display(cfg), "/rdo/shows/{show}/_project_ref/_cg/comfyui/renders")
        unc = PathsConfig(mappings=MAP, output_root="//rodeofx.com/smb/shows/{show}/renders")   # as before
        self.assertEqual(output_root(unc, "xyz"), "//rodeofx.com/smb/shows/xyz/renders")
        self.assertEqual(default_output_root_display(unc), "/rdo/shows/{show}/renders")

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

    def test_unsupported_format_refused(self):
        with self.assertRaisesRegex(SequenceError, "Can't use '.dpx' files"):
            resolve_sequence(f"{self.shot_dir}/abc_0010.%04d.dpx 1-2", self.cfg)

    def test_exr_sequence_checked_for_conversion(self):
        touch(self.shot_dir, *[f"abc_0010.plate.v1.{f}.exr" for f in (1001, 1002, 1004)])
        s = resolve_sequence(f"{self.shot_dir}/abc_0010.plate.v1.%04d.exr 1001-1002", self.cfg)
        self.assertEqual((s.kind, s.first, s.last, s.frame_count, s.source), ("exr", 1001, 1002, 2, "abc_0010.plate.v1"))
        with self.assertRaisesRegex(SequenceError, "Missing frames.*1003"):
            resolve_sequence(f"{self.shot_dir}/abc_0010.plate.v1.%04d.exr", self.cfg)

    def test_missing_folder(self):
        with self.assertRaisesRegex(SequenceError, "can't open the input folder"):
            resolve_sequence(f"{self.root}/nope/a.%04d.png 1-2", self.cfg)

    def test_picked_output_folder(self):
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
        self.assertEqual(default_output_dir_display(self.cfg, "alpha"), "/rdo/shows/{show}/renders/{shot}")

    def test_sequence_exists(self):
        d = f"{self.root}/out"
        pat = "a.v1.alpha_v002.%04d.png"
        self.assertFalse(sequence_exists(d, pat))          # folder missing
        os.makedirs(d)
        touch(d, "a.v1.alpha_v001.1001.png", "b.v1.alpha_v002.1001.png", "a.v1.alpha_v002_notes.txt")
        self.assertFalse(sequence_exists(d, pat))          # other versions / sources don't count
        touch(d, "a.v1.alpha_v002.1004.png")
        self.assertTrue(sequence_exists(d, pat))

    def test_context_by_position_any_shot_naming(self):
        self.assertEqual(context_from_path("/rdo/shows/raa/.published/bpash/bpash_1545/render.comp/x.%d.png", PATTERNS),
                         {"show": "raa", "seq": "bpash", "shot": "bpash_1545"})
        self.assertEqual(context_from_path("//rodeofx.com/smb/shows/ply/.published/15454/15454_656551/hi/a.png", PATTERNS),
                         {"show": "ply", "seq": "15454", "shot": "15454_656551"})
        self.assertEqual(context_from_path("/rdo/shows/ply/_project_ref/_cg/comfy/test/a.png", PATTERNS), {"show": "ply"})
        self.assertEqual(context_from_path("/home/peter/a.png", PATTERNS), {})

    def test_shot_work_area_movhistory(self):
        pats = PATTERNS[:1] + ["/shows/{show}/{seq}/{shot_short}/movhistory/"] + PATTERNS[1:]
        self.assertEqual(
            context_from_path("/rdo/shows/raa/bpash/1545/movhistory/bpash_1545.ingest.mp01.rdo-nwb.distort.compPlate.v2.mov", pats),
            {"show": "raa", "seq": "bpash", "shot_short": "1545", "shot": "bpash_1545"})
        self.assertEqual(context_from_path("//srv/shows/ply/15454/656551/movhistory/v3/a.mov", pats)["shot"], "15454_656551")
        self.assertEqual(context_from_path("/rdo/shows/raa/bpash/1545/movhistory/a.mov", pats, "{shot_short}")["shot"], "1545")
        # Not a movhistory folder: no shot is invented.
        self.assertNotIn("shot", context_from_path("/rdo/shows/raa/_project_ref/_cg/comfy/a.png", pats))
        self.assertNotIn("shot", context_from_path("/rdo/shows/raa/bpash/1545/comp/a.png", pats))

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
            output_dir(self.cfg.output_dir, root="//r", show=s.show, seq=s.seq_name, shot=s.shot, tag="alpha", version=1)

    def test_source_stem(self):
        self.assertEqual(source_stem("bpash_1545.preprocess.mp01_png.v1.%d.png", ""), "bpash_1545.preprocess.mp01_png.v1")
        self.assertEqual(source_stem("name.%04d_beauty.png", ""), "name_beauty")
        self.assertEqual(source_stem("still.png", ""), "still")
        self.assertEqual(source_stem("", "//s/plates/"), "plates")
        self.assertEqual(render("{source}.{tag}_v{version:03d}.{frame:04d}.{ext}", "x", source="a.v1", tag="alpha",
                                version=2, frame=1001, ext="png"), "a.v1.alpha_v002.1001.png")

    def test_output_dirs(self):
        root = output_root(self.cfg, "xyz", "abc", "abc_0010")
        self.assertEqual(output_dir(self.cfg.output_dir, root=root, show="xyz", seq="abc", shot="abc_0010",
                                    tag="alpha", version=3), f"{self.root}/shows/xyz/renders/abc_0010")
        with self.assertRaises(SequenceError):
            output_root(self.cfg, None, "abc", "abc_0010")


if __name__ == "__main__":
    unittest.main()
