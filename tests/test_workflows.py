import copy
import unittest
from pathlib import Path

from comfyfarm.paths import Sequence
from comfyfarm.workflows import ParamError, Workflow, WorkflowRegistry

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


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.wf = Workflow(WF_DIR / "ltx25_alpha_gen")

    def test_manifest_matches_template(self):
        self.assertEqual(self.wf.problems, [])
        self.assertEqual(self.wf.sequence_param, "input")
        self.assertIn("ltx25_alpha_gen", WorkflowRegistry(WF_DIR).workflows)

    def test_no_show_data_in_template(self):
        text = (WF_DIR / "ltx25_alpha_gen" / "workflow_api.json").read_text(encoding="utf-8")
        self.assertNotIn("/rdo/shows/", text)
        self.assertEqual(self.wf.template["9093"]["inputs"]["value"], "")

    def test_validate_defaults_and_coercion(self):
        v = self.wf.validate({"input": "/x/a.%04d.png", "seed": "7", "enhance_prompt": "yes"})
        self.assertEqual((v["seed"], v["enhance_prompt"], v["shorter_side"], v["fps"]), (7, True, 1088, 24))
        with self.assertRaisesRegex(ParamError, "multiple of 32"):
            self.wf.validate({"input": "x", "shorter_side": 1000})
        with self.assertRaisesRegex(ParamError, "Unknown parameter"):
            self.wf.validate({"input": "x", "sed": 1})
        with self.assertRaisesRegex(ParamError, "required"):
            self.wf.validate({})

    def test_build_injects_everything(self):
        v = self.wf.validate({"input": "x", "positive_prompt": "clean matte", "seed": 99, "fps": 25})
        seq = Sequence(raw="x", farm_path="//srv/shows/xyz/hi/abc_0010.%04d.png", directory="//srv/shows/xyz/hi/",
                       filename="abc_0010.%04d.png", first=1001, last=1010, frame_count=10, skip=3, cap=10,
                       show="xyz", shot="abc_0010")
        before = copy.deepcopy(self.wf.template)
        p = self.wf.build(v, seq, "//srv/out/abc_0010_alpha_v001", "abc_0010_alpha_raw", "comfyfarm/abc")
        self.assertEqual(self.wf.template, before, "build must not mutate the template")
        self.assertEqual(p["5508"]["inputs"]["value"], "clean matte")
        self.assertEqual(p["5516:4832"]["inputs"]["noise_seed"], 99)
        self.assertEqual(p["9107"]["inputs"]["fps"], 25)
        self.assertEqual(p["9048"]["inputs"]["frame_rate"], 25)
        self.assertEqual(p["5548:5026"]["inputs"]["resize_type.shorter_size"], 1088)
        ld = p["9104"]["inputs"]
        self.assertEqual((ld["directory"], ld["skip_first_images"], ld["image_load_cap"]), ("//srv/shows/xyz/hi/", 3, 10))
        self.assertEqual(p["9109"]["inputs"]["output_path"], "//srv/out/abc_0010_alpha_v001")
        self.assertEqual(p["9109"]["inputs"]["filename_prefix"], "abc_0010_alpha_raw")
        self.assertEqual(p["9048"]["inputs"]["filename_prefix"], "comfyfarm/abc")
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
