"""The agent's GPU probe (comfyfarm/agent/gpus.py, decision 0022): reading nvidia-smi, and
saying what went wrong when it can't. nvidia-smi is a stand-in (tests/fake_nvidia_smi.py)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from comfyfarm.agent import gpus as G

FAKE = [sys.executable, str(Path(__file__).resolve().parent / "fake_nvidia_smi.py")]
MIB = 2**20
# As the three pilot machines print it: index, uuid, name, MiB total/used/free, % busy, driver.
A6000 = "0, GPU-6f1c0c1e-aaaa-bbbb-cccc-000000000001, NVIDIA RTX A6000, 49140, 1203, 47425, 3, 580.159.04\n"
R5090 = "0, GPU-1b2c3d4e-aaaa-bbbb-cccc-000000000002, NVIDIA GeForce RTX 5090, 32607, 24500, 7614, 97, 610.47\n"


def nvidia_smi(**spec):
    return mock.patch.dict(os.environ, {"FAKE_NVIDIA_SMI": json.dumps(spec)})


class Parse(unittest.TestCase):
    def test_a_card(self):
        self.assertEqual(G.parse(A6000), [{
            "index": 0, "uuid": "GPU-6f1c0c1e-aaaa-bbbb-cccc-000000000001", "name": "NVIDIA RTX A6000",
            "vram_total": 49140 * MIB, "vram_used": 1203 * MIB, "vram_free": 47425 * MIB, "utilization": 3,
            "driver": "580.159.04"}])

    def test_several_cards_and_what_a_card_doesnt_say(self):
        text = A6000 + "1, GPU-x, NVIDIA GeForce RTX 3090, 24576, [N/A], [N/A], [Not Supported], 580.82.09\n"
        gpus = G.parse(text)
        self.assertEqual([g["index"] for g in gpus], [0, 1])
        self.assertEqual((gpus[1]["vram_total"], gpus[1]["vram_used"], gpus[1]["utilization"]),
                         (24576 * MIB, None, None))

    def test_lines_it_cant_read_are_left_out(self):
        self.assertEqual(G.parse("index, uuid\nNo devices were found\n" + R5090 + "\n")[0]["name"],
                         "NVIDIA GeForce RTX 5090")
        self.assertEqual(G.parse(""), [])


class Probe(unittest.TestCase):
    def test_it_asks_for_each_field_without_units(self):
        with tempfile.TemporaryDirectory() as d, nvidia_smi(out=R5090), \
                mock.patch.dict(os.environ, {"FAKE_NVIDIA_SMI_LOG": os.path.join(d, "log")}):
            gpus, error = G.probe(FAKE)
            args = json.loads(Path(d, "log").read_text(encoding="utf-8"))
        self.assertIsNone(error)
        self.assertEqual(gpus[0]["vram_used"], 24500 * MIB)
        self.assertEqual(args, ["--query-gpu=" + ",".join(G.FIELDS), "--format=csv,noheader,nounits"])

    def test_what_goes_wrong_is_said(self):
        cases = (({"exit": 9, "err": "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA "
                                     "driver.\n"},
                  "failed: NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."),
                 ({"out": "No devices were found\n"}, "listed no GPU"))
        for spec, why in cases:
            with self.subTest(why=why), nvidia_smi(**spec):
                gpus, error = G.probe(FAKE)
                self.assertEqual(gpus, [])
                self.assertIn(why, error)
        with nvidia_smi(sleep=5):
            self.assertEqual(G.probe(FAKE, timeout=0.5), ([], f"{sys.executable} didn't answer in 0.5 s"))
        gpus, error = G.probe(["no-such-nvidia-smi-here"])
        self.assertEqual(gpus, [])
        self.assertIn("isn't installed or isn't on PATH: it comes with the NVIDIA driver", error)


class Check(unittest.TestCase):
    def test_comfyfarm_check_lists_the_cards_or_warns(self):
        import io
        from contextlib import redirect_stdout
        from comfyfarm import cli
        with nvidia_smi(out=R5090), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.check_gpus(FAKE), 0)
        self.assertIn("[ ok ] GPU 0: NVIDIA GeForce RTX 5090, 32 GB, driver 610.47", out.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.check_gpus(["no-such-nvidia-smi-here"]), 0)      # optional: never a problem
        self.assertIn("[WARN] GPUs: no-such-nvidia-smi-here isn't installed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
