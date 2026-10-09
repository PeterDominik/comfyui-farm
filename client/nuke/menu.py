# Add these lines to your ~/.nuke/menu.py (or a studio menu.py).
# comfyfarm_nuke.py and comfyfarm_client.py must be on NUKE_PATH,
# and COMFYFARM_URL must be set, e.g. http://comfyfarm01:8000
import nuke

import comfyfarm_nuke

m = nuke.menu("Nuke").addMenu("ComfyFarm")
m.addCommand("Alpha from selected Read", comfyfarm_nuke.submit_selected)
m.addCommand("Load finished job...", comfyfarm_nuke.load_job)
