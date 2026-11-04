"""Keeping this machine's ComfyUI running (`comfyui_launch` in agent.yaml).

The agent's health loop tells the launcher whether ComfyUI answers. When it doesn't, and
no ComfyUI the launcher started is still running, it starts one: at once when the agent
starts, else after `restart_after` seconds (longer after a start that died quickly). The
ComfyUI it starts runs on its own (its own session/process group), logging to
logs/comfyui-<name>.log, so stopping or restarting the agent leaves it and its jobs
running; its pid is kept in logs/comfyui-<name>.pid for the next agent to find."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("comfyfarm")

BACKOFF_MAX = 600.0


def boot_time() -> float:
    """When this machine started (0 if unknown): a pid file older than that is stale."""
    try:
        if sys.platform.startswith("linux"):
            with open("/proc/stat") as fh:
                for line in fh:
                    if line.startswith("btime "):
                        return float(line.split()[1])
        elif os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_uint64
            return time.time() - ctypes.windll.kernel32.GetTickCount64() / 1000.0
    except (OSError, ValueError, AttributeError):
        pass
    return 0.0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                       # someone else's: it's running
    return True


class Launcher:
    def __init__(self, name: str, launch, log_dir: Path, base_dir: Path, clock=time.time):
        self.name = name
        self.lc = launch                                  # config.ComfyUILaunchConfig
        self.log_dir = Path(log_dir)
        self.base_dir = Path(base_dir)
        self.pid_file = self.log_dir / f"comfyui-{name}.pid"
        self.log_file = self.log_dir / f"comfyui-{name}.log"
        self.clock = clock
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.came_up = False              # the last start answered at some point
        self.dead_since: float | None = None
        self.backoff = 0.0
        self.starts = 0
        self._seen = False
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.lc and self.lc.command)

    def running_pid(self) -> int | None:
        """The ComfyUI this launcher (or the agent before it) started, if it still runs."""
        if self.proc is not None:
            return self.proc.pid if self.proc.poll() is None else None   # poll() reaps it
        try:
            st = self.pid_file.stat()
            if st.st_mtime < boot_time() - 5:
                return None                               # from before the machine restarted
            pid = int(self.pid_file.read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None
        if not pid_alive(pid):
            return None
        if self.started_at is None:                       # started by the agent before this one
            self.started_at = st.st_mtime
            self.came_up = self.clock() - st.st_mtime > self.lc.startup_seconds
        return pid

    def tick(self, reachable: bool) -> None:
        """Called after every health check of ComfyUI."""
        if not self.enabled:
            return
        t = self.clock()
        first, self._seen = not self._seen, True
        if reachable:
            if self.started_at is not None and not self.came_up:
                self.came_up, self.backoff = True, 0.0
                log.info("ComfyUI on %s is up, %.0f s after starting it", self.name, t - self.started_at)
            self.dead_since, self._warned = None, False
            return
        pid = self.running_pid()
        if pid is not None:
            self.dead_since = None
            if self.started_at is not None and t - self.started_at < self.lc.startup_seconds:
                return                                    # still starting
            if not self._warned:
                self._warned = True
                log.warning("ComfyUI on %s (pid %s) is running but doesn't answer; not starting another. "
                            "Stop it to have it started again.", self.name, pid)
            return
        if self.dead_since is None:
            self.dead_since = t
            if self.started_at is not None and not self.came_up:   # died before it ever answered
                self.backoff = min(max(self.backoff * 2, self.lc.restart_after), BACKOFF_MAX)
        wait = 0.0 if first else max(self.lc.restart_after, self.backoff)
        if t - self.dead_since >= wait:
            self.start()

    def start(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        cwd = self.lc.cwd or None
        if cwd and not os.path.isabs(cwd):
            cwd = str(self.base_dir / cwd)
        env = dict(os.environ, **self.lc.env)
        kw: dict = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL, "stderr": subprocess.STDOUT}
        if os.name == "nt":
            kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True                # not stopped with the agent
        try:
            with open(self.log_file, "ab") as out:
                out.write(f"\n===== started by agent {self.name} at {time.ctime()}: {' '.join(self.lc.command)}\n"
                          .encode())
                out.flush()
                self.proc = subprocess.Popen(list(self.lc.command), stdout=out, **kw)
        except OSError as e:
            log.error("couldn't start ComfyUI on %s (%s): %s", self.name, " ".join(self.lc.command), e)
            self.proc, self.started_at, self.came_up = None, self.clock(), False
            self.dead_since = None
            return
        self.started_at, self.came_up, self.dead_since = self.clock(), False, None
        self.starts += 1
        self.pid_file.write_text(f"{self.proc.pid}\n")
        log.info("started ComfyUI on %s (pid %s), output in %s", self.name, self.proc.pid, self.log_file)


__all__ = ["Launcher", "pid_alive", "boot_time"]
