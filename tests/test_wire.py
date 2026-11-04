"""Jobs between machines (fileio.wire): one machine's paths -> studio paths and @scratch /
@repo -> another machine's paths."""

import unittest

from comfyfarm.config import PathMapping
from comfyfarm.fileio import paths as P
from comfyfarm.fileio import wire as W

LINUX = W.View(mappings=[], scratch="/var/comfyfarm/scratch", repo="/opt/comfyfarm")
WINDOWS = W.View(mappings=[PathMapping(posix="/rdo/shows", windows="//rodeofx.com/smb/shows")],
                 scratch="D:/comfyfarm/data/scratch", repo="D:/comfyfarm")

JOB_ON_WINDOWS = {
    "info": {
        "output_dir": "//rodeofx.com/smb/shows/xyz/_project_ref/_cg/comfyui/renders/abc_0010",
        "output_dir_posix": "/rdo/shows/xyz/_project_ref/_cg/comfyui/renders/abc_0010",
        "input": {"farm_path": "//RODEOFX.COM/smb/shows/xyz/plate.%04d.exr",
                  "convert": {"dst_dir": "D:/comfyfarm/data/scratch/j1/", "config": "D:/comfyfarm/ocio/config.ocio"}},
        "tag": "alpha", "first_frame": 1001,
    },
    "prompt": {"9104": {"inputs": {"directory": "D:/comfyfarm/data/scratch/j1/", "image_load_cap": 0}},
               "9094": {"inputs": {"value": "//rodeofx.com/smb/shows/xyz/plate.%04d.exr 1001-1012"}},
               "5": {"inputs": {"text": "//rodeofx.com/smb/showsXYZ is not a path under the mapping"}}},
}


class WireTests(unittest.TestCase):
    def test_windows_job_to_the_wire(self):
        w = W.portable(JOB_ON_WINDOWS, WINDOWS)
        self.assertEqual(w["info"]["output_dir"], "/rdo/shows/xyz/_project_ref/_cg/comfyui/renders/abc_0010")
        self.assertEqual(w["info"]["input"]["farm_path"], "/rdo/shows/xyz/plate.%04d.exr")   # UNC case ignored
        self.assertEqual(w["info"]["input"]["convert"], {"dst_dir": "@scratch/j1/", "config": "@repo/ocio/config.ocio"})
        self.assertEqual(w["prompt"]["9104"]["inputs"]["directory"], "@scratch/j1/")         # scratch inside the repo
        self.assertEqual(w["prompt"]["9094"]["inputs"]["value"], "/rdo/shows/xyz/plate.%04d.exr 1001-1012")
        self.assertEqual(w["prompt"]["5"], JOB_ON_WINDOWS["prompt"]["5"])                     # only whole folders match
        self.assertEqual((w["info"]["tag"], w["info"]["first_frame"]), ("alpha", 1001))
        self.assertIsNot(w, JOB_ON_WINDOWS)
        self.assertEqual(JOB_ON_WINDOWS["info"]["input"]["convert"]["dst_dir"], "D:/comfyfarm/data/scratch/j1/")

    def test_to_a_linux_agent_and_back(self):
        on_linux = W.localize(W.portable(JOB_ON_WINDOWS, WINDOWS), LINUX)
        self.assertEqual(on_linux["info"]["output_dir"], "/rdo/shows/xyz/_project_ref/_cg/comfyui/renders/abc_0010")
        self.assertEqual(on_linux["prompt"]["9104"]["inputs"]["directory"], "/var/comfyfarm/scratch/j1/")
        self.assertEqual(on_linux["info"]["input"]["convert"]["config"], "/opt/comfyfarm/ocio/config.ocio")
        back = W.localize(W.portable(on_linux, LINUX), WINDOWS)
        self.assertEqual(back["info"]["output_dir"], JOB_ON_WINDOWS["info"]["output_dir"])
        self.assertEqual(back["prompt"]["9104"], JOB_ON_WINDOWS["prompt"]["9104"])

    def test_posix_keys_stay_studio_paths(self):
        outputs = {"frame_pattern": "//rodeofx.com/smb/shows/xyz/out/a.%04d.png",
                   "frame_pattern_posix": "/rdo/shows/xyz/out/a.%04d.png",
                   "maps": {"color01": {"pattern": "D:/comfyfarm/data/scratch/x.exr", "pattern_posix": "/rdo/x.exr"}}}
        on_windows = W.localize(W.portable(outputs, WINDOWS), WINDOWS)
        self.assertEqual(on_windows, outputs)
        self.assertEqual(W.localize(W.portable(outputs, WINDOWS), LINUX)["frame_pattern_posix"],
                         "/rdo/shows/xyz/out/a.%04d.png")

    def test_shows_alias_reaches_agents_as_rdo_shows(self):
        manager = W.View(mappings=[PathMapping(posix="/rdo/shows", windows="/rdo/shows"),
                                   PathMapping(posix="/shows", windows="/rdo/shows")], scratch="/s", repo="/r")
        job = {"input": P.to_farm("/shows/bri3/plate.%04d.exr", manager.mappings)}   # as the artist typed it
        wire = W.portable(job, manager)
        self.assertEqual(wire["input"], "/rdo/shows/bri3/plate.%04d.exr")
        self.assertEqual(W.localize(wire, WINDOWS)["input"], "//rodeofx.com/smb/shows/bri3/plate.%04d.exr")

    def test_same_machine_is_unchanged(self):
        self.assertEqual(W.localize(W.portable(JOB_ON_WINDOWS, WINDOWS), WINDOWS)["info"]["input"]["farm_path"],
                         "//rodeofx.com/smb/shows/xyz/plate.%04d.exr")   # normalized to the mapping's case
        job = {"a": ["/rdo/shows/x", "/var/comfyfarm/scratch/j/", "/opt/comfyfarm/ocio/config.ocio", 3, None]}
        self.assertEqual(W.localize(W.portable(job, LINUX), LINUX), job)


if __name__ == "__main__":
    unittest.main()
