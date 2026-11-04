# Makes ../comfyfarm_client.py importable, so only this folder needs to be on NUKE_PATH.
import os

import nuke

nuke.pluginAddPath(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

# The farm's address comes from manager.json in the shared folder this is installed in
# (the manager writes it at every start). COMFYFARM_URL, if set, wins.
