"""The farm's shared folder (manager.share): manager.json and the client copy, how the
client finds the manager from there, the artists' Nuke installer, and the read
permissions the farm gives what it writes (fileio.outputs.readable).

    python -m unittest tests.test_share -v
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.fileio.outputs import makedirs_readable, readable
from comfyfarm.manager import share

REPO = Path(__file__).resolve().parent.parent
POSIX = os.name != "nt"


def load_client(path: Path):
    spec = importlib.util.spec_from_file_location(f"client_{abs(hash(str(path)))}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dir = self.tmp / "bank/comfy_farm"
        self.cfg = Config.model_validate({"server": {"port": 8123}, "share": {"dir": self.dir.as_posix()},
                                          "workers": []})
        env = {k: os.environ.pop(k) for k in ("COMFYFARM_URL", "COMFYFARM_ADDRESS_FILE") if k in os.environ}
        self.addCleanup(os.environ.update, env)

    def test_manager_json_and_the_client(self):
        info = share.publish(self.cfg)
        self.assertEqual(info["url"], f"http://{share.socket.gethostname()}:8123")
        written = json.loads((self.dir / "manager.json").read_text())
        self.assertEqual((written["url"], written["version"]), (info["url"], info["version"]))
        self.assertTrue((self.dir / "client/comfyfarm_client.py").is_file())
        self.assertTrue((self.dir / "client/nuke/menu.py").is_file())
        self.assertFalse(list(self.dir.rglob("__pycache__")))
        self.assertEqual((self.dir / "client/nuke/menu.py").read_bytes(), (REPO / "client/nuke/menu.py").read_bytes())
        if POSIX:
            for p in [self.dir, self.dir / "manager.json", self.dir / "client/nuke", self.dir / "client/nuke/init.py"]:
                need = 0o555 if p.is_dir() else 0o444
                self.assertEqual(p.stat().st_mode & need, need, p)

    def test_the_next_start_updates_it(self):
        src = self.tmp / "client"
        shutil.copytree(REPO / "client", src)
        share.publish(self.cfg, src)
        (src / "nuke/menu.py").write_text("# changed\n")
        (src / "nuke/comfyfarm_nuke.py").unlink()
        cfg = self.cfg.model_copy(update={"share": self.cfg.share.model_copy(update={"url": "http://elsewhere:9000/"})})
        share.publish(cfg, src)
        self.assertEqual((self.dir / "client/nuke/menu.py").read_text(), "# changed\n")
        self.assertFalse((self.dir / "client/nuke/comfyfarm_nuke.py").exists())   # gone from the client: gone here
        self.assertEqual(json.loads((self.dir / "manager.json").read_text())["url"], "http://elsewhere:9000")

    def test_a_folder_that_cant_be_written_doesnt_stop_the_manager(self):
        (self.tmp / "bank").write_text("a file, not a folder")
        with self.assertLogs("comfyfarm", "ERROR") as logs:
            self.assertIsNone(share.publish(self.cfg))
        self.assertIn("won't find the manager", logs.output[0])
        self.assertIsNone(share.publish(Config.model_validate({"workers": []})))   # no share.dir: nothing

    def test_the_shared_client_finds_the_manager(self):
        share.publish(self.cfg)
        client = load_client(self.dir / "client/comfyfarm_client.py")
        url = f"http://{share.socket.gethostname()}:8123"
        self.assertEqual(client.base_url(), url)
        self.assertEqual(client.address_file(), str(self.dir / "manager.json"))
        (self.dir / "manager.json").write_text(json.dumps({"url": "http://moved:8000"}))
        os.utime(self.dir / "manager.json", (1, 1))                      # a different mtime: read again
        self.assertEqual(client.base_url(), "http://moved:8000")
        os.environ["COMFYFARM_URL"] = "http://mine:8000"                  # set by hand: wins
        self.assertEqual(client.base_url(), "http://mine:8000")
        del os.environ["COMFYFARM_URL"]
        other = self.tmp / "other.json"
        other.write_text(json.dumps({"url": "http://other:8000"}))
        os.environ["COMFYFARM_ADDRESS_FILE"] = str(other)
        self.assertEqual(client.base_url(), "http://other:8000")

    def test_a_client_without_an_address_says_how(self):
        del_env = os.environ.pop("COMFYFARM_ADDRESS_FILE", None)
        client = load_client(REPO / "client/comfyfarm_client.py")       # the repo: no manager.json above it
        self.assertIsNone(client.base_url())
        with self.assertRaisesRegex(client.FarmError, "manager.json"):
            client._base()
        if del_env:
            os.environ["COMFYFARM_ADDRESS_FILE"] = del_env

    def test_artists_nuke_installer(self):
        share.publish(self.cfg)
        script = self.dir / "client/nuke/install_for_me.py"
        home = self.tmp / "home"
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
        init = home / ".nuke/init.py"

        def run(*args):
            r = subprocess.run([sys.executable, str(script), *args], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return r.stdout

        home.mkdir()
        (home / ".nuke").mkdir()
        init.write_text("import nuke\nnuke.pluginAddPath('/my/own/tools')\n")
        self.assertIn("Added", run())
        self.assertIn("Already installed", run())
        lines = init.read_text().splitlines()
        self.assertEqual(lines[:2], ["import nuke", "nuke.pluginAddPath('/my/own/tools')"])   # theirs kept
        ours = [ln for ln in lines if "ComfyFarm" in ln]
        self.assertEqual(len(ours), 1)
        self.assertIn(repr((self.dir / "client/nuke").as_posix()), ours[0])
        self.assertIn("Removed", run("--remove"))
        self.assertEqual(init.read_text().splitlines(), lines[:2])


@unittest.skipUnless(POSIX, "read permissions are set on Linux; on Windows the share decides")
class ReadableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)

    def test_folders_and_files_everyone_can_read(self):
        makedirs_readable(f"{self.tmp}/a/b/c")
        for d in ("a", "a/b", "a/b/c"):
            self.assertEqual(stat.S_IMODE(os.stat(f"{self.tmp}/{d}").st_mode), 0o755, d)
        f = f"{self.tmp}/a/b/c/frame.png"
        Path(f).write_bytes(b"x")
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
        readable(f)
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o644)
        os.chmod(f, 0o664)                                      # group write kept
        readable(f)
        self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o664)
        readable(f"{self.tmp}/missing")                         # not there: nothing


if __name__ == "__main__":
    unittest.main()
