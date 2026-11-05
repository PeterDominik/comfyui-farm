"""How a workflow's image loader node gets its input: one adapter per loader class.

A workflow's input names its loader node (`loader: {node, class}`, or `sequence.loader` in
older manifests); the adapter for its class says which inputs the node must have, whether
the loader lists a whole folder (so the input must be checked against that listing), how
many images it loads, and fills those inputs from the input the manager resolved
(fileio.inputs.Sequence).

- ComfyFarmLoadImages (comfyui_nodes/): a frame pattern and the exact frame list. The
  farm's own loader, used by the farm's workflows.
- ComfyFarmLoadImage (comfyui_nodes/): one image, for inputs that take one: a file, or a
  pattern and its frame.
- VHS_LoadImagesPath: a folder, skip_first_images and image_load_cap. Kept for
  downloaded workflows: it reads every image in the folder in name order, so the farm
  works out skip/cap and refuses folders it can't read a range from.

Workflows never load movies (the farm extracts frames first), so video loader nodes are
refused in any workflow."""

from __future__ import annotations

from dataclasses import dataclass

from .fileio import inputs as I
from .fileio.paths import frames_text

__all__ = ["Loader", "LOADERS", "VIDEO_LOADERS", "get"]

# Movie loaders: a workflow with one of these is refused (workflows only take images).
VIDEO_LOADERS = {"VHS_LoadVideo", "VHS_LoadVideoPath", "VHS_LoadVideoFFmpeg", "VHS_LoadVideoFFmpegPath", "LoadVideo"}


@dataclass(frozen=True)
class Loader:
    cls: str
    inputs: tuple[str, ...]         # the node must have these (checked against the template)
    lists_folder: bool              # reads the whole folder, not exact files
    takes: str = "images"           # image: it loads exactly one

    def fill(self, inputs: dict, seq: I.Sequence) -> None:
        """Point the node at the input (changes `inputs`, the node's inputs, in place)."""
        if self.cls == "VHS_LoadImagesPath":
            inputs["directory"] = seq.load_dir or seq.directory
            inputs["skip_first_images"] = seq.skip
            inputs["image_load_cap"] = seq.cap
            return
        if self.takes == "image":
            inputs["path"] = I.load_file(seq)
            return
        path, frames = I.load_path(seq)
        inputs["path"] = path
        inputs["frames"] = frames_text(frames) if frames else ""


LOADERS = {
    "ComfyFarmLoadImages": Loader("ComfyFarmLoadImages", ("path",), lists_folder=False),
    "ComfyFarmLoadImage": Loader("ComfyFarmLoadImage", ("path",), lists_folder=False, takes="image"),
    "VHS_LoadImagesPath": Loader("VHS_LoadImagesPath", ("directory", "skip_first_images", "image_load_cap"),
                                 lists_folder=True),
}


def get(cls: str | None) -> Loader | None:
    return LOADERS.get(cls or "")
