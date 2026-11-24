"""The gateway (decision 0055): API definitions (runner api), their providers (fal.ai, off
until config.yaml switches it on), and the desk sending API jobs only to a gateway agent's
lanes, never to a GPU machine, and nothing else to the gateway."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from comfyfarm import catalog as C
from comfyfarm.config import Config
from comfyfarm.definitions import Definition, DefinitionRegistry
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.desk import Desk
from comfyfarm.manager.events import EventBus
from comfyfarm.manager import policy as PO
from comfyfarm.manager.farm import Farm, FarmError
from comfyfarm.protocol import now

REPO = Path(__file__).resolve().parent.parent
PLATE = str(REPO / "workflows/gpu_test/eval/input.png")
# fal.ai is outside Rodeo: no show may use it until the farm's policy allows it (decision 0058).
POLICY = """version: 1
providers:
  - name: pilot-fal-on-rnd
    shows: [rnd]
    allow: [fal]
    reason: The fal.ai pilot runs on rnd only
"""

EDIT = {   # an API definition, as nano_banana2_edit_fal will be
    "id": "fal_edit", "label": "Image edit (fal)", "task": "image", "provider": "fal",
    "family": "test-edit", "models": [],
    "params": {
        "input": {"type": "input", "takes": "image", "label": "Image", "required": True, "primary": True},
        "reference": {"type": "input", "takes": "image", "label": "Reference"},
        "prompt": {"type": "text", "label": "Prompt", "required": True},
        "resolution": {"type": "choice", "choices": ["1K", "2K"], "default": "1K"},
        "output_path": {"type": "output_folder", "label": "Output folder"}},
    "writes": {"frames": {"ext": "png"}},
    "runner": {"type": "api", "endpoint": "fal-ai/nano-banana-2/edit",
               "upload": {"input": "image_urls", "reference": "image_urls"},
               "send": {"prompt": "prompt", "resolution": "resolution"},
               "fixed": {"num_images": 1, "output_format": "png", "enable_web_search": False}}}


def edit(**changes) -> dict:
    p = yaml.safe_load(yaml.safe_dump(EDIT))
    for path, v in changes.items():
        *parents, last = path.split("__")
        d = p
        for k in parents:
            d = d[k]
        if v is None:
            d.pop(last, None)
        else:
            d[last] = v
    return p


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def definition(self, p: dict, name: str = "fal_edit") -> Definition:
        folder = self.tmp / "workflows" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        return Definition(folder)


class AnApiDefinition(Tmp):
    def test_it_loads(self):
        wf = self.definition(EDIT)
        self.assertEqual(wf.problems, [])
        self.assertEqual((wf.runner_type, wf.targets, wf.provider, wf.task, wf.template, wf.process),
                         ("api", ["gateway"], "fal", "image", None, None))
        self.assertEqual(wf.api, {"endpoint": "fal-ai/nano-banana-2/edit",
                                  "upload": {"input": "image_urls", "reference": "image_urls"},
                                  "send": {"prompt": "prompt", "resolution": "resolution"},
                                  "fixed": {"num_images": 1, "output_format": "png", "enable_web_search": False}})
        self.assertEqual(wf.outputs, {"frames": {"ext": "png"}})
        self.assertEqual(wf.sequence_param, "input")
        again = self.definition(EDIT, "again")
        self.assertEqual(again.digest, wf.digest)

    def test_what_is_wrong_with_its_runner_block(self):
        cases = {
            "runner: an API runner needs endpoint (runner: {type: api, endpoint: fal-ai/nano-banana-2/edit, upload: "
            "{input: image_urls}, send: {prompt: prompt}, fixed: {num_images: 1}})": edit(runner__endpoint=None),
            "runner block: unknown key(s) environment (an API runner takes endpoint and upload and send and fixed)":
                edit(runner__environment="x"),
            "runner.template: is ComfyUI wiring; an API runner takes endpoint and upload and send and fixed":
                edit(runner__template="x.json"),
            "runner.endpoint: needs to be the provider's model path, e.g. fal-ai/nano-banana-2/edit":
                edit(runner__endpoint="nano banana"),
            "runner.upload.prompt: only an input (type: input) is uploaded; send: sends the others":
                edit(runner__upload={"prompt": "image_urls"}),
            "runner.send.input: an input is uploaded (upload:), and the output folder is the farm's, never sent":
                edit(runner__send={"input": "image"}),
            "runner.send.output_path: an input is uploaded (upload:), and the output folder is the farm's, never "
            "sent": edit(runner__send={"output_path": "out"}),
            "runner.send.seed: no such parameter in params": edit(runner__send={"seed": "seed"}),
            "runner.send.prompt: needs to be the provider's field name, e.g. image_urls":
                edit(runner__send={"prompt": "the prompt"}),
            "runner.fixed.num_images: also sent from a param; a fixed field is never the artist's":
                edit(runner__send={"prompt": "num_images"}),
            "runner.fixed.size: needs to be a value (text, a number, true or false, or a list of them)":
                edit(runner__fixed={"size": {"w": 1}}),
            "runner.upload: needs to be a section, e.g. {input: image_urls}": edit(runner__upload=["input"]),
            "params.prompt: loader is ComfyUI wiring; an API definition sends them as its runner block's upload: "
            "and send: say": edit(params__prompt={"type": "text", "loader": "x"}),
            "writes.preview: own isn't something an API definition can do; true (the farm makes the preview movie of "
            "its frames) or false": edit(writes={"frames": {"ext": "png"}, "preview": "own"}),
        }
        for message, p in cases.items():
            with self.subTest(message=message[:60]):
                reg = DefinitionRegistry(self.tmp / "nowhere")
                folder = self.tmp / "workflows" / "bad"
                shutil.rmtree(folder, True)
                folder.mkdir(parents=True)
                (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
                reg.directory = self.tmp / "workflows"
                reg.reload()
                self.assertEqual(reg.load_errors.get("bad"), message)

    def test_its_head(self):
        self.assertEqual(self.definition(edit(provider=None)).problems,
                         ["provider: an API definition names its provider, one of providers/ (provider: fal)"])
        self.assertEqual(self.definition(edit(provider="comfyui-local")).problems,
                         ["provider: comfyui-local runs runner.type comfyui definitions, and this one is runner.type "
                          "api"])
        both = "targets: an API definition's jobs go to the gateway only ([gateway]), and no other definition's do"
        self.assertEqual(self.definition(edit(targets=["pool"])).problems, [both])
        self.assertEqual(self.definition(edit(targets=["gateway", "pool"])).problems, [both])
        gpu = REPO / "workflows" / "gpu_test"
        folder = self.tmp / "workflows" / "gpu_test"
        shutil.copytree(gpu, folder)
        p = yaml.safe_load((folder / "process.yaml").read_text(encoding="utf-8"))
        p["targets"] = ["gateway"]
        (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        self.assertIn(both, Definition(folder).problems)

    def test_a_manifest_cant_be_one(self):
        folder = self.tmp / "workflows" / "old"
        folder.mkdir(parents=True)
        (folder / "manifest.yaml").write_text("api: {endpoint: x/y}\n", encoding="utf-8")
        reg = DefinitionRegistry(self.tmp / "workflows")
        self.assertEqual(reg.load_errors["old"], "api: an API definition is a process.yaml (runner: {type: api})")


class TheFalProvider(Tmp):
    def test_the_repos(self):
        fal = C.load().providers["fal"]
        self.assertEqual((fal.label, fal.runner, fal.target, fal.offsite, fal.adapter, fal.lanes, fal.keys),
                         ("fal.ai", "api", "gateway", True, "fal", 2, ("requests", "billing")))
        self.assertEqual(list(C.load().providers), ["comfyui-local", "python-local", "fal"])
        self.assertEqual(C.load().providers["comfyui-local"].lanes, 0)

    def test_what_is_wrong_with_an_api_provider(self):
        root = self.tmp / "checkout"
        good = {"label": "X", "runner": "api", "target": "gateway", "adapter": "fal", "lanes": 2, "keys": ["requests"]}
        local = {"comfyui-local": {"label": "ComfyUI Local", "runner": "comfyui", "target": "pool"},
                 "python-local": {"label": "Python Local", "runner": "process", "target": "pool"}}
        cases = {
            "target: an API provider's jobs go to the gateway (runner: api, target: gateway), and no other "
            "provider's do": {**good, "target": "pool"},
            "adapter: needs to be the name of the gateway's code that speaks to it, e.g. fal": {**good, "adapter": ""},
            "lanes: needs to be a whole number, 1 or more: its jobs at once on a gateway": {**good, "lanes": 0},
            "keys: needs to be a list of names for the keys gateway.yaml holds, e.g. [requests, billing] (never the "
            "keys themselves)": {**good, "keys": ["fal_live_abc:def"]},
        }
        for message, data in cases.items():
            with self.subTest(message=message[:40]):
                shutil.rmtree(root, True)
                for name, d in {**local, "x": data}.items():
                    (root / "providers").mkdir(parents=True, exist_ok=True)
                    (root / "providers" / f"{name}.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
                (root / "workflows").mkdir()
                (root / "workflows" / "tasks.yaml").write_text("image: {label: Image}\n", encoding="utf-8")
                self.assertEqual(C.load(root).errors, [f"providers/x.yaml: {message}"])
        shutil.rmtree(root / "providers")
        (root / "providers").mkdir()
        for name, d in {**local, "comfyui-local": {**local["comfyui-local"], "lanes": 2}}.items():
            (root / "providers" / f"{name}.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
        self.assertEqual(C.load(root).errors, ["providers/comfyui-local.yaml: lanes: only an API provider (runner: "
                                               "api) has adapter, lanes, keys"])


A6000, GATEWAY = "tor-flame02-a6000", "wrk1125l-gateway"


class Manager(Tmp):
    """A farm with the GPU test, an API definition, a GPU machine and a gateway."""

    def setUp(self, enabled=("fal",)):
        super().setUp()
        root = str(self.tmp).replace("\\", "/")
        shutil.copytree(REPO / "workflows" / "gpu_test", self.tmp / "workflows" / "gpu_test")
        self.definition(EDIT)
        self.definition(edit(id="fal_other", label="Other edit"), "fal_other")
        (self.tmp / "shows").mkdir()
        (self.tmp / "policy.yaml").write_text(POLICY, encoding="utf-8")
        self.cfg = Config.model_validate({"database": f"{root}/farm.db", "workers": [],
                                          "workflows_dir": f"{root}/workflows",
                                          "convert": {"scratch_dir": f"{root}/scratch"},
                                          "paths": {"mappings": [{"posix": f"{root}/shows",
                                                                  "windows": f"{root}/shows"}]},
                                          "providers": {"enabled": list(enabled)},
                                          "policy": {"enabled": True, "file": f"{root}/policy.yaml"}})
        self.store = JobStore(self.tmp / "farm.db")
        self.addCleanup(self.store.close)
        self.farm = Farm(self.cfg, self.store, DefinitionRegistry(self.tmp / "workflows"), EventBus())
        self.desk = Desk(self.farm)

    def up(self, m, lanes=None, problems=None):
        self.desk.hello({"name": m, "comfyui": "x", "max_inflight": 1, "jobs": [], "lanes": lanes or {}})
        self.desk.status({"name": m, "online": True, "connected": True})
        return self.desk.nodes({"name": m, "processes": {
            n: {"digest": wf.digest, "problems": (problems or {}).get(n, [])}
            for n, wf in self.farm.registry.workflows.items()}})

    def submit(self, workflow="fal_edit", show="rnd", **params) -> dict:
        values = {"input": PLATE, "output_path": str(self.tmp / "shows")}
        if workflow != "gpu_test":
            values["prompt"] = "rain"
        return self.farm.submit(workflow, {**values, **params}, "ann", show=show)

    def claimed(self, m) -> list[str]:
        return [p["job"] for p in self.desk._claim(self.desk.agents[m])]


class Placement(Manager):
    def test_api_jobs_go_to_the_gateway_and_nothing_else_does(self):
        self.up(A6000)
        self.up(GATEWAY, lanes={"fal": 2})
        api, gpu = self.submit(), self.submit("gpu_test")
        self.assertEqual(self.claimed(A6000), [gpu["id"]])
        self.assertEqual(self.claimed(GATEWAY), [api["id"]])
        self.assertEqual(self.store.get(api["id"])["gpu"], "gateway")

    def test_lanes_are_its_jobs_at_once_per_provider(self):
        self.up(GATEWAY, lanes={"fal": 2})
        jobs = [self.submit()["id"] for _ in range(3)]
        self.assertEqual(self.claimed(GATEWAY), jobs[:2])
        self.assertEqual(self.claimed(GATEWAY), [])                       # both lanes taken
        self.store.transition(jobs[0], ("preparing",), status="done", finished_at=now())
        self.assertEqual(self.claimed(GATEWAY), [jobs[2]])

    def test_a_gateway_without_lanes_for_the_provider(self):
        self.up(GATEWAY, lanes={"higgsfield": 3})
        job = self.submit()
        self.assertEqual(self.claimed(GATEWAY), [])
        self.assertEqual(self.desk.unmet(self.desk.agents[GATEWAY], "fal_edit"), "has no lanes for fal (gateway.yaml)")
        self.assertEqual(self.store.get(job["id"])["status"], "queued")

    def test_api_jobs_never_wait_behind_gpu_jobs(self):
        self.up(A6000)
        self.up(GATEWAY, lanes={"fal": 1})
        gpu = [self.submit("gpu_test")["id"] for _ in range(4)]
        api = self.submit()
        self.assertEqual(self.claimed(GATEWAY), [api["id"]])
        self.assertEqual(self.claimed(A6000), gpu[:2])                    # its GPU and one ahead, as before

    def test_what_each_reports_it_can_run(self):
        """A GPU machine isn't asked about API definitions, nor the gateway about others:
        neither is said to lack them."""
        self.assertEqual(self.up(A6000), {"problems": {}})
        self.assertEqual(self.up(GATEWAY, lanes={"fal": 2}), {"problems": {}})
        machines = {w["name"]: w for w in self.desk.summaries()}
        self.assertEqual((machines[GATEWAY]["lanes"], machines[GATEWAY]["skips"]), ({"fal": 2}, {}))
        self.assertEqual((machines[A6000]["lanes"], machines[A6000]["skips"]), ({}, {}))
        self.assertEqual(self.desk.unmet(self.desk.agents[A6000], "fal_edit"),
                         "is a GPU machine: API jobs go to the gateway")
        self.assertEqual(self.desk.unmet(self.desk.agents[GATEWAY], "gpu_test"),
                         "is the gateway: it runs only API jobs")

    def test_waiting_for_a_gateway(self):
        self.up(A6000)
        self.assertEqual(self.desk.requires_note("fal_edit"),
                         "No gateway with lanes for fal has checked in: the job waits until one does (gateway.yaml "
                         "on the manager's machine).")
        self.up(GATEWAY, lanes={"fal": 2})
        self.assertIsNone(self.desk.requires_note("fal_edit"))
        self.desk.set_drained(GATEWAY, True, "peter")
        self.assertEqual(self.desk.requires_note("fal_edit"),
                         f"The gateway ({GATEWAY}) is offline or drained: the job waits.")
        self.assertIsNone(self.desk.requires_note("gpu_test"))

    def test_the_forecast_runs_lanes_side_by_side(self):
        self.up(GATEWAY, lanes={"fal": 2})
        for _ in range(3):
            self.store.record_gpu_run("fal_edit", "gateway", 1, 30.0)     # past runs: 30 s each
        jobs = [self.submit()["id"] for _ in range(3)]
        self.assertEqual(self.claimed(GATEWAY), jobs[:2])
        f = self.desk.forecast()
        self.assertEqual({f[j]["worker"] for j in jobs}, {GATEWAY})
        self.assertAlmostEqual(f[jobs[0]]["done_in"], 30, delta=1)       # two at once,
        self.assertAlmostEqual(f[jobs[1]]["done_in"], 30, delta=1)
        self.assertAlmostEqual(f[jobs[2]]["done_in"], 60, delta=1)       # then the third in the first lane free


class NotAGpuMachine(Manager):
    """Rollouts and ComfyUI syncs are for GPU machines: the gateway is left out, and refused
    when named (decision 0056)."""

    def test_rollouts_and_syncs_leave_the_gateway_out(self):
        from comfyfarm.manager.rollouts import RolloutError
        from comfyfarm.manager.syncs import SyncError
        self.up(A6000)
        self.up(GATEWAY, lanes={"fal": 2})
        r = self.desk.rollouts.start("demo", "new", None, "peter")
        self.assertEqual(r["machines"], [A6000])
        self.desk.rollouts.act("demo", "cancel", "peter")
        with self.assertRaisesRegex(RolloutError, f"{GATEWAY} is the gateway: it sends jobs to outside providers "
                                                  f"and builds no runner environments"):
            self.desk.rollouts.start("demo", "new", [GATEWAY], "peter")
        s = self.desk.syncs.start("comfyui", "deploy/comfyui/environment.yaml@abc", None, False, "peter")
        self.assertEqual(s["machines"], [A6000])
        self.desk.syncs.cancel("peter")
        with self.assertRaisesRegex(SyncError, f"{GATEWAY} is the gateway: it has no ComfyUI to sync"):
            self.desk.syncs.start("comfyui", "deploy/comfyui/environment.yaml@abc", [A6000, GATEWAY], False, "peter")


class OffSite(Manager):
    """Step 3's gate (decision 0058): an outside provider's job is refused on every show but the
    one a rule allows, with the rule named, when it is planned, in the menus and at dispatch."""

    def setUp(self):
        super().setUp()
        stat = mock.patch.object(PO, "STAT_SECONDS", 0)
        stat.start()
        self.addCleanup(stat.stop)

    def write_policy(self, text):
        path = self.tmp / "policy.yaml"
        path.write_text(text, encoding="utf-8")
        st = path.stat()                                    # seen as changed, even within the same second
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    def test_every_show_but_the_allowed_one(self):
        self.assertEqual(self.submit(show="rnd")["status"], "queued")
        for show in ("bri3", "xyz"):
            with self.subTest(show=show), self.assertRaises(FarmError) as e:
                self.submit(show=show)
            self.assertEqual((str(e.exception), e.exception.status),
                             (f"Show {show} isn't cleared to send work to fal.ai, outside Rodeo: no policy rule allows "
                              f"it. (Policy: providers.)", 403))
        with self.assertRaisesRegex(FarmError, "fal.ai, outside Rodeo, so the farm's policy needs to know the show"):
            self.submit(show=None)                          # the plate is in the repo: no show of its own
        self.assertEqual(self.submit("gpu_test", show="xyz")["status"], "queued")     # the farm's own: unchanged

    def test_the_menus_say_why(self):
        def fal(show):
            tree = self.farm.catalogue(show)
            p = next(x for x in tree["providers"] if x["name"] == "fal")
            return [(w["name"], w["allowed"], w["rule"]) for t in p["tasks"] for w in t["workflows"]]
        self.assertEqual(fal("rnd"), [("fal_edit", True, None), ("fal_other", True, None)])
        self.assertEqual(fal("bri3"), [("fal_edit", False, "providers"), ("fal_other", False, "providers")])
        self.assertEqual(fal(None), [("fal_edit", True, None), ("fal_other", True, None)])   # a menu for any show

    def test_a_rule_taken_away_fails_its_queued_jobs(self):
        self.up(GATEWAY, lanes={"fal": 2})
        job = self.submit(show="rnd")
        self.write_policy("version: 1\n")
        self.assertEqual(self.claimed(GATEWAY), [])
        j = self.store.get(job["id"])
        self.assertEqual((j["status"], j["error"]),
                         ("failed", "Not run: the farm's policy changed after it was queued. Show rnd isn't cleared to "
                                    "send work to fal.ai, outside Rodeo: no policy rule allows it. (Policy: "
                                    "providers.)"))

    def test_with_policy_off_nothing_goes_outside(self):
        self.farm.policy.enabled = False
        with self.assertRaisesRegex(FarmError, "runs on fal.ai, outside Rodeo, which the farm's policy must allow, "
                                               "and the policy is off"):
            self.submit(show="rnd")
        self.assertEqual(self.submit("gpu_test", show="rnd")["status"], "queued")

    def test_an_eval_of_an_outside_providers_definition(self):
        """The farm's own reference jobs skip the show rules, but have no show: one at an
        outside provider is refused until evals of paid models have their show (steps 4, 5)."""
        with self.assertRaisesRegex(FarmError, "needs to know the show"):
            self.farm.plan("fal_edit", {"input": PLATE, "prompt": "rain", "output_path": str(self.tmp / "shows")},
                           evaluation="test")


class Switch(Manager):
    def setUp(self):
        super().setUp(enabled=())

    def test_a_provider_off_is_neither_in_the_menus_nor_planned(self):
        self.assertIn("fal_edit", self.farm.registry.workflows)
        names = [w["name"] for p in self.farm.catalogue(None)["providers"] for t in p["tasks"]
                 for w in t["workflows"]]
        self.assertNotIn("fal_edit", names)
        with self.assertRaises(FarmError) as e:
            self.submit()
        self.assertEqual((str(e.exception), e.exception.status),
                         ("Image edit (fal) runs on fal.ai, which isn't switched on on this farm (providers.enabled "
                          "in the manager's config.yaml).", 403))
        self.assertIsNone(self.farm.provider_off(self.farm.registry.workflows["gpu_test"]))   # the farm's own: on

    def test_switched_off_after_it_was_queued(self):
        self.cfg.providers.enabled = ["fal"]
        job = self.submit()
        self.cfg.providers.enabled = []
        self.up(GATEWAY, lanes={"fal": 2})
        self.assertEqual(self.claimed(GATEWAY), [])
        j = self.store.get(job["id"])
        self.assertEqual((j["status"], j["error"]),
                         ("failed", "Not run: Image edit (fal) runs on fal.ai, which isn't switched on on this farm "
                                    "(providers.enabled in the manager's config.yaml)."))

    def test_switched_on(self):
        self.cfg.providers.enabled = ["fal"]
        fal = [p for p in self.farm.catalogue(None)["providers"] if p["name"] == "fal"]
        self.assertEqual([w["name"] for w in fal[0]["tasks"][0]["workflows"]], ["fal_edit", "fal_other"])
        self.assertTrue(fal[0]["offsite"])


if __name__ == "__main__":
    unittest.main()
