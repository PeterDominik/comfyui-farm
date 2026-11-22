# 0041. The sweeper lists only what provenance files name, and starts as a dry run

Date: 2026-11-22. Status: accepted.

## Context

0006 has a sweeper delete expired outputs and their jobs. It starts in dry-run and touches
only what the farm made. Output folders are shared: a shot's folder holds every version
of every job for that shot, and other people's files can sit beside them. A file's name
alone doesn't say who wrote it. Since 0038, each finished job's provenance file lists every
file it wrote.

## Decision

- **A job expires** `retention.keep_days` (3) after it finished, or after it was last sent to
  Tractor to be published (a `dispatched` publish), whichever is later. A job with a publish
  on its way (`queued`, `dispatching`, or asked for at submit and not made yet) is kept.
- **Only what a provenance file lists**, and only when that file:
  - is the one the job's outputs name;
  - sits in the job's output folder;
  - is a provenance file of this format (`comfyfarm_provenance: 1`);
  - names this job.

  If any of these fails, none of that job's files are touched.
- **Each listed name must stay in the folder:** relative, with no `..` and no drive, and it
  must be a plain file, not a folder or a link. Other names are left and reported.
- **Files nobody listed stay.** That covers other jobs' files, people's files, and the
  outputs of jobs without a provenance file: provenance off at submit, or a failed or
  cancelled run, whose partial files were never listed. For those jobs only the record
  would go.
- **A texture set is one unit:** it expires as a whole, and its batches' provenance files
  together list its files.
- **A dry run first.** Once a day at `retention.at` (or on start, if that day's run was
  missed) the sweeper writes a report to `data/sweeps/<date>.json` and deletes nothing:
  - each expired job, its folder, the files it would delete, their size;
  - files already gone, whether the folder would be empty, every problem.

  There is no delete code yet. Step 5 adds it with its own setting, after the gate.
- **Off by default** (`retention.enabled`). A run can be asked for from the manager's host
  only (`POST /api/v1/sweeps`), like everything that will one day delete files. Reading
  the reports is open.

## Consequences

- The gate is these reports: a week of them that list only files the farm wrote.
- Outputs of jobs from before provenance was on are never deleted by the sweeper. They are
  cleaned by hand, or left.
- Reading every expired job's provenance file once a day is a few hundred small reads at
  pilot volumes.
- Records the sweeper would delete keep their metrics rows (0040).
