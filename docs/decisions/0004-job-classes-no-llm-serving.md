# 0004. Two job classes, and no LLM serving

Date: 2026-11-14. Status: accepted.

## Context

A model server is a standing service that clients call directly; a queue adds nothing to it, and it would hold a GPU permanently.

## Decision

Jobs are `interactive` (an artist is waiting) or `batch` (training, long runs; may be preempted and resumed). The farm does not host LLM serving. A process that needs an LLM calls a separate service as a client.

## Consequences

No service class in the scheduler. Preemption arrives with the batch class in phase 11.
