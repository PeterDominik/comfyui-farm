# ComfyUI farm gateway

A small job queue and REST API in front of ComfyUI, so artists can run studio
workflows on the RTX 5090 box from a browser, Nuke, or a script, without touching
ComfyUI itself.

```
Artists (dashboard / Nuke / CLI)  →  Gateway :8000 (FastAPI + SQLite)  →  ComfyUI 127.0.0.1:8188
                                       queue · monitor · versioned outputs     never exposed
```

- **Queue owned by the gateway.** Jobs survive ComfyUI crashes and gateway restarts.
  Dispatch order is priority first, then whoever has the fewest jobs running, then age.
  ComfyUI only ever holds one running job plus one waiting.
- **Live monitoring** over ComfyUI's websocket shows status, the current node, sampler
  steps, progress, and queue position with an ETA learned from past runs. A periodic
  check against ComfyUI's queue and history catches anything the websocket missed.
- **Pipeline-aware inputs.** Paste `/rdo/shows/.../shot.step.%04d.png 1001-1158` and the
  gateway maps it to the farm path. It checks for missing frames and works out exactly
  which files the loader will read. Bad input is refused at submit, not 10 minutes in.
- **Versioned outputs:** `renders/<shot>/<shot>_alpha_v003/<shot>_alpha_v003.1001.png`
  numbered to match the plate, plus an MP4 preview when the workflow declares one.
  Artists can pick the output folder; versioning still applies inside it.
- **Preflight checks** catch a missing node pack, a node-pack update that renamed an
  input, or a missing model before a job is sent (`python -m comfyfarm check`).
- **Cancel, retry, and the exact API JSON** of any job, for loading back into ComfyUI to debug.

## Repo layout

| Path | What |
|---|---|
| `comfyfarm/` | The gateway: API, dispatcher/monitor, job DB, workflow builder, dashboard |
| `workflows/<name>/` | `workflow_api.json` (ComfyUI API export) + `manifest.yaml` (what's exposed) |
| `client/comfyfarm_client.py` | Stdlib-only client and CLI (Nuke, Houdini, terminal) |
| `client/nuke/` | Nuke menu: submit selected Read nodes, auto-create Reads for results |
| `scripts/` | Windows setup, services (NSSM), firewall, update |
| `tools/mock_comfyui.py` | Fake ComfyUI for testing without a GPU |
| `tests/` | Unit tests + end-to-end tests against the mock |

## Install on the Windows farm machine

Needs **Git for Windows**, **Python 3.10+** (python.org, tick *Add to PATH*), and for the
services, **[NSSM](https://nssm.cc)**. ComfyUI must have the custom node packs this
workflow uses: VideoHelperSuite, WAS Node Suite, Easy-Use and ComfyUI-LTXVideo.

```powershell
git clone https://github.com/<org>/comfyui-farm.git C:\comfyfarm
cd C:\comfyfarm
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` creates `.venv`, installs the requirements, copies `config.example.yaml` to
`config.yaml` (git-ignored, so `git pull` never overwrites it), and runs the preflight
check. Review `config.yaml`: path mappings, output layout, workers, optional API keys.

Try it in a console first:

```powershell
scripts\run_gateway.bat          # then open http://localhost:8000
```

Then make it permanent (as Administrator):

```powershell
# Gateway as a service; ComfyUI keeps running however you run it now:
scripts\install_services.ps1 -User "DOMAIN\svc_account"
# ...or both as services:
scripts\install_services.ps1 -ComfyUIDir "C:\ComfyUI_windows_portable" -User "DOMAIN\svc_account"
scripts\open_firewall.ps1        # TCP 8000, domain network only
```

> **Service account.** ComfyUI reads plates from, and writes renders to, the show share.
> Services running as LocalSystem usually can't reach it, so use a domain account with
> access to both (`-User`). If ComfyUI stays in your own logged-in session, it uses
> your access.

> **Keep ComfyUI on localhost.** Remove `--listen` from its launch line if you added it.
> Only the gateway should talk to it.

### Updating

```powershell
scripts\update.ps1               # git pull, pip install, preflight, restart service
```

Queued and running jobs carry on across the restart.

## Using it

**Dashboard:** `http://<farm-machine>:8000`. Pick the workflow, paste the sequence path,
submit. The job table updates live. Click a row for input/output paths (copy buttons),
warnings, errors with traceback, the MP4 preview (if any), *Run again* and *API JSON*.

**Python / CLI** (`client/comfyfarm_client.py`, or download it from
`http://<farm-machine>:8000/client.py`):

```bash
export COMFYFARM_URL=http://comfyfarm01:8000
python comfyfarm_client.py workflows
python comfyfarm_client.py submit ltx25_alpha_gen \
    "input=/rdo/shows/<show>/.../<shot>.preprocess.%04d.png 1001-1158" seed=7 --wait
python comfyfarm_client.py list --mine
python comfyfarm_client.py cancel 3f2a9c1e
```

```python
import comfyfarm_client as farm
job = farm.submit("ltx25_alpha_gen", {"input": ".../plate.%04d.png 1001-1158", "seed": 7})
job = farm.wait(job["id"])                  # call from a thread inside a DCC
farm.output_pattern(job)                    # /rdo/shows/.../<shot>_alpha_v001.%04d.png
```

**Nuke:** put `client/comfyfarm_client.py` and `client/nuke/comfyfarm_nuke.py` on
`NUKE_PATH`, set `COMFYFARM_URL`, and add the lines from `client/nuke/menu.py`. Then
*ComfyFarm → Alpha from selected Read* submits every selected Read with its frame
range. When a job finishes, a Read node for the result appears next to its source.

### Parameters for `ltx25_alpha_gen`

| Param | Default | Notes |
|---|---|---|
| `input` | required | Sequence path with `%04d` / `%d` / `####` and optional `first-last` range. png/jpg/tif/webp only |
| `output_path` | show default | Folder on show storage that already exists; jobs go into `<shot>/<shot>_alpha_vNNN` inside it. Empty = `paths.output_root` from `config.yaml` |
| `seed` | 42 | |
| `enhance_prompt` | false | Gemma prompt enhancer |
| `shorter_side` | 1088 | Working resolution; multiple of 32 |
| `fps` | 24 | |

Optional job fields: `name`, `priority` (−10…10; above 0 is admin only when keys are on),
and `show` / `shot` overrides for paths the regexes can't parse.

### REST API

| Method | Path | |
|---|---|---|
| GET | `/api/v1/health` | Liveness, auth on/off, queue size |
| GET | `/api/v1/workflows` | Workflows and parameter schemas, plus any preflight problems |
| POST | `/api/v1/jobs` | Submit `{workflow, params, name?, priority?, show?, shot?}` → job |
| GET | `/api/v1/jobs?user=&status=&active=true&limit=` | List jobs, with `queue_position` and `eta_seconds` |
| GET | `/api/v1/jobs/{id}` | One job (full id or 8-char prefix) |
| DELETE | `/api/v1/jobs/{id}` | Cancel (queued, waiting in ComfyUI, or running) |
| POST | `/api/v1/jobs/{id}/retry` | Same params, new version |
| GET | `/api/v1/jobs/{id}/prompt` | The exact API JSON sent to ComfyUI |
| GET | `/api/v1/jobs/{id}/preview` | MP4 preview |
| GET | `/api/v1/workers` | Online state, GPU, free VRAM, slots, preflight problems |
| WS | `/api/v1/events` | Live job and worker updates (snapshot first) |

Interactive docs are at `/docs`. Identify yourself with an `X-User` header, or `X-API-Key`
when `auth.api_keys` is set in `config.yaml`.

Job statuses: `queued` → `dispatched` (waiting in ComfyUI) → `running` →
`postprocessing` (renaming frames, copying preview) → `done`, or `failed` / `cancelled`.

## Adding or updating a workflow

1. In ComfyUI: **Workflow → Export (API)**. Save it as `workflows/<name>/workflow_api.json`.
   Blank out show-specific paths. The gateway fills those per job, and this keeps show
   codes out of git.
2. Write `workflows/<name>/manifest.yaml` (copy `ltx25_alpha_gen`, see below).
3. `python -m comfyfarm check`, then restart the gateway.

Paths left in the export from your last ComfyUI session are harmless: the gateway
overwrites the input, output and save-path nodes on every job.

Re-exporting can renumber nodes. Because every manifest target names its node class,
a renumbered workflow fails the check with a clear message. It can't silently write
the seed into the wrong node.

### Workflow manifests

The manifest says what artists can set and where each value goes in the API JSON.
Anything not listed stays exactly as exported.

```yaml
name: ltx25_alpha_gen          # id used by the API and clients
label: LTX 2.5 alpha generation
tag: alpha                     # goes into output folder and file names
template: workflow_api.json

params:                        # becomes the form in the dashboard, in this order
  seed:
    type: int                  # int | float | bool | text | string | choice
    label: Seed
    default: 42
    min: 0                     # optional: min, max, multiple_of, choices (for choice), help
    targets:                   # where the value is written; one param can drive several nodes
      - {node: "5516:4832", input: noise_seed, class: RandomNoise}

  input:  {type: sequence, label: Input sequence, required: true}
  output_path: {type: output_folder, label: Output folder}

sequence:                      # nodes the `sequence` param drives
  path_node: {node: "9094", input: value, class: PrimitiveString}
  loader: {node: "9104", class: VHS_LoadImagesPath}

outputs:                       # nodes the gateway points at each job's versioned folder
  root_node: {node: "9093", input: value, class: PrimitiveString}
  frames: {node: "9109", class: "Image Save", path_input: output_path, prefix_input: filename_prefix, ext: png}
  preview: {node: "...", class: VHS_VideoCombine, prefix_input: filename_prefix}   # optional
```

- **Plain params** (`int`, `text`, …) are written straight into `targets`: node id as
  it appears in the API JSON (`"5516:4832"` for nodes inside groups/subgraphs), the
  input name, and the class as a guard. Validation (min/max/required) happens before
  anything reaches ComfyUI.
- **`sequence` and `output_folder`** take no `targets`; the gateway wires them in.
  A sequence is mapped to the farm path, frame-checked and turned into the loader's
  directory/skip/cap. An output folder is checked (exists, on show storage) and used
  as the root for `<shot>/<shot>_<tag>_vNNN`.
- **Nodes in `sequence` and `outputs` belong to the gateway.** A plain param targeting
  them would be overwritten on every job, which is why the output folder is its own type
  rather than a param pointed at `OUTPUT_PATH`.
- Removing a param from the manifest changes the API: clients still sending it get
  "Unknown parameter". Update `client/nuke/comfyfarm_nuke.py` to match.

## Operations notes

- **Logs:** `logs/comfyfarm.log` (rotating), plus `logs/*-service.log` for the services.
- **State:** `data/comfyfarm.db` (SQLite). Back it up or delete it; nothing else depends on it.
- **Restarts:**
  - Gateway restarts mid-job: on reconnect it reads ComfyUI's history and finishes the job.
  - ComfyUI crashes mid-job: the job is requeued once (`farm.max_attempts`), with a warning on the job.
  - Jobs on a machine that stays unreachable for 5 minutes are requeued.
- **Pin WAS Node Suite.** This workflow saves frames with WAS *Image Save* using its
  `output_path` input. The current WAS release replaced that input with a sandboxed
  `root` setting. Don't update WAS on the farm machine without re-testing; the preflight
  check flags it if it happens.
- **This workflow's outputs are at the working resolution** (`shorter_side`, 1088 by
  default), not the plate's. Reformat in comp if you need plate res. If the model returns
  a different number of frames than it was given, the job carries a warning.
- **Security:** with no API keys, anyone on the network can submit and cancel, and user
  names are taken on trust. Set `auth.api_keys` to require keys.

## Development

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -t . -v      # unit + end-to-end (~30 s)

# Manual testing without a GPU:
python tools/mock_comfyui.py --port 8188 --output mock_output
python -m comfyfarm serve
```

The end-to-end suite runs the real gateway against the mock. It covers submit to done
(frame renaming, preview, versioning), cancelling in each state, failures with
tracebacks, bad input, a gateway restart mid-job, a ComfyUI crash mid-job, and a
missing node pack.
