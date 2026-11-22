# 0040. One metrics row per finished job, written where every change to a job passes

Date: 2026-11-22. Status: accepted.

## Context

0006 keeps one small row per job after the sweeper deletes the job: process, show, user,
GPU, timings, outcome; no prompts and no paths. Jobs finish in many places: the agent
reports done, failed or cancelled; the manager cancels a queued job, fails one whose
machine went quiet, or works out a texture set's status from its batches. The job record
names the machine that ran it, but not its GPU.

## Decision

- **A `job_metrics` table** in the manager's database, one row per job, keyed by its id.
- **Written in the job store, in `_changed`**, the same place the timeline is written. Every
  change to a job passes through there, so no way of finishing can forget the row. A job
  that finishes again (retried, or a set's failed batches run again) replaces its row, so
  the row has its last outcome.
- **What a row holds:**
  - the job, its kind (job, set or batch) and a batch's set;
  - the workflow and its fingerprint;
  - the user and the show;
  - the machine and its GPU type;
  - the outcome, its attempts, and its units (frames or tiles, as the ETAs count them);
  - five times: submitted, given to a machine (the last `preparing` in its timeline),
    handed to the runner, started, and finished.
- **Never in a row:** params, prompts, paths or error messages. Errors name files, and a
  row outlives the job's outputs.
- **The GPU type is noted on the job** (`jobs.gpu`) when the desk gives it to a machine, as
  the ETAs name it (ComfyUI's device without its number and allocator).
- **Jobs already finished** when the table first exists get their rows when the manager
  starts, before the sweeper can delete them. Their GPU type is empty.
- **A row that can't be written is logged**, and the change to the job stands.
- **Read through `GET /api/v1/job-metrics`**, by when jobs finished, as JSON or CSV. Each row
  gives local times and how long each part took (`manager/metrics.py`).
- **No setting.** CLAUDE.md asks for new behaviour to be off by default. The row changes
  nothing a job does or an artist sees, nothing leaves the manager, and the sweeper (phase 6)
  depends on the rows existing before it deletes anything. Prometheus `/metrics` stays in
  phase 8.

## Consequences

- How much the farm ran, for whom and on what stays answerable after the jobs are deleted.
- A set has a row and so does each of its batches. Summing GPU time means summing batches
  and jobs, not sets.
- The table grows by about a hundred bytes per job and is never pruned. At pilot volumes
  that is years before it matters.
- Old rows have no GPU type. The machine says which one it was.
