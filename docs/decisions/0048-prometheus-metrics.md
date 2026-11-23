# 0048. Prometheus metrics from what the manager holds, every family a gauge

Date: 2026-11-22. Status: accepted.

## Context

0010 asks for `/metrics` in Prometheus' text format. The manager already holds what a
scraper needs:

- the queue in its database;
- finished jobs in `job_metrics` (0040);
- each machine's last check-in, with its GPUs when `gpu_probe` is on;
- the policy's status;
- the sweeper's reports.

## Decision

- **`GET /metrics`**, the path scrapers expect, in the text format (version 0.0.4). It is
  written by hand in `manager/prom.py`: the format is simple, and the manager needs no
  new package for it.
- **Built from what the manager holds at the moment of the scrape,** with no sampling
  loop of its own:
  - the active jobs per state and definition (runs: a set's batches, not the set);
  - the oldest queued job's wait;
  - per definition, outcome and GPU type: the `job_metrics` counts, and for jobs that ran
    to done, their seconds and frames; waits per definition and outcome;
  - each machine: checking in, its runner up, drained, the jobs it holds, its agent build
    and GPU type;
  - each GPU: use and memory from `gpu_probe`, or memory from ComfyUI's report;
  - the policy (on, in force, file refused) and the sweeper's latest report.
- **Every family is a gauge.** A `job_metrics` row is replaced when a job finishes again
  (0040), so a count can go down, and a counter must not. A scraper takes rates over the
  gauges the same way.
- **Labels are machine, definition, status, outcome, GPU type and GPU index and name:**
  never a user, show, path or prompt. A metrics store keeps them long, and shows are
  client names.
- **The same identity check as every other endpoint.** Open while the pilot trusts
  identity; with API keys, a scraper sends one.

## Consequences

- How busy the farm is, how fast each definition runs per GPU type, and how often it
  fails can be graphed in Grafana with no farm-side storage.
- Per-show numbers stay in `job_metrics` and its CSV (`/api/v1/job-metrics`), not in the
  metrics store.
- GPU history in the manager itself (step 2) is for the dashboard, where no Prometheus is
  running.
