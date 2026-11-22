"""The Machines tab's runner environments (static/environments.js, decision 0032): what each
cell and each rollout step says, from data shaped as the pilot's machines sent it.

envCell() and rolloutSteps() touch no page, so node runs them alone. Skipped where node
isn't installed; GitHub's runners have it."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "comfyfarm" / "manager" / "static" / "environments.js"
NODE = shutil.which("node")

# Loads the script into a context of its own (no page: setInterval does nothing) and calls
# the named function with each case's arguments.
HARNESS = """
const fs = require("fs"), vm = require("vm");
const ctx = { setInterval: () => 0, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(JSON.stringify(cases.map(([fn, args]) => ctx[fn](...args))));
"""

GPU_TEST = "c817ab555ebac60a"
OLDER = "30cee042f35be171"
CANARY = "0c8de83c166dd7b2"


def built(fp: str, previous: str | None = None, **extra) -> dict:
    return {"current": fp, "previous": previous, "builds": {fp: {
        "built_at": "2026-11-21 20:01:36", "build_seconds": 0.3, "copied": False, "ok": True,
        "origin": {"release": fp, "published_at": "2026-11-21T19:40:00-0500", "commit": "2b9386ae83af1cfe93"},
        **extra}}}


def machine(envs: dict | None = None, wanted: dict | None = None, work: dict | None = None, online=True) -> dict:
    return {"name": "tor-flame02-a6000", "online": online, "environments": envs, "env_wanted": wanted or {},
            "env_work": work}


@unittest.skipUnless(NODE, "node isn't installed")
class Dashboard(unittest.TestCase):
    def run_js(self, *cases):
        with tempfile.TemporaryDirectory() as tmp:
            harness, data = Path(tmp) / "harness.js", Path(tmp) / "cases.json"
            harness.write_text(HARNESS, encoding="utf-8")
            data.write_text(json.dumps(list(cases)), encoding="utf-8")
            out = subprocess.run([NODE, str(harness), str(SCRIPT), str(data)], capture_output=True, text=True,
                                 encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def cell(self, w: dict, name: str = "gpu-test", latest: str | None = GPU_TEST) -> dict:
        return self.run_js(["envCell", [w, name, latest]])[0]

    # ------------------------------------------------------------ a machine's cell
    def test_installed_as_wanted(self):
        c = self.cell(machine({"gpu-test": built(GPU_TEST, OLDER)}, {"gpu-test": GPU_TEST}))
        self.assertEqual((c["text"], c["cls"], c["note"], c["error"]), ("c817ab55 ✓", "ok", "", ""))
        self.assertIn("from the bank's release c817ab55, published 2026-11-21T19:40:00-0500, commit 2b9386ae83",
                      c["tip"])
        self.assertIn(f"previous {OLDER}", c["tip"])
        self.assertIn(f"the manager wants {GPU_TEST}", c["tip"])

    def test_an_older_release_it_is_meant_to_have_stays_green(self):
        """A rollback, or a machine held back by hand: what matters is what the manager wants."""
        c = self.cell(machine({"gpu-test": built(OLDER)}, {"gpu-test": OLDER}))
        self.assertEqual((c["text"], c["cls"], c["note"]), ("30cee042 ✓", "ok", "older"))
        self.assertIn(f"the bank's latest is {GPU_TEST}", c["tip"])

    def test_told_and_not_begun(self):
        c = self.cell(machine({"gpu-test": built(OLDER)}, {"gpu-test": GPU_TEST}))
        self.assertEqual((c["text"], c["cls"], c["note"]), ("→ c817ab55", "warn", "has 30cee042"))
        c = self.cell(machine({"gpu-test": built(GPU_TEST)}, {"gpu-test": None}))
        self.assertEqual((c["text"], c["cls"]), ("→ remove", "warn"))

    def test_removed_as_wanted(self):
        c = self.cell(machine({}, {"gpu-test": None}))
        self.assertEqual((c["text"], c["cls"]), ("—", "muted"))
        self.assertIn("the manager wants it removed", c["tip"])

    def test_not_managed_by_the_manager(self):
        """Built by hand with `env install`: shown as built, and the tip says why it stays."""
        c = self.cell(machine({"gpu-test": built(GPU_TEST, origin=None)}))
        self.assertEqual((c["text"], c["cls"]), ("c817ab55 ✓", "ok"))
        self.assertIn("the manager doesn't manage it here", c["tip"])
        checkout = {"checkout": "/mntx/raid/pdominik/comfyui-farm/environments/gpu-test"}
        envs = {"gpu-test": built(GPU_TEST, origin=checkout)}
        self.assertIn("built from the checkout", self.cell(machine(envs))["tip"])

    def test_what_its_environment_manager_is_doing(self):
        wanted = {"canary-test": CANARY}
        failed = {"want": CANARY, "state": "failed", "at": 1795308130.0,
                  "error": "python -c raise SystemExit(...) failed:\ncanary-test is broken on purpose"}
        cases = [
            ({"want": CANARY, "state": "installing", "since": 1795308120.0}, "installing 0c8de83c", "warn", ""),
            ({"want": CANARY, "state": "waiting", "since": 1795308120.0, "error": "a job is running in it"},
             "0c8de83c: waits for its job", "warn", ""),
            ({"want": None, "state": "removing", "since": 1795308120.0}, "removing", "warn", ""),
            (failed, "0c8de83c failed", "err", failed["error"]),
            ({"want": CANARY, "state": "off", "error": "envs.managed is off in this machine's agent.yaml"},
             "not managed", "warn", "envs.managed is off in this machine's agent.yaml"),
        ]
        for work, text, cls, error in cases:
            with self.subTest(work["state"]):
                c = self.cell(machine({}, wanted, {"canary-test": work}), "canary-test", CANARY)
                self.assertEqual((c["text"], c["cls"], c["error"]), (text, cls, error))
                if work.get("error"):
                    self.assertIn(work["error"], c["tip"] + c["error"])      # in its tooltip, or listed below

    def test_a_machine_that_isnt_checking_in(self):
        c = self.cell(machine(None, {"gpu-test": GPU_TEST}, online=False))
        self.assertEqual((c["text"], c["cls"], c["note"]), ("offline", "muted", "wants c817ab55"))
        self.assertEqual(self.cell(machine(None))["text"], "not reported")      # an agent from before reports

    # ------------------------------------------------------------ a rollout's steps
    def steps(self, state: str, step: int, before: dict | None = None) -> list[tuple[str, str]]:
        r = {"machines": ["tor-flame02-a6000", "wrk59467w-5090", "wrk1125l-3090"], "state": state, "step": step,
             "before": before or {}}
        return [(s["mark"], s["cls"]) for s in self.run_js(["rolloutSteps", [r]])[0]]

    def test_rollout_steps(self):
        self.assertEqual(self.steps("running", 1), [("✓", "ok"), ("…", "warn"), ("○", "muted")])
        self.assertEqual(self.steps("paused", 1), [("✓", "ok"), ("‖", "warn"), ("○", "muted")])
        self.assertEqual(self.steps("failed", 0), [("✗", "err"), ("○", "muted"), ("○", "muted")])
        self.assertEqual(self.steps("done", 2), [("✓", "ok")] * 3)
        self.assertEqual(self.steps("cancelled", 1), [("✓", "ok"), ("■", "muted"), ("–", "muted")])
        self.assertEqual(self.steps("rolled_back", 0), [("↩", "muted"), ("–", "muted"), ("–", "muted")])

    def test_a_rollback_says_what_each_machine_goes_back_to(self):
        r = {"machines": ["tor-flame02-a6000", "wrk59467w-5090"], "state": "rolled_back", "step": 1,
             "before": {"tor-flame02-a6000": OLDER, "wrk59467w-5090": None}}
        tips = [s["tip"] for s in self.run_js(["rolloutSteps", [r]])[0]]
        self.assertEqual(tips, ["told to go back to 30cee042", "told to remove it (it had none before)"])


if __name__ == "__main__":
    unittest.main()
