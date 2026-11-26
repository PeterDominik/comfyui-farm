"""`comfyfarm fal draft <endpoint>` (decision 0065): a fal.ai definition drafted from fal.ai's
own description of an endpoint (its model API, with an OpenAPI schema) and its price, through
the gateway's settings, to review before it is committed. Here: what the draft makes of the
shapes fal.ai's schemas take, that the reviewed definitions agree with their drafts, and the
command against the stand-in (tools/mock_fal.py). Nothing reaches fal.ai itself.

    python -m unittest tests.test_fal_draft -v
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import mock_fal as M  # noqa: E402

from comfyfarm.runners.api import fal_draft as FD  # noqa: E402

try:   # the command reaches the stand-in with httpx, as the gateway does (its runner imports it)
    from comfyfarm import cli
    from comfyfarm.runners.api.runner import ApiRegistry
except ImportError:   # pragma: no cover
    cli = None

PRO = "fal-ai/nano-banana-pro/edit"
PRICE = {"unit_price": 0.15, "unit": "images", "currency": "USD"}


def described(endpoint: str = PRO) -> dict:
    """What fal.ai's model API says of one of the stand-in's models (expand=openapi-3.0)."""
    return {"endpoint_id": endpoint, "metadata": dict(M.ABOUT[endpoint]), "openapi": M.openapi(endpoint)}


def reshaped(takes: dict, gives: dict | None = None, endpoint: str = "fal-ai/some/model", **meta) -> dict:
    """A model whose input properties (and output properties) are these, shaped as fal.ai's are."""
    model = described()
    model["endpoint_id"] = endpoint
    model["metadata"].update(meta)
    api = model["openapi"]
    comps = api["components"]["schemas"]
    comps["NanoBananaProEditInput"] = {"type": "object", "properties": takes,
                                       "required": [k for k, v in takes.items() if v.pop("required", False)]}
    if gives is not None:
        comps["NanoBananaProEditOutput"] = {"type": "object", "properties": gives}
    api["paths"] = {f"/{endpoint}" if k == f"/{PRO}" else k.replace(PRO, endpoint): v for k, v in api["paths"].items()}
    return model


def loaded(text: str) -> dict:
    return yaml.safe_load(text)


class TheDraft(unittest.TestCase):
    def test_nano_banana_pro_edit(self):
        d = FD.draft(described(), PRICE, "2026-11-25")
        self.assertEqual((d.name, d.endpoint), ("nano_banana_pro_edit_fal", PRO))
        self.assertEqual(FD.check(d.text, d.name), [])                  # it loads, as the farm reads it
        y = loaded(d.text)
        self.assertEqual((y["id"], y["label"], y["task"], y["provider"], y["family"], y["class"]),
                         ("nano_banana_pro_edit_fal", "Nano Banana Pro edit", "image", "fal",
                          "nano-banana-pro-edit", "interactive"))
        self.assertEqual(y["models"], [{"file": PRO, "licence": "fal-commercial"}])
        self.assertEqual(y["cost"], {"unit": "image", "price": 0.15, "quantity": 1})
        self.assertEqual(list(y["params"]), ["input", "reference", "prompt", "seed", "aspect_ratio", "resolution",
                                             "output_path"])
        self.assertEqual((y["params"]["input"]["required"], y["params"]["input"]["primary"]), (True, True))
        self.assertEqual(y["params"]["resolution"], {"type": "choice", "label": "Resolution",
                                                     "choices": ["1K", "2K", "4K"], "default": "1K",
                                                     "help": "The resolution of the image to generate."})
        self.assertEqual(y["writes"], {"frames": {"ext": "png"}})
        runner = y["runner"]
        self.assertEqual((runner["type"], runner["endpoint"]), ("api", PRO))
        self.assertEqual(runner["upload"], {"input": "image_urls", "reference": "image_urls"})
        self.assertEqual(runner["send"], {"prompt": "prompt", "seed": "seed", "aspect_ratio": "aspect_ratio",
                                          "resolution": "resolution"})
        # The farm's, never the artist's: one PNG, in fal.ai's answer, nothing from the web.
        self.assertEqual(runner["fixed"], {"num_images": 1, "output_format": "png", "sync_mode": True,
                                           "enable_web_search": False})
        said = " ".join(ln.strip(" #") for ln in d.text.splitlines() if ln.startswith("  #"))
        self.assertIn("Left to fal.ai's defaults: safety_tolerance, system_prompt, temperature, limit_generations.",
                      said)
        self.assertEqual(d.todo, [
            "image_urls is optional at fal.ai: the farm needs an input image to name the job's outputs, so the "
            "draft requires one",
            "cost: check the price against the model's page, and add what changes it (cost.by, e.g. a multiple at "
            "4K)"])
        self.assertTrue(d.text.startswith("# Drafted by `comfyfarm fal draft fal-ai/nano-banana-pro/edit` on "
                                          "2026-11-25"))
        self.assertLessEqual(max(len(line) for line in d.text.splitlines()), 120)

    def test_what_the_first_live_draft_showed(self):
        """Peter's `comfyfarm fal draft` against fal.ai itself, 25 November: fal.ai lists the prompt
        after the other fields, its descriptions may end without a full stop, BiRefNet's price is
        "per compute seconds", and its fields made lines too long for the farm's 120."""
        model = described()
        props = model["openapi"]["components"]["schemas"]["NanoBananaProEditInput"]["properties"]
        props["prompt"] = props.pop("prompt")                        # last, as fal.ai's own schema has it
        model["metadata"]["description"] = "Google's new state-of-the-art image generation and editing model"
        d = FD.draft(model, PRICE, "2026-11-25")
        self.assertEqual(list(loaded(d.text)["params"])[:3], ["input", "reference", "prompt"])
        self.assertIn("editing model. The inputs and the prompt leave", " ".join(loaded(d.text)["description"].split()))
        many = {f"knob_{i}": {"type": "boolean", "default": False} for i in range(12)}
        bire = FD.draft(reshaped({"image_url": {"type": "string", "required": True}, **many},
                                 {"image": {"$ref": "#/components/schemas/ImageFile"}}, endpoint="fal-ai/birefnet/v2",
                                 display_name="Birefnet Background Removal V2", category="image-to-image"),
                        {"unit_price": 0.000575, "unit": "compute seconds", "currency": "USD"}, "2026-11-25")
        y = loaded(bire.text)
        self.assertEqual((y["label"], y["cost"]["unit"]), ("Birefnet Background Removal V2", "compute-second"))
        self.assertEqual(list(y["runner"]["send"]), list(many))       # wrapped, and all there
        self.assertLessEqual(max(len(line) for line in bire.text.splitlines()), 120)    # the farm's lines
        self.assertNotIn("cost.unit", " ".join(FD.check(bire.text, bire.name)))
        self.assertEqual(FD.label_for("fal-ai/kling-video/v3/pro/image-to-video", "Kling Video v3"),
                         "Kling Video v3 image to video")

    def test_one_image_in_and_one_out(self):
        """BiRefNet's shape: image_url, and an `image` back."""
        d = FD.draft(reshaped({"image_url": {"type": "string", "required": True},
                               "model": {"type": "string", "enum": ["General Use (Light)", "Matting"],
                                         "default": "General Use (Light)"},
                               "refine_foreground": {"type": "boolean", "default": True},
                               "output_format": {"type": "string", "enum": ["webp", "png", "gif"]},
                               "mask_url": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                              {"image": {"$ref": "#/components/schemas/ImageFile"}},
                              category="image-to-image"), PRICE, "2026-11-25")
        y = loaded(d.text)
        self.assertEqual(list(y["params"]), ["input", "mask", "model", "refine_foreground", "output_path"])
        self.assertEqual(y["runner"]["upload"], {"input": "image_url", "mask": "mask_url"})
        self.assertEqual(y["runner"]["fixed"], {"output_format": "png"})
        self.assertEqual(y["params"]["refine_foreground"]["default"], True)
        self.assertEqual(FD.check(d.text, d.name), [])

    def test_what_it_cant_draft_yet_is_a_todo(self):
        text_only = FD.draft(reshaped({"prompt": {"type": "string", "required": True},
                                       "duration": {"type": "integer", "minimum": 5, "maximum": 10, "default": 5},
                                       "video_url": {"type": "string"}},
                                      {"video": {"$ref": "#/components/schemas/ImageFile"}},
                                      endpoint="bytedance/some-video/text-to-video", category="text-to-video"),
                             {"unit_price": 0.1, "unit": "seconds", "currency": "USD"}, "2026-11-25")
        self.assertEqual(text_only.name, "bytedance_some_video_text_to_video_fal")
        self.assertEqual(text_only.todo[:2], [
            "video_url: a video input, which the farm's fal.ai definitions don't take yet: left out",
            "it returns a video, which the farm's fal.ai definitions don't bring back yet"])
        self.assertEqual(loaded(text_only.text)["cost"], {"unit": "second", "price": 0.1,
                                                          "quantity": {"param": "duration"}})
        self.assertEqual(loaded(text_only.text)["task"], "video")
        self.assertIn("writes: an API definition for fal.ai writes frames, e.g. writes: {frames: {ext: png}}",
                      FD.check(text_only.text, text_only.name))

    def test_text_to_image(self):
        """Decision 0068: no image in, so the Output folder is required: it says the job's show."""
        d = FD.draft(described("fal-ai/nano-banana-pro"), PRICE, "2026-11-26")
        y = loaded(d.text)
        self.assertEqual((d.name, y["label"], y["task"]), ("nano_banana_pro_fal", "Nano Banana Pro", "image"))
        self.assertEqual(list(y["params"])[:3], ["prompt", "seed", "aspect_ratio"])     # fal.ai's order
        self.assertEqual(y["params"]["output_path"]["required"], True)
        self.assertIn("with no input image, it says which show the job is for", y["params"]["output_path"]["help"])
        self.assertEqual(y["runner"]["upload"], {})
        self.assertNotIn("work_resolution", y)
        self.assertIn("The prompt leaves Rodeo's network: it is sent to fal.ai.", y["description"])
        self.assertFalse([t for t in d.todo if "image" in t.split(":")[0]], d.todo)   # nothing about a missing image
        self.assertEqual(FD.check(d.text, d.name), [])
        # Without the required folder the farm refuses it, saying why.
        bare = d.text.replace("    required: true\n    help: >\n      Where the result goes",
                              "    help: >\n      Where the result goes")
        self.assertIn("no input: needs an output_folder param with required: true, which says the job's show "
                      "(decision 0068)", FD.check(bare, d.name))

    def test_an_image_size_preset_or_a_width_and_height(self):
        """fal.ai's image_size: a preset's name, or a width and height. The presets are a choice,
        and the TODO says how to offer more (decision 0069)."""
        d = FD.draft(described("fal-ai/flux-2-pro"), {"unit_price": 0.03, "unit": "processed megapixels",
                                                      "currency": "USD"}, "2026-11-26")
        size = loaded(d.text)["params"]["image_size"]
        self.assertEqual((size["type"], size["choices"][0], size["default"]), ("choice", "square_hd", "landscape_4_3"))
        self.assertIn("image_size: fal.ai also takes a width and height; to offer sizes past its presets, make the "
                      "choices what the artist reads, and say what each sends under runner.values (decision 0069)",
                      d.todo)
        self.assertIn("cost: fal.ai bills it by processed megapixels: say how many a job is (cost.quantity)", d.todo)
        self.assertEqual(FD.check(d.text, d.name), [])

    def test_a_price_it_cant_read_or_count(self):
        unpriced = FD.draft(described(), None, "2026-11-25")
        self.assertEqual(loaded(unpriced.text)["cost"]["price"], 0)
        self.assertIn("cost: fal.ai's pricing API gave no price in US dollars: write it from the model's page",
                      unpriced.todo)
        self.assertIn("cost.price: the price of one unit in US dollars, as reviewed, e.g. 0.08",
                      FD.check(unpriced.text, unpriced.name))         # it can't run until someone writes it
        tokens = FD.draft(described(), {"unit_price": 30.0, "unit": "1m output tokens", "currency": "USD"}, "today")
        self.assertIn("cost: fal.ai bills it by 1m output tokens: say how many a job is (cost.quantity)", tokens.todo)

    def test_an_endpoint_with_no_schema(self):
        model = described()
        model["openapi"] = {"error": {"code": "expansion_failed", "message": "not available"}}
        with self.assertRaisesRegex(FD.DraftError, "fal.ai gave no schema for fal-ai/nano-banana-pro/edit"):
            FD.draft(model, PRICE, "today")

    def test_names(self):
        self.assertEqual([FD.name_for(e) for e in (PRO, "google/nano-banana-2.1/edit", "fal-ai/flux-2-pro")],
                         ["nano_banana_pro_edit_fal", "google_nano_banana_2_1_edit_fal", "flux_2_pro_fal"])


class TheReviewedDefinitions(unittest.TestCase):
    """Each fal.ai definition in the repo whose endpoint the stand-in describes sends only what
    fal.ai's schema takes, and fixes what its draft fixes: the review left nothing of fal.ai's out."""

    def test_they_agree_with_their_drafts(self):
        from comfyfarm.definitions import DefinitionRegistry
        found = 0
        for name, wf in DefinitionRegistry(REPO / "workflows").workflows.items():
            endpoint = (wf.api or {}).get("endpoint")
            if endpoint not in M.MODELS:
                continue
            found += 1
            with self.subTest(definition=name):
                price = dict(zip(("unit_price", "unit", "currency"), M.PRICES[endpoint]))
                draft = loaded(FD.draft(described(endpoint), price, "today").text)
                takes = set(M.MODELS[endpoint])
                api = wf.api
                for part in ("upload", "send"):
                    self.assertLessEqual(set((api.get(part) or {}).values()), takes, part)
                self.assertLessEqual(set(api.get("fixed") or {}), takes)
                self.assertLessEqual(set(draft["runner"]["fixed"].items()), set(api["fixed"].items()))
                # A review may name a param for artists (FLUX's size): the fields it sends are the draft's.
                self.assertLessEqual(set(api["send"].values()), set(draft["runner"]["send"].values()))
                self.assertEqual(wf.cost["unit"], draft["cost"]["unit"])
                self.assertEqual(wf.cost["price"], M.PRICES[endpoint][0])
        self.assertGreaterEqual(found, 6)


@unittest.skipIf(cli is None, "the command needs httpx")
class TheCommand(unittest.TestCase):
    """`comfyfarm fal draft` against the stand-in, as the gateway's gateway.yaml points it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mock = M.MockFal("rk-test")
        server = self.mock.server(0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        self.mock.base = f"http://127.0.0.1:{port}"
        (self.tmp / "requests.key").write_text("rk-test\n", encoding="utf-8")
        os.chmod(self.tmp / "requests.key", 0o600)          # the gateway reads none other users can
        self.gateway = self.tmp / "gateway.yaml"
        self.gateway.write_text(yaml.safe_dump({
            "name": "gw", "manager": "http://127.0.0.1:1", "gateway": {"enabled": True, "providers": {"fal": {
                "url": self.mock.base, "keys": {"requests": str(self.tmp / "requests.key")}}}}}), encoding="utf-8")

    def run_it(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["fal", "draft", *args, "--gateway", str(self.gateway)])
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_a_draft_and_what_to_review(self):
        code, out, err = self.run_it(PRO)
        self.assertEqual(code, 0, err)
        self.assertEqual(out, FD.draft(described(), PRICE, out.split(" on ", 1)[1][:10]).text)
        self.assertIn("  - cost: check the price against the model's page", err)
        self.assertIn("What the farm says of it as it is: it loads, with no problem.", err)
        asked = [(c["path"], c["keyed"]) for c in self.mock.calls]
        self.assertEqual(asked, [("/v1/models", True), ("/v1/models/pricing", True)])     # free questions only

    def test_it_writes_a_new_folder_never_over_one(self):
        repo = self.tmp / "repo"
        (repo / "workflows").mkdir(parents=True)
        with mock.patch.object(cli, "REPO", repo):
            code, out, err = self.run_it(PRO, "--write")
            self.assertEqual(code, 0, err)
            written = repo / "workflows" / "nano_banana_pro_edit_fal" / "process.yaml"
            self.assertEqual(out, f"Wrote {written}\n")
            self.assertEqual(loaded(written.read_text(encoding="utf-8"))["runner"]["endpoint"], PRO)
            code, _, err = self.run_it(PRO, "--write")
        self.assertEqual(code, 1)
        self.assertIn("is there already: a draft never replaces a definition", err)

    def test_what_stops_it(self):
        code, _, err = self.run_it("fal-ai/no-such-model")
        self.assertEqual(code, 1)
        self.assertIn("Can't draft fal-ai/no-such-model: fal.ai has no endpoint fal-ai/no-such-model", err)
        # Neither live nor a stand-in named: nothing is sent anywhere (decision 0062).
        cfg = yaml.safe_load(self.gateway.read_text(encoding="utf-8"))
        cfg["gateway"]["providers"]["fal"]["url"] = ""
        self.gateway.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        before = len(self.mock.calls)
        code, _, err = self.run_it(PRO)
        self.assertEqual((code, len(self.mock.calls)), (1, before))
        self.assertIn("can't reach fal.ai: the gateway sends jobs to fal.ai itself only with providers.fal.live: "
                      "true", err)
        self.gateway = self.tmp / "nope.yaml"
        code, _, err = self.run_it(PRO)
        self.assertEqual(code, 1)
        self.assertIn("No gateway.yaml at", err)

    def test_its_price_unread_is_a_todo_not_a_stop(self):
        self.mock.prices.pop(PRO)
        code, out, err = self.run_it(PRO)
        self.assertEqual(code, 0, err)
        self.assertEqual(loaded(out)["cost"]["price"], 0)
        self.assertIn("cost.price: the price of one unit in US dollars", err)


@unittest.skipIf(cli is None, "the gateway's registry needs httpx")
class TheStandIn(unittest.TestCase):
    def test_it_describes_every_model_it_runs(self):
        """Its schema is its own check of a job (MODELS), so a reviewed definition that sends a field
        fal.ai doesn't have fails here as it would at fal.ai."""
        for endpoint in M.MODELS:
            with self.subTest(endpoint=endpoint):
                takes, gives, _ = FD.schemas(described(endpoint))
                self.assertEqual(set(takes["properties"]), set(M.MODELS[endpoint]))
                self.assertIn(M.OUTPUT.get(endpoint, "images"), gives["properties"])
        api = {wf.api["endpoint"] for wf in ApiRegistry(REPO / "workflows").workflows.values()}
        self.assertEqual(api - set(M.MODELS), set())         # each reviewed definition runs on the stand-in
        self.assertEqual(api - set(M.PRICES), set())


if __name__ == "__main__":
    unittest.main()
