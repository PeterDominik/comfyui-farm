# 0038. A provenance file beside each job's outputs

Date: 2026-11-22. Status: accepted.

## Context

0010 says a provenance file goes beside each output. 0006 says a sweeper will delete
expired outputs, and only what the farm made. Today nothing on disk says which job wrote a
file, how it was made, or which files in a folder are the farm's. The job record says some
of it, but it lives in the manager's database, and 0006 deletes jobs with their outputs.

## Decision

- **One JSON file per run, beside its outputs.** The agent that ran the job writes it once
  the outputs are collected, before it reports the job done.
- **Named like the outputs.** Each handler names it (`provenance()` on the handler
  interface), and the job's first kind decides:
  - Frames: `<source>.<tag>_v001.provenance.json`, beside `<source>.<tag>_v001.1001.png`.
  - Kinds on the generic route: the same, from their own base name.
  - A texture folder is the run's own, so `provenance.json` there. Each batch of a set
    writes `provenance.<batch>.json` (for example `provenance.002.json`).
- **What it says:**
  - Who and what: the job, the attempt, the user and the name.
  - The definition: its name and the fingerprint pinned at submit. Its code is in the
    reviewed repo, at that fingerprint.
  - The shot.
  - Where it ran: the machine, its GPU and driver, and the farm's build.
  - What ran it, from the runner (`about()` on the runner interface):
    - for ComfyUI, its version, commit, Python and torch, and how the machine differed
      from the farm's environment file when last checked;
    - for a process, the environment, the build it ran in and where that came from (a
      release in the bank, or a checkout).
  - When: dispatched, started and finished, in local time with the offset, and the run's
    seconds.
  - The inputs: the input with its frame range, the other inputs, and the ShotGrid Versions.
  - The parameters.
  - The output folder, the files the job wrote there, the job's `artifacts`, and the
    warnings.
- **Each kind lists its files** (`written()` on the handler interface): every frame, the
  preview movie, every map of every tile, every file on the generic route. The sweeper
  (phase 6) will delete only files a provenance file lists, so a file the farm didn't
  write is never its to delete.
- **Studio paths**, the portable form (`fileio/wire.py`), so it reads the same from Linux and
  Windows.
- **Written whole or not at all:** to a temporary file in the folder, then moved into place.
  It is made readable by everyone, like the outputs.
- **Never a reason to fail the job.** A file that can't be written is a warning that names
  it; the outputs stand.
- **Off by default** (`provenance.enabled` in the manager's config). It is frozen into each
  job at submit, like `preview`, so a job queued before a change keeps what it had.
- **No secrets.** It holds what the job record holds. The farm's parameters are prompts,
  seeds and paths, not keys.

## Consequences

- A file found on disk months later says where it came from, without the database.
- A failed run writes none. Its partial files are not listed, so a sweeper leaves them.
  The next attempt clears its own kind's files, as it does today.
- The provenance file takes the version it sits in. A folder holding only that file still
  counts as taken, so the next run doesn't reuse the version.
- Writing to show storage adds one small file and a rename per run.
- The EXR stamp (phase 6) points from each map to this file. The sweeper reads these files.
