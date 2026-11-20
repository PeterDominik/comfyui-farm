# 0019. The head says what a definition takes and writes; the runner block, which node does it

Date: 2026-11-20. Status: accepted.

## Context

This amends 0015, which put a process.yaml's `outputs` and `textures` sections, and each
input's loader, wholly in the runner block. 0018 then found that the manager plans every job with part
of them: whether an input's folder is read whole, what the frames are written as, whether
the run makes its own preview video, and the texture maps by channel. To plan from the
head alone, as phase 2's gate asks, the head has to say those things. No workflow uses
process.yaml yet, so its format can still change at no cost.

## Decision

The head says what a definition takes and writes:

- `own_folder: true` on an input whose runner reads every file in its folder, so the farm
  gives it a folder of its own. For ComfyUI that is a loader such as VHS Load Images Path;
  the runner refuses a process.yaml whose loader reads a whole folder without it.
- `writes:` with `frames: {ext: png}` (it writes a frame sequence of that type), `preview`
  (true, the default: the farm makes a preview movie of the frames; false: none; own: the
  run makes its own video), and `textures:` (`name`, `dir`, `ext`, `batch` and `maps`, each
  map with its `channel`, `name`, `colorspace`, `color` and `bits`).

The runner block says which node does it: `runner.outputs` (`root_node`, `frames` and
`preview` with their nodes and inputs) and `runner.textures` (`path_input`,
`prefix_input`, and `maps` keyed by channel, each with its `node` and `class`).

Each half without the other is refused with a message naming the missing one, as is a key
in the wrong half. A process.yaml no longer has a top-level `preview`, `outputs` or
`textures`.

## Consequences

Both files still load into one Workflow, and a manifest converted with
`tools/manifest_to_process.py` keeps its fingerprint. The exception is a manifest whose
loader reads a whole folder: the converter adds `own_folder: true`, which changes it.

The next step converts the five workflows to process.yaml. After that, the manager can
read what it plans with from the head alone, and the move that splits `workflows.py` has
a line to cut along.
