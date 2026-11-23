# 0051. A workflow as a node in the Nuke script

Date: 2026-11-22. Status: accepted.

## Context

Until now a job is submitted from the panel's form, and its result comes back as a Read
next to the source. The form's values live in the panel, not in the script: a comp that
uses farm results doesn't say what made them, and running the same workflow again means
filling the form again. Artists asked for the submission itself to be a node, made from
the workflow's card, that they can wire, save with the script and submit again.

## Decision

- **A Group, built from the workflow's definition** (`GET /workflows`, the schema the panel's
  form is built from), by `client/nuke/comfyfarm_node.py`. One knob per parameter, with
  the farm's labels, defaults, ranges and help. The definition the node was built from is
  saved in a hidden knob.
- **One input arrow per image input**, the primary first (input 0). An input takes a Read or
  Write, through Dots, as the panel's *From selected* does. Anything else is refused at
  Submit: the farm reads files, and rendering a branch locally first would be slow and
  hidden. A path typed into the input's *path* knob (or a ShotGrid Version link) is used
  instead of the connection.
- **The node is its result.** Inside, a Switch passes the primary input through until the
  picked version is done, then shows its frames from a Read. A texture job shows one map
  at a time (*Map*), with missing UDIM tiles read as black, as the panel's map Reads do.
- **Every Submit is a new version**, as on the farm. The node keeps a small record of each
  of its jobs (saved with the script); *Version* picks the one it shows.
- **Publishing as in the panel:** *Publish when done* with the same fields, and *Publish
  version* for a finished one.
- **Following jobs:** one background thread for every node, started by Submit. A reopened
  script doesn't ask the farm on its own: *Refresh* asks about the node's jobs and follows
  the running ones again. It also says when the workflow changed on the farm; *Rebuild*
  then rebuilds knobs and inputs, keeping values, connections (by input name) and versions.
- **Expiry:** with the sweeper on (`retention.enabled`), a finished version shows when its
  frames go, and *Expired* after; the farm doesn't send this per job, so the client reads
  `/sweeps` and counts from the job's `finished_at`.
- **Two ways in:** the panel's *Create node* (the form as filled in, wired to the node the
  input came from or that reads the typed path) and Nodes/Tab → *ComfyFarm → Create Node →
  <workflow>* (the farm's defaults, wired to the selected Read/Write nodes). The menu is
  filled in the background, so a farm that's down doesn't slow Nuke's start.
- **Submitting goes through `PanelModel`**, so the panel and the node send the same request
  for the same values.

## Consequences

- Nothing changes on the farm or for artists who only use the panel; the node is new and
  additive. There is no config switch: the menu entry and the button are the opt-in.
- A script with farm nodes opened without the client installed keeps its last frames (the
  Read inside), but its buttons fail until the client is on `NUKE_PATH`.
- A version deleted by the sweeper reads as an error (not black) on purpose, and its status
  says *Expired*: a silently black plate would be worse.
- The node is the first piece of the A1 host adapter's shape: what's Nuke in it is the
  knobs, the inputs and the Read; the rest is the core at the top of the file.
