"""The ComfyUI nodes in comfyui_nodes/, without ComfyUI: numpy batches, and small
stand-ins for torch (the save node only uses tensor methods; the loaders call
torch.from_numpy last). Also the farm nodes' version, as ComfyUI's /object_info shows it."""
import ast
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "comfyui_nodes"))
import comfyfarm_nodes  # noqa: E402
from comfyfarm_nodes.load_images import ComfyFarmLoadImage, ComfyFarmLoadImages, load_batch  # noqa: E402
from comfyfarm_nodes.save_image import ComfyFarmSaveImage, next_number, to_uint8  # noqa: E402

from comfyfarm.fileio import farmnodes  # noqa: E402
from comfyfarm.fileio.errors import SequenceError  # noqa: E402
from comfyfarm.fileio.outputs import _raw_files  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


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


def png(path, size=(4, 3), mode="RGB", value=0):
    """A small image: every pixel `value` (a tuple for several channels)."""
    Image.new(mode, size, value).save(path)


class LoadImagesTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp().replace("\\", "/")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        sys.modules["torch"] = types.SimpleNamespace(from_numpy=lambda a: a)   # the loaders' last step
        self.addCleanup(sys.modules.pop, "torch", None)
        for f, v in ((1001, (255, 0, 0)), (1002, (0, 255, 0)), (1003, (0, 0, 255)), (1011, (255, 255, 255))):
            png(f"{self.dir}/plate.{f}.png", value=v)
        self.pattern = f"{self.dir}/plate.%04d.png"

    def test_exactly_the_frames_given_in_order(self):
        images, masks, count = ComfyFarmLoadImages().load(self.pattern, "1003,1001,1011")
        self.assertEqual((images.shape, images.dtype, count), ((3, 3, 4, 3), np.float32, 3))
        self.assertEqual([images[i, 0, 0].tolist() for i in range(3)], [[0, 0, 1], [1, 0, 0], [1, 1, 1]])
        self.assertEqual((masks.shape, float(masks.max())), ((3, 64, 64), 0.0))   # no alpha: like core Load Image

    def test_a_range_in_the_path_as_copied_from_nuke(self):
        _, _, count = ComfyFarmLoadImages().load(self.pattern + " 1001-1003")
        self.assertEqual(count, 3)
        _, _, count = ComfyFarmLoadImages().load(self.pattern.replace("%04d", "####") + " 1001-1002", "1011")
        self.assertEqual(count, 1)                                   # frames win over the range
        self.assertEqual(ComfyFarmLoadImages().load(self.pattern)[2], 4)   # neither: every frame

    def test_missing_frames_and_mixed_sizes_stop_the_run(self):
        msg = ComfyFarmLoadImages.VALIDATE_INPUTS(self.pattern, "1001-1004")   # checked when ComfyUI takes the job
        self.assertIn("Missing frames of", msg)
        self.assertIn("1004", msg)
        self.assertIs(ComfyFarmLoadImages.VALIDATE_INPUTS(self.pattern, "1001-1003"), True)
        self.assertIs(ComfyFarmLoadImages.VALIDATE_INPUTS(frames="1001"), True)   # path from another node: later
        self.assertIs(ComfyFarmLoadImage.VALIDATE_INPUTS(), True)
        png(f"{self.dir}/plate.1002.png", size=(8, 3))
        with self.assertRaisesRegex(SequenceError, r"plate.1002.png is 8×3, but plate.1001.png is 4×3"):
            ComfyFarmLoadImages().load(self.pattern, "1001-1003")    # VHS would crop it

    def test_alpha_comes_out_as_a_mask(self):
        png(f"{self.dir}/rgba.1001.png", mode="RGBA", value=(255, 0, 0, 255))
        png(f"{self.dir}/rgba.1002.png", mode="RGBA", value=(255, 0, 0, 0))
        images, masks, _ = ComfyFarmLoadImages().load(f"{self.dir}/rgba.%04d.png", "1001-1002")
        self.assertEqual((images.shape, masks.shape), ((2, 3, 4, 3), (2, 3, 4)))
        self.assertEqual((float(masks[0, 0, 0]), float(masks[1, 0, 0])), (0.0, 1.0))   # 1 - alpha

    def test_16_bit_grey(self):
        Image.fromarray(np.full((3, 4), 65535, np.uint16)).save(f"{self.dir}/h.1001.png")
        images, _ = load_batch([f"{self.dir}/h.1001.png"])
        self.assertEqual((images.shape, float(images.max())), ((1, 3, 4, 3), 1.0))

    def test_a_changed_file_is_read_again(self):
        before = ComfyFarmLoadImages.IS_CHANGED(self.pattern, "1001-1002")
        self.assertEqual(before, ComfyFarmLoadImages.IS_CHANGED(self.pattern, "1001-1002"))
        time.sleep(0.01)
        png(f"{self.dir}/plate.1002.png", value=(9, 9, 9))
        os.utime(f"{self.dir}/plate.1002.png", ns=(time.time_ns() + 10**9,) * 2)
        self.assertNotEqual(before, ComfyFarmLoadImages.IS_CHANGED(self.pattern, "1001-1002"))

    def test_load_image_takes_one(self):
        image, mask = ComfyFarmLoadImage().load(f"{self.dir}/plate.1002.png")
        self.assertEqual((image.shape, image[0, 0, 0].tolist(), mask.shape), ((1, 3, 4, 3), [0, 1, 0], (1, 64, 64)))
        image, _ = ComfyFarmLoadImage().load(self.pattern + " 1003")     # a pattern and one frame
        self.assertEqual(image[0, 0, 0].tolist(), [0, 0, 1])
        self.assertIn("gives 4 images, and Load Image takes one", ComfyFarmLoadImage.VALIDATE_INPUTS(self.pattern))
        self.assertIn("doesn't exist", ComfyFarmLoadImage.VALIDATE_INPUTS(f"{self.dir}/nope.png"))

    def test_outputs_like_vhs_load_images(self):
        """The farm's workflows swapped VHS Load Images (Path) for it: the same output slots."""
        self.assertEqual(ComfyFarmLoadImages.RETURN_TYPES, ("IMAGE", "MASK", "INT"))
        self.assertEqual(list(ComfyFarmLoadImages.INPUT_TYPES()["required"]), ["path"])
        self.assertEqual(list(ComfyFarmLoadImages.INPUT_TYPES()["optional"]), ["frames"])


class FarmNodeVersionTests(unittest.TestCase):
    def info(self, version=None, where=REPO, classes=farmnodes.CLASSES):
        mark = farmnodes.marker(version or farmnodes.version(REPO), where)
        return {c: {"description": f"Some node. {mark}"} for c in classes}

    def test_every_node_says_its_version(self):
        self.assertEqual(sorted(comfyfarm_nodes.NODE_CLASS_MAPPINGS), sorted(farmnodes.CLASSES))
        oi = {c: {"description": n.DESCRIPTION} for c, n in comfyfarm_nodes.NODE_CLASS_MAPPINGS.items()}
        self.assertEqual(set(farmnodes.loaded(oi).values()),
                         {(farmnodes.version(REPO), farmnodes.normalize(str(REPO)))})
        self.assertIsNone(farmnodes.stale(oi, REPO))
        self.assertEqual(comfyfarm_nodes.VERSION, farmnodes.version(REPO))

    def test_a_comfyui_with_other_nodes_is_stale(self):
        self.assertIsNone(farmnodes.stale({"KSampler": {}}, REPO))     # none installed: the workflow checks say so
        self.assertIn("runs version 0123456789ab from D:/old-checkout",
                      farmnodes.stale(self.info("0123456789ab", "D:/old-checkout"), REPO))
        self.assertIn("it lacks ComfyFarmLoadImages",
                      farmnodes.stale(self.info(classes=("ComfyFarmSaveImage", "ComfyFarmLoadImage")), REPO))
        old = {"ComfyFarmSaveImage": {"description": "Save images as PNGs."}}     # before nodes had versions
        self.assertIn("ComfyFarmSaveImage is from before nodes had versions", farmnodes.stale(old, REPO))

    def test_the_version_changes_with_the_code(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        for rel in [*farmnodes.DEPENDS, "comfyui_nodes/comfyfarm_nodes/__init__.py", "comfyfarm/fileio/convert.py"]:
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_bytes((REPO / rel).read_bytes())
        paths = tmp / "comfyfarm/fileio/paths.py"
        lf = paths.read_bytes().replace(b"\r\n", b"\n")              # this checkout may have either
        paths.write_bytes(lf)
        v = farmnodes.version(tmp)
        (tmp / "comfyfarm/fileio/convert.py").write_text("# not used by the nodes")
        self.assertEqual(farmnodes.version(tmp), v)
        paths.write_bytes(lf.replace(b"\n", b"\r\n"))                 # a Windows checkout: the same version
        self.assertEqual(farmnodes.version(tmp), v)
        (tmp / "comfyui_nodes/comfyfarm_nodes/__init__.py").write_text("# changed")
        self.assertNotEqual(farmnodes.version(tmp), v)

    def test_depends_lists_every_fileio_module_the_nodes_use(self):
        used, todo = set(), [*(REPO / "comfyui_nodes/comfyfarm_nodes").glob("*.py")]
        while todo:
            f = todo.pop()
            for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:                       # relative, inside comfyfarm.fileio
                        base = "comfyfarm.fileio" + (f".{base}" if base else "") if "fileio" in str(f) else ""
                    names = [f"{base}.{a.name}" for a in node.names] + [base]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                for n in names:
                    if n.startswith("comfyfarm.fileio."):
                        rel = n.replace(".", "/") + ".py"
                        if (REPO / rel).is_file() and rel not in used:
                            used.add(rel)
                            todo.append(REPO / rel)
        self.assertEqual(used, set(farmnodes.DEPENDS))
