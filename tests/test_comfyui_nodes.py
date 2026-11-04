"""The ComfyUI nodes in comfyui_nodes/, without ComfyUI: numpy batches, and a small
stand-in for torch tensors (the node only uses tensor methods, never imports torch)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "comfyui_nodes"))
import comfyfarm_nodes  # noqa: E402
from comfyfarm_nodes.save_image import ComfyFarmSaveImage, next_number, to_uint8  # noqa: E402

from comfyfarm.fileio.outputs import _raw_files  # noqa: E402


class FakeTensor:
    """The torch.Tensor methods to_uint8 uses, on a numpy array."""

    def __init__(self, a):
        self.a = np.asarray(a)

    def detach(self):
        return self

    def clamp(self, lo, hi):
        return FakeTensor(np.clip(self.a, lo, hi))

    def mul(self, k):
        return FakeTensor(self.a * k)

    def round(self):
        return FakeTensor(np.round(self.a))

    def byte(self):
        return FakeTensor(self.a.astype(np.uint8))

    def cpu(self):
        return self

    def numpy(self):
        return self.a

    def __getitem__(self, i):
        return FakeTensor(self.a[i])

    def __len__(self):
        return len(self.a)


class SaveImageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.node = ComfyFarmSaveImage()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def test_registered(self):
        self.assertIs(comfyfarm_nodes.NODE_CLASS_MAPPINGS["ComfyFarmSaveImage"], ComfyFarmSaveImage)
        inputs = ComfyFarmSaveImage.INPUT_TYPES()["required"]
        # The same inputs the farm's manifests point at on WAS Image Save.
        self.assertEqual(list(inputs), ["images", "output_path", "filename_prefix", "compress_level"])
        self.assertEqual(inputs["compress_level"][1]["default"], 0)
        self.assertEqual(ComfyFarmSaveImage.RETURN_NAMES, ("images", "files"))
        self.assertTrue(ComfyFarmSaveImage.OUTPUT_NODE)

    def test_rgb_and_grey_maps(self):
        rgb = np.zeros((2, 4, 5, 3), np.float32)
        rgb[..., 0] = 0.5            # 127.5 rounds to 128 (WAS truncated it to 127)
        rgb[1, ..., 1] = 1.2         # clipped
        out, files = self.node.save(rgb, self.dir, "_cfj1_color01")
        self.assertIs(out, rgb)      # passes the batch through, like WAS
        names = [os.path.basename(f) for f in files.split("\n")]
        self.assertEqual(names, ["_cfj1_color01_0001.png", "_cfj1_color01_0002.png"])
        self.assertEqual(_raw_files(os.listdir(self.dir), "_cfj1_color01", "png"), names)   # what the farm reads
        im = Image.open(files.split("\n")[1])
        self.assertEqual((im.mode, im.size), ("RGB", (5, 4)))
        self.assertEqual(np.asarray(im)[0, 0].tolist(), [128, 255, 0])
        # One-channel maps stay greyscale: (H, W) and (H, W, 1).
        for i, grey in enumerate((np.full((1, 4, 5), 0.25, np.float32), np.full((1, 4, 5, 1), 0.25, np.float32))):
            _, f = self.node.save(grey, self.dir, f"_cfj1_bmp0{i}")
            im = Image.open(f)
            self.assertEqual((im.mode, int(np.asarray(im)[0, 0])), ("L", 64))

    def test_tensors_are_converted_before_copying(self):
        t = FakeTensor(np.full((1, 2, 2, 3), 0.75, np.float32))
        px = to_uint8(t[0])
        self.assertEqual((px.dtype, int(px[0, 0, 0])), (np.uint8, 191))
        _, f = self.node.save(t, self.dir, "t")
        self.assertEqual(np.asarray(Image.open(f))[1, 1].tolist(), [191, 191, 191])

    def test_numbers_continue_and_compression(self):
        img = np.repeat(np.linspace(0, 1, 64, dtype=np.float32)[None, None, :, None], 64, 1).repeat(3, 3)  # a gradient
        _, a = self.node.save(img, self.dir, "m")
        _, b = self.node.save(img, self.dir, "m", compress_level=9)
        self.assertEqual([os.path.basename(a), os.path.basename(b)], ["m_0001.png", "m_0002.png"])
        self.assertEqual(next_number(self.dir, "m"), 3)
        self.assertEqual(next_number(self.dir, "other"), 1)
        self.assertGreater(os.path.getsize(a), os.path.getsize(b))           # level 0 = stored, not compressed
        self.assertEqual(np.asarray(Image.open(a)).tolist(), np.asarray(Image.open(b)).tolist())

    def test_empty_output_path_uses_comfyui_output(self):
        import types
        sys.modules["folder_paths"] = types.SimpleNamespace(get_output_directory=lambda: self.dir)
        self.addCleanup(sys.modules.pop, "folder_paths", None)
        _, f = self.node.save(np.zeros((1, 2, 2, 3), np.float32), "  ", "")
        self.assertEqual(f, os.path.join(self.dir, "map_0001.png"))


class ChordWorkflowTests(unittest.TestCase):
    def test_chord_saves_with_the_farm_node(self):
        from comfyfarm.workflows import Workflow
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_workflows import WF_DIR, fake_object_info
        wf = Workflow(WF_DIR / "chord_pbr")
        saves = {nid: n for nid, n in wf.template.items() if n["class_type"] == "ComfyFarmSaveImage"}
        self.assertEqual(sorted(saves), ["19", "24", "28", "31", "32"])
        self.assertFalse([n for n in wf.template.values() if n["class_type"] == "Image Save"])
        # Its inputs match the node's, so check finds nothing missing ...
        node_inputs = set(ComfyFarmSaveImage.INPUT_TYPES()["required"])
        self.assertTrue(all(set(n["inputs"]) == node_inputs for n in saves.values()))
        oi = fake_object_info(wf.template)
        self.assertEqual(wf.check_object_info(oi), [])
        # ... and without the node installed, check says how to install it.
        del oi["ComfyFarmSaveImage"]
        probs = wf.check_object_info(oi)
        self.assertEqual(len(probs), 5)
        self.assertIn("comes with this repo: run deploy\\windows\\setup.ps1 -ComfyUIDir <ComfyUI folder>", probs[0])
