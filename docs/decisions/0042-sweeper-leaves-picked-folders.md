# 0042. The sweeper leaves folders the artist picked

Date: 2026-11-22. Status: accepted.

## Context

0006 says the sweeper touches only folders the farm created. 0041 limits it to the files
a job's own provenance file lists, wherever the job wrote them. Artists can pick the
output folder, and the first dry run on the pilot showed about 60 of 217 expired jobs
written into the artists' own comp work areas (for example
`/rdo/shows/guem2609/shot/0040/comp/nuke/comp/jyu/comfyfarm_outputs`). A comp may read
those frames directly, so deleting them after three days could break it.

## Decision

- **Only the farm's own renders folders are swept:** a job's files are listed for deletion
  only when its output folder is under `paths.output_root` for its show, and the artist did
  not pick it (`info.output_dir_picked`).
- **A job whose artist picked its folder keeps all its files,** even a folder picked inside
  the renders root. So does a job whose folder is outside the renders root (jobs from
  before the flag was recorded), or whose show isn't known. Only its record would go.
- **Why files stay is kept apart from what went wrong.** Each report entry has
  `files_stay` for the rule (no provenance file, a folder the farm didn't make) and
  `problems` for anomalies. The gate reads `problems`.

Chosen by Peter after the first dry run.

## Consequences

- Outputs in artists' folders are theirs to clean up, as the rest of their work area is.
- A changed `paths.output_root` leaves jobs under the old root unswept, which is the safe
  side.
