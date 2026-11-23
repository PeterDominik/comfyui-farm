# 0050. GPU history graphs and a Usage tab on the dashboard, drawn by hand from the manager's own numbers

Date: 2026-11-22. Status: accepted.

## Context

The manager now keeps two kinds of history:

- each GPU's use and memory, sampled every minute (0049);
- one row per finished job, kept after the job (`job_metrics`, 0040).

Both are JSON or CSV today. An admin wants to see at a glance:

- how busy each card has been, and whether that was farm work;
- when a machine was missing;
- how much the farm ran, for which shows and definitions, and how much of it failed.

No Prometheus or Grafana runs for the pilot.

## Decision

- **A GPU history panel on the Machines tab** (`static/gpu_history.js`), below the machines.
  - A graph per GPU over the last 1 h, 6 h, 24 h or 7 days.
  - Use and memory in use are drawn on one 0 to 100% scale. Memory is shown as a share of
    the card, not in GB, so the two never need a second axis. The readout gives GB.
  - Shaded: the machine held farm jobs. A busy card with no shading is someone else's work.
  - A line breaks where samples are further apart than 2.5 steps: the machine wasn't
    checking in.
  - Pointing at a graph, or the arrow keys on it, gives that time's numbers.
  - It reads `GET /api/v1/workers/{name}/gpu-history` per machine when the tab opens, then
    every minute while it is open. It is hidden while `gpu_history.days` is 0.
- **A Usage tab** (`static/usage.js`) from a new endpoint, `GET /api/v1/usage`, which sums
  `job_metrics` in SQL. The browser never pages through the rows.
  - Per day (every day of the span, empty ones at zero), show, definition or machine, over
    the last 7, 30 or 90 days, for every show and definition or one of each.
  - Jobs, done, failed, cancelled, GPU hours, frames done, average wait.
  - **Runs are counted:** a job, or one batch of a texture set (each runs on its own). Not
    the set.
  - **GPU hours** run from a run's start on the GPU until it finished, outputs collected, of
    any outcome: a failed run held the GPU too. How many went on runs that didn't finish
    done is given too.
  - Days are the manager's local days, as `job-metrics?since=` reads them.
- **Drawn by hand, with no chart library.** SVG for the graphs, a bar in a table cell for
  GPU hours. The dashboard is one page the manager serves, with no build step and no
  third-party scripts, and the studio network shouldn't have to reach a CDN for it.
- **Two colours for the two series,** blue for use and orange for memory. Checked apart for
  colour-blind readers on the panel's background. The status colours stay for status.
- **Read-only, and always there.** They change nothing a job does and send nothing, like the
  Policy tab (0047). So, unlike new behaviour, they are on without a switch. The GPU panel
  follows `gpu_history.days`.
- **The functions that decide what to draw or say touch no page.** node runs them in tests,
  as for the environments panel and the Policy tab.

## Consequences

- **Retried jobs:** a job retried on another machine counts only its last attempt's GPU
  time. `job_metrics` keeps one row per job.
- **What each view covers:**
  - The Usage tab reaches as far back as `job_metrics` (since 0040 went in).
  - The graphs reach as far back as `gpu_history.days`.
- **Grafana later:** if one is set up, it reads `/metrics` (0048) for the same numbers. These
  pages don't depend on it.
