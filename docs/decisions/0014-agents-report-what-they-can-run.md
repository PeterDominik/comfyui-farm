# 0014. Agents report what they can run

Date: 2026-11-16. Status: accepted.

## Context

Each agent sent the manager its ComfyUI's whole node catalogue, and the manager checked every
workflow against it. That check needs the ComfyUI graph, which the manager is to stop
handling (0002, 0013). With graphs built on the agent, a machine whose copy of a workflow
differed from the manager's failed the job it was given.

## Decision

With `farm.build_on_agent` the manager's hello asks for `processes`, and the agent's runner
reports, for each workflow of its own checkout: its fingerprint, what the machine lacks for
it, and how many nodes a run goes through. The manager compares fingerprints and keeps the
problems. A workflow the agent lacks, or holds in another version, is a problem like a
missing node pack: the machine is passed over for that workflow and the dashboard says why.

An agent talking to a manager that does not ask keeps sending node definitions.

## Consequences

The manager no longer receives node definitions when the switch is on. A stale machine keeps
running the workflows it does match. The check now runs with the agent's copy of the
workflow, so a definition fix reaches a machine only when its code is updated; remote
updates are phase 4 and phase 9. The runner still refuses a job whose fingerprint does not
match, for a checkout that changed after it was reported.
