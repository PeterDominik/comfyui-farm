# 0001. Build the AI layer on this repo; Tractor is a place to run jobs

Date: 2026-11-12. Status: accepted.

## Context

Rodeo runs Tractor. Deadline, OpenCue and Tractor schedule render tasks; none knows about warm models, VRAM, typed AI outputs or DCC panels. The AI GPUs are a separate pool that Tractor does not serve.

## Decision

Keep building on this repo. The manager owns every AI job and its record. Tractor becomes a second dispatch target behind the same manager (0005), not a replacement.

## Consequences

The scheduler stays small (`manager/desk.py`, `manager/eta.py`) and is ours to maintain. Nothing here competes with Tractor for a card.
