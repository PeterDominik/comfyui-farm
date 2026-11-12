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

bit_depth 16 writes 16-bit PNGs, for mattes whose soft edges (hair, motion blur) 8 bits
would band: an image whose three channels are the same (a MASK through Convert Mask to
Image) is written as one 16-bit grey channel, others as 16-bit RGB. Pillow can't write
16-bit RGB, so these PNGs are encoded here (plain zlib, compress_level as for 8 bits).
"""
import os
import re
import struct
import zlib

import numpy as np
from PIL import Image


def to_uint8(image) -> np.ndarray:
    """One image (H, W) or (H, W, C), values 0-1, as 8-bit, rounded."""
    if hasattr(image, "detach"):     # a torch tensor: convert where it lives, copy 8 bits
        return image.detach().clamp(0, 1).mul(255).round().byte().cpu().numpy()
    return (np.clip(np.asarray(image, dtype=np.float32), 0, 1) * 255).round().astype(np.uint8)


def to_uint16(image) -> np.ndarray:
    """One image (H, W) or (H, W, C), values 0-1, as 16-bit, rounded. A single channel, or
    three equal ones (a matte), comes back as (H, W)."""
    if hasattr(image, "detach"):     # a torch tensor: float32 on the CPU, then as below
        image = image.detach().float().cpu().numpy()
    a = np.asarray(image, dtype=np.float32)
    if a.ndim == 3 and (a.shape[2] == 1 or (a.shape[2] == 3 and np.array_equal(a[..., 0], a[..., 1])
                                             and np.array_equal(a[..., 0], a[..., 2]))):
        a = a[..., 0]
    return (np.clip(a, 0, 1) * 65535).round().astype(np.uint16)


def write_png16(path: str, px: np.ndarray, compress_level: int = 0) -> None:
    """A 16-bit PNG: grey for (H, W), RGB for (H, W, 3). PNG stores samples big-endian,
    each row after a filter byte (0: none)."""
    h, w = px.shape[:2]
    channels = 1 if px.ndim == 2 else px.shape[2]
    if channels not in (1, 3):
        raise ValueError(f"16-bit PNGs are grey or RGB, not {channels} channels")
    rows = np.zeros((h, 1 + w * channels * 2), dtype=np.uint8)
    rows[:, 1:] = np.ascontiguousarray(px, dtype=">u2").view(np.uint8).reshape(h, -1)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", w, h, 16, 0 if channels == 1 else 2, 0, 0, 0)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(rows.tobytes(), int(compress_level))) + chunk(b"IEND", b""))


def next_number(folder: str, prefix: str, delimiter: str = "_") -> int:
    """1, or one past the highest <prefix>_NNNN.* already in the folder."""
    rx = re.compile(re.escape(prefix) + re.escape(delimiter) + r"(\d+)\.[^.]+$")
    nums = [int(m.group(1)) for m in map(rx.match, os.listdir(folder)) if m]
    return max(nums) + 1 if nums else 1


def save_images(images, output_path: str, filename_prefix: str, compress_level: int = 0,
                padding: int = 4, bit_depth: int = 8) -> list[str]:
    """Write each image of the batch as <output_path>/<prefix>_NNNN.png; the paths written."""
    os.makedirs(output_path, exist_ok=True)
    first = next_number(output_path, filename_prefix)
    files = []
    for i in range(len(images)):
        path = os.path.join(output_path, f"{filename_prefix}_{first + i:0{padding}d}.png")
        if int(bit_depth) == 16:
            write_png16(path, to_uint16(images[i]), compress_level)
        else:
            px = to_uint8(images[i])
            if px.ndim == 3 and px.shape[2] == 1:
                px = px[..., 0]
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
            "optional": {
                "bit_depth": (["8", "16"], {
                    "default": "8",
                    "tooltip": "16: 16-bit PNGs, for mattes (an image with three equal channels is saved grey)."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "files")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "ComfyFarm"
    DESCRIPTION = __doc__

    def save(self, images, output_path, filename_prefix, compress_level=0, bit_depth="8"):
        folder = (output_path or "").strip()
        if not folder:
            import folder_paths
            folder = folder_paths.get_output_directory()
        files = save_images(images, folder, filename_prefix or "map", compress_level, bit_depth=int(bit_depth))
        return (images, "\n".join(files))
