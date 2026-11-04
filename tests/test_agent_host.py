"""What an agent does on its own machine: start this machine's ComfyUI and keep it
running (agent.launch), and find the manager from the manager.json in the farm's shared
folder (agent.link.HttpLink).

    python -m unittest tests.test_agent_host -v
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from comfyfarm.config import ComfyUILaunchConfig
from comfyfarm.protocol import LinkError

try:
    from comfyfarm.agent.launch import Launcher, pid_alive
    from comfyfarm.agent.link import HttpLink
except ImportError:   # pragma: no cover
    Launcher = None

# The interpreter itself: in a venv on Windows, python.exe is a small launcher that starts
# it as a child, and a launcher started without a console (as the agent starts ComfyUI)
# gives that child a console window of its own.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable
SLEEPER = [PYTHON, "-c", "import time; time.sleep(120)"]


@unittest.skipIf(Launcher is None, "needs the agent's dependencies (httpx, websockets)")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.t = 1000.0
        self.procs = []
        self.addCleanup(self.kill_all)

    def kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
                p.wait(10)

    def launcher(self, command=None, **kw):
        lc = ComfyUILaunchConfig(command=SLEEPER if command is None else command,
                                 **{"restart_after": 30, "startup_seconds": 120, **kw})
        la = Launcher("gpu1", lc, self.tmp / "logs", self.tmp, clock=lambda: self.t)
        return la

    def tick(self, la, reachable, after=0.0):
        self.t += after
        la.tick(reachable)
        if la.proc is not None and la.proc not in self.procs:
            self.procs.append(la.proc)

    def kill(self, la):
        la.proc.kill()
        la.proc.wait(10)

    def test_started_when_the_agent_finds_it_down_and_not_twice(self):
        la = self.launcher()
        self.tick(la, False)
        self.assertEqual(la.starts, 1)
        pid = int(la.pid_file.read_text())
        self.assertTrue(pid_alive(pid))
        self.assertIn("started by agent gpu1", la.log_file.read_text())
        self.tick(la, False, after=5)                       # still starting
        self.tick(la, False, after=200)                     # running but never answered: left alone
        self.assertEqual(la.starts, 1)

    def test_up_then_gone_started_again_after_a_while(self):
        la = self.launcher()
        self.tick(la, False)
        self.tick(la, True, after=20)                       # it answers
        self.assertTrue(la.came_up)
        self.kill(la)                                       # ComfyUI crashes, or someone closes it
        self.tick(la, False, after=100)
        self.tick(la, False, after=20)
        self.assertEqual(la.starts, 1)                      # restart_after (30 s) not over yet
        self.tick(la, False, after=11)
        self.assertEqual(la.starts, 2)

    def test_a_start_that_keeps_dying_backs_off(self):
        la = self.launcher(command=[PYTHON, "-c", "raise SystemExit(3)"])
        self.tick(la, False)
        la.proc.wait(10)
        waits = []
        for _ in range(3):
            n, start = la.starts, self.t
            while la.starts == n:
                self.tick(la, False, after=5)
                if la.proc and la.proc.poll() is None:
                    la.proc.wait(10)
            waits.append(self.t - start)
        # Seen dead at the next check (5 s), then 30, 60, 120 s before the next start.
        self.assertEqual(waits, [35, 65, 125])

    def test_comfyui_the_agent_before_started_is_left_running(self):
        p = subprocess.Popen(SLEEPER, start_new_session=os.name != "nt")
        self.procs.append(p)
        la = self.launcher()
        la.pid_file.parent.mkdir(parents=True)
        la.pid_file.write_text(f"{p.pid}\n")
        self.t = time.time()
        self.tick(la, False)
        self.assertEqual(la.starts, 0)
        p.kill()
        p.wait(10)
        self.tick(la, False, after=40)                      # seen gone...
        self.assertEqual(la.starts, 0)
        self.tick(la, False, after=31)                      # ...and restart_after later, started
        self.assertEqual(la.starts, 1)

    def test_a_pid_file_from_before_the_machine_restarted_is_ignored(self):
        p = subprocess.Popen(SLEEPER)
        self.procs.append(p)
        la = self.launcher()
        la.pid_file.parent.mkdir(parents=True)
        la.pid_file.write_text(f"{p.pid}\n")
        os.utime(la.pid_file, (0, 0))                       # written before this boot
        self.tick(la, False)
        self.assertEqual(la.starts, 1)

    def test_its_output_reaches_the_log_as_it_runs(self):
        old = os.environ.pop("PYTHONUNBUFFERED", None)          # as under cron or a plain shell
        if old is not None:
            self.addCleanup(os.environ.__setitem__, "PYTHONUNBUFFERED", old)
        la = self.launcher(command=[PYTHON, "-c", "import os, time; print('starting', os.environ.get('X')); "
                                                          "time.sleep(60)"], env={"X": "42"})
        self.tick(la, False)
        end = time.time() + 20
        while time.time() < end and "starting 42" not in la.log_file.read_text():
            time.sleep(0.1)
        self.assertIn("starting 42", la.log_file.read_text())        # not held back while it runs

    def test_restart_stops_it_and_starts_it_again(self):
        la = self.launcher()
        self.tick(la, False)
        first = la.proc
        self.assertTrue(la.restart(wait=10))                # for new farm nodes
        self.procs.append(la.proc)
        self.assertIsNotNone(first.poll())                  # the old one is gone...
        self.assertNotEqual(la.proc.pid, first.pid)         # ...and a new one runs
        self.assertTrue(pid_alive(la.proc.pid))
        self.assertEqual((la.starts, la.pid_file.read_text().strip()), (2, str(la.proc.pid)))

    def test_restart_stops_the_comfyui_the_agent_before_started(self):
        p = subprocess.Popen(SLEEPER, start_new_session=os.name != "nt")
        self.procs.append(p)
        la = self.launcher()
        la.pid_file.parent.mkdir(parents=True)
        la.pid_file.write_text(f"{p.pid}\n")
        self.assertTrue(la.restart(wait=10))
        self.procs.append(la.proc)
        p.wait(10)                                          # stopped (the test's own child: reaped here)
        self.assertNotEqual(la.proc.pid, p.pid)

    def test_restart_never_stops_a_comfyui_it_didnt_start(self):
        la = self.launcher()
        self.assertFalse(la.restart())                      # none running that it started
        self.assertEqual(la.starts, 0)
        self.assertFalse(self.launcher(command=[]).restart())

    def test_nothing_without_a_command(self):
        la = self.launcher(command=[])
        self.tick(la, False)
        self.assertEqual((la.starts, la.pid_file.exists()), (0, False))

    def test_a_command_that_cant_run_is_tried_again_later(self):
        la = self.launcher(command=[str(self.tmp / "no-such-python")])
        with self.assertLogs("comfyfarm", "ERROR"):
            self.tick(la, False)
        self.assertEqual(la.starts, 0)
        self.tick(la, False, after=10)
        with self.assertLogs("comfyfarm", "ERROR"):
            self.tick(la, False, after=31)


@unittest.skipIf(Launcher is None, "needs the agent's dependencies (httpx, websockets)")
class AddressFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = self.tmp / "manager.json"

    def test_the_agent_follows_the_manager(self):
        async def go():
            self.file.write_text(json.dumps({"url": "http://ws42:8000/"}))
            link = HttpLink(str(self.file), "tok")
            self.assertEqual(link.shown, f"? (from {self.file})")
            http = await link._client()
            self.assertEqual((link.url, str(http.base_url).rstrip("/")), ("http://ws42:8000", "http://ws42:8000/api/v1/agent"))
            self.assertIs(await link._client(), http)              # unchanged: same connection
            self.file.write_text(json.dumps({"url": "http://ws77:8000"}))
            os.utime(self.file, (5, 5))
            await link._client()
            self.assertEqual(link.url, "http://ws77:8000")         # the manager moved
            self.file.unlink()
            await link._client()
            self.assertEqual(link.url, "http://ws77:8000")         # share unreachable: last address kept
            await link.close()
        asyncio.run(go())

    def test_no_address_yet(self):
        async def go():
            with self.assertRaisesRegex(LinkError, "can't read the manager's address"):
                await HttpLink(str(self.file))._client()
            self.file.write_text("{}")
            with self.assertRaisesRegex(LinkError, "no url"):
                await HttpLink(str(self.file))._client()
            link = HttpLink("http://ws42:8000/")
            await link._client()
            self.assertEqual((link.url, link.shown), ("http://ws42:8000", "http://ws42:8000"))
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
