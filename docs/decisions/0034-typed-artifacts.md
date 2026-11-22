# 0034. Typed artifacts: one handler per kind of output

Date: 2026-11-21. Status: accepted.

## Context

Phase 5: outputs become typed artifacts, one handler per type. Its gate is that a new output
type needs one handler file and no core edit. The plan names the types: sequence and
texture set (ported), then movie, model and file (added). Phase 6 gives each artifact an
expiry (0006).

Today each kind of output is spread over the core:
- the manager's `Farm.plan` picks the folder and the names of frames, and `_plan_textures`
  those of a texture set;
- the agent empties a texture set's scratch folder before the run;
- the agent's `_collect_outputs` renames frames, copies the run's own video or makes the
  preview movie, and writes texture maps as EXRs.

## Decision

- **A handler per kind,** in `comfyfarm/artifacts/`. Each is a module named by its key under
  the head's `writes:` (`frames.py`, `textures.py`), defining `HANDLER`. The package
  finds them by listing its modules, so a new kind is one more file there.
- **Two halves in one handler** (`artifacts/base.py`):
  - The manager's half: `writes(wf)` says whether a definition writes this kind, and
    `plan(plan, ctx)` works out where its files go and what they are called.
  - The agent's half: `collects(job)` says whether a job writes this kind, from the job
    alone. `prepare(ctx)` runs before the run, and `collect(ctx)` after it, returning the
    outputs and the warnings.

  A definition and a job each have one kind; the handlers' tests don't overlap.
- **Handlers import neither side.** They import neither the manager, the agent, a runner,
  ComfyUI's environment nor the web stack: what they need comes in a context. The manager
  passes its config, the job id, the scratch root and what is reserved. The agent passes
  the job, its config, the run, its runner and result, and how to report a stage. Errors
  are `PlanError` (with the HTTP status the manager answers) and `PrepareError` (the job
  fails with its message). `tests/test_layout.py` holds this.
- **First step, code moved only.** Frames (with the preview movie) and texture sets move
  out of `Farm.plan`, `Farm._plan_textures` and `Agent._collect_outputs` into their
  handlers, unchanged. The same folders, names, outputs, warnings and errors result.
- **Next steps.** The core still reads kind-specific parts of the plan: the frozen info
  (`name_tokens`, `textures`, `collect`), the dashboard's summary (`maps`), a texture set's
  batches, and what the runner is told (`frames_prefix`, `raw_dir`). The next step makes
  the core blind to the kind: those come from the handler, and each job's outputs are
  listed by kind beside the keys clients read today. Then movie, model and file are
  added, each one file: the gate.

## Consequences

- Reading how one kind of output works means reading one file.
- The handlers' own behaviour stays tested where it was: `test_agent`, `test_textures`,
  `test_sets`, `test_e2e`. `test_artifacts` holds the seam: every handler is a file named
  by its kind, with the members both sides call, and the repo's definitions get their kind.
- A runner still decides how its backend writes each kind (a ComfyUI save node, a
  process's files). The handler decides where the files end up, and what the farm does
  with them afterwards.
