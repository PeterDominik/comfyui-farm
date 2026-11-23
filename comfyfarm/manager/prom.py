"""The farm's numbers in Prometheus' text format: GET /metrics (decisions 0010, 0048).

What a Prometheus (or Grafana Agent, or anything that reads the format) scrapes from the
manager, every number from what the manager already holds:

- the manager: its build and since when it runs;
- the queue: jobs in each active state per definition, and how long the oldest has waited;
- finished jobs, from job_metrics (decision 0040), so they outlive the jobs: how many per
  definition, outcome and GPU type, and the seconds, frames and waits of those that ran;
- the machines: checking in, their runner up, drained, the jobs they hold;
- their GPUs: use and memory, as gpu_probe reads them (nvidia-smi), or memory as ComfyUI
  reports it when the probe is off;
- the policy (in force, the file refused) and the sweeper's latest report.

Every family is a gauge: job_metrics rows are replaced when a job finishes again, so their
counts can go down, which a counter mustn't. The text format is written here by hand (it
is simple, and the manager needs no new package for it)."""

from __future__ import annotations

from datetime import datetime

from ..protocol import ACTIVE, build, now
from . import eta as E

__all__ = ["render", "Exposition"]

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(v) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    return repr(float(v))


class Exposition:
    """Metric families in the order they're added, each with its HELP and TYPE once."""

    def __init__(self):
        self._families: dict[str, dict] = {}

    def gauge(self, name: str, help_text: str, value=None, /, **labels) -> None:
        """A sample of family `name` (declared on its first call, with `help_text`). Positional
        only, so a label may be called name (a GPU's)."""
        fam = self._families.setdefault(name, {"help": help_text, "samples": []})
        if value is not None:
            fam["samples"].append((labels, value))

    def text(self) -> str:
        lines = []
        for name, fam in self._families.items():
            lines.append(f"# HELP {name} {fam['help']}")
            lines.append(f"# TYPE {name} gauge")
            for labels, value in fam["samples"]:
                ls = ",".join(f'{k}="{_label(v)}"' for k, v in labels.items())
                lines.append(f"{name}{{{ls}}} {_number(value)}" if ls else f"{name} {_number(value)}")
        return "\n".join(lines) + "\n"


def _queue(x: Exposition, store, t: float) -> None:
    x.gauge("comfyfarm_jobs", "Jobs (runs: a texture set's batches, not the set) in each active state, per definition")
    counts: dict[tuple[str, str], int] = {}
    oldest = None
    for j in store.list(statuses=ACTIVE, limit=10_000_000, top_level=False, work_only=True):
        counts[(j["status"], j["workflow"])] = counts.get((j["status"], j["workflow"]), 0) + 1
        if j["status"] == "queued":
            oldest = min(oldest or j["created_at"], j["created_at"])
    for (status, wf), n in sorted(counts.items()):
        x.gauge("comfyfarm_jobs", "", n, status=status, workflow=wf)
    x.gauge("comfyfarm_queue_oldest_seconds", "How long the job queued longest has waited (0: nothing queued)",
            round(t - oldest, 1) if oldest else 0)


def _finished(x: Exposition, store) -> None:
    names = {
        "comfyfarm_jobs_finished": "Jobs finished (job_metrics), per definition, outcome and GPU type",
        "comfyfarm_job_run_seconds_sum": "Seconds jobs that finished done ran on their GPU, outputs collected",
        "comfyfarm_job_run_seconds_count": "Jobs those seconds are of",
        "comfyfarm_job_frames_sum": "Frames (tiles, for textures) those jobs read: seconds a frame is run / frames",
        "comfyfarm_job_wait_seconds_sum": "Seconds finished jobs waited, submitted until given to a machine",
        "comfyfarm_job_wait_seconds_count": "Jobs those seconds are of",
    }
    for name, help_text in names.items():
        x.gauge(name, help_text)
    for r in store.metrics_totals():
        x.gauge("comfyfarm_jobs_finished", "", r["n"], workflow=r["workflow"], outcome=r["outcome"], gpu=r["gpu"])
        if r["runs"]:
            x.gauge("comfyfarm_job_run_seconds_sum", "", round(r["run"] or 0.0, 1), workflow=r["workflow"],
                    gpu=r["gpu"])
            x.gauge("comfyfarm_job_run_seconds_count", "", r["runs"], workflow=r["workflow"], gpu=r["gpu"])
            x.gauge("comfyfarm_job_frames_sum", "", r["frames"] or 0, workflow=r["workflow"], gpu=r["gpu"])
        if r["waits"]:
            x.gauge("comfyfarm_job_wait_seconds_sum", "", round(r["wait"] or 0.0, 1), workflow=r["workflow"],
                    outcome=r["outcome"])
            x.gauge("comfyfarm_job_wait_seconds_count", "", r["waits"], workflow=r["workflow"], outcome=r["outcome"])


def _machines(x: Exposition, desk) -> None:
    x.gauge("comfyfarm_machine_up", "1 while the machine's agent checks in")
    x.gauge("comfyfarm_machine_runner_up", "1 while the machine's resident runner (ComfyUI) is reachable")
    x.gauge("comfyfarm_machine_drained", "1 while the machine is drained: it finishes what it holds, takes nothing new")
    x.gauge("comfyfarm_machine_jobs", "Jobs the machine holds (preparing, on its GPU, collecting outputs)")
    x.gauge("comfyfarm_machine_info", "The machine's agent build and GPU type, as labels (always 1)")
    x.gauge("comfyfarm_gpu_utilization_ratio", "A GPU's use, 0 to 1, as nvidia-smi reads it (gpu_probe)")
    x.gauge("comfyfarm_gpu_memory_used_bytes", "A GPU's memory in use: nvidia-smi's, or ComfyUI's with the probe off")
    x.gauge("comfyfarm_gpu_memory_total_bytes", "A GPU's memory")
    for a in sorted(desk.agents.values(), key=lambda a: a.name):
        alive = desk.alive(a.name)
        x.gauge("comfyfarm_machine_up", "", alive, machine=a.name)
        x.gauge("comfyfarm_machine_runner_up", "", alive and a.online, machine=a.name)
        x.gauge("comfyfarm_machine_drained", "", a.name in desk.drained, machine=a.name)
        x.gauge("comfyfarm_machine_jobs", "", len(desk.store.held_by(a.name)), machine=a.name)
        x.gauge("comfyfarm_machine_info", "", 1, machine=a.name, agent=a.version or "", gpu=desk.gpu_type(a))
        if not alive:
            continue
        if a.gpus:
            for g in a.gpus:
                at = {"machine": a.name, "gpu": str(g.get("index", 0)), "name": g.get("name") or ""}
                if g.get("utilization") is not None:
                    x.gauge("comfyfarm_gpu_utilization_ratio", "", round(g["utilization"] / 100, 3), **at)
                if g.get("vram_used") is not None:
                    x.gauge("comfyfarm_gpu_memory_used_bytes", "", g["vram_used"], **at)
                if g.get("vram_total") is not None:
                    x.gauge("comfyfarm_gpu_memory_total_bytes", "", g["vram_total"], **at)
            continue
        for i, dev in enumerate((a.stats or {}).get("devices") or []):
            at = {"machine": a.name, "gpu": str(dev.get("index", i)), "name": E.gpu_type(dev.get("name"))}
            total, free = dev.get("vram_total"), dev.get("vram_free")
            if total is not None and free is not None:
                x.gauge("comfyfarm_gpu_memory_used_bytes", "", max(0, total - free), **at)
            if total is not None:
                x.gauge("comfyfarm_gpu_memory_total_bytes", "", total, **at)


def _policy_and_sweeps(x: Exposition, farm) -> None:
    p = farm.policy
    x.gauge("comfyfarm_policy_enabled", "1 when the farm's policy is on (policy.enabled)", p.enabled)
    if p.enabled:
        x.gauge("comfyfarm_policy_in_force", "1 while a valid policy.yaml is in force (0: nothing is planned)",
                p.rules is not None)
        x.gauge("comfyfarm_policy_file_error", "1 while policy.yaml as it is now isn't taken", p.error is not None)
    latest = farm.sweeper.reports()[:1]
    if latest:
        r = latest[0]
        try:
            at = datetime.fromisoformat(r["at"]).timestamp()
        except (TypeError, ValueError):
            at = None
        x.gauge("comfyfarm_sweep_last_run_timestamp_seconds", "When the retention sweeper last ran", at)
        for key, help_text in (("expired", "Expired jobs the sweeper's latest report lists"),
                               ("files", "Files the sweeper's latest report would delete"),
                               ("bytes", "Bytes the sweeper's latest report would free"),
                               ("problems", "Problems in the sweeper's latest report: what to look at")):
            x.gauge(f"comfyfarm_sweep_{key}", help_text, r.get(key) or 0)


def render(farm, desk, started_at: float, t: float | None = None) -> str:
    """Everything above, as GET /metrics answers it."""
    t = now() if t is None else t
    x = Exposition()
    x.gauge("comfyfarm_manager_info", "The manager's build, as a label (always 1)", 1, build=build())
    x.gauge("comfyfarm_manager_start_time_seconds", "When the manager started", round(started_at, 3))
    _queue(x, farm.store, t)
    _finished(x, farm.store)
    if desk is not None:
        _machines(x, desk)
    _policy_and_sweeps(x, farm)
    return x.text()
