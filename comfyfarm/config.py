"""Gateway configuration, loaded from config.yaml (see config.example.yaml)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class WorkerConfig(BaseModel):
    name: str
    url: str = "http://127.0.0.1:8188"
    # Jobs handed to this ComfyUI at once. 2 = one running plus one waiting, so the
    # GPU never idles between jobs. Keep it low: anything queued inside ComfyUI can
    # no longer be reprioritised by the gateway.
    max_inflight: int = 2
    enabled: bool = True


class PathMapping(BaseModel):
    """One storage area, as Linux workstations see it (`posix`, the studio path) and as this
    machine sees it (`windows`, also accepted as `local`): a UNC path or drive on Windows, or
    another mount point on Linux. Left out, this machine sees the studio path as it is."""
    posix: str
    windows: str = ""

    @model_validator(mode="before")
    @classmethod
    def _local_alias(cls, data):
        if isinstance(data, dict) and "local" in data:
            data = dict(data)
            local = data.pop("local")
            data.setdefault("windows", local)
        return data

    @model_validator(mode="after")
    def _same_path(self):
        if not self.windows:
            self.windows = self.posix
        return self


class PathsConfig(BaseModel):
    mappings: list[PathMapping] = Field(default_factory=list)
    # Where show / seq / shot sit in an input path. Each {name} matches one folder, so
    # shot naming doesn't matter (bpash_1545, 15454_656551, ...). Tried in order; later
    # patterns only fill in what earlier ones didn't find.
    context_patterns: list[str] = Field(default_factory=lambda: [
        "/shows/{show}/.published/{seq}/{shot}/",
        "/shows/{show}/{seq}/{shot_short}/movhistory/",
        "/shows/{show}/_asset/{asset}/",
        "/shows/{show}/",
    ])
    # Full shot name when a pattern only has the short one (shot work areas are
    # /shows/raa/bpash/1545/..., the shot is bpash_1545).
    shot_name: str = "{seq}_{shot_short}"
    # Where frames go: output_dir inside output_root. The artist's "Output folder"
    # replaces output_root. Each job takes the first {version} not already used in
    # that folder (by files, or by a queued job), so runs never overwrite each other.
    # Tokens: {root} {show} {seq} {shot} {tag} {version}
    output_root: str = "//server/share/shows/{show}/comfyui/renders"
    output_dir: str = "{root}/{shot}"
    # File names. {source} = the input file name without frame number and extension,
    # {folder} = the output folder's name.
    # Tokens: {source} {folder} {show} {seq} {shot} {tag} {version} {frame} {ext}
    frame_name: str = "{source}.{tag}_v{version:03d}.{frame:04d}.{ext}"
    preview_name: str = "{source}.{tag}_v{version:03d}_preview.mp4"
    # Folder names nothing may ever be written into.
    no_write_dirs: list[str] = Field(default_factory=lambda: [".published"])
    # Folders an artist may pick as the output folder (workflow param of type
    # output_folder). Empty = anywhere on the mapped storage above. Use either the
    # Linux (/rdo/shows/...) or the farm (UNC) form.
    allowed_output_roots: list[str] = Field(default_factory=list)
    # Check input sequences exist (and work out frame ranges) when a job is submitted.
    # Turn off only if the gateway runs somewhere that cannot see the show storage.
    validate_inputs: bool = True


class OcioConfig(BaseModel):
    """The farm's OCIO config, kept in the repo (ocio/). It does two conversions, a Nuke
    Write and Read set to "default (color_picking)" in Rodeo's published show config:

      EXR inputs (plates and textures) -> 8-bit PNG for ComfyUI:  scene_linear -> color_picking
      texture colour maps from ComfyUI -> EXR:                  color_picking -> scene_linear

    Data maps (normal, bump, ...) are only tagged `data`. The names are the config's roles.
    """
    config: str = "ocio/config.ocio"   # relative paths are inside the repo


class ExrConvertConfig(BaseModel):
    # Plain RGB, cropped to the display window (drops overscan), before the transform.
    extra_args: list[str] = Field(default_factory=lambda: ["--ch", "R,G,B", "--croptofull"])
    # zlib level of the converted PNG frames: 0 = off (default), 1 = fast ... 9 = smallest
    # (6 is OpenImageIO's own default). They're scratch files ComfyUI reads once, and
    # level 6 took a 4K frame 8.3 s against 1.9 s at 0 on the farm machine. It only changes
    # their size on disk: ComfyUI decodes every frame to the same full-size image either way.
    png_compression: int = Field(0, ge=0, le=9)


class SlateConfig(BaseModel):
    # Movies whose first frame is a slate, not plate: that frame is dropped. Applies to
    # files with one of these extensions inside a folder with one of these names.
    folders: list[str] = Field(default_factory=lambda: ["movhistory"])
    extensions: list[str] = Field(default_factory=lambda: [".mov"])


class MovieConvertConfig(BaseModel):
    # Frame numbers come from the movie's timecode. Without one, the first frame that's
    # used (after a slate, if any) gets this number.
    default_start: int = 1001
    slate: SlateConfig = Field(default_factory=SlateConfig)


class ConvertConfig(BaseModel):
    """EXR sequences and movies are converted to PNG frames on the worker that runs the
    job, before the workflow runs, so one ComfyUI graph handles every input format."""
    scratch_dir: str = "data/scratch"
    max_parallel_jobs: int = 1      # conversions at once (CPU/disk); they overlap GPU work
    frame_workers: int = 6          # oiiotool processes per EXR job
    keep_failed: bool = False       # keep converted frames of failed jobs for debugging
    # Empty = look next to the gateway's Python (the venv), then on PATH.
    # A path, or a command as a list (e.g. [python, script.py]).
    tools: dict[str, str | list[str]] = Field(default_factory=lambda: {"oiiotool": "", "ffmpeg": "", "ffprobe": ""})
    exr: ExrConvertConfig = Field(default_factory=ExrConvertConfig)
    movie: MovieConvertConfig = Field(default_factory=MovieConvertConfig)


class TexturesConfig(BaseModel):
    """Texture workflows (e.g. CHORD) write their maps as EXRs, with the colour handling
    of `ocio:`."""
    workers: int = 6     # oiiotool processes per job when writing the EXR maps


class AuthConfig(BaseModel):
    # api key -> user name. Empty = no keys required; users identify themselves.
    api_keys: dict[str, str] = Field(default_factory=dict)
    # Admins can cancel anyone's job and submit with priority above 0.
    admins: list[str] = Field(default_factory=list)


class AgentsConfig(BaseModel):
    """Agents on other machines (`comfyfarm agent`) check in over HTTP."""
    # Shared secret agents send (their agent.yaml `token`). Empty = any agent may check in.
    token: str = ""
    # A job goes to the free GPU that ran its workflow fastest (run times per GPU type).
    # Machines whose GPU hasn't run the workflow yet come after those, in this order (agent
    # names, best first), then the biggest GPU (VRAM) first. Empty = those by VRAM.
    order: list[str] = []


class RolloutsConfig(BaseModel):
    """Rollouts of runner environments (manager/rollouts.py, decision 0031), and the ComfyUI
    syncs the manager starts (manager/syncs.py, decision 0033)."""
    # The machines in this order first: the canary first. The pilot's: the A6000, the 5090,
    # then the manager's host last. Machines not listed follow, by name.
    order: list[str] = []
    step_minutes: float = Field(30.0, gt=0)     # a machine that hasn't the release by then stops the rollout
    # A machine told to sync that hasn't finished by then (ComfyUI stopped, updated, started
    # again and checked) stops the sync. Waiting for its jobs to end, before, doesn't count.
    sync_minutes: float = Field(60.0, gt=0)


class EvalsConfig(BaseModel):
    """Evals (manager/evals.py, decision 0053): a definition's reference job (its eval:), run on
    one machine. `comfyfarm eval` runs them whenever asked. With `gate`, a rollout runs the
    evals of the definitions in its environment on each machine before going on, and a
    ComfyUI sync those of every ComfyUI definition: a failed eval stops it there."""
    gate: bool = False
    minutes: float = Field(30.0, gt=0)          # a machine's evals not over by then stop the rollout or sync


class GpuHistoryConfig(BaseModel):
    """Each machine's GPU use and memory, kept by the manager for the dashboard (decision 0049):
    sampled from what its agent last reported, every sample_seconds, kept `days`. 0 days:
    none kept."""
    sample_seconds: float = Field(60.0, ge=10)
    days: float = Field(7.0, ge=0)


class AlertsConfig(BaseModel):
    """Alerts (manager/alerts.py, decision 0052): checked every check_seconds, each raised once
    when it starts and cleared when it ends, in the log and on the dashboard's Alerts tab,
    and posted to a Google Chat space when google_chat names its webhook. Off until enabled.
    Each rule is off at 0 (or false)."""
    enabled: bool = False
    check_seconds: float = Field(60.0, ge=10)
    machine_silent_minutes: float = Field(10.0, ge=0)   # a machine not drained that hasn't checked in this long
    job_waiting_minutes: float = Field(60.0, ge=0)      # a job queued this long without a machine
    failures_in_a_row: int = Field(3, ge=0)             # a machine's last this many jobs all failed
    sweep_problems: bool = True                         # the retention sweeper's latest report lists problems
    rollout_problems: bool = True                       # a rollout or a sync stopped, waiting for a person
    budgets: bool = True                                # a show past its budget's warn_at, or at its cap (0060)
    bills: bool = True                                  # a bill that couldn't be read; a low balance (0061)
    # A Google Chat space's incoming webhook (Apps & integrations > Webhooks in the space):
    # https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=... It lets anyone
    # post to the space, so it stays in this file, which is never committed.
    google_chat: str = ""
    keep_days: float = Field(30.0, gt=0)                # cleared alerts kept, for the Alerts tab

    @field_validator("google_chat")
    @classmethod
    def _chat(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not v.startswith("https://chat.googleapis.com/"):
            raise ValueError("alerts.google_chat is a Google Chat webhook, "
                             "https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=...")
        return v


class FarmConfig(BaseModel):
    max_attempts: int = 2            # resubmits when a worker loses a job (crash/restart)
    reconcile_seconds: float = 20.0  # cross-check job state against ComfyUI this often
    health_seconds: float = 5.0
    # An agent that hasn't checked in for this long has its jobs queued again (its lease);
    # an agent whose ComfyUI is unreachable this long gives its jobs back.
    lost_after_seconds: float = 300.0
    delete_comfy_history: bool = True  # drop finished prompts from ComfyUI's in-memory history
    # Fail jobs straight away when the worker is missing a node pack, an input the
    # workflow uses, or a model file (see `python -m comfyfarm check`).
    strict_preflight: bool = True


class ProvidersConfig(BaseModel):
    """Which providers beyond the farm's own GPUs are switched on (decision 0055): their
    definitions show in the menus and can be planned. The farm's own (target pool) always
    are. A provider is a file in providers/ (catalog.py); which shows may use one is the
    policy's (step 3 of phase 11)."""
    enabled: list[str] = Field(default_factory=list)     # e.g. [fal]


class ShareConfig(BaseModel):
    """A folder every artist and agent can read. At startup the manager writes its address
    there (manager.json) and copies the repo's client/ into it, so artists' Nuke
    (<dir>/client/nuke on NUKE_PATH) and the agents find the manager without a machine name
    in any config, and the client always matches the manager."""
    dir: str = ""        # e.g. /rdo/bank/comfy_farm; empty = no shared folder
    url: str = ""        # the address written there; empty = http://<this machine's name>:<server.port>


# The studio's steps, as Daiquiri takes them (--step).
PUBLISH_STEPS = ["ai", "ani", "bmm", "cfx", "cmm", "comp", "crw", "env", "flo", "fx", "hair", "lay", "lig", "lin", "md",
                 "mcp", "mod", "mp", "mptd", "paint", "pnt", "prep", "pre", "pub", "qcenv", "rig", "roto", "std",
                 "shd", "tex", "uv"]
_LAUNCHER = "/rdo/rodeo/setup/rdo_rez_launcher/current"
# rez itself: in a terminal `rez` is an alias for this, which a script's shell doesn't expand.
_REZ = "/rdo/software/rez/rez_installations/linux/rocky8/current/bin/rez/rez"


class PreviewConfig(BaseModel):
    """The preview movie the agent makes from each job's frames (README, Preview movies):
    an H.264 MP4 next to them, named by paths.preview_name, played in the dashboard.
    Frozen into each job at submit, so every agent makes the same one."""
    enabled: bool = True
    fps: float = 24.0          # unless the input is a movie: its own rate
    max_width: int = 1920      # wider frames are scaled down (aspect kept); 0: as they are
    crf: int = 18              # H.264 quality: lower is better and bigger (18 looks like the frames)


class ProvenanceConfig(BaseModel):
    """A provenance file beside each job's outputs (decision 0038): what ran, where, on what
    and for whom, and which files are the farm's. Written by the agent that ran it; frozen
    into each job at submit. Each texture map's EXR then carries the job and the file's
    name in its header (0039)."""
    enabled: bool = False


class PolicyConfig(BaseModel):
    """The farm's policy (decisions 0007, 0043): show rules in policy.yaml, read from the bank
    folder and checked when a job is planned. Off: everything is allowed, nothing is read."""
    enabled: bool = False
    file: str = "policy.yaml"           # a name in share.dir, or a path of its own


class LedgerConfig(BaseModel):
    """The spend ledger read against the providers' bills (manager/bills.py, decision 0061):
    the gateway reads each provider's bill with its billing key when asked, and the ledger's
    rows are settled by it. Asked each night at `at` with nightly; `comfyfarm spend
    reconcile` on the manager's host asks at any time."""
    nightly: bool = False
    at: str = "05:00"                       # the manager's local time (HH:MM)
    # A job's request the bill doesn't show this long after the job ended is taken as not
    # charged: billed $0, or released if it may never have reached the provider.
    grace_hours: float = Field(48.0, gt=0)
    balance_below: float = Field(0.0, ge=0)     # an alert when a provider's balance is below it; 0: off

    @model_validator(mode="after")
    def _at(self):
        hh, _, mm = self.at.partition(":")
        if not (hh.isdigit() and mm.isdigit() and len(mm) == 2 and int(hh) < 24 and int(mm) < 60):
            raise ValueError(f"ledger.at is a time of day like 05:00, not {self.at!r}")
        return self


class RetentionConfig(BaseModel):
    """The retention sweeper (decisions 0006, 0041). For now a dry run once a day: it lists
    the expired jobs and the files it would delete (only those their provenance files list)
    in data/sweeps/, and deletes nothing."""
    enabled: bool = False
    keep_days: float = Field(3.0, gt=0)   # after a job finished, or was last sent to be published
    at: str = "03:00"                     # when it runs each day, the manager's local time (HH:MM)
    reports: int = Field(60, ge=1)        # reports kept in data/sweeps/

    @model_validator(mode="after")
    def _at(self):
        hh, _, mm = self.at.partition(":")
        if not (hh.isdigit() and mm.isdigit() and len(mm) == 2 and int(hh) < 24 and int(mm) < 60):
            raise ValueError(f"retention.at is a time of day like 03:00, not {self.at!r}")
        return self


class PublishConfig(BaseModel):
    """Publishing jobs through Daiquiri's update_and_render_nuke (comfyfarm.publish). Off
    until enabled; with dry_run, every publish is a dispatchGraph --dry-run (Daiquiri
    checks it, nothing goes to Tractor)."""
    enabled: bool = False
    dry_run: bool = True
    templates: str = "/rdo/bank/comfy_farm/publishTemplates/Nuke"        # <templates>/<workflow>/<name>.nk
    # (and <templates>/_global/<name>.nk for every workflow; .history/ keeps every version)
    default_step: str = "ai"             # for workflows whose manifest names none (ai: the farm's outputs)
    staging: str = "/rdo/bank/comfy_farm/publishTemplates/Nuke/.staging"  # <staging>/<job id>/<name>.nk
    # A show is published to only when it has this folder (on the manager); the ShotGrid
    # show list leaves out projects without one. Empty: no such check.
    show_folder: str = "/rdo/shows/{show}"
    keep_days: float = 14.0             # staged scripts are removed after this long
    steps: list[str] = Field(default_factory=lambda: list(PUBLISH_STEPS))
    description: str = "Auto Publish"
    # Added to every description: the dailies are made by the manager's account, so they
    # say who asked and which job it was. {job} {user} {workflow}
    description_suffix: str = " (ComfyFarm {job}, {user})"
    # The colorspace each placeholder Read gets, by file extension.
    read_colorspace: dict[str, str] = Field(default_factory=lambda: {"png": "color_picking", "exr": "rdo"})
    # A clean login shell: the pipeline's environment, not the farm's. {home} {user}
    shell: list[str] = Field(default_factory=lambda: ["env", "-i", "HOME={home}", "USER={user}", "LOGNAME={user}",
                                                      "/bin/bash", "-lc"])
    # {graph} (the manifest's publish.graph, update_and_render_nuke) {show} {shot} {step}
    # {description} {workfile} {frames} {first} {last} {job} {user}, each shell-quoted.
    # --owner {user}: the publish is the artist's who asked for it, not the manager's account
    # (Daiquiri's default is the account dispatchGraph runs as).
    command: str = (f"source {_LAUNCHER}/env/set_context.sh {{show}} && {_LAUNCHER}/rdo_rez_launcher.bash -a graph "
                    f"-c dispatchGraph {{graph}} --description {{description}} --step {{step}} "
                    f"--entities {{shot}} --workfile {{workfile}} --frameRange {{frames}} --owner {{user}}")
    job_id_pattern: str = r"#ID:\s*(\d+)"    # the Tractor job in dispatchGraph's output
    timeout: float = 600.0
    workers: int = 2                    # publishes dispatched at once


class MenuSyncConfig(BaseModel):
    """ShotGrid's right-click menu kept in step with the farm's by the manager itself
    (comfyfarm.shotgrid.menusync): tools/sg_ami.py --apply, run when the manager starts,
    when the menu changes (deploy/shotgrid/menu.yaml, the workflows) and once a day, in the
    pipeline's environment so it has rdo_publish_pipeline's ShotGrid client (which may
    create menu items; the manager's own script can't). Off until enabled."""
    enabled: bool = False
    # A clean login shell: the pipeline's environment, not the farm's. {home} {user}
    shell: list[str] = Field(default_factory=lambda: ["env", "-i", "HOME={home}", "USER={user}", "LOGNAME={user}",
                                                      "/bin/bash", "-lc"])
    # {tool} (tools/sg_ami.py) {menu} (the menu, as JSON) {farm} (its farm_url) {show} (the
    # pipeline context's show: `show`, else menu.yaml's first), each shell-quoted. The context
    # gives rez the studio's environment; rez gives the pipeline's client (tried by hand on
    # wrk1125l, 2026-11-08).
    command: str = (f"source {_LAUNCHER}/env/set_context.sh {{show}} && {_REZ} env rdo_publish_pipeline -- "
                    f"python {{tool}} --pipeline --menu-json {{menu}} --apply")
    show: str = ""                       # the show set_context.sh is given (empty: menu.yaml's first)
    check_minutes: float = 5.0           # how often the manager looks whether the menu changed
    every_hours: float = 24.0            # synced again even when unchanged (an item changed by hand)
    timeout: float = 600.0


class QueryConfig(BaseModel):
    """Batch Submit's ShotGrid query (comfyfarm.shotgrid.query): the Version fields it can
    filter on besides show, sequences, shots, name and date (status first, then these, in
    this order; a field the site doesn't have is left out), and how many Versions it takes."""
    # Rodeo's Task and Type were left out (Peter, 2026-11-09: not useful there); any Version
    # list, text or link field can be added, e.g. task: sg_task, type: sg_version_type.
    fields: dict[str, str] = Field(default_factory=lambda: {"step": "sg_pipeline_step", "department": "sg_department"})
    version_number: str = "sg_version_number"   # "latest" ranks by it (else the code's vN, else the newest)
    max_rows: int = 500                          # rows a query gives at most
    scan: int = 5000                             # Versions looked at, the newest first ("latest" picks among them)
    options_ttl: float = 600.0                   # seconds a show's filter values are used before they're asked again


class ShotGridConfig(BaseModel):
    """The manager's read-only ShotGrid lookups for the publish fields (comfyfarm.shotgrid):
    active shows, their sequences and shots. Off until enabled; the script's name and key
    come from the environment or env_file, never from this file."""
    enabled: bool = False
    url: str = "https://rodeofx.shotgrid.autodesk.com/"
    script_name_env: str = "SG_SCRIPT_NAME"
    script_key_env: str = "SG_SCRIPT_KEY"
    env_file: str = "~/.config/comfyfarm/shotgrid.env"     # KEY=value lines; chmod 600
    project_filters: list = Field(default_factory=lambda: [["sg_status", "is", "Active"]])   # the shows offered
    show_field: str = "tank_name"        # the Project field holding the show code (bri3)
    hidden_statuses: list[str] = Field(default_factory=lambda: ["hld", "omt", "hdn", "void"])   # shots left out
    projects_ttl: float = 900.0          # seconds a list is used before it's loaded again
    shots_ttl: float = 600.0
    refresh_hold: float = 10.0           # a refresh button reloads at most this often
    retry_seconds: float = 60.0          # a ShotGrid that didn't answer is left alone this long
    timeout: float = 20.0
    # A Version link as a job's input (README, ShotGrid Version links): the Version fields
    # holding its frames, its movie and its frame range.
    version_fields: dict[str, str] = Field(default_factory=lambda: {
        "frames": "sg_path_to_frames", "movie": "sg_path_to_movie",
        "first": "sg_first_frame", "last": "sg_last_frame"})
    version_ttl: float = 120.0           # seconds a Version's paths are used before they're asked again
    # ShotGrid's right-click menu (README: ShotGrid right-click): which shows have it, and
    # the address it posts to. Relative to the repo.
    menu_file: str = "deploy/shotgrid/menu.yaml"
    menu_sync: MenuSyncConfig = Field(default_factory=MenuSyncConfig)
    # Batch Submit's ShotGrid query (README: Batch Submit): the filters it offers.
    query: QueryConfig = Field(default_factory=QueryConfig)


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    share: ShareConfig = Field(default_factory=ShareConfig)
    rollouts: RolloutsConfig = Field(default_factory=RolloutsConfig)
    gpu_history: GpuHistoryConfig = Field(default_factory=GpuHistoryConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    evals: EvalsConfig = Field(default_factory=EvalsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)
    database: str = "data/comfyfarm.db"
    log_dir: str = "logs"
    log_json: bool = False                      # also write logs/comfyfarm.jsonl, one JSON object per line
    workflows_dir: str = "workflows"
    workers: list[WorkerConfig] = Field(default_factory=lambda: [WorkerConfig(name="local")])
    paths: PathsConfig = Field(default_factory=PathsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    ocio: OcioConfig = Field(default_factory=OcioConfig)
    convert: ConvertConfig = Field(default_factory=ConvertConfig)
    textures: TexturesConfig = Field(default_factory=TexturesConfig)
    farm: FarmConfig = Field(default_factory=FarmConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    preview: PreviewConfig = Field(default_factory=PreviewConfig)
    provenance: ProvenanceConfig = Field(default_factory=ProvenanceConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)
    shotgrid: ShotGridConfig = Field(default_factory=ShotGridConfig)

    base_dir: Path = Field(default=Path("."), exclude=True)
    obsolete_keys: list[str] = Field(default_factory=list, exclude=True)

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path)


class AgentPathsConfig(BaseModel):
    # Show storage as Linux workstations see it, and as this machine sees it. Empty on a
    # Linux machine that mounts it at the same place.
    mappings: list[PathMapping] = Field(default_factory=list)


class GpuProbeConfig(BaseModel):
    """The agent reads this machine's GPUs itself with nvidia-smi (agent/gpus.py, decision 0022)
    and reports them with its status: the Machines tab shows each card's memory in use by any
    program. Placement doesn't use it yet."""
    enabled: bool = False
    seconds: float = 30.0                       # read this often
    command: list[str] = ["nvidia-smi"]         # comes with the NVIDIA driver, on Linux and Windows


class ProcessRunnerConfig(BaseModel):
    """The process runner (runners/process.py, decision 0025): commands of process
    definitions, each in its runner environment, with a time limit."""
    enabled: bool = False                       # off: process definitions are reported as not runnable here
    runs_dir: str = "data/runs"                 # a folder per run; relative = next to agent.yaml
    default_minutes: float = Field(60.0, gt=0)  # the time limit of a definition without limits.max_minutes
    grace_seconds: float = Field(15.0, ge=0)    # after cancel or the limit, before it is killed


class EnvsConfig(BaseModel):
    """Runner environments built on this machine (comfyfarm/envs, decision 0024)."""
    dir: str = "data/envs"                      # builds, uv's cache and its Pythons; relative = next to agent.yaml
    uv: list[str] = []                          # a uv to use; empty: one installed with pip into <dir>/.uv-tool
    # Install, update and remove them as the manager says (agent/envman.py, decision 0030),
    # and sync ComfyUI to the farm's environment file when it says (decision 0033). Off: the
    # agent only reports that it is off when the manager asks for something.
    managed: bool = False


class ComfyUILaunchConfig(BaseModel):
    """How the agent starts this machine's ComfyUI when it isn't running (optional). ComfyUI
    runs on its own once started: restarting the agent leaves it, and its jobs, running."""
    command: list[str] = Field(default_factory=list)   # e.g. [<ComfyUI's python>, main.py, --port, "8188"]
    cwd: str = ""                                      # ComfyUI's folder
    env: dict[str, str] = Field(default_factory=dict)  # extra environment, e.g. CUDA_VISIBLE_DEVICES
    restart_after: float = 30.0        # unreachable this long, and not running: started again
    startup_seconds: float = 300.0     # a start gets this long to answer before it's tried again


class GatewayProviderConfig(BaseModel):
    """One outside provider on the gateway (decision 0056). Its keys stay on this machine:
    the files are read when the gateway starts, and what they hold goes only to the provider."""
    lanes: int | None = Field(None, ge=1)       # its jobs at once here; empty: providers/<name>.yaml's lanes
    keys: dict[str, str] = Field(default_factory=dict)  # each key providers/<name>.yaml names -> the file holding it
    url: str = ""                               # a stand-in for the provider (tools/mock_fal.py); empty: the provider
    media_seconds: int = Field(3600, ge=60)     # how long the provider keeps what the gateway uploads and gets made


class GatewayConfig(BaseModel):
    """The farm's gateway (decisions 0055, 0056): an agent that runs API definitions at outside
    providers, in lanes, and nothing else. Off: the agent is a GPU machine's."""
    enabled: bool = False
    account: str = ""                           # whose accounts the keys are, e.g. peter-pilot: kept with each run
    runs_dir: str = "data/api_runs"             # a folder per run; relative = next to gateway.yaml
    poll_seconds: float = Field(2.0, gt=0)      # each run's status is asked this often
    default_minutes: float = Field(30.0, gt=0)  # the time limit of a definition without limits.max_minutes
    prices_hours: float = Field(24.0, gt=0)     # each provider's prices are read this often (decision 0059)
    providers: dict[str, GatewayProviderConfig] = Field(default_factory=dict)


class AgentConfig(BaseModel):
    """One agent: runs the manager's jobs on one ComfyUI (agent.example.yaml). Everything
    here describes this machine; how jobs are run (colour, naming, versions) comes with
    each job from the manager. With gateway.enabled it is the farm's gateway instead
    (gateway.example.yaml)."""
    name: str                                   # as the dashboard shows it, e.g. rtx5090-a
    # The manager's address, or the manager.json it writes in the farm's shared folder
    # (share.dir), read again whenever the manager can't be reached.
    manager: str = "http://127.0.0.1:8000"
    token: str = ""                             # the manager's agents.token, if it has one
    comfyui: str = "http://127.0.0.1:8188"      # this machine's ComfyUI (keep it on localhost)
    comfyui_launch: ComfyUILaunchConfig = Field(default_factory=ComfyUILaunchConfig)
    # Where ComfyUI and its Python are, for checking it against the farm's environment
    # (comfyfarm comfyui verify). Empty: from comfyui_launch (its main.py, its python).
    comfyui_dir: str = ""
    comfyui_python: str = ""
    environment_seconds: float = 900.0  # the environment is checked this often (and when ComfyUI starts)
    gpu_probe: GpuProbeConfig = Field(default_factory=GpuProbeConfig)
    envs: EnvsConfig = Field(default_factory=EnvsConfig)
    process_runner: ProcessRunnerConfig = Field(default_factory=ProcessRunnerConfig)
    # The GPU claim (decision 0026): how long a run waits for the runner that used the card
    # before (ComfyUI) to free its memory, before it starts anyway with a warning on the job.
    claim_wait_seconds: float = Field(300.0, ge=0)
    max_inflight: int = 2                       # jobs in ComfyUI at once; one more is prepared ahead
    # Labels workflows can ask for (a manifest's requires.tags). linux or windows is added.
    tags: list[str] = []
    workflows_dir: str | None = None            # workflow definitions; None: this checkout's workflows/
    log_dir: str = "logs"
    log_json: bool = False                      # also write logs/agent-<name>.jsonl, one JSON object per line
    paths: AgentPathsConfig = Field(default_factory=AgentPathsConfig)
    convert: ConvertConfig = Field(default_factory=ConvertConfig)   # scratch_dir, tools, frame_workers
    textures: TexturesConfig = Field(default_factory=TexturesConfig)
    heartbeat_wait: float = 10.0       # the manager holds a check-in this long while it has nothing for us
    retry_seconds: float = 2.0         # first wait when the manager can't be reached (doubles, up to 30)
    health_seconds: float = 5.0        # ComfyUI checked this often
    reconcile_seconds: float = 20.0    # our jobs cross-checked against ComfyUI's queue and history
    lost_after_seconds: float = 300.0  # ComfyUI unreachable this long: its jobs go back to the manager
    delete_comfy_history: bool = True
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)

    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _gateway_scratch(self) -> "AgentConfig":
        # The gateway runs beside the GPU agent on the manager's machine. An agent empties the
        # scratch folders of jobs it doesn't hold when it starts, so the two never share one.
        if self.gateway.enabled and "scratch_dir" not in self.convert.model_fields_set:
            self.convert.scratch_dir = "data/scratch-gateway"
        return self

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path)


def standalone_agent_config(cfg: Config, worker: WorkerConfig, scratch_dir: str | None = None) -> AgentConfig:
    """The agent of one of config.yaml's workers, run inside the manager (standalone)."""
    convert = cfg.convert.model_copy()
    if scratch_dir:
        convert.scratch_dir = scratch_dir
    return AgentConfig(name=worker.name, comfyui=worker.url, max_inflight=worker.max_inflight, log_dir=cfg.log_dir,
                       paths=AgentPathsConfig(mappings=cfg.paths.mappings), convert=convert, textures=cfg.textures,
                       health_seconds=cfg.farm.health_seconds, reconcile_seconds=cfg.farm.reconcile_seconds,
                       lost_after_seconds=cfg.farm.lost_after_seconds,
                       delete_comfy_history=cfg.farm.delete_comfy_history, workflows_dir=str(cfg.resolve(cfg.workflows_dir)),
                       base_dir=cfg.base_dir)


def default_agent_config_path() -> Path:
    env = os.environ.get("COMFYFARM_AGENT_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "agent.yaml"


def load_agent_config(path: str | os.PathLike | None = None) -> AgentConfig:
    cfg_path = Path(path) if path else default_agent_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(f"No agent config at {cfg_path}. Copy agent.example.yaml to agent.yaml and edit it.")
    cfg = AgentConfig.model_validate(yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {})
    cfg.base_dir = cfg_path.resolve().parent
    return cfg


def default_config_path() -> Path:
    env = os.environ.get("COMFYFARM_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path) if path else default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"No config at {cfg_path}. Copy config.example.yaml to config.yaml and edit it."
        )
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(data)
    cfg.base_dir = cfg_path.resolve().parent
    paths = data.get("paths") or {}
    for key in ("show_regex", "shot_regex"):
        if key in paths:
            cfg.obsolete_keys.append(f"paths.{key} is no longer used (show/shot now come from "
                                     f"paths.context_patterns); remove it")
    # config.yaml is copied once from the example and never updated by git pull, so
    # flag the old naming defaults that would otherwise silently pin the old behaviour.
    old_defaults = {
        "frame_name": ("{shot}_{tag}_v{version:03d}.{frame:04d}.{ext}", "name files after their source"),
        "preview_name": ("{shot}_{tag}_v{version:03d}_preview.mp4", "name files after their source"),
        "output_dir": ("{root}/{shot}/{shot}_{tag}_v{version:03d}", "write to {root}/{shot}"),
    }
    for key, (old, new) in old_defaults.items():
        if paths.get(key) == old:
            cfg.obsolete_keys.append(f"paths.{key} is the old default; delete the line to {new}")
    # Show configs, display/view and the texture colour names gave way to the repo's
    # ocio/ config (`ocio:`); an old config.yaml still carrying them would look as if it
    # still steered the conversion.
    old_exr = sorted(set((data.get("convert") or {}).get("exr") or {}) - {"extra_args", "png_compression"})
    if old_exr:
        cfg.obsolete_keys.append(f"convert.exr: {', '.join(old_exr)} no longer used; EXRs are converted with the "
                                 f"repo's OCIO config (ocio: in config.yaml). Delete those lines")
    if "build_on_agent" in (data.get("farm") or {}):
        cfg.obsolete_keys.append("farm.build_on_agent is no longer used: the machine that runs a job always "
                                 "builds its graph. Delete the line")
    old_tex = sorted(set(data.get("textures") or {}) - {"workers"})
    if old_tex:
        cfg.obsolete_keys.append(f"textures: {', '.join(old_tex)} no longer used; colour comes from ocio: in "
                                 f"config.yaml. Delete those lines")
    old_ocio = sorted(set(data.get("ocio") or {}) - {"config"})
    if old_ocio:
        cfg.obsolete_keys.append(f"ocio: {', '.join(old_ocio)} no longer used; the repo's ocio/config.ocio "
                                 f"names its colour spaces itself. Delete those lines")
    if "batches_ahead" in (data.get("convert") or {}):
        cfg.obsolete_keys.append("convert.batches_ahead is no longer used: each worker converts the job it "
                                 "takes next while its GPU runs the current one. Delete the line")
    patterns = paths.get("context_patterns")
    if patterns and not any("movhistory" in p for p in patterns):
        cfg.obsolete_keys.append("paths.context_patterns has no shot movhistory pattern; add "
                                 "'/shows/{show}/{seq}/{shot_short}/movhistory/' (or delete the list "
                                 "to use the defaults)")
    return cfg
