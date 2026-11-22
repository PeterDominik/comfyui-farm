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
- Phase 4: done. Steps: releases in the bank; agents report their environments;
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
  uv's cache. Today's ComfyUI sync is started by the manager: `comfyui sync --all` on its
  host takes the machines one at a time, the canary first, each drained, synced by its
  agent to the manager's version of the environment file, checked and resumed (0033). On
  21 November it synced the A6000 after its `extra_model_paths.yaml` was moved aside:
  ComfyUI stopped, the farm's file written, ComfyUI started again, the machine checked and
  resumed.
- Phase 5: done. Steps: the handler interface, with frames and texture sets ported
  (code moved, nothing new); the core leaves each kind to its handler (code moved); the
  generic route for a new kind, with `file` the first and each job's outputs listed by
  kind; movie and model added, each one handler file (the gate). The interface is in:
  each kind of output is a handler in `comfyfarm/artifacts/`, one file named by its key
  under `writes:`, found by listing the package, with the manager's half (where it goes)
  and the agent's (prepare, collect), and frames and texture sets are ported unchanged
  (0034). The core leaves them to their handlers: what the job keeps, what the runner is
  told and the dashboard's summary come from the handler; texture sets (batches across
  jobs) stay the core's (0035). The generic route is in, with `file` the first kind: a
  definition says `writes.<kind>` (for ComfyUI, the node that writes it), the files go
  beside the frames or into a version of their own, named after the job, a job may write
  several kinds, and each job's outputs are listed by kind (`artifacts`). The GPU test
  writes its report as a file (0036). Movie and model are in, each one file
  (`artifacts/movie.py`, `artifacts/model.py`), with no other source file changed: the
  gate. A test also adds a kind none of the farm knows as one module and runs a job of it
  through the manager and an agent (0037).
- Phase 6: started. Steps:
  1. A provenance file beside each job's outputs.
  2. The EXR stamp: each EXR the farm writes (texture maps) carries its job id and its
     provenance file's name.
  3. `job_metrics`: one small row per finished job that outlives it (0006).
  4. The sweeper's dry run: daily, it lists expired jobs and the exact files it would
     delete (only those their provenance lists; a publish restarts the clock), and
     deletes nothing.
  5. After a week of dry runs that list only farm-made files (the gate), deletion is
     switched on in config.

  The provenance file is in, off by default (`provenance.enabled`). The agent that ran a
  job writes it beside the outputs, named like them, before it reports the job done. It
  says what ran, where, on what, for whom and from what, in studio paths, and lists every
  file the job wrote, as each kind says (0038). Peter updated the pilot machines and tested it.

  The EXR stamp is in, with the same setting: each texture map's EXR carries
  `comfyfarm:job` and `comfyfarm:provenance` in its header, and `comfyfarm check` writes
  its test map with them (0039). Peter read it in Nuke on a CHORD job's maps.

  `job_metrics` is in: one row per finished job, kept when the job is deleted, with no
  params, prompts, paths or errors; read as JSON or CSV (`/api/v1/job-metrics`). Jobs that
  finished before it get their rows when the manager starts (0040).

  The sweeper's dry run is in, off by default (`retention.enabled`): once a day it writes
  each expired job and the exact files it would delete to `data/sweeps/<date>.json`, only
  what the job's own provenance file lists in its own folder, and deletes nothing (0041).
  The gate starts when it is switched on: a week of these reports listing only files the
  farm wrote. Peter's first report (217 expired jobs, all from before provenance) listed no
  files. It showed jobs written into artists' comp folders, so the sweeper now leaves any
  folder the artist picked and sweeps only the farm's renders folders (0042). The first
  jobs with provenance files expire on 2026-11-25. Step 5 waits for that week.
- Phase 7: started. Steps:
  1. The policy file and show rules: `policy.yaml` in the bank folder, validated, re-read
     when it changes, the last valid one kept in force (across a restart too); show rules
     checked at plan, a refusal naming its rule; `GET /api/v1/policy`;
     `comfyfarm policy check`.
  2. Licences: each definition's models carry their licence (`models:` in `process.yaml`);
     the policy says which licences each show may use, checked at plan.
  3. Quotas: limits per show (and per user, trusted until API keys) on what is queued at
     once, checked at plan, the limit named.
  4. Checked again at dispatch: a queued job a newer rule refuses fails with the rule
     named instead of running.
  5. The dashboard shows the policy in force and the file's error.

  Step 1 is in, off by default (`policy.enabled`). Both halves of the gate are in it: a
  barred show is refused with its rule named, and a broken file leaves the last valid one
  in force (0043). Peter tested it on the pilot manager.

  Step 2 is in: licence rules (`licences:` in policy.yaml) against the `models:` each
  definition lists, `unlisted_models` for those that list none, and `except:` on every
  rule (0044). CHORD lists its weights (Ubisoft's research-only licence) and the GPU test
  lists none. The LTX, SAM 3 and Wan definitions don't list theirs yet: their licences
  are Rodeo's call.
