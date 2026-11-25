# ComfyFarm: notes for Claude and for reviewers

Claude writes and maintains this repo; Rodeo's pipeline team reviews every pull request.
Claude keeps no memory between sessions, so what a session needs to know lives here, in
`docs/plan.md` and in `docs/decisions/`. Keep all three true when a change makes them wrong.

## What this is

A farm for AI jobs in a VFX studio. Artists submit from a DCC (Nuke today), one manager
queues the job and gives it to an agent on a GPU machine, and the result lands on show
storage and can be published through the studio pipeline. Today every job is a ComfyUI
workflow. `docs/plan.md` is the plan to make the manager blind to the backend, add other
runners, and manage runner environments remotely.

## Commands

```bash
pip install -i https://pypi.org/simple -r requirements-dev.txt   # name the index: studio machines default to an internal mirror
python -m unittest discover -s tests -t . -v                     # everything, 2-3 minutes
python -m unittest tests.test_sets tests.test_layout -v          # one or two modules
python tools/mock_comfyui.py --port 8188 --output mock_output    # a ComfyUI stand-in, no GPU
python tools/mock_fal.py --gateway gateway.yaml                  # fal.ai's stand-in, where the gateway sends jobs
python -m comfyfarm fal draft <endpoint>                         # a fal.ai definition to review, read as the gateway does
python -m comfyfarm standalone                                   # manager and agents in one process
```

- Tests are `unittest` classes. Do not add pytest-only features.
- With only pydantic and pyyaml installed most tests still run; `test_agent` needs httpx
  and websockets, and the end-to-end suites need all of `requirements.txt`.
- CI (`.github/workflows/tests.yml`) runs the full suite on every pull request.

## Layout and layering

| Package | Role |
|---|---|
| `comfyfarm/manager/` | `app.py` REST and dashboard, `farm.py` plan and submit, `desk.py` agents, leases and placement, `rollouts.py` environment rollouts, `syncs.py` ComfyUI syncs one machine at a time, `prices.py`, `ledger.py` and `bills.py` what paid jobs cost, the caps and the providers' bills, `db.py` SQLite |
| `comfyfarm/agent/` | one per GPU machine, and the gateway's (`gateway.yaml`): takes jobs, prepares inputs, collects outputs, reports to the manager; `envman.py` keeps its runner environments as the manager says |
| `comfyfarm/runners/` | what runs a job on the GPU, behind `base.py`: `comfyui.py` (its client, websocket, health, environment), `comfyui_workflow.py` (a workflow's graph: its wiring checked, the graph built), `process.py` (the process runner and a process definition's half), `process_run.py` (each run's supervisor) and `api/` (the gateway's API runner, `runner.py`, an adapter per outside provider, `fal.py`, and `fal_draft.py`, a fal.ai definition drafted from fal.ai's schema) |
| `comfyfarm/definitions.py` | a definition's head: reading `process.yaml` / `manifest.yaml`, params, validation, the fingerprint |
| `comfyfarm/catalog.py`, `providers/` | the menus, Provider > Task > Workflow: one file per provider, the tasks in `workflows/tasks.yaml`; each definition's place is checked against them |
| `comfyfarm/protocol.py` | the only messages manager and agent exchange |
| `comfyfarm/fileio/` | path mapping, EXR and movie conversion, versioned outputs |
| `comfyfarm/artifacts/` | each kind of output, one handler file named by its `writes:` key: where it goes (manager), prepare and collect (agent); `base.Generic` is the route a new kind takes |
| `comfyfarm/comfyenv/` | the farm's ComfyUI environment: spec, compare, sync |
| `comfyfarm/envs/`, `environments/` | runner environments: uv projects in the repo, locked; published to the bank as releases; built and checked on each machine (`comfyfarm env`) |
| `comfyfarm/publish/`, `comfyfarm/shotgrid/` | publishing through Daiquiri and Tractor; ShotGrid |
| `client/` | the shared client and the Nuke panel |
| `workflows/` | one folder per definition: `process.yaml` (or the older `manifest.yaml`) and the ComfyUI graph, or a process's code; `tasks.yaml`, the menus' tasks |
| `deploy/` | per-machine install, start-up and update scripts |

`tests/test_layout.py` enforces these rules. A change that needs to break one needs a
decision record first.

- `fileio` imports the standard library only, so the farm's ComfyUI nodes can use it.
- The manager's `farm` and `desk` import neither the agent nor the web stack.
- The agent imports neither the manager nor its web server.
- `agent/agent.py` imports no backend: ComfyUI's client, websocket and environment live in
  `runners/comfyui.py`. Runners import neither the manager nor its web server, and the
  process runner nothing of ComfyUI.
- `runners/process_run.py` imports the standard library only: it runs with `python -I`.
- The API runner (`runners/api/`) imports nothing of ComfyUI: the gateway has no ComfyUI.
- The manager imports no runner and nothing ComfyUI-specific: it reads definitions through
  `definitions.py`. A workflow's ComfyUI half (wiring, building the graph) is
  `runners/comfyui_workflow.py`, used by agents and `comfyfarm check`.
- Artifact handlers import neither the manager, the agent, a runner nor the web stack: both
  sides call them with a context.
- `catalog.py` imports neither the manager, the agent, a runner nor the web stack:
  `definitions.py` checks every definition's place in the menus against it.
- `comfyfarm --help` works without the web stack.

## How a change is made

1. One step of `docs/plan.md` is one pull request. Never push to `main`.
2. A pull request either moves code or changes behaviour, not both.
3. Every change comes with tests. Seams (runner, artifact handler, environment manager,
   agent protocol) get contract tests.
4. New behaviour is off by default and switched on in config after review.
5. A design choice that is not already in `docs/decisions/` gets a new record in the same
   pull request.

Done means: the full suite passes in CI, the layering tests pass, the README and example
configs match the behaviour, and the pull request says how it was tested and what was not.

## House style

- Commit subjects name the area, then say what changed: `Agent: start ComfyUI with ...`.
- Lines up to 120 characters. Comments explain why, in plain sentences.
- Errors an artist can read: say what is wrong and what to do about it.
- `config.yaml`, `agent.yaml`, `data/`, `logs/` and `run/` are per machine and never committed.

## Things that bite

- **Package index.** Studio machines default to an internal pip mirror. Every install
  names `https://pypi.org/simple`, and torch comes from the PyTorch index.
- **Pipeline environment.** The farm's Python must not see the pipeline's `PYTHONPATH`
  and rez settings. Start-up scripts and `comfyenv/sync.py` strip them; new code that
  starts a Python must do the same.
- **Identity.** With no `auth.api_keys` the manager trusts the user name a client sends.
  That stays for the pilot, so no endpoint may run code or commands it is handed.
- **Shared folder.** Agents and clients find the manager through `manager.json` in the
  bank folder (`share.dir`). Whoever can write there is trusted.
- **Two operating systems.** Agents run on Linux and on Windows. Agent code must work on
  both, and paths go through `fileio/paths.py`.
- **Provider keys.** Only on the gateway's machine, in the files `gateway.yaml` names; never
  in git, the bank folder, `job.json`, events or logs (`tests/test_api_runner.py` checks).
  Nothing goes to fal.ai itself unless `gateway.yaml` says `live: true` for it (decision
  0062): tests and CI talk to `tools/mock_fal.py` only, and never set it against fal.ai.
- **Paid jobs.** A job at an outside provider is held against its show's cap as it is queued,
  and settled as it ends (`comfyfarm/manager/ledger.py`, decision 0060). The ledger never
  guesses down: money is released only when the job certainly never reached the provider, or
  the provider's bill shows it didn't (`bills.py`, decision 0061). A paid job is never queued
  again. Billing keys stay on the gateway: the manager asks it to read the bills.
- **Studio-only steps.** The Daiquiri graph, Tractor and ShotGrid cannot be reached from
  outside Rodeo's network. Those steps are done there and merged back by pull request.
