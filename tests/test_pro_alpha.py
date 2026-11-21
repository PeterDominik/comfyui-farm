"""ComfyFarm Pro Alpha (comfyui_nodes/comfyfarm_nodes/pro_alpha.py) without ComfyUI: the
picks it makes from SAM3's detections, the graph it adds when it runs (with stand-ins for
ComfyUI's GraphBuilder and folder_paths), and the sam3_ltx_alpha workflow around it."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "comfyui_nodes"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from comfyfarm_nodes import pro_alpha as P  # noqa: E402
from comfyfarm_nodes.load_images import ComfyFarmLoadImages  # noqa: E402
from comfyfarm_nodes.save_image import ComfyFarmSaveImage  # noqa: E402

from comfyfarm.runners.comfyui_workflow import Workflow  # noqa: E402

# The Comfy Pro Alpha Generator's inputs at the pinned commit (v0.6.0, 74e4368): the ones
# without a default in its execute() must all be given, since added nodes aren't filled in.
GEN_REQUIRED = {"images", "fps", "first_frame", "quality", "render", "picks", "filename_prefix", "format", "ltx_model",
                "alpha_lora", "text_encoder", "video_vae", "audio_vae", "vitmatte", "chunk", "overlap", "seed", "erode",
                "dilate", "near_px", "stabilize_radius", "sam3_threshold"}
GEN_OPTIONAL = {"fast_attention", "overlap_mode", "hair_guide", "v13_depth", "hair_radius", "attn_grow", "attn_blur",
                "attn_strength", "occluder_reach", "limb_cap", "front_smoothing", "native_edges", "edge_colour",
                "clean_plate_lora", "track_backward", "skip_sam3"}


class FakeGraph:
    """ComfyUI's GraphBuilder as these nodes use it: node(), out(), finalize()."""

    def __init__(self):
        self.nodes = {}

    def node(self, class_type, **inputs):
        nid = f"x{len(self.nodes)}"
        self.nodes[nid] = {"class_type": class_type, "inputs": inputs}
        return types.SimpleNamespace(out=lambda i, nid=nid: [nid, i])

    def finalize(self):
        return self.nodes


class FakePrompt:
    def get_node(self, unique_id):
        return {"inputs": {"images": ["1", 0]}}


def blob(h, w, y0, y1, x0, x1):
    m = np.zeros((h, w), np.float32)
    m[y0:y1, x0:x1] = 1.0
    return m


class Helpers(unittest.TestCase):
    def test_pick_frame(self):
        nums = "1001,1002,1003,1010"
        self.assertEqual(P.pick_index(-1, nums, 4), (0, "frame 1001"))          # empty on the form: the first
        self.assertEqual(P.pick_index(None, "", 4), (0, "the first frame"))
        self.assertEqual(P.pick_index(1010, nums, 4), (3, "frame 1010"))         # a frame number
        with self.assertRaisesRegex(P.ProAlphaError, r"Pick frame 1005 isn't one of the input's frames \(1001-1010\)"):
            P.pick_index(1005, nums, 4)
        self.assertEqual(P.pick_index(2, "", 4), (2, "image 2"))                 # by hand, no numbers: a position
        self.assertEqual(P.pick_index(2, "1001,1002", 4), (2, "image 2"))        # numbers that don't fit: ignored
        with self.assertRaisesRegex(P.ProAlphaError, "past the input's last image"):
            P.pick_index(4, "", 4)
        self.assertEqual(P.parse_frame_numbers("1001, 1002\n1003"), [1001, 1002, 1003])
        self.assertEqual(P.parse_frame_numbers("x"), [])

    def test_detections_as_the_picker_sees_them(self):
        masks = np.stack([blob(100, 200, 10, 30, 20, 60),       # a 40x20 box
                          blob(100, 200, 0, 5, 0, 5),           # 25 pixels: noise, left out
                          np.zeros((100, 200), np.float32)])
        dets = P.detections(masks)
        self.assertEqual(len(dets), 1)
        self.assertEqual(dets[0]["index"], 0)
        self.assertEqual([round(v, 4) for v in dets[0]["box"]], [0.2, 0.2, 0.2, 0.2])   # centre x, y, width, height
        self.assertAlmostEqual(dets[0]["area"], 800 / 20000)
        self.assertEqual(P.detections(np.zeros((1, 8, 8), np.float32)), [])               # SAM3 found nothing
        self.assertEqual(len(P.detections(blob(20, 20, 0, 10, 0, 10))), 1)                 # one mask, (H, W)

    def test_scores(self):
        self.assertEqual(P.parse_scores("[0.9, 0.5]"), [0.9, 0.5])
        self.assertEqual(P.parse_scores("[[0.9], [0.5]]"), [0.9, 0.5])
        self.assertEqual(P.parse_scores("not json"), [])
        self.assertEqual(P.parse_scores('["a"]'), [])

    def test_the_highest_score_is_the_subject(self):
        dets = [{"index": i, "box": [x, 0.5, 0.1, 0.1], "area": 0.01} for i, x in enumerate((0.1, 0.5, 0.9, 0.45))]
        target, others = P.choose([dict(d) for d in dets], [0.4, 0.95, 0.6, 0.3], 4)
        self.assertEqual((target["index"], target["score"]), (1, 0.95))
        self.assertEqual([o["index"] for o in others], [3, 0, 2])                  # nearest first
        target, _ = P.choose([dict(d) for d in dets], [], 4)                       # no scores: SAM3's first
        self.assertEqual((target["index"], target["score"]), (0, None))
        many = [{"index": i, "box": [i / 20, 0.5, 0.01, 0.01], "area": 0.001} for i in range(12)]
        self.assertEqual(len(P.choose(many, [], 12)[1]), P.MAX_OTHERS)             # each one costs a SAM3 track

    def test_picks_as_the_generator_reads_them(self):
        t = {"box": [0.5, 0.5, 0.2, 0.4]}
        picks = P.make_picks(t, [{"box": [0.1, 0.5, 0.1, 0.1]}], 7, "car")
        self.assertEqual(picks["target"], {"id": 0, "frame": 7, "box": [0.5, 0.5, 0.2, 0.4]})   # "box": a detection
        self.assertEqual(picks["others"], [{"id": 1, "frame": 7, "box": [0.1, 0.5, 0.1, 0.1]}])
        self.assertEqual((picks["cuts"], picks["adds"]), ([], []))

    def test_every_generator_input_is_given(self):
        s = P.generator_settings(42, 0.3, "l.safetensors", "a.safetensors", "v.safetensors", "au.safetensors")
        given = set(s) | {"images", "picks"}
        self.assertEqual(GEN_REQUIRED - given, set())
        self.assertEqual(given - GEN_REQUIRED - GEN_OPTIONAL, set())
        self.assertEqual((s["quality"], s["render"], s["edge_colour"], s["native_edges"], s["track_backward"]),
                         (P.BEST, "full shot", "off", False, True))
        self.assertTrue(s["filename_prefix"].startswith(P.OWN_OUTPUT + "/"))

    def test_models_where_the_packs_read_them(self):
        with tempfile.TemporaryDirectory() as d:
            models = os.path.join(d, "models")
            self.assertEqual(len(P.missing_models(d, models)), 2)
            os.makedirs(os.path.join(models, "sam3"))
            Path(models, "sam3", "sam3.safetensors").write_bytes(b"x")
            os.makedirs(os.path.join(models, "vitmatte", "vitmatte-base-composition-1k"))
            self.assertEqual(len(P.missing_models(d, models)), 1)
            Path(models, "vitmatte", "vitmatte-base-composition-1k", "config.json").write_text("{}")
            self.assertEqual(P.missing_models(d, models), [])


class Nodes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.models = os.path.join(self.tmp, "models")
        os.makedirs(os.path.join(self.models, "sam3"))
        Path(self.models, "sam3", "sam3.safetensors").write_bytes(b"x")
        os.makedirs(os.path.join(self.models, "vitmatte", "vitmatte-base-composition-1k"))
        Path(self.models, "vitmatte", "vitmatte-base-composition-1k", "config.json").write_text("{}")
        self.out, self.temp = os.path.join(self.tmp, "output"), os.path.join(self.tmp, "temp")
        stubs = {"folder_paths": types.SimpleNamespace(
                     base_path=self.tmp, models_dir=self.models, get_output_directory=lambda: self.out,
                     get_temp_directory=lambda: self.temp,
                     get_filename_list=lambda folder: {"loras": ["LTX\\ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors",
                                                                 "other.safetensors"]}.get(folder, [])),
                 "comfy_execution": types.ModuleType("comfy_execution"),
                 "comfy_execution.graph_utils": types.SimpleNamespace(GraphBuilder=FakeGraph)}
        for name, mod in stubs.items():
            sys.modules[name] = mod
            self.addCleanup(sys.modules.pop, name, None)

    def run_first(self, **kw):
        args = dict(images=np.zeros((4, 8, 8, 3), np.float32), subject=" car ", pick_frame=1002, seed=42,
                    sam3_threshold=0.3, ltx_model="l.safetensors", alpha_lora="a.safetensors",
                    video_vae="v.safetensors", audio_vae="au.safetensors", frame_numbers="1001,1002,1003,1004",
                    unique_id="4", dynprompt=FakePrompt())
        args.update(kw)
        return P.ComfyFarmProAlpha().run(**args)

    def test_registered_with_the_farm_nodes(self):
        import comfyfarm_nodes
        self.assertIs(comfyfarm_nodes.NODE_CLASS_MAPPINGS["ComfyFarmProAlpha"], P.ComfyFarmProAlpha)
        self.assertIs(comfyfarm_nodes.NODE_CLASS_MAPPINGS["ComfyFarmProAlphaPick"], P.ComfyFarmProAlphaPick)
        # The library's LoRA, as this OS lists it, comes first; the defaults otherwise.
        loras = P.ComfyFarmProAlpha.INPUT_TYPES()["required"]["alpha_lora"][0]
        self.assertEqual(loras[0], "LTX\\ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors")
        self.assertEqual(P.ComfyFarmProAlpha.INPUT_TYPES()["required"]["video_vae"][0],
                         ["ltx-2.5-video-vae-conv-bf16.safetensors"])

    def test_the_first_step_finds_the_subject_on_the_pick_frame(self):
        os.makedirs(os.path.join(self.out, P.OWN_OUTPUT, "alpha"))
        os.makedirs(os.path.join(self.temp, "comfy_pro_alpha_seq"))
        os.makedirs(os.path.join(self.out, "keep"))
        r = self.run_first()
        g = r["expand"]
        by = {n["class_type"]: n["inputs"] for n in g.values()}
        self.assertEqual(by["ComfyProAlpha_Frame"], {"images": ["1", 0], "index": 1})       # frame 1002
        found = by["SAM3Grounding"]
        self.assertEqual((found["text_prompt"], found["confidence_threshold"]), ("car", 0.3))
        pick = by["ComfyFarmProAlphaPick"]
        self.assertEqual((pick["images"], pick["frame"], pick["frame_label"], pick["subject"]),
                         (["1", 0], 1, "frame 1002", "car"))                 # the plate itself, never a copy
        self.assertEqual(json.loads(pick["settings"])["seed"], 42)
        pick_id = next(k for k, n in g.items() if n["class_type"] == "ComfyFarmProAlphaPick")
        self.assertEqual(r["result"], ([pick_id, 0], [pick_id, 1]))
        # The previous run's copies are gone; anything else in the output folder stays.
        self.assertFalse(os.path.exists(os.path.join(self.out, P.OWN_OUTPUT)))
        self.assertFalse(os.path.exists(os.path.join(self.temp, "comfy_pro_alpha_seq")))
        self.assertTrue(os.path.isdir(os.path.join(self.out, "keep")))

    def test_the_first_step_stops_early(self):
        with self.assertRaisesRegex(P.ProAlphaError, "No subject"):
            self.run_first(subject="  ")
        with self.assertRaisesRegex(P.ProAlphaError, "isn't one of the input's frames"):
            self.run_first(pick_frame=999)
        os.remove(os.path.join(self.models, "sam3", "sam3.safetensors"))
        with self.assertRaisesRegex(P.ProAlphaError, r"Missing on this machine: SAM3's model: .*sam3\.safetensors"):
            self.run_first()

    def test_the_second_step_runs_the_generator_with_picks(self):
        masks = np.stack([blob(100, 200, 10, 30, 20, 60), blob(100, 200, 50, 90, 100, 180), blob(100, 200, 0, 9, 0, 9)])
        settings = P.generator_settings(42, 0.3, "l.safetensors", "a.safetensors", "v.safetensors", "au.safetensors")
        r = P.ComfyFarmProAlphaPick().run(images=np.zeros((4, 100, 200, 3)), masks=masks, scores="[0.7, 0.9, 0.2]",
                                          subject="car", frame=1, frame_label="frame 1002",
                                          settings=json.dumps(settings), unique_id="x3", dynprompt=FakePrompt())
        (gid, gen), = r["expand"].items()
        self.assertEqual(gen["class_type"], P.GENERATOR)
        self.assertEqual(gen["inputs"]["images"], ["1", 0])
        picks = json.loads(gen["inputs"]["picks"])
        self.assertEqual(picks["target"]["frame"], 1)
        self.assertEqual([round(v, 3) for v in picks["target"]["box"]], [0.7, 0.7, 0.4, 0.4])   # the 0.9 one
        self.assertEqual(len(picks["others"]), 2)
        self.assertEqual({k: gen["inputs"][k] for k in settings}, settings)
        self.assertEqual(r["result"][0], [gid, 0])
        info = json.loads(r["result"][1])
        self.assertEqual((info["found"], info["score"], info["frame_label"]), (3, 0.9, "frame 1002"))

    def test_nothing_found_says_so(self):
        with self.assertRaisesRegex(P.ProAlphaError, "SAM3 found no 'unicorn' on frame 1001. Try another word"):
            P.ComfyFarmProAlphaPick().run(images=None, masks=np.zeros((1, 8, 8), np.float32), scores="[]",
                                          subject="unicorn", frame=0, frame_label="frame 1001", settings="{}",
                                          unique_id="x3", dynprompt=FakePrompt())


class Workflow_(unittest.TestCase):
    def setUp(self):
        self.wf = Workflow(REPO / "workflows" / "sam3_ltx_alpha")

    def test_manifest_matches_template(self):
        self.assertEqual(self.wf.problems, [])
        self.assertEqual([p["name"] for p in self.wf.schema()["params"]], ["input", "subject", "pick_frame",
                                                                           "output_path"])
        self.assertEqual((self.wf.requires["vram_gb"], self.wf.port.max_frames, self.wf.tag), (32, 960, "matte"))

    def test_the_form(self):
        self.assertEqual(self.wf.validate({"input": "x", "subject": "red car"}), {"input": "x", "subject": "red car"})
        for bad in ("car, dog", "-car"):
            with self.assertRaisesRegex(Exception, "one word or a short phrase"):
                self.wf.validate({"input": "x", "subject": bad})
        with self.assertRaisesRegex(Exception, "Subject is required"):
            self.wf.validate({"input": "x"})
        p = self.wf.build(self.wf.validate({"input": "x", "subject": "dog", "pick_frame": "1050"}))
        self.assertEqual((p["2"]["inputs"]["value"], p["3"]["inputs"]["value"]), ("dog", 1050))
        p = self.wf.build(self.wf.validate({"input": "x", "subject": "dog"}))
        self.assertEqual(p["3"]["inputs"]["value"], -1)                       # empty: the first frame

    def test_template_matches_the_nodes(self):
        """The template uses only inputs the farm nodes have, wired to the right outputs."""
        t = self.wf.template
        node = P.ComfyFarmProAlpha.INPUT_TYPES()
        self.assertEqual(set(t["4"]["inputs"]), set(node["required"]) | set(node["optional"]))
        numbers = ComfyFarmLoadImages.RETURN_NAMES.index("frame_numbers")
        self.assertEqual(t["4"]["inputs"]["frame_numbers"], ["1", numbers])
        save = ComfyFarmSaveImage.INPUT_TYPES()
        self.assertEqual(set(t["6"]["inputs"]), set(save["required"]) | set(save["optional"]))
        self.assertEqual(t["6"]["inputs"]["bit_depth"], "16")
        self.assertEqual((t["5"]["class_type"], t["5"]["inputs"]["mask"]), ("MaskToImage", ["4", 0]))


if __name__ == "__main__":
    unittest.main()
