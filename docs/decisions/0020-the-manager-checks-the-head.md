# 0020. The manager checks a definition's head; agents check how it wires its runner

Date: 2026-11-20. Status: accepted.

## Context

Phase 2 ends with `workflows.py` split, so the manager imports only the half of a
definition that any runner shares. Until now the manager checked the whole definition when
it started, and refused jobs for one with a problem in either half. The checks of the
runner block need the runner's knowledge: for ComfyUI, that each node a parameter, input or
output names is in the graph with the class it says, that a loader is one the farm can
fill, and that the graph loads no movie. The manager can't keep running them once it no
longer imports that knowledge.

The agents already load their own copy of each definition and report, per fingerprint,
what their machine lacks for it (0014). A wiring problem is the same on every machine
holding that fingerprint.

## Decision

- The manager checks a definition's head: its parameters, inputs, what it writes, what it
  requires, publishing, ShotGrid and the head-only fields.
- How the runner block wires the graph is checked where the graph is: by each agent, by
  `python -m comfyfarm check` and by CI, which run both halves.
- Each agent reports a definition's wiring problems with what it can run, as `broken`. The
  manager adds them to that workflow's problems the first time an agent with the same
  fingerprint reports them, so the workflow is disabled for everyone, as before, and the
  machine is passed over for it. They stay until the manager restarts with a fixed
  definition.
- An input whose runner reads every file in its folder says `own_folder: true` in a
  manifest too, not only in a process.yaml. The manager reads it instead of working it out
  from ComfyUI's loader, and the runner refuses a definition whose loader needs it and
  doesn't say it.

## Consequences

A definition with broken wiring shows as enabled on the manager until an agent holding the
same fingerprint connects. Until then no machine could run its jobs either. CI and
`python -m comfyfarm check` still catch it before it is merged.

An agent from before this change reports no `broken`, so its manager can't learn of a
broken definition from it: update all machines together.

A manifest with a loader that reads a whole folder (VHS Load Images Path) now needs
`own_folder: true`; none of the farm's workflows has one.
