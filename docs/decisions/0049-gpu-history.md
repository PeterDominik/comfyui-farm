# 0049. GPU history kept by the manager, sampled from what agents report

Date: 2026-11-22. Status: accepted.

## Context

0010 asks for sampled GPU state. `/metrics` (0048) gives Prometheus the GPUs as they are
now, but no Prometheus runs for the pilot, and the dashboard needs a history to draw.
Agents already report their GPUs at every check-in: use and memory from `gpu_probe`
(nvidia-smi) when it is on, and memory in ComfyUI's report either way.

## Decision

- **A `gpu_samples` table in the manager's database:** machine, GPU index, time, use
  (0 to 100), memory used and total, and the jobs the machine held.
- **Sampled by a loop in the desk** every `gpu_history.sample_seconds` (60), from what each
  machine checking in last reported. No extra call goes to a machine. Use comes only from
  `gpu_probe`; memory comes from it, or from ComfyUI's report. A machine that isn't
  checking in isn't sampled, so its gap is visible.
- **Kept `gpu_history.days` (7),** pruned once an hour. `days: 0` keeps none and starts no
  loop.
- **Read per machine,** `GET /api/v1/workers/{name}/gpu-history?hours=` (up to 31 days),
  oldest first. Over a long span samples are averaged into steps of at least
  `hours × 3600 / 500` seconds: about 500 per GPU at most, enough for a chart. Use is
  averaged; memory and jobs keep their highest, so a peak isn't averaged away.
- **On by default,** like `job_metrics` (0040): it changes nothing a job does, and it is
  small. Three machines with one GPU each, sampled every minute for 7 days, is about
  30,000 rows.

## Consequences

- The Machines tab can draw each GPU's use and memory over the last day or week (step 3).
- Samples are as fresh as the agent's last check-in, a few seconds old, and use only as
  fresh as the agent's probe.
- Machines without `gpu_probe` show memory only.
