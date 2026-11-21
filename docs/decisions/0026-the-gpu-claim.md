# 0026. One runner's runs at a time on the card; the last one frees it first

Date: 2026-11-21. Status: accepted.

## Context

With the process runner (0025), an agent has two runners on one card. ComfyUI is
resident: it keeps its models loaded between jobs, which is what makes its second run of
a workflow warm. The pilot's 24 GB 3090 holds 14 GB of ComfyUI's models after an LTX job
(0023), so a process started next to it can run out of memory. Until now, `max_inflight`
counted both runners' runs together. That kept two jobs from running at once, but not the
models in memory. The plan's phase 3 gate is ComfyUI, the test process and ComfyUI again
on one card with no out-of-memory, on Linux and Windows. Each pilot machine has one GPU and
one agent.

## Decision

- **The card is claimed by runner, not by job.** A run takes a place on the card only when
  no run of another runner holds one. Up to `max_runs` of one runner share it:
  `max_inflight` for ComfyUI, which queues its prompts itself, and one for the process
  runner.
- **First come, first served between runners.** A run doesn't start ahead of an earlier
  run of another runner that is still waiting, even when its own runner has room. A
  process job is not starved by a stream of ComfyUI jobs, nor the other way round. A run
  that stops waiting (cancelled, dropped) lets the ones behind it on.
- **The runner that used the card last frees it first.** The agent keeps which runner
  that was: at start, ComfyUI, since it may hold models. When another runner's run takes
  the card, the agent calls that runner's `release()` (0023): ComfyUI unloads its models
  and empties its cache, and the process runner holds nothing. A ComfyUI that isn't
  running holds nothing either and isn't asked.
- **If it can't free the card,** the run waits, its stage saying why (for example, ComfyUI has
  prompts queued that aren't the farm's), and asks again every 10 s. After
  `claim_wait_seconds` (300) it starts anyway. The job then carries a warning that it shared
  the card, and a failure message ends with it, so an out-of-memory error explains itself.
- **Each release is reported** with the machine's status (`released`), as Free VRAM's are.
  Free VRAM itself still refuses while ComfyUI has farm jobs, not while a process runs.
- **Nothing changes for an agent with only ComfyUI's jobs:** the card never changes
  runner, and nothing is freed.

## Consequences

- ComfyUI's first job after a process loads its models again. On a machine that alternates,
  every switch costs that load. The manager doesn't place by runner yet, so it may
  alternate more than it needs to.
- One card per agent. A machine with several GPUs runs an agent per GPU, each with its own
  ComfyUI; its process runner would also need that card's `CUDA_VISIBLE_DEVICES`, which
  comes with placing by GPU.
- The claim is within one agent. Another program on the card (an artist's session on the
  5090) isn't the farm's to free; `gpu_probe` shows it.
- The claim acts only when the process runner is on (`process_runner.enabled`, off by default).
