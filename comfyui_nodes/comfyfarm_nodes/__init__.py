"""ComfyUI nodes that come with the ComfyUI farm gateway (comfyui-farm repo).

The setup scripts link this folder into ComfyUI's custom_nodes (deploy/windows/setup.ps1
-ComfyUIDir <ComfyUI folder>, deploy/linux/setup.sh agent --comfyui <ComfyUI folder>), so
`git pull` on the worker updates the nodes too (restart ComfyUI to load a change).
"""
from .save_image import ComfyFarmSaveImage

NODE_CLASS_MAPPINGS = {"ComfyFarmSaveImage": ComfyFarmSaveImage}
NODE_DISPLAY_NAME_MAPPINGS = {"ComfyFarmSaveImage": "ComfyFarm Save Image"}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
