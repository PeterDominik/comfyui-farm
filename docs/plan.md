# Plan

The agreed design and build order, as of 15 November 2026. The reasons are in
`docs/decisions/`. Update this file in the pull request that finishes or changes a phase.

## Where we are going

- **One manager, blind to the backend.** It stores a request (process, typed inputs,
  parameters), never a ComfyUI graph.
- **Runners behind one contract.** The agent starts a runner as its own process and talks
  to it through files: `job.json` in, `events.jsonl` and `result.json` out, a `cancel` file.
  Two runners: ComfyUI (resident, models stay warm) and process (any command in its own
  environment).
- **Process definitions** live in this repo, are reviewed, and are pinned by hash per job.
- **GPUs are scheduled, not machines.** A job claims one GPU, and VRAM is released before
  another runner uses the card.
- **Two places to run a job.** The dedicated AI pool by default; Tractor later, through a
  Daiquiri graph that starts a one-shot agent. Routing is set per process.
- **Typed artifacts** with one handler per type, kept 3 days, then deleted from disk and
  database. A small metrics row per job is kept.
- **Runner environments managed remotely.** The manager publishes reviewed releases and
  each agent installs, updates and removes them; one machine goes first.
- **Out of scope:** LLM serving. It belongs to a separate service.

## Pilot

Three dedicated machines: Linux with an RTX 3090 (also runs the manager), Windows with an
RTX 5090 (also used for development), Linux with an A6000. Stages 1 to 3 are the pilot.
The pipeline team then reviews it; if accepted, the repo moves to Rodeo's GitHub.

## Phases

Every phase keeps Nuke to ComfyUI to publish working and ends at a gate.

| Phase | What | Done when |
|---|---|---|
| **Stage 1: open the seams** | | |
| 0 Foundations | `CLAUDE.md`, decision records, CI; `job_events` table and job timeline; JSON logs; cold and warm baseline timings | CI is green on main; a real job shows its full timeline |
| 1 Runner seam | ComfyUI code moved out of `agent/agent.py` behind a runner interface; no new behaviour | All tests pass; `agent.py` no longer imports the ComfyUI client |
| 2 Backend-blind manager | `process.yaml` (reads today's manifests too); graph built on the agent; agents report what they can run; hash pinned per job | The manager imports nothing ComfyUI-specific; the five workflows run unchanged |
| **Stage 2: a second runner, managed remotely** | | |
| 3 Process runner | GPU probe and claim; VRAM release; process runner with time limits; builder test (uv against the installers' method); environment spec and local `comfyfarm env install`; a test Python process | ComfyUI, test process, ComfyUI on one card with no out-of-memory, on Linux and Windows |
| 4 Remote environments | Releases in the bank folder; desired state in the check-in reply; environment manager in the agent; rollouts with canary, pause and rollback; today's ComfyUI sync started by the manager | An environment is installed, updated and removed on all three machines from the manager's host; a broken release stops at the canary |
| 5 Typed artifacts | Handler interface; sequence and texture set ported; movie, model and file added | A new output type needs one handler file and no core edit |
| **Stage 3: operate the pilot** | | |
| 6 Retention and provenance | `job_metrics`; `provenance.json` and EXR stamp; sweeper, dry-run first | A week of dry runs lists only farm-made folders |
| 7 Policy gates | `policy.yaml` read from the bank folder; show, licence and quota checks at `plan` | A barred show is refused with the rule named; a broken file leaves the last valid one in force |
| 8 Observability and evals | `/metrics`; GPU history; dashboard pages; alerts; a reference job per process | A changed node pack fails its eval and stops its rollout |
| **Pilot review by the pipeline team** | | |
| **Stage 4: scale out** | | |
| 9 Agent self-update | Agent code as a release; launcher with current and previous build | A bad agent build is replaced with no visit to the machine |
| 10 Workstation windows | Timed availability; window-fit placement; grace, then requeue; busy guard | A workstation serves overnight and holds no VRAM by 06:00 plus grace |
| 11 New runners | Batch class with preemption and resume; LoRA training; Griptape | Training is preempted and resumes; Griptape needs no core change |
| **Stage 5: join the studio** | | |
| 12 Tractor target | Router; one-shot agent; Daiquiri graph with the pipeline team | Same artifacts and job record on pool and Tractor |
| 13 Identity and alerts | API keys or Rodeo login; alerts to Google Chat or Gmail | A user cannot cancel another user's job |
| 14 Lend GPUs to Tractor | Optional | A pool GPU serves Tractor overnight and returns warm |

Clients, in parallel: A1 shared client core with Nuke as one host; A2 Maya; A3 Mari;
A4 launchers (xStudio, Daiquiri Automate).

## Progress

- Phase 0: `CLAUDE.md`, decision records and CI are in, and the suite passes on Linux and
  Windows. The `job_events` table and the job timeline (API, client, dashboard) are in.
  JSON logs are in, off by default (`log_json`). `tools/baseline.py` reports cold and warm
  timings per workflow and GPU from the farm's own jobs. Left: take the baseline on the
  three pilot machines and commit it to `docs/baseline/` (see its README).
- Phase 1: done. ComfyUI is behind the runner interface (`runners/base.py`,
  `runners/comfyui.py`, decision 0012) and `tests/test_layout.py` holds the gate. Left for
  later: `agent/launch.py` still lives in the agent package.
- Phase 2: done. The manager is blind to the backend and `tests/test_layout.py` holds the
  gate: it imports nothing ComfyUI-specific, and the five workflows run with their
  fingerprints unchanged. The graph is built where the job runs, from a stored request with
  the definition's fingerprint pinned per job (0013, 0016); agents say what they can run
  (0014) and compare their own environment (0017); jobs name no node (0018); a definition
  is a `process.yaml` whose head says what it takes and writes and whose runner block says
  which node does it (0015, 0019), and the five are written that way; the manager checks
  the head and agents check the wiring (0020); `definitions.py` holds the head and
  `runners/comfyui_workflow.py` the ComfyUI half (0021).
- Phase 3: done. The builder test is in (`tools/builder_test.py`, `docs/builder/`):
  it builds the same environment with uv and with the installers' method, cold and warm,
  and checks it. It ran on the three pilot machines and uv passed on all of them
  (`docs/builder/`). Agents read their GPUs with nvidia-smi, off by default (`gpu_probe`,
  0022). A resident runner frees its VRAM on request, and only when idle; the Machines tab's
  Free VRAM asks for it (0023). Runner environments are uv projects in `environments/`,
  locked in the repo and built on each machine with `comfyfarm env install` (0024): uv is
  the builder. The process runner runs a reviewed command in a runner environment with a
  time limit, kept by a supervisor per run that outlives the agent; the agent gives each
  job to the runner that knows its definition (0025). Off by default. One runner's runs at a
  time hold the card, first come first served, and ComfyUI frees its models before a
  process runs (the GPU claim, 0026). The test process, `workflows/gpu_test`, runs torch in
  `gpu-test` and holds as much of the card as it is asked (0027). The gate passed on
  21 November on all three machines: an LTX job, then the test holding most of the card
  (28 of 31.8 GB on the 5090, 44 of 47.4 on the A6000, 20 of 23.6 on the 3090, with 93 to
  97% of it free at the start), then LTX again, with no out-of-memory.
- Phase 4, in progress. Steps: releases in the bank; agents report their environments;
  desired state in the check-in reply, and the environment manager in the agent installing,
  updating and removing; rollouts with a canary (the A6000), pause and rollback; the
  Machines tab; today's ComfyUI sync started by the manager. Releases are in: `env publish`
  on the manager's host copies an environment, as committed, to the bank with its hashes,
  and `env install --release` builds from a checked copy (0028). Agents report what they
  have built with their status, and `GET /api/v1/environments` lists it beside the bank's
  releases (0029). The manager keeps what each machine should have of each environment
  (`env want` on its host), says it in the check-in reply, and the agent's environment
  manager installs, updates (switching back is a rollback) and removes, off by default
  (`envs.managed`, 0030). Rollouts take a release to the machines one at a time, the canary
  first (`rollouts.order`), each once the one before has it checked; a machine that fails
  or takes too long stops it there, with pause, resume, rollback and cancel (`env rollout`
  on the manager's host, 0031). `environments/canary-test` is broken on purpose to try it.
  The Machines tab shows each machine's environments, what the manager wants it to have,
  what its environment manager is doing and the rollouts, read-only: changes stay on the
  manager's host until API keys (0032). The gate passed on 21 November on all three
  machines, from the manager's host: `gpu-test` updated from 30cee042 to c817ab55 by a
  rollout (A6000, then 5090, then 3090); `canary-test`'s rollout stopped at the A6000 on its
  failed check, the 5090 and 3090 never told, then rolled back; `gpu-test` removed from all
  three (`env want gpu-test none`) and installed again by a rollout, the three in 26 s from
  uv's cache. Left: today's ComfyUI sync started by the manager.
