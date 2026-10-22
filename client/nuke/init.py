# Makes ../comfyfarm_client.py importable, so only this folder needs to be on NUKE_PATH.
import os

import nuke

nuke.pluginAddPath(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

# Studio farm address. Anyone who sets COMFYFARM_URL themselves keeps their own.
os.environ.setdefault("COMFYFARM_URL", "http://wrk59467w:8000")
