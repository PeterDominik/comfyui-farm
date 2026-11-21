"""The process runner's supervisor: one per run, between the agent and the command it runs
(decision 0025). Standard library only, and started with `python -I`, so it needs nothing
of the farm's environment or the command's.

    python -I process_run.py <run folder>

The agent writes run.json into the run folder: what to run and for how long.

    {"command": [...], "env": {name: value}, "seconds": 3600, "grace": 15}

The supervisor keeps there:

    alive        locked while it lives: the agent tells a live run from a dead one by the
                 lock, not by a process id that the system may have given to another program
    state.json   {"state": "running", "pid": <the command's>, "started_at": <unix time>}
    log.txt      what the command printed
    exit.json    {"reason": exited | cancelled | timeout | not_started, "code": <exit code>,
                  "seconds": <how long it ran>, "error": <why it didn't start>}

The command runs in the run folder, in its own process group. It stops when a `cancel`
file appears there, or when its time is up. It gets `grace` seconds to stop on its own
(SIGTERM on Linux, CTRL_BREAK on Windows), then it is killed with what it started. The
time limit is kept here, not in the agent, so it holds while the agent restarts."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"
TICK = 0.25


def lock(path: Path):
    """The open, locked file, or None if another process holds the lock. Closing the file
    (or the process ending, however it ends) releases it."""
    f = open(path, "a+b")
    try:
        if WINDOWS:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def alive(run: Path) -> bool:
    """A supervisor holds this run's lock."""
    if not (run / "alive").exists():
        return False
    held = lock(run / "alive")
    if held is None:
        return True
    held.close()
    return False


def write_json(path: Path, data: dict) -> None:
    """Whole or not at all: the agent may read it at any moment."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def _die_with_us() -> None:
    """Linux: the command is killed if this supervisor is (prctl PR_SET_PDEATHSIG), so it
    can't hold the GPU with nobody watching it."""
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)
    except Exception:  # noqa: BLE001 - not Linux, or no prctl: the process group still goes on stop
        pass


def start(command: list, run: Path, env: dict, log) -> subprocess.Popen:
    kw: dict = {"cwd": str(run), "env": env, "stdin": subprocess.DEVNULL, "stdout": log, "stderr": subprocess.STDOUT}
    if WINDOWS:
        # Its own process group, sharing this supervisor's (hidden) console: CTRL_BREAK
        # reaches it and nothing else.
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True              # its own process group: stopped as one
        if sys.platform.startswith("linux"):
            kw["preexec_fn"] = _die_with_us
    return subprocess.Popen(command, **kw)


def _kill_tree(proc: subprocess.Popen) -> None:
    if WINDOWS:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def stop(proc: subprocess.Popen, grace: float) -> None:
    """Ask the command to stop, then kill it and what it started."""
    try:
        if WINDOWS:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=max(grace, 0))
    except subprocess.TimeoutExpired:
        pass
    if proc.poll() is None:
        _kill_tree(proc)


def supervise(run: Path) -> int:
    held = lock(run / "alive")
    if held is None:
        print(f"another supervisor holds {run}", file=sys.stderr)
        return 3
    spec = json.loads((run / "run.json").read_text(encoding="utf-8"))
    env = {**os.environ, **{str(k): str(v) for k, v in (spec.get("env") or {}).items()}}
    seconds, grace = spec.get("seconds"), float(spec.get("grace") or 15)
    t0 = time.monotonic()
    with open(run / "log.txt", "ab") as log:
        try:
            proc = start([str(c) for c in spec["command"]], run, env, log)
        except OSError as e:
            write_json(run / "exit.json", {"reason": "not_started", "code": None, "seconds": 0.0,
                                           "error": f"{spec['command'][0]}: {e.strerror or e}"})
            return 0
        write_json(run / "state.json", {"state": "running", "pid": proc.pid, "started_at": time.time()})
        reason = "exited"
        while proc.poll() is None:
            if (run / "cancel").exists():
                reason = "cancelled"
            elif seconds and time.monotonic() - t0 > float(seconds):
                reason = "timeout"
            if reason != "exited":
                stop(proc, grace)
                break
            time.sleep(TICK)
        code = proc.wait()
        if reason == "exited" and (run / "cancel").exists():
            reason = "cancelled"        # it stopped on its own when asked, as a command should
        if not WINDOWS:
            # Whatever it started and left behind goes with it: it would hold the GPU.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    write_json(run / "exit.json", {"reason": reason, "code": code, "seconds": round(time.monotonic() - t0, 2)})
    held.close()
    return 0


if __name__ == "__main__":
    sys.exit(supervise(Path(sys.argv[1])))
