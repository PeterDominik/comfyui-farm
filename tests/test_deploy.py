"""The deploy scripts. Linux: setup.sh (with a stand-in conda), farmctl.sh (start, status,
ensure, stop, with a stand-in Python), install_cron.sh (with a stand-in crontab). Windows:
every PowerShell script parses, and install_startup.ps1 makes its logon shortcut.

Each runs in a copy of the repo's deploy/ folder in a temp dir, so nothing here touches
this checkout's run/, logs/ or your crontab.

    python -m unittest tests.test_deploy -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LINUX = sys.platform.startswith("linux") and shutil.which("bash") and shutil.which("setsid") and shutil.which("flock")

# Stands in for the farm's Python: records how it was run, and stays up like the manager
# or an agent would (pip and the checks just return).
FAKE_PYTHON = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, time
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "calls.jsonl"), "a") as fh:
        fh.write(json.dumps({{"args": sys.argv[1:], "PYTHONPATH": os.environ.get("PYTHONPATH"),
                             "nousersite": os.environ.get("PYTHONNOUSERSITE"), "umask": os.umask(0o022)}}) + "\\n")
    serving = {{"manager", "agent"}} & set(sys.argv) and not {{"check", "--check", "pip"}} & set(sys.argv)
    if serving:
        time.sleep(120)
    """)


def fake_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FAKE_PYTHON)
    path.chmod(0o755)
    return path


@unittest.skipUnless(LINUX, "the Linux deploy scripts (bash, setsid, flock)")
class LinuxDeployTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        shutil.copytree(REPO / "deploy/linux", self.root / "deploy/linux")
        for f in ("requirements.txt", "config.example.yaml", "agent.example.yaml"):
            shutil.copy(REPO / f, self.root / f)
        (self.root / "comfyui_nodes/comfyfarm_nodes").mkdir(parents=True)
        self.py = fake_python(self.root / "env/bin/python")
        self.env = dict(os.environ, PYTHONPATH="/pipeline/rez/python")   # must not reach the farm
        self.addCleanup(self.stop_all)

    def ctl(self, *args, check=True):
        r = subprocess.run(["bash", str(self.root / "deploy/linux/farmctl.sh"), *args], env=self.env,
                           capture_output=True, text=True, timeout=60)
        if check:
            self.assertIn(r.returncode, (0, 3), r.stdout + r.stderr)
        return r

    def calls(self):
        f = self.root / "env/bin/calls.jsonl"
        return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []

    def stop_all(self):
        for role in ("manager", "agent"):
            pid = self.root / f"run/{role}.pid"
            if pid.exists():
                subprocess.run(["bash", str(self.root / "deploy/linux/farmctl.sh"), "stop", role],
                               env=self.env, capture_output=True, timeout=60)

    def test_farmctl_start_ensure_stop(self):
        (self.root / "run").mkdir()
        (self.root / "run/python").write_text(f"{self.py}\n")
        self.assertEqual(self.ctl("status", "agent").returncode, 3)
        r = self.ctl("start", "agent")
        self.assertIn("agent started (pid", r.stdout)
        pid = (self.root / "run/agent.pid").read_text().strip()
        self.assertEqual(self.ctl("status", "agent").stdout.strip(), f"agent is running (pid {pid})")
        self.ctl("ensure", "agent")                              # running: left alone
        self.assertEqual((self.root / "run/agent.pid").read_text().strip(), pid)
        run = self.calls()[-1]
        self.assertEqual(run["args"], ["-m", "comfyfarm", "agent", "--config", str(self.root / "agent.yaml")])
        self.assertEqual((run["PYTHONPATH"], run["nousersite"], run["umask"]), (None, "1", 0o022))
        self.assertIn("starting agent", (self.root / "logs/agent.out").read_text())
        self.assertIn("agent stopped", self.ctl("stop", "agent").stdout)
        self.assertEqual(self.ctl("status", "agent").returncode, 3)
        self.ctl("ensure", "agent")                              # stopped (or crashed): started again
        self.assertEqual(self.ctl("status", "agent").returncode, 0)
        self.ctl("stop", "agent")
        self.ctl("start", "manager")
        self.assertEqual(self.calls()[-1]["args"][-1], "manager")

    def test_farmctl_verify_and_sync(self):
        (self.root / "run").mkdir()
        (self.root / "run/python").write_text(f"{self.py}\n")
        self.ctl("sync", "agent", "--apply")
        run = self.calls()[-1]
        self.assertEqual(run["args"], ["-m", "comfyfarm", "comfyui", "sync", "--config", str(self.root / "agent.yaml"),
                                       "--apply"])
        self.assertIsNone(run["PYTHONPATH"])                     # the pipeline's Python stays out
        self.ctl("verify", "agent")
        self.assertEqual(self.calls()[-1]["args"][3], "verify")
        self.assertEqual(self.ctl("sync", "manager", check=False).returncode, 2)

    def test_farmctl_without_setup_says_so(self):
        r = self.ctl("start", "manager", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("run deploy/linux/setup.sh manager first", r.stderr)

    def test_cron_lines_added_once_and_removed(self):
        (self.root / "run").mkdir()
        (self.root / "run/python").write_text(f"{self.py}\n")
        bin_ = self.root / "bin"
        bin_.mkdir()
        store = self.root / "crontab.txt"
        (bin_ / "crontab").write_text(textwrap.dedent(f"""\
            #!/bin/sh
            if [ "$1" = -l ]; then
              [ -f {store} ] && exec cat {store}
              echo "no crontab for $(id -un)" >&2; exit 1
            fi
            cat > {store}
            """))
        (bin_ / "crontab").chmod(0o755)
        self.env["PATH"] = f"{bin_}{os.pathsep}{self.env['PATH']}"
        cron = ["bash", str(self.root / "deploy/linux/install_cron.sh")]
        for _ in range(2):
            r = subprocess.run([*cron, "agent"], env=self.env, capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
        lines = store.read_text().splitlines()
        ctl = self.root / "deploy/linux/farmctl.sh"
        self.assertEqual(lines, [f"@reboot {ctl} ensure agent >/dev/null 2>&1 # comfyfarm-agent",
                                 f"* * * * * {ctl} ensure agent >/dev/null 2>&1 # comfyfarm-agent"])
        self.assertEqual(self.ctl("status", "agent").returncode, 0)   # and started it
        store.write_text("0 3 * * * my-own-backup\n" + store.read_text())
        r = subprocess.run([*cron, "agent", "--remove"], env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(store.read_text().splitlines(), ["0 3 * * * my-own-backup"])

    def test_setup_makes_the_env_and_the_config(self):
        conda = self.root / "miniconda3"
        (conda / "bin").mkdir(parents=True)
        (conda / "bin/conda").write_text(textwrap.dedent(f"""\
            #!/bin/sh
            # conda create -y -q -p <env> python=3.12
            [ "$1" = create ] || exit 9
            mkdir -p "$5/bin" && cp {self.py} "$5/bin/python"
            """))
        (conda / "bin/conda").chmod(0o755)
        comfy = self.root / "ComfyUI"
        (comfy / "custom_nodes").mkdir(parents=True)
        r = subprocess.run(["bash", str(self.root / "deploy/linux/setup.sh"), "agent", "--conda", str(conda),
                            "--comfyui", str(comfy)], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        env_py = conda / "envs/comfyfarm/bin/python"
        self.assertEqual((self.root / "run/python").read_text().strip(), str(env_py))
        self.assertEqual((self.root / "agent.yaml").read_text(), (REPO / "agent.example.yaml").read_text())
        self.assertFalse((self.root / "config.yaml").exists())
        link = comfy / "custom_nodes/comfyfarm_nodes"
        self.assertEqual(os.readlink(link), str(self.root / "comfyui_nodes/comfyfarm_nodes"))
        calls = [c["args"] for c in (json.loads(x) for x in (env_py.parent / "calls.jsonl").read_text().splitlines())]
        self.assertIn(["-m", "pip", "install", "--prefer-binary", "--index-url", "https://pypi.org/simple", "-r",
                       str(self.root / "requirements.txt")], calls)     # not the pipeline's mirror
        self.assertEqual(calls[-1], ["-m", "comfyfarm", "agent", "--config", str(self.root / "agent.yaml"), "--check"])
        self.assertIn("install_cron.sh agent", r.stdout)
        (self.root / "agent.yaml").write_text("name: mine\n")      # re-run: an edited config is kept
        r = subprocess.run(["bash", str(self.root / "deploy/linux/setup.sh"), "agent", "--conda", str(conda),
                            "--index-url", ""], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.root / "agent.yaml").read_text(), "name: mine\n")
        calls = [c["args"] for c in (json.loads(x) for x in (env_py.parent / "calls.jsonl").read_text().splitlines())]
        self.assertIn(["-m", "pip", "install", "--prefer-binary", "-r", str(self.root / "requirements.txt")],
                      calls)                                            # pip's own setting


# Stands in for both conda envs' Python in install_agent.sh: pip and the farm's commands
# are recorded, torch "is" on a GPU once pip has installed it, and a script on stdin (the
# environment file read) runs in the real Python.
INSTALL_PYTHON = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "calls.jsonl"), "a") as fh:
        fh.write(json.dumps({{"args": sys.argv[1:], "PYTHONPATH": os.environ.get("PYTHONPATH"),
                             "cache": os.environ.get("PIP_CACHE_DIR")}}) + "\\n")
    a = sys.argv[1:]
    if a == ["-"]:
        os.execv({sys.executable!r}, [{sys.executable!r}, "-"])
    if a[:3] == ["-m", "pip", "install"] and any(x.startswith("torch==") for x in a):
        open(os.path.join(here, "torch"), "w").write("2.13.0+cu130")
    if a[:1] == ["-c"] and "importlib.metadata" in a[1]:
        t = os.path.join(here, "torch")
        sys.exit(print(open(t).read()) if os.path.exists(t) else 1)
    if a[:1] == ["-c"] and "torch.cuda" in a[1]:
        print("    torch 2.13.0+cu130 on NVIDIA RTX A6000")
    """)


@unittest.skipUnless(LINUX and shutil.which("git"), "install_agent.sh (bash, git)")
class InstallAgentTests(unittest.TestCase):
    """install_agent.sh from nothing, with stand-ins: Miniconda's conda and both envs'
    Python, nvidia-smi, and ComfyUI (a git checkout with no history). The farm's checkout
    is a copy of this repo's files, so the real environment file is read."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.base = self.tmp / "softwareLocal"
        farm = self.farm = self.base / "comfyui-farm"
        for d in ("deploy", "comfyfarm", "comfyui_nodes"):
            shutil.copytree(REPO / d, farm / d, ignore=shutil.ignore_patterns("__pycache__"))
        for f in ("requirements.txt", "config.example.yaml", "agent.example.yaml"):
            shutil.copy(REPO / f, farm / f)
        self.git(farm, "init", "-q")
        self.git(farm, "add", "-A")
        self.git(farm, "commit", "-q", "-m", "farm")
        comfy = self.base / "comfyui"
        (comfy / "custom_nodes").mkdir(parents=True)
        (comfy / "requirements.txt").write_text("torch\nnumpy\n")
        self.git(comfy, "init", "-q")
        conda = self.base / "miniconda3/bin/conda"
        conda.parent.mkdir(parents=True)
        conda.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            # conda create -y -q -p <env> ... | conda config --show channels
            case "$1" in
              create) mkdir -p "$5/bin" && cp {self.tmp}/python "$5/bin/python" ;;
              config) printf 'channels:\\n  - conda-forge\\n' ;;
              *) exit 9 ;;
            esac
            """))
        conda.chmod(0o755)
        (self.tmp / "python").write_text(INSTALL_PYTHON)
        (self.tmp / "python").chmod(0o755)
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "nvidia-smi").write_text(textwrap.dedent("""\
            #!/bin/sh
            case "$*" in
              *name*) echo "NVIDIA RTX A6000" ;;
              *driver_version*) echo "${FAKE_DRIVER:-580.159.04}" ;;
            esac
            """))
        (bin_ / "nvidia-smi").chmod(0o755)
        storage = [self.tmp / "rdo/shows", self.tmp / "rdo/bank/comfyui", self.tmp / "rdo/bank/comfy_farm"]
        for d in storage:
            d.mkdir(parents=True)
        (self.tmp / "manager.json").write_text('{"url": "http://manager:9090"}')
        (self.tmp / "token").write_text("s3cr'et\n")
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", HOME=str(self.home),
                        PYTHONPATH="/pipeline/rez/python", COMFYFARM_MIN_FREE_GB="0",
                        COMFYFARM_STORAGE=" ".join(map(str, storage)))

    @staticmethod
    def git(folder: Path, *args):
        subprocess.run(["git", "-C", str(folder), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    def install(self, *extra, env=None):
        return subprocess.run(["bash", str(self.farm / "deploy/linux/install_agent.sh"), "--no-start",
                               "--manager", str(self.tmp / "manager.json"), "--token-file", str(self.tmp / "token"),
                               *extra], cwd=self.base, env=env or self.env, capture_output=True, text=True,
                              timeout=300)

    def calls(self, env: str):
        f = self.base / "miniconda3/envs" / env / "bin/calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()]

    def test_from_nothing(self):
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Installed. Not started (--no-start)", r.stdout)
        host = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip()
        name = f"{host}-a6000"
        cpy = self.base / "miniconda3/envs/comfy/bin/python"
        agent = self.farm / "agent.yaml"
        text = agent.read_text()
        for line in (f"name: {name}", f"manager: '{self.tmp / 'manager.json'}'", "token: 's3cr''et'",
                     "comfyui: http://127.0.0.1:8188", f"  command: ['{cpy}', main.py, --port, '8188']",
                     f"  cwd: '{self.base / 'comfyui'}'"):
            self.assertIn(line + "\n", text)
        self.assertEqual(agent.stat().st_mode & 0o777, 0o600)                 # it holds the token
        backup = self.home / f".config/comfyfarm/agent-{name}.yaml"
        self.assertEqual((backup.read_text(), backup.stat().st_mode & 0o777), (text, 0o600))
        self.assertEqual((self.base / "miniconda3/.condarc").read_text(), "channels:\n  - conda-forge\n")
        self.assertEqual(os.readlink(self.base / "comfyui/custom_nodes/comfyfarm_nodes"),
                         str(self.farm / "comfyui_nodes/comfyfarm_nodes"))
        self.assertTrue((self.base / "install_agent.log").read_text().count("==> "))

        comfy = [c["args"] for c in self.calls("comfy")]
        torch = ["-m", "pip", "install", "torch==2.13.0+cu130", "torchvision==0.28.0+cu130",
                 "torchaudio==2.11.0+cu130", "--index-url", "https://download.pytorch.org/whl/cu130"]
        self.assertIn(torch, comfy)                                             # the environment file's pins
        held = self.farm / "run/torch-held.txt"
        self.assertEqual(held.read_text().split(), torch[3:6])
        self.assertIn(["-m", "pip", "install", "--prefer-binary", "--index-url", "https://pypi.org/simple",
                       "-c", str(held), "-r", str(self.base / "comfyui/requirements.txt")], comfy)
        farm = self.calls("comfyfarm")
        sync = ["-m", "comfyfarm", "comfyui", "sync", "--config", str(agent), "--apply", "--no-manager"]
        self.assertEqual([c["args"] for c in farm].count(sync), 2)             # the second pass pins the rest
        self.assertIn(["-m", "comfyfarm", "comfyui", "verify", "--config", str(agent)], [c["args"] for c in farm])
        everything = farm + self.calls("comfy")
        self.assertEqual({c["PYTHONPATH"] for c in everything}, {None})          # the pipeline's stays out
        self.assertEqual({c["cache"] for c in self.calls("comfy")}, {str(self.base / "pip-cache")})

        # Again: an edited agent.yaml is kept, and torch isn't installed twice.
        agent.write_text(text.replace("max_inflight: 2", "max_inflight: 1"))
        r = self.install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("agent.yaml is there: kept as it is", r.stdout)
        self.assertIn("max_inflight: 1", agent.read_text())
        self.assertEqual([c["args"] for c in self.calls("comfy")].count(torch), 1)

        # An NVIDIA driver too old for the farm's torch stops it, saying so.
        r = self.install(env=dict(self.env, FAKE_DRIVER="570.86.10"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("STOPPED: NVIDIA driver 570.86.10: the farm's torch", r.stdout)

    def test_refuses_a_busy_port_and_a_missing_drive(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            r = self.install("--port", str(s.getsockname()[1]))
        self.assertEqual(r.returncode, 1)
        self.assertIn("is in use on this machine (another ComfyUI?)", r.stdout)
        self.assertFalse((self.farm / "agent.yaml").exists())
        r = self.install(env=dict(self.env, COMFYFARM_STORAGE=str(self.tmp / "nope")))
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"{self.tmp / 'nope'} isn't visible from this machine", r.stdout)


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "the Windows deploy scripts")
class WindowsDeployTests(unittest.TestCase):
    def ps(self, command: str) -> subprocess.CompletedProcess:
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                              capture_output=True, text=True, timeout=120)

    def test_every_script_parses(self):
        for f in sorted((REPO / "deploy/windows").glob("*.ps1")):
            r = self.ps("$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                        f"'{f}', [ref]$null, [ref]$e); $e | ForEach-Object {{ $_.ToString() }}")
            self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), f"{f.name}: {r.stdout}{r.stderr}")

    def test_logon_shortcut(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        script = REPO / "deploy/windows/install_startup.ps1"
        r = self.ps(f"& '{script}' -StartupDir '{tmp}' -Config 'D:\\farm\\agent.yaml'")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        link = tmp / "ComfyFarm agent.lnk"
        self.assertTrue(link.is_file(), r.stdout)
        r = self.ps(f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); $s.Arguments")
        self.assertIn(str(REPO / "deploy/windows/run_agent.ps1"), r.stdout)
        self.assertIn("-WindowStyle Hidden", r.stdout)
        self.assertIn('-Config "D:\\farm\\agent.yaml"', r.stdout)
        # Stop and Remove act on this checkout only: the live agent's task (another checkout)
        # isn't this one's, so here they only touch the shortcut.
        r = self.ps(f"& '{script}' -StartupDir '{tmp}' -Stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(link.is_file(), "-Stop keeps the shortcut")
        r = self.ps(f"& '{script}' -StartupDir '{tmp}' -Remove")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(link.exists())
        self.assertIn("no longer starts by itself", r.stdout)

    def test_always_and_at_logon_are_one_or_the_other(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        script = REPO / "deploy/windows/install_startup.ps1"
        r = self.ps(f"& '{script}' -StartupDir '{tmp}' -Always -AtLogon")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Pass -Always or -AtLogon, not both.", r.stdout + r.stderr)
        self.assertEqual(list(tmp.iterdir()), [])

    def test_install_agent_help(self):
        """install_agent.ps1 parses and documents itself (it can't run here: it installs
        Python, ComfyUI and torch)."""
        r = self.ps(f"Get-Help '{REPO / 'deploy/windows/install_agent.ps1'}' -Full | Out-String -Width 200")
        self.assertEqual(r.returncode, 0, r.stderr)
        for word in ("-Always", "-NoStart", "-Python", "comfyui-env"):
            self.assertIn(word, r.stdout)


if __name__ == "__main__":
    unittest.main()
