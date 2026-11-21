"""process.yaml: a head any runner shares plus a runner block, read beside the older
manifest.yaml (docs/decisions/0015, 0019). The farm's own workflows are process.yaml; each
must load as the very same workflow from the manifest it equals."""

import copy
import importlib.util
import json
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from comfyfarm import definitions as D
from comfyfarm.runners import comfyui_workflow as CW
from comfyfarm.runners.comfyui_workflow import Workflow, WorkflowRegistry

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

    @staticmethod
    def process_of(name: str) -> dict:
        """One of the farm's workflows, as its process.yaml says it."""
        return yaml.safe_load((REPO / "workflows" / name / "process.yaml").read_text(encoding="utf-8"))

    def manifest_of(self, name: str) -> dict:
        """The manifest.yaml that one of the farm's workflows is equal to."""
        m, head = D.from_process(self.process_of(name))
        self.assertEqual(head, {})
        return m

    def as_process(self, name: str, edit=None, keep_manifest=False) -> Path:
        """A copy of one of the farm's workflow folders, its process.yaml edited."""
        folder = self.tmp / name
        shutil.copytree(REPO / "workflows" / name, folder)
        p = copy.deepcopy(self.process_of(name))
        if edit:
            edit(p)
        (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        if keep_manifest:
            m = self.manifest_of(name)
            (folder / "manifest.yaml").write_text(yaml.safe_dump(m, sort_keys=False), encoding="utf-8")
        return folder

    def as_manifest(self, name: str) -> Path:
        """A copy of one of the farm's workflow folders, written as the manifest.yaml it equals."""
        folder = self.tmp / f"{name}-manifest" / name
        shutil.copytree(REPO / "workflows" / name, folder)
        (folder / "manifest.yaml").write_text(yaml.safe_dump(self.manifest_of(name), sort_keys=False), encoding="utf-8")
        (folder / "process.yaml").unlink()
        return folder

    def test_the_farms_workflows_are_the_same_written_either_way(self):
        self.assertEqual(len(self.registry.workflows), 5)
        for name, new in self.registry.workflows.items():
            with self.subTest(workflow=name):
                old = Workflow(self.as_manifest(name))
                self.assertEqual((new.source, old.source, new.runner_type),
                                 ("process.yaml", "manifest.yaml", "comfyui"))
                self.assertEqual((new.problems, old.problems), ([], []))
                self.assertEqual(new.digest, old.digest)            # no head-only field set: the same definition
                # What the converter writes for that manifest is the farm's file.
                self.assertEqual(CW.to_process(self.manifest_of(name)), self.process_of(name))
                self.assertEqual(new.schema(), old.schema())
                self.assertEqual((new.params, new.ports, new.outputs, new.requires, new.publish, new.node_packs),
                                 (old.params, old.ports, old.outputs, old.requires, old.publish, old.node_packs))
                self.assertEqual(new.template, old.template)
                values = old.validate({k: "/rdo/shows/x/a.%04d.png 1-2" if p.type in D.INPUT_TYPES else "a subject"
                                       for k, p in old.params.items() if p.required})
                self.assertEqual(new.build(values), old.build(values))
                self.assertEqual((new.job_class, new.targets, new.limits, new.models, new.eval, new.version),
                                 ("interactive", ["pool"], {}, [], None, None))

    def test_the_two_files_keep_the_wiring_in_different_places(self):
        m = self.manifest_of("wan22_i2i")
        p = CW.to_process(m)
        self.assertEqual(list(p)[:2], ["id", "label"])
        self.assertEqual(p["id"], "wan22_i2i")
        self.assertEqual(p["params"]["prompt"], {k: v for k, v in m["params"]["prompt"].items() if k != "targets"})
        self.assertEqual(p["runner"]["type"], "comfyui")
        self.assertEqual(p["runner"]["params"]["prompt"], {"targets": m["params"]["prompt"]["targets"]})
        self.assertEqual(p["runner"]["params"]["input"], {"loader": m["params"]["input"]["loader"]})
        self.assertEqual((p["runner"]["template"], p["runner"]["node_packs"]), (m["template"], m["node_packs"]))
        # What it writes in the head, which node writes it in the runner block (decision 0019).
        frames = dict(m["outputs"]["frames"])
        self.assertEqual(p["writes"], {"frames": {"ext": frames.pop("ext")}})
        self.assertEqual(p["runner"]["outputs"], {"frames": frames})
        self.assertNotIn("outputs", p)
        self.assertNotIn("output_path", p["runner"]["params"])      # it drives no node
        self.assertNotIn("own_folder", p["params"]["input"])        # ComfyFarm Load Image gets its files
        back, head = D.from_process(p)
        self.assertEqual((back, head), (m, {}))

    def test_texture_maps_by_channel_in_the_head_their_nodes_in_the_runner_block(self):
        m = self.manifest_of("chord_pbr")
        p = CW.to_process(m)
        tex = p["writes"]["textures"]
        self.assertEqual(sorted(tex), sorted({"maps", "name", "ext", "batch", "dir"} & set(m["textures"])))
        self.assertEqual([mp["channel"] for mp in tex["maps"]], [mp["channel"] for mp in m["textures"]["maps"]])
        self.assertFalse({"node", "class"} & {k for mp in tex["maps"] for k in mp})
        self.assertEqual(p["runner"]["textures"]["maps"],
                         {mp["channel"]: {"node": mp["node"], "class": mp["class"]} for mp in m["textures"]["maps"]})
        self.assertEqual(p["runner"]["outputs"], m["outputs"])      # root_node only: it writes no frames
        self.assertNotIn("frames", p["writes"])
        self.assertEqual(D.from_process(p), (m, {}))

    def test_a_loader_that_reads_a_whole_folder_says_so_in_the_head(self):
        def vhs(p):
            p["runner"]["template"] = "workflow_api.json"
            p["runner"]["params"]["input"]["loader"] = {"node": "1", "class": "VHS_LoadImagesPath"}
        folder = self._from("sam3_ltx_alpha", vhs, template=lambda t: t["1"].update(
            class_type="VHS_LoadImagesPath", inputs={"directory": "", "skip_first_images": 0, "image_load_cap": 0}))
        wf = Workflow(folder)
        self.assertIn("param input: its loader VHS_LoadImagesPath reads every file in the input's folder: say "
                      "own_folder: true, so the farm gives it a folder of its own", wf.problems)
        p = yaml.safe_load((folder / "process.yaml").read_text(encoding="utf-8"))
        p["params"]["input"]["own_folder"] = True
        (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        wf = Workflow(folder)
        self.assertEqual(wf.problems, [])
        self.assertTrue(wf.port.reads_folder)
        # A manifest says it too (decision 0020), and the converter adds it for one that doesn't.
        m, _ = D.from_process(p)
        (folder / "process.yaml").unlink()
        (folder / "manifest.yaml").write_text(yaml.safe_dump(m), encoding="utf-8")
        self.assertEqual(Workflow(folder).problems, [])
        self.assertTrue(Workflow(folder).port.reads_folder)
        del m["params"]["input"]["own_folder"]
        self.assertIs(CW.to_process(m)["params"]["input"]["own_folder"], True)
        (folder / "manifest.yaml").write_text(yaml.safe_dump(m), encoding="utf-8")
        self.assertIn("param input: its loader VHS_LoadImagesPath reads every file in the input's folder: say "
                      "own_folder: true, so the farm gives it a folder of its own", Workflow(folder).problems)

    def test_own_folder_is_checked(self):
        for value, problem in ((False, "its loader ComfyFarmLoadImages reads"), ("yes", "true or false")):
            with self.subTest(own_folder=value):
                wf = Workflow(self._fresh(lambda p: p["params"]["input"].update(own_folder=value)))
                if value is False:      # said false for a loader that reads its files: fine, and not a folder
                    self.assertEqual((wf.problems, wf.port.reads_folder), ([], False))
                else:
                    self.assertIn(problem, " | ".join(wf.problems))
        wf = Workflow(self._fresh(lambda p: p["params"]["prompt"].update(own_folder=True)))
        self.assertIn("param prompt: own_folder is for inputs (type: input)", wf.problems)

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

        def preview_in_the_head(p):
            p["preview"] = False

        def frames_without_their_node(p):
            del p["runner"]["outputs"]

        def frames_node_without_writes(p):
            del p["writes"]

        def ext_in_the_runner_block(p):
            p["runner"]["outputs"]["frames"]["ext"] = p["writes"]["frames"].pop("ext")

        def node_in_writes(p):
            p["writes"]["frames"]["node"] = p["runner"]["outputs"]["frames"]["node"]

        def own_preview_without_its_node(p):
            p["writes"]["preview"] = "own"

        def preview_node_without_own(p):
            p["runner"]["outputs"]["preview"] = {"node": "1", "prefix_input": "filename_prefix"}

        def unknown_writes(p):
            p["writes"]["movie"] = True

        def unknown_param(p):
            p["runner"]["params"]["nope"] = {"targets": []}

        def no_runner(p):
            del p["runner"]
        for edit, why in ((other_runner, "runner type 'process' isn't supported yet"),
                          (wiring_in_the_head, "params.prompt: targets belong under runner.params.prompt"),
                          (section_in_the_head, "outputs: in a process.yaml, what it writes goes under writes: and "
                                                "which nodes write it under runner.outputs"),
                          (preview_in_the_head, "preview: in a process.yaml it is writes.preview"),
                          (frames_without_their_node, "writes.frames needs runner.outputs.frames"),
                          (frames_node_without_writes, "runner.outputs.frames needs writes.frames"),
                          (ext_in_the_runner_block, "runner.outputs.frames.ext: what the frames are written as "
                                                    "belongs in writes.frames.ext"),
                          (node_in_writes, "writes.frames.node: belongs in runner.outputs.frames"),
                          (own_preview_without_its_node, "writes.preview: own needs runner.outputs.preview"),
                          (preview_node_without_own, "say writes.preview: own"),
                          (unknown_writes, "writes: unknown key movie"),
                          (unknown_param, "runner.params.nope: no such parameter"),
                          (no_runner, "needs a runner block with a type")):
            with self.subTest(case=edit.__name__):
                with self.assertRaisesRegex(ValueError, why):
                    Workflow(self._fresh(edit))
        with self.assertRaisesRegex(ValueError, "has both process.yaml and manifest.yaml"):
            Workflow(self._fresh(None, keep_manifest=True))

    def test_what_does_not_load_texture_maps(self):
        def missing_node(p):
            del p["runner"]["textures"]["maps"]["nrm02"]

        def extra_node(p):
            p["runner"]["textures"]["maps"]["occ02"] = {"node": "1"}

        def node_in_the_head(p):
            p["writes"]["textures"]["maps"][0]["node"] = "19"

        def channel_data_in_the_runner(p):
            p["runner"]["textures"]["maps"]["nrm02"]["bits"] = "half"

        def name_in_the_runner(p):
            p["runner"]["textures"]["name"] = p["writes"]["textures"].pop("name")

        def no_runner_textures(p):
            del p["runner"]["textures"]
        for edit, why in ((missing_node, r"writes.textures.maps \(nrm02\): needs runner.textures.maps.nrm02"),
                          (extra_node, "runner.textures.maps.occ02: writes.textures.maps has no map with that "
                                       "channel"),
                          (node_in_the_head, r"writes.textures.maps \(color01\).node: belongs in "
                                             "runner.textures.maps.color01"),
                          (channel_data_in_the_runner, "runner.textures.maps.nrm02.bits: belongs in "
                                                       "writes.textures.maps"),
                          (name_in_the_runner, "runner.textures.name: belongs in writes.textures"),
                          (no_runner_textures, "writes.textures needs runner.textures")):
            with self.subTest(case=edit.__name__):
                with self.assertRaisesRegex(ValueError, why):
                    Workflow(self._from("chord_pbr", edit))

    def _from(self, name: str, edit=None, template=None) -> Path:
        """A fresh process.yaml copy of one of the farm's workflows, edited."""
        shutil.rmtree(self.tmp / name, ignore_errors=True)
        folder = self.as_process(name, edit)
        if template:
            path = folder / "workflow_api.json"
            t = json.loads(path.read_text(encoding="utf-8"))
            template(t)
            path.write_text(json.dumps(t), encoding="utf-8")
        return folder

    def _fresh(self, edit, keep_manifest=False) -> Path:
        shutil.rmtree(self.tmp / "wan22_i2i", ignore_errors=True)
        return self.as_process("wan22_i2i", edit, keep_manifest)

    def test_the_registry_loads_both_and_reports_the_ones_it_cannot(self):
        root = self.tmp / "workflows"
        root.mkdir()
        shutil.move(str(self.as_manifest("chord_pbr")), root / "chord_pbr")
        shutil.move(str(self.as_process("wan22_i2i")), root / "wan22_i2i")

        def other_runner(p):
            p["runner"] = {"type": "process"}
        shutil.move(str(self.as_process("sam3_ltx_alpha", other_runner)), root / "sam3_ltx_alpha")
        with self.assertLogs("comfyfarm.definitions", level="ERROR"):
            reg = WorkflowRegistry(root)
        self.assertEqual(sorted(reg.workflows), ["chord_pbr", "wan22_i2i"])
        self.assertEqual({n: w.source for n, w in reg.workflows.items()},
                         {"chord_pbr": "manifest.yaml", "wan22_i2i": "process.yaml"})
        self.assertIn("isn't supported yet", reg.load_errors["sam3_ltx_alpha"])

    def test_the_tool_prints_or_writes_it(self):
        folder = self.as_manifest("wan22_i2i")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(TOOL.main([str(folder)]), 0)
        self.assertEqual(D.from_process(yaml.safe_load(out.getvalue()))[0],
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
