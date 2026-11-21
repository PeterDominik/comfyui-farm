# 0027. The test process: a torch job that checks the process runner and the GPU claim

Date: 2026-11-21. Status: accepted.

## Context

Phase 3's last step is a test Python process. The process runner (0025) and the GPU claim
(0026) pass their tests against stand-ins, but neither has run on a farm machine: the repo
has no process definition. The phase's gate is ComfyUI, the test process, then ComfyUI
again on one card with no out-of-memory, on Linux and Windows. The `gpu-test` environment
(0024) is built on all three pilot machines.

## Decision

- **`workflows/gpu_test`** is a process definition in the `gpu-test` environment. Like a
  workflow, it takes frames (a sequence, a movie or one image; EXRs converted with the
  shot's look) and writes PNGs, renamed by the farm, with a preview movie. On the GPU with
  torch it blurs, inverts or grades them. Its limit is 10 minutes. It isn't published
  (`publish: false`) and isn't in ShotGrid's menu.
- **It tests the claim:** "Hold GPU memory" keeps that much of the card while it runs. After a
  ComfyUI job, a hold of most of the card fits only if ComfyUI freed its models. If the hold
  doesn't fit, the job fails and says how much was free. "Seconds per frame" slows it down,
  so a cancel or the time limit can be tried on a running job.
- **It reports what it found:** `gpu_test.<job>.json` beside the frames holds the card, the
  memory free when it started, what it held and its peak. If less than 80% of the card was
  free at the start, the job gets a warning.
- **job.json gives each input's `files`,** in order, with their frame `numbers`. These are
  the same files the farm's ComfyUI loaders read: a converted input's PNGs, or the frames
  of a pattern. A command doesn't have to read frame patterns itself. The field is optional,
  so job.json is still contract 1.
- **A command's `result.json` may hold `warnings`,** a list of lines that become the job's
  warnings. The agent asks each runner for them (`Runner.warnings`); ComfyUI has none.
- **The script is checked in CI without a GPU.** A stand-in torch backed by numpy
  (`tests/fake_torch`) runs it for real: the blur (a flat image stays flat, noise is
  smoothed), invert and grade (exact), holding the card and running out of it, no GPU, and
  stopping when asked.

Two faults this step found are fixed with it:

- **A cancelled command that stopped on its own was reported as succeeded.** A command that
  does what the contract asks, exiting when `cancel` appears, ended with exit code 0 before
  the supervisor's next look. The supervisor now counts any end after `cancel` as cancelled.
- **`publish: false` in a definition was refused** by the head check, which read a step from
  it. No definition had used it before.

## Consequences

- With the process runner on, the Nuke panel and the dashboard list the GPU test for every
  machine. A machine with the runner off says why it can't run it.
- To run the gate on a machine, have it hold most of its card after a ComfyUI job: 20 GB on
  the 3090, 28 GB on the 5090, 44 GB on the A6000. README.md ("The GPU test") has the steps.
- The test reads 8-bit RGB: 16-bit frames are read as 8 and alpha is dropped. It is a test
  of the farm, not of image quality.
