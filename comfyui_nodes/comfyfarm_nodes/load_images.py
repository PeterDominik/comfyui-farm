"""ComfyFarm Load Images and ComfyFarm Load Image: read exactly the files the farm names.

The farm's loaders for its workflows, instead of VHS "Load Images (Path)" and core "Load
Image":
  - Load Images takes a file or frame pattern and an exact frame list (1001-1158,
    1001,1002,1011) and reads those files in that order. Nothing else in the folder
    matters, a missing frame is an error, and images of different sizes are refused
    instead of being cropped to the first one's size.
  - Load Image reads one image: a file, or a pattern and one frame (plate.%04d.png 1050).
  - The path is typed as in Nuke or the pipeline, range included (plate.%04d.png
    1001-1158), so the workflow also runs by hand in ComfyUI.
  - Alpha comes out as a MASK the way core Load Image gives it (1 - alpha; 64×64 zeros
    without alpha). The outputs match VHS Load Images (Path): IMAGE, MASK, frame_count.
The file handling is the farm's own (comfyfarm.fileio.farmnodes.input_files), so the
manager checks an input exactly the way these nodes will read it.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image, ImageOps

from comfyfarm.fileio.errors import SequenceError
from comfyfarm.fileio.farmnodes import input_files

_PATH_TIP = ("A file, or a frame pattern (%04d, ####, $F4) with an optional range, as copied from Nuke: "
             "/shows/x/plate.%04d.png 1001-1158. The farm sets it per job.")
_FRAMES_TIP = ("The exact frames to read, in order: 1001-1158, or 1001,1002,1011. Empty = the range in the "
               "path, or every frame of the pattern.")


def read_image(path: str) -> tuple[np.ndarray, np.ndarray | None]:
    """One image as float32 RGB (H, W, 3) in 0-1, and its alpha (H, W) or None."""
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):          # 16-bit or float greyscale
            a = np.asarray(im, dtype=np.float32)
            a = a / 65535.0 if im.mode != "F" else a
            grey = np.clip(a, 0.0, 1.0)
            return np.repeat(grey[..., None], 3, axis=2), None
        alpha = None
        if "A" in im.getbands():
            alpha = np.asarray(im.getchannel("A"), dtype=np.float32) / 255.0
        elif im.mode == "P" and "transparency" in im.info:
            alpha = np.asarray(im.convert("RGBA").getchannel("A"), dtype=np.float32) / 255.0
        rgb = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    return rgb, alpha


def load_batch(files: list[str], threads: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """The images as one batch (N, H, W, 3) and their masks (N, H, W), or (N, 64, 64)
    zeros when none has alpha. Images of different sizes are refused."""
    if not files:
        raise SequenceError("No images to load.")
    first, first_alpha = read_image(files[0])
    h, w = first.shape[:2]
    images = np.empty((len(files), h, w, 3), dtype=np.float32)     # filled in place: no second copy
    images[0] = first
    alphas: list = [None] * len(files)
    alphas[0] = first_alpha

    def one(i: int) -> None:
        rgb, alpha = read_image(files[i])
        if rgb.shape[:2] != (h, w):
            raise SequenceError(f"{os.path.basename(files[i])} is {rgb.shape[1]}×{rgb.shape[0]}, but "
                                f"{os.path.basename(files[0])} is {w}×{h}: the images of one load must all "
                                f"be the same size.")
        images[i] = rgb
        alphas[i] = alpha

    if len(files) > 1:
        with ThreadPoolExecutor(max_workers=threads or min(8, os.cpu_count() or 1)) as pool:
            for f in [pool.submit(one, i) for i in range(1, len(files))]:
                f.result()
    if any(a is not None for a in alphas):
        masks = np.zeros((len(files), h, w), dtype=np.float32)
        for i, a in enumerate(alphas):
            if a is not None:
                masks[i] = 1.0 - a
    else:
        masks = np.zeros((len(files), 64, 64), dtype=np.float32)
    return images, masks


def _signature(path: str | None, frames: str):
    """Changes when any file read changes, so ComfyUI doesn't reuse an older load. A path
    from another node isn't known yet: read again every time."""
    if path is None:
        return float("nan")
    try:
        files = input_files(path, frames)
    except SequenceError as e:
        return f"error: {e}"
    parts = []
    for _, f in files:
        try:
            st = os.stat(f)
            parts.append(f"{f}:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append(f"{f}:gone")
    return "\n".join(parts)


def _check(path: str | None, frames: str) -> bool | str:
    if path is None:                    # from another node (a run by hand): checked when it runs
        return True
    try:
        input_files(path, frames)
    except SequenceError as e:
        return str(e)
    return True


def _tensors(images: np.ndarray, masks: np.ndarray):
    import torch
    return torch.from_numpy(images), torch.from_numpy(masks)


class ComfyFarmLoadImages:
    """Load a frame sequence: exactly the frames listed, in order (ComfyFarm)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {"path": ("STRING", {"default": "", "tooltip": _PATH_TIP})},
            "optional": {"frames": ("STRING", {"default": "", "tooltip": _FRAMES_TIP})},
        }

    RETURN_TYPES = ("IMAGE", "MASK", "INT")
    RETURN_NAMES = ("IMAGE", "MASK", "frame_count")
    FUNCTION = "load"
    CATEGORY = "ComfyFarm"
    DESCRIPTION = ("Loads exactly the frames given (a pattern and a frame list), in order. A missing frame "
                   "or images of different sizes stop the run.")

    # ComfyUI leaves out inputs that come from another node: path=None.
    @classmethod
    def VALIDATE_INPUTS(cls, path=None, frames=""):
        return _check(path, frames)

    @classmethod
    def IS_CHANGED(cls, path=None, frames=""):
        return _signature(path, frames)

    def load(self, path, frames=""):
        files = [f for _, f in input_files(path, frames)]
        images, masks = load_batch(files)
        image, mask = _tensors(images, masks)
        return (image, mask, len(files))


class ComfyFarmLoadImage:
    """Load one image (ComfyFarm)."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"path": ("STRING", {
            "default": "", "tooltip": "An image file, or a frame pattern and one frame: /shows/x/plate.%04d.png 1050. "
                                      "The farm sets it per job."})}}

    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES = ("IMAGE", "MASK")
    FUNCTION = "load"
    CATEGORY = "ComfyFarm"
    DESCRIPTION = "Loads one image: a file, or a frame pattern and one frame number."

    @classmethod
    def _one(cls, path):
        files = input_files(path)
        if len(files) != 1:
            raise SequenceError(f"{path} gives {len(files)} images, and Load Image takes one: put the frame "
                                f"after the pattern, e.g. … {files[0][0]}.")
        return files[0][1]

    @classmethod
    def VALIDATE_INPUTS(cls, path=None):
        if path is None:
            return True
        try:
            cls._one(path)
        except SequenceError as e:
            return str(e)
        return True

    @classmethod
    def IS_CHANGED(cls, path=None):
        return _signature(path, "")

    def load(self, path):
        images, masks = load_batch([self._one(path)])
        image, mask = _tensors(images, masks)
        return (image, mask)
