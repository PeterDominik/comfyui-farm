# 0022. Agents read their GPUs with nvidia-smi

Date: 2026-11-20. Status: accepted.

## Context

Phase 3 schedules GPUs, not machines, and adds a process runner beside ComfyUI. Until now
the manager knew a machine's GPU only from ComfyUI's own stats (torch's view of the card,
reported by the ComfyUI runner). A process runner has no such stats, a machine with ComfyUI
down reports no GPU at all, and ComfyUI's numbers don't show memory another program holds
on the card (a developer's session on the 5090, a process runner's job).

## Decision

The agent reads its GPUs itself, with `nvidia-smi --query-gpu=... --format=csv,noheader,nounits`
(`comfyfarm/agent/gpus.py`): index, uuid, name, memory total, used and free, utilization,
driver. nvidia-smi comes with the NVIDIA driver on Linux and Windows, so the agent needs no
new Python package. NVML through a Python binding (pynvml) would add one to every agent;
torch is in the runners' environments, not the agent's.

It is behind `gpu_probe.enabled` in agent.yaml, off by default, and read every
`gpu_probe.seconds` (30). The agent sends the cards with its status, and the manager shows
them on the Machines tab. Placement still goes by ComfyUI's stats: the GPU claim and the
process runner, which need the probe, switch it over in their own pull requests.

A machine whose nvidia-smi fails reports why (`gpu_error`), in words a person can act on.

## Consequences

The Machines tab shows what is on each card from any program, so a card filled by
something outside the farm is visible before a job runs out of memory on it.

`python -m comfyfarm agent --check` lists the cards whether or not the probe is on.

Cards other than NVIDIA's aren't read. None is on the farm or planned.
