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
    # Applied to the farm-side input path / the input file name.
    show_regex: str = r"/shows/(?P<show>[^/]+)/"
    shot_regex: str = r"(?P<shot>[A-Za-z]+_\d+)"
    # Output layout. Placeholders: {show} {shot} {tag} {version} {root} {frame} {ext}
    output_root: str = "//server/share/shows/{show}/comfyui/renders"
    output_dir: str = "{root}/{shot}/{shot}_{tag}_v{version:03d}"
    frame_name: str = "{shot}_{tag}_v{version:03d}.{frame:04d}.{ext}"
    preview_name: str = "{shot}_{tag}_v{version:03d}_preview.mp4"
    # Check input sequences exist (and work out frame ranges) when a job is submitted.
    # Turn off only if the gateway runs somewhere that cannot see the show storage.
    validate_inputs: bool = True


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
    farm: FarmConfig = Field(default_factory=FarmConfig)

    base_dir: Path = Field(default=Path("."), exclude=True)

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
    return cfg
