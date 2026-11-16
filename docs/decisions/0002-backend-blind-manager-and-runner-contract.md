# 0002. The manager is blind to the backend; runners sit behind a file contract

Date: 2026-11-11. Status: accepted.

## Context

Today the manager builds the ComfyUI graph and the agent is a ComfyUI client. Other backends (Python tools, LoRA training, Griptape) cannot be added without touching both.

## Decision

The manager stores a request: process id, typed inputs, parameters. The agent starts a runner as a separate process and talks to it through files in a job folder: `job.json` in; `events.jsonl` and `result.json` out; a `cancel` file. Two runners: ComfyUI (resident) and process (any command in its own environment).

## Consequences

A new backend is a process definition plus an environment, with no change to the manager. A crashed runner cannot take the agent down. Phases 1 and 2 are refactors with no new behaviour.
