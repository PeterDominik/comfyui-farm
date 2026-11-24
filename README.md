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
- **One subject, named by a word.** `sam3_ltx_alpha` mattes the subject an artist names
  (`person`, `car`, `dog`): SAM3 finds and tracks it, LTX 2.5 alpha-gen draws its edges,
  and everything else is painted out first. 16-bit alpha, numbered like the plate.
- **Preflight checks** catch a missing node pack, a node-pack update that renamed an
  input, or a missing model before a job is sent (`python -m comfyfarm check`).
- **Cancel, retry, and the exact API JSON** of any job, for loading back into ComfyUI to debug.

## Repo layout

| Path | What |
|---|---|
| `comfyfarm/manager/` | The manager: API, dashboard, job database, planning, texture sets, the queue, and the desk agents check in with (`desk.py`) |
| `comfyfarm/agent/` | The agent, one per ComfyUI: takes jobs from the manager, converts their inputs, hands them to its runner, writes the outputs |
| `comfyfarm/runners/` | What runs a job on the GPU, behind one interface (`base.py`). `comfyui.py`: ComfyUI's client and websocket, its health, node definitions and environment. `comfyui_workflow.py`: a workflow's ComfyUI half, checking how it wires the graph and building the API JSON. `process.py`: the process runner and a process definition's half; `process_run.py`: the supervisor of each run (standard library only) |
| `comfyfarm/fileio/` | Files in, files out: paths, inputs (sequences, UDIM tiles, movies), EXR/movie conversion, OCIO, output names, writing the outputs. Standard library only |
| `comfyfarm/artifacts/` | Each kind of output the farm writes, one file each, named by its key under `writes:`: where its files go (the manager), and what the agent does before and after the run. `frames.py`: a frame sequence and its preview movie; `textures.py`: a texture set's EXR maps; on the generic route (`base.py`), `file.py` (other files, such as a report), `movie.py` (a video) and `model.py` (trained weights) |
| `comfyfarm/definitions.py` | A definition's head, as the manager uses it: reading `process.yaml` and `manifest.yaml`, parameters and validation, inputs and what it writes, the fingerprint |
| `comfyfarm/catalog.py`, `providers/` | The menus, Provider > Task > Workflow: one file per provider in `providers/`, the tasks in `workflows/tasks.yaml`, and each definition's place checked against them (see *Menus: providers and tasks*) |
| `comfyfarm/protocol.py` | Job states, and the messages between agents and the manager |
| `comfyfarm/shotgrid/` | The manager's read-only ShotGrid lookups: active shows, their sequences and shots, cached, for the publish fields; the publish check; Versions and Playlists as inputs and Batch Submit rows; Batch Submit's ShotGrid query; where ShotGrid's right-click goes |
| `comfyfarm/publish/` | Publishing finished jobs through the pipeline: Nuke templates and their placeholder Reads, staging them, and dispatching Daiquiri's `update_and_render_nuke` to Tractor (see *Publishing*) |
| `comfyfarm/comfyenv/` | The farm's ComfyUI environment: what each machine runs, how it differs from `deploy/comfyui/environment.yaml`, and `sync` to bring it there |
| `comfyfarm/envs/` | Runner environments: reading `environments/<name>/`, locking it, publishing it to the bank as a release, building this machine's copy with uv and checking it (see *Runner environments*) |
| `environments/<name>/` | A runner environment as a uv project: `pyproject.toml`, `.python-version`, `env.yaml` (its check) and `uv.lock`. `gpu-test`: torch, numpy and pillow, for the test process |
| `comfyfarm/cli.py` | `python -m comfyfarm standalone` (or `serve`), `manager`, `agent`, `check`, `comfyui snapshot\|verify\|sync` and `env list\|lock\|install\|remove` |
| `config.example.yaml`, `agent.example.yaml` | The manager's settings; one GPU machine's agent |
| `workflows/<name>/` | `workflow_api.json` (ComfyUI API export) + `process.yaml` (what's exposed and written, and which nodes do it): `ltx25_alpha_gen`, `chord_pbr`, `wan22_i2i`, `ltx25_layout_render`, `sam3_ltx_alpha`. `gpu_test`: a process (`process.yaml` + `run.py`), the farm's test of its process runner (see *The GPU test*). `workflows/tasks.yaml`: the menus' tasks |
| `client/comfyfarm_client.py` | Stdlib-only client and CLI (Nuke, Houdini, terminal) |
| `client/nuke/` | Nuke panel: the dashboard's form and job list inside Nuke |
| `deploy/linux/`, `deploy/windows/` | Setup, running and keeping it running on each kind of machine (see *Deploying*) |
| `deploy/comfyui/` | The farm's ComfyUI: `environment.yaml` (versions, node packs, models) and `extra_model_paths.yaml` (see *The farm's ComfyUI*) |
| `deploy/shotgrid/menu.yaml` | Which shows have the farm in ShotGrid's right-click menu, and the address it posts to (see *ShotGrid right-click*) |
| `tools/mock_comfyui.py` | Fake ComfyUI for testing without a GPU |
| `tools/sg_probe.py`, `tools/sg_ami.py` | What ShotGrid holds for a Version (read only); ShotGrid's ComfyFarm right-click menu made to match the farm's, with the pipeline's client or an admin script (see *ShotGrid right-click*) |
| `tests/` | Unit tests + end-to-end tests against the mock |

## Deploying

The farm runs on one machine (`standalone`: the manager plus an agent per local ComfyUI),
or as a **manager** on one machine and an **agent** on each GPU machine, Linux or
Windows. Nothing below needs sudo or admin rights, except the optional Windows services.

Every machine runs from its own git checkout of this repo, on local disk, at the same
commit. ComfyUI needs the custom node packs the workflows use (`deploy/comfyui/environment.yaml`
lists them, and `comfyui sync` installs them; see The farm's ComfyUI): WAS Node Suite,
Easy-Use, KJNodes, RES4LYF and ComfyUI-LTXVideo, plus
[ComfyUI-Chord](https://github.com/ubisoft/ComfyUI-Chord) with `chord_v1.safetensors` in
`ComfyUI/models/checkpoints` for `chord_pbr`. CHORD's weights are under Ubisoft's
research-only licence. The farm's own nodes (`comfyui_nodes/`: *ComfyFarm Load Images*,
*ComfyFarm Load Image*, *ComfyFarm Save Image*, *ComfyFarm Pro Alpha*) come with this repo; the setup scripts link
them into ComfyUI (`--comfyui` on Linux, `-ComfyUIDir` on Windows). The tools for EXR and movie inputs come with the Python
requirements: `oiiotool` (OpenImageIO, with OpenColorIO) and `ffmpeg` (imageio-ffmpeg).

| Script | What |
|---|---|
| `deploy/linux/install_agent.sh` | A GPU machine from nothing: Miniconda, ComfyUI at the farm's commit with its torch, the agent, `comfyui sync`, cron (run in the folder to install into) |
| `deploy/linux/setup.sh manager\|agent` | Conda env (Python 3.12, your Miniconda), requirements (from pypi.org rather than the pipeline's package mirror; `--index-url` to change), `config.yaml` or `agent.yaml` from its example, the check |
| `deploy/linux/farmctl.sh start\|stop\|restart\|status\|ensure\|log\|check manager\|agent` | Runs it in the background, with the pipeline's Python settings (`PYTHONPATH`, rez) removed |
| `deploy/linux/install_cron.sh manager\|agent` | Your crontab starts it at boot and checks it every minute (`--remove` to undo) |
| `deploy\windows\install_agent.ps1 [-Always]` | A GPU machine from nothing: Python, ComfyUI at the farm's commit with its torch, the agent, `comfyui sync`, started at logon or always (run in the folder to install into) |
| `deploy\windows\setup.ps1 [-Agent]` | `.venv`, requirements, `agent.yaml` (`-Agent`) or `config.yaml`, the check |
| `deploy\windows\install_startup.ps1 [-Always]` | Keeps the agent running, no admin: at logon (Startup folder), or with `-Always` whether or not you're logged in (a scheduled task); `-Stop`, `-Remove`, `-AtLogon` |
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
- `agents.order` (optional): jobs go to the GPU that ran their workflow fastest; machines whose
  GPU hasn't run it yet follow, the biggest GPU (VRAM) first, or in this order (agent names, best first);
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

**From nothing** (no Miniconda, no ComfyUI yet): make a folder on the machine's own disk,
clone the repo into it and run `install_agent.sh` there. It asks for the manager's
`agents.token` once.

```bash
mkdir -p /softwareLocal/comfyfarm && cd /softwareLocal/comfyfarm
git clone https://github.com/PeterDominik/comfyui-farm.git
bash comfyui-farm/deploy/linux/install_agent.sh       # --name, --port, --token-file, --no-start: see its header
```

- **What it puts in that folder:** `miniconda3/` (conda-forge only, with the envs `comfyfarm` and `comfy`), `comfyui/` at the
  environment file's commit with its torch, and `comfyui-farm/` with its `agent.yaml` (readable by you only; a copy goes to
  `~/.config/comfyfarm/agent-<name>.yaml`).
- **Then it:** brings ComfyUI to the farm's (`comfyui sync`), starts the agent and adds the crontab lines.
- **Checks first:** the driver against `driver_min`, local disk with 40 GB free, show storage and the model libraries visible,
  ComfyUI's port free.
- **Re-running** keeps what's there. The agent is named `<host>-<gpu>` (`tor-flame02-a6000`) unless `--name` says otherwise,
  and everything it prints is also in `install_agent.log`.

**With Miniconda and ComfyUI already there:**

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

**From nothing** (no Python, no ComfyUI yet; needs **Git for Windows**, whose *Only for me* install needs no admin):
make a folder on the machine's own disk, clone the repo into it and run `install_agent.ps1` there. It asks for the
manager's `agents.token` once, and with `-Always` for your Windows password (see *Keeping it running* below).

```powershell
mkdir D:\comfyfarm; cd D:\comfyfarm
git clone https://github.com/PeterDominik/comfyui-farm.git
powershell -ExecutionPolicy Bypass -File comfyui-farm\deploy\windows\install_agent.ps1 -Always
```

- **What it puts in that folder:**
  - `python\`: Python from python.org, for you only, unless the machine already has the pinned version or `-Python`
    names one;
  - `comfyui\` at the environment file's commit, and `comfyui-env\`, its Python with the farm's torch;
  - `comfyui-farm\` with its `.venv` and `agent.yaml` (readable by you only; a copy goes to
    `%USERPROFILE%\.config\comfyfarm\agent-<name>.yaml`).
- **Then it:** brings ComfyUI to the farm's (`comfyui sync`) and starts the agent.
- **Checks first:** the driver against `driver_min`, a local drive with 40 GB free, `\\rodeofx.com\smb\shows`,
  `B:\comfy_farm\manager.json` and the model libraries reachable, ComfyUI's port free.
- **Re-running** keeps what's there. The agent is named `<computer>-<gpu>` (`wrk59467w-5090`) unless `-Name` says
  otherwise. Everything it prints is also in `install_agent.log`. `-NoStart` stops before starting it.

**With Python and ComfyUI already there** (needs **Python 3.10+** from python.org, tick *Add to PATH*):

```powershell
git clone https://github.com/PeterDominik/comfyui-farm.git D:\comfyfarm
cd D:\comfyfarm
powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1 -Agent -ComfyUIDir "C:\ComfyUI_windows_portable"
```

Edit `agent.yaml`:

- `name`;
- `manager: B:\comfy_farm\manager.json`: the shared folder's `manager.json` (`/rdo/bank` is
  `B:\` on Windows), so the agent follows the manager if it moves; or `http://<manager>:8000`.
  And `token`;
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

**Keeping it running.** `install_startup.ps1` runs `run_agent.ps1`, which restarts the agent if it exits (one per
`agent.yaml`, across login sessions). It does this in one of two ways, neither needing admin:

| | at logon (default) | `-Always` |
|---|---|---|
| How | a shortcut in your Startup folder | a scheduled task every 5 minutes, run whether or not you're logged in (a run while the agent runs ends at once) |
| Screen locked | keeps running | keeps running |
| You sign out | stops | keeps running |
| After a reboot | off until you log in | back within 5 minutes |
| Needs | nothing | your Windows password, kept by Windows with the task (run `-Always` again after changing it), and the *Log on as a batch job* right on that machine; without it, Windows refuses the task and nothing changes |

- **No mapped drives:** the task's background session has none. `-Always` records your network drives (`B:` for
  `/rdo/bank`) in `run\drives.txt`, and `run_agent.ps1` maps any that are missing before starting the agent, so
  `agent.yaml` and ComfyUI keep using `B:\...`.
- **Switching:** `-Always` removes the logon shortcut; `-AtLogon` removes the task and puts the shortcut back.
- **Stop, restart, remove:** `-Stop` stops the agent (with `-Always` it also pauses the task until `-StartNow`), and
  `-Stop -StartNow` restarts it in whichever way is set up. `-Remove` stops it and removes both.

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -Always -StartNow   # whether or not you're logged in
powershell -ExecutionPolicy Bypass -File deploy\windows\install_startup.ps1 -StartNow           # at logon
```

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

On the manager's machine, restart the gateway too, if it runs there
(`deploy/linux/farmctl.sh restart gateway`, see *The gateway*).

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
  A pack is found from the workflow's node classes. One that only adds choices to a core
  node shows in none of them: RES4LYF's `res_2s` sampler and `beta57` scheduler in
  `wan22_i2i`'s KSampler. The manifest names such packs (`node_packs: [RES4LYF]`, by their
  `custom_nodes` folder name), and `snapshot` records them like the others. A worker without
  one says which pack should provide the missing choice.
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

#### The manager syncs the machines

On the manager's host, the manager can sync the machines instead, one at a time, the canary
first (`rollouts.order`; decision 0033). Commit the new environment file and update every
machine's checkout first: each machine must have the same version of it as the manager's
host, or it refuses.

```bash
python -m comfyfarm comfyui sync --all                        # every machine, one at a time
python -m comfyfarm comfyui sync --machine tor-flame02-a6000  # this one only (again for more)
python -m comfyfarm comfyui syncs                             # how it goes: each machine's steps, what is left
python -m comfyfarm comfyui sync --cancel                     # no next machine
```

Each machine in turn:

1. The manager drains it, unless it was drained already.
2. Once it holds no jobs, however long they take, the manager tells its agent to sync.
3. The agent does what `sync --apply` does by hand. It puts the hold in place, stops the
   ComfyUI it started, does the steps and starts ComfyUI again. Then it waits until
   ComfyUI's node definitions are read and compares again.
4. Once the manager has checked its ComfyUI again, it resumes the machine, unless it was
   drained before. Then the next machine's turn comes.

The sync stops at a machine, and the machines after it are never touched, when:

- a step fails;
- a workflow the machine could run before can't run after the sync;
- the machine hasn't finished within `rollouts.sync_minutes` (60) of being told;
- with `evals.gate` on, an eval fails on it, or its evals aren't over within `evals.minutes`
  (see *Evals*).

That machine stays drained for you to look at. A machine whose agent refused before
touching anything takes jobs again. That happens with another version of the file in its
checkout, a ComfyUI the agent didn't start, or `envs.managed` off in its `agent.yaml`. The
agent syncs only with `envs.managed` on, as for runner environments. What the sync never
does (torch, the driver, launch flags, models) is listed for each machine, and the sync
goes on. The dashboard's Rollouts panel shows the sync with the rollouts.

Each agent also checks its machine when ComfyUI starts and every `environment_seconds`
(15 min), compares it with its own checkout's copy of the file and tells the manager what
differs. So after the file changes, a machine compares with the new one once its code is
updated. The Machines tab marks
ComfyUI's version ✓ (matches) or ≠, and lists the differences under the machine. A
machine that differs keeps taking jobs; missing nodes or models still keep a workflow off
it, as before. `agent --check` lists the differences as warnings.

### Runner environments

The process runner (and later runners) run their jobs in environments the farm builds
itself, never in the pipeline's Python. Each is a folder in `environments/` of this repo, a
small uv project (decision 0024):

| File | What |
|---|---|
| `pyproject.toml` | Python and the packages, pinned; PyPI as the index, and PyTorch's index for torch only |
| `.python-version` | The Python version uv installs (its own, whatever the machine has) |
| `env.yaml` | What it is, and the command that checks a build (`python` is the build's own) |
| `uv.lock` | Every package, with its hashes, for Linux and Windows. Made with `env lock`, committed |

```bash
python -m comfyfarm env list                  # the repo's environments, and what this machine has built
python -m comfyfarm env lock gpu-test         # after changing its pyproject.toml: needs pypi.org; commit uv.lock
python -m comfyfarm env install gpu-test      # build this machine's copy, check it, make it current
python -m comfyfarm env remove gpu-test       # remove this machine's builds of it
```

Run them with the farm's Python, as for `agent --check`: `env -u PYTHONPATH -u PYTHONHOME
"$(cat run/python)" -m comfyfarm env ...` on Linux, `.venv\Scripts\python.exe -m comfyfarm
env ...` on Windows.

`env install` builds into `data/envs/<name>/<fingerprint>/` (`envs.dir` in `agent.yaml`),
with `uv sync --frozen`: exactly the committed lock. It runs the check in the build and only
then makes it `current`. The build before stays as `previous`, older ones are removed, and
a build that fails leaves `current` as it was. Installing an environment that is already
built does nothing (`--force` builds it again). The first run installs uv itself into
`data/envs/.uv-tool` with pip from pypi.org (or set `envs.uv` to a uv the machine has).

A `uv.lock` that no longer matches its `pyproject.toml` is refused: `env install` and
`env list` say what changed (*LOCK OUT OF DATE*), and CI fails. Lock it again and commit
both files together.

A build with torch takes about 4.6 GB on Linux and 2.9 GB on Windows. uv hard-links packages
from its cache (`data/envs/.uv-cache`), so a second build with the same torch costs little;
if a disk can't, uv copies, and `env install` says so.

#### Releases

The manager hands environments to its agents as *releases* in the bank (the farm's shared
folder, `share.dir`, where `manager.json` is; decision 0028). A release is an environment's
four files as committed, with their hashes, and is never changed once published:

```bash
python -m comfyfarm env publish gpu-test      # on the manager's host: into <share.dir>/environments/gpu-test/<fingerprint>/
python -m comfyfarm env releases              # what the bank has, newest first
python -m comfyfarm env install gpu-test --release latest     # on a machine: from the bank, not its checkout
```

`env publish` refuses an environment that isn't locked, whose lock is out of date, or with
changes not committed. `env install --release` copies the release to the machine, checks
every file against its hash, and builds from the copy. The build's `farm-env.json` says
which release it came from. An agent finds the bank through the `manager.json` its
`agent.yaml` names; otherwise give `--bank`. The manager rolling releases out to its agents
by itself comes in the next steps of phase 4.

Each agent tells the manager what it has built: for each environment, its current and
previous build, when each was built and from which release (or its checkout). It reads
them again every 30 s, so an `env install` on a machine shows within half a minute.
`GET /api/v1/environments` lists the bank's releases and what each machine has built; the
same is in `/api/v1/workers` as each machine's `environments`.

#### The manager installs them

The manager keeps what each machine should have of an environment, and each agent's
environment manager makes it so (decision 0030). Set it on the manager's host. Its API
takes this from its own host only, until the farm has API keys:

```bash
python -m comfyfarm env want gpu-test latest                     # every machine: install or update to the latest release
python -m comfyfarm env want gpu-test 30cee042f35be171 --machine tor-flame02-a6000   # one machine, one release
python -m comfyfarm env want gpu-test none                       # remove it
python -m comfyfarm env want gpu-test --forget                   # stop managing it: what is built stays
```

Each agent hears it at its next check-in. It installs from the bank's release (checked
against its hashes), builds beside the current build, checks it, and only then makes it
current. The build before stays as previous, so wanting the previous release again is a
rollback that switches back without building. It waits while a job is running in that
environment. It doesn't try a release that failed on it again until it is told something
else. An environment the manager doesn't name is left as it is.

It is off on each machine until `agent.yaml` says `envs: {managed: true}`. Until then the
agent says it is off. `GET /api/v1/environments` shows what each machine should have
(`wanted`) and what its environment manager is doing about it (`work`: installing,
waiting, failed and why, off).

#### Rollouts

`env want` tells the machines it names all at once. A rollout takes a release to them one
at a time instead: the canary first, then each next machine once the one before has it
installed and checked (decision 0031). The order is `rollouts.order` in `config.yaml`; for
the pilot that's the A6000, then the 5090, then the manager's host. On the manager's host:

```bash
python -m comfyfarm env publish gpu-test             # the release, from the repo as committed
python -m comfyfarm env rollout gpu-test latest      # to every machine, the canary first
python -m comfyfarm env rollouts                     # how they go: running at <machine>, done, failed and why
python -m comfyfarm env rollout gpu-test --pause     # before the next machine (--resume goes on)
python -m comfyfarm env rollout gpu-test --rollback  # the machines it reached go back to what they had
python -m comfyfarm env rollout gpu-test --cancel    # stop; the machines keep what they have
```

A machine whose install fails (a hash, the build, its check), or whose agent has
`envs.managed` off, stops the rollout there: the machines after it never hear of the
release. So does a machine that hasn't the release within `rollouts.step_minutes` (30).
A stopped rollout is rolled back or cancelled; a new one of that environment can start
once it is. A rollout outlives a manager restart. With `evals.gate` on, each machine is
drained when its turn starts and runs the evals of the definitions in the environment once
it has the release; a failed eval stops the rollout there (see *Evals*). `environments/canary-test` is broken on
purpose: its check always fails, so a rollout of it shows one stopping at the canary.

The dashboard's Machines tab shows all of it below the machines (decision 0032): a table of
machines by environments, each cell what that machine has current (✓ installed and checked)
or what its environment manager is doing, green when it is what the manager wants, red when
its install failed, with the error under the table; and the rollouts, each machine marked
in order. Hover a cell for where its build came from, its previous build and what the
manager wants. The tab only shows: changes are made on the manager's host, with the
commands above, until the farm has API keys.

### Process runner

Off by default (`process_runner.enabled` in `agent.yaml`; decision 0025). It runs *process
definitions*: a command from this repo, in one of the runner environments above, with a
time limit. A process definition is a `process.yaml` like a workflow's, with this runner
block:

```yaml
limits: {max_minutes: 30}            # stopped after this; default process_runner.default_minutes (60)
writes: {frames: {ext: png}}
runner:
  type: process
  environment: gpu-test              # environments/gpu-test: its current build on the machine
  command: [python, "{definition}/run.py"]
```

`python` first is the environment's Python. The farm fills in `{definition}` (the
definition's folder), `{run}` (the run's folder) and `{job}` (its `job.json`). Nothing of
the command comes from the job, and the definition's fingerprint covers every file in its
folder: a machine with another copy refuses the job.

The command runs in its run's folder (`data/runs/<attempt>/`) and reads `job.json` (also
in `$COMFYFARM_JOB`):

| Field | What |
|---|---|
| `params` | the job's parameters, checked against the definition |
| `input`, `inputs` | the image inputs as the farm resolved them, with `files`: the files to read, in order (a converted EXR or movie's PNGs), and their frame `numbers` |
| `output_dir`, `frames_prefix` | where its frames go and what they start with: `<frames_prefix>_00001.png`, ... |
| `writes` | what it writes: `frames` (`ext`), and each other kind its definition says (`file`, see below) with its `dir`, `prefix` and `ext` |
| `events`, `result`, `cancel` | the files it writes and watches (below) |
| `seconds` | its time limit |

It writes progress to `events` as JSON lines (`{"progress": 0.5, "stage": "frame 2/4"}`).
It can write `result`: an error for the artist if it fails (`{"error": "..."}`), and
`"warnings": [...]` for the job. It stops when the `cancel` file appears. At a cancel or the time limit, it gets `process_runner.grace_seconds`
(15) to stop, then it is killed with what it started. The agent then collects the frames as
it does a workflow's: renamed to the job's names, with a preview movie.

**Other files** (decision 0036). A process that writes something besides frames, such as a
report, says so in its definition: `writes: {frames: {ext: png}, file: {ext: json}}`.
`job.json`'s `writes.file` then gives it a `dir` and a `prefix`. It writes its files there,
each named `<prefix><anything>` (with that extension, when the definition says one). The
farm names them after the job, beside the frames and with their version:
`_comfyfarm_1234abcd_file.json` becomes `abc_0010.gputest_v001.json`, and
`..._file_stats.json` becomes `abc_0010.gputest_v001_stats.json`. A run that writes none
fails, since its definition said it would. Without frames, the files choose the output
folder and its version themselves. The job lists all it wrote by kind (`outputs.artifacts`:
`frames`, `file`), and the dashboard shows the files under the job.

Two more kinds take the same route (decision 0037). `movie` is a video (`ext`: mp4, mov,
webm, mkv or avi); in a job that writes no frames its MP4 is what the dashboard plays.
`model` is trained weights, such as a LoRA (`ext`: safetensors, ckpt, pt, pth, bin or
gguf), with each file's size in the outputs. It stays in the job's folder: putting it
where ComfyUI reads models is a step of its own, for later. A new kind is one file in
`comfyfarm/artifacts/`, a subclass of `base.Generic` naming its kind.

A small supervisor runs each command and keeps its time limit, also while the agent
restarts. An agent that comes back finds its runs again by their folders. A run that went
well leaves nothing behind. A failed run keeps its folder (`log.txt` holds what the command
printed) for 7 days.

`python -m comfyfarm agent --check` lists each process and whether this machine can run it:
with the runner on, its environment has to be built here at this checkout's version
(`env install`).

**One runner at a time on the card** (the GPU claim, decision 0026). A process job waits
while ComfyUI runs the farm's jobs, and the other way round, first come first served. Before
a process runs, ComfyUI frees its models (as *Free VRAM* does). If it can't, because it
has prompts queued that aren't the farm's, the job waits, saying why, for
`claim_wait_seconds` (5 minutes). Then it starts anyway, and the job carries a warning that
it shared the card. The process runner runs one job at a time; ComfyUI keeps
`max_inflight`. ComfyUI's next job after a process loads its models again.

#### The GPU test

`gpu_test` (`workflows/gpu_test`, decision 0027) is the farm's own test of all this: torch
reads the input frames onto the GPU, blurs, inverts or grades them there, and writes PNGs
like a workflow. *Hold GPU memory* keeps that much of the card while it runs. It writes a
report beside the frames, named like them (`<source>.gputest_v001.json`, a `file`): the
card, how much was free when it started, and the most it used.

Phase 3's gate, on each machine:

1. In `agent.yaml`, set `process_runner: {enabled: true}`; restart the agent. `agent --check`
   should say *process gpu_test: its environment is built here* (otherwise `env install
   gpu-test`).
2. Drain the other machines on the Machines tab, so the jobs go to this one.
3. Run a ComfyUI workflow (`ltx25_alpha_gen`), so ComfyUI holds its models.
4. Run `gpu_test` on a few frames with *Hold GPU memory* at most of the card: 20 on the 3090,
   28 on the 5090, 44 on the A6000. It should finish with no warning. The Machines tab's
   memory tooltip shows ComfyUI freeing its models just before, and the report shows the
   card free.
5. Run the ComfyUI workflow again: it loads its models again, and finishes.

To try a cancel or the time limit, give it *Seconds per frame*.

### How the manager and agents work together

- **Agents only ever call the manager** (`/api/v1/agent/...`), so GPU machines need no
  open port and the manager needs no list of them. An agent checks in every few seconds;
  one that stops for `farm.lost_after_seconds` (5 minutes) has its jobs queued again for
  the others.
- **A job goes to the fastest free GPU for it:** machines rank by how fast their GPU type
  ran the job's workflow before, for the job's size (the run times below, the *Run times*
  tab's #), so LTX goes to a free 5090 before a free A6000 though the A6000 has more VRAM.
  Machines whose GPU type hasn't run the workflow yet come after those (they get it when
  the faster ones are busy, which times them too), by VRAM (or by `agents.order` in the
  manager's config, then VRAM); a workflow that has never run goes to the biggest GPU
  first. Each job goes to the best machine that is free and can run its workflow; the next
  job to the next machine. Only when every GPU is busy does a machine get a second job,
  prepared ahead of its current one (the best machine first). The Machines tab's # is the
  order for a workflow without run times. Machines whose ComfyUI lacks a workflow's nodes or
  models don't get it, nor do machines its manifest's `requires` keeps it off (too little
  VRAM: the LTX workflows need 32 GB, so the 3090 never gets them; missing tags). A job
  no machine may take now waits in the queue, saying why, and runs once one checks in.
- **Warm models:** between equal cards (two 5090s), the one that last ran the workflow,
  with its models still loaded, gets the job. A faster free card still comes first.
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

**Dashboard:** the address in `manager.json` (e.g. `http://wrk1125l.rodeofx.com:9090`). Its tabs:

- **Submit:** pick the provider, the task and the workflow (ComfyUI Local › Masking › SAM3; see
  *Menus: providers and tasks*), fill in the form, check the destination line, submit. The
  farm's hidden workflows (its GPU test) are listed too, marked "hidden from artists". An input
  takes a path or a ShotGrid Version link (see *ShotGrid Version links*). A link to
  `/#submit?workflow=<name>&input=<path or Version link>` opens the form filled in, its provider
  and task picked.
- **Batch Submit:** many jobs from one table, e.g. one per Version of a ShotGrid playlist, or
  of a ShotGrid query (the latest `mp01` plates of a sequence; see *Batch Submit*).
- **From ShotGrid:** `/#shotgrid` opens the ComfyFarm button for the bookmarks bar, and how to
  drag Versions and Playlists here or right-click them in ShotGrid (see *ShotGrid right-click*).
- **Queue:** everyone's jobs, live, with the workflow each runs and the machine it runs on (for a
  queued job, → the machine expected to take it). *Wait* shows its place in the queue and when it
  should be done, or the time left once running. Click a
  row for input/output paths (copy buttons), the preview movie (▶ in the row: it has one; see
  *Preview movies*), warnings, errors with traceback, *Run again* and *API JSON*, and on a
  finished job *Publish…* (see *Publishing*).
- **Machines:** each GPU machine, in the order they get jobs of a workflow without run times (#): whether it takes jobs, its GPU and
  free VRAM, its jobs, versions, and any workflow it can't run (a missing node pack or model)
  and why. *Drain* / *Resume* per machine. Below them, *GPU history*: each GPU's use and memory
  over the last hour, 6 hours, day or week (see *GPU history*).
- **Run times:** how long each workflow takes on each GPU type, what the ETAs come from, and
  the order free GPUs get its jobs in (#: fastest first).
- **Usage:** jobs, GPU hours and failures over the last 7, 30 or 90 days, per day, show,
  definition or machine (see *Usage*).
- **Alerts** (while alerts are on): what needs a person, and the latest cleared. While any
  alert holds, a red chip in the header says how many, on every tab (see *Alerts*).
- **Nuke Publish Templates** (while publishing is on): the publish templates, their history, publishing and restoring one (see *Publishing*).

**Python / CLI** (`client/comfyfarm_client.py`, or download it from
`http://<farm-machine>:8000/client.py`):

```bash
export COMFYFARM_URL=http://comfyfarm01:8000
python comfyfarm_client.py workflows
python comfyfarm_client.py catalog show=bri3        # the menus, and what bri3 may not use
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
created next to its source. Jobs are submitted from the form, or from a ComfyFarm node
(*ComfyFarm node* below).

**Install:** each artist runs `python3 /rdo/bank/comfy_farm/client/nuke/install_for_me.py`
once and restarts Nuke (*Deploying → Artists*). The manager keeps that copy of the client
up to date, and the panel finds the manager through the `manager.json` next to it.

The panel works in Nuke 13 to 15 (PySide2) and Nuke 16+ (PySide6), and needs nothing
installed: the client is standard library only.

**Artist workflow:**

1. Prerender the plate to a PNG sequence with a Write node (`…/name.%04d.png`), with the
   colour baked in the way the workflow expects. The farm reads the PNGs exactly as they are.
2. Open the panel (*ComfyFarm → Open panel*, or *Windows → Custom → ComfyFarm*). On the
   *Submit* tab, pick the *Provider*, the *Task* and the *Workflow* (ComfyUI Local → Masking →
   SAM3; the farm's menus, see *Menus: providers and tasks*). The card under them says what
   it does, where it runs, whether its inputs leave Rodeo (off-site), and why the session's
   show (`RDO_CURRENT_SHOW`) may not use it, if it may not; such a workflow is listed
   *(not on this show)*. Then fill in the form: paste the input path, *Browse…*, or select the Read (or
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
session's `RDO_CURRENT_SHOW` / `RDO_CURRENT_SHOT` (with `RDO_CURRENT_SEQUENCE`), and the step
as `RDO_CURRENT_STEP` when it's one of the studio's. Everything can be changed. A field you've
typed in stays as it is while the input path names the same shot; a path naming another show
or shot fills show, sequence and shot again. With ShotGrid lookups on, show and shot suggest as you type, *Sequence* narrows the
shots down and **↻** reloads them. *Publish log* (right-click) shows every publish of the job
with the command and Daiquiri's output.

Movies: a Read of a `movhistory` `.mov` sends the whole movie. The farm numbers its frames
from the timecode and drops the slate, as it does from the dashboard.

### ComfyFarm node

A workflow can also be a node in the script: a Group with the workflow's fields as knobs,
its image inputs as input arrows, and its result as its output (decision 0051).

- **Make one:** in the panel, fill in the form and click *Create node* (the node gets the
  form's values and is wired to the Read the input came from), or press Tab in the node
  graph and pick *ComfyFarm → <provider> → <task> → <workflow>*, e.g. *ComfyFarm → ComfyUI
  Local → Masking → SAM3* (the farm's defaults, wired to the selected Read/Write nodes).
  *Reload workflows*, at the top of that menu, asks the farm again.
  - The menus are the farm's for the Nuke session's show. A workflow that show may not use
    is listed *(not on this show)*, and picking it says why instead of making the node.
  - Hidden workflows (the GPU test) aren't listed. A farm without the menus lists the
    workflows straight under *ComfyFarm*.
- **Its label** names its provider, then its workflow, then the picked version: *ComfyUI
  Local / ltx25_layout_render / v003 done*.
- **Inputs:** connect a Read or Write to each input arrow (Dots are fine). The farm reads
  files, so a comp branch needs a Write rendered first. A one-image input takes the current
  frame. A path typed into the input's *path* knob is used instead of the connection.
- **Submit** queues a job from the knobs as they are. Every Submit is a new version;
  *Version* picks the one the node shows. Until it's done the primary input passes through;
  then the node outputs its frames (a texture job: one map at a time, *Map*).
- **Refresh** asks the farm about the node's jobs. After reopening a script, press it to
  follow running jobs again. It also says when the workflow changed on the farm: **Rebuild**
  updates the knobs and inputs and keeps the values, connections and versions.
- **Cancel** stops the picked version. **Publish when done** and **Publish version** use
  the same fields as the panel.
- With the retention sweeper on, the status says when a version's frames are deleted, and
  *Expired* after. Publish what you keep.

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
text encoder, and the node packs KJNodes (`WanVideoNAG`), Easy-Use (`easy float`) and
RES4LYF (the `res_2s` sampler and `beta57` scheduler, named in the manifest's `node_packs`);
the Machines tab lists what a machine is missing.

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

### Parameters for `sam3_ltx_alpha`

| Param | Default | Notes |
|---|---|---|
| `input` | required | The plate: a sequence with an optional `first-last` range, or a `.mov`/`.mp4` (timecode frame numbers). Up to 960 frames |
| `subject` | required | What to matte: one word or a short phrase SAM3 knows (`person`, `car`, `dog`, `red car`). When SAM3 finds several, the one it scores highest is matted and the others (the nearest 8) are painted out |
| `pick_frame` | first frame | The frame number SAM3 looks for the subject on. Give one where it is clearly visible if it enters later; it is tracked backward as well as forward from there |
| `output_path` | show default | Root folder: frames go to `<this>/<shot>/<source>.matte_vNNN.<frame>.png`. Same rules as above |

The alpha comes out as 16-bit grey PNGs (*ComfyFarm Save Image* with `bit_depth` 16), one
per input frame, at the input's size. Fixed in the workflow: Best quality (two LTX passes,
the second on a plate with everything but the subject painted out), 24 fps, seed 42,
SAM3 threshold 0.3. Plates bigger than 1920×1088 are matted at that size (LTX alpha-gen's)
and the alpha is scaled up to theirs.

It runs [Comfy Pro Alpha Generator](https://github.com/rik-python/ComfyUI-Comfy-Pro-Alpha-Generator)
(GPL-3.0) without its picker: *ComfyFarm Pro Alpha* (`comfyui_nodes/comfyfarm_nodes/pro_alpha.py`)
asks SAM3 for the subject on the pick frame, writes the picks the picker would, and adds the
generator to the graph. What each machine needs on top of LTX 2.5:

- The node packs ComfyUI-Comfy-Pro-Alpha-Generator (git, 74e4368) and SAM3 (the Nodes
  Manager's *SAM3* by pznodes, 0.1.21), once for every machine in the farm library's
  `custom_nodes` (`/rdo/bank/comfy_farm/comfyui/custom_nodes`, `B:\comfy_farm\comfyui\custom_nodes`)
  as `ComfyUI-Comfy-Pro-Alpha-Generator` and `comfyui-sam3`: the farm finds a pack by its
  folder name. No machine may have its own copy as well: two SAM3s register the same web
  route and ComfyUI stops at start ("method POST is already registered"); `comfyui sync`
  moves a local copy aside. SAM3 sets up its own Python environment with `comfy-env` (pixi,
  in the user's `~/.ce`) the first time ComfyUI starts with it, which needs the internet once.
- On Linux, enough open files. SAM3 runs in its own process and hands each result to
  ComfyUI in shared memory, one open file per tensor ComfyUI holds: a tracked shot is one
  per frame and object, past the usual limit of 1024 ("[Errno 24] Too many open files").
  The agent raises the limit to the machine's hard limit for the ComfyUI it starts (its
  log's `started by agent` line shows it). A ComfyUI started by hand needs
  `ulimit -n $(ulimit -Hn)` first. Raising the hard limit itself needs root (limits.conf).
- LTX 2.5's conv video VAE, `ltx-2.5-video-vae-conv-bf16.safetensors`, in the library's
  `models/vae`: only it can encode the plate that guides alpha-gen.
- SAM3's and ViTMatte's models in ComfyUI's own models folder, where those packs read them
  (they ignore `extra_model_paths.yaml`): `ComfyUI/models/sam3/sam3.safetensors` and the
  folder `ComfyUI/models/vitmatte/vitmatte-base-composition-1k/`. Copy them from
  `/rdo/bank/comfy_farm/comfyui/models/` (or link the folders). Without them a job stops
  with the paths, instead of the packs downloading 4 GB from Hugging Face mid-job.

The generator also saves its own copy of the alpha in ComfyUI's output folder
(`comfyfarm_pro_alpha/`); the next run clears it, with the generator's temp files. The
author's timings on a 5090: about 25 minutes for 247 frames at 1080p.

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

### Preview movies

Every job that writes two or more frames gets a preview movie: once the frames are renamed, the
agent that ran it encodes them into an H.264 MP4 next to them, named by `paths.preview_name`,
e.g. `305046_0010.plate.alpha_v002_preview.mp4`. The dashboard plays it in the job's details,
with its path (copy button); in the queue, ▶ marks the jobs that have one.

- **The frames as they are:** PNG is display-referred, so there's no colour conversion (a matte
  shows grey), and no burn-ins. Tagged BT.709.
- **Frame rate:** the input movie's, when the input is a movie; otherwise `preview.fps` (24).
- **Size:** frames wider than `preview.max_width` (1920) are scaled down, aspect kept.
- **Numbering:** the movie's timecode is its first frame's number (1001 is `00:00:41:17` at 24
  fps), so a preview used as an input is numbered like the frames.
- **Which jobs:** not a single image (Wan) or texture maps (CHORD). A workflow's manifest can
  say `preview: false`; one with a video node of its own (`outputs.preview`) keeps that instead.
- **Cost:** made while the outputs are collected, when the GPU is already free for the next
  job: a few seconds for a shot. The agents' ffmpeg (from `imageio-ffmpeg`) needs no setup.
- **When it can't be made:** a warning on the job; the frames are what counts, and the job is
  done.

The settings (`preview:` in the manager's `config.yaml`: `enabled`, `fps`, `max_width`, `crf`)
are frozen into each job at submit, so every agent makes the same movie and a change only
affects jobs submitted afterwards.

### Provenance

With `provenance: {enabled: true}` in the manager's `config.yaml`, the agent that ran a job
writes a JSON file beside its outputs, before it reports the job done (decision 0038). A file
found on disk later says where it came from, and the retention sweeper (to come) will delete
only the files one lists.

- **Its name** follows the outputs': `305046_0010.plate.alpha_v002.provenance.json` beside
  `305046_0010.plate.alpha_v002.1001.png`. Files of other kinds (a report, a movie, a model)
  are named the same way. A texture folder gets `provenance.json`, and a set of batches
  one per batch (`provenance.002.json`).
- **What it says:**
  - the job, the attempt, the user and the name;
  - the definition's name and fingerprint;
  - the machine, its GPU and driver, and the farm's build;
  - what ran it: ComfyUI's version, commit, Python and torch, and how the machine differed
    from the farm's environment file; or a process's environment, its build and the release
    it came from;
  - when it was dispatched, started and finished, and the run's seconds;
  - the inputs with their frame range, and the ShotGrid Versions;
  - the parameters;
  - the output folder, every file the job wrote there, and the warnings.
- **Paths** are studio paths (`/rdo/shows/...`), whichever machine ran the job.
- **When it can't be written:** a warning on the job; the outputs stand and the job is done.
- **Where to see it:** the dashboard's job details give its path (Provenance), and
  `GET /api/v1/jobs/{id}` gives it as `outputs.provenance_posix`.
- **The EXR stamp:** each texture map's EXR carries two string attributes in its header
  (decision 0039), so a map copied away from its folder still says where it came from:
  - `comfyfarm:job`: the job's id (`http://<manager>:9090/#queue?job=<id>` opens it on the dashboard);
  - `comfyfarm:provenance`: the provenance file's name in the map's folder.

  Nuke shows them in a Read's metadata as `exr/comfyfarm:job` and `exr/comfyfarm:provenance`
  (a ViewMetaData node, search `comfyfarm`). From a shell: `oiiotool --info -v <map>.exr`.

The setting is frozen into each job at submit: a change only affects jobs submitted afterwards.

### Job metrics

Each job leaves one small row in the manager's database when it finishes (done, failed or
cancelled), and the row stays when the job itself is deleted (decision 0040). It is how much
the farm ran, for whom and on what, after the jobs are gone.

- **What a row holds:**
  - the job, its kind (a job, a texture set, or one batch of a set, with its set);
  - the workflow and its fingerprint;
  - the user and the show;
  - the machine and its GPU type;
  - how it ended, its attempts, and its frames or tiles;
  - when it was submitted, given to a machine, handed to the runner, started on the GPU and
    finished.
- **Never in a row:** its parameters, prompts, paths or error messages.
- **A job that finishes again** (retried, or a set's failed batches run again) keeps one row,
  with its last outcome.
- **Jobs that had already finished** when the manager first ran this get their rows when it
  starts. Their GPU type is empty: it is noted when a job is given to a machine, and the job
  record now says it (`gpu`).
- **To read them:** `GET /api/v1/job-metrics` (JSON), or with `?format=csv` for a spreadsheet,
  for example
  `http://wrk1125l.rodeofx.com:9090/api/v1/job-metrics?format=csv&since=2026-11-01`. Each row
  gives local times and how long each part took:
  - `wait_seconds`: submitted until given to a machine;
  - `prepare_seconds`: inputs prepared;
  - `gpu_wait_seconds`: waiting for the GPU;
  - `run_seconds`: on the GPU, outputs collected;
  - `total_seconds`: submitted until finished.

### Policy

Which shows may use the farm, and with which definitions, is the farm's policy (decisions
0007, 0043): `policy.yaml` in the bank folder (`share.dir`), edited by the farm's admins. A
job the policy refuses is never planned: the artist reads which rule refused it, why, and
who to ask.

```yaml
policy:
  enabled: true            # in the manager's config.yaml; off by default
  file: policy.yaml        # a name in share.dir, or a path of its own
```

- **Start from `policy.example.yaml`.** Each rule names its shows (patterns work: `ret*`;
  `*` is every job, its show known or not), shows to leave out (`except: [rnd*]`), and a
  reason the artist reads.
  - A show rule says `deny: all`, `deny: [definitions]` or `allow: [definitions]` (only
    those).
  - A licence rule (under `licences:`) says `deny: [licences]` or `allow: [licences]`.
    These are the licences of the model weights a definition lists under `models:` in its
    `process.yaml` (decision 0044). Licences are compared without case.
  - `unlisted_models` (allow or deny) is for a definition that doesn't list its models, on a
    show a licence rule is for. `models: []` says it uses no weights.
  - A quota (under `quotas:`) sets `max_jobs` and/or `max_frames` (tiles, for textures):
    the most that may be queued or running at once (decision 0045). The count is per show
    (`per: show`, the default), per user over all the rule's shows (`per: user`), or for the
    shows together (`per: all`). A texture set counts as one job; its frames are its
    unfinished batches' tiles. The refusal says how many are queued and what the most is.
  - A provider rule (under `providers:`) says `allow: [outside providers]` or
    `deny: [providers]` (decision 0058). See *Outside providers* below.
  - Every rule naming a job's show is checked, and the first that refuses it is named.
  - `other_shows` (allow or deny) is for shows no rule names.
  - `unknown_show` is for a job whose show can't be worked out.
  - `contact` is named in every refusal.
- **Check a file before it goes in the bank:**
  `python -m comfyfarm policy check /path/to/policy.yaml`. It validates the file and every
  definition it names against this checkout's `workflows/`, and lists the rules and the
  models and licences each definition lists. Notes point out:
  - definitions that don't list their models;
  - files `deploy/comfyui/environment.yaml` gives a definition that its `models:` leaves out;
  - licences a rule names that no definition carries, which may be a typo.
- **The manager reads it again when it changes** (within seconds).
- **A file that doesn't validate isn't taken.** The last valid one stays in force, and the
  manager keeps a copy in `data/policy.json` so a restart keeps it too.
  `GET /api/v1/policy` shows the rules in force and what's wrong with the file.
- **The dashboard's Policy tab**, shown when policy is on, is read-only (decision 0047):
  - where the rules came from and when, and the file's error if it isn't taken;
  - the show rules, licence rules and quotas;
  - what is queued or running against each quota, a full one marked;
  - the models and licences each definition lists.

  It reads again every 15 seconds while it's open. With policy
  on and no valid file ever read, nothing is planned, so a client's restriction can't
  lapse because of a typo.
- **Checked when a job is planned:** the dashboard's and Nuke's preview, every submit,
  Run again, Batch Submit's rows, and a set's Retry failed. A submit plans under the
  submit lock, so two submits can't both take a quota's last place.
- **Checked again when a machine would take a queued job** (decision 0046), against the
  show and licence rules as they are then. A job a rule written since it was queued refuses
  fails with "Not run: the farm's policy changed after it was queued" and the rule. Quotas
  aren't asked again: they are for new submissions. A job already on a machine runs to the
  end. While the policy can't be read (on, and no valid file ever read), queued jobs wait.
- **Identity is trusted until API keys come.** Show and licence rules are by show and
  definition. Quotas per user count the name the client sends, so they keep honest users
  within a share, not anyone determined to get round them.

#### Outside providers

A workflow that runs at an outside provider (fal.ai; `offsite: true` in its
`providers/<name>.yaml`) sends the show's images and prompts outside Rodeo. The policy is
where that is signed off (decision 0058):

- **No show may use one until a rule allows it.**
- **Its jobs need a known show,** from the input path or given. A menu drawn without a
  show (the dashboard's) lists it if a rule allows it on some show; the job's own show is
  checked when it is planned.
- **With policy off, no show may use one.** `providers.enabled` in config.yaml switches a
  provider on for the farm; the policy says which shows may use it.
- **A `deny` rule refuses a provider on its shows**, the farm's own too
  (`deny: [python-local]`). A deny wins over an allow, so `shows: [rnd*]` with
  `allow: [fal]` and a rule for `rnd2` with `deny: [fal]` leaves rnd2 out. An `allow` names
  outside providers only: the farm's own need no allowing.
- **The pilot's rule** allows fal.ai on `rnd` alone, so no client material reaches the
  pilot's account (`policy.example.yaml`):

  ```yaml
  providers:
    - name: pilot-fal-on-rnd
      shows: [rnd]
      allow: [fal]
      reason: The fal.ai pilot runs on rnd alone, so no client material reaches the pilot's account
  ```
- **Where it is checked:**
  - when a job is planned. A refusal names the rule, or says no rule allows the show
    (`Policy: providers`);
  - in the menus: Nuke's are for the session's show, so another show's fal.ai workflows
    are marked with the reason;
  - again when the gateway would take a queued job, so a rule taken away fails the jobs it
    allowed;
  - for the farm's own reference jobs (evals) too, which have no show: one at an outside
    provider is refused.
- **`comfyfarm policy check`** lists the provider rules and, for each outside provider, the
  shows that may use it.
- **The Policy tab** shows them under *Provider rules*.

### Prometheus metrics

`GET /metrics` gives the farm's numbers in Prometheus' text format, for a Prometheus or a
Grafana Agent to scrape (decision 0048). Every number comes from what the manager already
holds, and nothing needs switching on:

- **the manager:** `comfyfarm_manager_info{build}`, `comfyfarm_manager_start_time_seconds`;
- **the queue:**
  - `comfyfarm_jobs{status,workflow}`, the jobs in each active state (a texture set's
    batches, not the set);
  - `comfyfarm_queue_oldest_seconds`;
- **finished jobs,** from `job_metrics`, so they outlive the jobs:
  - `comfyfarm_jobs_finished{workflow,outcome,gpu}`;
  - `comfyfarm_job_run_seconds_sum`/`_count{workflow,gpu}` and
    `comfyfarm_job_frames_sum{workflow,gpu}`: seconds a frame is the one over the other;
  - `comfyfarm_job_wait_seconds_sum`/`_count{workflow,outcome}`;
- **machines:** `comfyfarm_machine_up`, `_runner_up`, `_drained`, `_jobs` and
  `_info{agent,gpu}`, each by `machine`;
- **GPUs:** `comfyfarm_gpu_utilization_ratio` and `comfyfarm_gpu_memory_used_bytes`/`_total_bytes`
  `{machine,gpu,name}`. Use and memory come from `gpu_probe` (nvidia-smi); with the probe
  off, memory comes from ComfyUI's report;
- **the policy and the sweeper:** `comfyfarm_policy_enabled`, `_in_force`, `_file_error`; the
  sweeper's latest report as `comfyfarm_sweep_expired`, `_files`, `_bytes`, `_problems` and
  `_last_run_timestamp_seconds`.

Every family is a gauge: a job finishing again replaces its `job_metrics` row, so its
counts can go down. A scrape every 30 to 60 seconds is plenty:

```yaml
scrape_configs:
  - job_name: comfyfarm
    static_configs: [{targets: ["wrk1125l.rodeofx.com:9090"]}]
```

### GPU history

The manager keeps each machine's GPU use and memory over time, for the dashboard (decision
0049), in its own database. Nothing else needs running.

```yaml
gpu_history:
  sample_seconds: 60       # in the manager's config.yaml
  days: 7                  # 0 keeps none
```

- **Sampled from what each agent last reported,** every `sample_seconds`. Each GPU of each
  machine checking in gets one sample: its use and memory, and the jobs the machine held.
  - Use needs `gpu_probe` on in the machine's `agent.yaml` (nvidia-smi).
  - Without it, only memory, from ComfyUI's report.
  - A machine that isn't checking in isn't sampled, so its gap shows.
- **Samples older than `days` go,** once an hour.
- **`GET /api/v1/workers/{name}/gpu-history?hours=24`** gives a machine's samples over the
  last hours, oldest first. Over a long span they're averaged into steps, about 500 per GPU
  at most: use averaged, memory and jobs at their highest.
- **On the dashboard,** the Machines tab's *GPU history* panel draws it (decision 0050): a
  graph per GPU over the last hour, 6 hours, 24 hours or 7 days.
  - Use and memory in use share one scale, 0 to 100% of the card. Shaded: the machine held
    farm jobs.
  - A break in a line is a time the machine wasn't checking in.
  - Above each graph: its average use, its highest memory, and how long it held farm jobs.
  - Point at a graph (or focus it and use the arrow keys) for that time's numbers.
  - It reads again every minute while the tab is open.

### Usage

The dashboard's **Usage** tab gives the farm's jobs, GPU hours and failures over the last 7,
30 or 90 days, from the job metrics (decision 0050), so the numbers outlive the jobs.

- **Per day, per show, per definition or per machine,** for every show and definition, or one
  of each (the two lists). Clicking a show or a definition shows it per day.
- **For each:** jobs, done, failed (with their share), cancelled, GPU hours (with a bar),
  frames done, and the average wait to be given to a machine.
- **What counts:**
  - Jobs that finished in those days, today included. Days are the manager's local days.
  - A texture set counts each of its batches: each runs on its own.
  - GPU hours run from a job's start on the GPU until it finished, outputs collected, whatever
    its outcome: a failed job held the GPU too. The tooltip says how many went on jobs that
    failed or were cancelled.
  - A retried job counts its last attempt only: job metrics keep one row per job.
- **CSV** downloads every job of those days, one row each (`/api/v1/job-metrics`).
- **`GET /api/v1/usage?days=30&by=day`** gives the same numbers as JSON. `by` is `day`,
  `show`, `workflow` or `machine`; `show=` and `workflow=` narrow it to one (`show=` with
  nothing: jobs with no show).

### Alerts

The manager checks every minute for what needs a person (decision 0052). It is off until
switched on, in the manager's `config.yaml`:

```yaml
alerts:
  enabled: true
  machine_silent_minutes: 10   # 0 turns a rule off
  job_waiting_minutes: 60
  failures_in_a_row: 3
  sweep_problems: true
  rollout_problems: true
  google_chat: 'https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=...'
```

- **The rules:**
  - **A machine silent:** it hasn't checked in for `machine_silent_minutes`. A drained machine
    is left out: drain a machine before working on it. A machine that hasn't checked in since
    the manager restarted is known from its GPU history (see *GPU history*).
  - **Jobs waiting:** jobs queued for `job_waiting_minutes` without a machine. One alert for
    them all, naming the oldest and, when the farm knows, why it waits (no machine that can
    run it is taking jobs, or no valid policy).
  - **Failures in a row:** a machine's last `failures_in_a_row` jobs, done or failed, all
    failed. Cancelled jobs don't count. It names the latest and its error.
  - **Sweep problems:** the retention sweeper's latest report lists problems.
  - **A rollout or sync stopped:** a rollout stopped at a machine, or a ComfyUI sync stopped at
    a machine it left drained. Each waits for a person: roll back or cancel, or look and
    resume.
- **Raised once, cleared by itself.** An alert is raised when its condition starts and
  cleared when it ends: the machine checks in, the jobs leave the queue, a job on the
  machine finishes done, the rollout is rolled back. While it holds, its message follows
  what it is about (how many jobs wait).
- **Where they go:**
  - The manager's log: a warning when raised, a line when cleared.
  - The dashboard: a red chip in the header on every tab, and the *Alerts* tab with the rules,
    what holds (what is wrong and what to do) and the latest cleared.
  - With `google_chat`, one message to that Google Chat space for each check that raised or
    cleared any, with a link to the Alerts tab.
- **Google Chat:**
  - In the space: *Apps & integrations* > *Webhooks* > *Add webhook*. Copy its address into
    `google_chat`.
  - The address lets anyone post to the space, so it stays in `config.yaml`. That file is
    never committed, and no endpoint gives the address.
  - `comfyfarm alerts test` on the manager's host posts a test message, to check the address,
    and that the host reaches `chat.googleapis.com` (`HTTPS_PROXY`, if the studio needs one):
    `env -u PYTHONPATH -u PYTHONHOME "$(cat run/python)" -m comfyfarm alerts test`.
  - A post that fails is logged and shown on the Alerts tab. The alert still holds there.
- **`GET /api/v1/alerts`** gives the rules, what holds and the latest cleared, as JSON.
- **Cleared alerts are kept** `keep_days` (30) in the manager's database.

### Evals

A definition's eval is its reference job: an input kept in its own folder, its params, and
checks on what it writes (decision 0053). `comfyfarm eval` runs it on the machines. With
`evals.gate` on, rollouts and ComfyUI syncs run the evals on each machine before they go on,
and a failed eval stops them there.

```yaml
eval:                                   # in the definition's process.yaml
  inputs: {input: eval/input.png}       # each input: a file (or eval/plate.%04d.png 1-3) in its folder
  params: {operation: invert}           # its other params; the rest take their defaults
  checks:
    files: {png: 1, json: 1}            # how many files it writes, of each extension named
    size: [64, 48]                      # every image it writes
    like:                               # an output against a reference image in its folder
      - {output: '*.png', expected: eval/expected.png, max_error: 0.002}
```

- **`gpu_test` and `chord_pbr` have one.**
  - `gpu_test`'s inverts a 64 × 48 image and compares it with the exact inversion.
  - `chord_pbr`'s runs a brick texture through CHORD and checks its five maps are written. It
    has no `like` check yet: see the last point.
- **An eval is a farm job,** on the Queue tab as "eval on <machine>":
  - It is pinned to its machine, which takes it even while drained, before any artist's job.
  - Its outputs go into that machine's scratch folder, `evals/<definition>`, emptied before
    each eval of it, so the latest stays there to look at.
  - The farm plans it for no show, so the policy doesn't apply.
  - The machine's copy of each eval file must be the manager's (their SHA-256 come with the
    job), or it doesn't run: update its checkout.
- **The checks run on the machine,** once the farm has collected the outputs.
  - One that doesn't pass fails the job, and its error says which and by how much.
  - `like` measures the mean difference per channel: 0 to 1 for 8- and 16-bit images, EXRs in
    their own values. It uses OpenImageIO, or the standard library for PNGs.
- **`comfyfarm eval`,** on the manager's host:

  ```bash
  python -m comfyfarm eval gpu_test                                  # every machine that may run it
  python -m comfyfarm eval chord_pbr --machine tor-flame02-a6000     # this one (again for more)
  ```

  It prints each machine's checks and what they measured. It exits 1 if any didn't pass.
- **The gate** (`evals.gate: true` in the manager's `config.yaml`, off by default):
  - **A rollout** runs the evals of the definitions run in its environment
    (`runner.environment`). Each machine is drained when its turn starts, installs the
    release, runs its evals, then takes jobs again.
  - **A ComfyUI sync** runs the evals of the ComfyUI definitions each machine could run
    before. It runs them once that machine's ComfyUI is back and checked, while it is still
    drained.
  - **What stops them:** a failed eval, or evals not over within `evals.minutes` (30). The
    rollout or sync stops at that machine, which stays drained. Rolling a rollout back gives
    it its jobs again, with what it had.
- **Adding `like` to an eval:**
  1. Run the eval on a machine you trust.
  2. Take the outputs from its scratch folder (`evals/<definition>`).
  3. Put the ones to compare against in the definition's `eval/` folder, and add a `like`
     check for each, by pull request.
  4. Run `comfyfarm eval` on every machine. Each prints the mean error it measured: set
     `max_error` a little above the largest, since GPUs of different types differ slightly.

### Retention

The sweeper is how the farm's outputs will stop filling show storage (decisions 0006, 0041).
For now it is a **dry run**: it writes what it would delete, and deletes nothing. Deleting
comes after a week of dry runs that list only files the farm wrote.

```yaml
retention:
  enabled: true            # in the manager's config.yaml; off by default
  keep_days: 3
  at: "03:00"
```

- **When a job expires:** `keep_days` after it finished, or after it was last sent to Tractor
  to be published. A job with a publish on its way (queued, or asked for at submit and not
  made yet) is kept.
- **What it would delete:** only the files the job's own provenance file lists (README,
  Provenance), in the job's own output folder, and the provenance file itself. And only in
  the farm's own renders folders (`paths.output_root` for the job's show): a job whose
  artist picked its output folder keeps all its files, since a comp may read them
  (decision 0042).
  - A provenance file that belongs to another job, isn't the farm's, can't be read or sits
    in another folder means nothing of that job is deleted.
  - A listed name that leads out of the folder, or isn't a plain file, is left alone.
  - Files nobody listed stay. A job without a provenance file (provenance was off when it
    was submitted, or it didn't finish) keeps all its files; only its record would go.
  - A texture set goes as a whole, with every batch's files.
- **When it runs:** once a day at `at` (the manager's local time). If the manager was down at
  that time, it runs when the manager starts.
- **The reports** are kept in `data/sweeps/<date>.json`, the last `reports` (60) of them:
  - each expired job, its folder, the files it would delete and their size;
  - files already gone, and whether the folder would then be empty;
  - why a job's files all stay, when that is the rule (no provenance file, or a folder
    the farm didn't make);
  - every problem (anything else: those are what to look at), and the totals.

  To read them:
  - `GET /api/v1/sweeps` lists them;
  - `GET /api/v1/sweeps/latest?format=text` is the latest one to read, for example
    `http://wrk1125l.rodeofx.com:9090/api/v1/sweeps/latest?format=text`.

  To run one now, on the manager's machine:
  `curl -X POST http://localhost:9090/api/v1/sweeps`.

### REST API

| Method | Path | |
|---|---|---|
| GET | `/api/v1/health` | Liveness, auth on/off, queue size |
| GET | `/api/v1/workflows` | Workflows and parameter schemas, plus any preflight problems |
| GET | `/api/v1/catalog?show=&hidden=` | The menus, Provider > Task > Workflow, for a show: each workflow with its family's siblings, and `allowed: false` with the policy's `reason` when the show may not use it; `hidden=true` adds the farm's hidden ones (see *Menus: providers and tasks*) |
| POST | `/api/v1/jobs` | Submit `{workflow, params, name?, priority?, show?, shot?, publish?}` → job (`publish`: publish it when done, the fields below) |
| POST | `/api/v1/plan` | Same body as submit; returns the destination folder and first/last file names without queuing, and `eta` (`run`: its seconds, `done_in`: behind the queue, `workers`); with `publish`, also whether publishing it when done would be accepted (`publish`: `{request}` or `{why}`) |
| GET | `/api/v1/jobs?user=&status=&active=true&limit=` | List jobs and texture sets, with `queue_position`, `eta_seconds` (until done) and, while queued, `next_worker` |
| GET | `/api/v1/jobs?parent={set id}` | A texture set's batches, in order |
| GET | `/api/v1/jobs/{id}` | One job (full id or 8-char prefix) |
| DELETE | `/api/v1/jobs/{id}` | Cancel (queued, waiting in ComfyUI, or running) |
| POST | `/api/v1/jobs/{id}/retry` | Same params, new version |
| POST | `/api/v1/jobs/{id}/retry-failed` | A texture set: run its failed and cancelled batches again, same folder |
| GET | `/api/v1/jobs/{id}/prompt` | The exact API JSON sent to ComfyUI, as the machine that ran the job reported it (there once the job has started) |
| GET | `/api/v1/jobs/{id}/events` | Its timeline, oldest first: `{at, event, status, worker, attempt, detail, seconds}` per line. `event` is `submitted`, a status, `requeued`, `retried`, `cancel_requested` or `stage` (a new step while preparing or collecting outputs); `seconds` is how long it lasted. The dashboard's *Timeline* button shows it |
| GET | `/api/v1/jobs/{id}/preview` | The job's preview movie (MP4); `?key=` instead of the header, for players |
| GET | `/api/v1/workers` | Online state, GPU, free VRAM, slots, preflight problems |
| GET | `/api/v1/workers/{name}/gpu-history?hours=` | A machine's GPU use and memory over the last hours (GPU history) |
| GET | `/api/v1/workflows/{name}/publish` | What it can be published with: `templates` (and `unusable` ones, with why), `steps`, the default `step` and `description`; `why` when it can't be |
| POST | `/api/v1/jobs/{id}/publish` | Publish a finished job `{step?, show?, shot?, template?, description?, dry_run?, wait?}` → the publish (`state`: `queued`, `dispatching`, `dispatched` with `tractor_id`, `dry_run`, `failed` with `error`) |
| GET | `/api/v1/jobs/{id}/publishes` | Its publishes, the latest first, with the command run and dispatchGraph's output |
| GET | `/api/v1/publish/templates` | Every scope's templates (`_global`, then each published workflow): usable or `why`, `saved_by`, `saved_at`, `versions` |
| GET | `/api/v1/publish/templates/{scope}/{name}/history` | Every version kept: `version`, `time`, `user`, `action` (save, restore, found), `current` |
| GET | `/api/v1/publish/templates/{scope}/{name}/file?version=` | The `.nk` (the one in place, or a version) |
| POST | `/api/v1/publish/templates/{scope}/{name}` | Publish a template `{text}` (the whole script), checked like publishing checks it |
| POST | `/api/v1/publish/templates/{scope}/{name}/restore` | Put `{version}` back (as the newest version) |
| GET | `/api/v1/shotgrid` | Whether the ShotGrid lookups work (`enabled`, `set_up`), or `why` not |
| GET | `/api/v1/shotgrid/projects?refresh=` | The active shows `[{code, name}]`; `refresh=true` reloads them from ShotGrid |
| GET | `/api/v1/shotgrid/projects/{show}/shots?refresh=` | A show's `shots` `[{code, sequence, status}]` and `sequences` |
| GET | `/api/v1/shotgrid/playlist?link=` | Batch Submit's rows from a Playlist (link or id): `playlist` and `rows` `[{version, input}]`, in its order |
| GET | `/api/v1/shotgrid/versions?ids=` | Batch Submit's rows from Versions (ids, comma-separated), in that order: `source` (`code`, `show`, `ids`, `missing`) and `rows` |
| GET | `/api/v1/shotgrid/query/options?show=&refresh=` | What Batch Submit's ShotGrid query offers for a show: `sequences` `[{code, shots}]`, `shots`, `filters` `[{key, field, label, kind (choice, text), values [{value, label, count}], note}]`, `left_out` `[{key, label, why}]`, or `why` not |
| POST | `/api/v1/shotgrid/query` | Batch Submit's rows from a query `{show, sequences, shots, name, filters {key: [values] or "text"}, since_days, latest}`: `source` (`code`, `show`, `query`, `matched`, `latest`, `more`, `notes`) and `rows` |
| GET | `/api/v1/shotgrid/menu` | ShotGrid's right-click menu as the farm makes it: `items` (folder, title, entity type, url, shows), `workflows` (where each goes, and why), `problems`, `sync` (the manager's own syncing of it) |
| POST | `/api/v1/shotgrid/menu/sync` | Make ShotGrid's menu match now (`shotgrid.menu_sync` on; with API keys, admins) |
| POST | `/api/v1/shotgrid/ami?action=&workflow=` | ShotGrid's right-click posts here: a redirect to the dashboard's deep link (Open); with the Submit folders on, Submit (+ Publish) on one Version submits it and redirects to the job (see *ShotGrid right-click*) |
| GET | `/api/v1/run-times` | How long each workflow takes per GPU type: runs, usual size and time, overhead, seconds per frame |
| GET | `/api/v1/policy` | The farm's policy: the file, the rules in force, what's wrong with the file if anything, what is queued against each quota, and each definition's models (Policy) |
| GET | `/api/v1/sweeps` | The retention sweeper's reports, newest first, with their totals (Retention) |
| GET | `/api/v1/sweeps/{name}?format=` | One report (`latest`, or its name such as `2026-11-23`): each expired job and the files it would delete; `format=text` to read it |
| POST | `/api/v1/sweeps` | A sweep now, as the daily one (a dry run). From the manager's host only |
| GET | `/metrics` | The farm's numbers in Prometheus' text format (Prometheus metrics) |
| GET | `/api/v1/job-metrics?since=&until=&format=&limit=` | One row per finished job, kept after it (Job metrics): `since` and `until` when it finished (a date, a date and time, or seconds since 1970); `format=csv` for a spreadsheet |
| GET | `/api/v1/usage?days=&by=&show=&workflow=` | Jobs, GPU hours and failures over the last days, per day, show, workflow or machine (Usage) |
| GET | `/api/v1/alerts` | The alert rules, the alerts that hold, the latest cleared, and whether Google Chat is set (Alerts) |
| POST | `/api/v1/evals` | `{definition, machines}`: a definition's eval on each machine, pinned to it, from the manager's host only (Evals) |
| GET | `/api/v1/evals?jobs=` | How those evals went: each one's state and what each check measured (Evals) |
| WS | `/api/v1/events` | Live job and worker updates (snapshot first) |

Interactive docs are at `/docs`. Identify yourself with an `X-User` header, or `X-API-Key`
when `auth.api_keys` is set in `config.yaml`.

Jobs in lists carry `publish`: their latest publish (or null), and `info.publish_request` when
one was asked for at submit.

Job statuses: `preparing` (EXR/movie inputs being converted) → `queued` → `dispatched`
(waiting in ComfyUI) → `running` → `postprocessing` (renaming frames, making the preview
movie, writing texture maps as EXR) → `done`, or `failed` / `cancelled`.

A job's `kind` is `job`, or for a large texture `set` (what was submitted; its status,
progress and outputs come from its batches) and `batch` (one run of a set, with
`parent_id` = the set). Lists show jobs and sets; a set's batches come with `?parent=`.

A finished texture job's `outputs.maps` has one entry per channel: its `pattern` (a
`%04d` over the UDIMs, or the file itself for one tile), `pattern_posix`, `first`/`last`
and `files`. `outputs.frame_pattern` is the base colour's.

## Publishing (Tractor, through Daiquiri)

A finished job of any video or image workflow (`ltx25_alpha_gen`, `ltx25_layout_render`,
`wan22_i2i`; not texture workflows such as CHORD, which go through the texture publisher)
can be published through the pipeline: the manager copies a Nuke
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
| Show, shot | Filled in from the input path until you type in them, and again (with the sequence) when a new input path names another show or shot (in Nuke, the session's `RDO_CURRENT_*` when the path names none); empty: the ones the input path names. With ShotGrid lookups on, both suggest from ShotGrid. The shot is Daiquiri's `--entities`. Only shows with a folder on disk (`publish.show_folder`, `/rdo/shows/<show>`) are published to |
| Sequence | With ShotGrid lookups on: narrows the shot suggestions down (not sent to Daiquiri) |
| Step | One of the studio's steps (`publish.steps`, `ai` among them); the default is the manifest's `publish.step`, else `publish.default_step` (`ai`) |
| Nuke template | The workflow's own templates, then the global ones, each named where it comes from (`LTX 2.5 alpha generation / default`, `global / default`); it starts on the workflow's `default`, else its first, else the global `default`. **↻** reloads the list after a template is published |
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
they say so and keep the last lists. Publishing never waits on ShotGrid. The same set-up lets
a job's input be a ShotGrid Version link (see *ShotGrid Version links*).

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
`/rdo/bank/comfy_farm/publishTemplates/Nuke/ltx25_alpha_gen/default.nk`, and the **global**
ones, `<publish.templates>/_global/<name>.nk`, shared by every workflow and offered after its own
(so a workflow without a template of its own publishes with the global `default`). An ordinary
Nuke script, built around nodes with set names:

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

A global template can only count on `Placeholder_Output_Comfyui`: an input Read in it makes
it unusable for workflows without that input (listed so, with why).

**Publishing a template** (anyone can; every version is kept):

- **In Nuke:** build the comp on a real ComfyFarm output (the panel's Read), save the script,
  then *ComfyFarm ▸ Save as Nuke Publish Template…*. A selected Read is renamed
  `Placeholder_Output_Comfyui` if the script has none (asked first). Pick the workflow (it
  starts on the one the Read came from) or *global*, and a name (`default` is the one
  publishing starts with). The whole saved script is published.
- *ComfyFarm ▸ Open Nuke Publish Template…* opens a copy of one to edit (in `~/.nuke/comfyfarm_templates`);
  *Save as Nuke Publish Template…* then offers to save it back to the same one.
- **In the dashboard's Nuke Publish Templates tab:** pick the workflow (or global), a name and the `.nk`.

The manager checks the script (the output Read, a WriteNukleus, input Reads the workflow
has) and writes it, so artists don't need write access to the bank. Every version put in
place is kept in `<publish.templates>/.history/<scope>/<name>/` with who saved it and when
(a file found changed on disk is kept first). The **Nuke Publish Templates** tab lists every
workflow's templates and the global ones (usable or why not, last saved by, versions); *History* shows
each version with *Download* and **Restore**, which puts it back as the newest version, so
nothing is lost.

The global templates were called *generic* at first (`_generic/`): when the manager starts it
moves `_generic/` to `_global/`, and `.history/_generic/` with it, unless `_global/` is already
there (then both are left for you to merge, and the manager's log says so). Publishes asked
for with a `_generic/` template still work.

### Turning it on

Off until it's switched on in the manager's `config.yaml` (see `config.example.yaml`):

```yaml
publish:
  enabled: true
  dry_run: true      # first: every publish is a dispatchGraph --dry-run
```

Restart the manager (`deploy/linux/farmctl.sh restart manager`), publish a finished job (a dry
run), then set `dry_run: false` and publish one for real.

dispatchGraph runs as the manager's user, but `--owner <user>` publishes as the artist who
asked for the publish (the user in the description suffix): without it Daiquiri would give
every daily to the manager's account. A `publish.command` of your own in `config.yaml` needs
`--owner {user}` too. It runs in a clean login shell with the show's context (`publish.shell`,
`publish.command`), because `ctxl` and `dispatchGraph` are aliases a non-interactive shell
doesn't have:

```bash
env -i HOME=$HOME USER=$USER LOGNAME=$USER /bin/bash -lc \
  'source /rdo/rodeo/setup/rdo_rez_launcher/current/env/set_context.sh <show> &&
   /rdo/rodeo/setup/rdo_rez_launcher/current/rdo_rez_launcher.bash -a graph -c dispatchGraph \
     update_and_render_nuke --description "…" --step lay --entities <shot> \
     --workfile <staging>/<job id>/default.nk --frameRange 1001-1084 --owner <user>'
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

### ShotGrid Version links

With ShotGrid lookups set up (see *ShotGrid lookups*), any input takes a Version's link
instead of a path, in the dashboard, the Nuke panel and the client:

```
https://rodeofx.shotgrid.autodesk.com/detail/Version/4865327
https://rodeofx.shotgrid.autodesk.com/page/9921#Version_4865327     (a page with it open)
https://rodeofx.shotgrid.autodesk.com/detail/Version/4865327 1001-1050   (some of its frames)
```

The manager asks ShotGrid for the Version (read only) and reads:

- **Its frames** (`sg_path_to_frames`, the published images), over the Version's frame range
  (`sg_first_frame`–`sg_last_frame`) unless frames follow the link. ShotGrid's lone `#` is
  read as the padding the files on disk have (`x.#.exr` with `x.0977.exr` is `x.####.exr`).
- **Else its movie** (`sg_path_to_movie`): when it has no frames, or they're no longer on
  disk (the destination line says so).
- **The show and shot come from the Version** (its Project and Shot), not from the path, and
  the sequence from the shot's name (`yht_1040` → `yht`, as in the pipeline; it's what the
  Shot's ShotGrid Sequence should be too). The output folder and the publish fields follow
  them.

The destination line and the job's details show the Version, with a link back to it. The
job keeps the link as its input, so *Run again* asks ShotGrid again. A link to another
ShotGrid site, a Version ShotGrid doesn't have, or one with neither frames nor a movie is
refused, and so is a link while ShotGrid can't be asked (the message says why; paste the
path instead). A request ShotGrid refuses (a field the site doesn't have) says so and doesn't
hold the show and shot lists back; only a ShotGrid that doesn't answer is left alone for
`retry_seconds`. The Version fields used are `shotgrid.version_fields` in `config.yaml`;
`tools/sg_probe.py <link>` (on the manager) shows what ShotGrid holds for a Version.

### Batch Submit

The dashboard's **Batch Submit** tab makes one job per row of a table, one workflow per table.
Two sources fill it, each ShotGrid Version a row with its link as the input (see *ShotGrid
Version links*):

- **Playlist:** paste a ShotGrid Playlist's link (or its id) and *Load*; its Versions come in
  the playlist's order.
- **ShotGrid query:** a show's Versions found by a few filters (below), in shot order.

*Variations* is coming.

- **Columns:** the shot, the Version (a link to it) and its frames, then the workflow's own
  fields, as in the Submit form, then, for a workflow that publishes, *Publish*, *Step*,
  *Nuke template*, *Description* and *Dry run*, then where the row writes and its state
  (which stays in view on the right).
- **Fill:** the cell under each header sets that column for every ticked row. Any cell can
  still be changed per row; a cell that differs from its column is outlined. The table and
  the fill are kept in the browser, but only what you set in the fill yourself stays: the
  other columns take the farm's defaults as they are when the page loads (the publish step
  `publish.default_step`, the template, a manifest's), and so do the rows not changed on
  their own.
- **Checks:** the manager checks every row as the Submit form's "Will write" line does, its
  publish included (a show without a folder, a shot ShotGrid doesn't have…), a few at a time,
  again after each change: *ready* (with notes, e.g. a Version read from its movie, and where
  it publishes), or why it can't run. A row that can't run starts unticked. Rows going into
  the same folder show `v002+`: they take the next versions, in the order they're sent.
- **Publishing:** each row publishes to its own Version's show and shot; step, template,
  description and dry run come from the columns (fill them for all, change them per row).
- **Submit:** *Submit N jobs* sends the ticked ready rows, one ordinary job each, one at a
  time in the table's order; each row also has its own *Submit*. It asks first when that's
  more than 20 jobs. A
  refused row says why and stays ticked; the others go on. *Stop* sends no more.
- **After sending:** a sent row is unticked and follows its job live (queued, running with its
  progress, done, its publish), its id opening it in the queue; *Submit again* sends it once
  more. The log under the table keeps what was sent and refused.
- **Kept in your browser:** the table, its sent jobs and the log survive a reload; nobody else
  sees them. ↻ loads the playlist again, keeping your edits.

Versions picked in ShotGrid come to the table too (see *ShotGrid right-click*): several
selected and right-clicked, or Version links pasted into the Playlist field (one or more).
The info line says how many, and which ShotGrid doesn't have.

A link opens it with a playlist loaded: `/#batch?playlist=<link or id>&workflow=<name>`,
or with Versions: `/#batch?versions=<id>,<id>,…`. `GET /api/v1/shotgrid/playlist?link=`
and `GET /api/v1/shotgrid/versions?ids=` give the rows
(`comfyfarm_client.shotgrid_playlist(link)`, `shotgrid_versions(ids)` from Python).

#### The ShotGrid query

Pick a show, narrow it down, and *Search* (or Enter in a text field):

- **Sequences** and **Shots:** dropdowns of the show's, as the publish fields list them (shots
  with a status the lists hide, `hld omt hdn void`, are left out). The shots offered are those
  of the sequences picked. Type in a long list to filter it, then *Tick shown*. None picked
  means all.
- **Name contains:** words in the Version's name (`mp01`); several words find any of them.
- **Status**, **Pipeline Step** and **Department**: each a dropdown of the values the show's
  Versions have, with how many, as ShotGrid labels them. Several picked find any of them. At
  Rodeo, Department `Plate` marks plates (`Reference` the references).
  - **Which fields:** `shotgrid.query.fields` in the manager's config. Any Version list, text
    or link field can be added, e.g. `task: sg_task` (a link, listed by the Task's name across
    shots, `LINEUP (AUTO)`) or `type: sg_version_type`. Task and Type are off by default: at
    Rodeo they weren't useful in a query.
  - A text or linked field ShotGrid can't list, or with more than 300 values, is typed instead
    ("contains"); a list field always stays a dropdown, since ShotGrid takes no "contains" on
    one.
  - A field the site doesn't have, or none of the show's Versions fill in, is left out (*N not
    offered ⓘ* says why).
  - On Rodeo's site (probed): Status is a status list, Pipeline Step a link to a Step,
    Department a list, Version Number a number.
- **Created:** in the last day, week, month…
- **Latest of each shot and name** (on by default): only the highest version of each Version
  name in each shot. The name is the Version's name without its version
  (`yht_1040.lineup.autoLineup.v3` → `yht_1040.lineup.autoLineup`; a movie's extension goes
  too); the highest is by `sg_version_number`, else the name's `vN`, else the newest.

Only Versions of shots (not assets) with frames or a movie are found, since a row needs one.
The newest 5000 are looked at and 500 rows given at most (`shotgrid.query.scan`,
`max_rows`): when there are more, the info line says so. Hovering the info line shows the
query the table came from; ↻ runs it again, keeping your edits. ↻ next to the filters asks
ShotGrid again for the show's sequences, shots and values (they're kept 10 minutes).

The browser never sends ShotGrid filters: it names a show and the values picked, and the
manager builds the filters from what it offered for that show, refusing anything else. It's
read only, with the manager's own ShotGrid script. `GET /api/v1/shotgrid/query/options?show=`
gives what a show offers; `POST /api/v1/shotgrid/query` gives the rows.

### ShotGrid right-click

Three ways to bring a Version or a Playlist from ShotGrid to the farm (the dashboard's
*From ShotGrid* dialog, at `/#shotgrid`, explains the first two and the menu).

**ShotGrid's right-click menu**, on the Versions and Playlists of the shows in
`deploy/shotgrid/menu.yaml`: a submission form, like the Nuke panel's, shaped by what a menu
can do.

```
right-click ▸ ComfyFarm Open ▸   every workflow with an image input
```

| Picked in ShotGrid | Opens |
|---|---|
| one Version | the **Submit** form, the workflow picked, the Version as its input |
| several Versions | **Batch Submit**, a row each |
| a Playlist | **Batch Submit**, a row per Version, in the playlist's order |

- *Publish when done* is ticked (the form's checkbox, Batch Submit's Publish column) for a
  workflow that publishes (`publish: false` in `menu.yaml` leaves it unticked). The dashboard's
  user is ShotGrid's login when none is entered yet. Nothing is sent until you submit.
- **Which workflows** is worked out from their manifests (`comfyfarm/shotgrid/menu.py`):
  those with an image input. `shotgrid: false` in a manifest leaves a workflow out,
  `shotgrid: {title: …}` names it in the menu.
- **Submit folders, off for now.** `menus: [open, submit, publish]` in `menu.yaml` adds
  *ComfyFarm Submit ▸* and *ComfyFarm Submit + Publish ▸*, for the workflows a Version alone
  can run (one required input taking frames, every other field optional: LTX 2.5 alpha
  generation; CHORD's manifest keeps it out with `shotgrid: {submit: false}`, as Versions are
  mostly plates and precomps, not UDIM tiles). One Version is then submitted at once with the
  workflow's defaults, as ShotGrid's `user_login` (else `anonymous`, not checked yet), and the
  tab shows the job (`#queue?job=<id>`); several open Batch Submit with `send=1`, which sends
  them once checked; when it can't submit, the tab says why with a link to the form. Only
  ShotGrid's own POST submits; with `auth.api_keys` set it opens the form. They're off while
  every click opens a new tab anyway: they'll come back when the manager has https and
  ShotGrid can show the result over its own page (its modal overlay).
- ShotGrid is on https and the farm on http, so Chrome warns that the form isn't secure
  (*Send anyway*), every time, until the manager has https (planned).

**The ComfyFarm button:** a bookmarklet. Drag it from the *From ShotGrid* dialog onto the
browser's bookmarks bar once; then click it on a ShotGrid page with a Version or a Playlist
open. It reads the page's address: a Version's or Playlist's own page (`/detail/Version/<id>`),
one open over a page (`#Version_<id>`), the Media Center (`type=Version&id=…`, or a playlist
in its tree). A page that only lists them (a Shots or Versions page) doesn't name one: open
it, or drag it instead. The farm opens in a tab of its own, reused by the next click: a
Version in the Submit form, a Playlist in Batch Submit.

**Drag and drop:** drag a Version's or Playlist's name (its link) from ShotGrid onto the
dashboard, anywhere but a field (a field takes it as text, as before).

#### Setting the menu up

`deploy/shotgrid/menu.yaml` says which shows have the menu (`raa: all`, or a list of
workflows for a show), `farm_url` (the manager's address as artists' browsers reach it),
`publish` (Open ticks Publish when done) and `menus` (the folders: `[open]`).
`GET /api/v1/shotgrid/menu` shows the menu the manager makes of it: the items, and why
each workflow is where it is.

ShotGrid's items (Action Menu Items) are made to match it by `tools/sg_ami.py`, run after
deploying the manager whenever a show or a workflow is added. It needs a ShotGrid connection
that may create them (not the manager's read-only script): the pipeline's own client
(`--pipeline`: `rdo_publish_pipeline`, in a rez environment that has it, or in Maya; the
file is standalone, standard library only, Python 3.7+), or an admin script. It only shows
what it would do until `--apply`:

```python
# Maya's Script Editor (Python)
import sys; sys.path.insert(0, "/mntx/raid/pdominik/comfyui-farm/tools")
import sg_ami
sg_ami.main(["--pipeline", "--farm", "http://wrk1125l.rodeofx.com:9090"])   # then with "--apply"
```

```bash
cd /mntx/raid/pdominik/comfyui-farm
rez env <the package with rdo_publish_pipeline> -- python tools/sg_ami.py --pipeline --farm http://wrk1125l.rodeofx.com:9090
# or an admin script (the key: SG_ADMIN_SCRIPT_KEY, or it asks)
export SG_ADMIN_SCRIPT_NAME=<the admin script's name>
PYTHONPATH=. $(cat run/python) tools/sg_ami.py --farm http://wrk1125l.rodeofx.com:9090
```

- It reads the farm's menu, then creates the items ShotGrid doesn't have, changes those that
  differ (title, folder, order, shows' projects), and deletes any other item posting to the
  farm (the first *Send to ComfyFarm* pair included). Run again, it changes nothing.
- It reads ActionMenuItem's fields first, and stops when the menu has problems (a show
  ShotGrid doesn't have, a workflow that isn't on the farm) or the site can't hold folders.
- `--remove --apply` deletes every item posting to the farm. `--pipeline-path <folder>`
  imports `rdo_publish_pipeline` from a folder instead of rez; with `--pipeline`, `--site`
  checks the client is on that site.
- An admin script's key is used for that run only: never written, never printed (not even
  in ShotGrid's error messages). With `--pipeline` no key passes through it at all.

#### The manager keeping it in step

With `shotgrid.menu_sync.enabled: true` in its `config.yaml`, the manager runs that sync
itself, so nobody has to after a change:

- **When:** 20 s after it starts, then every `check_minutes` (5) when the menu differs from
  the one it last synced (`menu.yaml` edited, a workflow added or changed), and every
  `every_hours` (24) anyway, which puts back an item changed by hand in ShotGrid.
  `POST /api/v1/shotgrid/menu/sync` runs it now.
- **How:** it can't make the items with its own read-only script, and the pipeline's client
  lives in the studio's rez environment, not in the farm's Python. So, as publishing runs
  dispatchGraph, it runs `shotgrid.menu_sync.command` in a clean login shell: the show's
  pipeline context, then rez by its full path (in a terminal `rez` is an alias, which a
  script's shell doesn't expand), then the tool with the menu it wrote to
  `<data>/shotgrid_menu.json` (`{tool}`, `{menu}`, `{farm}`, `{show}`: `menu_sync.show`, else
  `menu.yaml`'s first show):
  `source …/rdo_rez_launcher/current/env/set_context.sh raa && /rdo/software/rez/rez_installations/linux/rocky8/current/bin/rez/rez env rdo_publish_pipeline -- python tools/sg_ami.py --pipeline --menu-json <menu> --apply`
- **When it doesn't:** a menu with problems, without a `farm_url`, or with no items while
  it names shows (the workflows didn't load) isn't synced. A failed run is tried again at
  the next check. `GET /api/v1/shotgrid/menu`'s `sync` says how the last one went (when,
  why, created / changed / deleted, the tool's output), and the manager's log too.

To try it by hand on the manager, as the manager runs it (a clean login shell; without
`--apply` it changes nothing; it takes a few seconds to resolve):

```bash
env -i HOME=$HOME USER=$USER LOGNAME=$USER /bin/bash -lc \
  "source /rdo/rodeo/setup/rdo_rez_launcher/current/env/set_context.sh raa && /rdo/software/rez/rez_installations/linux/rocky8/current/bin/rez/rez env rdo_publish_pipeline -- python /mntx/raid/pdominik/comfyui-farm/tools/sg_ami.py --pipeline --farm http://wrk1125l.rodeofx.com:9090"
```

When that shows the menu ("… as they are"), switch it on and restart the manager. If the
studio's rez moves (another OS, another path), change `menu_sync.command` to what works.

## Adding or updating a workflow

1. In ComfyUI: **Workflow → Export (API)**. Save it as `workflows/<name>/workflow_api.json`.
   Blank out show-specific paths. The gateway fills those per job, and this keeps show
   codes out of git.
2. Write `workflows/<name>/process.yaml` (copy one of the farm's, e.g. `ltx25_alpha_gen`; see below),
   with its `task:` (see *Menus: providers and tasks*). A `manifest.yaml` still loads.
3. `python -m comfyfarm check`, then restart the gateway. The check reads both halves of the
   definition. The manager itself checks only the head; each agent checks the wiring of its
   copy and tells the manager, which disables a workflow any agent finds broken.

Paths left in the export from your last ComfyUI session are harmless: the gateway
overwrites the input, output and save-path nodes on every job.

Re-exporting can renumber nodes. Because every manifest target names its node class,
a renumbered workflow fails the check with a clear message. It can't silently write
a value into the wrong node.

### process.yaml

The farm's workflows are written as `process.yaml`; the older `manifest.yaml` still loads
(never both in one folder). A process.yaml says the same things in two halves: a head any kind of job shares, with what
the workflow takes and writes, and a `runner:` block with which ComfyUI node does each
thing. `docs/decisions/0015-process-yaml.md` and `0019-the-head-says-what-a-definition-writes.md`
have the why.

```yaml
id: wan22_i2i
label: WAN 2.2 image to image
task: image                   # in the menus: Provider > Task > Workflow
requires: {vram_gb: 24}
class: interactive            # or batch
targets: [pool]               # and/or tractor
limits: {max_minutes: 30}
models:
  - {file: wan2.2_i2v_low_noise_14B.safetensors, licence: apache-2.0}
params:
  input: {type: input, takes: image, label: Image, required: true}
  prompt: {type: string, label: Prompt}        # type, label, default, min/max, choices
writes:
  frames: {ext: png}          # it writes a frame sequence of this type
  preview: true               # the farm makes a preview movie (false: none; own: the graph makes one)
runner:
  type: comfyui
  template: workflow_api.json
  params:
    input:
      loader: {node: "254", class: ComfyFarmLoadImage}
    prompt:                                    # the nodes it drives
      targets: [{node: "6", input: text, class: CLIPTextEncode}]
  outputs:
    frames: {node: "256", class: ComfyFarmSaveImage, path_input: output_path, prefix_input: filename_prefix}
```

Other kinds of output take the generic route (decision 0036): `writes.file: {ext: txt}`, with
the node that writes the files in `runner.outputs.file: {node: "12", prefix_input:
filename_prefix}`. The node gets the job's prefix, and the agent fetches the files ComfyUI
lists for it and names them after the job, as for a process (see *Process runner*).

A texture workflow says `writes.textures` instead of `writes.frames`: `name`, `dir`, `ext`,
`batch`, and `maps` with each map's `channel`, `name`, `colorspace`, `color` and `bits`.
Its `runner.textures` has `path_input`, `prefix_input` and `maps` keyed by channel, each
with the `node` and `class` that saves it. An input whose loader reads every file in its
folder (VHS Load Images Path) says `own_folder: true` in the head (in a manifest, on the
input param).

Each half needs the other (frames in `writes` and their node in `runner.outputs`), and a
key in the wrong half is refused with a message saying where it goes.

`class`, `targets`, `limits`, `models`, `eval` and `version` are optional. They are checked
when the workflow loads. `models` (every weights file the workflow loads, with its licence)
is what the farm's policy checks licences against (see *Policy*; `models: []` for none).
`eval` is the definition's reference job (see *Evals*). The others are not used yet.
`python tools/manifest_to_process.py workflows/<name>` prints a manifest as a process.yaml
(`--write` saves it; comments are not carried over, and you remove the manifest yourself). A
converted workflow keeps its fingerprint until a head field is added, so machines on the old
and the new file still agree.

#### Menus: providers and tasks

Every menu reads Provider > Task > Workflow, for example ComfyUI Local > Masking > SAM3
(decision 0054). Four optional fields place a definition there:

```yaml
task: masking                 # a task in workflows/tasks.yaml
provider: comfyui-local       # only when it isn't its runner type's usual one
family: sam3-matte            # the same model on several providers (their prices are compared)
hidden: true                  # kept out of artists' menus (gpu_test); the dashboard and CLI still run it
```

- **Providers** are files in `providers/`, one each, named like the provider:
  - `label` (its name in the menus), `runner` (the runner type its definitions have) and
    `target` (where its jobs go);
  - `offsite` (its jobs send material outside Rodeo) and `order` (smallest first);
  - an optional `description`.

  Today there are `comfyui-local` (every `runner.type: comfyui` definition's),
  `python-local` (every `runner.type: process` one's) and `fal` (fal.ai, below).
- **Tasks** are `workflows/tasks.yaml`, in menu order: Masking, Video, Image, Textures and
  Tests.
- **Checks.** An unknown task or provider, or a provider whose runner type isn't the
  definition's, turns the definition off with the reason. A definition with no task still
  loads.
- **Not in the fingerprint.** These fields don't change what runs, so moving a workflow to
  another task never makes a machine refuse a job.
- **`python -m comfyfarm check`** checks the providers' and tasks' files and says where
  each workflow shows ("in the menus under ComfyUI Local > Masking").
- **`GET /api/v1/catalog?show=bri3`** is the tree every client draws its menus from:
  providers in their order, their tasks in tasks.yaml's order, workflows by label.
  - A provider or task with nothing in it isn't listed. A workflow with no task is under
    Other.
  - A workflow the show's policy refuses stays listed, with `allowed: false`, the reason
    and the rule, so a menu can say why.
  - Hidden ones are left out unless `hidden=true` (the dashboard).
  - `python comfyfarm_client.py catalog show=bri3` prints it the way the menus read.

#### API definitions and the gateway

A job can also run at an outside provider, fal.ai first, through the gateway: an agent on
the manager's machine with lanes instead of GPUs (decision 0055). An API definition is a
process.yaml with `runner: {type: api}`:

```yaml
id: nano_banana2_edit_fal
task: image
provider: fal                 # an API definition names its provider
models: [{file: fal-ai/nano-banana-2/edit, licence: google-via-fal}]   # the model it uses there
cost: {unit: image, price: 0.08, by: {param: resolution, factors: {"1K": 1, "2K": 1.5}}}
params:
  input: {type: input, takes: image, required: true, primary: true}
  prompt: {type: text, required: true}
  output_path: {type: output_folder}
writes: {frames: {ext: png}}
runner:
  type: api
  endpoint: fal-ai/nano-banana-2/edit
  upload: {input: image_urls}   # inputs: uploaded, their URLs sent in this field
  send: {prompt: prompt}        # other params: sent as this field
  fixed: {num_images: 1}        # sent as they are; no artist sets them
```

- **Its jobs go to the gateway only** (`targets: [gateway]`, the default for an API
  definition). They take a lane of their provider and never wait behind GPU jobs; a
  gateway takes nothing else.
- **An API provider's file** also has `adapter` (the gateway's code that speaks to it),
  `lanes` (its jobs at once on a gateway) and `keys` (the names of the keys the gateway's
  file holds, never the keys). `providers/fal.yaml`: fal.ai, off-site, 2 lanes.
- **Off until switched on.** An outside provider's workflows show in the menus and can be
  submitted only when the manager's config.yaml lists it:

  ```yaml
  providers:
    enabled: [fal]
  ```

  Until then the menus leave them out and a submit is refused, saying so. The farm's own
  GPUs (ComfyUI Local, Python Local) are always on.
- **Then only for the shows the policy allows** (*Policy*, *Outside providers*): none
  until a rule in `policy.yaml` allows it, and none with policy off.
- **What is sent.** The fixed fields, each `send` param under its field, and each `upload`
  field: the URLs of its inputs, in the params' order. fal.ai names a field that takes
  several URLs `..._urls` (a list is sent) and one that takes one `..._url`. An empty param
  or input isn't sent, so the provider uses its default. `workflows/nano_banana2_edit_fal`
  is the first: the input and an optional reference, prompt, resolution and aspect ratio;
  one PNG, and no web or image search, fixed.
- **What it costs** (decision 0059). An API definition has a `cost` block, from its
  provider's page for the model, reviewed like the rest of the definition:
  - `unit`: the provider's billing unit (image);
  - `price`: one unit's price in US dollars when it was reviewed;
  - `quantity`: how many units a job is, a number or `{param: <a number param>}` (a
    video's seconds), 1 if left out;
  - `by`: a choice param and a factor for each of its choices (Nano Banana 2: 0.75× at
    0.5K, 1.5× at 2K, 2× at 4K).

  The gateway reads each provider's prices every `gateway.prices_hours` (a day), from its
  pricing API, and sends them with what it can run. When a job is planned, the manager
  estimates it from that day's price, or from the reviewed one until the gateway has read
  it. The estimate shows in the preview as `Cost` and is kept with the job
  (`info.cost`).
  - A price that moved more than 10% from the reviewed one is used, and noted.
  - A price in another unit or another currency than US dollars is refused, never
    converted: the definition needs reviewing.
  - The price isn't in the definition's fingerprint, so a review needs no gateway update.
- **Its model, listed.** An off-site definition lists the model it uses there and its
  licence under `models:`, so licence rules see it.

#### The gateway

The gateway is an agent with `gateway.enabled` in its config (`gateway.example.yaml`),
run beside the GPU agent on the manager's machine (decisions 0056, 0057). It has the API
runner alone: no ComfyUI and no GPU, and it says its lanes when it checks in. The
Machines tab shows its lanes where a GPU machine shows its GPU (`gateway · fal ×2`).
Rollouts, ComfyUI syncs and the Runner environments panel leave it out.

On the manager's machine, from the checkout:

```bash
cp gateway.example.yaml gateway.yaml            # then edit: manager; paths and convert.tools as in agent.yaml
mkdir -p ~/.comfyfarm
(umask 077 && read -rs KEY && printf '%s\n' "$KEY" > ~/.comfyfarm/fal_requests.key)   # paste the key, then Enter
deploy/linux/farmctl.sh start mock_fal          # fal.ai's stand-in, until step 5
deploy/linux/farmctl.sh check gateway           # its providers, keys and definitions; never a key
deploy/linux/farmctl.sh start gateway           # or install_cron.sh gateway: kept running
```

`read -rs` keeps the key off the screen and out of the shell's history, and the umask makes
the file readable by this account alone. For the stand-in, any made-up word is the key.
`farmctl.sh stop|restart|status|log gateway` (and `mock_fal`) work as for the manager. The
gateway reads its keys when it starts: restart it after changing them.

- **Keys.** Each in a file of its own, readable by its account alone (a file others can
  read is refused), named in `gateway.yaml` under `providers.<name>.keys`; `~` is the
  account's home. The file holds the key alone, as plain text: a byte-order mark some
  editors put first is dropped, and any other character a key can't have is refused,
  saying so. The gateway reads them when it starts and sends them to the provider only:
  they are in no log, job, event, run folder or output (`tests/test_api_runner.py` and
  `tests/test_e2e_gateway.py` check). git ignores `gateway.yaml` and `*.key`.
- **Each job:** its inputs are converted as for any job (an EXR to PNG), uploaded, and the
  job is sent once. What is sent is in the run folder (`gateway.runs_dir/<attempt>`) before
  it goes, and the request's id as soon as fal.ai gives it. Its status is asked every
  `poll_seconds`; what it made is downloaded into the output folder at once and renamed
  like any job's frames. Its time limit is `limits.max_minutes` (or
  `gateway.default_minutes`), the provider's queue included: then it is cancelled there.
- **Never paid for twice.** A job is sent again only when it certainly never left (no
  connection, or the provider said it was too busy). If the gateway lost the answer, or
  stopped while sending, the job fails, saying to look in the provider's request history.
  After a restart the gateway follows its jobs again from their run folders.
- **What fal.ai keeps.** Uploads and results are kept `media_seconds` (an hour unless
  `gateway.yaml` says), asked for with each upload and job.
- **Nothing reaches fal.ai itself yet.** Until phase 11's step 5 (after the ledger and
  caps) the gateway sends jobs only to a stand-in on its own machine,
  `tools/mock_fal.py`, which takes no money and sends nothing out:

  ```bash
  deploy/linux/farmctl.sh start mock_fal        # python tools/mock_fal.py --gateway gateway.yaml
  ```

  It answers where `gateway.yaml` sends fal.ai's jobs (`providers.fal.url`, e.g.
  `http://127.0.0.1:8189`) and takes the key in its `keys.requests` file. Any other
  address is refused (`REAL_SERVICE` in `comfyfarm/runners/api/fal.py`). In the
  prompt, `MOCK_FAIL` makes its model fail, `MOCK_POLICY` its content check refuse it and
  `MOCK_SLOW` it run a minute.

Everything below about manifests holds for both. In a process.yaml the per-parameter
`targets`, `loader`, `path_node`, `empty` and `given`, and the sections `template`,
`node_packs` and `sequence`, sit under `runner:`. What a manifest's `outputs`, `textures`
and top-level `preview` say is split between `writes:` and `runner:` as above.

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
# publish: {step: comp}        # optional: the step publishing offers first, else publish.default_step (ai);
                               # publish: false keeps a video/image workflow from being published
# preview: false               # optional: no preview movie of the frames (see Preview movies)
# shotgrid: {submit: false}    # optional: ShotGrid's right-click menu (see ShotGrid right-click): false keeps
                               # it out of the menu, submit: false out of the Submit folders, title: its name there
# node_packs: [RES4LYF]        # optional: node packs it needs that none of its nodes shows (custom_nodes
                               # folder names), e.g. one adding samplers to KSampler; see The farm's ComfyUI

outputs:                       # nodes the gateway points at each job's versioned folder
  root_node: {node: "9093", input: value, class: PrimitiveString}
  frames: {node: "9109", class: "Image Save", path_input: output_path, prefix_input: filename_prefix, ext: png}
  # optional: the workflow's own video node, copied instead of the farm's preview movie
  preview: {node: "...", class: VHS_VideoCombine, prefix_input: filename_prefix}
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
    outputs as VHS: IMAGE, MASK (from alpha), frame_count; then frame_numbers
    (`1001,1002,1011`, the frames read), for nodes that take a frame number.
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
  See `workflows/chord_pbr/process.yaml` (there, the maps' nodes are under `runner.textures`).
- Removing a param from the manifest changes the API: clients still sending it get
  "Unknown parameter". The Nuke panel and dashboard read the parameters from the farm,
  so they follow on their own; only scripts that send the old name need changing.

## Operations notes

When something goes wrong (an agent or the manager down, jobs failing or waiting, a machine
that can't run a workflow), follow `docs/runbook.md`: it starts from what you see.

- **Logs:** `logs/comfyfarm.log` (rotating), plus `logs/*-service.log` for the services. Each
  agent on another machine logs to `logs/agent-<name>.log` there, and ComfyUI, when its agent
  starts it, to `logs/comfyui-<name>.log`. `farmctl.sh` keeps console output in
  `logs/manager.out` and `logs/agent.out`.
- **Free VRAM:** *Free VRAM* on the dashboard's Machines tab asks that machine's ComfyUI to
  unload its models and empty its cache, when the farm has no job on it (drain it first) and
  ComfyUI has no prompt queued. The memory cell's tooltip then says what ComfyUI held
  before and after, or why it wasn't freed. The next job there loads its models again.
- **GPU probe:** set `gpu_probe: {enabled: true}` in an agent's `agent.yaml` to have it read
  its GPUs with `nvidia-smi` (it comes with the NVIDIA driver) every 30 s. The Machines tab
  then shows each card's memory in use by any program, not only ComfyUI, with its driver
  and how busy it is in the tooltip. `python -m comfyfarm agent --check` lists the cards it
  finds either way. Jobs are still placed by what ComfyUI reports; the process runner will
  place by the probe.
- **JSON logs:** set `log_json: true` in `config.yaml` (and in an agent's `agent.yaml`) to also get
  `logs/comfyfarm.jsonl` and `logs/agent-<name>.jsonl`: the same lines as the text log, one JSON
  object each, for a log collector. Fields: `ts` (UTC), `level`, `logger`, `role` (`manager` or
  `agent`), `host`, `agent` (its name, on an agent), `msg`, and, when the line is about one, `job`
  (the 8-character id) and `worker`; `error` holds a traceback. Off by default.
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

`CLAUDE.md` holds the working rules (layering, tests, how a change is made), `docs/plan.md` the
build plan and `docs/decisions/` the reasons behind it. Every pull request runs the full suite on
GitHub Actions (`.github/workflows/tests.yml`): Linux must pass; Windows is reported but does not
block yet.

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
