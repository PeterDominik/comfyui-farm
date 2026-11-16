# 0006. Outputs are kept three days; a metrics row per job is kept

Date: 2026-11-14. Status: accepted.

## Context

Generated outputs fill show storage, and nothing removes them today.

## Decision

Every output gets an expiry, 3 days by default and set in config. A sweeper deletes expired outputs from disk and their jobs from the database. It starts in dry-run and only touches folders the farm created. Dispatching a publish restarts the clock. One small row per job survives: process, show, user, GPU, timings, outcome; no prompts and no paths.

## Consequences

Anything worth keeping must be published. Run-time estimates are unaffected, because `run_stats` is an aggregate.
