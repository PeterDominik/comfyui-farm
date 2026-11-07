# ComfyFarm menu and panel. This folder must be on NUKE_PATH (init.py adds the client); the
# farm's address comes from the shared folder's manager.json, $COMFYFARM_URL, or the
# panel's Farm field.
import nuke

import comfyfarm_nuke

comfyfarm_nuke.register_panel()   # Windows > Custom > ComfyFarm, restored with workspaces

m = nuke.menu("Nuke").addMenu("ComfyFarm")
m.addCommand("Open panel", comfyfarm_nuke.show_panel)
m.addCommand("Load finished job...", comfyfarm_nuke.load_job)
m.addSeparator()
m.addCommand("Save as Nuke Publish Template...", comfyfarm_nuke.save_as_template)
m.addCommand("Open Nuke Publish Template...", comfyfarm_nuke.open_template)
