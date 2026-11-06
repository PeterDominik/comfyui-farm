"""Publishing a job's output through the studio's pipeline (chunk 8).

A workflow with a `publish:` section in its manifest can be published. The artist picks
a Nuke template (publish.templates/<workflow>/<name>.nk), the step, show and shot, and a
description, at submit ("publish when done") or later on a finished job. The manager
then copies the template to the staging folder with its placeholder Reads pointed at the
job's files (publish.nuke) and dispatches Daiquiri's update_and_render_nuke on it
(publish.dispatch), which renders the template's pipeline Writes on Tractor and makes a
daily for each (in Artist Review).

  nuke.py       templates: their placeholder Reads, checking and staging them
  dispatch.py   the dispatchGraph command, run in the pipeline's environment
  publisher.py  the manager's side: options, checking a request, running it off the
                event loop, the publishes table (manager/db.py)"""
