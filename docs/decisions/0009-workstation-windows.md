# 0009. Workstations join in timed windows

Date: 2026-11-14. Status: accepted.

## Context

Artist workstations have idle GPUs at night.

## Decision

Availability rules in the manager's config open and close a machine on a schedule, for example 21:00 to 06:00. A job is placed only if its estimate fits the window. At close a running job gets a grace period, then is stopped and requeued, which does not count as a failed attempt. ComfyUI is stopped at close so the artist gets the VRAM back. A process with no run history is never placed on a workstation.

## Consequences

Built in phase 10, after the pilot, whose machines are all dedicated.
