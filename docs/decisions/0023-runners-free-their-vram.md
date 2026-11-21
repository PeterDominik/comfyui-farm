# 0023. A resident runner frees its VRAM on request, and only when idle

Date: 2026-11-21. Status: accepted.

## Context

ComfyUI is resident: it keeps its models in GPU memory between jobs, which is what makes a
second run of a workflow warm. Phase 3 puts a process runner on the same card, and its job
needs that memory. Peter has freed VRAM with a clear-all node inside a workflow; ComfyUI
also has a `/free` endpoint that unloads every model and empties torch's cache.

## Decision

- Every runner has `release()` (runners/base.py): free what it keeps on the card while
  idle, and say how much it held before and after.
- The ComfyUI runner calls `/free` (unload models, free memory) and waits up to 30 s until
  torch holds less than 1 GiB (`torch_vram_total` in `/system_stats`). It refuses while
  ComfyUI has any prompt running or queued, the farm's or anyone's: ComfyUI frees between
  prompts, and a prompt queued behind would load the models again.
- The agent refuses while the farm has a job in the runner, and says to drain the machine
  first. It reports every attempt with its status (`released`: when, ok, before, after, why
  not), with the GPUs read again when gpu_probe is on.
- For now a person asks for it: Free VRAM on the Machines tab, through the manager
  (`POST /api/v1/workers/{name}/free-vram`, admins only once API keys exist), which passes
  it on in the agent's next check-in. The GPU claim calls the same `release()` before
  another runner uses the card.

The endpoint runs a fixed action and takes nothing to run. Until API keys exist, anyone who
can reach the manager can press it; the cost is the next job loading its models cold.

## Consequences

The release can be tried on the pilot machines before the process runner depends on it, and
gpu_probe shows the card's memory before and after.

The next job of a released machine is cold. The ETAs don't know a release happened, so that
one run may take longer than its estimate.
