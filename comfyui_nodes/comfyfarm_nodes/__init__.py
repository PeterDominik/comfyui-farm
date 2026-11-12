"""ComfyUI nodes that come with the ComfyUI farm (comfyui-farm repo).

The setup scripts link this folder into ComfyUI's custom_nodes (deploy/windows/setup.ps1
-ComfyUIDir <ComfyUI folder>, deploy/linux/setup.sh agent --comfyui <ComfyUI folder>), so
`git pull` on the worker updates the nodes too. ComfyUI loads them only at startup: each
node's description carries its version and the checkout it came from, and the agent
restarts ComfyUI (when it's idle) once a pull changed them (comfyfarm.fileio.farmnodes).

The loaders use the farm's own file handling (comfyfarm.fileio) from the same checkout.
"""
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]     # through the custom_nodes link: the checkout
if str(_REPO) not in sys.path:
    sys.path.append(str(_REPO))                 # appended: the checkout's folders never shadow ComfyUI's

from comfyfarm.fileio import farmnodes as _farmnodes  # noqa: E402

from .load_images import ComfyFarmLoadImage, ComfyFarmLoadImages  # noqa: E402
from .pro_alpha import ComfyFarmProAlpha, ComfyFarmProAlphaPick  # noqa: E402
from .save_image import ComfyFarmSaveImage  # noqa: E402

VERSION = _farmnodes.version(_REPO)
MARKER = _farmnodes.marker(VERSION, _REPO)

NODE_CLASS_MAPPINGS = {"ComfyFarmLoadImage": ComfyFarmLoadImage, "ComfyFarmLoadImages": ComfyFarmLoadImages,
                       "ComfyFarmSaveImage": ComfyFarmSaveImage, "ComfyFarmProAlpha": ComfyFarmProAlpha,
                       "ComfyFarmProAlphaPick": ComfyFarmProAlphaPick}
NODE_DISPLAY_NAME_MAPPINGS = {"ComfyFarmLoadImage": "ComfyFarm Load Image", "ComfyFarmLoadImages": "ComfyFarm Load Images",
                              "ComfyFarmSaveImage": "ComfyFarm Save Image",
                              "ComfyFarmProAlpha": "ComfyFarm Pro Alpha (SAM3 subject)",
                              "ComfyFarmProAlphaPick": "ComfyFarm Pro Alpha: pick (internal)"}
for _cls in NODE_CLASS_MAPPINGS.values():
    _cls.DESCRIPTION = f"{(_cls.DESCRIPTION or '').strip()} {MARKER}".strip()

_where = Path(sys.modules["comfyfarm"].__file__).resolve().parents[1]
if _where != _REPO:
    logging.getLogger(__name__).warning("comfyfarm_nodes: comfyfarm is imported from %s, not this checkout (%s)",
                                        _where, _REPO)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "VERSION"]
