"""The farm's batch test (workflows/batch_test, decision 0063), run by the process runner in
the gpu-test environment: python run.py, with $COMFYFARM_JOB naming its job.json.

A stand-in for a long job such as training. It counts `steps`, one every `step_seconds`,
holding `hold_gb` of the card, and keeps where it is in `checkpoint.json` in its resume folder
(job.json's resume_dir): every `save_every` steps, and when the farm wants its GPU (the
`preempt` file appears), after which it stops. A run that finds a checkpoint goes on from it,
so a job stopped on one machine finishes on another. Its report (writes.file) lists each run:
the machine, its card and the steps it did. It stops at once, saving nothing, when the
`cancel` file appears.

torch is imported when it is needed, so the farm's tests can run this file with a stand-in."""

from __future__ import annotations

import json
import os
import socket
import sys
import time

GB = 2**30
TICK = 0.1                              # how often it looks for the cancel and preempt files while it waits


class Job:
    """job.json, and the files the farm watches."""

    def __init__(self, path: str):
        with open(path, encoding="utf-8") as f:
            self.d = json.load(f)
        self.params = self.d.get("params") or {}
        # A job.json written by hand (no resume_dir) keeps its checkpoint where it runs.
        self.resume = self.d.get("resume_dir") or "."

    def event(self, **e) -> None:
        with open(self.d["events"], "a", encoding="utf-8") as f:
            f.write(json.dumps(e) + "\n")

    def result(self, **r) -> None:
        with open(self.d["result"], "w", encoding="utf-8") as f:
            json.dump(r, f)

    def cancelled(self) -> bool:
        return os.path.exists(self.d["cancel"])

    def preempted(self) -> bool:
        return bool(self.d.get("preempt")) and os.path.exists(self.d["preempt"])

    def load(self) -> dict | None:
        try:
            with open(os.path.join(self.resume, "checkpoint.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def save(self, state: dict) -> None:
        """Whole or not at all: a run stopped while saving leaves the last checkpoint."""
        os.makedirs(self.resume, exist_ok=True)
        path = os.path.join(self.resume, "checkpoint.json")
        with open(path + ".part", "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(path + ".part", path)

    def report_path(self) -> str:
        """Where its report goes: as job.json's writes.file says, for the farm to name it after
        the job; without one (a job.json written by hand), where it runs."""
        w = (self.d.get("writes") or {}).get("file")
        return os.path.join(w["dir"], f"{w['prefix']}.json") if w else f"batch_test.{self.d['job'][:8]}.json"


def fail(job: Job, message: str) -> int:
    print(message, file=sys.stderr, flush=True)
    job.result(error=message)
    return 1


def wait(job: Job, seconds: float) -> str | None:
    """Wait one step; "cancel" or "preempt" if the farm asked meanwhile."""
    end = time.monotonic() + seconds
    while True:
        if job.cancelled():
            return "cancel"
        if job.preempted():
            return "preempt"
        left = end - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(TICK, left))


def main() -> int:
    job = Job(os.environ["COMFYFARM_JOB"])
    p = job.params
    steps, every = int(p.get("steps", 300)), float(p.get("step_seconds", 1))
    save_every, hold = max(1, int(p.get("save_every", 10))), float(p.get("hold_gb") or 0)
    import torch
    if not torch.cuda.is_available():
        return fail(job, "torch sees no GPU on this machine: check its NVIDIA driver (nvidia-smi)")
    name = torch.cuda.get_device_name(0)
    state = job.load() or {"step": 0, "steps": steps, "runs": []}
    if state.get("steps") != steps:
        return fail(job, f"its resume folder {job.resume} holds a run of {state.get('steps')} steps, not {steps}")
    run = {"machine": socket.gethostname(), "gpu": name, "from": state["step"], "to": state["step"],
           "ended": "running"}
    state["runs"].append(run)
    print(f"torch {torch.__version__} on {name}: " +
          (f"resuming at step {state['step']} of {steps}" if state["step"] else f"{steps} steps"), flush=True)
    held = None
    if hold > 0:
        free, total = torch.cuda.mem_get_info()
        try:
            held = torch.empty(int(hold * GB), dtype=torch.uint8, device=torch.device("cuda"))
            held.fill_(1)                               # written, so the memory is really in use
        except torch.cuda.OutOfMemoryError:
            return fail(job, f"couldn't hold {hold:g} GB of the GPU: {free / GB:.1f} of {total / GB:.1f} GB were "
                             f"free when it started")
    job.event(progress=state["step"] / steps, stage=f"step {state['step']}/{steps}" +
              (f" (resumed on {run['machine']})" if state["step"] else ""))
    while state["step"] < steps:
        said = wait(job, every)
        if said == "cancel":
            return 0                                    # the farm asked: it reports the job cancelled
        if said == "preempt":
            run["ended"] = "preempted"
            job.save(state)                             # where it is: the next run goes on from here
            job.event(stage=f"preempted at step {state['step']}/{steps}: saved")
            print(f"preempted at step {state['step']}: saved to {job.resume}", flush=True)
            return 0
        state["step"] += 1
        run["to"] = state["step"]
        job.event(progress=state["step"] / steps, stage=f"step {state['step']}/{steps}")
        if state["step"] % save_every == 0:
            job.save(state)
    run["ended"] = "finished"
    job.save(state)
    report = {"steps": steps, "runs": state["runs"],
              "preempted": sum(1 for r in state["runs"] if r["ended"] == "preempted"), "held_gb": hold}
    with open(job.report_path(), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report), flush=True)
    job.result(warnings=[])
    del held
    return 0


if __name__ == "__main__":
    sys.exit(main())
