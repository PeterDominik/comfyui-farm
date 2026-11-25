# 0063. Batch jobs: after artists' jobs, preempted with a file, resumed from a folder beside their outputs

Date: 2026-11-24. Status: accepted.

## Context

Decision 0004 split jobs into `interactive` (an artist is waiting) and `batch` (training, long
runs; may be preempted and resumed), and left preemption to phase 11. A definition's `class`
has been read and checked since phase 2, and nothing used it.

Phase 11's step 6 is the batch class:

- `class: batch` sorts after interactive jobs.
- An artist's job that can't find a free GPU preempts a batch job: its agent writes a
  `preempt` file, and the runner saves a checkpoint within 2 minutes and exits.
- The job is requeued, not counted as a failed attempt, and resumes from a resume folder
  beside its outputs.
- Draining a machine preempts its batch jobs too.
- Process jobs only; off by default.

The gate is an artist's job preempting a batch test job, which resumes on another machine and
finishes. Step 7, LoRA training with ai-toolkit, is the first real batch job.

Today a process run already stops when a `cancel` file appears (0025). Its run folder is on
the machine that runs it, so a run that goes on elsewhere can't read it. The desk prepares a
second job ahead on a machine whose GPU is busy (`PER_GPU`).

## Decision

- **Process definitions only.** `class: batch` is refused for a ComfyUI or API definition,
  and for one that writes a texture set. Only a process can save where it is and go on from
  there, and a texture set's batches are runs of their own.
- **The class is frozen at submit** in the job's info (`class`), as everything else about a
  job is.
- **The queue.** Artists' jobs come before batch jobs, whatever a batch job's priority. A
  batch job takes only a free GPU, never a place prepared ahead. Nothing is prepared ahead
  behind a batch job: it may hold the card for hours.
- **The resume folder** is in the job's output folder and named like its outputs, as its
  provenance file is: `abc_0010.batchtest_v001.resume/`. It is on show storage, so a run on
  another machine reads what the last one saved.
  - The manager names it at submit (`info.resume_dir`); `job.json` gives it to the command as
    `resume_dir`.
  - The agent makes it before the run starts.
  - The manager removes it when the job ends, however it ends. The manager acts only on a
    job's current run, so a stale run on a machine that lost touch can't remove the folder of
    the run that took over. It removes only a folder ending `.resume` inside the job's output
    folder, in a thread of its own, and logs one it can't remove.
- **Preempting is a file**, as cancelling is. The agent writes `preempt` in the run folder;
  `job.json` names it (`preempt`). The command saves what it resumes from and exits. The
  supervisor gives it `process_runner.preempt_seconds` (120), then stops it as at a cancel,
  and the next run goes on from what it saved last. Once the file is there:
  - the run counts as preempted however it ends, unless it is cancelled;
  - its time limit stops counting, since saving may take longer than what was left of it.
  `exit.json` says `preempted`, and the agent reports `preempted` to the manager.
- **The protocol.** The check-in reply's `preempt` lists `{job, attempt, why}`. The agent lists
  such a job with `preempting` until it ends, so it is asked once, as `cancelling` works. A job
  still being prepared stops at once, having run nothing. `job.json` stays contract 1: its
  new fields are optional.
- **Queued again, not failed.** A preempted job goes back to the queue with the attempt it was
  counted given back, its stage saying it resumes on the next free GPU. Its info keeps each
  preemption: when, where and why. Its timeline says *preempted* and why. A cancel asked
  meanwhile ends it cancelled. A batch job lost with its machine is queued again as any job
  is, counted, and goes on from what it saved last.
- **Its outputs.** Frames a run writes stay in the output folder for the run that finishes.
  Every other kind of output is cleared when a run starts, as for any job (0036), so the run
  that finishes writes it. What a run needs to go on belongs in the resume folder.
- **This pull request preempts a drained machine's batch jobs**, with `batch.preempt` in the
  manager's config, off by default. Off, they finish where they are, as any job on a drained
  machine. Rollouts and syncs drain machines too, so their machines are freed sooner.
- **The runner seam:** `Preemptible`, beside `Syncable`, has `preempt(run)`; only the process
  runner has it. The host has `run_preempted(run)`.
- **The farm's test:** `workflows/batch_test` (hidden) counts steps holding some of the card,
  saves where it is every few steps and when preempted, and reports each run.

## Consequences

- The next pull request has an artist's job preempt a batch job when it finds no free GPU,
  and carries the gate (0064).
- Each run of a batch job has its own time limit (`limits.max_minutes`), not the job as a
  whole.
- The queue's ETAs don't know yet that nothing waits ahead behind a batch job, so a job behind
  one may be shown on its machine.
- A command that ignores the `preempt` file is stopped after 2 minutes and starts over from
  its last save, or from nothing. A command that crashes as it saves is preempted all the same,
  and the next run finds what it can.
- The manager now removes a folder on show storage. A folder it can't remove (permissions) is
  left there, and the log says so.
- `batch_test` sorts before `gpu_test` in Python Local › Tests, by label.
