import copy
import json
import tempfile
import unittest
from pathlib import Path

from comfyfarm.fileio import farmnodes
from comfyfarm.fileio.inputs import Sequence
from comfyfarm.workflows import ParamError, Port, Target, Workflow, WorkflowRegistry, fit_to_options

REPO = Path(__file__).resolve().parent.parent
WF_DIR = REPO / "workflows"


def fake_object_info(template: dict) -> dict:
    """object_info that accepts exactly what the template uses, with this checkout's farm
    nodes (as a ComfyUI started after the last pull has them)."""
    mark = farmnodes.marker(farmnodes.version(REPO), REPO)
    oi = {c: {"input": {"required": {"path": ["STRING", {}]}}, "output_node": False, "description": mark}
          for c in farmnodes.CLASSES}
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


def plate_matte_workflow(directory: Path) -> Workflow:
    """A workflow with three inputs: a plate (primary), a matte read at the plate's frames,
    and a reference image. The matte and reference are optional: left empty, each flips
    its switch (a boolean the graph reads) off."""
    template = {
        "1": {"class_type": "ComfyFarmLoadImages", "inputs": {"path": "", "frames": ""}, "_meta": {"title": "PLATE"}},
        "2": {"class_type": "ComfyFarmLoadImages", "inputs": {"path": "", "frames": ""}, "_meta": {"title": "MATTE"}},
        "3": {"class_type": "ComfyFarmLoadImage", "inputs": {"path": ""}, "_meta": {"title": "REFERENCE"}},
        "4": {"class_type": "PrimitiveBoolean", "inputs": {"value": True}, "_meta": {"title": "USE MATTE"}},
        "5": {"class_type": "PrimitiveBoolean", "inputs": {"value": True}, "_meta": {"title": "USE REFERENCE"}},
        "6": {"class_type": "ComfyFarmSaveImage", "inputs": {"images": ["1", 0], "output_path": "", "filename_prefix": "x",
                                                             "compress_level": 0}},
    }
    switch = lambda node, value: [{"node": node, "input": "value", "class": "PrimitiveBoolean", "value": value}]  # noqa: E731
    manifest = {
        "name": "plate_matte", "tag": "pm", "template": "workflow_api.json",
        "params": {
            "plate": {"type": "input", "takes": "images", "primary": True, "required": True, "label": "Plate",
                      "loader": {"node": "1", "class": "ComfyFarmLoadImages"}},
            "matte": {"type": "input", "takes": "images", "match": "plate", "label": "Matte",
                      "loader": {"node": "2", "class": "ComfyFarmLoadImages"},
                      "empty": switch("4", False), "given": switch("4", True)},
            "reference": {"type": "input", "takes": "image", "label": "Reference",
                          "loader": {"node": "3", "class": "ComfyFarmLoadImage"}, "empty": switch("5", False)},
            "output_path": {"type": "output_folder"},
        },
        "outputs": {"frames": {"node": "6", "class": "ComfyFarmSaveImage", "path_input": "output_path",
                               "prefix_input": "filename_prefix", "ext": "png"}},
    }
    directory.mkdir(parents=True)
    (directory / "workflow_api.json").write_text(json.dumps(template))
    (directory / "manifest.yaml").write_text(json.dumps(manifest))
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
        self.assertEqual(p["9104"]["inputs"], {"path": "//srv/shows/xyz/hi/abc_0010.%04d.png", "frames": "1001-1010"})
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
        del broken["LTXPadFrames"]
        self.assertTrue(any("LTXPadFrames is not installed (missing custom node pack?)" in p
                            for p in self.wf.check_object_info(broken)))
        del broken["ComfyFarmLoadImages"]                    # the farm's own: comes with this repo
        self.assertTrue(any("ComfyFarmLoadImages is not installed. It comes with this repo" in p
                            for p in self.wf.check_object_info(broken)))

    def test_loaders_the_farm_can_fill(self):
        for name in ("ltx25_alpha_gen", "chord_pbr"):          # the farm's own loader
            wf = Workflow(WF_DIR / name)
            self.assertEqual((wf.loader.cls, wf.loader.lists_folder), ("ComfyFarmLoadImages", False))
        with tempfile.TemporaryDirectory() as d:
            wf = tiny_workflow(Path(d) / "tiny")                 # VHS: downloaded workflows still work
            self.assertEqual((wf.problems, wf.loader.lists_folder), ([], True))
            wf.port.loader["class"] = "LoadImage"
            self.assertIn("sequence.loader: class 'LoadImage' isn't a loader the farm can fill "
                          "(use ComfyFarmLoadImages, ComfyFarmLoadImage, VHS_LoadImagesPath)", wf.static_check())

    def test_movie_loaders_refused(self):
        with tempfile.TemporaryDirectory() as d:
            wf = tiny_workflow(Path(d) / "tiny")
            wf.template["9"] = {"class_type": "VHS_LoadVideoPath", "inputs": {"video": "x.mov"},
                                "_meta": {"title": "Load plate"}}
            self.assertIn("node 9 'Load plate' loads a movie (VHS_LoadVideoPath): farm workflows only take images. "
                          "Load frames with ComfyFarm Load Images; the farm extracts a movie's frames itself",
                          wf.static_check())

    def test_input_ports(self):
        """`type: input` says what the workflow takes; `type: sequence` (with its loader in a
        `sequence:` section) is the older way to write an `images` input."""
        wan = Workflow(WF_DIR / "wan22_i2i")
        self.assertEqual(wan.problems, [])
        self.assertEqual((wan.port.takes, wan.loader.cls, wan.sequence_param), ("image", "ComfyFarmLoadImage", "input"))
        chord = Workflow(WF_DIR / "chord_pbr")
        self.assertEqual((chord.port.takes, chord.port.per_run, chord.textures.batch), ("images", 4, 4))
        with tempfile.TemporaryDirectory() as d:
            tiny = tiny_workflow(Path(d) / "tiny")
            self.assertEqual((tiny.port.takes, tiny.port.loader["class"]), ("images", "VHS_LoadImagesPath"))
        # Clients see every input as a "sequence" field (as before `input`), and how many it takes.
        field = next(p for p in wan.schema()["params"] if p["name"] == "input")
        self.assertEqual((field["type"], field["takes"]), ("sequence", "image"))
        self.assertEqual(next(p for p in self.wf.schema()["params"] if p["name"] == "input")["takes"], "images")

    def test_requires(self):
        ltx = Workflow(WF_DIR / "ltx25_alpha_gen")
        self.assertEqual(ltx.requires, {"vram_gb": 32, "tags": []})
        self.assertEqual(ltx.schema()["requires"], {"vram_gb": 32, "tags": []})
        self.assertEqual(Workflow(WF_DIR / "wan22_i2i").requires, {"vram_gb": None, "tags": []})   # any machine
        for bad, why in (({"vram_gb": "lots"}, "requires.vram_gb 'lots' isn't a number of GB"),
                         ("big", "requires must be a mapping")):
            ltx.requires = {"bad": bad} if not isinstance(bad, dict) else {"vram_gb": bad["vram_gb"], "tags": []}
            self.assertTrue(any(why in p for p in ltx.static_check()), ltx.static_check())

    def test_input_port_rules(self):
        wan = Workflow(WF_DIR / "wan22_i2i")

        def problems(**change):
            wan.ports["input"] = Port("input", **{"takes": "image", "loader": {"node": "254", "class": "ComfyFarmLoadImage"},
                                                  **change})
            return wan.static_check()
        self.assertEqual(problems(), [])
        self.assertIn("param input.loader: ComfyFarmLoadImage loads one image, so the input takes image, not images",
                      problems(takes="images"))
        self.assertIn("param input: takes must be image or images, not 'video'", problems(takes="video"))
        self.assertIn("param input: frames.max is for inputs that take images; this one takes one image",
                      problems(max_frames=10))
        self.assertIn("param input: per_run splits texture inputs into runs; frame inputs can't be split yet",
                      problems(takes="images", loader={"node": "254", "class": "ComfyFarmLoadImages"}, per_run=4))
        wan.ports["input"] = Port("input", takes="image", loader={"node": "254", "class": "ComfyFarmLoadImage"})
        wan.ports["matte"] = Port("matte", loader={"node": "254", "class": "ComfyFarmLoadImage"}, takes="image")
        self.assertIn("several inputs (input, matte): mark the one that names and numbers the outputs primary: true",
                      wan.static_check())

    def test_several_inputs(self):
        with tempfile.TemporaryDirectory() as d:
            wf = plate_matte_workflow(Path(d) / "pm")
            self.assertEqual(wf.problems, [])
            self.assertEqual((wf.port.name, [p.name for p in wf.other_ports]), ("plate", ["matte", "reference"]))
            fields = {p["name"]: p for p in wf.schema()["params"]}
            self.assertEqual([(fields[n]["primary"], fields[n].get("match")) for n in ("plate", "matte", "reference")],
                             [(True, None), (False, "plate"), (False, None)])
            plate = Sequence(raw="x", farm_path="//s/p.%04d.png", directory="//s/", filename="p.%04d.png", first=1001,
                             last=1003, frame_count=3, skip=0, cap=0, show="x", shot="a")
            matte = Sequence(raw="x", farm_path="//s/m.%04d.png", directory="//s/", filename="m.%04d.png", first=1001,
                             last=1003, frame_count=3, skip=0, cap=0, show="x", shot="a")
            p = wf.build(wf.validate({"plate": "x"}), plate, "//out", "raw", inputs={"matte": matte, "reference": None})
            self.assertEqual(p["1"]["inputs"], {"path": "//s/p.%04d.png", "frames": "1001-1003"})
            self.assertEqual((p["2"]["inputs"], p["4"]["inputs"]["value"]),
                             ({"path": "//s/m.%04d.png", "frames": "1001-1003"}, True))      # given: its switch on
            self.assertEqual((p["3"]["inputs"], p["5"]["inputs"]["value"]),
                             ({"path": "", "optional": True}, False))                       # left empty: off
            p = wf.build(wf.validate({"plate": "x"}), plate, "//out", "raw")                 # nothing but the plate
            self.assertEqual((p["2"]["inputs"]["optional"], p["4"]["inputs"]["value"]), (True, False))

    def test_several_inputs_rules(self):
        with tempfile.TemporaryDirectory() as d:
            wf = plate_matte_workflow(Path(d) / "pm")
            wf.ports["plate"].primary = False
            self.assertIn("several inputs (plate, matte, reference): mark the one that names and numbers the outputs "
                          "primary: true", wf.static_check())
            wf.ports["plate"].primary = wf.ports["matte"].primary = True
            self.assertIn("only one input can be primary (found plate, matte)", wf.static_check())
            wf.ports["matte"].primary = False
            wf.ports["reference"].match = "matte"
            self.assertIn("param reference: match names the primary input (plate), not 'matte'", wf.static_check())
            wf.ports["reference"].match = "plate"
            self.assertIn("param reference: takes one image, so it can't match plate's frames", wf.static_check())
            wf.ports["reference"].match = None
            wf.ports["matte"].required = True
            self.assertIn("param matte.empty: only optional inputs (required: false) can be left empty",
                          wf.static_check())
            wf.ports["matte"].required = False
            wf.ports["reference"].empty = [{"node": "5", "input": "value"}]
            self.assertIn("param reference.empty[0]: needs node, input and value", wf.static_check())
            wf.ports["reference"].empty = [{"node": "99", "input": "value", "value": False}]
            self.assertIn("param reference.empty[0]: node 99 is not in the workflow (re-exported? update the manifest)",
                          wf.static_check())

    def test_build_one_image(self):
        wan = Workflow(WF_DIR / "wan22_i2i")
        v = wan.validate({"input": "x", "prompt": "a foggy street", "steps": 20, "noise": 0.3})
        self.assertEqual((v["cfg"], v["steps"]), (3, 20))       # defaults as exported
        seq = Sequence(raw="x", farm_path="//srv/shows/xyz/hi/abc_0010.%04d.png", directory="//srv/shows/xyz/hi/",
                       filename="abc_0010.%04d.png", first=1050, last=1050, frame_count=1, skip=0, cap=0,
                       show="xyz", shot="abc_0010")
        p = wan.build(v, seq, "//srv/out/abc_0010", "_comfyfarm_j1_raw")
        self.assertEqual(p["254"]["inputs"]["path"], "//srv/shows/xyz/hi/abc_0010.%04d.png 1050")   # the loader finds it
        self.assertEqual((p["255"]["inputs"]["value"], p["208"]["inputs"]["value"], p["210"]["inputs"]["value"]),
                         ("a foggy street", 20, 0.3))
        self.assertEqual((p["256"]["inputs"]["output_path"], p["256"]["inputs"]["filename_prefix"]),
                         ("//srv/out/abc_0010", "_comfyfarm_j1_raw"))
        seq.convert = {"kind": "exr", "dst_dir": "C:/scratch/j1/", "dst_pattern": "abc_0010.plate.%04d.png"}
        p = wan.build(v, seq, "//srv/out/abc_0010", "_comfyfarm_j1_raw")
        self.assertEqual(p["254"]["inputs"]["path"], "C:/scratch/j1/abc_0010.plate.1050.png")   # converted: the file

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

    def test_file_names_follow_the_workers_separators(self):
        prompt = copy.deepcopy(self.wf.template)
        node = next(n for n in prompt.values() if n["class_type"] == "LTXICLoRALoaderModelOnly")
        self.assertIn("\\", node["inputs"]["lora_name"])                     # saved on Windows
        oi = fake_object_info(self.wf.template)
        lora = node["inputs"]["lora_name"].replace("\\", "/")
        oi["LTXICLoRALoaderModelOnly"]["input"]["required"]["lora_name"] = [["other.safetensors", lora], {}]
        fit_to_options(prompt, oi)                                           # a Linux ComfyUI's list
        self.assertEqual(node["inputs"]["lora_name"], lora)
        windows = copy.deepcopy(self.wf.template)
        self.assertEqual(fit_to_options(windows, fake_object_info(self.wf.template)), self.wf.template)
        unknown = copy.deepcopy(self.wf.template)
        oi["LTXICLoRALoaderModelOnly"]["input"]["required"]["lora_name"] = [["other.safetensors"], {}]
        fit_to_options(unknown, oi)                                          # not there: left for ComfyUI to refuse
        self.assertEqual(unknown, self.wf.template)

    def test_expected_nodes_skips_unused(self):
        n = self.wf.expected_nodes()
        self.assertLess(n, len(self.wf.template))   # 9069/9099/9101/... never run
        self.assertGreater(n, 30)


if __name__ == "__main__":
    unittest.main()
