"""The gateway's API runner and its fal.ai adapter (decisions 0055, 0056), against the fal.ai
stand-in (tools/mock_fal.py) in memory: the adapter's calls and refusals, the guard that
keeps everything on this machine until phase 11's step 5, the gateway agent with the
manager in this process (a job end to end, failures, cancels, restarts, a job never sent
twice), and the keys, which reach the stand-in and appear nowhere else.

    python -m unittest tests.test_api_runner -v
"""

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from comfyfarm.config import AgentConfig, Config, load_agent_config
from comfyfarm.definitions import Definition, DefinitionRegistry
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.desk import Desk
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm

try:   # the gateway talks to providers with httpx
    import httpx
    from comfyfarm.agent.agent import Agent
    from comfyfarm.agent.link import LocalLink
    from comfyfarm.runners.api import ApiError
    from comfyfarm.runners.api import fal as F
    from comfyfarm.runners.api.runner import ApiRunner, key_path, read_key
except ImportError:   # pragma: no cover
    Agent = None

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("mock_fal", REPO / "tools" / "mock_fal.py")
mock_fal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock_fal)

KEY = "0f1e2d3c-mock-4b5a:9e8d7c6b5a4f3e2d"     # what the stand-in takes; it must appear nowhere else
BASE = "http://127.0.0.1:8189"
EDIT = "fal-ai/nano-banana-2/edit"
NANO = REPO / "workflows" / "nano_banana2_edit_fal"
PLATE = REPO / "workflows" / "gpu_test" / "eval" / "input.png"
GATEWAY = "wrk1125l-gateway"


def png_size(path) -> tuple[int, int]:
    return struct.unpack(">II", Path(path).read_bytes()[16:24])


class Flaky(httpx.AsyncBaseTransport if Agent else object):
    """The stand-in's transport, with a failure for the next call matching (method, path end):
    before it reaches the stand-in (it was never sent) or after (its answer is lost)."""

    def __init__(self, inner):
        self.inner = inner
        self.fail: list[tuple[str, str, type, bool]] = []

    async def handle_async_request(self, request):
        for f in list(self.fail):
            method, end, exc, after = f
            if request.method == method and request.url.path.endswith(end):
                self.fail.remove(f)
                if after:
                    await self.inner.handle_async_request(request)
                raise exc("test", request=request)
        return await self.inner.handle_async_request(request)


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def key_file(self, key=KEY, mode=0o600) -> Path:
        path = self.tmp / "keys" / "fal_requests.key"
        path.parent.mkdir(exist_ok=True)
        path.write_text(key + "\n", encoding="utf-8")
        os.chmod(path, mode)
        return path


@unittest.skipIf(Agent is None, "the gateway needs httpx")
class FalAdapter(Tmp):
    def adapter(self, keys=None, url=BASE, transport=None, **kw):
        self.mock = getattr(self, "mock", None) or mock_fal.MockFal(KEY, BASE)
        return F.Adapter({"requests": KEY} if keys is None else keys, url=url,
                         transport=transport or self.mock.transport(), **kw)

    def image(self) -> str:
        path = self.tmp / "in.png"
        path.write_bytes(mock_fal.grey_png(8, 8))
        return str(path)

    def test_a_job_there_and_back(self):
        async def go():
            a = self.adapter()
            url = await a.upload(self.image(), "image/png")
            req = await a.submit(EDIT, {"prompt": "rain", "image_urls": [url], "resolution": "0.5K"})
            st = await a.status(req)
            made = await a.result(req)
            dest = str(self.tmp / "out.png")
            await a.download(a.outputs(made)[0], dest)
            await a.close()
            return req, st, dest
        req, st, dest = asyncio.run(go())
        self.assertEqual(set(req), {"id", "endpoint"})
        self.assertEqual(st["state"], "done")
        self.assertEqual(png_size(dest), (512, 512))
        calls = [(c["method"], c["path"].replace(req["id"], "<id>"), c["keyed"]) for c in self.mock.calls]
        self.assertEqual(calls, [
            ("POST", "/storage/auth/token", True),          # the key buys an upload token,
            ("POST", "/files/upload", False),               # which uploads without it
            ("POST", "/fal-ai/nano-banana-2/edit", True),
            ("GET", "/fal-ai/nano-banana-2/requests/<id>/status", True),     # the owner and app only
            ("GET", "/fal-ai/nano-banana-2/requests/<id>", True),
            ("GET", calls[5][1], False)])                   # its file: public, no key
        kept = [c["lifecycle"] for c in self.mock.calls if c["path"] in ("/files/upload", "/fal-ai/nano-banana-2/edit")]
        self.assertEqual(kept, ['{"expiration_duration_seconds": 3600}'] * 2)

    def test_nothing_reaches_fal_itself_before_step_5(self):
        """REAL_SERVICE is switched on by phase 11's step 5, in a reviewed change that changes this test."""
        self.assertFalse(F.REAL_SERVICE)

        async def go(a):
            try:
                return await a.submit(EDIT, {"prompt": "x"})
            finally:
                await a.close()
        for url in ("", "http://10.1.2.3:8189", "https://queue.fal.run"):
            with self.subTest(url=url):
                a = self.adapter(url=url)
                self.assertEqual(a.problems(), [F.OFF])
                with self.assertRaisesRegex(ApiError, "step 5"):
                    asyncio.run(go(a))
        self.assertEqual(self.mock.calls, [])
        stand_in = self.adapter()
        self.assertEqual(stand_in.problems(), [])
        with self.assertRaisesRegex(ApiError, "won't download from v3.fal.media"):
            asyncio.run(stand_in.download("https://v3.fal.media/files/x.png", str(self.tmp / "x.png")))

    def test_what_fal_says_when_it_refuses(self):
        async def go(a, call):
            try:
                return await call(a)
            finally:
                await a.close()
        with self.assertRaises(ApiError) as e:
            asyncio.run(go(self.adapter({"requests": "not-the-key"}), lambda a: a.upload(self.image(), "image/png")))
        self.assertEqual((e.exception.kind, str(e.exception)),
                         ("refused", "fal.ai refused the gateway's key while asking for an upload token (401): "
                                     "check keys.requests in gateway.yaml on the gateway"))
        with self.assertRaises(ApiError) as e:
            asyncio.run(go(self.adapter(), lambda a: a.submit(EDIT, {"image_urls": [], "colour": "red"})))
        self.assertEqual(str(e.exception), "fal.ai refused it: prompt: Field required; colour: Not a field of this "
                                           "model (the stand-in is strict)")

        async def ran(a, prompt):
            req = await a.submit(EDIT, {"prompt": prompt, "image_urls": [await a.upload(self.image(), "image/png")]})
            st = await a.status(req)
            return st, (await a.result(req) if st["state"] == "done" else None)
        st, _ = asyncio.run(go(self.adapter(), lambda a: ran(a, "MOCK_FAIL please")))
        self.assertEqual(st, {"state": "failed", "error": "MOCK_FAIL: the stand-in's model failed",
                              "error_type": "runner_server_error"})
        with self.assertRaises(ApiError) as e:
            asyncio.run(go(self.adapter(), lambda a: ran(a, "MOCK_POLICY please")))
        self.assertEqual(str(e.exception),
                         "fal.ai refused it: prompt: The content was flagged (fal.ai's content check)")

    def test_a_job_is_sent_again_only_when_it_never_left(self):
        mock = self.mock = mock_fal.MockFal(KEY, BASE)
        flaky = Flaky(mock.transport())
        a = self.adapter(transport=flaky)

        async def go():
            body = {"prompt": "x", "image_urls": [await a.upload(self.image(), "image/png")]}
            flaky.fail.append(("POST", "/edit", httpx.ConnectError, False))      # no connection: never sent
            first = await a.submit(EDIT, body)
            flaky.fail.append(("POST", "/edit", httpx.ReadTimeout, True))        # sent, its answer lost
            try:
                await a.submit(EDIT, body)
            except ApiError as e:
                return first, e
            finally:
                await a.close()
        first, e = asyncio.run(go())
        self.assertEqual((e.kind, str(e)), ("unknown", "fal.ai at 127.0.0.1:8189 didn't answer in time"))
        sent = [c for c in mock.calls if c["path"] == "/fal-ai/nano-banana-2/edit"]
        self.assertEqual(len(sent), 2)                      # the first once, the lost one once: never again
        self.assertIn(first["id"], mock.requests)

    def test_sync_mode_files_come_inline(self):
        async def go():
            a = self.adapter()
            req = await a.submit(EDIT, {"prompt": "x", "sync_mode": True,
                                        "image_urls": [await a.upload(self.image(), "image/png")]})
            made = await a.result(req)
            await a.download(a.outputs(made)[0], str(self.tmp / "inline.png"))
            await a.close()
            return made
        made = asyncio.run(go())
        self.assertTrue(made["images"][0]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(png_size(self.tmp / "inline.png"), (1024, 1024))


@unittest.skipIf(Agent is None, "the gateway needs httpx")
class WhatIsSent(Tmp):
    def definition(self, change=None, name="nano") -> Definition:
        p = yaml.safe_load((NANO / "process.yaml").read_text(encoding="utf-8"))
        if change:
            change(p)
        folder = self.tmp / "workflows" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "process.yaml").write_text(yaml.safe_dump(p, sort_keys=False), encoding="utf-8")
        return Definition(folder)

    def test_the_body(self):
        wf = self.definition()
        self.assertEqual(F.check(wf), [])
        body = F.Adapter.body(wf.api, {"prompt": "rain", "resolution": "2K", "aspect_ratio": ""},
                              {"input": ["u1"], "reference": ["u2"]})
        self.assertEqual(body, {"num_images": 1, "output_format": "png", "enable_web_search": False,
                                "enable_image_search": False, "prompt": "rain", "resolution": "2K",
                                "image_urls": ["u1", "u2"]})                 # the input first, an empty param left out
        one = F.Adapter.body({"upload": {"input": "image_url"}, "send": {}, "fixed": {}}, {}, {"input": ["u1"]})
        self.assertEqual(one, {"image_url": "u1"})
        none = F.Adapter.body(wf.api, {"prompt": "x"}, {"input": ["u1"], "reference": []})
        self.assertEqual(none["image_urls"], ["u1"])

    def test_what_is_wrong_for_fal(self):
        def two_to_one(p):
            p["runner"]["upload"] = {"input": "image_url", "reference": "image_url"}

        def jpeg(p):
            p["writes"]["frames"]["ext"] = "jpeg"

        def webp_both(p):
            p["writes"]["frames"]["ext"] = "webp"
            p["runner"]["fixed"]["output_format"] = "webp"
        cases = {
            "runner.upload: image_url takes one URL, and input and reference can give more (fal.ai names a field "
            "that takes several ..._urls)": two_to_one,
            "runner.fixed.output_format is png and writes.frames.ext is jpeg: make them the same": jpeg,
        }
        for message, change in cases.items():
            with self.subTest(message=message[:40]):
                self.assertEqual(F.check(self.definition(change, message[:5].replace(".", ""))), [message])
        self.assertEqual(F.check(self.definition(webp_both, "webp")), [])


class Keys(Tmp):
    def test_a_key_file(self):
        self.assertEqual(read_key(self.key_file()), (KEY, None))
        self.assertEqual(read_key(self.tmp / "nope.key"), (None, f"the key file {self.tmp / 'nope.key'} doesn't exist"))
        two = self.key_file("one two")
        self.assertEqual(read_key(two), (None, f"the key file {two} should hold the key alone, on one line"))

    def test_as_editors_save_it(self):
        """A byte-order mark some editors put first is dropped (a pilot key file had one, and
        its job failed on an HTTP header); any other character a key can't have is refused,
        saying so."""
        path = self.key_file()
        path.write_bytes(b"\xef\xbb\xbf" + KEY.encode() + b"\r\n")
        self.assertEqual(read_key(path), (KEY, None))
        self.assertEqual(mock_fal.read_key_file(str(path)), KEY)        # the stand-in reads it the same way
        path.write_text(KEY + "\u00e9", encoding="utf-8")
        self.assertEqual(read_key(path), (None, f"the key file {path} holds characters a key doesn't have (an "
                                                f"editor may have added them): save it again as plain text, the key "
                                                f"alone"))
        path.write_text(KEY, encoding="utf-16")
        self.assertEqual(read_key(path), (None, f"the key file {path} isn't plain text: save it again as plain text "
                                                f"(UTF-8), the key alone"))

    def test_home_in_a_key_path(self):
        cfg = AgentConfig(name="gw", base_dir=str(self.tmp))
        home = str(self.tmp / "home")
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            self.assertEqual(key_path(cfg, "~/.comfyfarm/fal.key"), self.tmp / "home" / ".comfyfarm" / "fal.key")
        self.assertEqual(key_path(cfg, "keys/fal.key"), self.tmp / "keys" / "fal.key")     # next to gateway.yaml

    def test_the_stand_in_answers_where_gateway_yaml_sends_fal(self):
        gw = self.tmp / "gateway.yaml"

        def write(url):
            gw.write_text(yaml.safe_dump({"name": "gw", "gateway": {"enabled": True, "providers": {
                "fal": {"url": url, "keys": {"requests": "keys/fal_requests.key"}}}}}), encoding="utf-8")
        write("http://127.0.0.1:8199")
        port, key = mock_fal.from_gateway(str(gw))
        # Next to gateway.yaml, as resolved: on Windows its long name, where the temp folder may be RUNNER~1.
        self.assertEqual((port, Path(key)), (8199, self.tmp.resolve() / "keys" / "fal_requests.key"))
        for url in ("", "https://queue.fal.run", "http://10.1.2.3:8199"):
            write(url)
            with self.assertRaisesRegex(SystemExit, "doesn't send fal.ai's jobs to a stand-in on this machine"):
                mock_fal.from_gateway(str(gw))

    @unittest.skipIf(os.name == "nt", "file modes are POSIX's")
    def test_one_others_can_read_is_refused(self):
        path = self.key_file(mode=0o644)
        self.assertEqual(read_key(path), (None, f"the key file {path} can be read by other users on this machine: "
                                                f"chmod 600 {path}"))

    def test_the_gateway_has_its_own_scratch_folder(self):
        """It runs beside the GPU agent, which would empty a scratch folder they shared."""
        self.assertEqual(AgentConfig(name="gw", gateway={"enabled": True}).convert.scratch_dir, "data/scratch-gateway")
        self.assertEqual(AgentConfig(name="gpu").convert.scratch_dir, "data/scratch")
        mine = AgentConfig(name="gw", gateway={"enabled": True}, convert={"scratch_dir": "/fast/gw"})
        self.assertEqual(mine.convert.scratch_dir, "/fast/gw")

    @unittest.skipIf(Agent is None, "the gateway needs httpx")
    def test_the_example_config(self):
        cfg = load_agent_config(REPO / "gateway.example.yaml")
        self.assertTrue(cfg.gateway.enabled)
        self.assertEqual((cfg.name, cfg.gateway.account, cfg.convert.scratch_dir), (GATEWAY, "peter-pilot",
                                                                                   "data/scratch-gateway"))
        r = ApiRunner(cfg)
        self.assertEqual((r.problems, r.lanes, r.max_runs), ([], {"fal": 2}, 2))
        named = key_path(cfg, "~/.comfyfarm/fal_requests.key")      # this account's home, as this OS writes it
        self.assertEqual(r.provider_problems["fal"], [f"the key file {named} doesn't exist"])
        self.assertEqual(r.processes()["nano_banana2_edit_fal"]["broken"], [])

    @unittest.skipIf(Agent is None, "the gateway needs httpx")
    def test_what_gateway_yaml_can_get_wrong(self):
        keys = {"requests": str(self.key_file())}
        r = ApiRunner(AgentConfig(name="gw", gateway={"enabled": True, "providers": {
            "fal": {"url": BASE, "keys": {**keys, "billingg": "x"}}, "comfyui-local": {}, "nope": {}}}))
        self.assertEqual(r.problems, [
            "providers.comfyui-local in gateway.yaml: providers/ has no outside provider comfyui-local (it has fal)",
            "providers.nope in gateway.yaml: providers/ has no outside provider nope (it has fal)"])
        self.assertEqual(r.provider_problems["fal"], ["keys.billingg for fal in gateway.yaml: fal.ai has keys "
                                                      "requests, billing (providers/fal.yaml)"])
        off = ApiRunner(AgentConfig(name="gw", gateway={"enabled": True, "providers": {"fal": {"keys": keys}}}))
        self.assertEqual(off.provider_problems["fal"], [F.OFF])
        self.assertEqual(off.status()["online"], False)
        self.assertEqual(off.processes()["nano_banana2_edit_fal"]["problems"], [F.OFF])
        bare = ApiRunner(AgentConfig(name="gw", gateway={"enabled": True, "providers": {"fal": {"url": BASE}}}))
        self.assertEqual(bare.provider_problems["fal"], ["there's no file for fal.ai's requests key "
                                                         "(providers.fal.keys.requests in gateway.yaml)"])


class RecordingLink(LocalLink if Agent else object):
    """The desk in this process; every message the gateway sends it, kept."""

    def __init__(self, desk, said):
        super().__init__(desk)
        self.said = said

    async def hello(self, msg):
        self.said.append(json.dumps(msg, default=str))
        return await super().hello(msg)

    async def status(self, msg):
        self.said.append(json.dumps(msg, default=str))
        return await super().status(msg)

    async def nodes(self, msg):
        self.said.append(json.dumps(msg, default=str))
        return await super().nodes(msg)

    async def heartbeat(self, msg):
        self.said.append(json.dumps(msg, default=str))
        return await super().heartbeat(msg)

    async def report(self, msg):
        self.said.append(json.dumps(msg, default=str))
        return await super().report(msg)


class Logged(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


@unittest.skipIf(Agent is None, "the gateway needs httpx")
class TheGateway(Tmp):
    """The manager and the gateway agent in this process, against the stand-in."""

    def setUp(self):
        super().setUp()
        self.root = root = str(self.tmp).replace("\\", "/")
        shutil.copytree(NANO, self.tmp / "workflows" / NANO.name)
        (self.tmp / "shows" / "out").mkdir(parents=True)
        shutil.copy(PLATE, self.tmp / "shows" / "plate.png")
        self.cfg = Config.model_validate({
            "database": f"{root}/farm.db", "workers": [], "workflows_dir": f"{root}/workflows",
            "convert": {"scratch_dir": f"{root}/scratch"}, "preview": {"enabled": False},
            "provenance": {"enabled": True},
            "paths": {"mappings": [{"posix": f"{root}/shows", "windows": f"{root}/shows"}]},
            "providers": {"enabled": ["fal"]}})
        self.keys = self.key_file()
        self.mock = mock_fal.MockFal(KEY, BASE)
        self.transport = Flaky(self.mock.transport())
        self.said: list[str] = []
        self.log = Logged()
        root_log = logging.getLogger()
        self.addCleanup(setattr, root_log, "level", root_log.level)
        root_log.addHandler(self.log)
        root_log.setLevel(logging.DEBUG)
        self.addCleanup(root_log.removeHandler, self.log)
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.store = JobStore(self.tmp / "farm.db")
        self.farm = Farm(self.cfg, self.store, DefinitionRegistry(self.tmp / "workflows"), EventBus())
        self.desk = Desk(self.farm)
        self.wait(self.farm.start())
        self.gateways: list = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for g in self.gateways:
            self.wait(g.stop())
        self.wait(self.farm.stop())
        self.store.close()

    def wait(self, coro):
        return self.loop.run_until_complete(coro)

    def gateway(self, **fal):
        cfg = AgentConfig(name=GATEWAY, base_dir=self.root, heartbeat_wait=0, retry_seconds=0.05,
                          workflows_dir=f"{self.root}/workflows",
                          paths={"mappings": [{"posix": f"{self.root}/shows", "windows": f"{self.root}/shows"}]},
                          convert={"scratch_dir": f"{self.root}/scratch-gateway"},
                          gateway={"enabled": True, "account": "test-pilot", "runs_dir": f"{self.root}/api_runs",
                                   "poll_seconds": 0.01,
                                   "providers": {"fal": {"url": BASE, "keys": {"requests": str(self.keys)}, **fal}}})
        g = Agent(cfg, RecordingLink(self.desk, self.said), runners=[ApiRunner(cfg, transport=self.transport)])
        self.gateways.append(g)
        self.wait(g._hello())
        return g

    def beat(self, g):
        async def go():
            await g._obey(await g._heartbeat())
        self.wait(go())

    def until(self, pred, what="condition", timeout=10.0):
        async def go():
            end = time.time() + timeout
            while time.time() < end:
                if pred():
                    return
                await asyncio.sleep(0.01)
            self.fail(f"timed out waiting for {what}")
        self.wait(go())

    def submit(self, prompt="remove the crane", **params) -> str:
        values = {"input": f"{self.root}/shows/plate.png", "prompt": prompt,
                  "output_path": f"{self.root}/shows/out", **params}
        return self.farm.submit("nano_banana2_edit_fal", values, "ann")["id"]

    def job(self, jid) -> dict:
        return self.store.get(jid)

    def ended(self, jid, status="done") -> dict:
        self.until(lambda: self.job(jid)["status"] in ("done", "failed", "cancelled", "lost"), f"job {jid} to end")
        j = self.job(jid)
        self.assertEqual(j["status"], status, j.get("error"))
        return j

    def sent(self) -> list:
        return [c for c in self.mock.calls if c["path"] == "/fal-ai/nano-banana-2/edit"]

    # ---------------------------------------------------------------- tests
    def test_it_checks_in_with_its_lanes(self):
        self.gateway()
        a = self.desk.agents[GATEWAY]
        self.assertEqual((a.lanes, a.capacity, a.online, a.checked, a.problems), ({"fal": 2}, 2, True, True, {}))
        self.assertTrue(self.desk.accepting(a))
        hello = json.loads(self.said[0])
        self.assertEqual((hello["lanes"], hello["comfyui"], hello["max_inflight"]), ({"fal": 2}, None, 2))

    def test_a_job_end_to_end(self):
        g = self.gateway()
        jid = self.submit()
        self.beat(g)
        j = self.ended(jid)
        out = j["outputs"]
        self.assertEqual((out["frames"], out["first"]), (1, 1))
        frame = Path(out["frame_pattern"].replace("%04d", "0001"))
        self.assertTrue(frame.is_file(), sorted(os.listdir(frame.parent)))
        self.assertEqual(png_size(frame), (1024, 1024))                     # the stand-in's 1K grey square
        self.assertEqual([n for n in os.listdir(frame.parent) if n.startswith("_comfyfarm")], [])
        rid = next(iter(self.mock.requests))
        body = self.mock.requests[rid]["input"]
        self.assertEqual(body["prompt"], "remove the crane")
        self.assertEqual((body["num_images"], body["output_format"], body["resolution"]), (1, "png", "1K"))
        self.assertEqual(len(body["image_urls"]), 1)
        self.assertEqual(j["gpu"], "gateway")
        events = [e["event"] for e in self.store.events(jid)]
        self.assertEqual(events[-2:], ["postprocessing", "done"])
        self.assertIn("dispatched", events)
        provenance = json.loads(Path(out["provenance"]).read_text(encoding="utf-8"))
        self.assertEqual({k: provenance["ran"]["runner"][k] for k in ("backend", "provider", "endpoint", "request_id",
                                                                "account")},
                         {"backend": "api", "provider": "fal", "endpoint": EDIT, "request_id": rid,
                          "account": "test-pilot"})
        run = self.tmp / "api_runs" / j["prompt_id"]
        self.until(lambda: not run.exists(), "its run folder to go")          # a run that went well leaves nothing

    def test_the_reference_goes_second(self):
        shutil.copy(PLATE, self.tmp / "shows" / "look.png")
        g = self.gateway()
        jid = self.submit(reference=f"{self.root}/shows/look.png")
        self.beat(g)
        self.ended(jid)
        body = next(iter(self.mock.requests.values()))["input"]
        uploads = [p for p in self.mock.files if "/out_" not in p]
        self.assertEqual([u.rpartition("/")[2] for u in body["image_urls"]], ["plate.png", "look.png"])
        self.assertEqual(len(uploads), 2)

    def test_what_fal_refuses_fails_the_job(self):
        g = self.gateway()
        failed, flagged = self.submit("MOCK_FAIL"), self.submit("MOCK_POLICY")
        self.beat(g)
        self.assertEqual(self.ended(failed, "failed")["error"],
                         "fal.ai couldn't run it (runner_server_error): MOCK_FAIL: the stand-in's model failed")
        self.assertEqual(self.ended(flagged, "failed")["error"],
                         "fal.ai finished, but fal.ai refused it: prompt: The content was flagged (fal.ai's content "
                         "check)")

    def test_cancelled_in_the_queue_and_while_running(self):
        g = self.gateway()
        self.mock.paused = True
        queued = self.submit()
        self.beat(g)
        self.until(lambda: self.job(queued)["status"] == "dispatched", "dispatched")
        self.wait(self.farm.cancel(queued))
        self.beat(g)
        self.ended(queued, "cancelled")
        self.assertEqual(next(iter(self.mock.requests.values()))["cancel"], "queue")
        self.mock.paused = False
        running = self.submit("MOCK_SLOW")
        self.beat(g)
        self.until(lambda: self.job(running)["status"] == "running", "running")
        self.wait(self.farm.cancel(running))
        self.beat(g)
        self.ended(running, "cancelled")
        self.assertEqual(list(self.mock.requests.values())[1]["cancel"], "running")

    def test_its_time_limit(self):
        g = self.gateway()
        g.runner._seconds = lambda wf: 0.3
        jid = self.submit("MOCK_SLOW")
        self.beat(g)
        error = self.ended(jid, "failed")["error"]
        self.assertIn("fal.ai didn't finish the job in 0.005 minutes, its time limit", error)
        self.assertEqual(next(iter(self.mock.requests.values()))["cancel"], "running")

    def test_a_restart_follows_its_jobs_again(self):
        g = self.gateway()
        self.mock.paused = True
        jid = self.submit()
        self.beat(g)
        self.until(lambda: self.job(jid)["status"] == "dispatched", "dispatched")
        self.wait(g.stop())                                 # the gateway stops; fal.ai keeps the job
        self.gateways.remove(g)
        self.mock.paused = False
        self.gateway()                                      # the manager hands the job back: followed again
        self.ended(jid)
        self.assertEqual(len(self.sent()), 1)

    def test_a_job_it_cant_tell_was_sent_is_never_sent_again(self):
        """It stopped between writing what it sends and hearing back: whether fal.ai has it,
        it can't tell. It fails, saying so, rather than paying twice."""
        g = self.gateway()
        self.mock.paused = True
        jid = self.submit()
        self.beat(g)
        self.until(lambda: self.job(jid)["status"] == "dispatched", "dispatched")
        self.wait(g.stop())
        self.gateways.remove(g)
        req = self.tmp / "api_runs" / self.job(jid)["prompt_id"] / "request.json"
        rec = json.loads(req.read_text(encoding="utf-8"))
        rec["state"] = "sending"
        req.write_text(json.dumps(rec), encoding="utf-8")
        self.gateway()
        error = self.ended(jid, "failed")["error"]
        self.assertTrue(error.startswith("The gateway stopped while sending the job to fal.ai, so it can't tell "
                                         "whether fal.ai got it. It wasn't sent again, so it is never paid for "
                                         "twice"), error)
        self.assertEqual(len(self.sent()), 1)

    def test_an_answer_lost_on_the_way_back(self):
        g = self.gateway()
        self.transport.fail.append(("POST", "/edit", httpx.ReadTimeout, True))
        jid = self.submit()
        self.beat(g)
        error = self.ended(jid, "failed")["error"]
        self.assertTrue(error.startswith("The gateway lost fal.ai's answer while sending the job (fal.ai at "
                                         "127.0.0.1:8189 didn't answer in time), so it can't tell whether fal.ai got "
                                         "it. It wasn't sent again"), error)
        self.assertEqual(len(self.sent()), 1)

    def test_a_png_or_exr_only(self):
        (self.tmp / "shows" / "plate.tif").write_bytes(b"II*\x00" + b"\x00" * 64)
        g = self.gateway()
        jid = self.submit()
        tif = self.farm.submit("nano_banana2_edit_fal", {"input": f"{self.root}/shows/plate.tif", "prompt": "x",
                                                         "output_path": f"{self.root}/shows/out"}, "ann")["id"]
        self.beat(g)
        self.assertEqual(self.ended(tif, "failed")["error"],
                         "fal.ai takes PNG, JPEG, WebP or MP4 files, and plate.tif isn't one. Render it as PNG or "
                         "EXR (an EXR is converted to PNG first).")
        self.ended(jid)
        self.assertEqual(len(self.sent()), 1)

    @unittest.skipIf(os.name == "nt", "file modes are POSIX's")
    def test_a_key_others_can_read_takes_it_offline(self):
        os.chmod(self.keys, 0o644)
        self.gateway()
        a = self.desk.agents[GATEWAY]
        self.assertFalse(a.online)
        self.assertIn("can be read by other users on this machine", a.error)
        jid = self.submit()
        self.beat(self.gateways[0])
        self.assertEqual(self.job(jid)["status"], "queued")
        self.assertEqual(self.mock.calls, [])

    def test_the_key_goes_to_fal_and_nowhere_else(self):
        """The requests key reaches the stand-in, and is in no log line, no message to the
        manager, no job, no run folder, no output and no other file the farm wrote."""
        g = self.gateway()
        good, bad = self.submit(), self.submit("MOCK_FAIL")
        self.beat(g)
        self.ended(good)
        self.ended(bad, "failed")
        self.wait(g.stop())
        self.gateways.remove(g)
        self.assertTrue(any(c["keyed"] for c in self.sent()))
        secret = KEY.partition(":")[2]
        for text in self.log.lines + self.said:
            self.assertNotIn(secret, text)
        for jid in (good, bad):
            self.assertNotIn(secret, json.dumps([self.job(jid), self.store.events(jid)], default=str))
        for path in self.tmp.rglob("*"):
            if path.is_file() and path != self.keys:
                self.assertNotIn(secret.encode(), path.read_bytes(), path)


@unittest.skipIf(Agent is None, "the gateway needs httpx")
class TheGatewaysCheck(Tmp):
    def test_agent_check_on_the_gateway(self):
        """`comfyfarm agent --config gateway.yaml --check`: its providers, keys and definitions,
        and never a key."""
        import contextlib
        import io
        from comfyfarm import cli
        cfg = AgentConfig(name="gw", base_dir=str(self.tmp), gateway={"enabled": True, "account": "test-pilot",
                          "providers": {"fal": {"url": BASE, "keys": {"requests": str(self.key_file())}}}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bad = cli.check_gateway(cfg)
        self.assertEqual(bad, 0, out.getvalue())
        self.assertIn(f"[ ok ] fal: 2 lane(s), sent to {BASE}, its keys read (account test-pilot)", out.getvalue())
        self.assertIn("[ ok ] API definition nano_banana2_edit_fal: the gateway can send it", out.getvalue())
        self.assertNotIn(KEY.partition(":")[2], out.getvalue())


if __name__ == "__main__":
    unittest.main()
