# ComfyFarm menu and panel. This folder and ../comfyfarm_client.py must be on NUKE_PATH,
# and COMFYFARM_URL set (or typed once into the panel's Farm field).
import nuke

import comfyfarm_nuke

comfyfarm_nuke.register_panel()   # Windows > Custom > ComfyFarm, restored with workspaces

m = nuke.menu("Nuke").addMenu("ComfyFarm")
m.addCommand("Open panel", comfyfarm_nuke.show_panel)
m.addCommand("Submit selected Read...", comfyfarm_nuke.submit_selected)
m.addCommand("Load finished job...", comfyfarm_nuke.load_job)
