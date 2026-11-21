# 0021. A definition's head is definitions.py; its ComfyUI half is runners/comfyui_workflow.py

Date: 2026-11-20. Status: accepted.

## Context

Phase 2's gate is a manager that imports nothing ComfyUI-specific. After 0016 to 0020 the
manager plans and checks jobs with a definition's head alone, but the head and the ComfyUI
wiring were still one class in one module (`workflows.py`), which imports ComfyUI's
loaders.

## Decision

`workflows.py` is split, the code moved unchanged:

- `comfyfarm/definitions.py`: reading process.yaml and manifest.yaml, `Definition` (the
  head: params and validation, inputs, what it writes, requires, publishing, the head's
  checks, the request, the schema, the fingerprint) and `DefinitionRegistry`. The manager
  loads its definitions with it. It imports no runner.
- `comfyfarm/runners/comfyui_workflow.py`: `Workflow(Definition)`, which adds checking the
  wiring, checking a machine's ComfyUI against the graph, and building the graph; its
  `Port` adds the loader that fills an input; `WorkflowRegistry`; `fit_to_options`; and
  `to_process` for the converter. Agents and `python -m comfyfarm check` use it.

Two things stay in the neutral module on purpose:

- The fingerprint is computed there, over the definition as read and its graph's file
  (the comfyui block's `template`), as it always was. That keeps every fingerprint
  unchanged; the module hashes the graph and never reads it. A new runner type will
  say which of its files the fingerprint covers.
- The definition as read keeps the runner block's fields as plain data (each parameter's
  `targets`, an input's `loader`). The neutral module never interprets them.

## Consequences

`tests/test_layout.py` holds the gate: the manager's modules, publishing and ShotGrid's
menu import with `comfyfarm.runners`, `comfyfarm.loaders` and `comfyfarm.comfyenv` blocked.
`app.py` still imports the agent, only in standalone mode, where the manager and its
agents share one process.

Code that imported `comfyfarm.workflows` imports `ParamError` and the format readers from
`comfyfarm.definitions`, and `Workflow` and `WorkflowRegistry` from
`comfyfarm.runners.comfyui_workflow`.
