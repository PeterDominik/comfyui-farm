"""Environment releases (comfyfarm/envs/release.py, decision 0028): published from the repo
as committed into the bank, never changed there, checked against their hashes, and
installed from a copy on the machine. uv is the stand-in (tests/fake_builder.py).

    python -m unittest tests.test_releases -v
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_envs import UV, make  # noqa: E402

from comfyfarm import cli  # noqa: E402
from comfyfarm.envs import build as B  # noqa: E402
from comfyfarm.envs import release as R  # noqa: E402
from comfyfarm.envs import spec as S  # noqa: E402


def git(repo: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


class Bank(unittest.TestCase):
    """A temp repo with environments/demo, a bank, and a machine's builds folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.envs = self.tmp / "repo" / "environments"
        self.bank = self.tmp / "bank"
        self.builds = self.tmp / "machine" / "envs"
        for patch in (mock.patch.object(S, "ENVS", self.envs),
                      mock.patch.dict(os.environ, {"FAKE_BUILDER_LOG": "",
                                                   "COMFYFARM_AGENT_CONFIG": str(self.tmp / "no-agent.yaml"),
                                                   "COMFYFARM_CONFIG": str(self.tmp / "no-config.yaml")})):
            patch.start()
            self.addCleanup(patch.stop)

    def locked(self, **kw):
        make(self.envs, **kw)
        B.lock(S.load("demo"), UV, self.builds)
        return S.load("demo")


class Releases(Bank):
    def test_a_release_is_the_environments_files_with_their_hashes(self):
        spec = self.locked()
        rel, new = R.publish(spec, self.bank, commit="abc123", by="peter@wrk1125l")
        self.assertTrue(new)
        self.assertEqual(rel.folder, self.bank / "environments" / "demo" / spec.fingerprint)
        self.assertEqual(sorted(os.listdir(rel.folder)),
                         sorted([".python-version", "env.yaml", "pyproject.toml", "release.json", "uv.lock"]))
        meta = json.loads((rel.folder / "release.json").read_text())
        self.assertEqual((meta["name"], meta["fingerprint"], meta["python"], meta["commit"], meta["published_by"]),
                         ("demo", spec.fingerprint, "3.12", "abc123", "peter@wrk1125l"))
        self.assertEqual(meta["files"]["uv.lock"], R.sha(spec.folder / "uv.lock"))
        self.assertEqual([p.name for p in rel.folder.parent.iterdir()], [spec.fingerprint])   # no temp folder left
        again, new = R.publish(spec, self.bank, commit="def456")
        self.assertFalse(new)                       # the same fingerprint is the same release: left as it was
        self.assertEqual(again.meta["commit"], "abc123")

    def test_only_a_locked_environment_whose_lock_matches(self):
        make(self.envs)
        with self.assertRaises(R.ReleaseError) as e:
            R.publish(S.load("demo"), self.bank)
        self.assertIn("has no uv.lock", str(e.exception))
        self.locked()
        make(self.envs, deps=("torch==2.13.0",))
        with self.assertRaises(R.ReleaseError) as e:
            R.publish(S.load("demo"), self.bank)
        self.assertIn("uv.lock doesn't match its pyproject.toml: numpy is in the lock", str(e.exception))
        self.assertFalse((self.bank / "environments" / "demo").exists() and
                         any((self.bank / "environments" / "demo").iterdir()))

    def test_a_release_that_changed_in_the_bank_is_refused(self):
        rel, _ = R.publish(self.locked(), self.bank)
        lock = rel.folder / "uv.lock"
        good = lock.read_bytes()
        lock.write_bytes(good + b"\n# changed\n")
        with self.assertRaises(R.ReleaseError) as e:
            R.load(rel.folder)
        self.assertIn("uv.lock doesn't match its hash", str(e.exception))
        lock.write_bytes(good)
        (rel.folder / "env.yaml").unlink()
        with self.assertRaises(R.ReleaseError) as e:
            R.load(rel.folder)
        self.assertIn("env.yaml doesn't match its hash", str(e.exception))
        shutil.copytree(rel.folder, rel.folder.parent / "0000000000000000")
        with self.assertRaises(R.ReleaseError) as e:
            R.load(rel.folder.parent / "0000000000000000")
        self.assertIn(f"says it is demo {rel.fingerprint}, not demo 0000000000000000", str(e.exception))
        (rel.folder / "release.json").unlink()
        with self.assertRaises(R.ReleaseError) as e:
            R.load(rel.folder)
        self.assertIn("isn't a release", str(e.exception))

    def test_the_latest_and_by_fingerprint(self):
        with self.assertRaises(R.ReleaseError) as e:
            R.find(self.bank, "demo")
        self.assertIn("has no release of demo: publish one with `python -m comfyfarm env publish demo`",
                      str(e.exception))
        one, _ = R.publish(self.locked(), self.bank)
        time.sleep(1.1)                             # published_at is to the second
        two, _ = R.publish(self.locked(deps=("torch==2.13.0",)), self.bank)
        self.assertEqual([r["fingerprint"] for r in R.listed(self.bank, "demo")], [two.fingerprint, one.fingerprint])
        self.assertEqual(R.find(self.bank, "demo").fingerprint, two.fingerprint)
        self.assertEqual(R.find(self.bank, "demo", one.fingerprint).fingerprint, one.fingerprint)
        with self.assertRaises(R.ReleaseError):
            R.find(self.bank, "demo", "ffffffffffffffff")

    def test_installed_from_a_checked_copy_on_the_machine(self):
        rel, _ = R.publish(self.locked(), self.bank, commit="abc123")
        stage = self.builds / ".releases" / "demo" / rel.fingerprint
        spec = R.fetch(rel, stage)
        self.assertEqual((spec.folder, spec.fingerprint, spec.stale), (stage, rel.fingerprint, None))
        meta = B.install(spec, self.builds, UV, log=lambda _m: None,
                         origin={"release": rel.fingerprint, "commit": "abc123"})
        self.assertEqual(meta["origin"], {"release": rel.fingerprint, "commit": "abc123"})
        self.assertEqual(B.builds(self.builds, "demo").current, rel.fingerprint)
        # Changed in the bank between finding it and copying it: the copy is checked, not trusted.
        (rel.folder / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        with self.assertRaises(R.ReleaseError):
            R.fetch(rel, stage)

    def test_a_windows_checkout_publishes_the_same_release(self):
        spec = self.locked()
        for f in S.FILES:
            path = spec.folder / f
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        crlf = S.load("demo")
        self.assertEqual(crlf.fingerprint, spec.fingerprint)
        rel, _ = R.publish(crlf, self.bank)
        self.assertEqual(R.load(rel.folder).fingerprint, spec.fingerprint)


class Commands(Bank):
    """`env publish` on the manager's host, `env releases` and `env install --release` anywhere."""

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["env", *argv])
        return code, out.getvalue()

    def repo(self):
        self.locked()
        repo = self.envs.parent
        git(repo, "init", "-q")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "demo")
        return repo

    def test_publish_takes_the_environment_as_committed(self):
        repo = self.repo()
        code, said = self.run_cli("publish", "demo", "--bank", str(self.bank))
        self.assertEqual(code, 0, said)
        fp = S.load("demo").fingerprint
        self.assertIn(f"Published demo {fp} in", said)
        self.assertEqual(R.find(self.bank, "demo").meta["commit"], git(repo, "rev-parse", "HEAD"))
        code, said = self.run_cli("publish", "demo", "--bank", str(self.bank))
        self.assertIn(f"Already published: demo {fp}", said)
        (self.envs / "demo" / "env.yaml").write_text("check: [python, -c, 'pass']\n", encoding="utf-8")
        code, said = self.run_cli("publish", "demo", "--bank", str(self.bank))
        self.assertEqual(code, 1)
        self.assertIn("STOPPED: demo has changes that aren't committed (environments/demo/env.yaml)", said)

    def test_publish_finds_the_bank_in_the_managers_config(self):
        self.repo()
        config = self.tmp / "config.yaml"
        config.write_text(f"share:\n  dir: {json.dumps(str(self.bank))}\n", encoding="utf-8")
        code, said = self.run_cli("publish", "demo", "--manager-config", str(config))
        self.assertEqual(code, 0, said)
        self.assertEqual(len(R.listed(self.bank, "demo")), 1)

    def test_an_agent_lists_and_installs_a_release(self):
        self.repo()
        self.run_cli("publish", "demo", "--bank", str(self.bank))
        fp = S.load("demo").fingerprint
        agent = self.tmp / "machine" / "agent.yaml"
        agent.parent.mkdir(parents=True, exist_ok=True)
        # As on a farm machine: it finds the manager, and so the bank, through manager.json.
        agent.write_text(f"name: gpu1\nmanager: {json.dumps(str(self.bank / 'manager.json'))}\n"
                         f"envs:\n  dir: envs\n  uv: {json.dumps(UV)}\n", encoding="utf-8")
        code, said = self.run_cli("releases", "--config", str(agent))
        self.assertEqual(code, 0, said)
        self.assertIn(f"demo  {fp}  Python 3.12  published", said)
        code, said = self.run_cli("install", "demo", "--release", "latest", "--config", str(agent))
        self.assertEqual(code, 0, said)
        self.assertIn(f"demo {fp}: published", said)
        info = B.builds(self.builds, "demo").info[fp]
        self.assertEqual(info["origin"]["release"], fp)
        self.assertFalse((self.builds / ".releases" / "demo" / fp).exists())      # the copy goes once built
        code, said = self.run_cli("install", "demo", "--release", "ffffffffffffffff", "--config", str(agent))
        self.assertEqual(code, 1)
        self.assertIn("has no release demo ffffffffffffffff", said)

    def test_without_a_bank_it_says_how_to_name_one(self):
        code, said = self.run_cli("releases")
        self.assertEqual(code, 1)
        self.assertIn("STOPPED: say where the bank is: --bank", said)


if __name__ == "__main__":
    unittest.main()
