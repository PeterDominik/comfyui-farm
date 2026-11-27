"""What a job at an outside provider costs (decision 0059): a definition's cost block, checked;
the estimate from the provider's price or the reviewed one; a move since the review noted; a
price in another unit or currency refused; and the estimate in the plan and the job.

The gateway reading prices from fal.ai's stand-in is in test_api_runner.

    python -m unittest tests.test_prices -v
"""

import sys
import unittest
from pathlib import Path

import yaml

from comfyfarm.definitions import Definition
from comfyfarm.manager import prices as PR

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_gateway import GATEWAY, Manager, Tmp, edit   # noqa: E402

REPO = Path(__file__).resolve().parent.parent
NANO = REPO / "workflows" / "nano_banana2_edit_fal"
TODAY = {"unit_price": 0.08, "unit": "image", "currency": "USD", "at": 1795536000.0}


class TheCostBlock(Tmp):
    def test_nano_banana_2s(self):
        wf = Definition(NANO)
        self.assertEqual(wf.problems, [])
        self.assertEqual(wf.cost, {"unit": "image", "price": 0.08, "quantity": 1, "by": {
            "param": "resolution", "factors": {"0.5K": 0.75, "1K": 1, "2K": 1.5, "4K": 2}}})
        self.assertEqual(wf.models, [{"file": "fal-ai/nano-banana-2/edit", "licence": "google-via-fal"}])

    def test_a_price_isnt_in_the_fingerprint(self):
        """A review of a price is the manager's: the gateway needn't be updated for it."""
        a = self.definition(edit())
        b = self.definition(edit(cost__price=0.09), "fal_other")
        self.assertEqual(a.digest, b.digest)

    def test_what_is_wrong_is_said(self):
        cases = {
            "cost: an API definition says what a job costs, from its provider's page, e.g. cost: {unit: image, "
            "price: 0.08, by: {param: resolution, factors: {1K: 1, 2K: 1.5}}}": {"cost": None},
            "cost.price: the price of one unit in US dollars, as reviewed, e.g. 0.08": {"cost__price": 0},
            "cost.unit: the provider's billing unit, as its pricing says, e.g. image": {"cost__unit": "Image s"},
            "cost.currency: the farm's ledger is in US dollars (USD); a price in another currency isn't "
            "converted": {"cost__currency": "EUR"},
            "cost: per isn't something a cost has (unit, price, currency, quantity, by, provider_price)":
                {"cost__per": 1},
            "cost.provider_price: true (the estimate uses the provider's price that day) or false (its pricing "
            "doesn't price a job, so the reviewed one; decision 0071)": {"cost__provider_price": "no"},
            "cost.quantity: a number of units, 1 or more, or {param: <a number param>}": {"cost__quantity": 0},
            "cost.quantity: a number of units, or {param: <a number param>} (e.g. a video's seconds)":
                {"cost__quantity": {"param": "prompt"}},
            "cost.by: {param: <a choice param>, factors: {<each choice>: <its multiple of the price>}}, e.g. "
            "{param: resolution, factors: {1K: 1, 2K: 1.5}}": {"cost__by": {"param": "prompt", "factors": {}}},
            "cost.by.factors: one for each of resolution's choices (1K, 2K), no more":
                {"cost__by": {"param": "resolution", "factors": {"1K": 1}}},
            "cost.by.factors: each is a number above 0, e.g. 1.5":
                {"cost__by": {"param": "resolution", "factors": {"1K": 1, "2K": 0}}},
            "models: it runs outside Rodeo, so it lists the model it uses there and its licence, e.g. models: "
            "[{file: fal-ai/nano-banana-2/edit, licence: provider-terms}]": {"models": []},
        }
        for said, changes in cases.items():
            with self.subTest(said=said[:50]):
                self.assertEqual(self.definition(edit(**changes)).head_problems, [said])

    def test_only_an_outside_providers_definition_costs_money(self):
        p = yaml.safe_load((REPO / "workflows" / "gpu_test" / "process.yaml").read_text(encoding="utf-8"))
        p["cost"] = {"unit": "image", "price": 1}
        wf = self.definition(p, "gpu_test")
        self.assertIn("cost: only an outside provider's definitions cost money (runner: {type: api})",
                      wf.head_problems)


class TheEstimate(unittest.TestCase):
    def setUp(self):
        self.wf = Definition(NANO)

    def test_by_resolution(self):
        for res, amount in (("0.5K", 0.06), ("1K", 0.08), ("2K", 0.12), ("4K", 0.16)):
            with self.subTest(res=res):
                e = PR.estimate(self.wf, {"resolution": res}, TODAY, "fal.ai")
                self.assertAlmostEqual(e["amount"], amount)
                self.assertEqual((e["currency"], e["source"], e["unit"], e["by"]), ("USD", "provider", "image", res))
        self.assertEqual(PR.estimate(self.wf, {"resolution": "2K"}, TODAY, "fal.ai")["text"][:56],
                         "About $0.12 at fal.ai: 1 image at 2K, $0.08 an image, ×1")

    def test_the_reviewed_price_until_the_providers_is_read(self):
        e = PR.estimate(self.wf, {"resolution": "1K"}, None, "fal.ai")
        self.assertEqual((e["amount"], e["source"], e["read_at"]), (0.08, "definition", None))
        self.assertEqual(e["text"], "About $0.08 at fal.ai: 1 image at 1K, $0.08 an image (the price its definition "
                                    "was reviewed at; fal.ai's own isn't read yet).")
        self.assertIsNone(PR.drift_note(self.wf, e, "fal.ai"))

    def test_a_move_since_the_review(self):
        small = PR.estimate(self.wf, {"resolution": "1K"}, {**TODAY, "unit_price": 0.085}, "fal.ai")
        self.assertIsNone(PR.drift_note(self.wf, small, "fal.ai"))           # 6%: within DRIFT
        big = PR.estimate(self.wf, {"resolution": "1K"}, {**TODAY, "unit_price": 0.1}, "fal.ai")
        self.assertAlmostEqual(big["amount"], 0.1)                           # today's price is used
        self.assertEqual(PR.drift_note(self.wf, big, "fal.ai"),
                         "fal.ai's price for Nano Banana 2 edit is now $0.10 an image, 25% above the $0.08 its "
                         "definition was reviewed at: the estimate uses today's price. Its definition's cost needs "
                         "reviewing.")
        down = PR.estimate(self.wf, {"resolution": "1K"}, {**TODAY, "unit_price": 0.06}, "fal.ai")
        self.assertIn("25% below the $0.08", PR.drift_note(self.wf, down, "fal.ai"))

    def test_the_same_unit_however_its_written(self):
        """fal.ai's pricing says "images" for Nano Banana 2 edit (seen on the pilot, 24 November)."""
        for unit in ("images", "Image", " IMAGES "):
            with self.subTest(unit=unit):
                e = PR.estimate(self.wf, {"resolution": "2K"}, {**TODAY, "unit": unit}, "fal.ai")
                self.assertEqual((e["amount"], e["unit"], e["source"]), (0.12, "image", "provider"))
        self.assertTrue(PR.same_unit("seconds", "second"))
        self.assertTrue(PR.same_unit("gpu seconds", "GPU_second"))
        # fal.ai's pricing for BiRefNet (the pilot's first `fal draft`, 25 November), and the
        # name a definition writes for it.
        self.assertTrue(PR.same_unit("compute seconds", "compute-second"))
        self.assertFalse(PR.same_unit("megapixels", "image"))
        self.assertFalse(PR.same_unit("pass", "pas"))           # a double s is no plural

    def test_another_unit_or_currency_is_refused(self):
        with self.assertRaises(PR.PriceError) as e:
            PR.estimate(self.wf, {"resolution": "1K"}, {**TODAY, "unit": "megapixel"}, "fal.ai")
        self.assertEqual(str(e.exception), "fal.ai now bills Nano Banana 2 edit per megapixel, and its definition "
                                           "prices it per image: the definition needs reviewing before it runs again.")
        with self.assertRaisesRegex(PR.PriceError, "now prices Nano Banana 2 edit in EUR"):
            PR.estimate(self.wf, {"resolution": "1K"}, {**TODAY, "currency": "EUR"}, "fal.ai")

    def test_a_price_by_the_token(self):
        """Decision 0071: fal.ai's pricing says "$1 a unit" of GPT Image 2 and Nano Banana 2.1
        (Peter's drafts, 27 November). Their definitions say not to read it."""
        gpt = Definition(REPO / "workflows" / "gpt_image2_fal")
        unit = {"unit_price": 1.0, "unit": "unit", "currency": "USD", "at": 1795622400.0}
        e = PR.estimate(gpt, {"size": "2048 x 1152 (16:9)"}, unit, "fal.ai")
        self.assertEqual((e["amount"], e["source"], e["unit_price"], e["factor"]), (0.495, "definition", 0.22, 2.25))
        self.assertEqual(e["text"], "About $0.495 at fal.ai: 1 image at 2048 x 1152 (16:9), $0.22 an image, ×2.25 at "
                                    "2048 x 1152 (16:9) (the price its definition was reviewed at, by the token; "
                                    "fal.ai's pricing doesn't price a job).")
        self.assertIsNone(PR.drift_note(gpt, e, "fal.ai"))
        self.assertEqual(PR.estimate(gpt, {"size": "3840 x 2160 (16:9)"}, None, "fal.ai")["amount"], 1.76)
        nano = Definition(REPO / "workflows" / "nano_banana2_1_fal")
        self.assertEqual([PR.estimate(nano, {"resolution": r}, unit, "fal.ai")["amount"] for r in ("1K", "2K", "4K")],
                         [0.06, 0.084, 0.162])
        # Without it, fal.ai's "unit" isn't the definition's: refused, as any other unit.
        with self.assertRaisesRegex(PR.PriceError, "now bills GPT Image 2 per unit, and its definition prices it per "
                                                   "image"):
            PR.estimate(Definition(self.read_without(gpt, "provider_price")), {}, unit, "fal.ai")

    def read_without(self, wf, key: str) -> Path:
        import shutil
        import tempfile
        folder = Path(tempfile.mkdtemp()) / wf.name
        self.addCleanup(shutil.rmtree, folder.parent, True)
        shutil.copytree(REPO / "workflows" / wf.name, folder)
        text = (folder / "process.yaml").read_text(encoding="utf-8")
        (folder / "process.yaml").write_text(text.replace(f"  {key}: false\n", ""), encoding="utf-8")
        return folder

    def test_money(self):
        self.assertEqual([PR.money(x) for x in (0.08, 1.5, 0.0075, 0.1425, 41.2, 1234.5, 0.000575, 0.0115)],
                         ["$0.08", "$1.50", "$0.0075", "$0.1425", "$41.20", "$1,234.50", "$0.000575", "$0.0115"])


class WhenPlanned(Manager):
    """The estimate in the plan, the preview and the job (decision 0059)."""

    def nodes(self, price):
        self.desk.nodes({"name": GATEWAY, "processes": {
            n: {"digest": wf.digest, "problems": [], "price": price}
            for n, wf in self.farm.registry.workflows.items() if wf.runner_type == "api"}})

    def plan(self, **params):
        values = {"input": str(REPO / "workflows/gpu_test/eval/input.png"), "prompt": "rain",
                  "output_path": str(self.tmp / "shows"), **params}
        return self.farm.plan("fal_edit", values, show="rnd")

    def test_from_the_reviewed_price_then_the_gateways(self):
        self.up(GATEWAY, lanes={"fal": 2})
        cost = self.farm.plan_summary(self.plan(resolution="2K"))["cost"]
        self.assertEqual((cost["amount"], cost["source"]), (0.12, "definition"))
        self.nodes({**TODAY, "unit_price": 0.085})
        cost = self.farm.plan_summary(self.plan(resolution="2K"))["cost"]
        self.assertAlmostEqual(cost["amount"], 0.1275)
        self.assertEqual((cost["source"], cost["read_at"]), ("provider", TODAY["at"]))
        job = self.submit(resolution="2K")
        self.assertAlmostEqual(job["info"]["cost"]["amount"], 0.1275)       # kept with the job, as planned
        self.assertFalse([w for w in job["warnings"] if "$" in w])          # an estimate isn't a warning

    def test_a_move_is_noted_and_another_unit_refused(self):
        self.up(GATEWAY, lanes={"fal": 2})
        self.nodes({**TODAY, "unit_price": 0.1})
        self.assertIn("fal.ai's price for Image edit (fal) is now $0.10 an image, 25% above", self.plan()["notes"][0])
        self.nodes({**TODAY, "unit": "megapixel"})
        with self.assertRaises(Exception) as e:
            self.plan()
        self.assertEqual((getattr(e.exception, "status", None), str(e.exception)[:60]),
                         (409, "fal.ai now bills Image edit (fal) per megapixel, and its def"))

    def test_the_latest_gateway_price_and_only_for_its_fingerprint(self):
        self.up(GATEWAY, lanes={"fal": 2})
        self.nodes({**TODAY, "unit_price": 0.09, "at": 100.0})
        self.up("wrk2-gateway", lanes={"fal": 2})
        self.desk.nodes({"name": "wrk2-gateway", "processes": {
            "fal_edit": {"digest": "an-older-one", "problems": [], "price": {**TODAY, "unit_price": 0.5,
                                                                              "at": 200.0}}}})
        self.assertEqual(self.desk.price(self.farm.registry.workflows["fal_edit"])["unit_price"], 0.09)

    def test_the_dashboard_shows_it(self):
        html = (REPO / "comfyfarm/manager/static/index.html").read_text(encoding="utf-8")
        self.assertIn("(r.cost ? `<br><b>Cost</b> ${esc(r.cost.text)}${r.cost.budget ? \" \" + esc(r.cost.budget.text) "
                      ": \"\"}`", html)       # with what its show's cap has left (decision 0060)


if __name__ == "__main__":
    unittest.main()
