# 0017. Each agent compares its own environment and reports what differs

Date: 2026-11-16. Status: accepted.

## Context

Each agent sent the manager everything it read about its ComfyUI (version, launch flags,
Python packages, node packs, model files), and the manager compared that with the farm's
environment file. It was the manager's last ComfyUI-specific job (0002), and it would have
needed a second comparison in the manager for every runner that has an environment.

## Decision

The runner compares. The ComfyUI runner reads its machine, compares it with its own
checkout's `deploy/comfyui/environment.yaml`, and reports: when it checked, the ComfyUI
version and commit, what differs and what could not be checked, as text. The manager keeps
the report for the Machines tab and understands none of it. Node packs and models used
only by workflows the machine never runs (`requires`) are left out by the runner, which
knows its own GPU and tags.

There is no switch. The report is shown and never holds a job back, so a wrong comparison
cannot stop work, and running both comparisons side by side would keep the manager reading
ComfyUI's data.

An agent that still sends what it read gets a note on the Machines tab saying to update it.

## Consequences

A change to the environment file reaches a machine when its code is updated, as a change
to a workflow does (0014); before, the manager's copy ruled at once. The Machines tab can
therefore show a machine as matching a file that is one commit behind until that machine
pulls. Remote updates (phases 4 and 9) close that gap.

`tests/test_layout.py` holds that the manager's `farm` and `desk` import with `comfyenv`
and `runners` blocked. What is left of phase 2 is `workflows.py`, which the manager imports
and which still holds graph building beside the neutral head.
