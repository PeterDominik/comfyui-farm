# 0015. A definition is a process.yaml: a shared head and a runner block

Date: 2026-11-16. Status: accepted.

## Context

A workflow's `manifest.yaml` mixes two things: what any job needs said about it (its name,
its parameters and their limits, which machines may run it, how it is published) and how
ComfyUI is driven (the graph's file, which node each parameter, input and output goes to).
The manager is to stop reading the second kind (0002), and the runners that come next (a
Python process, LoRA training) have no graph at all. The plan's later phases also need
facts no manifest holds: a job class, where it may run, a time limit, the licences of its
weights and a reference job for evals.

## Decision

A definition folder holds a `process.yaml`:

- The head is the same for every runner: `id`, `label`, `description`, `tag`, `version`,
  `class` (`interactive` or `batch`), `targets` (`pool`, `tractor`), `requires`, `limits`
  (`max_minutes`), `models` (`file` and `licence` per weights file), `eval`, `params`
  (type, label, default, range, choices), `publish`, `preview`, `shotgrid`.
- `runner:` names its `type` and holds everything only that runner reads. For `comfyui`:
  `template`, `node_packs`, `sequence`, `outputs`, `textures`, and under `runner.params`
  the nodes each parameter drives (`targets`, `loader`, `path_node`, `empty`, `given`).

A key in the wrong half is an error that says where it belongs. `manifest.yaml` keeps
loading, as `runner.type: comfyui`, and both files become the same `Workflow`; a folder
with both does not load. `tools/manifest_to_process.py` writes one from the other.

The fingerprint (0013) of a definition with none of the new head fields is the same in
either file, so converting a workflow does not make machines disagree. Head fields count
towards it once set.

The new head fields are read and checked in this step and acted on later: classes and
limits with the process runner (phase 3), models with the policy gates (phase 7), eval
with the evals (phase 8), targets with Tractor (phase 12). Only `comfyui` is a known
runner type until phase 3; any other is refused when the definition loads.

## Consequences

The farm's five workflows stay `manifest.yaml` for now and are converted one at a time,
by pull request, when someone needs a head field. The split in the file is the line the
next step cuts along: the manager keeps the head, the runner keeps its block. Comments in
a manifest are not carried over by the tool. A definition can still only come from the
reviewed repository; nothing here is accepted over the API.

Later: 0019 moved what a definition writes (frames, preview, texture maps) and `own_folder`
into the head; the runner block keeps which nodes do it.
