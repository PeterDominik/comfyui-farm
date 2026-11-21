"""The environment manager (agent/envman.py, decision 0030), the seam between what the
manager wants of a machine's runner environments and what the machine has built:
installing a release from the bank, switching back to the previous one, removing,
waiting for a job running in it, a release that fails, and the manager's side, through the
real desk: what it keeps per machine, its check-in replies, and `env want`.

    python -m unittest tests.test_envman -v
"""

import asyncio
import http.server
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_envs import UV, make  # noqa: E402

from comfyfarm import cli  # noqa: E402
from comfyfarm.agent.agent import Agent  # noqa: E402
from comfyfarm.agent.link import LocalLink  # noqa: E402
from comfyfarm.config import AgentConfig, Config  # noqa: E402
from comfyfarm.definitions import DefinitionRegistry  # noqa: E402
from comfyfarm.envs import build as B  # noqa: E402
from comfyfarm.envs import release as R  # noqa: E402
from comfyfarm.envs import spec as S  # noqa: E402
from comfyfarm.manager.db import JobStore  # noqa: E402
from comfyfarm.manager.desk import Desk, wanted_hash  # noqa: E402
from comfyfarm.manager.events import EventBus  # noqa: E402
from comfyfarm.manager.farm import Farm  # noqa: E402


class Runner:
    """ComfyUI as far as these tests go; `using` are the environments its jobs run in."""
    label, ready, max_runs = "ComfyUI", True, None
    object_info = farm_nodes = environment = None

    def __init__(self):
        self.using: set = set()

    def attach(self, host):
        self.host = host

    def start(self):
        return []

    async def close(self):
        pass

    def status(self):
        return {"online": True, "connected": True}

    def processes(self):
        return {}

    def knows(self, process):
        return False

    def environments_in_use(self):
        return set(self.using)


class Link:
    def __init__(self):
        self.statuses: list[dict] = []

    async def status(self, msg):
        self.statuses.append(msg)
        return {}


class Farmhouse(unittest.TestCase):
    """A repo with environments/demo, the bank with its releases, and one machine."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.envs = self.tmp / "repo" / "environments"
        self.bank = self.tmp / "bank"
        self.bank.mkdir()
        self.machine = self.tmp / "machine"
        self.builds = self.machine / "data" / "envs"
        self.log = self.tmp / "uv.jsonl"
        for patch in (mock.patch.object(S, "ENVS", self.envs),
                      mock.patch.dict(os.environ, {"FAKE_BUILDER_LOG": str(self.log)})):
            patch.start()
            self.addCleanup(patch.stop)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.runner = Runner()

    def release(self, name="demo", deps=("numpy==2.5.2",)):
        """A new version of the environment in the repo, published to the bank."""
        make(self.envs, name, deps=deps, check=["python", "-c", "pass"])
        B.lock(S.load(name), UV, self.tmp / "lock-scratch")
        rel, _ = R.publish(S.load(name), self.bank, commit="abc123")
        return rel.fingerprint

    def agent(self, link=None, managed=True, manager=None):
        cfg = AgentConfig(name="gpu1", base_dir=self.machine, manager=manager or str(self.bank / "manager.json"),
                          envs={"dir": "data/envs", "uv": UV, "managed": managed})
        a = Agent(cfg, link or Link(), runners=[self.runner])
        a.registered = True
        return a

    def wait(self, coro):
        return self.loop.run_until_complete(coro)

    def syncs(self) -> int:
        if not self.log.exists():
            return 0
        return sum(1 for line in self.log.read_text().splitlines() if json.loads(line)["args"][:1] == ["sync"])

    def current(self, name="demo"):
        return B.builds(self.builds, name).current


class Manages(Farmhouse):
    def want(self, a, wanted):
        a.envman.want({"wanted": wanted, "hash": wanted_hash(wanted), "bank": None})
        self.wait(a.envman.apply())

    def test_it_installs_a_release_and_says_so(self):
        fp = self.release()
        link = Link()
        a = self.agent(link)
        self.want(a, {"demo": fp})
        self.assertEqual(self.current(), fp)
        info = B.builds(self.builds, "demo").info[fp]
        self.assertEqual((info["origin"]["release"], info["origin"]["commit"]), (fp, "abc123"))
        self.assertEqual(a.envman.work, {})
        works = [s["env_work"] for s in link.statuses]
        self.assertEqual(works[0]["demo"]["state"], "installing")
        self.assertEqual(works[-1], {})
        self.assertEqual(link.statuses[-1]["environments"]["demo"]["current"], fp)
        self.assertEqual(link.statuses[-1]["env_wanted"], wanted_hash({"demo": fp}))
        self.assertFalse((self.builds / ".releases" / "demo" / fp).exists())
        n = self.syncs()
        self.wait(a.envman.apply())                     # as wanted: nothing to do
        self.assertEqual(self.syncs(), n)

    def test_update_then_back_to_the_previous_without_a_build(self):
        one = self.release()
        two = self.release(deps=("numpy==2.5.2", "pillow==12.3.0"))
        a = self.agent()
        self.want(a, {"demo": one})
        self.want(a, {"demo": two})
        self.assertEqual((self.current(), B.builds(self.builds, "demo").previous), (two, one))
        n = self.syncs()
        self.want(a, {"demo": one})                     # a rollback: the previous build is still here
        self.assertEqual((self.current(), self.syncs()), (one, n))

    def test_it_removes_and_leaves_what_it_isnt_told_about(self):
        fp = self.release()
        other = self.release("tools")
        a = self.agent()
        self.want(a, {"demo": fp, "tools": other})
        self.want(a, {"demo": None})                    # tools isn't managed any more: left as it is
        self.assertFalse((self.builds / "demo").exists())
        self.assertEqual(self.current("tools"), other)

    def test_it_waits_while_a_job_runs_in_it(self):
        fp = self.release()
        a = self.agent()
        self.runner.using = {"demo"}
        self.want(a, {"demo": fp})
        self.assertIsNone(self.current())
        self.assertEqual(a.envman.work["demo"]["state"], "waiting")
        self.assertIn("a job is running in it", a.envman.work["demo"]["error"])
        self.runner.using = set()
        self.wait(a.envman.apply())
        self.assertEqual((self.current(), a.envman.work), (fp, {}))

    def test_a_release_that_fails_isnt_tried_again_until_told_otherwise(self):
        fp = self.release()
        (self.bank / "environments" / "demo" / fp / "uv.lock").write_text("changed\n", encoding="utf-8")
        a = self.agent()
        self.want(a, {"demo": fp})
        work = a.envman.work["demo"]
        self.assertEqual(work["state"], "failed")
        self.assertIn("uv.lock doesn't match its hash", work["error"])
        self.wait(a.envman.apply())
        self.assertEqual(a.envman.work["demo"], work)  # not tried again: still the same failure
        good = self.release(deps=("numpy==2.5.2", "pillow==12.3.0"))
        self.want(a, {"demo": good})                    # the manager asks for something else
        self.assertEqual((self.current(), a.envman.work), (good, {}))

    def test_off_it_says_so_and_installs_nothing(self):
        fp = self.release()
        a = self.agent(managed=False)
        self.want(a, {"demo": fp})
        self.assertIsNone(self.current())
        self.assertEqual(a.envman.work["demo"]["state"], "off")
        self.assertIn("envs.managed is off", a.envman.work["demo"]["error"])

    def test_the_bank_from_the_manager_when_the_agent_has_a_url(self):
        fp = self.release()
        a = self.agent(manager="http://wrk1125l:9090")
        a.envman.want({"wanted": {"demo": fp}, "hash": "x", "bank": str(self.bank)})
        self.wait(a.envman.apply())
        self.assertEqual(self.current(), fp)
        B.remove(self.builds, "demo")                   # another machine, with nothing built
        b = self.agent(manager="http://wrk1125l:9090")
        b.envman.want({"wanted": {"demo": fp}, "hash": "x", "bank": None})
        self.wait(b.envman.apply())
        self.assertIn("can't find the bank", b.envman.work["demo"]["error"])


class ThroughTheManager(Farmhouse):
    """The manager keeps what each machine should have, and says it in a check-in reply."""

    def setUp(self):
        super().setUp()
        (self.tmp / "workflows").mkdir()
        self.cfg = Config.model_validate({"database": str(self.tmp / "farm.db"), "workers": [],
                                          "workflows_dir": str(self.tmp / "workflows"),
                                          "share": {"dir": str(self.bank)}})
        self.store = JobStore(self.tmp / "farm.db")
        self.addCleanup(self.store.close)
        self.desk = Desk(Farm(self.cfg, self.store, DefinitionRegistry(self.tmp / "workflows"), EventBus()))

    def beat(self, a):
        async def go():
            a.cfg.heartbeat_wait = 0
            await a._obey(await a._heartbeat())
            await a.envman.apply()
        self.wait(go())

    def test_wanted_reaches_the_machine_and_comes_back_done(self):
        fp = self.release()
        a = self.agent(LocalLink(self.desk))
        self.wait(a._hello())
        self.assertEqual(self.desk.want_environment("demo", fp, None, "peter"), {"gpu1": fp})
        self.beat(a)
        self.assertEqual(self.current(), fp)
        st = self.desk.agents["gpu1"]
        self.assertEqual((st.env_work, st.envs["demo"]["current"]), ({}, fp))
        reply = self.wait(self.desk.heartbeat({"name": "gpu1", "seq": 99, "held": [], "wait": 0}))
        self.assertNotIn("environments", reply)         # said once: the long poll stays a long poll
        # Removed everywhere, then no longer managed.
        self.desk.want_environment("demo", None, ["gpu1"], "peter")
        self.beat(a)
        self.assertFalse((self.builds / "demo").exists())
        self.desk.want_environment("demo", None, None, "peter", forget=True)
        self.assertEqual(self.store.env_wanted(), {})

    def test_a_lost_reply_is_sent_again(self):
        fp = self.release()
        a = self.agent(LocalLink(self.desk))
        self.wait(a._hello())
        self.desk.want_environment("demo", fp, None, "peter")
        self.wait(self.desk.heartbeat({"name": "gpu1", "seq": 50, "held": [], "wait": 0}))     # the reply is lost
        self.wait(a.send_status())                      # it says it holds nothing yet
        self.beat(a)
        self.assertEqual(self.current(), fp)

    def test_what_is_wanted_outlives_the_manager(self):
        fp = self.release()
        a = self.agent(LocalLink(self.desk))
        self.wait(a._hello())
        self.desk.want_environment("demo", fp, None, "peter")
        again = Desk(Farm(self.cfg, self.store, DefinitionRegistry(self.tmp / "workflows"), EventBus()))
        a.link = LocalLink(again)
        self.wait(a._hello())                           # the manager restarted: the agent checks in again
        self.beat(a)
        self.assertEqual(self.current(), fp)
        self.assertEqual(again.agents["gpu1"].env_wanted, {"demo": fp})

    def test_only_machines_it_knows(self):
        with self.assertRaises(ValueError) as e:
            self.desk.want_environment("demo", "x", None, "peter")
        self.assertIn("no machine has checked in yet", str(e.exception))
        a = self.agent(LocalLink(self.desk))
        self.wait(a._hello())
        with self.assertRaises(ValueError) as e:
            self.desk.want_environment("demo", "x", ["gpu9"], "peter")
        self.assertIn("no machine gpu9 (known: gpu1)", str(e.exception))


class FromTheManagersHost(unittest.TestCase):
    def test_only_from_this_host(self):
        try:
            from comfyfarm.manager.app import from_this_host
        except ImportError:
            self.skipTest("needs the web stack")
        for host, ok in (("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True), ("localhost", True),
                         ("10.0.4.17", False), ("testclient", False), (None, False)):
            self.assertEqual(from_this_host(host), ok, host)

    def test_env_want_asks_the_manager_on_this_host(self):
        seen = {}

        class Manager(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.update(path=self.path, user=self.headers.get("X-User"),
                            body=json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                ok = seen["body"]["release"] != "bad"
                out = json.dumps({"name": "gpu-test", "release": "30cee042f35be171", "forget": False,
                                  "machines": {"gpu1": "30cee042f35be171", "gpu2": "30cee042f35be171"}}
                                 if ok else {"detail": "the bank has no release gpu-test bad"}).encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        server = http.server.HTTPServer(("127.0.0.1", 0), Manager)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        config = tmp / "config.yaml"
        config.write_text(f"server:\n  port: {server.server_port}\n", encoding="utf-8")

        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["env", "want", *argv, "--manager-config", str(config)])
            return code, out.getvalue()
        code, said = run("gpu-test", "latest", "--machine", "gpu1", "--machine", "gpu2")
        self.assertEqual(code, 0, said)
        self.assertEqual(seen["path"], "/api/v1/environments/gpu-test/want")
        self.assertEqual(seen["body"], {"release": "latest", "machines": ["gpu1", "gpu2"], "forget": False})
        self.assertTrue(seen["user"])
        self.assertIn("gpu1: gpu-test 30cee042f35be171", said)
        code, said = run("gpu-test", "bad")
        self.assertEqual(code, 1)
        self.assertIn("STOPPED: the manager said no: the bank has no release gpu-test bad", said)
        code, said = run("gpu-test")
        self.assertEqual(code, 1)
        self.assertIn("say which release", said)


if __name__ == "__main__":
    unittest.main()
