"""ComfyUI nodes that come with the ComfyUI farm gateway (comfyui-farm repo).

scripts/setup.ps1 -ComfyUI <ComfyUI folder> links this folder into ComfyUI's
custom_nodes, so `git pull` on the farm machine updates the nodes too (restart ComfyUI
to load a change).
"""
from .save_image import ComfyFarmSaveImage

NODE_CLASS_MAPPINGS = {"ComfyFarmSaveImage": ComfyFarmSaveImage}
NODE_DISPLAY_NAME_MAPPINGS = {"ComfyFarmSaveImage": "ComfyFarm Save Image"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
