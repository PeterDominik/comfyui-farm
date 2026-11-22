# 0035. The core leaves each kind of output to its handler; texture sets stay the core's

Date: 2026-11-21. Status: accepted.

## Context

0034 moved where frames and texture sets go, and how the agent collects them, into their
handlers. The core still read their parts of a plan:
- the job's frozen info: the frames' prefixes, names and what the agent collects;
- what the runner is told: the frames' prefixes and the maps' scratch folder;
- the dashboard's summary: a texture set's maps;
- the run-time estimate's size: a texture set's tiles.

0034 also said the outputs would be listed by kind in this step.

## Decision

- **Three more hooks on a handler,** each fed by the plan its `plan()` made:
  - `info(plan, ctx)` is what the job keeps of it, frozen at submit for the agent;
  - `request(plan)` is what the runner is told;
  - `summary(plan)` is what the dashboard shows before submitting.

  The planner merges them in. The `units` a handler puts in the plan size the estimate.
- **Frames and texture sets move onto them unchanged.** The frames' prefixes, names, the
  preview movie's settings and what the agent collects are the frames handler's. The maps
  are the texture handler's. The planner's source names none of the frames' fields, and
  `test_artifacts` holds that.
- **Texture sets stay in the core.** That covers the set row, its batch jobs, their
  refresh and retry, and their size in the estimate. A set is several jobs filling one
  folder, which is how jobs are queued, not how one job's files are written. Today only
  texture sets do it. The other texture-specific reads are left as they are too: tiles
  allowed to have gaps when the input is read, and the Output folder's placeholder.
  No new kind needs any of them.
- **Outputs listed by kind come with the first new kind.** A list of each job's
  artifacts changes what clients receive, so it goes in the pull request that adds a kind
  (file), not in this one, which only moves code.

## Consequences

- A job's info no longer carries the other kind's empty fields. A texture job has no
  frames prefix or `collect`; a frame job has no `textures`. Nothing read them.
- Phase 5's steps become:
  - the generic route for a new kind, with `file` as the first and each job's outputs
    listed by kind;
  - movie and model added, each one handler file: the gate.
