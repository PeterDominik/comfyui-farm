"""Evals (decision 0053): a definition's reference job, run on one machine.

A definition's `eval:` names its inputs (files in its own folder), its other params, and the
checks on what it writes (definitions.py). An eval is an ordinary job, planned by the farm
from those files, with three differences:

- It is pinned to one machine, and that machine takes it even while drained (desk._claim):
  rollouts and syncs drain a machine before they change it and evaluate it before it takes
  artists' jobs again.
- Its outputs go into the machine's own scratch folder (evals/<definition>, emptied before
  each eval of it), not a show's. The farm plans the folder in its own scratch folder; the
  job carries it as @scratch/..., which each machine reads as its own (fileio.wire).
- The policy doesn't apply: it is the farm testing a machine, for no show.

The job carries what the agent checks (info.eval): the checks, with the reference images
as @repo/... paths, and the SHA-256 of each eval file as the manager's checkout has it. The
agent refuses to run it if its own copies differ, and fails it if a check doesn't pass
(agent/evals.py): a failed eval is a failed job, whose error says which check and by how
much. A passed one says what each check measured, in its outputs.

`comfyfarm eval` starts one through POST /api/v1/evals, from the manager's host only. With
evals.gate, rollouts (rollouts.py) and ComfyUI syncs (syncs.py) start them on each machine
before they go on."""

from __future__ import annotations

import copy
import hashlib
import logging
import os
from pathlib import Path

from ..fileio import paths as P
from ..protocol import ACTIVE, ON_WORKER, now

log = logging.getLogger("comfyfarm")

__all__ = ["Evals", "EvalError", "PRIORITY"]

PRIORITY = 1000                 # before any artist's job, on the machine it is pinned to


class EvalError(Exception):
    """An eval that can't start. The message says what to do."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Evals:
    def __init__(self, desk):
        self.desk = desk
        self.farm = desk.farm
        self.store = desk.store
        self.cfg = desk.cfg.evals

    @property
    def root(self) -> str:
        """The farm's evals folder in its scratch folder: each machine's own, as @scratch/evals."""
        return f"{self.farm.scratch_root}/evals"

    def definitions(self, runner: str | None = None, environment: str | None = None) -> list[str]:
        """The definitions with an eval that loads without problems: of a runner type
        (comfyui, process), or run in a runner environment."""
        out = []
        for name, wf in sorted(self.farm.registry.workflows.items()):
            if not wf.eval or wf.head_problems:
                continue
            if runner is not None and wf.runner_type != runner:
                continue
            if environment is not None and (wf.process or {}).get("environment") != environment:
                continue
            out.append(name)
        return out

    # ---------------------------------------------------------------- starting
    def start(self, definition: str, machine: str, by: str | None, purpose: str) -> dict:
        """Queue the eval of a definition on a machine. `purpose`: what it is for, as the
        dashboard shows it (`comfyfarm eval`, rollout <id>, sync <id>)."""
        from ..definitions import ParamError
        from ..fileio.errors import SequenceError
        from .farm import FarmError
        wf = self.farm.registry.workflows.get(definition)
        if wf is None:
            raise EvalError(f"no definition {definition} (the manager's checkout has "
                            f"{', '.join(sorted(self.farm.registry.workflows)) or 'none'})")
        if not wf.eval:
            raise EvalError(f"{definition} has no eval: in its process.yaml (README, Evals)")
        if wf.head_problems:
            raise EvalError(f"{definition} doesn't load: {'; '.join(wf.head_problems)}")
        a = self.desk.agents.get(machine)
        if a is None:
            raise EvalError(f"no machine {machine} (known: {', '.join(sorted(self.desk.agents)) or 'none'})")
        why = self.desk.unmet(a, definition)
        if why:
            raise EvalError(f"{machine} may not run {definition}: it {why}")
        repo = self.desk.view.repo
        try:
            inside = wf.dir.resolve().relative_to(Path(repo).resolve()).as_posix()
        except ValueError:
            raise EvalError(f"{definition} isn't in the manager's checkout ({repo}): its eval files can't be "
                            f"named for the machines") from None
        # Named from the checkout as the desk names it, so each becomes @repo/... for the machines.
        folder = P.normalize(f"{repo}/{inside}")
        e = wf.eval
        values = dict(e.get("params") or {})
        for name, raw in (e.get("inputs") or {}).items():
            path, _, rest = raw.partition(" ")
            values[name] = f"{folder}/{path}" + (f" {rest}" if rest else "")
        out = f"{self.root}/{definition}"
        os.makedirs(self.root, exist_ok=True)
        values[wf.output_folder_param] = out
        checks = copy.deepcopy(e.get("checks") or {})
        for x in checks.get("like") or []:
            x["expected"] = f"{folder}/{x['expected']}"
        evaluation = {"root": self.root, "machine": machine, "for": purpose, "definition": definition,
                      "checks": checks, "folder": out,
                      "files": [[f"{folder}/{rel}", sha256(wf.dir / rel)] for rel in wf.eval_files()]}
        try:
            job = self.farm.submit(definition, values, user=by or "farm", name=f"eval on {machine}",
                                   priority=PRIORITY, evaluation=evaluation)
        except (FarmError, ParamError, SequenceError) as e:
            raise EvalError(f"the eval of {definition} can't be planned: {e}") from None
        log.info("eval of %s on %s queued for %s: job %s", definition, machine, purpose, job["id"][:8])
        return job

    def begin(self, machine: str, definitions: list[str], purpose: str, by: str | None) -> list[str]:
        """The evals of these definitions on a machine, all or none: their job ids."""
        ids: list[str] = []
        try:
            for d in definitions:
                ids.append(self.start(d, machine, by, purpose)["id"])
        except EvalError:
            self.cancel(ids)
            raise
        return ids

    def begin_each(self, definition: str, machines: list[str], purpose: str, by: str | None) -> list[str]:
        """The eval of one definition on each of these machines, all or none: their job ids."""
        ids: list[str] = []
        try:
            for m in dict.fromkeys(machines):
                ids.append(self.start(definition, m, by, purpose)["id"])
        except EvalError:
            self.cancel(ids)
            raise
        return ids

    # ---------------------------------------------------------------- how they went
    def judge(self, ids: list[str]) -> tuple[str, str | None]:
        """running, passed, or failed with why (the first that didn't pass)."""
        jobs = [self.store.get(i) for i in ids]
        for i, j in zip(ids, jobs):
            if j is None:
                return "failed", f"its eval job {i[:8]} is gone"
            label = j["workflow"]
            if j["status"] == "failed":
                return "failed", f"the eval of {label} failed (job {i[:8]}): {j.get('error') or 'no reason given'}"
            if j["status"] == "cancelled":
                return "failed", f"the eval of {label} was cancelled (job {i[:8]})"
        if all(j["status"] == "done" for j in jobs):
            return "passed", None
        return "running", None

    def cancel(self, ids: list[str]) -> None:
        """Cancel those not finished, as Farm.cancel does: at once if no agent runs it yet, else
        its agent is asked. One collecting outputs finishes."""
        for i in ids:
            j = self.store.get(i)
            if j is None or j["status"] not in ACTIVE:
                continue
            if j["status"] in ("queued", "preparing"):
                j = self.store.transition(i, ("queued", "preparing"), status="cancelled", stage="cancelled",
                                          finished_at=now(), cancel_requested=1)
            elif j["status"] in ON_WORKER:
                j = self.store.transition(i, ON_WORKER, cancel_requested=1, stage="cancelling")
            if j:
                self.farm._emit(j)
        self.farm.wake()

    def results(self, ids: list[str]) -> list[dict]:
        """Each eval job as `comfyfarm eval` prints it."""
        out = []
        for i in ids:
            j = self.store.get(i) or {"id": i, "status": "gone"}
            ev = (j.get("info") or {}).get("eval") or {}
            out.append({"job": i, "definition": j.get("workflow"), "machine": ev.get("machine"),
                        "status": j["status"], "stage": j.get("stage"), "error": j.get("error"),
                        "checks": ((j.get("outputs") or {}).get("eval") or {}).get("checks")})
        return out
