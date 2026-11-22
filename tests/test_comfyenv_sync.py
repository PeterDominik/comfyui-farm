"""`comfyfarm comfyui sync` (comfyfarm.comfyenv.sync): bringing a machine's ComfyUI to the
farm's environment file. ComfyUI and the git node pack are real git checkouts (of local
repos); pip and the Comfy registry are stood in for."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_comfyenv import Farm, write  # noqa: E402

from comfyfarm.comfyenv import cli as envcli  # noqa: E402
from comfyfarm.comfyenv import machine as M  # noqa: E402
from comfyfarm.comfyenv import spec as S  # noqa: E402
from comfyfarm.comfyenv import sync as Y  # noqa: E402

GIT = shutil.which("git")


def rmtree(path) -> None:
    """Git's objects are read-only on Windows: make them writable to delete them."""
    def writable(func, p, _exc):
        os.chmod(p, 0o700)
        func(p)
    shutil.rmtree(path, onerror=writable)


def sh(*cmd, cwd=None) -> str:
    return subprocess.run([str(c) for c in cmd], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for name, text in files.items():
        write(repo / name, text)
    sh("git", "-C", repo, "add", "-A")
    sh("git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)
    return sh("git", "-C", repo, "rev-parse", "HEAD")


def origin(path: Path, versions: list[dict[str, str]]) -> list[str]:
    path.mkdir(parents=True)
    sh("git", "init", "-q", path)
    return [commit(path, files, f"v{i}") for i, files in enumerate(versions)]


def kj_zip(top: bool = True, extra: dict | None = None) -> bytes:
    files = {"__init__.py": "Y = 5\n", "pyproject.toml": '[project]\nname = "comfyui-kjnodes"\nversion = "1.5.0"\n',
             "requirements.txt": "scipy\n", **(extra or {})}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, t in files.items():
            z.writestr(("comfyui-kjnodes/" if top else "") + n, t)
    return buf.getvalue()


@unittest.skipIf(GIT is None, "needs git")
class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(lambda: os.path.isdir(self.tmp) and rmtree(self.tmp))
        self.farm = f = Farm(self.tmp)
        # ComfyUI: a real checkout of a local "origin", at the older of two commits
        self.core = origin(self.tmp / "origin-comfy", [
            {"comfyui_version.py": '__version__ = "0.32.0"\n', "requirements.txt": "numpy\n",
             ".gitignore": "custom_nodes/\ncustom_nodes.old/\nmodels/\nextra_model_paths.yaml*\n"},
            {"comfyui_version.py": '__version__ = "0.33.0"\n',
             "requirements.txt": "numpy\ncomfyui-frontend-package==1.49.6\n"}])
        shutil.rmtree(f.comfy / ".git")
        sh("git", "init", "-q", f.comfy)
        sh("git", "-C", f.comfy, "remote", "add", "origin", self.tmp / "origin-comfy")
        sh("git", "-C", f.comfy, "fetch", "-q", "origin")
        sh("git", "-C", f.comfy, "checkout", "-q", "-f", self.core[0])
        # LTXVideo: a git pack, one commit behind
        self.ltx = origin(self.tmp / "origin-ltx", [{"__init__.py": "X = 1\n"},
                                                    {"__init__.py": "X = 2\n", "requirements.txt": "einops\n"}])
        nodes = f.comfy / "custom_nodes"
        shutil.rmtree(nodes / "ComfyUI-LTXVideo")
        sh("git", "clone", "-q", self.tmp / "origin-ltx", nodes / "ComfyUI-LTXVideo")
        sh("git", "-C", nodes / "ComfyUI-LTXVideo", "checkout", "-q", self.ltx[0])
        # the hashes the farm's copies have
        ref = self.tmp / "ref-ltx"
        sh("git", "clone", "-q", self.tmp / "origin-ltx", ref)
        ltx_hash = M.source_hash(ref)
        with zipfile.ZipFile(io.BytesIO(kj_zip())) as z:
            z.extractall(self.tmp / "ref-kj")
        kj_hash = M.source_hash(self.tmp / "ref-kj" / "comfyui-kjnodes")
        chord_hash = M.source_hash(f.farmlib / "custom_nodes" / "ComfyUI-Chord")
        shutil.copytree(f.farmlib / "custom_nodes" / "ComfyUI-Chord", nodes / "ComfyUI-Chord")   # shadows the shared one
        self.farm_paths_file = self.tmp / "farm_paths.yaml"
        f.farm_file()
        f.model_paths(f"other_ui:\n  base_path: {f.bank}\n  is_default: false\n  checkpoints: models/checkpoints/\n"
                      "  diffusion_models: |\n    models/diffusion_models\n    models/unet\n  vae: models/vae/\n")
        self.spec = {
            "comfyui": {"version": "0.33.0", "commit": self.core[1], "url": str(self.tmp / "origin-comfy"), "flags": []},
            "packages": {"pyyaml": "0.0.1", "torch": "2.13.0+cu130", "torchvision": "0.28.0+cu130"},
            "libraries": f.libraries,
            "node_packs": {
                "ComfyUI-LTXVideo": {"shared": False, "url": str(self.tmp / "origin-ltx"), "commit": self.ltx[1],
                                     "hash": ltx_hash, "for": ["ltx"]},
                "comfyui-kjnodes": {"shared": False, "version": "1.5.0", "registry": True, "hash": kj_hash,
                                    "for": ["wan"]},
                "ComfyUI-Chord": {"shared": True, "hash": chord_hash, "for": ["chord"]}},
            "models": {"ltx.safetensors": {"folder": "diffusion_models", "library": str(f.bank), "size": 100,
                                           "for": ["ltx"]}},
        }
        self.spec_file = self.tmp / "environment.yaml"
        S.save(self.spec, self.spec_file)
        self.pip_calls = []

    def state(self):
        return M.collect(str(self.farm.comfy), sys.executable, None, libraries=self.farm.libraries,
                         **{k: v for k, v in S.wanted(self.spec).items() if k != "libraries"})

    def plan(self, skip=(), force=False):
        return Y.plan(self.spec, self.state(), S.farm_model_paths(self.farm_paths_file), skip,
                      comfyui_dir=str(self.farm.comfy), python=sys.executable, force=force, stamp="T",
                      farm_file=self.farm_paths_file)

    def fake_pip(self, python, reqs, pins, held, index_url):
        self.pip_calls.append({"reqs": [Path(r) for r in reqs if Path(r).is_file()], "pins": pins, "held": held,
                               "index": index_url})
        return "pip (test)"

    def registry(self):
        def get_json(url):
            self.assertEqual(url, f"{Y.REGISTRY}/nodes/comfyui-kjnodes/install?version=1.5.0")
            return {"id": "x", "version": "1.5.0", "downloadUrl": "https://cdn/kj.zip"}
        return mock.patch.multiple(Y, get_json=get_json, download=lambda url: kj_zip(),
                                   pip_install=self.fake_pip)

    def test_the_plan(self):
        p = self.plan()
        titles = [s.title for s in p.steps]
        self.assertEqual(titles[0], f"ComfyUI 0.32.0 / {self.core[0][:7]} → 0.33.0 / {self.core[1][:7]} (git checkout)")
        self.assertIn(f"ComfyUI-LTXVideo {self.ltx[0][:7]} → {self.ltx[1][:7]} (git checkout)", titles)
        self.assertIn("comfyui-kjnodes 1.1.9 → 1.5.0 (Comfy registry)", titles)
        self.assertTrue([t for t in titles if t.startswith("ComfyUI-Chord: move the local copy")])
        self.assertTrue([t for t in titles if t.startswith("extra_model_paths.yaml: the farm's")])
        self.assertTrue(titles[-1].startswith("pip install pyyaml==0.0.1 + the requirements of what changed"))
        torch = [m for m in p.manual if m.startswith("torch:")]
        self.assertEqual(len(torch), 1)
        self.assertIn("torch==2.13.0+cu130 torchvision==0.28.0+cu130 --index-url https://download.pytorch.org/whl/cu130",
                      torch[0])
        # The 3090 never runs LTX: its pack and models are left alone there.
        titles = [s.title for s in self.plan(skip={"ltx"}).steps]
        self.assertFalse([t for t in titles if "LTXVideo" in t])

    def test_apply(self):
        with self.registry():
            for step in self.plan().steps:
                step.run()
        f = self.farm
        self.assertEqual(M.git_head(f.comfy)["commit"], self.core[1])
        self.assertIn("0.33.0", (f.comfy / "comfyui_version.py").read_text())
        self.assertEqual(M.git_head(f.comfy / "custom_nodes" / "ComfyUI-LTXVideo")["commit"], self.ltx[1])
        kj = f.comfy / "custom_nodes" / "comfyui-kjnodes"
        self.assertEqual(M.pyproject(kj)["version"], "1.5.0")
        self.assertEqual(sorted((kj / ".tracking").read_text().split()), ["__init__.py", "pyproject.toml",
                                                                        "requirements.txt"])
        old = sorted(p.name for p in (f.comfy / "custom_nodes.old").iterdir())
        self.assertEqual(old, ["ComfyUI-Chord.T", "comfyui-kjnodes.T"])              # moved aside, not deleted
        self.assertFalse((f.comfy / "custom_nodes" / "ComfyUI-Chord").exists())
        self.assertTrue((f.comfy / "extra_model_paths.yaml.T.bak").exists())
        [pip] = self.pip_calls
        self.assertEqual(pip["pins"], ["pyyaml==0.0.1"])
        self.assertEqual(pip["index"], Y.PYPI)
        self.assertEqual({r.parent.name for r in pip["reqs"]}, {"ComfyUI", "ComfyUI-LTXVideo", "comfyui-kjnodes"})
        # What's left is what sync doesn't do (pip stood in for here, torch never).
        left = {d["item"] for d in S.compare(self.spec, self.state(), S.farm_model_paths(self.farm_paths_file))["differs"]}
        self.assertEqual(left, {"pyyaml", "torch", "torchvision"})

    def test_local_changes_need_force(self):
        write(self.farm.comfy / "comfyui_version.py", '__version__ = "edited"\n')
        step = self.plan().steps[0]
        with self.assertRaises(Y.SyncError) as e:
            step.run()
        self.assertIn("has local changes", str(e.exception))
        self.plan(force=True).steps[0].run()
        self.assertIn("0.33.0", (self.farm.comfy / "comfyui_version.py").read_text())

    def test_a_pack_that_isnt_a_git_checkout_is_replaced_by_one(self):
        ltx = self.farm.comfy / "custom_nodes" / "ComfyUI-LTXVideo"
        rmtree(ltx / ".git")
        [step] = [s for s in self.plan().steps if "LTXVideo" in s.title]
        self.assertIn("not a git checkout", step.title)
        self.assertIn("git clone", step.title)
        step.run()
        self.assertEqual(M.git_head(ltx)["commit"], self.ltx[1])
        self.assertTrue((self.farm.comfy / "custom_nodes.old" / "ComfyUI-LTXVideo.T").is_dir())

    def test_a_missing_pack_is_cloned(self):
        rmtree(self.farm.comfy / "custom_nodes" / "ComfyUI-LTXVideo")
        [step] = [s for s in self.plan().steps if "LTXVideo" in s.title]
        self.assertIn("not installed", step.title)
        step.run()
        self.assertEqual(M.git_head(self.farm.comfy / "custom_nodes" / "ComfyUI-LTXVideo")["commit"], self.ltx[1])

    def test_nothing_to_do(self):
        with self.registry():
            for step in self.plan().steps:
                step.run()
        self.spec["packages"] = {}
        self.assertEqual(self.plan().steps, [])

    def run_cli(self, *argv, get=None, ping=None):
        from comfyfarm.cli import main
        printed = []
        with mock.patch.dict(os.environ, {"COMFYFARM_AGENT_CONFIG": str(self.tmp / "no-agent.yaml")}), \
                mock.patch.object(envcli, "_get", get or (lambda url, timeout: None)), \
                mock.patch.object(S, "SPEC", self.spec_file), \
                mock.patch.object(S, "FARM_MODEL_PATHS", self.farm_paths_file), \
                mock.patch.object(envcli, "_ping", ping or (lambda cfg: None)), \
                mock.patch("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a)))), \
                self.registry():
            code = main(["comfyui", "sync", "--comfyui", str(self.farm.comfy), "--python", sys.executable,
                         "--url", "http://127.0.0.1:1", *argv])
        return code, "\n".join(printed)

    def test_cli_plan_then_apply(self):
        code, text = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("Nothing changed yet. Run again with --apply", text)
        self.assertEqual(M.git_head(self.farm.comfy)["commit"], self.core[0])
        code, text = self.run_cli("--apply")
        self.assertIn("ComfyUI isn't running", text)
        self.assertIn("Still different:", text)
        self.assertIn("pyyaml", text)
        self.assertEqual(code, 1)                   # pyyaml (pip stood in for) and torch are left
        self.assertEqual(M.git_head(self.farm.comfy)["commit"], self.core[1])

    def test_cli_wont_stop_a_busy_comfyui(self):
        def get(url, timeout):
            return {"queue_running": [[1, "x"]], "queue_pending": []} if url.endswith("/queue") else None
        code, text = self.run_cli("--apply", get=get)
        self.assertEqual(code, 2)
        self.assertIn("ComfyUI has a job in its queue", text)
        self.assertEqual(M.git_head(self.farm.comfy)["commit"], self.core[0])

    def test_cli_wont_stop_a_comfyui_the_agent_didnt_start(self):
        code, text = self.run_cli("--apply", get=lambda url, timeout: {} if url.endswith("/system_stats") else None)
        self.assertEqual(code, 2)
        self.assertIn("the agent didn't start it, so sync won't stop it", text)
        self.assertEqual(M.git_head(self.farm.comfy)["commit"], self.core[0])


class ManagerRunsIt(unittest.TestCase):
    """`comfyui sync --all` on the manager's host asks the manager (decision 0033)."""

    def run_cli(self, *argv, replies=None):
        calls = []

        def manager(args, method, path, body=None):
            calls.append((method, path, body))
            return (replies or {}).get(path, {})
        out = io.StringIO()
        with mock.patch("comfyfarm.envs.cli._manager", manager), redirect_stdout(out), \
                mock.patch.object(S, "fingerprint", lambda: "3f2a"):
            from comfyfarm.cli import main
            code = main(["comfyui", *argv])
        return code, out.getvalue(), calls

    SYNC = {"id": "s1", "runner": "comfyui", "file": "3f2a", "state": "running", "phase": "syncing", "step": 1,
            "machines": ["tor-flame02-a6000", "wrk59467w-5090"], "by": "peter", "error": None,
            "results": {"tor-flame02-a6000": {"state": "done", "note": "synced",
                                              "steps": [["stop ComfyUI", "stopped (pid 7)"]],
                                              "differs": ["torch 2.8.0"], "manual": ["torch: by hand"]},
                        "wrk59467w-5090": {"state": "applying", "step": "1/2: pip install"}}}

    def test_all_names_this_checkouts_file(self):
        code, said, calls = self.run_cli("sync", "--all", replies={"/api/v1/syncs": self.SYNC})
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("POST", "/api/v1/syncs", {"file": "3f2a", "runner": "comfyui", "force": False,
                                                           "machines": None})])
        self.assertIn("s1  comfyui to the farm's environment 3f2a  running at wrk59467w-5090 (syncing)  "
                      "[tor-flame02-a6000 > wrk59467w-5090]  (by peter)", said)
        self.assertIn("    tor-flame02-a6000: done: synced\n      stop ComfyUI: stopped (pid 7)\n"
                      "      still different: torch 2.8.0\n      by hand: torch: by hand\n"
                      "    wrk59467w-5090: applying: 1/2: pip install", said)
        code, said, calls = self.run_cli("sync", "--machine", "wrk59467w-5090", "--force",
                                         replies={"/api/v1/syncs": self.SYNC})
        self.assertEqual(calls[0][2]["machines"], ["wrk59467w-5090"])
        self.assertTrue(calls[0][2]["force"])

    def test_cancel_list_and_not_with_apply(self):
        cancelled = {**self.SYNC, "state": "cancelled"}
        code, _, calls = self.run_cli("sync", "--cancel", replies={"/api/v1/syncs/cancel": cancelled})
        self.assertEqual(calls, [("POST", "/api/v1/syncs/cancel", None)])
        code, said, _ = self.run_cli("syncs", replies={"/api/v1/syncs": {"syncs": [self.SYNC]}})
        self.assertIn("s1  comfyui", said)
        code, said, calls = self.run_cli("sync", "--all", "--apply")
        self.assertEqual((code, calls), (2, []))
        self.assertIn("--apply syncs this machine", said)


class SafetyTests(unittest.TestCase):
    """What the manager must say before sync stops ComfyUI, and the hold it leaves."""

    def cfg(self):
        return SimpleNamespace(name="gpu1", manager="http://m:1", token="")

    def not_safe(self, reply):
        def ping(cfg):
            if isinstance(reply, Exception):
                raise reply
            return reply
        with mock.patch.object(envcli, "_get", lambda url, timeout: None), \
                mock.patch.object(envcli.asyncio, "run", lambda coro: (coro.close(), ping(None))[1]):
            return envcli._not_safe(self.cfg(), "http://c:1", ask_manager=True)

    def test_the_manager_must_say_drained_and_idle(self):
        from comfyfarm.protocol import LinkError
        self.assertIsNone(self.not_safe({"drained": True, "held": 0}))
        self.assertIn("Drain gpu1", self.not_safe({"drained": False, "held": 0}))
        self.assertIn("still holds 2 job(s)", self.not_safe({"drained": True, "held": 2}))
        self.assertIn("an older manager", self.not_safe({"known": True}))
        self.assertIn("--no-manager", self.not_safe(LinkError("down")))

    def test_model_paths_as_each_os_sees_them(self):
        linux = Y.model_paths_text(S.FARM_MODEL_PATHS, S.LIBRARIES, "linux", "2026-11-05")
        win = Y.model_paths_text(S.FARM_MODEL_PATHS, S.LIBRARIES, "windows", "2026-11-05")
        self.assertTrue(linux.startswith("# Written by `comfyfarm comfyui sync`"))
        self.assertEqual([c["base_path"] for c in yaml.safe_load(linux).values()],
                         ["/rdo/bank/comfyui/", "/rdo/bank/comfy_farm/comfyui/"])
        self.assertEqual([c["base_path"] for c in yaml.safe_load(win).values()],
                         ["B:\\comfyui\\", "B:\\comfy_farm\\comfyui\\"])
        self.assertEqual(yaml.safe_load(win)["other_ui"]["loras"], "models/loras/")

    def test_registry_zips(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "ComfyUI" / "custom_nodes" / "comfyui-kjnodes"
        write(target / "old.py", "")
        info = {"downloadUrl": "u"}
        for top in (True, False):
            with mock.patch.multiple(Y, get_json=lambda url: info, download=lambda url, t=top: kj_zip(top=t)):
                Y.registry_install("comfyui-kjnodes", "1.5.0", target, tmp / "ComfyUI", f"T{top}")
            self.assertEqual(sorted(p.name for p in target.iterdir()),
                             [".tracking", "__init__.py", "pyproject.toml", "requirements.txt"])
        self.assertEqual(len(list((tmp / "ComfyUI" / "custom_nodes.old").iterdir())), 2)
        with mock.patch.multiple(Y, get_json=lambda url: info, download=lambda url: kj_zip(extra={"../evil.py": "x"})):
            with self.assertRaises(Y.SyncError):
                Y.registry_install("comfyui-kjnodes", "1.5.0", target, tmp / "ComfyUI", "T3")
        self.assertFalse((tmp / "ComfyUI" / "custom_nodes" / "evil.py").exists())
        with mock.patch.multiple(Y, get_json=lambda url: {}):
            with self.assertRaises(Y.SyncError):
                Y.registry_install("comfyui-kjnodes", "9.9.9", target, tmp / "ComfyUI", "T4")

    def test_torch_command(self):
        cmd = Y.torch_command("py", {"torch": "2.13.0+cu130", "torchaudio": "2.11.0+cu130"}, "linux")
        self.assertEqual(cmd, "py -m pip install torch==2.13.0+cu130 torchaudio==2.11.0+cu130 "
                              "--index-url https://download.pytorch.org/whl/cu130")

    def test_pip_holds_torch(self):
        seen = {}

        def run(cmd, cwd=None, env=None, timeout=0):
            seen["cmd"] = cmd
            seen["constraints"] = Path(cmd[cmd.index("-c") + 1]).read_text()
            seen["env"] = env
            return "Successfully installed pyyaml-0.0.1\n"
        with mock.patch.object(Y, "run", run), mock.patch.dict(os.environ, {"PYTHONPATH": "/rez/stuff"}):
            out = Y.pip_install("py", [], ["pyyaml==0.0.1"], {"torch": "2.13.0+cu130"}, Y.PYPI)
        self.assertEqual(out, "Successfully installed pyyaml-0.0.1")
        self.assertEqual(seen["constraints"], "torch==2.13.0+cu130\n")
        self.assertEqual(seen["cmd"][-1], "pyyaml==0.0.1")
        self.assertIn("--index-url", seen["cmd"])
        self.assertNotIn("PYTHONPATH", seen["env"])                  # the pipeline's Python settings stay out
        self.assertEqual(Y.pip_install("py", [Path("/none/requirements.txt")], [], {}, None), "nothing to install")


if __name__ == "__main__":
    unittest.main()
