# 0013. The machine that runs a job builds its graph, from a stored request

Date: 2026-11-16. Status: accepted.

## Context

Decision 0002 makes the manager blind to the backend. Today it builds each job's ComfyUI
graph at submit and stores it. The graph is the one ComfyUI-specific thing in a job row,
and building it is the one ComfyUI-specific step of `farm.submit`.

## Decision

The manager stores a request: the workflow's name and fingerprint, the validated parameters,
the resolved inputs and the planned output names (`Workflow.request`). The ComfyUI runner
builds the graph from it with its own checkout's copy of the workflow (`build_from`) and
maps studio paths written in the graph to its own, as the agent does for the rest of the job.

The fingerprint is a hash of the manifest and the graph as parsed. A machine whose copy has
another fingerprint refuses the job with a message that says to update it. This is the hash
pin of decision 0003.

It is switched on with `farm.build_on_agent`, off by default. Jobs queued before the switch
keep their stored graph and run from it.

## Consequences

Every machine's code must be current before the switch is turned on; until agents report
which definitions they hold (the next step of phase 2), a stale machine fails the job
instead of being passed over. The dashboard's API JSON is built on request from the
manager's copy of the workflow, so it shows what the manager would build, not a copy of
what ran. When the switch has been on through the pilot, the old path and the stored graph
are removed, and with them the manager's last call into graph building.

Later: 0016 removed the switch and the old path.
