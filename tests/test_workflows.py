import copy
import json
import tempfile
import unittest
from pathlib import Path

from comfyfarm.fileio.inputs import Sequence
from comfyfarm.workflows import ParamError, Target, Workflow, WorkflowRegistry

WF_DIR = Path(__file__).resolve().parent.parent / "workflows"


def fake_object_info(template: dict) -> dict:
    """object_info that accepts exactly what the template uses."""
    oi = {}
    for node in template.values():
        spec = oi.setdefault(node["class_type"], {"input": {"required": {}}, "output_node": False})
        for k, v in node["inputs"].items():
            base = k.split(".", 1)[0]
            if isinstance(v, str) and k.endswith("_name"):
                opts = spec["input"]["required"].setdefault(base, [["other.safetensors"], {}])[0]
                opts.append(v)
            else:
                spec["input"]["required"].setdefault(base, ["*", {}])
    return oi


def tiny_workflow(directory: Path) -> Workflow:
    """A small workflow with a setting of each plain type, for validation and build. The
    shipped manifests have none: LTX exposes only its input and output folder, CHORD only
    settings that go into file names."""
    target = lambda node, inp, cls: [{"node": node, "input": inp, "class": cls}]  # noqa: E731
    template = {
        "1": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
        "2": {"class_type": "PrimitiveBoolean", "inputs": {"value": False}},
        "3": {"class_type": "PrimitiveInt", "inputs": {"value": 1088}},
        "4": {"class_type": "CreateVideo", "inputs": {"fps": 24}},
        "5": {"class_type": "VHS_LoadImagesPath", "inputs": {"directory": "", "skip_first_images": 0,
                                                             "image_load_cap": 0}},
    }
    manifest = {
        "name": "tiny", "template": "workflow_api.json",
        "params": {
            "input": {"type": "sequence", "required": True},
            "seed": {"type": "int", "default": 42, "min": 0, "targets": target("1", "noise_seed", "RandomNoise")},
            "enhance": {"type": "bool", "default": False, "targets": target("2", "value", "PrimitiveBoolean")},
            "shorter_side": {"type": "int", "default": 1088, "multiple_of": 32,
                             "targets": target("3", "value", "PrimitiveInt")},
            "fps": {"type": "float", "default": 24, "targets": target("4", "fps", "CreateVideo")},
        },
        "sequence": {"loader": {"node": "5", "class": "VHS_LoadImagesPath"}},
    }
    directory.mkdir(parents=True)
    (directory / "workflow_api.json").write_text(json.dumps(template))
    (directory / "manifest.yaml").write_text(json.dumps(manifest))   # JSON is YAML too
    return Workflow(directory)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.wf = Workflow(WF_DIR / "ltx25_alpha_gen")

    def test_manifest_matches_template(self):
        self.assertEqual(self.wf.problems, [])
        self.assertEqual(self.wf.sequence_param, "input")
        self.assertEqual(self.wf.output_folder_param, "output_path")
        self.assertIn("ltx25_alpha_gen", WorkflowRegistry(WF_DIR).workflows)

    def test_gateway_overrides_paths_left_in_template(self):
        # Re-exports carry whatever paths were last used in ComfyUI; every job replaces them.
        v = self.wf.validate({"input": "x"})
        seq = Sequence(raw="x", farm_path="//srv/a/b.%04d.png", directory="//srv/a/", filename="b.%04d.png",
                       first=1, last=2, frame_count=2, skip=0, cap=0, show="s", shot="b")
        p = self.wf.build(v, seq, "//srv/out/v001", "b_alpha_raw")
        self.assertEqual(p["9093"]["inputs"]["value"], "//srv/out/v001")
        self.assertEqual(p["9094"]["inputs"]["value"], "//srv/a/b.%04d.png 1-2")
        self.assertEqual(p["9109"]["inputs"]["output_path"], "//srv/out/v001")

    def test_ltx_form_is_input_and_output_folder(self):
        self.assertEqual([p["name"] for p in self.wf.schema()["params"]], ["input", "output_path"])
        self.assertEqual(self.wf.validate({"input": "x"}), {"input": "x"})
        # Settings that used to be on the form are refused, not quietly ignored.
        with self.assertRaisesRegex(ParamError, r"Unknown parameter\(s\): seed\. Known: input, output_path"):
            self.wf.validate({"input": "x", "seed": 7})
        with self.assertRaisesRegex(ParamError, "required"):
            self.wf.validate({})

    def test_validate_defaults_and_coercion(self):
        with tempfile.TemporaryDirectory() as d:
            wf = tiny_workflow(Path(d) / "tiny")
        self.assertEqual(wf.problems, [])
        v = wf.validate({"input": "/x/a.%04d.png", "seed": "7", "enhance": "yes"})
        self.assertEqual((v["seed"], v["enhance"], v["shorter_side"], v["fps"]), (7, True, 1088, 24))
        with self.assertRaisesRegex(ParamError, "multiple of 32"):
            wf.validate({"input": "x", "shorter_side": 1000})
        with self.assertRaisesRegex(ParamError, "at least 0"):
            wf.validate({"input": "x", "seed": -1})
        with self.assertRaisesRegex(ParamError, "Unknown parameter"):
            wf.validate({"input": "x", "sed": 1})
        with self.assertRaisesRegex(ParamError, "required"):
            wf.validate({})

    def test_build_writes_settings_into_their_targets(self):
        with tempfile.TemporaryDirectory() as d:
            wf = tiny_workflow(Path(d) / "tiny")
        seq = Sequence(raw="x", farm_path="//srv/a/b.%04d.png", directory="//srv/a/", filename="b.%04d.png",
                       first=1, last=2, frame_count=2, skip=0, cap=0, show="s", shot="b")
        p = wf.build(wf.validate({"input": "x", "seed": 99, "fps": 25}), seq)
        self.assertEqual((p["1"]["inputs"]["noise_seed"], p["4"]["inputs"]["fps"]), (99, 25))
        self.assertEqual((p["3"]["inputs"]["value"], p["2"]["inputs"]["value"]), (1088, False))   # defaults
        self.assertEqual(p["5"]["inputs"]["directory"], "//srv/a/")

    def test_build_injects_everything(self):
        v = self.wf.validate({"input": "x"})
        seq = Sequence(raw="x", farm_path="//srv/shows/xyz/hi/abc_0010.%04d.png", directory="//srv/shows/xyz/hi/",
                       filename="abc_0010.%04d.png", first=1001, last=1010, frame_count=10, skip=3, cap=10,
                       show="xyz", shot="abc_0010")
        before = copy.deepcopy(self.wf.template)
        p = self.wf.build(v, seq, "//srv/out/abc_0010_alpha_v001", "abc_0010_alpha_raw", "comfyfarm/abc")
        self.assertEqual(self.wf.template, before, "build must not mutate the template")
        # Not on the form: as exported.
        self.assertEqual(p["5516:4832"]["inputs"]["noise_seed"], 42)
        self.assertEqual(p["9107"]["inputs"]["fps"], 24)
        self.assertEqual(p["5548:5026"]["inputs"]["resize_type.shorter_size"], 1088)
        ld = p["9104"]["inputs"]
        self.assertEqual((ld["directory"], ld["skip_first_images"], ld["image_load_cap"]), ("//srv/shows/xyz/hi/", 3, 10))
        self.assertEqual(p["9109"]["inputs"]["output_path"], "//srv/out/abc_0010_alpha_v001")
        self.assertEqual(p["9109"]["inputs"]["filename_prefix"], "abc_0010_alpha_raw")
        self.assertEqual(p["9094"]["inputs"]["value"], "//srv/shows/xyz/hi/abc_0010.%04d.png 1001-1010")

    def test_object_info_check(self):
        oi = fake_object_info(self.wf.template)
        self.assertEqual(self.wf.check_object_info(oi), [])
        # Newer WAS Image Save: output_path replaced by root
        broken = copy.deepcopy(oi)
        del broken["Image Save"]["input"]["required"]["output_path"]
        self.assertTrue(any("no input 'output_path'" in p for p in self.wf.check_object_info(broken)))
        # Missing model file
        broken = copy.deepcopy(oi)
        broken["UNETLoader"]["input"]["required"]["unet_name"] = [["something_else.safetensors"], {}]
        self.assertTrue(any("not available for unet_name" in p for p in self.wf.check_object_info(broken)))
        # Missing node pack
        broken = copy.deepcopy(oi)
        del broken["VHS_LoadImagesPath"]
        self.assertTrue(any("VHS_LoadImagesPath is not installed" in p for p in self.wf.check_object_info(broken)))

    def test_output_folder_manifest_rules(self):
        wf = Workflow(WF_DIR / "ltx25_alpha_gen")
        wf.params["output_path"].targets = [Target(node="9093", input="value", cls="PrimitiveString")]
        self.assertTrue(any("wired in by the gateway" in p for p in wf.static_check()))
        wf = Workflow(WF_DIR / "ltx25_alpha_gen")
        wf.outputs.pop("frames")
        self.assertTrue(any("needs outputs.frames" in p for p in wf.static_check()))
        self.assertEqual(self.wf.validate({"input": "x"}).get("output_path"), None)

    def test_lora_path_separators(self):
        oi = fake_object_info(self.wf.template)
        oi["LTXICLoRALoaderModelOnly"]["input"]["required"]["lora_name"] = [
            ["LTX/ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors"], {}]
        self.assertEqual(self.wf.check_object_info(oi), [])

    def test_expected_nodes_skips_unused(self):
        n = self.wf.expected_nodes()
        self.assertLess(n, len(self.wf.template))   # 9069/9099/9101/... never run
        self.assertGreater(n, 30)


if __name__ == "__main__":
    unittest.main()
