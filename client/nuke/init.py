# Makes ../comfyfarm_client.py importable, so only this folder needs to be on NUKE_PATH.
import os

import nuke

nuke.pluginAddPath(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
