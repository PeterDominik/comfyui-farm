# 0016. The manager stores requests only, and keeps what a run submitted as the agent reports it

Date: 2026-11-16. Status: accepted.

## Context

This completes 0013 and 0014, which moved graph building and the workflow check to the
machine that runs the job, behind `farm.build_on_agent`. The switch has been on for all three pilot machines and the
five workflows run as before. 0013 left the old path in place "through the pilot"; keeping
two ways to run a job for that long means every later step has to be written and tested
twice, so the old path goes now that the new one is proven on the farm.

One thing the old path gave for free was the dashboard's API JSON: the stored graph. With
the switch on it was rebuilt on request from the manager's copy of the workflow, which is
the manager's last call into graph building, and shows what the manager would build rather
than what ran.

## Decision

- The manager stores a request for every job and never builds a graph or reads node
  definitions. `farm.build_on_agent` is gone; a config that still sets it gets a note.
- Agents always report `processes`. An agent that sends node definitions instead is too old
  and is passed over for every workflow, with a message saying to update it.
- A runner may hand the agent what it gave its backend (`Runner.submitted`; for ComfyUI the
  API JSON, with the paths of the machine that ran it). The agent sends it with the run's
  `dispatched` report, and the manager keeps it in the job row's `prompt` column and gives
  it out at `GET /api/v1/jobs/{id}/prompt`. The manager never reads it and never sends it
  to an agent.
- A job row from before requests (a stored graph and no request) is refused by the runner
  with a message that says to submit it again.

## Consequences

The API JSON shows what actually ran, on the machine that ran it. It exists once a machine
has started the job; before that the dashboard says so. A job picked up again after an
agent restart, before the agent had reported it, has none.

Jobs submitted before the switch was turned on can no longer be retried in place; they are
submitted again. Their stored graphs stay in the database and can still be downloaded.

`tests/test_layout.py` now holds that the manager's code calls none of the workflow's
graph-facing methods. The manager still imports `workflows.py` (which holds both halves of
a definition) and the ComfyUI environment comparison; splitting those is the last step of
phase 2.
