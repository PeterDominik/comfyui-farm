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
- **Preflight checks** catch a missing node pack, a node-pack update that renamed an
  input, or a missing model before a job is sent (`python -m comfyfarm check`).
- **Cancel, retry, and the exact API JSON** of any job, for loading back into ComfyUI to debug.

## Repo layout

| Path | What |
|---|---|
| `comfyfarm/` | The gateway: API, dispatcher/monitor, job DB, workflow builder, dashboard |
| `workflows/<name>/` | `workflow_api.json` (ComfyUI API export) + `manifest.yaml` (what's exposed): `ltx25_alpha_gen`, `chord_pbr` |
| `client/comfyfarm_client.py` | Stdlib-only client and CLI (Nuke, Houdini, terminal) |
| `client/nuke/` | Nuke panel: the dashboard's form and job list inside Nuke |
| `scripts/` | Windows setup, services (NSSM), firewall, update |
| `tools/mock_comfyui.py` | Fake ComfyUI for testing without a GPU |
| `tests/` | Unit tests + end-to-end tests against the mock |

## Install on the Windows farm machine

Needs **Git for Windows**, **Python 3.10+** (python.org, tick *Add to PATH*), and for the
services, **[NSSM](https://nssm.cc)**. The tools for EXR and movie inputs come with the
Python requirements, so `setup.ps1` installs them into `.venv`: `oiiotool` (OpenImageIO,
with OpenColorIO) and `ffmpeg` (imageio-ffmpeg). ComfyUI must have the custom node packs
the workflows use: VideoHelperSuite, WAS Node Suite, Easy-Use and ComfyUI-LTXVideo, plus
[ComfyUI-Chord](https://github.com/ubisoft/ComfyUI-Chord) with `chord_v1.safetensors` in
`ComfyUI/models/checkpoints` for `chord_pbr`. CHORD's weights are under Ubisoft's
research-only licence. The farm's own nodes (`comfyui_nodes/`, e.g. *ComfyFarm Save
Image*) come with this repo; `setup.ps1 -ComfyUIDir` links them into ComfyUI.

```powershell
git clone https://github.com/<org>/comfyui-farm.git C:\comfyfarm
cd C:\comfyfarm
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -ComfyUIDir "C:\ComfyUI_windows_portable"
```

`setup.ps1` does the following:
- creates `.venv` and installs the requirements;
- copies `config.example.yaml` to `config.yaml` (git-ignored, so `git pull` never
  overwrites it);
- links `comfyui_nodes\comfyfarm_nodes` into ComfyUI's `custom_nodes` as a junction, so
  `git pull` updates the nodes too (restart ComfyUI after a change);
- runs the preflight check. Review `config.yaml`: path mappings, output layout, workers, optional API keys.

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

**Nuke:** see [Nuke panel](#nuke-panel) below.

### Nuke panel

The ComfyFarm panel is the dashboard inside Nuke. The form is built from the farm's
workflow list, so it has the same fields as the web page and picks up manifest changes
without a client update. It shows the same live destination line, and it has a job list.
Nuke stays usable while jobs run, and a Read node for each finished job is created next to
its source.

**Install once (pipeline):**

1. Put the repo's `client/` folder (`comfyfarm_client.py` plus `nuke/`) somewhere every
   workstation can read, e.g. a pipeline tools share. A git checkout works too.
2. Add `<that>/client/nuke` to `NUKE_PATH`. Its `init.py` makes `../comfyfarm_client.py`
   importable, and its `menu.py` adds the *ComfyFarm* menu and the panel.
3. The farm address comes from `client/nuke/init.py` (`http://wrk59467w:8000`), so
   artists don't need to set anything. If the farm moves, change it there. A
   `COMFYFARM_URL` already set in someone's environment wins. An address typed into the
   panel's *Farm* field applies until Nuke closes.

The panel works in Nuke 13 to 15 (PySide2) and Nuke 16+ (PySide6), and needs nothing
installed: the client is standard library only. `NUKE_PATH` is read when Nuke starts, so
artists restart Nuke after step 2.

**Artist workflow:**

1. Prerender the plate to a PNG sequence with a Write node (`…/name.%04d.png`), with the
   colour baked in the way the workflow expects. The farm reads the PNGs exactly as they are.
2. Select the Read (or the Write that rendered it) and run *ComfyFarm → Submit selected
   Read…*. You can also open the panel (*ComfyFarm → Open panel*, or *Windows → Custom →
   ComfyFarm*) and click *From selected*. Either way the input field gets `path first-last`
   from the node. *Browse…* or pasting a path works too.
3. Check the line under the form. It shows the frame count, the exact output folder, and
   the first and last file names, or the problem (missing frames, no shot found in the
   path, and so on) before anything is queued.
4. Click *Submit*. *Submit selected nodes* queues one job per selected Read/Write, with the
   same settings.
5. Follow the job in the list. It shows everyone's jobs, like the dashboard; tick *Only mine*
   or *Only active* to narrow it. When a job finishes, a Read
   appears next to the source node (turn this off with the checkbox). Right-click a job to
   create its Read, copy the output path, reuse its input, cancel it, run it again, or see
   the error. Double-click a finished job to create its Read.

Movies: a Read of a `movhistory` `.mov` sends the whole movie. The farm numbers its frames
from the timecode and drops the slate, as it does from the dashboard.

Outside Nuke, `python client/nuke/comfyfarm_panel.py` opens the same panel as a window,
if PySide6 or PySide2 is installed.

### Parameters for `ltx25_alpha_gen`

| Param | Default | Notes |
|---|---|---|
| `input` | required | Sequence path with `%04d` / `%d` / `####` and optional `first-last` range (png/jpg/tif/webp/exr), or a `.mov`/`.mp4` with an optional range in timecode frame numbers |
| `output_path` | show default | Root folder: frames go to `<this>/<shot>/<source>.alpha_vNNN.<frame>.png`, each run taking the next free version. Its parent must exist; never inside `.published`. Empty = the show's renders folder. Inputs outside the publish layout (no shot) go straight into it |
| `seed` | 42 | |
| `enhance_prompt` | false | Gemma prompt enhancer |
| `shorter_side` | 1088 | Working resolution; multiple of 32 |
| `fps` | 24 | |

Optional job fields: `name`, `priority` (−10…10; above 0 is admin only when keys are on),
and `show` / `shot` overrides for inputs outside the publish layout.

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
| POST | `/api/v1/jobs` | Submit `{workflow, params, name?, priority?, show?, shot?}` → job |
| POST | `/api/v1/plan` | Same body as submit; returns the destination folder and first/last file names without queuing |
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

Job statuses: `preparing` (EXR/movie inputs being converted) → `queued` → `dispatched`
(waiting in ComfyUI) → `running` → `postprocessing` (renaming frames, copying preview,
writing texture maps as EXR) → `done`, or `failed` / `cancelled`.

A finished texture job's `outputs.maps` has one entry per channel: its `pattern` (a
`%04d` over the UDIMs, or the file itself for one tile), `pattern_posix`, `first`/`last`
and `files`. `outputs.frame_pattern` is the base colour's.

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

### OCIO

The farm does two colour conversions, with the small OCIO config in this repo's `ocio/`
folder. `git pull` (or `scripts\update.ps1`) updates it on the farm machine. They are a
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

# work_resolution: 1024        # optional: inputs larger than this (px, longer side) are
                               # scaled down to it before the workflow, aspect kept

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
  directory/skip/cap. An output folder is checked (parent exists, on show storage,
  outside `.published`) and used as the root for `paths.output_dir` (`{root}/{shot}`);
  the version is the first one with no files there and no queued job claiming it.
  `POST /api/v1/plan` shows the result without queuing.
- **Nodes in `sequence` and `outputs` belong to the gateway.** A plain param targeting
  them would be overwritten on every job, which is why the output folder is its own type
  rather than a param pointed at `OUTPUT_PATH`.
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
  See `workflows/chord_pbr/manifest.yaml`.
- Removing a param from the manifest changes the API: clients still sending it get
  "Unknown parameter". The Nuke panel and dashboard read the parameters from the farm,
  so they follow on their own; only scripts that send the old name need changing.

## Operations notes

- **Logs:** `logs/comfyfarm.log` (rotating), plus `logs/*-service.log` for the services.
- **State:** `data/comfyfarm.db` (SQLite). Back it up or delete it; nothing else depends on it.
- **Restarts:**
  - Gateway restarts mid-job: on reconnect it reads ComfyUI's history and finishes the job.
  - ComfyUI crashes mid-job: the job is requeued once (`farm.max_attempts`), with a warning on the job.
  - Jobs on a machine that stays unreachable for 5 minutes are requeued.
- **Pin WAS Node Suite.** `ltx25_alpha_gen` saves frames with WAS *Image Save* using its
  `output_path` input (`chord_pbr` uses *ComfyFarm Save Image* instead). The current WAS release replaced that input with a sandboxed
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

`tests/test_nuke.py` covers the Nuke panel's logic and its Nuke helpers (with a stand-in
`nuke` module), and the end-to-end suite drives the panel's logic against the gateway. The
Qt widgets need a real Nuke (or PySide) to try.
