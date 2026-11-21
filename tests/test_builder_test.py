"""tools/builder_test.py, phase 3's test of uv against the installers' method: what each
builds, that it runs both and reports them, and how it judges a build. uv, conda and pip
are stand-ins (tests/fake_builder.py), so nothing is downloaded."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().parent / "fake_builder.py"
spec = importlib.util.spec_from_file_location("builder_test", REPO / "tools" / "builder_test.py")
BT = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = BT             # its dataclasses look their module up while it loads
spec.loader.exec_module(BT)

WANT = {"python": "3.12", "torch": ["torch==2.13.0+cu130", "torchvision==0.28.0+cu130"],
        "torch_index": "https://download.pytorch.org/whl/cu130", "small": ["numpy==2.5.2", "pillow==12.3.0"]}


class Plans(unittest.TestCase):
    def test_what_it_builds_comes_from_the_environment_file(self):
        spec = {"python": "3.12", "packages": {"torch": "2.13.0+cu130", "torchvision": "0.28.0+cu130",
                                               "numpy": "2.5.2", "pillow": "12.3.0"}}
        self.assertEqual(BT.wanted(spec, torch=True), WANT)
        self.assertEqual(BT.wanted(spec, torch=False), {**WANT, "torch": [], "torch_index": None})
        repo = BT.wanted(BT.S.load(), torch=True)               # the farm's own file
        self.assertEqual(repo["torch"][0].split("==")[0], "torch")
        self.assertTrue(repo["torch_index"].startswith("https://download.pytorch.org/whl/cu"))

    def test_uv_installs_its_own_python_and_names_both_indexes(self):
        b = BT.plan("uv", "cold", Path("/w"), WANT, {"uv": ["uv"]})
        self.assertEqual([s.name for s in b.steps], ["python", "venv", "torch", "packages", "check"])
        self.assertEqual(b.steps[0].argv, ["uv", "python", "install", "3.12"])
        self.assertEqual(b.steps[1].argv, ["uv", "venv", "--python", "3.12", str(Path("/w/uv-cold"))])
        self.assertEqual(b.steps[2].argv, ["uv", "pip", "install", "--python", BT.PY, *WANT["torch"],
                                           "--index-url", WANT["torch_index"]])
        self.assertEqual(b.steps[3].argv, ["uv", "pip", "install", "--python", BT.PY, "--index-url",
                                           "https://pypi.org/simple", *WANT["small"]])
        env = b.steps[0].env
        self.assertEqual((env["UV_PYTHON_PREFERENCE"], env["UV_NO_CONFIG"]), ("only-managed", "1"))
        self.assertEqual(env["UV_CACHE_DIR"], str(Path("/w/uv-cache")))

    def test_the_installers_method_is_conda_on_linux_and_a_venv_on_windows(self):
        conda = BT.plan("installers", "warm", Path("/w"), WANT, {"conda": ["conda"], "base_python": "py"})
        self.assertEqual(conda.steps[0].argv, ["conda", "create", "-y", "-q", "-p", str(Path("/w/installers-warm")),
                                               "--override-channels", "-c", "conda-forge", "python=3.12"])
        venv = BT.plan("installers", "warm", Path("/w"), WANT, {"base_python": "py"})
        self.assertEqual(venv.steps[0].argv, ["py", "-m", "venv", str(Path("/w/installers-warm"))])
        for b in (conda, venv):
            self.assertEqual(b.steps[1].argv, [BT.PY, "-m", "pip", "install", *WANT["torch"], "--index-url",
                                               WANT["torch_index"]])
            self.assertEqual(b.steps[2].argv, [BT.PY, "-m", "pip", "install", "--prefer-binary", "--index-url",
                                               "https://pypi.org/simple", *WANT["small"]])
            self.assertEqual(b.steps[1].env["PIP_CACHE_DIR"], str(Path("/w/pip-cache")))

    def test_no_torch_leaves_it_out(self):
        b = BT.plan("uv", "cold", Path("/w"), {**WANT, "torch": [], "torch_index": None}, {"uv": ["uv"]})
        self.assertEqual([s.name for s in b.steps], ["python", "venv", "packages", "check"])

    def test_the_installers_conda_is_found_from_the_agents_python(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d, "miniconda3", "Scripts/conda.exe" if BT.WINDOWS else "bin/conda")
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            agent = Path(d, "miniconda3", "envs", "comfyfarm", "bin", "python")
            self.assertEqual(BT.find_conda(str(agent)), [str(exe.resolve())])


class Verdicts(unittest.TestCase):
    GOOD = {"python": "3.12.7", "torch": "2.13.0+cu130", "cuda": True, "gpu": "x", "numpy": "2.5.2", "PIL": "12.3.0",
            "foreign_paths": []}

    def build(self, **check):
        b = BT.Build("uv", "cold", "/w/uv-cold", steps=[BT.Step("check", [], ok=True)])
        b.check = {**self.GOOD, **check}
        return b

    def test_a_good_build_passes(self):
        self.assertEqual(BT.verdict(self.build(), WANT), [])

    def test_what_fails_a_build(self):
        for check, why in (({"python": "3.11.9"}, "Python 3.11.9, not 3.12"),
                           ({"torch": "2.12.0+cu128"}, "torch 2.12.0+cu128, not 2.13.0+cu130"),
                           ({"cuda": False}, "torch doesn't see the GPU"),
                           ({"numpy": "error: no module"}, "numpy: error: no module"),
                           ({"foreign_paths": ["/rdo/rez/packages/x"]}, "paths from outside the environment: "
                                                                         "/rdo/rez/packages/x")):
            with self.subTest(why=why):
                self.assertEqual(BT.verdict(self.build(**check), WANT), [why])
        failed = BT.Build("uv", "cold", "/w", steps=[BT.Step("torch", [], ok=False)])
        self.assertEqual(BT.verdict(failed, WANT), ["step torch failed"])


class Runs(unittest.TestCase):
    """The whole test, both methods, with the stand-ins."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.log = self.tmp / "calls.jsonl"
        leak = self.tmp / "rez"                 # what a pipeline shell would put on PYTHONPATH
        leak.mkdir()
        (leak / "pipeline_leak.py").write_text("")
        env = {"FAKE_BUILDER_LOG": str(self.log), "FAKE_BUILDER_DIR": str(FAKE.parent), "PYTHONPATH": str(leak)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        spec = {"python": "%d.%d" % sys.version_info[:2],      # the stand-in venvs are this Python
                "packages": {"torch": "2.13.0+cu130", "torchvision": "0.28.0+cu130", "numpy": "2.5.2",
                             "pillow": "12.3.0"}}
        patcher = mock.patch.object(BT.S, "load", lambda *a, **k: spec)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *extra):
        argv = ["--work", str(self.tmp / "work"), "--uv", sys.executable, str(FAKE), "uv",
                "--conda", sys.executable, str(FAKE), "conda", *extra]
        with redirect_stdout(io.StringIO()) as out:
            code = BT.main(argv)
        report = json.loads(next((self.tmp / "work").glob("*.json")).read_text(encoding="utf-8"))
        return code, report, out.getvalue()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_both_methods_build_twice_and_pass(self):
        code, report, out = self.main()
        self.assertEqual(code, 0, out)
        for m in ("uv", "installers"):
            r = report["methods"][m]
            with self.subTest(method=m):
                self.assertTrue(r["pass"], json.dumps(r, indent=1))
                self.assertEqual([b["run"] for b in r["builds"]], ["cold", "warm"])
                check = r["builds"][0]["check"]
                self.assertEqual((check["torch"], check["cuda"], check["numpy"], check["PIL"]),
                                 ("2.13.0+cu130", True, "2.5.2", "12.3.0"))
                self.assertEqual(check["foreign_paths"], [])        # the pipeline's PYTHONPATH stays out
                self.assertTrue(all(isinstance(s["seconds"], float) for b in r["builds"] for s in b["steps"]))
                self.assertIsInstance(r["disk_mb"], float)
        self.assertIn("uv: PASS  (uv 0.0.0 (fake))", out)
        self.assertIn("installers: PASS", out)
        # Every step ran without the pipeline's Python settings, and each tool got the indexes.
        calls = self.calls()
        self.assertTrue(calls and all(c["env"]["PYTHONPATH"] is None and c["env"]["PYTHONNOUSERSITE"] == "1"
                                      for c in calls))
        pip = [c["args"] for c in calls if c["tool"] == "pip"]
        self.assertEqual([a[a.index("--index-url") + 1] for a in pip],
                         ["https://download.pytorch.org/whl/cu130", "https://pypi.org/simple"] * 2)
        uv = [c for c in calls if c["tool"] == "uv" and c["args"][:2] == ["pip", "install"]]
        self.assertEqual({c["env"]["UV_PYTHON_PREFERENCE"] for c in uv}, {"only-managed"})
        # Nothing it built is left behind; the report is.
        self.assertEqual(sorted(p.name for p in (self.tmp / "work").iterdir()), [report["facts"]["machine"] + ".json"])

    def test_a_failing_step_fails_its_method_and_says_which(self):
        with mock.patch.dict(os.environ, {"FAKE_BUILDER_FAIL": "uv pip"}):
            code, report, out = self.main("--no-torch", "--keep")
        self.assertEqual(code, 1)
        self.assertFalse(report["methods"]["uv"]["pass"])
        self.assertTrue(report["methods"]["installers"]["pass"])
        cold = report["methods"]["uv"]["builds"][0]
        self.assertEqual([(s["name"], s["ok"]) for s in cold["steps"]],
                         [("python", True), ("venv", True), ("packages", False)])
        self.assertIn("failed on purpose", cold["steps"][-1]["tail"])
        self.assertIn("step packages failed", out)
        self.assertTrue((self.tmp / "work" / "uv-cold").exists())          # --keep

    def test_one_method(self):
        code, report, _ = self.main("--method", "installers", "--no-torch")
        self.assertEqual((code, sorted(report["methods"])), (0, ["installers"]))
        self.assertFalse(any(c["tool"] == "uv" for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
