"""Gateway configuration, loaded from config.yaml (see config.example.yaml)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


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
    # The same storage as Linux workstations see it, and as the farm machine sees it.
    posix: str
    windows: str


class PathsConfig(BaseModel):
    mappings: list[PathMapping] = Field(default_factory=list)
    # Where show / seq / shot sit in an input path. Each {name} matches one folder, so
    # shot naming doesn't matter (bpash_1545, 15454_656551, ...). Tried in order; later
    # patterns only fill in what earlier ones didn't find.
    context_patterns: list[str] = Field(default_factory=lambda: [
        "/shows/{show}/.published/{seq}/{shot}/",
        "/shows/{show}/",
    ])
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


class ExrConvertConfig(BaseModel):
    # The show's OCIO config. It finds the per-shot client LUTs itself through its
    # search_path (../../_color_bundle/${SEQ}_${SHOT}:...), so SEQ/SHOT are set per job.
    ocio_config: str = "/rdo/shows/{show}/_project_ref/_lut/_ocio/current/config.ocio"
    # OCIO context variables. {shot_short} = shot folder without its "<seq>_" prefix
    # (bpash_1545 in seq bpash -> 1545), so ${SEQ}_${SHOT} names the shot's bundle.
    context: dict[str, str] = Field(default_factory=lambda: {"SEQ": "{seq}", "SHOT": "{shot_short}"})
    input_colorspace: str = "scene_linear"   # role or colorspace of the EXR plates
    display: str = "sRGB-monitor"
    view: str = "Client-look"
    # Plain RGB, cropped to the display window (drops overscan), before the transform.
    extra_args: list[str] = Field(default_factory=lambda: ["--ch", "R,G,B", "--croptofull"])


class MovieConvertConfig(BaseModel):
    # Frame numbers come from the movie's timecode; this is used when it has none.
    default_start: int = 1001


class ConvertConfig(BaseModel):
    """EXR sequences and movies are converted to PNG frames on the farm machine before
    the workflow runs, so one ComfyUI graph handles every input format."""
    scratch_dir: str = "data/scratch"
    max_parallel_jobs: int = 1      # conversions at once (CPU/disk); they overlap GPU work
    frame_workers: int = 6          # oiiotool processes per EXR job
    keep_failed: bool = False       # keep converted frames of failed jobs for debugging
    # Empty = look next to the gateway's Python (the venv), then on PATH.
    # A path, or a command as a list (e.g. [python, script.py]).
    tools: dict[str, str | list[str]] = Field(default_factory=lambda: {"oiiotool": "", "ffmpeg": "", "ffprobe": ""})
    exr: ExrConvertConfig = Field(default_factory=ExrConvertConfig)
    movie: MovieConvertConfig = Field(default_factory=MovieConvertConfig)


class AuthConfig(BaseModel):
    # api key -> user name. Empty = no keys required; users identify themselves.
    api_keys: dict[str, str] = Field(default_factory=dict)
    # Admins can cancel anyone's job and submit with priority above 0.
    admins: list[str] = Field(default_factory=list)


class FarmConfig(BaseModel):
    max_attempts: int = 2            # resubmits when a worker loses a job (crash/restart)
    reconcile_seconds: float = 20.0  # cross-check job state against ComfyUI this often
    health_seconds: float = 5.0
    lost_after_seconds: float = 300.0  # worker unreachable this long -> its jobs are requeued
    delete_comfy_history: bool = True  # drop finished prompts from ComfyUI's in-memory history
    # Fail jobs straight away when the worker is missing a node pack, an input the
    # workflow uses, or a model file (see `python -m comfyfarm check`).
    strict_preflight: bool = True


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    database: str = "data/comfyfarm.db"
    log_dir: str = "logs"
    workflows_dir: str = "workflows"
    workers: list[WorkerConfig] = Field(default_factory=lambda: [WorkerConfig(name="local")])
    paths: PathsConfig = Field(default_factory=PathsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    convert: ConvertConfig = Field(default_factory=ConvertConfig)
    farm: FarmConfig = Field(default_factory=FarmConfig)

    base_dir: Path = Field(default=Path("."), exclude=True)
    obsolete_keys: list[str] = Field(default_factory=list, exclude=True)

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (self.base_dir / path)


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
    return cfg
