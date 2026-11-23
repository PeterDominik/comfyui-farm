# 0052. Alerts checked by the manager, raised once and cleared by themselves, optionally posted to Google Chat

Date: 2026-11-22. Status: accepted.

## Context

The pilot's problems have so far been found by someone looking:

- a machine whose agent didn't come back after an update;
- jobs queued behind a drained or offline machine;
- a card that fails every job;
- a rollout or a sync that stopped at a machine and waits for a person.

The manager already knows all of these. `/metrics` (0048) could feed Prometheus alert
rules, but no Prometheus runs for the pilot. The studio's people are on Google Chat.

## Decision

- **Fixed rules with thresholds,** in `alerts:` in the manager's `config.yaml`. Each is off
  at 0 or false. There's no rule language: the five cases are what the plan names, and
  thresholds are what needs tuning.
  - **A machine silent:** `machine_silent_minutes` (10) without checking in.
    - A drained machine is left out: draining is how a machine is taken out to be worked
      on.
    - A machine not seen since the manager started is known from its GPU history (0049),
      so one that didn't come back after a restart still alerts.
  - **Jobs waiting:** `job_waiting_minutes` (60) queued without a machine.
    - One alert for all of them, not one per job: a set of 50 batches is one problem.
    - It names the oldest and, where the desk knows, why it waits. That is a machine that
      can run it not taking jobs (`requires_note`), or no valid policy (0046).
  - **Failures in a row:** a machine's last `failures_in_a_row` (3) runs, done or failed,
    all failed.
    - The runs come from `job_metrics`, so this outlives the jobs.
    - Cancelled runs say nothing of the machine, so they are left out.
    - It names the latest and the first line of its error.
  - **Sweep problems:** the retention sweeper's latest report lists problems (0041).
  - **A rollout or sync stopped, waiting for a person:**
    - a rollout `failed` (0031): it needs a rollback or a cancel;
    - a sync `failed` at a machine it left drained (0033): it needs a look and a resume.
    A sync whose agent refused before touching anything resumes the machine itself, so
    nothing waits.
- **An alert is a condition with a start and an end.**
  - Each check works out what holds.
  - An alert is raised once, when its condition starts, and cleared when the condition
    ends. Nobody acknowledges an alert: it clears when what it is about is fixed.
  - While it holds, its message follows (how many jobs wait), without being raised again.
- **Kept in the manager's database** (`alerts`), so a restart neither forgets an alert nor
  raises it again. Cleared alerts are kept `keep_days` (30), for the dashboard.
- **Where alerts go:**
  - **The log:** a warning when raised, a line when cleared.
  - **The dashboard:** a red chip in the header on every tab while any holds, and an
    Alerts tab (`static/alerts.js`) with the rules, what holds and the latest cleared.
    Read-only, from `GET /api/v1/alerts`, read every 30 seconds.
  - **Google Chat,** with `google_chat`: one message per check that raised or cleared any,
    not one per alert, so a network outage that silences three machines is one message.
    - Posted with the standard library, so there's no new package. It goes through the
      host's `HTTPS_PROXY` if it has one.
    - A failed post is logged and shown on the tab, and not retried. The alert still holds
      on the dashboard.
- **The webhook is a secret.**
  - It lets anyone post to the space, so it lives only in `config.yaml`, which is never
    committed.
  - It must be a `https://chat.googleapis.com/` address, so a typo can't send alerts
    elsewhere.
  - No endpoint gives it (`google_chat: true` only), and a failed post's reason never
    contains it.
- **`comfyfarm alerts test`** posts a test message from the manager's host, to check the
  webhook and the way out before an alert needs it.
- **Off by default** (`alerts.enabled`). With it off, no loop runs, the tab and chip are
  hidden, and the endpoint says so.

## Consequences

- **A retired machine** alerts as silent until its GPU history ages out (`gpu_history.days`).
  Draining it first keeps it quiet.
- **An artist's input that fails** three times on one machine alerts as failures in a row.
  The message names the error, so a person can tell.
- **Machines that sleep:** a workstation turned off each night alerts each night, unless
  it is drained or the rule is off.
- **More rules:** a disk filling, or a machine running hot, would each be a new rule here,
  with its threshold.
