"""The process runner: any command from a reviewed definition, in one of the farm's runner
environments, with a time limit (base.Runner, decision 0025).

A definition for it is a process.yaml with

    runner:
      type: process
      environment: gpu-test                    # environments/<name>: its current build here
      command: [python, "{definition}/run.py"]

`python` first is the environment's Python. In the command, {definition} is the
definition's folder, {run} the run's folder and {job} its job.json. Nothing of the command
comes from the job: only its parameters and files, in job.json.

Each run has a folder (process_runner.runs_dir/<attempt id>) and a supervisor
(process_run.py) that starts the command there, keeps its time limit and stops it. The
command reads job.json and writes, as it goes:

    events.jsonl   one JSON object a line: {"progress": 0.4, "stage": "denoising 12/30"}
    result.json    optional: {"error": "<for the artist>"} when it fails, and "warnings":
                   ["..."] for the job
    frames         into job.json's output_dir, named <frames_prefix>_<number>.<ext>, the
                   numbers zero-padded so they sort in order (writes.frames)
    other kinds    a file, a movie...: into job.json's writes.<kind>.dir, each named
                   <writes.<kind>.prefix><anything>, with writes.<kind>.ext when it says one
                   (decision 0036); the farm names them after the job

It stops when job.json's `cancel` file appears. Runs outlive the agent: the next agent finds
them again by their folder and the supervisor's lock."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import AgentConfig
from ..definitions import Definition, DefinitionRegistry
from ..envs import build as EB
from ..envs import spec as ES
from ..fileio import inputs as I
from ..fileio import paths as FP
from ..fileio import wire as W
from ..fileio.ocio import REPO
from ..protocol import ON_WORKER
from . import process_run as PR
from .base import Host, RunRejected

log = logging.getLogger("comfyfarm")

SUPERVISOR = Path(PR.__file__).resolve()
PLACEHOLDERS = ("{definition}", "{run}", "{job}")
CONTRACT = 1                            # job.json's shape, for a command that wants to check
KEEP_DAYS = 7                           # run folders left by failed runs, kept to look at


class ProcessDefinition(Definition):
    """A process definition, its runner block checked too. What's wrong here is the same on
    every machine with its fingerprint (`wiring_problems`: broken, decision 0020)."""

    def __init__(self, directory: Path):
        super().__init__(directory)
        self.wiring_problems = self.check_wiring()

    @property
    def environment(self) -> str:
        return str((self.process or {}).get("environment") or "")

    def check_wiring(self) -> list[str]:
        problems = []
        try:
            ES.load(self.environment)
        except ES.SpecError as e:
            problems.append(f"runner.environment: {e}")
        cmd = (self.process or {}).get("command")
        if not (isinstance(cmd, list) and cmd and all(isinstance(c, str) and c for c in cmd)):
            return problems + ['runner.command: needs to be a list of words, e.g. [python, "{definition}/run.py"]']
        for c in cmd:
            for p in re.findall(r"\{[^{}]*\}", c):
                if p not in PLACEHOLDERS:
                    problems.append(f"runner.command: {p} isn't something the farm fills in "
                                    f"(it knows {', '.join(PLACEHOLDERS)})")
        return problems

    def argv(self, python: Path, run: Path) -> list[str]:
        """The command as this machine runs it."""
        cmd = list(self.process["command"])
        if cmd[0] == "python":
            cmd[0] = str(python)
        fill = {"{definition}": str(self.dir), "{run}": str(run), "{job}": str(run / "job.json")}
        out = []
        for c in cmd:
            for k, v in fill.items():
                c = c.replace(k, v)
            out.append(c)
        return out

    def seconds(self, default_minutes: float) -> float:
        """Its time limit: limits.max_minutes, or this machine's process_runner.default_minutes."""
        minutes = (self.limits or {}).get("max_minutes") if isinstance(self.limits, dict) else None
        return float(minutes or default_minutes) * 60


class ProcessRegistry(DefinitionRegistry):
    """The process definitions in a folder: an agent's."""
    KIND = ProcessDefinition
    RUNNER = "process"


@dataclass
class ProcRun:
    """A run's live progress (base.RunState), from its events.jsonl."""
    job_id: str
    proc: subprocess.Popen | None       # the supervisor, when this agent started it
    frac: float = 0.0
    stage: str | None = None
    offset: int = 0                     # how far events.jsonl has been read
    last_push: float = 0.0
    ended: bool = False

    def progress(self) -> float:
        return min(max(self.frac, 0.0), 0.99)

    def nodes(self) -> int | None:
        return None


def with_files(d: dict | None) -> dict | None:
    """An input as job.json gives it: as the farm resolved it, and its `files` in order, with
    their frame `numbers` (None: one image read as it is). A converted input's are the PNGs
    the agent converted it into."""
    if not d:
        return d
    try:
        path, numbers = I.load_path(I.Sequence(**d))
    except (TypeError, KeyError):
        return d                        # a shape this farm doesn't know: the command gets it as it is
    return {**d, "files": [FP.frame_file(path, n) for n in numbers] if numbers else [path], "numbers": numbers}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tail(path: Path, chars: int = 3000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - chars))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


class ProcessRunner:
    label = "process"
    max_runs = 1                        # one run on the card at a time (the GPU claim, decision 0026)

    def __init__(self, cfg: AgentConfig, registry: ProcessRegistry | None = None, tick: float = 0.5):
        self.cfg = cfg
        self.pc = cfg.process_runner
        self.name = cfg.name
        self.host: Host | None = None
        self.runs_dir = Path(cfg.resolve(self.pc.runs_dir))
        self.envs_dir = Path(cfg.resolve(cfg.envs.dir))
        self._registry = registry
        self.tick = tick
        self.abandoned: set[str] = set()
        self._reported: str | None = None       # what processes() said last, to tell the manager of a change
        self._next_look = 0.0

    # ================================================================ the agent's side
    def attach(self, host: Host) -> None:
        self.host = host

    def start(self) -> list:
        if not self.pc.enabled:
            return []
        self._prune()
        return [asyncio.create_task(self._watch_loop(), name=f"process-{self.name}")]

    async def close(self) -> None:
        return None                     # runs carry on; the next agent finds them again

    @property
    def ready(self) -> bool:
        return True                     # nothing to wait for: a run it can't start is refused at submit

    def status(self) -> dict:
        return {"enabled": self.pc.enabled}

    def _definitions(self) -> dict:
        if self._registry is None:
            folder = self.cfg.workflows_dir
            self._registry = ProcessRegistry(self.cfg.resolve(folder) if folder else Path(REPO) / "workflows")
        return self._registry.workflows

    def knows(self, process: str) -> bool:
        return process in self._definitions()

    def environments_in_use(self) -> set[str]:
        """The environments its runs are in: not to be removed or switched under them (decision 0030)."""
        used = set()
        for h in self.host.runs() if self.host else []:
            wf = self._definitions().get(h.job.get("workflow")) if getattr(h, "runner", None) is self else None
            if wf is not None and not getattr(h, "finished", False):
                used.add(wf.environment)
        return used

    def _environment_problem(self, wf: ProcessDefinition) -> str | None:
        """Why this machine has no current build of the definition's environment at the
        version this checkout has (None: it has)."""
        try:
            spec = ES.load(wf.environment)
        except ES.SpecError:
            return None                 # the definition's own problem (broken), said once
        have = EB.builds(self.envs_dir, spec.name)
        if have.current == spec.fingerprint and have.info.get(spec.fingerprint, {}).get("ok"):
            return None
        what = "isn't built" if not have.current else f"is built at another version ({have.current})"
        return (f"the environment {spec.name} {what} on {self.name}: run `python -m comfyfarm env install "
                f"{spec.name}` there")

    def processes(self) -> dict:
        """Each process definition of this checkout, what this machine lacks for it (the
        runner off, its environment not built here) and what's wrong with its runner block."""
        out = {}
        for name, wf in self._definitions().items():
            problems = []
            if not self.pc.enabled:
                problems.append(f"the process runner is off on {self.name} (process_runner.enabled in agent.yaml)")
            else:
                env = self._environment_problem(wf)
                if env:
                    problems.append(env)
            out[name] = {"digest": wf.digest, "problems": problems, "broken": wf.wiring_problems,
                         "expected_nodes": None}
        return out

    async def release(self) -> dict:
        return {"ok": True, "before": None, "after": None, "error": None}     # it keeps nothing on the card

    def _folder(self, pid: str) -> Path:
        return self.runs_dir / pid

    def submitted(self, h) -> dict | None:
        """What the run was started with: its command and job.json, for a person to look at."""
        return h.job.get("process_run")

    async def submit(self, h) -> None:
        wf = self._definitions().get(h.job["workflow"])
        if wf is None:
            raise RunRejected(f"{self.name} has no process {h.job['workflow']}. Update the farm's code on it.")
        if not self.pc.enabled:
            raise RunRejected(f"The process runner is off on {self.name} (process_runner.enabled in agent.yaml).")
        request = h.job.get("request") or {}
        if request.get("definition") != wf.digest:
            raise RunRejected(f"The process {wf.name} on this machine ({wf.digest}) and the one the manager planned "
                              f"the job with ({request.get('definition')}) differ. Update the farm's code on both; a "
                              f"change that was not committed on either one causes this too.")
        if wf.wiring_problems:
            raise RunRejected(f"The process {wf.name} can't run: {'; '.join(wf.wiring_problems)}")
        env = self._environment_problem(wf)
        if env:
            raise RunRejected(env[0].upper() + env[1:] + ".")
        python = EB.current_python(self.envs_dir, wf.environment)
        run = self._folder(h.attempt)
        seconds = wf.seconds(self.pc.default_minutes)
        job = W.localize({
            "contract": CONTRACT, "job": h.id, "attempt": h.attempt, "process": wf.name, "definition": wf.digest,
            "params": request.get("values") or {}, "input": with_files(request.get("seq")),
            "inputs": {k: with_files(v) for k, v in (request.get("inputs") or {}).items()},
            "output_dir": request.get("out_dir"), "frames_prefix": request.get("frames_prefix"),
            "writes": {**({"frames": {"ext": wf.frames_ext}} if wf.frames_ext else {}),
                       **(request.get("writes") or {})},
            "seconds": seconds, "events": str(run / "events.jsonl"), "result": str(run / "result.json"),
            "cancel": str(run / "cancel")}, self.host.view)
        spec = {"command": wf.argv(python, run), "seconds": seconds, "grace": self.pc.grace_seconds,
                "env": {"COMFYFARM_JOB": str(run / "job.json"), "COMFYFARM_RUN": str(run), "PYTHONUNBUFFERED": "1"}}
        try:
            proc = await asyncio.to_thread(self._start, run, job, spec)
        except OSError as e:
            raise RunRejected(f"Couldn't start the process {wf.name} on {self.name}: {e.strerror or e}") from None
        h.job["process_run"] = {"command": spec["command"], "seconds": seconds, "job": job}
        h.pid = h.attempt
        h.rt = ProcRun(job_id=h.id, proc=proc)

    def _start(self, run: Path, job: dict, spec: dict) -> subprocess.Popen:
        shutil.rmtree(run, ignore_errors=True)          # an earlier try of the same attempt
        run.mkdir(parents=True)
        (run / "job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
        (run / "run.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        kw: dict = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True      # not stopped with the agent: it keeps the time limit
        out = open(run / "supervisor.log", "ab")
        try:
            # The interpreter itself, not a venv's launcher, in isolated mode: the supervisor is
            # standard library only, and the pipeline's Python settings stay out (clean_env).
            python = getattr(sys, "_base_executable", None) or sys.executable
            return subprocess.Popen([python, "-I", str(SUPERVISOR), str(run)], cwd=str(run), env=EB.clean_env(),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **kw)
        finally:
            out.close()

    async def cancel(self, h) -> bool:
        """Ask the supervisor to stop it. False: it ends as interrupted, which finishes the cancel."""
        self._ask_to_stop(h.pid)
        return False

    def _ask_to_stop(self, pid: str | None) -> None:
        run = self._folder(pid) if pid else None
        if run is not None and run.is_dir() and not (run / "exit.json").exists():
            try:
                (run / "cancel").touch()
            except OSError:
                pass

    def give_up(self, pid: str) -> None:
        self.abandoned.add(pid)
        self._ask_to_stop(pid)

    async def abandon(self, pid: str) -> None:
        self.give_up(pid)

    async def locate(self, h) -> tuple[str, dict | None]:
        run = self._folder(h.pid)
        if not (run / "run.json").exists():
            return "absent", None
        for _ in range(2):              # once more: it may end between the two looks
            ended = _read_json(run / "exit.json")
            if ended is not None:
                return "finished", ended
            if PR.alive(run):
                return ("running" if (run / "state.json").exists() else "pending"), None
        return "absent", None

    def follow(self, h) -> None:
        h.rt = ProcRun(job_id=h.id, proc=None)

    async def settle(self, h, ended: dict) -> None:
        if h.rt is None:
            self.follow(h)
        await self._end(h, ended)

    async def result(self, h) -> dict | None:
        return _read_json(self._folder(h.pid) / "exit.json")

    def warnings(self, h, result) -> list[str]:
        """What the command said about its job (result.json's warnings), for the job's warnings."""
        said = (_read_json(self._folder(h.pid) / "result.json") or {}).get("warnings")
        return [str(w)[:500] for w in said if str(w).strip()][:20] if isinstance(said, list) else []

    def preview(self, h, result) -> list:
        return []                       # a process doesn't make its own preview video (writes.preview: own)

    def files(self, h, result, kind: str, spec: dict) -> list:
        """What the run wrote of a kind: the files in the folder job.json gave it, starting with
        its prefix (and with its extension, when the definition says one), in name order."""
        folder, prefix, ext = spec.get("dir") or "", spec.get("prefix") or "", spec.get("ext")
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        return [os.path.join(folder, n) for n in sorted(names)
                if n.startswith(prefix) and (not ext or n.lower().endswith("." + ext.lower()))]

    async def fetch(self, entry, dest) -> None:
        """One of its files, renamed: the run wrote it where it goes."""
        await asyncio.to_thread(os.replace, entry, dest)

    async def forget(self, pid: str | None) -> None:
        """A run that went well leaves nothing behind; a failed one's folder is kept a while."""
        if not pid:
            return
        run = self._folder(pid)
        ended = _read_json(run / "exit.json") or {}
        if ended.get("reason") == "exited" and ended.get("code") == 0:
            await asyncio.to_thread(shutil.rmtree, run, True)

    # ================================================================ following runs
    async def _watch_loop(self) -> None:
        while True:
            try:
                for h in self.host.runs():
                    if getattr(h, "runner", None) is self and isinstance(h.rt, ProcRun) and h.status in ON_WORKER:
                        await self._watch(h)
                for pid in list(self.abandoned):
                    if (self._folder(pid) / "exit.json").exists() or not self._folder(pid).exists():
                        self.abandoned.discard(pid)
                        await self.forget(pid)
                await self._report_changes()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("agent %s: following its process runs failed", self.name)
            await asyncio.sleep(self.tick)

    async def _report_changes(self) -> None:
        """An environment built or removed here since the last look (`env install`): the
        manager hears what this machine can run now, without waiting for ComfyUI's next report."""
        if time.monotonic() < self._next_look:
            return
        self._next_look = time.monotonic() + 15
        now_says = json.dumps(await asyncio.to_thread(self.processes), sort_keys=True)
        if self._reported is not None and now_says != self._reported and self.host.registered:
            log.info("agent %s: what its process runner can run changed; telling the manager", self.name)
            await self.host.send_nodes()
        self._reported = now_says

    async def _watch(self, h) -> None:
        rt: ProcRun = h.rt
        if rt.ended or h.pid in self.abandoned:
            return
        run = self._folder(h.pid)
        if h.status == "dispatched" and (run / "state.json").exists():
            await self.host.run_started(h)
        if self._read_events(run, rt):
            await self.host.run_progress(h, rt.stage)
        ended = _read_json(run / "exit.json")
        if ended is None:
            if rt.proc is not None and rt.proc.poll() is not None:
                ended = _read_json(run / "exit.json")      # it may have written it just now
                if ended is None:
                    rt.ended = True
                    await self.host.run_failed(h, f"The farm's supervisor for this run stopped without saying how "
                                                  f"the process ended: see {run / 'supervisor.log'} on {self.name}.")
                    return
            elif rt.proc is None and not PR.alive(run) and not (run / "exit.json").exists():
                rt.ended = True
                await self.host.run_lost(h, f"The process on {self.name} stopped while the agent wasn't watching.")
                return
            else:
                return
        await self._end(h, ended)

    @staticmethod
    def _read_events(run: Path, rt: ProcRun) -> bool:
        """Read what the command wrote since the last look. True if its progress moved."""
        path = run / "events.jsonl"
        try:
            with open(path, "rb") as f:
                f.seek(rt.offset)
                data = f.read()
        except OSError:
            return False
        end = data.rfind(b"\n")
        if end < 0:
            return False                # half a line: read it when it's whole
        rt.offset += end + 1
        moved = False
        for line in data[:end].splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            p = ev.get("progress")
            if isinstance(p, (int, float)) and not isinstance(p, bool):
                rt.frac, moved = float(p), True
            if isinstance(ev.get("stage"), str):
                rt.stage, moved = ev["stage"][:200], True
        return moved

    async def _end(self, h, ended: dict) -> None:
        rt = h.rt
        if rt is not None:
            if rt.ended:
                return
            rt.ended = True
            if rt.proc is not None:     # the supervisor ends just after exit.json: collect it
                self.host.spawn(asyncio.to_thread(self._reap, rt.proc))
        if h.finished or h.dropped:
            return
        run = self._folder(h.pid)
        wf = self._definitions().get(h.job.get("workflow"))
        name = wf.name if wf else h.job.get("workflow")
        reason, code = ended.get("reason"), ended.get("code")
        if reason == "exited" and code == 0:
            await self.host.run_succeeded(h, ended)
        elif reason == "cancelled":
            await self.host.run_interrupted(h)
        elif reason == "timeout":
            minutes = (wf.seconds(self.pc.default_minutes) / 60) if wf else None
            limit = f"{minutes:g} minutes" if minutes else "its time limit"
            await self.host.run_failed(h, f"{name} was stopped after {limit}, its time limit (limits.max_minutes in "
                                          f"its process.yaml, or process_runner.default_minutes). Give it less to do, "
                                          f"or raise the limit in a reviewed change.", traceback=_tail(run / "log.txt"))
        elif reason == "not_started":
            await self.host.run_failed(h, f"{name}'s command couldn't be started on {self.name}: {ended.get('error')}")
        else:
            said = (_read_json(run / "result.json") or {}).get("error")
            if not isinstance(said, str) or not said.strip():
                last = [ln for ln in _tail(run / "log.txt", 2000).splitlines() if ln.strip()]
                said = f"it exited with code {code}" + (f": {last[-1].strip()[:300]}" if last else "")
            await self.host.run_failed(h, f"{name} failed: {said.strip()}", traceback=_tail(run / "log.txt") or None)

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            log.warning("a process run's supervisor (pid %s) didn't end after its run did", proc.pid)

    def _prune(self) -> None:
        """Folders of runs that ended days ago (failed ones are kept to look at)."""
        if not self.runs_dir.is_dir():
            return
        old = time.time() - KEEP_DAYS * 86400
        for run in self.runs_dir.iterdir():
            try:
                if run.is_dir() and (run / "exit.json").exists() and (run / "exit.json").stat().st_mtime < old:
                    shutil.rmtree(run, ignore_errors=True)
            except OSError:
                pass
