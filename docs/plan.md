# Plan

The agreed design and build order, as of 15 November 2026, with phase 11 revised on
23 November. The reasons are in `docs/decisions/`. Update this file in the pull request that
finishes or changes a phase.

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
- **Menus by provider, then task.** Every client reads Provider > Task > Workflow (ComfyUI
  Local > Masking > SAM3). Outside providers (fal.ai first) run through a gateway agent on
  the manager's machine, charged to a show and capped per show.
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
| **Stage 4: new runners first, then scale out** | | |
| 11 New runners and providers | Eleven steps (below): the catalogue and menus; the gateway and lanes; off-site rules; the ledger and caps; the fal.ai pilot on rnd; the batch class with preemption and resume; LoRA training; Griptape; more fal.ai models; Higgsfield; ComfyUI Cloud | A fal.ai job runs from Nuke on rnd within its cap; training is preempted and resumes; Griptape needs no core change |
| 9 Agent self-update | Agent code as a release; launcher with current and previous build | A bad agent build is replaced with no visit to the machine |
| 10 Workstation windows | Timed availability; window-fit placement; grace, then requeue; busy guard | A workstation serves overnight and holds no VRAM by 06:00 plus grace |
| **Stage 5: join the studio** | | |
| 12 Tractor target | Router; one-shot agent; Daiquiri graph with the pipeline team | Same artifacts and job record on pool and Tractor |
| 13 Identity and alerts | API keys or Rodeo login; alerts to Google Chat or Gmail | A user cannot cancel another user's job |
| 14 Lend GPUs to Tractor | Optional | A pool GPU serves Tractor overnight and returns warm |

Clients, in parallel: A1 shared client core with Nuke as one host; A2 Maya; A3 Mari;
A4 launchers (xStudio, Daiquiri Automate).
Nuke farm node (0051): a workflow as a node in the script, from the panel or the Tab menu.

## Phase 11: new runners and providers

Agreed with Peter on 23 November. Phase 11 comes next, before 9 and 10, while the pipeline
team reviews the pilot: nothing in it needs self-update or workstation windows, which pay off
when more machines or workstations join. It holds the providers plan ("AI Farm: Providers,
Catalogue and Spending Plan", P1 to P8) and the original batch class, LoRA training and
Griptape. It runs on the three pilot machines and the `rnd` project.

1. **Catalogue and menus (P1).** `providers/`, `workflows/tasks.yaml`, a definition's
   `task`, `provider`, `family` and `hidden`; `GET /api/v1/catalog?show=`; the dashboard's
   and Nuke's menus read Provider > Task > Workflow, on at once with no switch (Peter).
   Gate: the six workflows show by provider and task; nothing else changes.
2. **Gateway agent and lanes (P2).** A `gateway` target; lanes in the desk; the API runner;
   a mock fal.ai. Gate: a mock fal.ai job runs from Nuke end to end, in CI and on the
   manager's machine.
3. **Off-site policy (P3).** A `providers:` section in policy.yaml, off-site denied until a
   rule allows it; off-site jobs need a known show. Gate: a mock off-site job is refused on
   every show but the allowed one, with the rule named.
4. **Ledger and caps (P4).** Estimate at plan, reserve at submit, settle at the end; a cap
   per show for the life of the show. Gate: a job over its cap is refused before submit,
   and racing submits never overspend.
5. **fal.ai pilot (P5).** Nano Banana 2 edit on `rnd` with a $50 cap, on Peter's personal
   fal.ai account; no real request before step 4. Gate: it runs from Nuke on `rnd`, and the
   ledger matches fal.ai's billing for a week. That week overlaps steps 6 to 8.
6. **Batch class, preemption and resume.** `class: batch` sorts after interactive jobs. An
   artist's job that can't find a free GPU preempts one: its agent writes a `preempt`
   file, and the runner saves a checkpoint within 2 minutes and exits. The job is
   requeued, not counted as a failed attempt, and resumes from a resume folder beside its
   outputs. Draining a machine preempts its batch jobs too. Process jobs only; off by
   default. Gate: an artist's job preempts a batch test job, which resumes on another
   machine and finishes.
7. **LoRA training.** Python Local > Training, with [ai-toolkit](https://github.com/ostris/ai-toolkit)
   (Peter), first for Wan 2.2 A14B text-to-video. Datasets are on show storage
   (`/rdo/shows/<show>/...`), a caption `.txt` beside each image. The LoRA is a `model`
   output, copied into `/rdo/bank/comfy_farm/comfyui/models/loras` when training finishes
   so it outlives retention. Gate: an artist's job preempts training, which resumes and
   finishes.
8. **Griptape.** A spike first: one workflow run headless on a pool machine, to learn
   whether it needs Griptape Cloud or the internet. Offline, it is a process under Python
   Local; otherwise it is off-site and runs on the gateway under step 3's rules. Gate:
   Griptape is added with no change to core code.
9. **More fal.ai models (P6).** `comfyfarm fal draft <endpoint>` drafts a process.yaml from
   fal.ai's schema, and each one is reviewed. Gate: a new model is one reviewed process.yaml
   in one pull request.
10. **Higgsfield and price comparison (P7).** Gate: one node switches between fal.ai and
    Higgsfield, with both estimates shown.
11. **ComfyUI Cloud (P8).** Gate: an existing graph runs on ComfyUI Cloud unchanged, charged
    to its show.

On 24 November Peter moved step 9 ahead of steps 7 and 8. On 26 November he agreed the order
for the rest:
1. step 9's last image models: GPT Image 2 and its edit, and Nano Banana 2.1;
2. a licence pass: every local definition lists its models' licences, and the policy keeps
   any model not cleared for production on `rnd`;
3. ComfyUI Local > Masking with licence-cleared models: SAM 3.1, BiRefNet-HR-matting and
   MultiMatte, CorridorKey after legal's sign-off (the project's masking licence check,
   26 November);
4. step 9's video generation, then video editing and upscaling;
5. then steps 7, 8, 10 and 11.

Licences (Peter, 26 November). Rodeo uses no model that needs a licence it doesn't hold, even
through a provider's API: LTX-2.x is dropped, the farm's local LTX workflows and fal.ai's
alike. A model whose licence comes with using it through a provider's API may go to
production. CHORD runs on `rnd` only, never on a production show.

After phase 13 (Rodeo login): paid jobs for other shows, and Rodeo's fal.ai account in
place of Peter's (a key swap on the gateway). Until then a cap rests on a show name the
client types. Decision records follow the steps from 0054.

Still open: which Griptape workflow step 8 tries, and whether it calls outside services.

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
  are Rodeo's call. Peter tested it on the pilot manager.

  Step 3 is in: quotas (`quotas:` in policy.yaml), the most jobs and/or frames queued or
  running at once per show, per user over a rule's shows, or for shows together, checked
  at plan under the submit lock and on a set's Retry failed (0045). Peter tested it on the
  pilot manager.

  Step 4 is in: the show and licence rules are checked again when a machine would take a
  queued job, and a job a newer rule refuses fails with the rule named. Quotas aren't
  asked again. While the policy can't be read, queued jobs wait (0046). Peter tested it on
  the pilot manager.

  Step 5 is in: a read-only Policy tab, shown when policy is on, with where the rules came
  from, the file's error, every rule, what is queued against each quota, and each
  definition's models and licences (0047). With it, phase 7's steps are all in; its gate
  (a barred show refused with the rule named, a broken file leaving the last valid one in
  force) was met in step 1. Peter tested it on the pilot manager.
- Phase 8: started. Steps:
  1. `/metrics` in Prometheus' text format: the queue, finished jobs (`job_metrics`), the
     machines and their GPUs, the policy and the sweeper.
  2. GPU history: the manager keeps each machine's GPU use and memory, sampled every
     minute, for some days; `GET /api/v1/workers/<name>/gpu-history`.
  3. Dashboard pages: GPU history on the Machines tab; a page of jobs, GPU hours and
     failures per day, show and definition, from `job_metrics`.
  4. Alerts: rules in config (a machine silent too long, a job waiting too long, failures
     in a row on a machine, a sweep or rollout with problems), shown on the dashboard and
     in the log, optionally posted to a Google Chat webhook.
  5. Evals: `eval:` in a definition (a reference input, its params, and checks on what it
     writes); `comfyfarm eval` runs one on a machine; rollouts and ComfyUI syncs run the
     evals of the definitions they touch on each machine before going on, and a failed
     eval stops them (the gate).

  Step 1 is in: `GET /metrics`, every family a gauge, from what the manager holds (0048).
  Peter tested it on the pilot manager.

  Step 2 is in: GPU history in the manager's database (`gpu_samples`), sampled every
  `gpu_history.sample_seconds` from what each agent last reported, kept
  `gpu_history.days`, and read per machine, averaged into steps over long spans (0049).
  Peter tested it on the pilot manager.

  Step 3 is in: a GPU history panel on the Machines tab (a graph per GPU, use and memory
  on one scale, farm jobs shaded, gaps where a machine was silent), and a Usage tab with
  jobs, GPU hours and failures per day, show, definition or machine from `job_metrics`
  (`GET /api/v1/usage`). Both read-only, drawn without a chart library (0050). Peter
  tested it on the pilot manager.

  Step 4 is in, off until `alerts.enabled`: five rules with thresholds (a machine silent, jobs
  waiting, failures in a row on a machine, sweep problems, a rollout or sync stopped),
  each raised once and cleared by itself. They go to the log, a header chip and an Alerts
  tab on the dashboard, and with `alerts.google_chat`, a Google Chat space;
  `comfyfarm alerts test` checks the webhook (0052). Peter tested it on the pilot manager.

  Step 5 is in, the gate off until `evals.gate`: `eval:` in a definition (inputs from its
  own folder, params, and checks: files, size, like a reference image), run as a farm job
  pinned to one machine, which takes it even while drained, its outputs in that machine's
  scratch folder and checked by its agent. `comfyfarm eval` runs one from the manager's
  host. With the gate, a rollout drains each machine for its turn and runs the evals of its
  environment's definitions, and a ComfyUI sync those of the ComfyUI definitions; a failed
  eval stops it there, the machine drained (0053). gpu_test and chord_pbr have evals.
  Phase 8's gate (a changed node pack fails its eval and stops its rollout or sync) is
  still to be tested on the pilot. It needs `like` references for CHORD's eval, from a
  machine Peter trusts.
- Pilot review: next, once what is left above is in. Its checks, as agreed on 15 November:
  1. The five workflows run from Nuke as before: to check at the review.
  2. The test process runs on Linux and Windows beside ComfyUI on the same card: phase 3's
     gate, passed on 21 November.
  3. An environment update reaches all three machines from the manager's host, and a broken
     one stops at the canary: phase 4's gate, passed on 21 November.
  4. A week of retention dry runs lists only folders the farm made: phase 6's gate, from
     25 November.
  5. A job's timeline and provenance show what ran, where, and why it waited: in (phases 0
     and 6).
  6. A pipeline team member follows the runbook through an agent failure with no Claude
     session open: the runbook is `docs/runbook.md`.

  Left before it: phase 0's baseline timings, phase 6's week of dry runs and deletion, and
  phase 8's gate.
- Phase 11: started on 23 November, during the pilot review. Step 1 is four pull requests:
  the registry and the definitions' new fields; `GET /api/v1/catalog`; the dashboard's
  Submit tab; Nuke's Tab menu, panel and node.

  The registry is in: `providers/` (ComfyUI Local, Python Local), `workflows/tasks.yaml`
  (Masking, Video, Image, Textures, Tests), and a definition's `task`, `provider`,
  `family` and `hidden`, checked when it loads against the checkout's catalogue
  (`comfyfarm/catalog.py`). An unknown task or provider turns a definition off with the
  reason. Each of the six has its task and `gpu_test` is hidden. These fields are left out
  of the fingerprint, so the six keep the ones they had. `comfyfarm check` says where each
  workflow shows in the menus (0054).

  `GET /api/v1/catalog?show=` is in: the tree every client will draw, with a workflow the
  show's policy refuses kept and marked with the reason, and hidden ones only on request.
  The shared client has `catalog()` and a `catalog` command (0054).

  The dashboard's Submit tab picks Provider › Task › Workflow from it (`static/catalog.js`),
  the hidden GPU test listed and marked; the plain list stays if the catalogue can't be read.

  Nuke reads the same menus for the session's show:
  - the Tab menu is ComfyFarm › provider › task › workflow, with Reload workflows at the top;
  - the panel has Provider, Task and Workflow pickers and a card under them;
  - the node's label names its provider;
  - a workflow the show may not use is listed "(not on this show)" and says why;
  - hidden ones aren't listed.

  That is step 1's last pull request; its gate is the pilot's six workflows showing by
  provider and task.

  Step 2 (the gateway) is three pull requests: the definitions, providers and desk; the
  gateway agent with its API runner, fal.ai adapter and `tools/mock_fal.py`; running it on
  the manager's machine with an end-to-end test.

  The first is in (0055):
  - the `api` runner type and `gateway` target;
  - `providers/fal.yaml`, off until `providers.enabled` in config.yaml lists it;
  - the desk's lanes: an API job goes only to a gateway's free lane of its provider, and
    a gateway takes nothing else.

  The second is in (0056):
  - the gateway, an agent with `gateway.enabled` and the API runner alone
    (`gateway.example.yaml`), left out of rollouts and syncs;
  - the fal.ai adapter: keys from files on the gateway only, and a job never sent twice;
  - `tools/mock_fal.py`, fal.ai's stand-in. Nothing reaches fal.ai itself before step 5;
  - `workflows/nano_banana2_edit_fal`.

  The third is in (0057):
  - `farmctl.sh` runs the gateway and the stand-in on the manager's machine;
  - key files are read as editors save them, after Peter's first run on the manager's
    machine, whose key file began with a byte-order mark;
  - the Machines tab shows the gateway's lanes;
  - the gate's CI half: a stand-in job submitted by the Nuke node's code, end to end over
    HTTP. The other half is a stand-in job from Nuke on the manager's machine.

  Step 2 is done: Peter updated and tested it on the pilot on 24 November.

  Step 3 (the off-site policy) is one pull request (0058):
  - a `providers:` section in `policy.yaml`. An outside provider is refused on every show
    until a rule allows it, for a known show, and refused with policy off;
  - `deny` rules for any provider;
  - checked at plan, in the menus and at dispatch;
  - `policy.example.yaml` has the pilot's rule, fal on `rnd`.
  Peter updated and tested it on the pilot on 24 November.

  Step 4 (ledger and caps) is three pull requests:
  1. prices and estimates;
  2. the ledger and caps, which carry the gate;
  3. billing events, reconciliation and reports.

  The first is in (0059):
  - a reviewed `cost` block in each API definition;
  - the gateway reads fal.ai's prices from its pricing API, daily;
  - each job is estimated when it is planned, shown in the preview and kept with the job;
  - a move of more than 10% is noted, and another unit or currency refused.
  Peter updated and tested it on the pilot on 24 November.

  The second carries the gate (0060):
  - `budgets:` in `policy.yaml`: a cap per show for its life, `other_shows`, `confirm_above`
    and `warn_at`. Without it no show may spend;
  - the `spend` table, never swept. A paid job's estimate is checked against its show's cap
    at plan and again as it is queued, in the job's own transaction;
  - when the job ends its money is charged, released or unsure, from what the gateway says
    reached fal.ai. The ledger never guesses down;
  - a paid job is never queued again, and a cancel while it is prepared goes to the gateway;
  - a yes at `confirm_above`, asked by the dashboard, the Nuke panel and node, and the client;
  - each job's money on the Queue tab, the budgets on the Policy tab, and a `budgets` alert.
  Peter updated and tested it on the pilot on 24 November.

  The third reads the providers' bills (0061):
  - the manager asks, each night with `ledger.nightly` or with `comfyfarm spend reconcile` on
    its host; the gateway reads each provider's bill and balance with its billing key;
  - each job the bill charges is billed at what it charged. An unsure one takes a charge it
    can be placed with. One the bill doesn't show 48 hours after it ended is billed $0, or
    released;
  - top-ups recorded on the manager's host (`comfyfarm spend topup fal 50`), and alerts for a
    bill not read and a low balance;
  - the Spend tab, the CSV of a show's jobs for any dates, and `comfyfarm spend`;
  - the stand-in's bill charges $0 (it takes no money), or fal.ai's prices with `--bill`.
  Peter updated and tested it on the pilot on 24 November. Step 4 is done.

  Step 5 (the fal.ai pilot) is two pull requests:
  1. fal.ai live;
  2. the week's match, if the first week shows it is needed: each day's charges in the farm
     beside fal.ai's, for the gate.

  The first is in (0062):
  - `live: true` for fal in `gateway.yaml` sends jobs to fal.ai itself, off by default; it
    replaces the `REAL_SERVICE` constant;
  - each job with `X-Fal-Store-IO: 0` (unless `keep_payloads`) and fal.ai's fallbacks off;
    Nano Banana 2 edit with `sync_mode`, so its result is never a file on fal.ai's CDN;
  - with `live`, the key only to fal.ai's own hosts and results only from its CDN;
  - `farmctl.sh check gateway` tries the keys on two questions fal.ai answers for free.

  Peter's first job on fal.ai ran on 24 November, from the dashboard, once fal.ai's
  plural unit (`images`) was taken as the definition's `image`.

  Step 6 (batch class, preemption and resume) is two pull requests:
  1. the batch class, the resume folder, and a drained machine's batch jobs preempted;
  2. an artist's job preempting a batch job, which carries the gate.

  The first is in (0063):
  - `class: batch` for process definitions only, frozen in the job; batch jobs queue after
    artists' jobs, take only a free GPU, and nothing is prepared ahead behind one;
  - each batch job's resume folder beside its outputs, named like them, made by the agent
    and removed by the manager when the job ends;
  - the `preempt` file: the command saves and exits within `preempt_seconds`, or is stopped.
    The job is queued again, its attempt given back, and its next run goes on from its folder;
  - with `batch.preempt`, off by default, a drained machine's batch jobs are preempted;
  - `workflows/batch_test`, the farm's test of it.
  With a fix: a definition that writes only files may have an output folder, and
  `batch_test` has one. Peter updated and tested it on the pilot on 24 November.

  The second carries the gate (0064). With `batch.preempt`, an artist's job that finds no
  free GPU stops one batch job:
  - on a machine that may run the artist's job and has nothing else on its GPU, the fastest
    for it;
  - one batch job for each artist's job, and the artist's job waits for that GPU;
  - never for a batch job, an eval or an API job;
  - one that hasn't stopped after 10 minutes is left to run.
  Peter updated and tested it on the pilot on 24 November: the gate passed. Step 6 is done.

  Step 9 comes next, before steps 7 and 8: Peter asked on 24 November for fal.ai's models
  before LoRA training. He chose the draft tool and a reviewed first set of the most-used
  models in image generation and editing, video generation, and video editing and upscaling,
  in a few pull requests, more later one each. The pull requests:
  1. `comfyfarm fal draft`, and Nano Banana Pro edit;
  2. image generation: jobs with no input image, prices fal.ai gives in tokens or megapixels,
     and the image models, in two: first the jobs with no input and the models priced by the
     image or the compute second (Nano Banana 2 and Pro from a prompt, BiRefNet); then, from
     live drafts, those priced in megapixels or tokens (FLUX 2 Pro, GPT Image 2, Nano Banana
     2.1);
  3. video generation: video results, and the video models;
  4. video editing and upscaling: video inputs, and those models.

  The first is in (0065):
  - `comfyfarm fal draft <endpoint>` drafts a process.yaml from fal.ai's model API (its
    OpenAPI schema) and its price, read as the gateway reads fal.ai, with TODO lines for the
    review; `--write` puts it in `workflows/`;
  - the stand-in describes its models;
  - `nano_banana_pro_edit_fal`, drafted and reviewed;
  - the tests take every fal.ai definition alike, so a new one is one file.
  Peter drafted Nano Banana Pro edit and BiRefNet against fal.ai itself on 25 November. A fix
  followed for what those drafts showed: the prompt first, the endpoint in the label, units
  in words (BiRefNet's `compute seconds`), long lines wrapped.

  Before step 9's next pull request, Peter asked on 25 November to turn off the LTX and SAM3
  workflows and keep them in the repo (0066): `disabled_workflows` in the manager's
  config.yaml takes a definition out of every menu and list and refuses it at submit, while
  `comfyfarm check` still lists it.
  On the pilot, the sync took the three off ShotGrid's menu. Peter had first taken every show
  out of `menu.yaml`, which the sync refused for want of a show for the pipeline's context. He
  asked that no shows remove every item (0067): the context is then the last good sync's show.

  Step 9's second pull request (0068): a job with no input image takes its show, sequence and
  shot from its Output folder, which such a definition requires, and is named after the shot
  or the show, from frame 1001. Nano Banana 2 and Nano Banana Pro from a prompt, and BiRefNet
  background removal by the compute second, reviewed.
  Its second half began with FLUX 2 Pro (0069), drafted live by Peter: billed by the processed
  megapixel, at six sizes that `runner.values` sends as fal.ai's width and height. GPT Image 2
  and Nano Banana 2.1 couldn't be drafted: fal.ai describes partner endpoints (`openai/`,
  `google/`) in a form the draft tool doesn't read yet.
  The descriptions Peter saved showed why: fal.ai files a model it runs for its maker under
  its own owner in the schema (`POST /fal-ai/gpt-image-2` for `openai/gpt-image-2`). The draft
  tool now finds it there, follows fal.ai's own field order, and takes the image field fal.ai
  requires as the input (GPT Image 2 edit's `image_urls`, not its `mask_url`). Their prices
  come from Peter's next drafts.

  The licence pass (0070): every definition lists its models' licences (Wan 2.2's, and the LTX
  workflows', SAM 3's and ViTMatte's, added), and `policy.example.yaml` holds Rodeo's rules:
  production shows only cleared licences, the rest on `rnd` only, LTX-2.x on no show, and a
  definition without its models refused. `tests/test_licences.py` keeps every licence decided.

  Step 9's image models finished (0071): Peter's drafts showed fal.ai's pricing API gives "$1
  a unit" for the token-billed ones, so `cost.provider_price: false` holds a job at its
  reviewed price. GPT Image 2, its edit and Nano Banana 2.1, reviewed. Next: the ComfyUI Local
  masking menu (item 3 of 26 November's order).
