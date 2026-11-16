# 0010. Observability is a job timeline, GPU history, metrics and evals

Date: 2026-11-14. Status: accepted.

## Context

When a job waits or fails, nothing stored says why: events exist only in memory.

## Decision

Store a `job_events` timeline per job, sample GPU state, expose `/metrics` in Prometheus text format, write a provenance file beside each output, and keep one reference job per process to rerun after an environment change. No LLM tracing tools: this farm runs image and video jobs.

## Consequences

The timeline comes first, in phase 0, so the refactors of stage 1 can be checked against it.
