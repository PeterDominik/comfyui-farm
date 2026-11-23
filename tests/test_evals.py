"""Evals (decision 0053), the manager's part: a definition's eval: section and what is wrong
with it; an eval as a job pinned to its machine, planned from the definition's own files into
the farm's scratch folder, the policy aside; a drained machine taking its eval and nothing
else; and rollouts and ComfyUI syncs that wait for each machine's evals (evals.gate) and
stop at one that fails. What the agent checks is in test_eval_checks; the endpoint and a real
eval through an agent in test_e2e.

    python -m unittest tests.test_evals -v
"""

import copy
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from comfyfarm.config import Config
from comfyfarm.definitions import Definition, DefinitionRegistry
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.desk import Desk
from comfyfarm.manager.evals import PRIORITY, EvalError
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.protocol import now

REPO = Path(__file__).resolve().parent.parent
ORDER = ["tor-flame02-a6000", "wrk59467w-5090"]
A6000, W5090 = ORDER


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TheSection(unittest.TestCase):
    """What a definition's eval: says, and what is wrong with it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def gpu_test(self, edit=None) -> Definition:
        folder = self.tmp / "gpu_test"
        if folder.exists():
            shutil.rmtree(folder)
        shutil.copytree(REPO / "workflows" / "gpu_test", folder)
        p = yaml.safe_load((folder / "process.yaml").read_text(encoding="utf-8"))
        if edit:
            edit(p)
        (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        return Definition(folder)

    def test_the_farms_evals_load(self):
        reg = DefinitionRegistry(REPO / "workflows")
        self.assertEqual(reg.load_errors, {})
        gpu, chord = reg.workflows["gpu_test"], reg.workflows["chord_pbr"]
        self.assertEqual((gpu.head_problems, chord.head_problems), ([], []))
        self.assertEqual(gpu.eval_files(), ["eval/input.png", "eval/expected.png"])
        self.assertEqual(chord.eval_files(), ["eval/diffuse.1001.png"])
        self.assertEqual(gpu.eval["checks"]["like"][0]["max_error"], 0.002)

    def test_what_is_wrong_with_one(self):
        def bad(p):
            p["eval"] = {"input": "x", "inputs": {"plate": "eval/input.png", "input": "../elsewhere.png"},
                         "params": {"nope": 1, "output_path": "/tmp", "operation": "invert"},
                         "checks": {"files": {"png": -1}, "size": [64], "like": [
                             {"output": "a/b.png", "expected": "eval/missing.png", "max_error": 2}], "speed": 1}}
        text = " | ".join(self.gpu_test(bad).head_problems)
        for said in ("eval: input isn't something an eval has (inputs, params, checks)",
                     "eval.inputs: plate isn't one of its inputs (input)",
                     "eval.inputs.input: ../elsewhere.png needs to be a path inside the definition's folder",
                     "eval.params: nope isn't one of its params",
                     "eval.params: output_path is the output folder: the farm picks it",
                     "eval.checks: speed isn't a check (files, size, like)",
                     "eval.checks.files: needs to be how many files of each extension",
                     "eval.checks.size: needs to be [width, height]",
                     "eval.checks.like[0].output: needs to be the file name it writes",
                     "eval.checks.like[0].expected: eval/missing.png isn't in gpu_test/",
                     "eval.checks.like[0].max_error: needs to be how far it may be from it on average, 0 to 1"):
            self.assertIn(said, text)
        self.assertNotIn("operation", text)                     # a param it has, with a value it takes
        none = self.gpu_test(lambda p: p.update(eval={"inputs": {"input": "eval/input.png"}}))
        self.assertIn("eval.checks: needs at least one check (files, size, like)", none.head_problems)
        given = self.gpu_test(lambda p: p.update(eval={"checks": {"files": {"png": 1}}}))
        self.assertIn("eval.inputs: the required input input isn't given", given.head_problems)
        self.assertIn("eval: needs to be a section", " ".join(self.gpu_test(lambda p: p.update(eval="yes"))
                                                              .head_problems))

    def test_sequences_and_an_output_folder(self):
        def frames(p):
            p["eval"] = {"inputs": {"input": "eval/plate.%04d.png 1-2"}, "checks": {"files": {"png": 2}}}
        d = self.gpu_test(frames)
        self.assertIn("eval.inputs.input: no file matches eval/plate.%04d.png in gpu_test/", d.head_problems)
        for n in (1, 2):
            shutil.copy(d.dir / "eval/input.png", d.dir / f"eval/plate.{n:04d}.png")
        d = Definition(d.dir)
        self.assertEqual(d.head_problems, [])
        self.assertEqual(d.eval_files(), ["eval/plate.0001.png", "eval/plate.0002.png"])
        no_folder = self.gpu_test(lambda p: p["params"].pop("output_path"))
        self.assertIn("eval: needs an output_folder param, for the farm to put the eval's outputs in the machine's "
                      "scratch folder", no_folder.head_problems)


class Manager(unittest.TestCase):
    def setUp(self, gate=True):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = str(self.tmp).replace("\\", "/")
        self.cfg = Config.model_validate({"database": f"{root}/farm.db", "workers": [],
                                          "workflows_dir": str(REPO / "workflows"),
                                          "convert": {"scratch_dir": f"{root}/scratch"},
                                          "paths": {"mappings": [{"posix": f"{root}/shows",
                                                                  "windows": f"{root}/shows"}]},
                                          "rollouts": {"order": ORDER}, "evals": {"gate": gate}})
        (self.tmp / "shows").mkdir()
        self.store = JobStore(self.tmp / "farm.db")
        self.addCleanup(self.store.close)
        self.farm = Farm(self.cfg, self.store, DefinitionRegistry(REPO / "workflows"), EventBus())
        self.desk = Desk(self.farm)
        for m in ORDER:
            self.up(m)

    def up(self, m, problems=None):
        """Checking in, its runner up, able to run every definition (but those in `problems`)."""
        self.desk.hello({"name": m, "comfyui": "x", "max_inflight": 1, "jobs": []})
        self.desk.status({"name": m, "online": True, "connected": True})
        self.desk.nodes({"name": m, "processes": {
            n: {"digest": wf.digest, "problems": (problems or {}).get(n, [])}
            for n, wf in self.farm.registry.workflows.items()}})

    def claimed(self, m) -> list[str]:
        return [p["job"] for p in self.desk._claim(self.desk.agents[m])]

    def ends(self, job_id, status, error=None):
        self.store.transition(job_id, ("queued", "preparing", "dispatched", "running"), status=status,
                              error=error, finished_at=now())


class AnEval(Manager):
    def test_a_job_pinned_to_its_machine_from_the_definitions_files(self):
        job = self.desk.evals.start("gpu_test", A6000, "peter", "comfyfarm eval")
        wf = self.farm.registry.workflows["gpu_test"]
        folder = f"{self.desk.view.repo}/workflows/gpu_test"
        ev = job["info"]["eval"]
        self.assertEqual((ev["machine"], ev["for"], ev["definition"]), (A6000, "comfyfarm eval", "gpu_test"))
        self.assertEqual(ev["folder"], f"{self.farm.scratch_root}/evals/gpu_test")
        self.assertTrue(job["output_dir"].startswith(ev["folder"]), job["output_dir"])
        self.assertEqual(ev["files"], [[f"{folder}/eval/input.png", sha(wf.dir / "eval/input.png")],
                                       [f"{folder}/eval/expected.png", sha(wf.dir / "eval/expected.png")]])
        self.assertEqual(ev["checks"]["like"][0]["expected"], f"{folder}/eval/expected.png")
        self.assertEqual(job["params"]["operation"], "invert")
        self.assertTrue(job["info"]["input"]["farm_path"].startswith(f"{folder}/eval/input.png"))
        self.assertEqual((job["user"], job["name"], job["priority"], job["info"]["show"]),
                         ("peter", f"eval on {A6000}", PRIORITY, None))
        self.assertNotIn("root", ev)
        payload = self.desk._payload(self.store.get(job["id"], with_prompt=True), self.desk.agents[A6000])
        self.assertTrue(payload["info"]["eval"]["folder"].startswith("@scratch/evals/"))     # each machine's own
        self.assertTrue(payload["info"]["eval"]["files"][0][0].startswith("@repo/workflows/gpu_test/"))

    def test_the_policy_is_aside(self):
        self.farm.policy.enabled, self.farm.policy.rules = True, None      # no valid policy: nothing is planned
        with self.assertRaises(Exception):
            self.farm.plan("gpu_test", {"input": str(REPO / "workflows/gpu_test/eval/input.png")})
        job = self.desk.evals.start("gpu_test", A6000, "peter", "comfyfarm eval")
        self.assertIsNone(self.farm.refused_by_policy(job))
        self.assertEqual(self.claimed(A6000), [job["id"]])

    def test_what_stops_one_starting(self):
        for args, said in ((("nope", A6000), "no definition nope"),
                           (("ltx25_alpha_gen", A6000), "ltx25_alpha_gen has no eval: in its process.yaml"),
                           (("gpu_test", "wrk0000l"), "no machine wrk0000l")):
            with self.subTest(args=args), self.assertRaisesRegex(EvalError, said):
                self.desk.evals.start(*args, "peter", "comfyfarm eval")
        self.farm.registry.workflows["gpu_test"].requires["vram_gb"] = 4000
        with self.assertRaisesRegex(EvalError, f"{A6000} may not run gpu_test: it needs a GPU with 4000 GB"):
            self.desk.evals.start("gpu_test", A6000, "peter", "comfyfarm eval")

    def test_a_drained_machine_takes_its_eval_and_nothing_else(self):
        for m in ORDER:
            self.desk.set_drained(m, True, "peter")
        art = self.farm.submit("gpu_test", {"input": str(REPO / "workflows/gpu_test/eval/input.png"),
                                            "output_path": str(self.tmp / "shows")}, "ann")
        ev = self.desk.evals.start("gpu_test", A6000, "peter", "comfyfarm eval")
        theirs = self.desk.evals.start("gpu_test", W5090, "peter", "comfyfarm eval")
        self.assertEqual(self.claimed(W5090), [theirs["id"]])               # its own eval, not the other's
        self.assertEqual(self.claimed(A6000), [ev["id"]])                   # drained: its eval, not the artist's job
        self.assertEqual(self.store.get(art["id"])["status"], "queued")
        self.desk.set_drained(A6000, False, "peter")
        self.ends(ev["id"], "done")
        self.assertEqual(self.claimed(A6000), [art["id"]])
        self.assertEqual(self.desk.forecast()[art["id"]]["worker"], A6000)

    def test_one_its_machine_cant_run_fails(self):
        self.up(A6000, problems={"gpu_test": ["its environment gpu-test isn't built"]})
        job = self.desk.evals.start("gpu_test", A6000, "peter", "comfyfarm eval")
        self.assertEqual(self.claimed(A6000), [])
        j = self.store.get(job["id"])
        self.assertEqual(j["status"], "failed")
        self.assertIn(f"{A6000} can't run gpu_test: its environment gpu-test isn't built", j["error"])

    def test_how_they_went(self):
        a = self.desk.evals.begin(A6000, ["gpu_test", "chord_pbr"], "rollout x", "peter")
        self.assertEqual(self.desk.evals.judge(a), ("running", None))
        self.ends(a[0], "done")
        self.store.update(a[0], outputs={"eval": {"ok": True, "checks": [{"check": "size", "ok": True, "said": "1"}]}})
        self.assertEqual(self.desk.evals.judge(a), ("running", None))
        self.ends(a[1], "failed", "The eval failed: files .exr: 4 written, 5 wanted.")
        state, why = self.desk.evals.judge(a)
        self.assertEqual(state, "failed")
        self.assertIn("the eval of chord_pbr failed", why)
        self.assertIn("files .exr: 4 written, 5 wanted", why)
        rows = self.desk.evals.results(a)
        self.assertEqual([(r["definition"], r["machine"], r["status"]) for r in rows],
                         [("gpu_test", A6000, "done"), ("chord_pbr", A6000, "failed")])
        self.assertEqual(rows[0]["checks"][0]["check"], "size")
        b = self.desk.evals.begin_each("gpu_test", ORDER, "comfyfarm eval", "peter")
        self.desk.evals.cancel(b)
        self.assertEqual([self.store.get(i)["status"] for i in b], ["cancelled", "cancelled"])
        self.assertEqual(self.desk.evals.judge(b)[0], "failed")
        with self.assertRaises(EvalError):                                  # all or none
            self.desk.evals.begin(W5090, ["gpu_test", "nope"], "rollout y", "peter")
        self.assertEqual(self.store.queued(), [])


class Rollouts(Manager):
    """A rollout of gpu-test, the environment gpu_test runs in, with what the machines report
    set by hand."""

    def setUp(self, gate=True):
        super().setUp(gate)
        for m in ORDER:
            self.has(m, "old")

    def has(self, m, release):
        self.desk.agents[m].envs = {"gpu-test": {"current": release, "builds": {release: {"ok": True}}}}

    def evals_of(self, m) -> list[str]:
        return self.desk.rollouts.recent(1)[0]["eval_jobs"].get(m) or []

    def test_each_machine_drained_evaluated_then_given_jobs_again(self):
        r = self.desk.rollouts.start("gpu-test", "new", None, "peter")
        self.assertEqual(r["evals"], ["gpu_test"])
        self.assertEqual(self.desk.drained[A6000]["by"], f"rollout {r['id']}")    # before it has the release
        self.assertNotIn(W5090, self.desk.drained)
        self.has(A6000, "new")
        self.desk.rollouts.tick()
        [ev] = self.evals_of(A6000)
        job = self.store.get(ev)
        self.assertEqual((job["info"]["eval"]["machine"], job["info"]["eval"]["for"]), (A6000, f"rollout {r['id']}"))
        self.desk.rollouts.tick()
        self.assertEqual(self.desk.rollouts.recent(1)[0]["step"], 0)            # waits for it
        self.ends(ev, "done")
        self.desk.rollouts.tick()
        r = self.desk.rollouts.recent(1)[0]
        self.assertEqual(r["step"], 1)
        self.assertNotIn(A6000, self.desk.drained)                            # passed: takes jobs again
        self.assertIn(W5090, self.desk.drained)
        self.has(W5090, "new")
        self.desk.rollouts.tick()
        self.ends(self.evals_of(W5090)[0], "done")
        self.desk.rollouts.tick()
        r = self.desk.rollouts.recent(1)[0]
        self.assertEqual((r["state"], self.desk.drained), ("done", {}))

    def test_a_failed_eval_stops_it_with_the_machine_drained(self):
        self.desk.set_drained(W5090, True, "ann")                             # drained before: stays so
        self.desk.rollouts.start("gpu-test", "new", None, "peter")
        self.has(A6000, "new")
        self.desk.rollouts.tick()
        self.ends(self.evals_of(A6000)[0], "failed", "The eval failed: like *.png: mean error 0.2000, at most 0.002.")
        self.desk.rollouts.tick()
        r = self.desk.rollouts.open("gpu-test")
        self.assertEqual((r["state"], r["step"]), ("failed", 0))
        self.assertIn(f"{A6000}: the eval of gpu_test failed", r["error"])
        self.assertIn("mean error 0.2000", r["error"])
        self.assertIn(f"{A6000} stays drained", r["error"])
        self.assertIn(A6000, self.desk.drained)
        self.assertEqual(self.store.env_wanted().get(W5090, {}).get("gpu-test"), None)   # never told
        self.desk.rollouts.act("gpu-test", "rollback", "peter")
        self.assertNotIn(A6000, self.desk.drained)                            # back to what it had: jobs again
        self.assertEqual(self.desk.drained[W5090]["by"], "ann")

    def test_evals_too_slow_stop_it_and_cancelling_gives_jobs_back(self):
        r = self.desk.rollouts.start("gpu-test", "new", None, "peter")
        self.has(A6000, "new")
        self.desk.rollouts.tick()
        r = self.desk.rollouts.recent(1)[0]
        r["eval_started"] = now() - 31 * 60
        self.store.save_rollout(r)
        self.desk.rollouts.tick()
        r = self.desk.rollouts.recent(1)[0]
        self.assertEqual(r["state"], "failed")
        self.assertIn("its evals didn't finish within 30 minutes (evals.minutes)", r["error"])
        self.assertEqual(self.store.get(r["eval_jobs"][A6000][0])["status"], "cancelled")
        self.desk.rollouts.act("gpu-test", "cancel", "peter")
        self.assertIn(A6000, self.desk.drained)                               # failed: left for a person
        r = self.desk.rollouts.start("gpu-test", "newer", None, "peter")
        self.desk.rollouts.act("gpu-test", "cancel", "peter")                 # cancelled while installing
        self.assertNotEqual((self.desk.drained.get(A6000) or {}).get("by"), f"rollout {r['id']}")

    def test_without_the_gate_nothing_changes(self):
        self.tearDown_and(gate=False)
        r = self.desk.rollouts.start("gpu-test", "new", None, "peter")
        self.assertEqual((r["evals"], self.desk.drained), ([], {}))
        self.has(A6000, "new")
        self.desk.rollouts.tick()
        self.assertEqual((self.desk.rollouts.recent(1)[0]["step"], self.store.queued()), (1, []))

    def tearDown_and(self, gate):
        self.doCleanups()
        self.setUp(gate)


class Syncs(Manager):
    """A ComfyUI sync: after a machine's ComfyUI is back and checked, the evals of the ComfyUI
    definitions it could run before."""

    def test_checked_then_evaluated_then_the_next(self):
        self.up(W5090, problems={"chord_pbr": ["no node ChordMaterialEstimation"]})   # couldn't before either
        r = self.desk.syncs.start("comfyui", "abc", None, False, "peter")
        self.assertEqual(r["evals"], ["chord_pbr"])
        for m in ORDER:
            self.desk.syncs.tick()                                          # holds no jobs: told
            self.desk.status({"name": m, "online": True, "connected": True,
                              "sync": {"id": r["id"], "state": "done", "steps": [["pip", "ok"]]}})
            self.desk.syncs.tick()                                          # done: checked
            s = self.desk.syncs.recent(1)[0]
            if m == W5090:                                                  # nothing to evaluate there
                self.assertEqual((s["state"], s["eval_jobs"].get(m)), ("done", None))
                break
            self.desk.syncs.tick()
            s = self.desk.syncs.recent(1)[0]
            self.assertEqual(s["phase"], "evaluating")
            [ev] = s["eval_jobs"][m]
            self.assertEqual(self.claimed(m), [ev])                         # drained, it takes its eval
            self.desk.syncs.tick()
            self.assertEqual(self.desk.syncs.recent(1)[0]["phase"], "evaluating")
            self.ends(ev, "done")
            self.desk.syncs.tick()
            self.assertNotIn(m, self.desk.drained)
        self.assertEqual(self.desk.drained, {})

    def test_a_failed_eval_stops_it(self):
        r = self.desk.syncs.start("comfyui", "abc", [A6000, W5090], False, "peter")
        self.desk.syncs.tick()
        self.desk.status({"name": A6000, "online": True, "connected": True,
                          "sync": {"id": r["id"], "state": "done", "steps": [["pip", "ok"]]}})
        self.desk.syncs.tick()
        self.desk.syncs.tick()
        [ev] = self.desk.syncs.recent(1)[0]["eval_jobs"][A6000]
        self.ends(ev, "failed", "The eval failed: files .exr: 0 written, 5 wanted.")
        self.desk.syncs.tick()
        s = self.desk.syncs.recent(1)[0]
        self.assertEqual(s["state"], "failed")
        self.assertIn(f"{A6000}: the eval of chord_pbr failed", s["error"])
        self.assertIn(A6000, self.desk.drained)                               # for a person to look at
        self.assertNotIn(W5090, self.desk.drained)                            # never reached


class OlderDatabase(unittest.TestCase):
    def test_its_rollouts_gain_the_gate(self):
        import sqlite3
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        db = sqlite3.connect(tmp / "farm.db")
        db.execute("CREATE TABLE rollouts (id TEXT PRIMARY KEY, name TEXT NOT NULL, release TEXT NOT NULL, "
                   "machines TEXT NOT NULL, before TEXT NOT NULL, step INTEGER NOT NULL DEFAULT 0, "
                   "state TEXT NOT NULL, "
                   "error TEXT, by TEXT, started_at REAL, step_started REAL, updated_at REAL)")
        db.execute("INSERT INTO rollouts VALUES ('r0', 'demo', 'old', '[\"m\"]', '{}', 0, 'done', NULL, 'ann', "
                   "1, 1, 1)")
        db.commit()
        db.close()
        store = JobStore(tmp / "farm.db")
        self.addCleanup(store.close)
        [r] = store.rollouts()
        self.assertEqual((r["evals"], r["eval_jobs"], r["drained_before"]), ([], {}, {}))
        store.save_rollout({**r, "evals": ["gpu_test"], "eval_jobs": {"m": ["j"]}})
        self.assertEqual(store.rollouts()[0]["eval_jobs"], {"m": ["j"]})


class Config_(unittest.TestCase):
    def test_off_until_switched_on(self):
        self.assertEqual((Config().evals.gate, Config().evals.minutes), (False, 30))
        with self.assertRaises(ValueError):
            Config.model_validate({"workers": [], "evals": {"minutes": 0}})
        self.assertEqual(copy.deepcopy(Config().evals).gate, False)


if __name__ == "__main__":
    unittest.main()
