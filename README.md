# ComfyUI farm gateway

A small job queue and REST API in front of ComfyUI, so artists can run studio
workflows on the farm's GPUs from a browser, Nuke, or a script, without touching
ComfyUI itself.

```
Artists (dashboard / Nuke / CLI)
     │  HTTP :8000
Manager (FastAPI + SQLite)        queue · planning · monitoring · versioned outputs
     ▲  agents check in over HTTP; they need no open port
Agent, one per GPU machine        converts inputs · runs the job · writes the outputs
     │  localhost
ComfyUI 127.0.0.1:8188            never exposed
```

One machine can run all of it (`standalone`), or the manager runs on one machine and an
agent on each GPU machine, Linux or Windows (*Deploying* below).

- **Queue owned by the manager** (the gateway). Jobs survive ComfyUI crashes and restarts
  of the manager or an agent. Dispatch order is priority first, then whoever has the
  fewest jobs running, then age. Each ComfyUI only ever holds one running job plus one
  waiting.
- **Live monitoring** over ComfyUI's websocket shows status, the current node, sampler
  steps, progress, and queue position with an ETA learned from past runs on each GPU type. A periodic
  check against ComfyUI's queue and history catches anything the websocket missed.
- **Pipeline-aware inputs.** Paste `/rdo/shows/.../shot.step.%04d.png 1001-1158` and the
  gateway maps it to the farm path. It checks for missing frames and works out exactly
  which files the loader will read. Bad input is refused at submit, not 10 minutes in.
- **PNG/JPG/TIF, EXR and movies, one workflow.** EXR sequences are converted with the
  farm's OCIO config (`ocio/` in this repo), exactly like a Nuke Write set to
  `default (color_picking)`; `.mov`/`.mp4` are numbered from their timecode. See
  *Input formats* below.
- **Outputs that never overwrite.** Frames go to `<output folder>/<shot>/`, named after
  their source and numbered like the plate:
  `.../comfy/test/bpash_1545/bpash_1545.preprocess.mp01_png.v1.alpha_v001.1001.png`.
  Each run takes the next free version (`alpha_v002`, …) in that folder. Left empty,
  the output folder is the show's renders folder. The form shows the exact
  destination before you submit. Nothing is ever written into `.published`.
- **Any shot naming.** Show, sequence and shot are read from their position in the
  path, so `bpash_1545` and `15454_656551` both work. The gateway knows two layouts:
  - publishes, `/shows/<show>/.published/<seq>/<shot>/`;
  - shot movhistory folders, `/shows/<show>/<seq>/<shot number>/movhistory/`. There
    `/rdo/shows/raa/bpash/1545/movhistory/` gives shot `bpash_1545`.

  More layouts can be added in `config.yaml` (`paths.context_patterns`). If a path
  matches none of them, the gateway asks for an output folder instead of guessing.
- **Texture workflows.** `chord_pbr` turns a diffuse texture (or its UDIM tiles) into
  PBR maps with Ubisoft's CHORD, written as linear EXRs named by the TEX naming
  convention: `acescg_map1_noTile_defState_color01.1001.0000.exr`. See *Texture workflows*.
- **One-image workflows.** `wan22_i2i` reworks one image with Wan 2.2 (image to image,
  steered by a prompt). It takes a still, or one frame of a sequence or movie, named after
  the path (`…/plate.%04d.exr 1050`); the result is numbered like that frame.
- **Several inputs.** `ltx25_layout_render` renders a layout sequence with LTX 2.5's
  layout-to-render IC-LoRA, taking its look from a reference frame and a prompt.
- **Preflight checks** catch a missing node pack, a node-pack update that renamed an
  input, or a missing model before a job is sent (`python -m comfyfarm check`).
- **Cancel, retry, and the exact API JSON** of any job, for loading back into ComfyUI to debug.

## Repo layout

| Path | What |
|---|---|
| `comfyfarm/manager/` | The manager: API, dashboard, job database, planning, texture sets, the queue, and the desk agents check in with (`desk.py`) |
| `comfyfarm/agent/` | The agent, one per ComfyUI: takes jobs from the manager, converts their inputs, runs them, follows them live, writes the outputs |
| `comfyfarm/fileio/` | Files in, files out: paths, inputs (sequences, UDIM tiles, movies), EXR/movie conversion, OCIO, output names, writing the outputs. Standard library only |
| `comfyfarm/workflows.py` | Workflow manifests: parameters, validation, building the API JSON |
| `comfyfarm/protocol.py` | Job states, and the messages between agents and the manager |
| `comfyfarm/shotgrid/` | The manager's read-only ShotGrid lookups: active shows, their sequences and shots, cached, for the publish fields; the publish check |
| `comfyfarm/publish/` | Publishing finished jobs through the pipeline: Nuke templates and their placeholder Reads, staging them, and dispatching Daiquiri's `update_and_render_nuke` to Tractor (see *Publishing*) |
| `comfyfarm/comfyenv/` | The farm's ComfyUI environment: what each machine runs, how it differs from `deploy/comfyui/environment.yaml`, and `sync` to bring it there |
| `comfyfarm/cli.py` | `python -m comfyfarm standalone` (or `serve`), `manager`, `agent`, `check` and `comfyui snapshot\|verify\|sync` |
| `config.example.yaml`, `agent.example.yaml` | The manager's settings; one GPU machine's agent |
| `workflows/<name>/` | `workflow_api.json` (ComfyUI API export) + `manifest.yaml` (what's exposed): `ltx25_alpha_gen`, `chord_pbr`, `wan22_i2i`, `ltx25_layout_render` |
| `client/comfyfarm_client.py` | Stdlib-only client and CLI (Nuke, Houdini, terminal) |
| `client/nuke/` | Nuke panel: the dashboard's form and job list inside Nuke |
| `deploy/linux/`, `deploy/windows/` | Setup, running and keeping it running on each kind of machine (see *Deploying*) |
| `deploy/comfyui/` | The farm's ComfyUI: `environment.yaml` (versions, node packs, models) and `extra_model_paths.yaml` (see *The farm's ComfyUI*) |
| `tools/mock_comfyui.py` | Fake ComfyUI for testing without a GPU |
| `tests/` | Unit tests + end-to-end tests against the mock |

## Deploying

The farm runs on one machine (`standalone`: the manager plus an agent per local ComfyUI),
or as a **manager** on one machine and an **agent** on each GPU machine, Linux or
Windows. Nothing below needs sudo or admin rights, except the optional Windows services.

Every machine runs from its own git checkout of this repo, on local disk, at the same
commit. ComfyUI needs the custom node packs the workflows use: VideoHelperSuite, WAS Node
Suite, Easy-Use and ComfyUI-LTXVideo, plus
[ComfyUI-Chord](https://github.com/ubisoft/ComfyUI-Chord) with `chord_v1.safetensors` in
`ComfyUI/models/checkpoints` for `chord_pbr`. CHORD's weights are under Ubisoft's
research-only licence. The farm's own nodes (`comfyui_nodes/`: *ComfyFarm Load Images*,
*ComfyFarm Load Image*, *ComfyFarm Save Image*) come with this repo; the setup scripts link
them into ComfyUI (`--comfyui` on Linux, `-ComfyUIDir` on Windows). The tools for EXR and movie inputs come with the Python
requirements: `oiiotool` (OpenImageIO, with OpenColorIO) and `ffmpeg` (imageio-ffmpeg).

| Script | What |
|---|---|
| `deploy/linux/setup.sh manager\|agent` | Conda env (Python 3.12, your Miniconda), requirements (from pypi.org rather than the pipeline's package mirror; `--index-url` to change), `config.yaml` or `agent.yaml` from its example, the check |
| `deploy/linux/farmctl.sh start\|stop\|restart\|status\|ensure\|log\|check manager\|agent` | Runs it in the background, with the pipeline's Python settings (`PYTHONPATH`, rez) removed |
| `deploy/linux/install_cron.sh manager\|agent` | Your crontab starts it at boot and checks it every minute (`--remove` to undo) |
| `deploy\windows\setup.ps1 [-Agent]` | `.venv`, requirements, `agent.yaml` (`-Agent`) or `config.yaml`, the check |
| `deploy\windows\install_startup.ps1` | Starts the agent when you log in, and again if it exits (Startup folder, no admin; `-Stop`, `-Remove`) |
| `deploy\windows\install_services.ps1` | `standalone` and ComfyUI as services (NSSM, admin) |
| `deploy\windows\run_gateway.bat`, `update.ps1`, `open_firewall.ps1` | `standalone` in a console; update; open port 8000 (admin) |

### The manager (Linux)

On local disk (here `/mntx/raid/pdominik`), with Miniconda already there:

```bash
git clone https://github.com/PeterDominik/comfyui-farm.git /mntx/raid/pdominik/comfyui-farm
cd /mntx/raid/pdominik/comfyui-farm
deploy/linux/setup.sh manager --conda /mntx/raid/pdominik/miniconda3
```

Then edit `config.yaml`:

- `share.dir: /rdo/bank/comfy_farm`: the shared folder (below);
- `agents.token`: a long random string, e.g. `python3 -c "import secrets; print(secrets.token_hex(16))"`;
- `agents.order` (optional): agent names that get jobs first, best first. Without it the
  biggest GPUs (VRAM) get jobs first;
- `paths.mappings: [{posix: /rdo/shows}, {posix: /shows, local: /rdo/shows}]`: this machine sees show
  storage as it is, and `/shows/...` inputs are the same storage (the farm reports them as `/rdo/shows/...`);
- `paths.output_root: /rdo/shows/{show}/_project_ref/_cg/comfyui/renders`;
- `workers: []`: agents check in instead.

```bash
deploy/linux/farmctl.sh check manager     # workflows, tools, OCIO, storage, the shared folder
deploy/linux/farmctl.sh start manager     # then open http://<this machine>:8000
deploy/linux/install_cron.sh manager      # keep it running: at boot, and checked every minute
```

The database (`data/`) and logs (`logs/comfyfarm.log`, console output in
`logs/manager.out`) stay in the checkout. If the workstation's firewall blocks port 8000,
agents and artists can't reach it: that's the one thing that may need IT.

**The shared folder** (`share.dir`) is how everyone finds the manager without a machine
name in any config. At every start the manager writes `manager.json` (its address) there and
copies the repo's `client/` into it, so the client artists run always matches the manager.
If the manager moves to another machine, start it there: artists and agents follow.

```
/rdo/bank/comfy_farm/
  manager.json       {"url": "http://<manager>:8000", "version": ..., ...}
  client/            comfyfarm_client.py and nuke/ (the panel)
```

### An agent on a Linux GPU machine

```bash
git clone https://github.com/PeterDominik/comfyui-farm.git <local disk>/comfyui-farm
cd <local disk>/comfyui-farm
deploy/linux/setup.sh agent --conda <local disk>/miniconda3 --comfyui <ComfyUI folder>
```

Edit `agent.yaml` (see `agent.example.yaml`):

- `name`;
- `manager: /rdo/bank/comfy_farm/manager.json` (or the manager's URL), and `token`;
- `comfyui` and `max_inflight` (2 on a 32 GB card);
- `paths.mappings`: none needed where `/rdo/shows` is mounted as is;
- `convert.scratch_dir`: on local disk;
- `comfyui_launch` (optional): how to start ComfyUI. The agent then starts ComfyUI when it
  isn't running and starts it again if it dies. Stopping or restarting the agent leaves
  ComfyUI and its jobs running.

Then:

```bash
deploy/linux/farmctl.sh check agent       # manager + token, ComfyUI and its nodes, storage, scratch, tools
deploy/linux/farmctl.sh start agent
deploy/linux/install_cron.sh agent
```

The agent shows up on the dashboard's worker list once it has checked in and its
ComfyUI's nodes are checked. Logs: `logs/agent-<name>.log`; ComfyUI's output, when the
agent starts it, `logs/comfyui-<name>.log`.

### An agent on a Windows GPU machine

Needs **Git for Windows** and **Python 3.10+** (python.org, tick *Add to PATH*).

```powershell
git clone https://github.com/PeterDominik/comfyui-farm.git D:\comfyfarm
cd D:\comfyfarm
powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1 -Agent -ComfyUIDir "C:\ComfyUI_windows_portable"
```

Edit `agent.yaml`:

- `name`;
- `manager: http://<manager>:8000` (or the shared folder's `manager.json` as this
  machine sees it), and `token`;
- `paths.mappings: [{posix: /rdo/shows, windows: //rodeofx.com/smb/shows}]`;
- `convert.scratch_dir`;
- `comfyui_launch`, optionally, e.g.
  `command: [C:/ComfyUI_windows_portable/python_embeded/python.exe, -s, ComfyUI/main.py, --windows-standalone-build, --disable-auto-launch]`
  with `cwd: C:/ComfyUI_windows_portable`.

Then:

```powershell
.venv\Scripts\python.exe -m comfyfarm agent --check
powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -StartNow
```

It starts whenever you log in (locking the screen is fine, logging out stops it) and
restarts the agent if it exits. `install_startup.ps1 -Stop` stops it; `-Remove` stops it
starting at logon.

### Artists (Nuke)

Each artist runs this once, then restarts Nuke:

```bash
python3 /rdo/bank/comfy_farm/client/nuke/install_for_me.py
```

It adds one line to their `~/.nuke/init.py` (`--remove` takes it out again). The panel
finds the manager through `manager.json` next to the client. A `COMFYFARM_URL` in
someone's environment wins, and an address typed into the panel's *Farm* field applies
until Nuke closes. Later, a Rez package can put `/rdo/bank/comfy_farm/client/nuke` on
`NUKE_PATH` instead.

### One machine (standalone, Windows)

The whole farm on one Windows machine, as before: the manager plus an agent for each
ComfyUI in `config.yaml`'s `workers:`.

```powershell
git clone https://github.com/PeterDominik/comfyui-farm.git C:\comfyfarm
cd C:\comfyfarm
powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1 -ComfyUIDir "C:\ComfyUI_windows_portable"
deploy\windows\run_gateway.bat          # then open http://localhost:8000
```

As services, with admin rights and [NSSM](https://nssm.cc):

```powershell
deploy\windows\install_services.ps1 -User "DOMAIN\svc_account"          # ComfyUI keeps running as you run it now
deploy\windows\install_services.ps1 -ComfyUIDir "C:\ComfyUI_windows_portable" -User "DOMAIN\svc_account"
deploy\windows\open_firewall.ps1        # TCP 8000, domain network only
```

> **Service account.** ComfyUI reads plates from, and writes renders to, the show share.
> Services running as LocalSystem usually can't reach it, so use a domain account with
> access to both (`-User`).

> **Keep ComfyUI on localhost.** Remove `--listen` from its launch line if you added it.
> Only its agent should talk to it.

### Updating

Run the same commit everywhere: the manager logs a warning when an agent runs another
version, and `/api/v1/workers` lists each agent's `agent_version`. On each machine:

```bash
git pull && deploy/linux/setup.sh manager|agent && deploy/linux/farmctl.sh restart manager|agent
```

```powershell
git pull; deploy\windows\setup.ps1 -Agent; deploy\windows\install_startup.ps1 -Stop -StartNow
```

Queued and running jobs carry on across restarts of either. ComfyUI loads the farm's own
nodes only at startup, so after a pull that changed them its agent takes no new jobs and,
once it holds none, restarts ComfyUI itself (when it starts ComfyUI, `comfyui_launch`).
The Machines tab shows *new farm nodes: restarting ComfyUI when idle* meanwhile. Without
`comfyui_launch`, restart ComfyUI by hand. (`standalone` with services:
`deploy\windows\update.ps1`.)

### The farm's ComfyUI

Every farm machine should run the same ComfyUI, so a job comes out the same wherever it
runs. `deploy/comfyui/environment.yaml` records it, from a reference machine:

- ComfyUI's version and git commit, and the launch flags that change results
  (`--use-sage-attention`, `--fast`, precision flags).
- Python, and the packages that matter: torch, torchvision and torchaudio with their CUDA
  build (`2.7.1+cu128`), xformers, sageattention, triton, transformers and the like. A
  package that exists on one OS only (`triton-windows`) is pinned per OS. Same versions
  everywhere is safe on mixed cards: the CUDA 12.8 builds the 5090 needs also run on a
  3090. What each machine needs on top is an NVIDIA driver new enough for that build
  (`driver_min`, 570 for CUDA 12.8). Results can still differ slightly between card
  generations (a 3090 has no FP8 hardware), whatever the packages.
- The node packs the farm's workflows use: git commit or registry version, and a hash of
  their Python files, so a pack edited in place shows too. Other packs on a machine don't
  count, nor do the packs and models of workflows a machine never gets (`requires`: LTX on
  the 3090).
  Packs live on each machine (`ComfyUI/custom_nodes`), except those in the farm library
  (ComfyUI-Chord).
- The models the farm's workflows name: which library each comes from and its size (not
  its contents). Models come from the two shared libraries only, `/rdo/bank/comfyui` and
  `/rdo/bank/comfy_farm/comfyui` (`B:\comfyui`, `B:\comfy_farm\comfyui` on Windows);
  a copy in ComfyUI's own `models/` folder shadows the library's and shows as a difference.

`deploy/comfyui/extra_model_paths.yaml` is the farm's `extra_model_paths.yaml`, the same
for every farm ComfyUI (studio paths; `B:\` on Windows).

```bash
python -m comfyfarm comfyui snapshot    # on the reference machine, with ComfyUI running: writes environment.yaml
python -m comfyfarm comfyui verify      # on any machine: how its ComfyUI differs (exit 1 when it does)
python -m comfyfarm comfyui sync        # what it would take to bring this machine to it
python -m comfyfarm comfyui sync --apply
```

All read `agent.yaml` for ComfyUI's folder and Python (`comfyui_dir`, `comfyui_python`,
else from `comfyui_launch`) and its address; `--comfyui`, `--python` and `--url` override
them. Review the snapshot's diff before committing it. On Linux,
`deploy/linux/farmctl.sh verify|sync agent [--apply]` runs them with the farm's Python and
the pipeline's Python settings kept out.

**sync** fixes what verify lists that it can fix safely, and says how to do the rest:

- ComfyUI (a git checkout) is checked out at the file's commit.
- Node packs: a git pack is checked out at its commit, or cloned (a copy that isn't a git
  checkout is replaced by one); a registry pack is installed at its version from the Comfy
  registry; a local copy shadowing a pack of the farm library, or a second copy, is moved
  aside. Whatever it replaces goes to `<ComfyUI>/custom_nodes.old/`.
- One `pip install` (from pypi.org; `--index-url` to change it) of ComfyUI's and the changed
  packs' requirements and the file's package pins, with torch, torchvision, torchaudio and
  the packages built for the GPU (xformers, sageattention, flash-attn, triton) held at the
  versions this machine has: pip fails rather than replace them.
- `extra_model_paths.yaml` becomes the farm's, with the libraries as this OS sees them; the
  old one is kept as `extra_model_paths.yaml.<date>.bak`.
- Never done, only explained (with the command where there is one): torch & co., the NVIDIA
  driver, launch flags (`comfyui_launch` in agent.yaml), Python's version, models (they live
  in the libraries), and the shared packs of the farm library (every machine's).
- A git checkout with local changes is refused; `--force` discards them.

Without `--apply` it only prints the plan. With it, the machine must be drained on the
dashboard and hold no jobs (it asks the manager; `--no-manager` when the manager is down),
and ComfyUI's queue must be empty. It keeps the agent from starting ComfyUI meanwhile (the
Machines tab shows *updating ComfyUI (sync)*), stops the ComfyUI the agent started, does the
plan, lets the agent start ComfyUI again and verifies. Then *Resume* the machine.

Each agent also checks its machine when ComfyUI starts and every `environment_seconds`
(15 min), and the manager compares it with its copy of the file. The Machines tab marks
ComfyUI's version ✓ (matches) or ≠, and lists the differences under the machine. A
machine that differs keeps taking jobs; missing nodes or models still keep a workflow off
it, as before. `agent --check` lists the differences as warnings.

### How the manager and agents work together

- **Agents only ever call the manager** (`/api/v1/agent/...`), so GPU machines need no
  open port and the manager needs no list of them. An agent checks in every few seconds;
  one that stops for `farm.lost_after_seconds` (5 minutes) has its jobs queued again for
  the others.
- **A job goes to the best free GPU:** machines rank by VRAM (or by `agents.order` in
  the manager's config, then VRAM), and each job goes to the best machine that is free and
  can run its workflow; the next job to the next machine. Only when every GPU is busy does
  a machine get a second job, prepared ahead of its current one (the best machine first).
  The Machines tab shows the order (#). Machines whose ComfyUI lacks a workflow's nodes or
  models don't get it, nor do machines its manifest's `requires` keeps it off (too little
  VRAM: the LTX workflows need 32 GB, so the 3090 never gets them; missing tags). A job
  no machine may take now waits in the queue, saying why, and runs once one checks in.
- **Warm models:** between equal cards (two 5090s), the one that last ran the workflow,
  with its models still loaded, gets the job. A bigger free card still comes first.
- **Drain:** *Drain* on the dashboard's Machines tab lets a machine finish what it holds
  and take no new jobs (before working on it, or restarting it) until *Resume*. It lasts
  across manager restarts.
- **ETAs:** every finished run is timed per workflow and GPU type (an RTX 5090, whichever
  machine it's in), with its size: the frames it read, or a texture batch's tiles. Its
  time includes converting its inputs. From the recent runs the manager fits an overhead
  plus a time per frame, so a 200-frame job is expected to take longer than a 50-frame
  one, and the 3090 longer than a 5090. It then walks the queue the way jobs are handed
  out (best free GPU, then one prepared ahead, then whichever frees first) to say when
  each job will be done and which machine should take it. A workflow that hasn't run on a
  GPU type yet goes by its runs on the others; one that has never run has no ETA. The
  *Run times* tab lists them.
- **Paths travel as studio paths** (`/rdo/shows/...`) and each machine maps them its own
  way (`paths.mappings` in its config), so Linux and Windows workers mix. Inputs are
  converted on the agent that runs the job, in its own scratch folder.
- **Everything the farm writes is readable by everyone:** on Linux, agents add read
  permission for all to the output files and folders, whatever ComfyUI's umask. On
  Windows, the share's permissions apply.
- **Restarts:** a restarted agent follows its jobs again in its ComfyUI's queue and
  history. While the manager is down, agents finish what they hold and report it when the
  manager is back.

## Using it

**Dashboard:** the address in `manager.json` (e.g. `http://wrk1125l.rodeofx.com:9090`). Four tabs:

- **Submit:** pick the workflow, fill in the form, check the destination line, submit.
- **Queue:** everyone's jobs, live, with the workflow each runs and the machine it runs on (for a
  queued job, → the machine expected to take it). *Wait* shows its place in the queue and when it
  should be done, or the time left once running. Click a
  row for input/output paths (copy buttons), warnings, errors with traceback, the MP4 preview
  (if any), *Run again* and *API JSON*, and on a finished job *Publish…* (see *Publishing*).
- **Machines:** each GPU machine, in the order they get jobs (#): whether it takes jobs, its GPU and
  free VRAM, its jobs, versions, and any workflow it can't run (a missing node pack or model)
  and why. *Drain* / *Resume* per machine.
- **Run times:** how long each workflow takes on each GPU type, what the ETAs come from.

**Python / CLI** (`client/comfyfarm_client.py`, or download it from
`http://<farm-machine>:8000/client.py`):

```bash
export COMFYFARM_URL=http://comfyfarm01:8000
python comfyfarm_client.py workflows
python comfyfarm_client.py submit ltx25_alpha_gen \
    "input=/rdo/shows/<show>/.../<shot>.preprocess.%04d.png 1001-1158" --wait
python comfyfarm_client.py list --mine
python comfyfarm_client.py cancel 3f2a9c1e
python comfyfarm_client.py publish 3f2a9c1e step=lay --dry-run
```

```python
import comfyfarm_client as farm
job = farm.submit("ltx25_alpha_gen", {"input": ".../plate.%04d.png 1001-1158"})
job = farm.wait(job["id"])                  # call from a thread inside a DCC
farm.output_pattern(job)                    # /rdo/shows/.../<shot>_alpha_v001.%04d.png
```

**Nuke:** see [Nuke panel](#nuke-panel) below.

### Nuke panel

The ComfyFarm panel is the dashboard inside Nuke, with the same three tabs: *Submit*, *Queue*
and *Machines*, and publishing (*Publishing* below). The form is built from the farm's workflow list, so it has the same fields as
the web page and picks up manifest changes without a client update. It shows the same live
destination line. Nuke stays usable while jobs run, and a Read node for each finished job is
created next to its source. Jobs are always submitted from the form.

**Install:** each artist runs `python3 /rdo/bank/comfy_farm/client/nuke/install_for_me.py`
once and restarts Nuke (*Deploying → Artists*). The manager keeps that copy of the client
up to date, and the panel finds the manager through the `manager.json` next to it.

The panel works in Nuke 13 to 15 (PySide2) and Nuke 16+ (PySide6), and needs nothing
installed: the client is standard library only.

**Artist workflow:**

1. Prerender the plate to a PNG sequence with a Write node (`…/name.%04d.png`), with the
   colour baked in the way the workflow expects. The farm reads the PNGs exactly as they are.
2. Open the panel (*ComfyFarm → Open panel*, or *Windows → Custom → ComfyFarm*). On the
   *Submit* tab, fill in the form: paste the input path, *Browse…*, or select the Read (or
   the Write that rendered it) and click *From selected*, which fills in `path first-last`
   from the node. For a workflow that takes one image (`wan22_i2i`), it fills in
   `path <current frame>` instead: go to the frame first. A workflow with several inputs
   (a plate and its matte) has a field and a *From selected* for each.
3. Check the line under the form. It shows the frame count, the exact output folder, the
   first and last file names and about how long the job will take on which machine (and
   when it'll be done, with the queue ahead of it), or the problem (missing frames, no shot
   found in the path, and so on) before anything is queued.
4. Click *Submit*.
5. Follow the job on the *Queue* tab: everyone's jobs, like the dashboard, with the workflow,
   the machine running it (→ the one expected to take it, while queued) and when it should be
   done; tick *Only mine* or *Only active* to narrow it. When a job
   finishes, a Read appears next to the source node (turn this off on the *Submit* tab).
   Right-click a job to create its Read, copy the output path, reuse its input, cancel it,
   run it again, or see the error. Double-click a finished job to create its Read. The
   *Publish* column shows where its publish is (*when done*, *Tractor 6505856*, *dry run
   passed*, *publish failed*: hover for the details).
6. The *Machines* tab shows the farm's GPU machines and what each is running.

**Publishing from Nuke** (workflows that publish, when the farm has publishing on): tick
*Publish when done* under the form, or right-click a finished job, *Publish…*. The fields are
the dashboard's. Show and shot start as the input path's; when it names none, as the Nuke
session's `RDO_CURRENT_SHOW` / `RDO_CURRENT_SHOT` (with `RDO_CURRENT_SEQUENCE`; a shot given as
`0040` becomes `<sequence>_0040`), and the step as `RDO_CURRENT_STEP` when it's one of the
studio's. Everything can be changed, and a field you've typed in stops following the input
path. With ShotGrid lookups on, show and shot suggest as you type, *Sequence* narrows the
shots down and **↻** reloads them. *Publish log* (right-click) shows every publish of the job
with the command and Daiquiri's output.

Movies: a Read of a `movhistory` `.mov` sends the whole movie. The farm numbers its frames
from the timecode and drops the slate, as it does from the dashboard.

Outside Nuke, `python client/nuke/comfyfarm_panel.py` opens the same panel as a window,
if PySide6 or PySide2 is installed.

### Parameters for `ltx25_alpha_gen`

| Param | Default | Notes |
|---|---|---|
| `input` | required | Sequence path with `%04d` / `%d` / `####` and optional `first-last` range (png/jpg/tif/webp/exr), or a `.mov`/`.mp4` with an optional range in timecode frame numbers |
| `output_path` | show default | Root folder: frames go to `<this>/<shot>/<source>.alpha_vNNN.<frame>.png`, each run taking the next free version. Its parent must exist; never inside `.published`. Empty = the show's renders folder. Inputs outside the publish layout (no shot) go straight into it |

That's the whole form. The workflow's other settings are fixed in it: seed 42, working
resolution 1088 on the shorter side, 24 fps, prompt enhancer off. Sending any of them is
refused as an unknown parameter.

Optional job fields: `name`, `priority` (−10…10; above 0 is admin only when keys are on),
and `show` / `shot` overrides for inputs outside the publish layout.

### Parameters for `wan22_i2i`

| Param | Default | Notes |
|---|---|---|
| `input` | required | One image: a file, or a sequence or movie with the frame after it (`…/plate.%04d.exr 1050`; a movie's frames are numbered from its timecode). A sequence or movie without its frame is refused, saying which frames it has. Used at its own size |
| `prompt` | empty | What the image should become. Empty runs with the negative prompt only |
| `steps` | 12 | 1–100 |
| `cfg` | 3 | 0–30 |
| `noise` | 0.18 | The sampler's denoise, 0–1: low values keep the result close to the input |
| `output_path` | show default | Root folder: the image goes to `<this>/<shot>/<source>.i2i_vNNN.<frame>.png`. Same rules as above |

The seed and the negative prompt are fixed in the workflow. It needs Wan 2.2's low-noise
14B model (fp8) and Wan 2.1's VAE in `models/…/WAN/`, `umt5_xxl_fp8_e4m3fn_scaled` as its
text encoder, and the node packs for `WanVideoNAG`, `easy float` and the `res_2s` sampler
with the `beta57` scheduler; the Machines tab lists what a machine is missing.

```bash
python comfyfarm_client.py submit wan22_i2i \
    "input=/rdo/shows/<show>/.published/<seq>/<shot>/plate/v1/<shot>.plate.v1.%04d.exr 1050" \
    "prompt=wet street at night" noise=0.25 --wait
```

### Parameters for `ltx25_layout_render`

| Param | Default | Notes |
|---|---|---|
| `input` | required | The layout: a sequence with an optional `first-last` range, or a `.mov`/`.mp4` (timecode frame numbers). Names and numbers the outputs |
| `reference` | required | One image for the look: a file, or a sequence or movie with the frame after it (`…/comp.%04d.exr 1050`) |
| `prompt` | required | What the shot should look like |
| `negative` | empty | The negative prompt |
| `output_path` | show default | Root folder: frames go to `<this>/<shot>/<source>.render_vNNN.<frame>.png`. Same rules as above |

Fixed in the workflow: 544 on the shorter side then upscaled 2×, 24 fps, seed 42. LTX
keeps lengths of 8×n + 1 frames (97, 121, …): with another length the job may come back
with fewer frames than the layout, and says so in its warnings.

### Parameters for `chord_pbr`

| Param | Default | Notes |
|---|---|---|
| `input` | required | A texture file, its UDIM tiles (`.../rock_diffuse.%04d.png`, gaps fine, optional range), or a folder holding one texture. png/jpg/tif/webp, or exr (converted to color_picking first). Larger than 1024 px: scaled to 1024 first |
| `output_path` | show default | Root folder: maps go to `<this>/<shot or asset>/<source>.chord_vNNN/`. Same rules as above |
| `uvset` | `map1` | `map` + 1–2 digits, optionally `-oneWord` (`map10-preview`) |
| `tile` | `noTile` | `tile` / `noTile` / `auto`. auto: UDIM tiles are `noTile`, a single texture is `tile` (UDIM 1001) |
| `state` | `defState` | One camelCase word (`damaged`) |
| `description` | empty | One word after the channel: `color01-sand` |
| `numbering` | `udim` | What the number in the input names is; `frame` for an animated texture |

```bash
python comfyfarm_client.py submit chord_pbr \
    "input=/rdo/shows/<show>/_asset/rockA/cg/textures/src/" state=damaged --wait
```

**Naming and layout** live in `config.yaml` under `paths`: `context_patterns` (where
show/seq/shot sit in a path), `output_root` (default root) + `output_dir` (`{root}/{shot}`),
`frame_name` / `preview_name` (file names; tokens `{source} {folder} {show} {seq} {shot}
{tag} {version} {frame} {ext}`) and `no_write_dirs`. `config.example.yaml` documents each.
Keep `{version}` in `frame_name`: it's what keeps runs in the same shot folder apart.
Changing it only affects jobs submitted afterwards.

### REST API

| Method | Path | |
|---|---|---|
| GET | `/api/v1/health` | Liveness, auth on/off, queue size |
| GET | `/api/v1/workflows` | Workflows and parameter schemas, plus any preflight problems |
| POST | `/api/v1/jobs` | Submit `{workflow, params, name?, priority?, show?, shot?, publish?}` → job (`publish`: publish it when done, the fields below) |
| POST | `/api/v1/plan` | Same body as submit; returns the destination folder and first/last file names without queuing, and `eta` (`run`: its seconds, `done_in`: behind the queue, `workers`) |
| GET | `/api/v1/jobs?user=&status=&active=true&limit=` | List jobs and texture sets, with `queue_position`, `eta_seconds` (until done) and, while queued, `next_worker` |
| GET | `/api/v1/jobs?parent={set id}` | A texture set's batches, in order |
| GET | `/api/v1/jobs/{id}` | One job (full id or 8-char prefix) |
| DELETE | `/api/v1/jobs/{id}` | Cancel (queued, waiting in ComfyUI, or running) |
| POST | `/api/v1/jobs/{id}/retry` | Same params, new version |
| POST | `/api/v1/jobs/{id}/retry-failed` | A texture set: run its failed and cancelled batches again, same folder |
| GET | `/api/v1/jobs/{id}/prompt` | The exact API JSON sent to ComfyUI |
| GET | `/api/v1/jobs/{id}/preview` | MP4 preview |
| GET | `/api/v1/workers` | Online state, GPU, free VRAM, slots, preflight problems |
| GET | `/api/v1/workflows/{name}/publish` | What it can be published with: `templates` (and `unusable` ones, with why), `steps`, the default `step` and `description`; `why` when it can't be |
| POST | `/api/v1/jobs/{id}/publish` | Publish a finished job `{step?, show?, shot?, template?, description?, dry_run?, wait?}` → the publish (`state`: `queued`, `dispatching`, `dispatched` with `tractor_id`, `dry_run`, `failed` with `error`) |
| GET | `/api/v1/jobs/{id}/publishes` | Its publishes, the latest first, with the command run and dispatchGraph's output |
| GET | `/api/v1/shotgrid` | Whether the ShotGrid lookups work (`enabled`, `set_up`), or `why` not |
| GET | `/api/v1/shotgrid/projects?refresh=` | The active shows `[{code, name}]`; `refresh=true` reloads them from ShotGrid |
| GET | `/api/v1/shotgrid/projects/{show}/shots?refresh=` | A show's `shots` `[{code, sequence, status}]` and `sequences` |
| GET | `/api/v1/run-times` | How long each workflow takes per GPU type: runs, usual size and time, overhead, seconds per frame |
| WS | `/api/v1/events` | Live job and worker updates (snapshot first) |

Interactive docs are at `/docs`. Identify yourself with an `X-User` header, or `X-API-Key`
when `auth.api_keys` is set in `config.yaml`.

Jobs in lists carry `publish`: their latest publish (or null), and `info.publish_request` when
one was asked for at submit.

Job statuses: `preparing` (EXR/movie inputs being converted) → `queued` → `dispatched`
(waiting in ComfyUI) → `running` → `postprocessing` (renaming frames, copying preview,
writing texture maps as EXR) → `done`, or `failed` / `cancelled`.

A job's `kind` is `job`, or for a large texture `set` (what was submitted; its status,
progress and outputs come from its batches) and `batch` (one run of a set, with
`parent_id` = the set). Lists show jobs and sets; a set's batches come with `?parent=`.

A finished texture job's `outputs.maps` has one entry per channel: its `pattern` (a
`%04d` over the UDIMs, or the file itself for one tile), `pattern_posix`, `first`/`last`
and `files`. `outputs.frame_pattern` is the base colour's.

## Publishing (Tractor, through Daiquiri)

A finished job of a workflow whose manifest has a `publish:` section (`ltx25_alpha_gen`,
`ltx25_layout_render`) can be published through the pipeline: the manager copies a Nuke
template with the job's frames in it to a staging folder and dispatches Daiquiri's
`update_and_render_nuke` on it. That renders the template's WriteNukleus nodes on Tractor and
makes a daily for each in Artist Review.

Two ways to ask for it:

- **Publish when done**, at submit (the dashboard's form, the Nuke panel; client
  `submit(..., publish={})`, CLI `submit … --publish`). It's checked with the job: a step, template or shot that won't work
  refuses the submit. It runs once the job is done, and again for *Run again*.
- **Publish…** on a finished job, in its details (Nuke panel: right-click it; client `publish(job_id, …)`, CLI
  `publish <job id> [step=…] [shot=…] [show=…] [template=…] [description=…] [--dry-run]`).

| Field | |
|---|---|
| Show, shot | Filled in from the input path until you type in them (in Nuke, the session's `RDO_CURRENT_*` when the path names none); empty: the ones the input path names. With ShotGrid lookups on, both suggest from ShotGrid. The shot is Daiquiri's `--entities`. Only shows with a folder on disk (`publish.show_folder`, `/rdo/shows/<show>`) are published to |
| Sequence | With ShotGrid lookups on: narrows the shot suggestions down (not sent to Daiquiri) |
| Step | One of the studio's steps (`publish.steps`); the default is the manifest's `publish.step` |
| Nuke template | One of the workflow's templates, `default` first |
| Description | Default `Auto Publish`; ` (ComfyFarm <job>, <user>)` is added to it |
| Dry run | dispatchGraph `--dry-run`: Daiquiri checks the context and the script, nothing goes to Tractor |

The frame range is the job's (`--frameRange 1001-1084`). The job's details show its latest
publish: *sent to Tractor* with the Tractor job id and title, *dry run passed*, or *failed* with
dispatchGraph's last line. *Publish log* has every publish of the job with the command run and
dispatchGraph's output. The Queue marks published jobs with **pub**.

### ShotGrid lookups

With them on, the show field suggests ShotGrid's active shows that have a show folder
(`publish.show_folder`), and the shot field that
show's shots (without on-hold, omitted, hidden and void ones), narrowed by the sequence
field. **↻** next to the shot reloads both from ShotGrid, for a shot created since. A
publish to a shot that isn't in the show is refused at submit (after reloading the show's
shots once). When ShotGrid isn't set up the fields are plain text; when it doesn't answer
they say so and keep the last lists. Publishing never waits on ShotGrid.

Only the manager talks to ShotGrid, with its own read-only API script; the dashboard (and
the Nuke panel) ask the manager, so the key never leaves wrk1125l. To set it up:

1. The script's name and key in `~/.config/comfyfarm/shotgrid.env`, readable only by the
   manager's account (the manager reads the file itself, so cron needs nothing). Make the
   file first, then put the two lines in with an editor (so the key isn't in the shell's
   history):
   ```bash
   mkdir -p ~/.config/comfyfarm && touch ~/.config/comfyfarm/shotgrid.env && chmod 600 ~/.config/comfyfarm/shotgrid.env
   ```
   ```
   SG_SCRIPT_NAME=<script name>
   SG_SCRIPT_KEY=<script key>
   ```
   (Or set `SG_SCRIPT_NAME` / `SG_SCRIPT_KEY` in the manager's environment.)
2. shotgun_api3 in the manager's Python (only the manager needs it):
   `$(cat run/python) -m pip install git+https://github.com/shotgunsoftware/python-api.git`
3. `shotgrid: {enabled: true}` in `config.yaml` (see `config.example.yaml` for the filters),
   then restart the manager. `GET /api/v1/shotgrid` says whether it's set up, and
   `GET /api/v1/shotgrid/projects` asks ShotGrid (its `why` says what went wrong).

### Publish templates

`<publish.templates>/<workflow>/<name>.nk`, e.g.
`/rdo/bank/comfy_farm/publishTemplates/Nuke/ltx25_alpha_gen/default.nk`. An ordinary Nuke
script, built around nodes with set names:

- a Read named **`Placeholder_Output_Comfyui`**: the job's frames go into it (file, frame range,
  file type, and the colorspace by extension: PNG `color_picking`, EXR `rdo`, from
  `publish.read_colorspace`), with a label naming the job;
- optionally, Reads named **`Placeholder_Input_<input>`**, e.g. `Placeholder_Input_input`: that
  input of the job, at its studio path and frames (a movie keeps the template's frame
  numbering; an input left empty keeps the template's file);
- at least one **WriteNukleus** (a Write with the pipeline's rdo knobs): what's rendered and
  published, with its own profile and tokens (step, variant, name, part).

Whatever is in between (a Clamp, a grade, a merge with the plate) stays as it is, and the
script's frame range becomes the job's. A template without the output Read or a Write isn't
offered; the form lists it as not usable, with why. The staged copy is
`<publish.staging>/<job id>/<name>.nk` (readable by everyone, removed after
`publish.keep_days`), so what Tractor rendered can be opened afterwards.

To make one: build the comp in Nuke on a real ComfyFarm output, name the Read
`Placeholder_Output_Comfyui`, set up the WriteNukleus, and save it in the workflow's folder.

### Turning it on

Off until it's switched on in the manager's `config.yaml` (see `config.example.yaml`):

```yaml
publish:
  enabled: true
  dry_run: true      # first: every publish is a dispatchGraph --dry-run
```

Restart the manager (`deploy/linux/farmctl.sh restart manager`), publish a finished job (a dry
run), then set `dry_run: false` and publish one for real.

dispatchGraph runs as the manager's user, so the dailies are published as that account for
now. It runs in a clean login shell with the show's context (`publish.shell`,
`publish.command`), because `ctxl` and `dispatchGraph` are aliases a non-interactive shell
doesn't have:

```bash
env -i HOME=$HOME USER=$USER LOGNAME=$USER /bin/bash -lc \
  'source /rdo/rodeo/setup/rdo_rez_launcher/current/env/set_context.sh <show> &&
   /rdo/rodeo/setup/rdo_rez_launcher/current/rdo_rez_launcher.bash -a graph -c dispatchGraph \
     update_and_render_nuke --description "…" --step lay --entities <shot> \
     --workfile <staging>/<job id>/default.nk --frameRange 1001-1084'
```

The Tractor job id comes from its output (`Dispatched engine, #ID: …`, `publish.job_id_pattern`).

## Texture workflows

`chord_pbr` runs [ComfyUI-Chord](https://github.com/ubisoft/ComfyUI-Chord): CHORD estimates
base colour, normal, roughness and metalness from a diffuse texture, and the workflow
turns the normal into height. The gateway writes the maps the way the TEX department's
naming convention and the TexturePublisher expect:

```
<colorspace>_<uvset>_<tile>_<state>_<channel>[-<description>].<UDIM>.<frame>.exr
acescg_map1_noTile_defState_color01.1001.0000.exr
```

| CHORD output | Channel | EXR |
|---|---|---|
| base colour | `color01` | half, converted from color_picking to scene_linear (ACEScg) |
| normal | `nrm02` | half, values kept |
| height | `bmp02` | 32-bit float, values kept |
| roughness | `spcr01` | half, values kept |
| metalness | `metal01` | half, values kept |

- **`nrm02` / `bmp02`, not `01`:** bmp01, nrm01, dsp01 and cav01 belong to maps
  published by modeling. The manifest check refuses them.
- **Input:** one texture per job: a file, its UDIM tiles, or a folder holding exactly
  one texture. A folder with several textures (`rock_diffuse`, `rock_normal`, …) is
  refused with their names; submit each one. UDIMs come from the file names
  (`rock_diffuse.1011.png`, or `….1011.0000.exr` in convention names), and gaps are fine.
  All tiles of a job should be the same size.
- **Colour round trip:** EXR inputs (e.g. ACEScg from Mari) go to 8-bit color_picking
  PNGs for CHORD, like any EXR input. CHORD's base colour comes back as a color_picking
  PNG and is converted to scene_linear, like a Nuke Read set to `default (color_picking)`.
  So a Write and a Read with that transform in Nuke reproduce exactly what the farm does.
- **Output folder:** the file names have no version, so each run gets a new folder:
  `renders/<asset>/<source>.chord_v001/`. The asset comes from `/shows/<show>/_asset/<asset>/`
  in the input path (`paths.context_patterns`; add that line to an existing
  `config.yaml`). Nothing is written into `_toBePublished`; copy a version there when
  you're happy with it.
- **Colour:** see *OCIO* below. The `acescg` in the file names is the config's
  `scene_linear`.
- **Greyscale maps.** CHORD saves height, roughness and metalness as one-channel
  PNGs, so the farm copies that grey into R, G and B of the EXR. The same goes for
  greyscale EXR inputs. Asked for `R,G,B` it doesn't have, oiiotool writes 0 and only
  warns. That warning now fails the job, so a map can't silently come out black.
- **Saving the maps.** The workflow saves its maps with *ComfyFarm Save Image*, from
  `comfyui_nodes/` in this repo.
  - It's a drop-in for WAS *Image Save*: the same `output_path` and `filename_prefix`
    inputs and the same file names.
  - It skips WAS's history: each WAS save re-checked every file it had ever saved (on the
    show share, one network request each) and rewrote its `was_history.json`. That took
    about 0.4 s per map. ComfyFarm Save Image also writes uncompressed (`compress_level` 0).
  - Rounding to 8-bit is done on the GPU.
- **8-bit in between.** The maps are 8-bit PNGs between ComfyUI and the farm, so they
  carry 8 bits of precision even in the EXRs. Height can show steps when used for
  displacement. A save node writing 16-bit or float would fix it.
- **CHORD works at 1024×1024, so textures are scaled to 1024 first.** ComfyUI-Chord
  resizes the texture to 1024² for the model and for Normal to Height anyway, then resizes
  the maps back. So the manifest sets `work_resolution: 1024`: a larger texture is scaled
  down before the workflow (longer side 1024, aspect kept), and the maps come out at that
  size.
  - **Why:** less for every step around CHORD to handle. The EXR conversion, the colour
    conversion, the load into ComfyUI, the map saves and the EXR writing all work on 1024²
    instead of 4096², 16 times fewer pixels. The detail is the same: CHORD only ever saw
    1024².
  - **EXRs** are scaled in `scene_linear` as they're converted, before the colour
    conversion. **png/jpg/tif** inputs larger than 1024 are scaled into the scratch folder,
    with no colour change. Textures of 1024 or less are read as they are.
  - **UDIM tiles** are each scaled to fit, so a 2048×1024 tile becomes 1024×512.
  - The dashboard and the Nuke panel show the scaling before you submit (*4096×4096 →
    1024×1024*).
  - For maps at the texture's own size, remove `work_resolution` from the manifest. They
    then have 1024² worth of detail, scaled up.
- **Nuke:** *Create Read node* on a texture job makes one Read per map, in a row. UDIM
  tiles read as frames (1001, 1002, 1011, …), missing tiles show black.

### Large textures: sets of batches

CHORD estimates every tile of a run in one pass on the GPU, so its memory grows with
the number of tiles: 5 tiles used about 29 GB of the 5090's 32 GB. So a texture with more
tiles than its input's `per_run` (4 for `chord_pbr`) is submitted as a **set**:

- **You submit it once** and it shows as one job: *rockA_diffuse v001 · 148 batches*, with
  progress in tiles (*230/591 UDIMs*) and a time left from its finished batches.
- **One output folder.** The set takes one version; every batch writes its maps into it.
  The file names are per UDIM, so batches never overwrite each other.
- **Each batch is an ordinary run** of up to 4 tiles, with its own scratch folder,
  conversion, retries and cancel. Open the set in the dashboard to see them.
- **Other jobs get in between.** The farm picks the next run by priority, then the user
  with the fewest runs on the GPU, then the set (or job) with the fewest. So someone else's
  job, or your own small one, waits for at most a batch or two, not the whole set.
- **Conversion stays one batch ahead** of the GPU: each worker converts only the run it
  takes next, so other jobs' inputs get converted in between and scratch space stays small.
- **A failed batch doesn't stop the set.** The rest carry on, and the set ends *failed*,
  listing which UDIMs didn't finish and why. **Retry failed** (dashboard, Nuke panel, or
  `comfyfarm_client.py retry-failed <set id>`) runs just those batches again, into the
  same folder. **Run again** runs the whole set as a new version.
- **Cancel** cancels every batch that hasn't finished. The maps already written stay, and
  Retry failed can finish the set later.
- **Tiles of different sizes go in different batches**, even in a set smaller than the
  batch size: one run's images go through the model as one batch, so they must all be the
  same size (ComfyFarm Load Images refuses a mix; VHS would crop them to one size).
- **Timing on the farm machine** (4K EXR tiles at `work_resolution` 1024): 1 tile 5 s,
  2 tiles 8 s, 3 tiles 11 s, 4 tiles 15 s, 5 tiles 18 s. That's about 2 s per run plus
  3.3 s per tile, so batches of 4 cost 3.75 s a tile. 591 tiles take roughly 35 minutes.
- **Height per tile.** CHORD stretches each tile's height to 0–1 on its own, so bump
  strength can differ a little between UDIMs. That doesn't depend on the batch size: each
  tile is computed on its own.

### OCIO

The farm does two colour conversions, with the small OCIO config in this repo's `ocio/`
folder. `git pull` on each worker updates it (see *Deploying → Updating*). They are a
Nuke Write and Read set to `default (color_picking)` in Rodeo's published show config
(bri3's `config_1_0_v1`):

| What | From | To |
|---|---|---|
| EXR inputs (plates, textures) → PNG for ComfyUI | `scene_linear` (ACEScg) | `color_picking` (ACES Rec.709 output transform) |
| Texture colour maps from ComfyUI → EXR | `color_picking` | `scene_linear` |
| Texture data maps (normal, bump, ...) | values kept, tagged `data` | |

- **It matches Nuke.** `ocio/config.ocio` keeps only what these conversions use from the
  published config: the ACEScg matrix and the ACES Rec.709 output transform's three LUTs.
  The shot's white balance cancels in both directions, so it's left out.
  - In Nuke, linear grey 0.02 / 0.05 / 0.18 / 0.5 / 1.0 gives PNG codes 23 / 43 / 99 /
    170 / 209. The farm gives the same, and `python -m comfyfarm check` and the tests
    fail if it doesn't.
  - The tests also compare it pixel for pixel with the published config, kept in
    `ocio/reference/`.
  - How to update it when the published config changes is in `ocio/README.md`.
- **The PNGs are tone-mapped,** like the `ACES-look` view in Nuke: it's an output transform,
  not a plain sRGB curve.
- **Bright colour maps come back above 1.0.** Linear 1.0 lands at PNG 209, and the way back
  undoes the highlight roll-off: 220 comes back as 1.32, 240 as 3.0, 255 as 16.3. So a
  ComfyUI colour map brighter than 209 gives an EXR above 1.0, which for a base colour is
  more than 100% reflectance. Check bright materials with `oiiotool --stats`.
- **Settings:** only `ocio.config` in `config.yaml`, the config file (default
  `ocio/config.ocio`). Each job is frozen with the config it was submitted with.

To check by eye in Nuke: load `ocio/config.ocio`, or the show's published config. A Write
with `default (color_picking)` gives the farm's PNG, and a Read with `default
(color_picking)` brings a ComfyUI PNG back to linear.

### Input formats

The ComfyUI workflow only ever reads PNG frames. Other inputs are converted on the farm
machine first, into `convert.scratch_dir`. Conversion runs while the GPU works on other
jobs, and the frames are deleted when the job finishes. Settings live under `convert:`
in `config.yaml`.

- **PNG / JPG / TIF / WebP:** read directly, as before. If the workflow has a
  `work_resolution` (see *Workflow manifests*) and the image is larger, it's scaled down to
  it into the scratch folder first, with `oiiotool` (no colour change, 8-bit PNG).
- **EXR sequences:** converted with `oiiotool` and the farm's OCIO config, from
  `scene_linear` to `color_picking`. That's the same conversion as a Nuke Write with its
  output transform set to `default (color_picking)`, as WriteNukleus writes the PNG
  preprocess.
  - RGB only, cropped to the display window (`--ch R,G,B --croptofull`).
  - Output is 8-bit PNG at plate resolution, or scaled down to the workflow's
    `work_resolution` if it has one (in `scene_linear`, before the colour conversion).
  - The PNGs are written uncompressed by default. `convert.exr.png_compression` sets the
    zlib level: 0 is off (the default), 1 is fast, 6 is OpenImageIO's own default, 9 is
    smallest. They're scratch files ComfyUI reads once.
    - On the farm machine, a 4K frame took 8.3 s at level 6 and 1.9 s at 0.
    - Uncompressed takes about 3 bytes per pixel, so a 4096² frame is 50 MB.
    - Raise the level only if scratch disk space runs short. It changes the files' size on
      disk, not memory: ComfyUI decodes every frame to the same full-size image either way.
- **If EXR colours look wrong** (lifted, shifted), compare numbers rather than by eye.
  On the farm machine:

  ```
  .venv\Scripts\python tools\ocio_compare.py
  .venv\Scripts\python tools\ocio_compare.py --exr D:\test\tile.1001.exr --nuke-png D:\test\tile_nuke.1001.png
  ```

  - **Without files**, it converts a test ramp with the farm's exact oiiotool commands. It
    compares them with OpenColorIO and with the values measured in Nuke.
  - **With `--exr` and `--nuke-png`**, it also converts your EXR and compares the result with
    the PNG Nuke wrote from it with `default (color_picking)`.
  - **The header** shows which oiiotool, OpenImageIO and OpenColorIO the farm uses.
- **Movies (.mov/.mp4/.m4v/.mxf):** extracted with `ffmpeg` (the one pip installs; a
  system ffmpeg/ffprobe is used instead if you set `convert.tools`), numbered from the movie's
  timecode (`00:00:41:17` at 24 fps = frame 1001; drop-frame handled). A range on the
  input picks frames by those numbers. With no timecode, numbering starts at
  `convert.movie.default_start` (1001) and the job carries a warning. Movies are assumed
  to be display-referred with the look already baked in, like the PNG preprocess, so
  they get no colour transform. Untagged movies are decoded as BT.709.
  - **Slates:** in `.mov` files inside a `movhistory` folder, the first frame is a slate,
    so it's dropped. The plate keeps its timecode frame numbers: a slate at 1000 means
    the plate is 1001 onwards. Set by `convert.movie.slate` in `config.yaml`.

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
a value into the wrong node.

### Workflow manifests

The manifest says what artists can set and where each value goes in the API JSON.
Anything not listed stays exactly as exported. In the example below, `seed` shows how a
plain setting is written; the shipped `ltx25_alpha_gen` has only `input` and `output_path`.

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

  input:                       # the images the workflow takes
    type: input
    takes: images              # images: a batch of frames | image: exactly one
    label: Input sequence
    required: true
    loader: {node: "9104", class: ComfyFarmLoadImages}               # the node that reads it
    path_node: {node: "9094", input: value, class: PrimitiveString}  # optional, informational
    # frames: {max: 257}       # optional: longer inputs are refused
  output_path: {type: output_folder, label: Output folder}

# work_resolution: 1024        # optional: inputs larger than this (px, longer side) are
                               # scaled down to it before the workflow, aspect kept
# requires: {vram_gb: 32, tags: [nightly]}   # optional: the machines that may run it: the
                               # least GPU memory (GB, as the card is sold: a 5090 is 32) and
                               # labels from their agent.yaml `tags` (linux / windows always set)
# publish: {step: lay}         # optional: its outputs can be published (see Publishing); the
                               # step offered first

outputs:                       # nodes the gateway points at each job's versioned folder
  root_node: {node: "9093", input: value, class: PrimitiveString}
  frames: {node: "9109", class: "Image Save", path_input: output_path, prefix_input: filename_prefix, ext: png}
  preview: {node: "...", class: VHS_VideoCombine, prefix_input: filename_prefix}   # optional
```

- **Plain params** (`int`, `text`, …) are written straight into `targets`: node id as
  it appears in the API JSON (`"5516:4832"` for nodes inside groups/subgraphs), the
  input name, and the class as a guard. Validation (min/max/required) happens before
  anything reaches ComfyUI.
- **`input` and `output_folder`** take no `targets`; the gateway wires them in.
  An input is mapped to the farm path, frame-checked and given to its `loader`. A folder
  means the one sequence (or texture) in it; a folder holding several is refused. One
  input per workflow for now.
  - `takes: images`: a batch of frames (a sequence, a movie's frames, a still as a batch
    of one). `frames: {max: N}` refuses longer inputs. In texture workflows, `per_run`
    is the most tiles one run takes (more make a set; see *Large textures*).
  - `takes: image`: exactly one image, for ComfyFarm Load Image. A sequence or movie must
    name its frame (`…/plate.%04d.png 1050`); without one it's refused with the frames it
    has. The output is numbered like that frame.
  - Older manifests write an `images` input as `type: sequence`, with `loader` and
    `path_node` in a `sequence:` section. Both work; clients see either as the same field.
- **Several inputs** (a plate, its matte, a reference image): one `type: input` param each.
  - One is `primary: true`. It must be required; it names the outputs and numbers their frames.
  - `match: <primary>`: read at the primary's frames. Given as a pattern alone, the matte
    gets the plate's range; with other frames it's refused.
  - `required: false`: may be left empty. Left empty, its loader gets an empty path with
    `optional` on (a small black image, so the graph still validates), and its `empty`
    values are written into the graph: point them at whatever switches that branch off.
    `given` values are written when it's given.
  - Each input converts into its own scratch folder. Texture workflows take one input.

  ```yaml
  plate: {type: input, takes: images, primary: true, required: true,
          loader: {node: "9104", class: ComfyFarmLoadImages}}
  matte: {type: input, takes: images, match: plate, required: false,
          loader: {node: "9120", class: ComfyFarmLoadImages},
          empty: [{node: "9121", input: value, class: PrimitiveBoolean, value: false}],
          given: [{node: "9121", input: value, class: PrimitiveBoolean, value: true}]}
  ``` An output folder is checked (parent exists, on show storage,
  outside `.published`) and used as the root for `paths.output_dir` (`{root}/{shot}`);
  the version is the first one with no files there and no queued job claiming it.
  `POST /api/v1/plan` shows the result without queuing.
- **Nodes in `sequence` and `outputs` belong to the gateway.** A plain param targeting
  them would be overwritten on every job, which is why the output folder is its own type
  rather than a param pointed at `OUTPUT_PATH`.
- **The loader** (`sequence.loader`) is the node that reads the input:
  - `ComfyFarmLoadImage` (*ComfyFarm Load Image*): one image, for `takes: image`. It gets
    a file, or a pattern and its frame.
  - `ComfyFarmLoadImages` (*ComfyFarm Load Images*, from `comfyui_nodes/`): gets the exact
    files, a frame pattern plus a frame list (`1001-1158`, `1001,1002,1011`). Nothing else in
    the folder matters, a missing frame stops the run, and images of different sizes are
    refused rather than cropped. Its `path` also takes a range as copied from Nuke
    (`…/plate.%04d.png 1001-1158`), so the workflow runs by hand in ComfyUI too. Same
    outputs as VHS: IMAGE, MASK (from alpha), frame_count.
  - `VHS_LoadImagesPath`, for downloaded workflows: it reads the whole folder in name
    order, so the farm works out `skip_first_images`/`image_load_cap` and refuses a range
    that other images in the folder sort into.
  - Workflows never load movies: one with a video loader node (`VHS_LoadVideo`,
    `VHS_LoadVideoPath`, core `LoadVideo`) is refused. The farm extracts a movie's frames
    first, numbered from its timecode.
- **Text checks:** `pattern` (a regex the value must match; empty is always allowed) and
  `invalid` (the message when it doesn't).
- **`work_resolution`** (pixels, 64 or more): for workflows that work at a fixed size
  anyway, like CHORD at 1024. Larger inputs are scaled down to it before the workflow
  runs, so nothing around the workflow handles pixels it would throw away. Inputs that fit
  are untouched. Leave it out to pass inputs at their own size. Applies to images and
  EXRs; movies aren't scaled.
- **Texture workflows** have a `textures` section instead of `outputs.frames`: the file
  name template, and per map its save node (`node`, `class`), TEX `channel`,
  `colorspace` token (default `acescg`), `color: true` for a colour map (color_picking → scene_linear), and `bits`
  (`half`/`float`). Optional `dir`: output folder templates, the first whose tokens the
  input gives wins (default `{root}/{shot}/…`, then `{root}/{asset}/…`, then
  `{root}/{source}.{tag}_v{version:03d}`). Params with `naming: true` (`uvset`, `tile`,
  `state`, `description`, `numbering`) go into the file names and take no `targets`.
  The most tiles per run is the input's `per_run` (older manifests: `textures.batch`); a
  texture with more is a set of batches (see *Large textures*). Set it from the GPU memory
  one run of that many tiles needs.
  See `workflows/chord_pbr/manifest.yaml`.
- Removing a param from the manifest changes the API: clients still sending it get
  "Unknown parameter". The Nuke panel and dashboard read the parameters from the farm,
  so they follow on their own; only scripts that send the old name need changing.

## Operations notes

- **Logs:** `logs/comfyfarm.log` (rotating), plus `logs/*-service.log` for the services. Each
  agent on another machine logs to `logs/agent-<name>.log` there, and ComfyUI, when its agent
  starts it, to `logs/comfyui-<name>.log`. `farmctl.sh` keeps console output in
  `logs/manager.out` and `logs/agent.out`.
- **State:** `data/comfyfarm.db` (SQLite). Back it up or delete it; nothing else depends on it.
- **Restarts:**
  - Manager restarts mid-job: agents carry on, and report what finished meanwhile when it's back.
  - Agent (or `standalone`) restarts mid-job: it reads its ComfyUI's queue and history and finishes the job.
  - ComfyUI crashes mid-job: the job is requeued once (`farm.max_attempts`), with a warning on the job.
  - Jobs of an agent that stops checking in, or whose ComfyUI stays unreachable, for 5
    minutes are requeued.
- **Pin WAS Node Suite.** `ltx25_alpha_gen` saves frames with WAS *Image Save* using its
  `output_path` input (`chord_pbr` uses *ComfyFarm Save Image* instead). The current WAS release replaced that input with a sandboxed
  `root` setting. Don't update WAS on the farm machine without re-testing; the preflight
  check flags it if it happens.
- **This workflow's outputs are at the working resolution** (1088 on the shorter side, fixed
  in the workflow), not the plate's. Reformat in comp if you need plate res. If the model returns
  a different number of frames than it was given, the job carries a warning.
- **Security:** with no API keys, anyone on the network can submit and cancel, and user
  names are taken on trust. Set `auth.api_keys` to require keys. Set `agents.token` so only
  your agents can check in and take jobs.

## Development

```bash
pip install -r requirements-dev.txt               # the farm's requirements + what only the tests use
python -m unittest discover -s tests -t . -v      # unit + end-to-end (2-3 minutes)

# Manual testing without a GPU:
python tools/mock_comfyui.py --port 8188 --output mock_output
python -m comfyfarm standalone                    # `serve` is the same, under its old name
```

`standalone` runs the manager and an agent for each ComfyUI in `workers:`, in one process.
Agents talk to the manager only through the messages in `comfyfarm/protocol.py`, in the
same process (`agent/link.py`'s `LocalLink`) or over HTTP (`HttpLink`). `tests/test_agent.py`
runs the manager and agents together against an in-memory ComfyUI: jobs, cancels, lost
runs, restarts of either side, leases, agents with their own path mappings.
`tests/test_layout.py` keeps the layers apart: `fileio` imports with the standard library
alone, the manager doesn't need the agent or the web stack, and the agent doesn't need the
manager. So most tests run with just pydantic and pyyaml installed; `test_agent` also needs
httpx and websockets, and the end-to-end suites the whole of `requirements.txt`.

`tests/test_e2e.py` runs `standalone` against the mock. It covers submit to done (frame
renaming, preview, versioning), cancelling in each state, failures with tracebacks, bad
input, a restart mid-job, a ComfyUI crash mid-job, and a missing node pack.
`tests/test_e2e_agents.py` runs a `manager` and two `agent` processes, each with its own
mock ComfyUI, one seeing show storage under another folder: jobs spread over both, inputs
converted on the agent, cancels, an agent restarting, an agent gone for good, the
manager restarting, an agent keeping its ComfyUI running, `agent --check`, and the shared
client finding the manager. `tests/test_share.py` covers the shared folder and read
permissions, `tests/test_agent_host.py` the ComfyUI launcher and `manager.json` lookup, and
`tests/test_deploy.py` the deploy scripts (Linux scripts with stand-ins for conda, Python
and crontab; on Windows, that every PowerShell script parses).

`tests/test_nuke.py` covers the Nuke panel's logic and its Nuke helpers (with a stand-in
`nuke` module), and the end-to-end suite drives the panel's logic against the gateway. The
Qt widgets need a real Nuke (or PySide) to try.
