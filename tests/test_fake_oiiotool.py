"""The oiiotool stand-in itself. Many tests count its log lines, and the farm runs several
conversions at once, so the log must keep every call whole on Linux and on Windows."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

FAKE = Path(__file__).resolve().parent / "fake_oiiotool.py"


class LogTests(unittest.TestCase):
    def test_calls_made_at_once_are_all_logged_whole(self):
        calls = 32
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "calls.jsonl")
            env = {**os.environ, "FAKE_OIIOTOOL_LOG": log}
            procs = [subprocess.Popen([sys.executable, str(FAKE), os.path.join(d, f"in.{i:04d}.exr"), "--ch", "R,G,B",
                                       "-o", os.path.join(d, f"out.{i:04d}.png")], env=env) for i in range(calls)]
            self.assertEqual([p.wait(timeout=120) for p in procs], [0] * calls)
            lines = Path(log).read_text(encoding="utf-8").splitlines()
            outs = sorted(json.loads(line)["args"][-1] for line in lines)        # every line parses
            self.assertEqual(outs, sorted(os.path.join(d, f"out.{i:04d}.png") for i in range(calls)))
            self.assertFalse(os.path.exists(log + ".lock"))
            self.assertEqual(len(list(Path(d).glob("out.*.png"))), calls)


if __name__ == "__main__":
    unittest.main()
