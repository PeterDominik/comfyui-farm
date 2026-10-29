"""ComfyFarm Save Image: writes images where the farm gateway picks them up.

A drop-in for WAS "Image Save" in farm workflows: the same output_path and
filename_prefix inputs, the same file names (<prefix>_0001.png, ...) and the same two
outputs (images, files). It leaves out what made each WAS save slow:
  - No history: WAS reloads its was_history.json, checks every file it ever saved still
    exists (on the network share, one request each), and rewrites it, on every save.
  - No compression by default (compress_level 0): the farm reads these files once and
    turns them into EXRs. 1-9 is zlib's level if disk space ever matters more.
  - No workflow metadata and no previews.
The 8-bit conversion is rounded and done on the GPU, so only the 8-bit image is copied
to the CPU. One-channel images (CHORD's height, roughness, metalness) stay greyscale.
"""
import os
import re

import numpy as np
from PIL import Image


def to_uint8(image) -> np.ndarray:
    """One image (H, W) or (H, W, C), values 0-1, as 8-bit, rounded."""
    if hasattr(image, "detach"):     # a torch tensor: convert where it lives, copy 8 bits
        return image.detach().clamp(0, 1).mul(255).round().byte().cpu().numpy()
    return (np.clip(np.asarray(image, dtype=np.float32), 0, 1) * 255).round().astype(np.uint8)


def next_number(folder: str, prefix: str, delimiter: str = "_") -> int:
    """1, or one past the highest <prefix>_NNNN.* already in the folder."""
    rx = re.compile(re.escape(prefix) + re.escape(delimiter) + r"(\d+)\.[^.]+$")
    nums = [int(m.group(1)) for m in map(rx.match, os.listdir(folder)) if m]
    return max(nums) + 1 if nums else 1


def save_images(images, output_path: str, filename_prefix: str, compress_level: int = 0,
                padding: int = 4) -> list[str]:
    """Write each image of the batch as <output_path>/<prefix>_NNNN.png; the paths written."""
    os.makedirs(output_path, exist_ok=True)
    first = next_number(output_path, filename_prefix)
    files = []
    for i in range(len(images)):
        px = to_uint8(images[i])
        if px.ndim == 3 and px.shape[2] == 1:
            px = px[..., 0]
        path = os.path.join(output_path, f"{filename_prefix}_{first + i:0{padding}d}.png")
        Image.fromarray(np.ascontiguousarray(px)).save(path, format="PNG", compress_level=int(compress_level))
        files.append(path)
    return files


class ComfyFarmSaveImage:
    """Save images as PNGs into a folder, for the farm gateway to pick up."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "output_path": ("STRING", {
                    "default": "",
                    "tooltip": "Folder to write into (the farm sets it per job). Empty = ComfyUI's output folder."}),
                "filename_prefix": ("STRING", {
                    "default": "map",
                    "tooltip": "Files are named <prefix>_0001.png, <prefix>_0002.png, ..."}),
                "compress_level": ("INT", {
                    "default": 0, "min": 0, "max": 9, "step": 1,
                    "tooltip": "PNG zlib level: 0 = none (fastest), 9 = smallest."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "files")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "ComfyFarm"
    DESCRIPTION = __doc__

    def save(self, images, output_path, filename_prefix, compress_level=0):
        folder = (output_path or "").strip()
        if not folder:
            import folder_paths
            folder = folder_paths.get_output_directory()
        files = save_images(images, folder, filename_prefix or "map", compress_level)
        return (images, "\n".join(files))
