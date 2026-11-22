# 0032. Environments and rollouts on the Machines tab: shown, not changed

Date: 2026-11-21. Status: accepted.

## Context

What each machine has built of the runner environments (0029), what the manager wants it
to have (0030) and the rollouts (0031) were in the API's JSON and in `env rollouts` on the
manager's host. Phase 4's step after rollouts is to show them on the dashboard's Machines
tab. Until the farm has API keys, environments change only from the manager's host,
because the user name a dashboard request gives isn't checked (0030, 0031).

## Decision

- **Read-only.** The Machines tab shows the environments and the rollouts and has no button
  that changes them. It names the commands to type on the manager's host instead. Buttons
  can come with API keys (phase 13).
- **A table of machines by environments,** below the machines, in the same order. Each
  environment's column head shows the bank's latest release of it. Each cell shows what
  that machine has current, or what its environment manager is doing (installing, waiting
  for a job, removing, failed, not managed), or that it has been told and hasn't begun. Its
  tooltip says where the build came from (a release, or the checkout), when it was built,
  its previous build and what the manager wants. A failed install's error is listed under
  the table, whole.
- **Colour by what the manager wants, not by the latest release.** A machine that has what
  the manager wants is green, even when the bank has something newer: a rollback, or a
  machine held back on purpose, is as wanted. It says "older" beside it. When the manager
  doesn't manage an environment on a machine, what is built there counts as wanted.
- **The rollouts,** open ones first, then the rest newest first (the last 10): each with
  its machines in order, each marked (has it, told, paused there, failed there, rolled
  back, not told), and its error.
- **Live, and cheap.** Each machine's part comes with its status in the dashboard's live
  events. The bank's releases and the rollouts are read from `GET /api/v1/environments`
  only while the tab is open: when it opens, every 15 s, and every 5 s while a rollout is
  running.
- **In its own file,** `static/environments.js`, like `batch.js`. What a cell and a
  rollout step say is worked out by functions that touch no page, so a test runs them
  with node (`tests/test_dashboard_envs.py`).

## Consequences

- Seeing a rollout no longer needs a shell on the manager's host; changing one still does.
- The dashboard gets its first JavaScript test. It is skipped where node isn't installed;
  GitHub's runners have it.
- An environment shows as soon as any machine has it, the manager wants it somewhere, or the
  bank has a release of it. With none of these, the section isn't shown.
