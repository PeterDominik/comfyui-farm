# 0036. The generic route for a new kind of output; files first

Date: 2026-11-21. Status: accepted.

## Context

Phase 5's gate is that a new output type needs one handler file and no core edit. After
0034 and 0035, frames and texture sets have their handlers, but each still has its own
fields: its part of the head (`writes.frames`, `writes.textures`), its prefixes in the
runner's request, and its keys in a job's outputs. A new kind would need all of these
added to the core. The plan names three new kinds: file, movie and model.

## Decision

- **One generic route**, which any kind other than frames and texture sets takes. Its
  code is `base.Generic`, and a kind is a subclass naming `kind`: `artifacts/file.py` is
  six lines. Each part of the farm handles the route once, for every kind:
  - **The head.** `writes.<kind>` is accepted for every handler on the route and read by
    that handler (`head()`). For ComfyUI, `runner.outputs.<kind>` names the node that
    writes it (`node`, `class`, `prefix_input`). A texture set writes only its maps.
  - **The plan.** Next to frames, the files go into the frames' folder with their
    version. Alone, they take the first version of the output folder that no file of that
    name, nor a queued job, has. They are named after the job: `paths.frame_name` up to
    its frame number (`abc_0010.gputest_v001`), so they follow the farm's naming without
    a setting of their own.
  - **The runner.** It is told `writes.<kind>: {dir, prefix, ext}`. A process writes its
    files into `dir`, each named `<prefix><anything>`. A ComfyUI node gets the prefix,
    and its files are what ComfyUI lists for it. The Runner seam gains
    `files(run, result, kind, spec)`. With `fetch()`, which the process runner now has
    too (a rename), the agent gets each file.
  - **The agent.** It removes an earlier attempt's files before the run. After it, it
    names each file `<base><anything>` in the folder. A run that writes none fails, since
    its definition said it would.
- **A job writes several kinds.** A definition and a job get a list of handlers: frames
  or a texture set first, which choose the folder, then the generic ones in that folder.
  What they give is merged: their `writes` side by side, their `artifacts` one after the
  other.
- **Outputs listed by kind.** Every handler has `record(outputs)`, so a job's outputs
  carry `artifacts`: one entry per kind, with its files (frames: pattern, range and
  preview; a texture set: its maps; a file: its paths). The keys clients read today stay
  as they are. A texture set's outputs list their maps too. The dashboard shows the files
  of the other kinds under the job, and names them on the plan line.
- **file first.** `writes.file` is any other files a run writes, such as a report. The
  GPU test writes its report as one: `<source>.gputest_vNNN.json` beside its frames,
  where it used to write `gpu_test.<job>.json` itself.

## Consequences

- Adding movie and model is a file each in `comfyfarm/artifacts/`, with nothing else
  edited. That is the next step, and the gate.
- Frames and texture sets keep their own route: clients and publishing read their
  fields. Moving them onto the generic route would be a later move, if ever wanted.
- A ComfyUI node writing a kind on the route is fetched from ComfyUI's output folder, as
  the run's own preview video is. A node writing straight into the farm's folder
  (`path_input`) isn't supported on this route.
- The GPU test's fingerprint changes (its process.yaml and run.py), so every machine
  needs the same checkout before it runs.
