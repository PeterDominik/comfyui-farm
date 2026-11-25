# 0064. An artist's job with no free GPU stops one batch job, the fastest for it, and waits for that GPU

Date: 2026-11-24. Status: accepted.

## Context

Decision 0063 gave the farm batch jobs: process definitions with `class: batch`. They queue
after artists' jobs and take only a free GPU, and nothing is prepared ahead behind one. A
batch job stops when a `preempt` file appears, having saved what it resumes from, and is
queued again to go on elsewhere. With `batch.preempt`, a drained machine's batch jobs stop.

Phase 11's step 6 also says that an artist's job that can't find a free GPU preempts a batch
job. Its gate is an artist's job preempting a batch test job, which resumes on another machine
and finishes. Four questions are left:

- When has an artist's job found no free GPU?
- Which batch job stops?
- How many?
- What does the artist's job do meanwhile?

Placing jobs (`Desk._claim`) gives each queued job the best slot on a machine that may run it:
- a free GPU first;
- once every GPU is busy, a place prepared ahead behind the job a machine is running.

A batch job saves and stops within `process_runner.preempt_seconds` (2 minutes) of its agent
hearing.

## Decision

- **No free GPU** means the job's best slot is none, or only a place prepared ahead. In both
  cases it stops a batch job, when one may make way. Waiting behind another artist's job,
  which may run for minutes, isn't better than a batch job saving and stopping.
- **Which batch job:**
  - it is on a machine that may run the artist's job: its `requires`, its nodes and models,
    not drained;
  - nothing but batch jobs is on that machine's GPU;
  - of those machines, the one placing jobs ranks first for the artist's job (`_rank_key`): the
    fastest GPU for its workflow and size, then `agents.order`, then VRAM.
- **One batch job for one artist's job.** The desk keeps each batch job making way and which
  job it makes way for (`_preempting`). A second artist's job stops another batch job, if there
  is one. A batch job already making way is never asked for another.
- **The artist's job waits for that GPU.** It isn't prepared ahead elsewhere meanwhile, which
  would leave the freed GPU to the batch job it stopped. Its stage says which machine it waits
  for. When the batch job has stopped, the GPU is free, and the artist's job takes it as any
  job takes a free GPU: first in the queue, so the batch job, queued after it, doesn't take its
  GPU back. If another GPU frees first, the artist's job takes the best one, and the batch job
  stops anyway.
- **Told in the check-in reply**, as a drained machine's batch jobs are (0063). Placing a job
  happens in any machine's check-in. The batch job's machine is woken, so it hears in the
  check-in it is waiting in, and the reply is built after placing, so a machine hears at once
  about its own batch job. `why` names the artist's job: "carol's job 1a2b3c4d needed its GPU".
- **Never for a batch job, an eval or an API job.** A batch job doesn't stop another. An eval
  goes to its machine. An API job needs no GPU.
- **A batch job that doesn't stop is left to run.** Its supervisor stops a run 2 minutes after
  the file appears. One still running 10 minutes after it was asked (`PREEMPT_WAIT`) didn't
  hear, or can't: an agent running older code, say. It is left to run and not asked again for
  that run, with a warning in the log, and the artist's job is placed as if it weren't there.
- **The same switch:** `batch.preempt`, off by default, for this and for drained machines.

## Consequences

- With `batch.preempt` on, phase 11's step 6 is done when the gate passes on the pilot.
- An artist's job may wait up to 2 minutes for a batch job to save and stop, more than it would
  wait for a free GPU. A command that saves quickly keeps that short.
- A batch job may stop and resume often while artists submit. Each resume costs its start-up
  (loading a model, for training) and whatever it did since its last save.
- The batch job that stops isn't chosen by how much it would lose: one that saved a minute ago
  and one that saved an hour ago are the same to the desk. A batch job reporting when it last
  saved would let it choose; not until a real batch job (step 7's training) shows it matters.
- The queue's ETAs don't know about preemption yet. An artist's job that will stop a batch job
  is shown waiting on a machine, as before.
