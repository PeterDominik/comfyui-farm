"""process.yaml: a head any runner shares plus a runner block, read beside the older
manifest.yaml (docs/decisions/0015). The farm's own workflows, rewritten as process.yaml,
must load as the very same workflows."""

import importlib.util
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from comfyfarm import workflows as WF
from comfyfarm.workflows import Workflow, WorkflowRegistry

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("manifest_to_process", REPO / "tools" / "manifest_to_process.py")
TOOL = importlib.util.module_from_spec(spec)
spec.loader.exec_module(TOOL)


class ProcessYamlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = WorkflowRegistry(REPO / "workflows")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def as_process(self, name: str, edit=None, keep_manifest=False) -> Path:
        """A copy of one of the farm's workflow folders, its manifest rewritten as process.yaml."""
        folder = self.tmp / name
        shutil.copytree(REPO / "workflows" / name, folder)
        m = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
        p = WF.to_process(m)
        if edit:
            edit(p)
        (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        if not keep_manifest:
            (folder / "manifest.yaml").unlink()
        return folder

    def test_the_farms_workflows_are_the_same_written_either_way(self):
        self.assertEqual(len(self.registry.workflows), 5)
        for name, old in self.registry.workflows.items():
            with self.subTest(workflow=name):
                new = Workflow(self.as_process(name))
                self.assertEqual((new.source, old.source, new.runner_type),
                                 ("process.yaml", "manifest.yaml", "comfyui"))
                self.assertEqual(new.problems, [])
                self.assertEqual(new.digest, old.digest)            # no head-only field set: the same definition
                self.assertEqual(new.schema(), old.schema())
                self.assertEqual((new.params, new.ports, new.outputs, new.requires, new.publish, new.node_packs),
                                 (old.params, old.ports, old.outputs, old.requires, old.publish, old.node_packs))
                self.assertEqual(new.template, old.template)
                values = old.validate({k: "/rdo/shows/x/a.%04d.png 1-2" if p.type in WF.INPUT_TYPES else "a subject"
                                       for k, p in old.params.items() if p.required})
                self.assertEqual(new.build(values), old.build(values))
                self.assertEqual((new.job_class, new.targets, new.limits, new.models, new.eval, new.version),
                                 ("interactive", ["pool"], {}, [], None, None))

    def test_the_two_files_keep_the_wiring_in_different_places(self):
        m = yaml.safe_load((REPO / "workflows/wan22_i2i/manifest.yaml").read_text(encoding="utf-8"))
        p = WF.to_process(m)
        self.assertEqual(list(p)[:2], ["id", "label"])
        self.assertEqual(p["id"], "wan22_i2i")
        self.assertEqual(p["params"]["prompt"], {k: v for k, v in m["params"]["prompt"].items() if k != "targets"})
        self.assertEqual(p["runner"]["type"], "comfyui")
        self.assertEqual(p["runner"]["params"]["prompt"], {"targets": m["params"]["prompt"]["targets"]})
        self.assertEqual(p["runner"]["params"]["input"], {"loader": m["params"]["input"]["loader"]})
        self.assertEqual((p["runner"]["template"], p["runner"]["node_packs"], p["runner"]["outputs"]),
                         (m["template"], m["node_packs"], m["outputs"]))
        self.assertNotIn("outputs", p)
        self.assertNotIn("output_path", p["runner"]["params"])      # it drives no node
        back, head = WF.from_process(p)
        self.assertEqual((back, head), (m, {}))

    def test_head_only_fields_are_read_and_change_the_fingerprint(self):
        def head(p):
            p.update({"version": 2, "class": "batch", "targets": ["pool", "tractor"], "limits": {"max_minutes": 45},
                      "models": [{"file": "wan2.2_i2v_low_noise_14B_fp8.safetensors", "licence": "apache-2.0"}],
                      "eval": {"inputs": "eval/a.png", "tolerance": 0.01}})
        wf = Workflow(self.as_process("wan22_i2i", head))
        self.assertEqual(wf.problems, [])
        self.assertEqual((wf.version, wf.job_class, wf.targets, wf.limits["max_minutes"], wf.models[0]["licence"],
                          wf.eval["tolerance"]), (2, "batch", ["pool", "tractor"], 45, "apache-2.0", 0.01))
        self.assertNotEqual(wf.digest, self.registry.workflows["wan22_i2i"].digest)

    def test_bad_head_fields_disable_the_workflow_and_say_why(self):
        def bad(p):
            p.update({"version": "two", "class": "service", "targets": ["cloud"], "limits": {"max_hours": 1},
                      "models": [{"file": "x.safetensors"}], "eval": "yes"})
        wf = Workflow(self.as_process("wan22_i2i", bad))
        self.assertFalse(wf.enabled)
        text = " | ".join(wf.problems)
        for word in ("version:", "class: needs to be one of interactive, batch", "targets:", "limits:", "models:",
                     "eval:"):
            self.assertIn(word, text)

    def test_what_does_not_load(self):
        def other_runner(p):
            p["runner"] = {"type": "process", "env": "py-test", "command": ["python", "run.py"]}

        def wiring_in_the_head(p):
            p["params"]["prompt"]["targets"] = p["runner"]["params"].pop("prompt")["targets"]

        def section_in_the_head(p):
            p["outputs"] = p["runner"].pop("outputs")

        def unknown_param(p):
            p["runner"]["params"]["nope"] = {"targets": []}

        def no_runner(p):
            del p["runner"]
        for edit, why in ((other_runner, "runner type 'process' isn't supported yet"),
                          (wiring_in_the_head, "params.prompt: targets belong under runner.params.prompt"),
                          (section_in_the_head, "outputs: belongs in the runner block"),
                          (unknown_param, "runner.params.nope: no such parameter"),
                          (no_runner, "needs a runner block with a type")):
            with self.subTest(case=edit.__name__):
                with self.assertRaisesRegex(ValueError, why):
                    Workflow(self._fresh(edit))
        with self.assertRaisesRegex(ValueError, "has both process.yaml and manifest.yaml"):
            Workflow(self._fresh(None, keep_manifest=True))

    def _fresh(self, edit, keep_manifest=False) -> Path:
        shutil.rmtree(self.tmp / "wan22_i2i", ignore_errors=True)
        return self.as_process("wan22_i2i", edit, keep_manifest)

    def test_the_registry_loads_both_and_reports_the_ones_it_cannot(self):
        root = self.tmp / "workflows"
        root.mkdir()
        shutil.copytree(REPO / "workflows/chord_pbr", root / "chord_pbr")
        shutil.move(str(self.as_process("wan22_i2i")), root / "wan22_i2i")

        def other_runner(p):
            p["runner"] = {"type": "process"}
        shutil.move(str(self.as_process("sam3_ltx_alpha", other_runner)), root / "sam3_ltx_alpha")
        with self.assertLogs("comfyfarm.workflows", level="ERROR"):
            reg = WorkflowRegistry(root)
        self.assertEqual(sorted(reg.workflows), ["chord_pbr", "wan22_i2i"])
        self.assertEqual({n: w.source for n, w in reg.workflows.items()},
                         {"chord_pbr": "manifest.yaml", "wan22_i2i": "process.yaml"})
        self.assertIn("isn't supported yet", reg.load_errors["sam3_ltx_alpha"])

    def test_the_tool_prints_or_writes_it(self):
        folder = self.tmp / "wan22_i2i"
        shutil.copytree(REPO / "workflows/wan22_i2i", folder)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(TOOL.main([str(folder)]), 0)
        self.assertEqual(WF.from_process(yaml.safe_load(out.getvalue()))[0],
                         yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8")))
        self.assertFalse((folder / "process.yaml").exists())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(TOOL.main([str(folder), "--write"]), 0)
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(TOOL.main([str(folder), "--write"]), 1)        # never over an existing one
            self.assertEqual(TOOL.main([str(self.tmp)]), 1)
        self.assertIn("already there", err.getvalue())
        (folder / "manifest.yaml").unlink()
        self.assertEqual(Workflow(folder).digest, self.registry.workflows["wan22_i2i"].digest)


if __name__ == "__main__":
    unittest.main()
