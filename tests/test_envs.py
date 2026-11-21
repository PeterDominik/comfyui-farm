"""Runner environments (comfyfarm/envs, decision 0024): an environment folder and its
fingerprint, locking it, building this machine's copy, checking it, keeping the current and
the previous build. uv is a stand-in (tests/fake_builder.py), so nothing is downloaded."""

import io
import json
import os
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from comfyfarm import cli
from comfyfarm.envs import build as B
from comfyfarm.envs import spec as S

REPO = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_builder.py"
UV = [sys.executable, str(FAKE), "uv"]
GOOD = ["python", "-c", "import torch; assert torch.cuda.is_available(); print('torch', torch.__version__)"]


def make(root: Path, name: str = "demo", deps=("torch==2.13.0", "numpy==2.5.2"), check=GOOD,
         python: str = "3.12", package: bool = False) -> Path:
    """An environment folder as environments/<name>/ holds it, without its uv.lock."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    listed = "".join(f'    "{d}",\n' for d in deps)
    (folder / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "1"\nrequires-python = "=={python}.*"\n'
        f'dependencies = [\n{listed}]\n\n[tool.uv]\npackage = {str(package).lower()}\n', encoding="utf-8")
    (folder / ".python-version").write_text(python + "\n", encoding="utf-8")
    (folder / "env.yaml").write_text(f"description: a test environment\ncheck: {json.dumps(check)}\n",
                                     encoding="utf-8")
    return folder


class Folders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_folder_reads_as_a_spec(self):
        make(self.root)
        spec = S.load("demo", self.root)
        self.assertEqual((spec.name, spec.python, spec.dependencies),
                         ("demo", "3.12", ["torch==2.13.0", "numpy==2.5.2"]))
        self.assertEqual((spec.description, spec.check, spec.locked), ("a test environment", GOOD, False))
        self.assertEqual(S.names(self.root), ["demo"])

    def test_the_fingerprint_follows_the_files_and_not_their_line_endings(self):
        folder = make(self.root)
        # Bytes, not text: on Windows write_text already writes CRLF.
        text = (folder / "pyproject.toml").read_bytes().replace(b"\r\n", b"\n")
        (folder / "pyproject.toml").write_bytes(text)
        first = S.load("demo", self.root).fingerprint
        self.assertEqual(len(first), 16)
        (folder / "pyproject.toml").write_bytes(text.replace(b"\n", b"\r\n"))      # a Windows checkout
        self.assertEqual(S.load("demo", self.root).fingerprint, first)
        (folder / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        locked = S.load("demo", self.root).fingerprint
        self.assertNotEqual(locked, first)
        (folder / "env.yaml").write_text("check: [python, -c, 'pass']\n", encoding="utf-8")
        self.assertNotEqual(S.load("demo", self.root).fingerprint, locked)

    def test_what_is_wrong_with_a_folder_is_said(self):
        make(self.root, "nopkg", package=True)
        make(self.root, "nocheck", check=[])
        make(self.root, "nopython")
        (self.root / "nopython" / ".python-version").unlink()
        for name, said in (("Bad Name", "isn't an environment name"), ("missing", "there: nocheck, nopkg, nopython"),
                           ("nopkg", "package = false"), ("nocheck", "needs check:"),
                           ("nopython", ".python-version")):
            with self.subTest(name), self.assertRaises(S.SpecError) as e:
                S.load(name, self.root)
            self.assertIn(said, str(e.exception))

    def test_a_lock_made_for_another_pyproject_is_said(self):
        project = {"project": {"name": "demo", "requires-python": "==3.12.*",
                               "dependencies": ["torch==2.13.0", "numpy==2.5.2",
                                                'pillow==12.3.0; sys_platform == "linux"']},
                   "tool": {"uv": {"index": [{"name": "pt", "url": "https://download.pytorch.org/whl/cu130"}],
                                   "sources": {"torch": {"index": "pt"}}}}}
        lock = {"requires-python": "==3.12.*", "package": [
            {"name": "demo", "source": {"virtual": "."}, "metadata": {"requires-dist": [
                {"name": "numpy", "specifier": "==2.5.2"},
                {"name": "pillow", "specifier": "==12.3.0", "marker": "sys_platform == 'linux'"},
                {"name": "torch", "specifier": "==2.13.0", "index": "https://download.pytorch.org/whl/cu130"}]}}]}
        self.assertIsNone(S.lock_differs(project, lock))       # quotes in a marker don't count
        def changed(**kw):
            p = {**project, "project": {**project["project"], **kw}}
            return S.lock_differs(p, lock)
        pillow = 'pillow==12.3.0; sys_platform == "linux"'
        self.assertEqual(changed(dependencies=["torch==2.13.1", "numpy==2.5.2", pillow]), "torch changed (its version)")
        self.assertEqual(changed(dependencies=["torch==2.13.0", pillow]),
                         "numpy is in the lock but no longer in pyproject.toml")
        self.assertEqual(changed(dependencies=[*project["project"]["dependencies"], "scipy==1.17.0"]),
                         "scipy is in pyproject.toml but not in the lock")
        self.assertIn("made for Python ==3.12.*", changed(**{"requires-python": "==3.13.*"}))
        self.assertIn("no entry for the project other", changed(name="other"))
        cu128 = [{"name": "pt", "url": "https://download.pytorch.org/whl/cu128"}]
        moved = {**project, "tool": {"uv": {**project["tool"]["uv"], "index": cu128}}}
        self.assertEqual(S.lock_differs(moved, lock), "torch changed (its index)")

    def test_the_repos_environments_load_and_name_their_indexes(self):
        names = S.names()
        self.assertIn("gpu-test", names)
        for name in names:
            with self.subTest(name):
                spec = S.load(name)
                project = tomllib.loads((spec.folder / "pyproject.toml").read_text(encoding="utf-8"))
                indexes = project["tool"]["uv"].get("index", [])
                # Never the studio's mirror (CLAUDE.md: package index), and torch only from PyTorch's.
                self.assertTrue(any(i["url"] == B.PYPI and i.get("default") for i in indexes), indexes)
                torch = project["tool"]["uv"].get("sources", {}).get("torch")
                if any(d.startswith("torch") for d in spec.dependencies):
                    by_name = {i["name"]: i for i in indexes}
                    self.assertTrue(by_name[torch["index"]]["url"].startswith("https://download.pytorch.org/whl/"))
                    self.assertTrue(by_name[torch["index"]].get("explicit"))
                self.assertEqual(spec.check[0], "python")
                # A changed pyproject.toml comes with its new lock, or CI says so here.
                self.assertTrue(spec.locked, f"{name} has no uv.lock: run `python -m comfyfarm env lock {name}`")
                self.assertIsNone(spec.stale,
                                  f"{name}'s uv.lock: {spec.stale}: run `python -m comfyfarm env lock {name}`")


class Builds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.envs = Path(self.tmp.name) / "environments"
        self.root = Path(self.tmp.name) / "builds"
        self.log = Path(self.tmp.name) / "calls.jsonl"
        patch = mock.patch.dict(os.environ, {"FAKE_BUILDER_LOG": str(self.log), "PYTHONPATH": "/pipeline/python",
                                             "UV_INDEX_URL": "https://mirror.studio/simple"})
        patch.start()
        self.addCleanup(patch.stop)
        self.said = []

    def tearDown(self):
        self.tmp.cleanup()

    def spec(self, **kw):
        make(self.envs, **kw)
        return S.load("demo", self.envs)

    def locked(self, **kw):
        spec = self.spec(**kw)
        B.lock(spec, UV, self.root)
        return S.load("demo", self.envs)

    def calls(self, verb=None):
        if not self.log.exists():
            return []
        lines = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]
        return [c for c in lines if verb is None or c["args"][:1] == [verb]]

    def install(self, spec, **kw):
        return B.install(spec, self.root, UV, log=self.said.append, **kw)

    def test_it_wont_build_without_a_lock(self):
        with self.assertRaises(B.BuildError) as e:
            self.install(self.spec())
        self.assertIn("python -m comfyfarm env lock demo", str(e.exception))
        self.assertIn("commit environments/demo/uv.lock", str(e.exception))
        self.assertEqual(self.calls(), [])

    def test_it_wont_build_a_lock_out_of_date(self):
        spec = self.locked()
        make(self.envs, deps=("torch==2.13.0", "numpy==2.6.0"))        # changed, not locked again
        stale = S.load("demo", self.envs)
        self.assertEqual(stale.stale, "numpy changed (its version)")
        with self.assertRaises(B.BuildError) as e:
            self.install(stale)
        self.assertIn("uv.lock doesn't match its pyproject.toml: numpy changed (its version)", str(e.exception))
        self.assertIn("python -m comfyfarm env lock demo", str(e.exception))
        self.assertEqual(self.calls("sync"), [])
        self.assertIsNone(spec.stale)

    def test_lock_writes_the_folders_uv_lock(self):
        spec = self.spec()
        B.lock(spec, UV, self.root)
        self.assertTrue((spec.folder / "uv.lock").exists())
        self.assertTrue(S.load("demo", self.envs).locked)

    def test_a_build_is_checked_then_made_current(self):
        spec = self.locked()
        meta = self.install(spec)
        fp = spec.fingerprint
        build = self.root / "demo" / fp
        self.assertTrue((build / "ok").exists())
        self.assertEqual(json.loads((build / "farm-env.json").read_text(encoding="utf-8"))["fingerprint"], fp)
        self.assertEqual((meta["name"], meta["python"], meta["copied"]), ("demo", "3.12", False))
        self.assertIn("torch 2.13.0", meta["check"])
        have = B.builds(self.root, "demo")
        self.assertEqual((have.current, have.previous), (fp, None))
        self.assertEqual(B.current_python(self.root, "demo"), B.python_of(build))
        self.assertTrue(B.current_python(self.root, "demo").exists())
        sync = self.calls("sync")[0]
        self.assertEqual(sync["args"][:6], ["sync", "--frozen", "--no-install-project", "--project", str(spec.folder),
                                            "--python"])
        env = sync["env"]
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(build))
        self.assertEqual(env["UV_LINK_MODE"], "hardlink")
        self.assertEqual((env["UV_CACHE_DIR"], env["UV_PYTHON_INSTALL_DIR"], env["UV_PYTHON_PREFERENCE"]),
                         (str(self.root / ".uv-cache"), str(self.root / ".uv-python"), "only-managed"))
        # Neither the pipeline's Python nor the machine's index settings reach uv.
        self.assertEqual((env["PYTHONPATH"], env["UV_INDEX_URL"], env["PYTHONNOUSERSITE"]), (None, None, "1"))

    def test_a_built_version_isnt_built_again_unless_forced(self):
        spec = self.locked()
        self.install(spec)
        self.install(spec)
        self.assertEqual(len(self.calls("sync")), 1)
        self.assertIn(f"demo {spec.fingerprint} is built and checked: current", self.said)
        self.install(spec, force=True)
        self.assertEqual(len(self.calls("sync")), 2)
        self.assertTrue(B.builds(self.root, "demo").info[spec.fingerprint]["ok"])

    def test_the_current_and_the_previous_build_are_kept(self):
        one = self.locked()
        self.install(one)
        two = self.locked(deps=("torch==2.13.0", "numpy==2.5.2", "pillow==12.3.0"))
        self.install(two)
        have = B.builds(self.root, "demo")
        self.assertEqual((have.current, have.previous), (two.fingerprint, one.fingerprint))
        self.assertEqual(sorted(have.info), sorted([one.fingerprint, two.fingerprint]))
        three = self.locked(deps=("torch==2.13.0",))
        self.install(three)
        have = B.builds(self.root, "demo")
        self.assertEqual((have.current, have.previous), (three.fingerprint, two.fingerprint))
        self.assertFalse((self.root / "demo" / one.fingerprint).exists())
        self.assertIn(f"demo: removed the older build {one.fingerprint}", self.said)

    def test_going_back_to_the_previous_version_needs_no_build(self):
        one = self.locked()
        self.install(one)
        two = self.locked(deps=("torch==2.13.0",))
        self.install(two)
        back = self.locked()                                    # the repo went back
        self.assertEqual(back.fingerprint, one.fingerprint)
        self.install(back)
        self.assertEqual(len(self.calls("sync")), 2)
        have = B.builds(self.root, "demo")
        self.assertEqual((have.current, have.previous), (one.fingerprint, two.fingerprint))

    def test_a_build_that_fails_its_check_leaves_the_current_one(self):
        good = self.locked()
        self.install(good)
        bad = self.locked(check=["python", "-c", "raise SystemExit('torch sees no GPU')"])
        with self.assertRaises(B.BuildError) as e:
            self.install(bad)
        self.assertIn("torch sees no GPU", str(e.exception))
        have = B.builds(self.root, "demo")
        self.assertEqual((have.current, have.previous), (good.fingerprint, None))
        self.assertFalse((self.root / "demo" / bad.fingerprint).exists())

    def test_a_failed_sync_leaves_the_current_one(self):
        good = self.locked()
        self.install(good)
        other = self.locked(deps=("torch==2.13.0",))
        with mock.patch.dict(os.environ, {"FAKE_BUILDER_FAIL": "uv sync"}), self.assertRaises(B.BuildError):
            self.install(other)
        self.assertEqual(B.builds(self.root, "demo").current, good.fingerprint)
        self.assertFalse((self.root / "demo" / other.fingerprint).exists())

    def test_it_says_when_uv_copied_instead_of_linking(self):
        with mock.patch.dict(os.environ, {"FAKE_UV_COPIES": "1"}):
            meta = self.install(self.locked())
        self.assertTrue(meta["copied"])

    def test_remove(self):
        self.install(self.locked())
        self.assertTrue(B.remove(self.root, "demo"))
        self.assertFalse((self.root / "demo").exists())
        self.assertFalse(B.remove(self.root, "demo"))
        self.assertIsNone(B.current_python(self.root, "demo"))

    def test_uv_named_in_the_config_is_used_as_it_is(self):
        self.assertEqual(B.get_uv(self.root, ["/opt/uv/bin/uv"]), ["/opt/uv/bin/uv"])
        self.assertFalse((self.root / ".uv-tool").exists())


class Command(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.envs, self.root = tmp / "environments", tmp / "builds"
        make(self.envs)
        for patch in (mock.patch.object(S, "ENVS", self.envs),
                      mock.patch.dict(os.environ, {"COMFYFARM_AGENT_CONFIG": str(tmp / "no-agent.yaml"),
                                                   "FAKE_BUILDER_LOG": ""})):
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["env", *argv])
        return code, out.getvalue()

    def test_lock_install_list_remove(self):
        code, said = self.run_cli("install", "demo", "--dir", str(self.root), "--uv", *UV)
        self.assertEqual(code, 1)
        self.assertIn("STOPPED: demo has no uv.lock", said)
        code, said = self.run_cli("list", "--dir", str(self.root))
        self.assertIn("NOT LOCKED  not built here", said)
        self.assertEqual(self.run_cli("lock", "demo", "--dir", str(self.root), "--uv", *UV)[0], 0)
        code, said = self.run_cli("install", "demo", "--dir", str(self.root), "--uv", *UV)
        self.assertEqual(code, 0, said)
        fp = S.load("demo").fingerprint
        self.assertIn(f"Python: {B.python_of(self.root / 'demo' / fp)}", said)
        code, said = self.run_cli("list", "--dir", str(self.root))
        self.assertIn(f"demo  {fp}  Python 3.12  locked  built, current", said)
        (self.envs / "demo" / "env.yaml").write_text("check: [python, -c, 'pass']\n", encoding="utf-8")
        code, said = self.run_cli("list", "--dir", str(self.root))
        self.assertIn(f"this machine has {fp}, an older version: run `env install demo`", said)
        make(self.envs, deps=("torch==2.13.0",))
        code, said = self.run_cli("list", "--dir", str(self.root))
        self.assertIn("LOCK OUT OF DATE", said)
        self.assertIn("uv.lock doesn't match pyproject.toml: numpy is in the lock but no longer in pyproject.toml",
                      said)
        code, said = self.run_cli("remove", "demo", "--dir", str(self.root))
        self.assertIn("Removed demo's builds", said)

    def test_builds_go_where_agent_yaml_says(self):
        agent = Path(self.tmp.name) / "machine" / "agent.yaml"
        agent.parent.mkdir()
        agent.write_text("name: test-agent\nenvs:\n  dir: envs-here\n  uv: [/opt/uv]\n", encoding="utf-8")
        code, said = self.run_cli("list", "--config", str(agent))
        # Resolved, as the config resolves it (Windows' temp folder can come as a short 8.3 name).
        self.assertIn(f"(builds in {agent.resolve().parent / 'envs-here'})", said)
        from comfyfarm.config import load_agent_config
        cfg = load_agent_config(agent)
        self.assertEqual((cfg.envs.dir, cfg.envs.uv), ("envs-here", ["/opt/uv"]))
        self.assertEqual(load_agent_config(agent).envs.model_dump(), cfg.envs.model_dump())


if __name__ == "__main__":
    unittest.main()
