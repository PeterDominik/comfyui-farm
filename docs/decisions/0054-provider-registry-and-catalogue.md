# 0054. Menus by provider, then task: a provider registry and a task list in the repo, outside the fingerprint

Date: 2026-11-23. Status: accepted.

## Context

Phase 11 (docs/plan.md) brings runners that aren't ComfyUI on our own GPUs:

- outside providers, fal.ai first, through a gateway;
- LoRA training and Griptape as processes.

Artists pick from one flat list of workflows today. Peter's plan of 23 November ("AI Farm:
Providers, Catalogue and Spending Plan") makes every menu read Provider > Task > Workflow,
for example ComfyUI Local > Masking > SAM3, or fal.ai > Video > Seedance 2.0. The artist
picks the provider, and each provider has its own label, place in the menus and off-site
flag.

## Decision

- **Providers are files in the repo,** one per provider in `providers/<name>.yaml`:
  - `label`: its name in the menus;
  - `runner`: the runner type its definitions have;
  - `target`: where its jobs go;
  - `offsite`: whether its jobs send material outside Rodeo;
  - `order`: its place in the menus, smallest first;
  - `description`.

  Unknown keys are refused. The two that exist today are `comfyui-local` (runner
  `comfyui`, target `pool`) and `python-local` (runner `process`, target `pool`). Later
  providers add their own keys (lanes, prices, the names of their keys) in their own
  steps.
- **Tasks are one list, `workflows/tasks.yaml`,** in menu order: a name and a label each,
  with an optional description. It sits beside the definitions, which name its tasks. The
  first five are Masking, Video, Image, Textures and Tests.
- **A definition gains four optional fields:**
  - `task`: a name from tasks.yaml.
  - `provider`: needed only when it isn't the usual one of the definition's runner type.
    `comfyui` is `comfyui-local` and `process` is `python-local`.
  - `family`: one name for the same model on several providers, for comparing their
    prices later.
  - `hidden: true`: keeps it out of artists' menus. The dashboard, the CLI and evals still
    run it.
- **Checked when the definition loads.** These make a problem, which turns the definition
  off:
  - an unknown task or provider;
  - a provider whose runner type isn't the definition's;
  - a field of the wrong shape.

  A definition with no task loads, and the menus show it under its provider with no task.
  Each of the farm's six now has a task, and `gpu_test` is hidden.
- **The catalogue is the checkout's,** read again when its files change
  (`comfyfarm/catalog.py`). It needs no runner, the manager or the web stack. A broken
  catalogue file is said once, by `comfyfarm check` and in the manager's log. It is not put
  on every definition, so a bad edit to tasks.yaml doesn't turn every workflow off.
- **The menu fields aren't part of the fingerprint.** A definition's fingerprint pins
  what a job runs, and where it shows in a menu doesn't change that. Adding these fields
  changed none of the six fingerprints. A machine that hasn't updated its checkout yet
  keeps taking jobs, and moving a workflow to another task later never makes one refuse a
  job.
- **The menus go on at once, with no switch** (Peter, 23 November), when their steps land
  (the catalogue endpoint, the dashboard, Nuke). That departs from "new behaviour is off by
  default" in CLAUDE.md, for the client side of this one change: menus that show the same
  workflows in a new order, with nothing new run or sent anywhere. Providers that send work
  elsewhere (fal.ai) are still off until switched on, and allowed per show by the policy.
- **`comfyfarm check` says where each workflow shows,** for example "in the menus under
  ComfyUI Local > Masking", and checks the catalogue's files.
- **`GET /api/v1/catalog?show=` is the one tree every client draws** (step 1's second pull
  request, the same day). Clients hard-code no tree, so changing the menus never needs a
  client update.
  - **Order:** providers in their order, their tasks in tasks.yaml's order, workflows by
    label.
  - **Empty branches:** a provider or a task with nothing in it isn't listed.
  - **A refused workflow stays listed.** When the show's policy refuses it, it has
    `allowed: false`, the policy's reason and the rule. Nuke can then say "not on this
    show" and why, instead of the workflow vanishing. `GET /api/v1/workflows` stays as it
    is, since forms are built from its schemas.
  - **Each workflow carries** its family's siblings on other providers (empty until a model
    exists on two) and whether it is off-site. Estimates come with the ledger (step 4).
  - **Hidden ones** are left out unless `hidden=true`, which the dashboard asks for.
  - **Nothing goes missing.** A workflow with no task is under "Other", after its provider's
    tasks. One whose task or provider the catalogue doesn't have stays listed under that
    name, after the known ones. A turned-off one stays listed with its problems. So a
    broken file never empties a menu.
  - **The shared client** has `catalog(show, hidden)`, and
    `python comfyfarm_client.py catalog show=bri3` prints the menus.

## Consequences

- Adding a provider is a file in `providers/`. Adding a task is a line in tasks.yaml.
  Moving a workflow is a change to its `task:`. Each is one reviewed pull request, and no
  client changes.
- Everything stays one list until the catalogue endpoint and the clients use these fields
  (step 1's later pull requests). This pull request changes what `comfyfarm check` prints,
  and nothing a job does.
- The pilot review's first check (the five workflows run from Nuke) will run through the
  new menus once step 1's Nuke pull request lands.
- `label` and `description` remain in the fingerprint, as before.
