"""The catalogue (decision 0054): the providers and tasks every menu is drawn from, Provider >
Task > Workflow, and each definition's place in it. Its place never changes what a job
runs, so it is left out of the definition's fingerprint."""

import importlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from comfyfarm import catalog as C
from comfyfarm import cli
from comfyfarm.definitions import Definition, DefinitionRegistry

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "client") not in sys.path:
    sys.path.insert(0, str(REPO / "client"))
client = importlib.import_module("comfyfarm_client")
# The farm's own definitions. fal.ai's are more each time one is reviewed in (decision 0065): the
# tests check them all alike, so a new one needs no test changed.
TASKS_OF = {"batch_test": "tests", "chord_pbr": "textures", "gpu_test": "tests", "ltx25_alpha_gen": "masking",
            "ltx25_layout_render": "video", "sam3_ltx_alpha": "masking", "wan22_i2i": "image"}
BEFORE = ("chord_pbr", "gpu_test", "ltx25_alpha_gen", "ltx25_layout_render", "sam3_ltx_alpha", "wan22_i2i")


def write_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def catalogue(self, providers: dict | None = None, tasks=None) -> Path:
        """A checkout's catalogue in a folder of its own: providers/<name>.yaml and
        workflows/tasks.yaml."""
        root = self.tmp / "checkout"
        if providers is None:
            providers = {"comfyui-local": {"label": "ComfyUI Local", "runner": "comfyui", "target": "pool"},
                         "python-local": {"label": "Python Local", "runner": "process", "target": "pool"}}
        for name, data in providers.items():
            write_yaml(root / "providers" / f"{name}.yaml", data)
        (root / "providers").mkdir(parents=True, exist_ok=True)
        write_yaml(root / "workflows" / "tasks.yaml",
                   {"masking": {"label": "Masking"}, "video": {"label": "Video"}} if tasks is None else tasks)
        return root


class TheCheckoutsCatalogueTests(unittest.TestCase):
    def test_it_loads_in_menu_order(self):
        cat = C.load()
        self.assertEqual(cat.errors, [])
        self.assertEqual(list(cat.providers), ["comfyui-local", "python-local", "fal"])
        self.assertEqual([(p.label, p.runner, p.target, p.offsite) for p in cat.providers.values()],
                         [("ComfyUI Local", "comfyui", "pool", False), ("Python Local", "process", "pool", False),
                          ("fal.ai", "api", "gateway", True)])
        self.assertEqual(list(cat.tasks), ["masking", "video", "image", "textures", "tests"])
        self.assertEqual([t.label for t in cat.tasks.values()], ["Masking", "Video", "Image", "Textures", "Tests"])

    def test_every_definition_has_a_task_and_its_runners_provider(self):
        reg = DefinitionRegistry(REPO / "workflows")
        self.assertEqual(reg.load_errors, {})
        self.assertEqual({n: wf.task for n, wf in reg.workflows.items() if wf.runner_type != "api"}, TASKS_OF)
        self.assertIn("nano_banana2_edit_fal", reg.workflows)
        for name, wf in reg.workflows.items():
            with self.subTest(definition=name):
                self.assertEqual(wf.problems, [])
                if wf.runner_type == "api":         # an API definition names its provider (decision 0055)
                    self.assertEqual((wf.provider, wf.provider_named), ("fal", "fal"))
                    continue
                self.assertEqual(wf.provider, "python-local" if wf.runner_type == "process" else "comfyui-local")
                self.assertIsNone(wf.provider_named)
                self.assertIsNone(wf.family)
                self.assertEqual(wf.hidden, name in ("gpu_test", "batch_test"))      # the farm's own tests

    def test_where_each_shows(self):
        cat = C.load()
        reg = DefinitionRegistry(REPO / "workflows")
        places = {n: cat.place(wf.task, wf.provider, wf.hidden) for n, wf in reg.workflows.items()}
        self.assertEqual(places["sam3_ltx_alpha"], "ComfyUI Local > Masking")
        self.assertEqual(places["chord_pbr"], "ComfyUI Local > Textures")
        self.assertEqual(places["gpu_test"], "Python Local > Tests (hidden from artists)")


class FingerprintTests(Tmp):
    """Adding the menu fields changed no fingerprint, so machines that haven't updated their
    checkout still take jobs; a later move to another task never makes one refuse a job."""

    def copy(self, name: str, edit) -> Path:
        folder = self.tmp / name
        shutil.copytree(REPO / "workflows" / name, folder)
        path = folder / "process.yaml"
        path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
        return folder

    def test_the_menu_fields_are_not_in_the_fingerprint(self):
        for name in ("wan22_i2i", "gpu_test"):
            with self.subTest(definition=name):
                repo = Definition(REPO / "workflows" / name)
                without = Definition(self.copy(name, lambda t: "\n".join(
                    line for line in t.split("\n") if not line.startswith(("task:", "hidden:")))))
                self.assertIsNone(without.task)
                self.assertEqual(without.digest, repo.digest)
                shutil.rmtree(self.tmp / name)
                moved = Definition(self.copy(name, lambda t: t.replace(f"task: {TASKS_OF[name]}", "task: video", 1)
                                             + "\nfamily: some-model\n"))
                self.assertEqual((moved.task, moved.family), ("video", "some-model"))
                self.assertEqual(moved.digest, repo.digest)
                shutil.rmtree(self.tmp / name)
                relabelled = Definition(self.copy(name, lambda t: t.replace("label: ", "label: New ", 1)))
                self.assertNotEqual(relabelled.digest, repo.digest)     # what runs and says still counts
                shutil.rmtree(self.tmp / name)

    def test_the_fingerprints_are_the_ones_before_the_catalogue(self):
        """Those of main before decision 0054, as the pilot machines hold them (the definitions
        there were then)."""
        reg = DefinitionRegistry(REPO / "workflows")
        self.assertEqual({n: wf.digest for n, wf in reg.workflows.items() if n in BEFORE}, {
            "chord_pbr": "04bef8eb6e508071", "gpu_test": "264652bea4ae1a75",
            "ltx25_alpha_gen": "3eabe6e35d9bd3b2", "ltx25_layout_render": "2bfa32bf1ad88243",
            "sam3_ltx_alpha": "a21ab909f5f39aba", "wan22_i2i": "4eb3f5da88634457"})


class DefinitionPlaceTests(Tmp):
    """A definition's task, provider, family and hidden, checked when it loads."""

    def definition(self, name: str = "wan22_i2i", **fields) -> Definition:
        folder = self.tmp / f"{name}-{len(list(self.tmp.iterdir()))}"
        shutil.copytree(REPO / "workflows" / name, folder)
        path = folder / "process.yaml"
        p = yaml.safe_load(path.read_text(encoding="utf-8"))
        for k, v in fields.items():
            if v is None:
                p.pop(k, None)
            else:
                p[k] = v
        path.write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        return Definition(folder)

    def test_an_unknown_task_turns_it_off(self):
        wf = self.definition(task="relight")
        self.assertEqual(wf.problems, ["task: relight isn't in workflows/tasks.yaml (it has masking, video, image, "
                                       "textures, tests)"])
        self.assertFalse(wf.enabled)

    def test_no_task_is_allowed(self):
        wf = self.definition(task=None)
        self.assertEqual((wf.task, wf.problems), (None, []))
        self.assertEqual(C.load().place(wf.task, wf.provider), "ComfyUI Local > no task")

    def test_fields_of_the_wrong_shape(self):
        wf = self.definition(task="Masking", provider=["comfyui-local"], family="Not a name", hidden="yes")
        self.assertEqual(wf.problems, [
            "family: needs to be one short name for the same model on every provider, e.g. nano-banana-2-edit",
            "hidden: needs to be true (kept out of artists' menus) or false",
            "provider: needs to be the name of a provider in providers/, e.g. comfyui-local",
            "task: needs to be the name of a task in workflows/tasks.yaml, e.g. masking"])

    def test_its_provider(self):
        self.assertEqual(self.definition(provider="comfyui-local").problems, [])
        self.assertEqual(self.definition(provider="nope").problems,
                         ["provider: nope isn't in providers/ (it has comfyui-local, python-local, fal)"])
        self.assertEqual(self.definition(provider="fal").problems,
                         ["provider: fal runs runner.type api definitions, and this one is runner.type comfyui"])
        self.assertEqual(self.definition("gpu_test", provider="comfyui-local").problems,
                         ["provider: comfyui-local runs runner.type comfyui definitions, and this one is runner.type "
                          "process"])
        wf = self.definition(family="wan-2-2-i2i")
        self.assertEqual((wf.family, wf.problems), ("wan-2-2-i2i", []))

    def test_a_broken_catalogue_is_said_once_not_on_every_definition(self):
        broken = C.Catalog(root=self.tmp, errors=["workflows/tasks.yaml: needs to be the tasks in menu order"])
        with mock.patch.object(C, "current", return_value=broken):
            self.assertEqual(self.definition(task="relight").problems, [])
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.check_catalog(), 1)
        self.assertIn("[FAIL] menus: workflows/tasks.yaml: needs to be the tasks in menu order", out.getvalue())


class CatalogueFileTests(Tmp):
    def test_menu_order_is_order_then_name(self):
        root = self.catalogue({
            "zeta": {"label": "Zeta", "runner": "comfyui", "target": "pool", "order": 5},
            "comfyui-local": {"label": "ComfyUI Local", "runner": "comfyui", "target": "pool", "order": 10},
            "alpha": {"label": "Alpha", "runner": "process", "target": "pool"},
            "python-local": {"label": "Python Local", "runner": "process", "target": "pool", "order": 20,
                             "description": " Python on our GPUs. "}})
        cat = C.load(root)
        self.assertEqual(cat.errors, [])
        self.assertEqual(list(cat.providers), ["zeta", "comfyui-local", "python-local", "alpha"])
        self.assertEqual(cat.providers["python-local"].as_dict(), {
            "name": "python-local", "label": "Python Local", "runner": "process", "target": "pool",
            "offsite": False, "description": "Python on our GPUs."})
        self.assertEqual(cat.summary(), "4 provider(s) (Zeta, ComfyUI Local, Python Local, Alpha), "
                                        "2 task(s) (Masking, Video)")

    def test_broken_files_are_said_plainly(self):
        root = self.catalogue({
            "comfyui-local": {"label": "ComfyUI Local", "runner": "comfyui", "target": "pool", "price": 2},
            "python-local": {"label": "Python Local", "runner": "python", "target": "pool"},
            "Bad_Name": {"label": "Bad", "runner": "comfyui", "target": "pool"},
            "elsewhere": {"label": "Elsewhere", "runner": "comfyui", "target": "cloud"},
            "maybe": {"label": "Maybe", "runner": "comfyui", "target": "pool", "offsite": "no"},
            "unlabelled": {"runner": "comfyui", "target": "pool"},
            "first": {"label": "First", "runner": "comfyui", "target": "pool", "order": "1"}},
            tasks=[{"masking": "Masking"}])
        errors = C.load(root).errors
        self.assertEqual(errors, [
            "providers/Bad_Name.yaml: Bad_Name isn't a provider's name: lower-case letters, digits and dashes, "
            "starting with a letter (comfyui-local)",
            "providers/comfyui-local.yaml: unknown key(s) price (a provider has label, description, runner, "
            "target, offsite, order, adapter, lanes, keys)",
            "providers/elsewhere.yaml: target: needs to be where its jobs go (pool, tractor, gateway)",
            "providers/first.yaml: order: needs to be a whole number, its place in the menus (smallest first)",
            "providers/maybe.yaml: offsite: needs to be true (its jobs send material outside Rodeo) or false",
            "providers/python-local.yaml: runner: needs to be the runner type its definitions have (comfyui, "
            "process, api)",
            "providers/unlabelled.yaml: label: needs to be its name in the menus, e.g. ComfyUI Local",
            "workflows/tasks.yaml: needs to be the tasks in menu order, e.g. masking: {label: Masking}"])

    def test_tasks_and_labels(self):
        root = self.catalogue(tasks={"masking": {"label": "Masking"}, "Video": {"label": "Video"}})
        self.assertEqual(C.load(root).errors, ["workflows/tasks.yaml: Video isn't a task's name: lower-case letters, "
                                               "digits and dashes, starting with a letter (masking)"])
        root = self.catalogue(tasks={"masking": {"label": "Masking", "order": 1}})
        self.assertEqual(C.load(root).errors, ["workflows/tasks.yaml: masking: needs to be a section with label and "
                                               "description, e.g. {label: Masking}"])
        root = self.catalogue(tasks={"masking": {"label": "Masking"}, "mattes": {"label": "masking"}})
        self.assertEqual(C.load(root).errors, ["two tasks are labelled masking: the menus couldn't tell them apart"])

    def test_missing_pieces(self):
        root = self.catalogue({"comfyui-local": {"label": "ComfyUI Local", "runner": "comfyui", "target": "pool"}})
        self.assertEqual(C.load(root).errors, [
            "providers/python-local.yaml is missing: it is the provider of runner.type process definitions"])
        (root / "workflows" / "tasks.yaml").unlink()
        shutil.rmtree(root / "providers")
        self.assertEqual(C.load(root).errors, [f"providers/ isn't in {root}: the menus have no providers",
                                               f"workflows/tasks.yaml isn't in {root}: the menus have no tasks"])

    def test_the_problems_of_a_place(self):
        cat = C.load(self.catalogue())
        self.assertEqual(cat.problems("masking", "comfyui-local", "comfyui", named=False), [])
        self.assertEqual(cat.problems(None, "python-local", "process", named=True), [])
        self.assertEqual(cat.problems("relight", "comfyui-local", "comfyui", named=False),
                         ["task: relight isn't in workflows/tasks.yaml (it has masking, video)"])
        self.assertEqual(cat.problems("video", "api-thing", "api", named=False),
                         ["runner.type api has no provider: providers/api-thing.yaml is missing"])

    def test_current_reads_its_files_again_when_they_change(self):
        root = self.catalogue()
        first = C.current(root)
        self.assertIs(C.current(root), first)
        tasks = root / "workflows" / "tasks.yaml"
        write_yaml(tasks, {"masking": {"label": "Masking"}, "video": {"label": "Video"}, "image": {"label": "Image"}})
        st = tasks.stat()
        os.utime(tasks, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        again = C.current(root)
        self.assertIsNot(again, first)
        self.assertEqual(list(again.tasks), ["masking", "video", "image"])


def defn(name, label=None, task=None, provider="comfyui-local", family=None, hidden=False, enabled=True,
         problems=()):
    """What the tree reads of a definition."""
    return SimpleNamespace(name=name, label=label or name, description=f"{name}'s description", task=task,
                           provider=provider, family=family, hidden=hidden, enabled=enabled,
                           problems=list(problems))


def flat(tree) -> list[tuple]:
    return [(p["label"], t["label"], w["name"]) for p in tree for t in p["tasks"] for w in t["workflows"]]


class TreeTests(unittest.TestCase):
    """The menus GET /api/v1/catalog returns: providers, then tasks, then workflows by label."""

    def test_the_farms_menus(self):
        cat = C.load()
        reg = DefinitionRegistry(REPO / "workflows")
        shown = flat(cat.tree(reg.workflows.values()))
        fal = [x for x in shown if x[0] == "fal.ai"]
        self.assertEqual(shown[:len(shown) - len(fal)], [
            ("ComfyUI Local", "Masking", "ltx25_alpha_gen"), ("ComfyUI Local", "Masking", "sam3_ltx_alpha"),
            ("ComfyUI Local", "Video", "ltx25_layout_render"), ("ComfyUI Local", "Image", "wan22_i2i"),
            ("ComfyUI Local", "Textures", "chord_pbr")])
        # fal.ai's last (its order: 30), each under its task: as many as are reviewed in.
        self.assertEqual(sorted(fal), sorted(("fal.ai", cat.tasks[wf.task].label, n) for n, wf in reg.workflows.items()
                                             if wf.runner_type == "api"))
        self.assertIn(("fal.ai", "Image", "nano_banana2_edit_fal"), fal)
        tree = cat.tree(reg.workflows.values(), hidden=True)
        self.assertEqual(flat(tree)[-len(fal) - 2:], [("Python Local", "Tests", "batch_test"),
                                                     ("Python Local", "Tests", "gpu_test"), *fal])
        self.assertEqual({k: tree[0][k] for k in ("name", "label", "runner", "target", "offsite")},
                         {"name": "comfyui-local", "label": "ComfyUI Local", "runner": "comfyui", "target": "pool",
                          "offsite": False})
        self.assertEqual({k: tree[0]["tasks"][0][k] for k in ("name", "label", "description")},
                         {"name": "masking", "label": "Masking", "description": "Mattes and alphas of a plate."})
        sam3 = tree[0]["tasks"][0]["workflows"][1]
        self.assertEqual(sam3, {
            "name": "sam3_ltx_alpha", "label": "SAM3 x LTX alpha (one subject)",
            "description": reg.workflows["sam3_ltx_alpha"].description, "task": "masking",
            "provider": "comfyui-local", "family": None, "offsite": False, "hidden": False, "enabled": True,
            "problems": [], "allowed": True, "reason": None, "rule": None, "siblings": []})
        self.assertTrue(tree[1]["tasks"][0]["workflows"][0]["hidden"])

    def test_by_label_and_in_the_catalogues_order(self):
        cat = C.load()
        tree = cat.tree([defn("b", "Zed", "video"), defn("a", "alpha", "video"), defn("c", "Mid", "masking"),
                         defn("p", "Proc", "tests", provider="python-local")])
        self.assertEqual(flat(tree), [("ComfyUI Local", "Masking", "c"), ("ComfyUI Local", "Video", "a"),
                                      ("ComfyUI Local", "Video", "b"), ("Python Local", "Tests", "p")])

    def test_what_the_show_may_not_use_stays_and_says_why(self):
        cat = C.load()
        tree = cat.tree([defn("a", task="video"), defn("b", task="video")],
                        refusal=lambda wf: ("b can't run for show x: no.", "x-rule") if wf.name == "b" else None)
        a, b = tree[0]["tasks"][0]["workflows"]
        self.assertEqual((a["allowed"], a["reason"], a["rule"]), (True, None, None))
        self.assertEqual((b["allowed"], b["reason"], b["rule"]), (False, "b can't run for show x: no.", "x-rule"))

    def test_siblings_are_the_same_family_elsewhere(self):
        cat = C.load()
        tree = cat.tree([defn("seed_local", "Seedance", "video", family="seedance-2"),
                         defn("seed_py", "Seedance py", "video", provider="python-local", family="seedance-2"),
                         defn("seed_hidden", "Seedance test", "video", family="seedance-2", hidden=True),
                         defn("other", "Other", "video")])
        rows = {w["name"]: w for p in tree for t in p["tasks"] for w in t["workflows"]}
        self.assertEqual(rows["seed_local"]["siblings"], [{"name": "seed_py", "label": "Seedance py",
                                                           "provider": "python-local",
                                                           "provider_label": "Python Local"}])
        self.assertEqual([s["name"] for s in rows["seed_py"]["siblings"]], ["seed_local"])
        self.assertEqual(rows["other"]["siblings"], [])
        self.assertNotIn("seed_hidden", rows)

    def test_nothing_is_lost_from_a_menu(self):
        """No task: under Other, last. A task or provider the catalogue lacks (a broken file):
        under its own name, after the known ones. A turned-off definition: listed, with why."""
        cat = C.load()
        tree = cat.tree([defn("plain", task=None), defn("known", task="video"), defn("odd", task="relight"),
                         defn("far", task="video", provider="elsewhere"),
                         defn("off", task="video", enabled=False, problems=["task: relight isn't in ..."])])
        self.assertEqual(flat(tree), [("ComfyUI Local", "Video", "known"), ("ComfyUI Local", "Video", "off"),
                                      ("ComfyUI Local", "relight", "odd"), ("ComfyUI Local", "Other", "plain"),
                                      ("elsewhere", "Video", "far")])
        self.assertEqual(tree[0]["tasks"][2]["name"], None)
        self.assertEqual(tree[1]["runner"], None)
        off = tree[0]["tasks"][0]["workflows"][1]
        self.assertEqual((off["enabled"], off["problems"]), (False, ["task: relight isn't in ..."]))

    def test_an_empty_provider_or_task_isnt_listed(self):
        self.assertEqual(C.load().tree([]), [])
        tree = C.load().tree([defn("t", task="tests", provider="python-local", hidden=True)])
        self.assertEqual(tree, [])


class ClientTests(unittest.TestCase):
    """The shared client: catalog() and `python comfyfarm_client.py catalog`."""

    def test_it_asks_for_a_show(self):
        with mock.patch.object(client, "_request", return_value={"providers": []}) as r:
            client.catalog()
            client.catalog("bri3", hidden=True)
        self.assertEqual([c.args for c in r.call_args_list],
                         [("GET", "/api/v1/catalog"), ("GET", "/api/v1/catalog?show=bri3&hidden=true")])

    def test_it_prints_the_menus(self):
        cat = C.load()
        tree = cat.tree([defn("sam3", "SAM3", "masking"), defn("wan", "Wan", "image"),
                         defn("t", "GPU test", "tests", provider="python-local", hidden=True)], hidden=True,
                        refusal=lambda wf: ("wan can't run for show bri3: no.", "r") if wf.name == "wan" else None)
        out = io.StringIO()
        with mock.patch.object(client, "_request", return_value={"providers": tree, "errors": ["x: broken"]}) as r, \
                redirect_stdout(out):
            self.assertEqual(client.main(["catalog", "show=bri3", "--hidden"]), 0)
        self.assertEqual(r.call_args.args, ("GET", "/api/v1/catalog?show=bri3&hidden=true"))
        self.assertEqual(out.getvalue().splitlines(), [
            "ComfyUI Local", "  Masking", "    SAM3  [sam3]", "  Image",
            "    Wan  [wan]  (not on this show: wan can't run for show bri3: no.)",
            "Python Local", "  Tests", "    GPU test  [t]  (hidden)", "menus: x: broken"])
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(client.main(["catalog", "shot=0010"]), 1)
        self.assertIn("catalog takes show=, not shot", err.getvalue())


class CheckTests(unittest.TestCase):
    def test_check_says_where_each_workflow_shows(self):
        out = io.StringIO()
        with redirect_stdout(out):
            bad = cli.check_catalog() + cli.check_workflows(DefinitionRegistry(REPO / "workflows"))
        self.assertEqual(bad, 0)
        text = out.getvalue()
        self.assertIn("[ ok ] menus: 3 provider(s) (ComfyUI Local, Python Local, fal.ai), 5 task(s) (Masking, Video, "
                      "Image, Textures, Tests)", text)
        self.assertIn("workflow sam3_ltx_alpha (", text)
        self.assertIn("in the menus under ComfyUI Local > Masking", text)
        self.assertIn("in the menus under Python Local > Tests (hidden from artists)", text)


if __name__ == "__main__":
    unittest.main()
