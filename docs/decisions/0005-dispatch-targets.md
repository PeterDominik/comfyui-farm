# 0005. Two dispatch targets, chosen per process

Date: 2026-11-14. Status: accepted.

## Context

The AI pool is small. Tractor has more blades but cannot run ComfyUI today, and the pools are separate.

## Decision

Every job has a target: `pool` (default) or `tractor`. A process lists the targets it allows; there is no automatic overflow. On Tractor a Daiquiri graph starts a one-shot agent that leases the job from the manager and uses the same runner contract. Lending pool GPUs to Tractor is a later, optional phase.

## Consequences

One job record and one artifact layout whichever target ran the job. Tractor jobs start cold, so batch work goes there first. The Daiquiri graph is built on Rodeo's network.
