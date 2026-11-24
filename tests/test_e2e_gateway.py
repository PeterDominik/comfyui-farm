"""End-to-end, the gateway: phase 11 step 2's gate (decisions 0055, 0056). The manager
(`comfyfarm manager`), the gateway (`comfyfarm agent --config gateway.yaml`) and fal.ai's
stand-in (`tools/mock_fal.py --gateway gateway.yaml`) as processes, over HTTP. The job is
submitted by the Nuke node's own code (client/nuke/comfyfarm_node.py, with the stand-in
Nuke of tests/test_nuke_node.py) through the artist client, and comes back as a frame on
show storage.

Also: a job fal.ai refuses, the gateway on the Machines tab, `agent --check` on the gateway,
and the key, which reaches the stand-in and is in no other process's log, the manager's
database or any file the farm wrote.

    python -m unittest tests.test_e2e_gateway -v
"""

import importlib
import importlib.util
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for sub in ("client", "client/nuke", "tests"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))
farm = importlib.import_module("comfyfarm_client")
from test_e2e import free_port, wait_http          # noqa: E402
from test_nuke_node import make_nuke              # noqa: E402

WEB = bool(importlib.util.find_spec("uvicorn") and importlib.util.find_spec("fastapi"))
TOKEN = "e2e-agent-token"
KEY = "e2e-made-up:5c1f0e9d8b7a6c4d"            # the stand-in's; the secret part must appear nowhere else
BILLING_KEY = "e2e-billing:7d2a9c4e1b"             # its bill's, made up too
GATEWAY = "e2e-gateway"
SEQ = "/rdo/shows/xyz/.published/abc/abc_0010/plate/v1/abc_0010.plate.v1.%04d.png"     # a shot's plate
OTHER = "/rdo/shows/qqq/.published/def/def_0020/plate/v1/def_0020.plate.v1.%04d.png"   # a show fal.ai isn't for


def wait_port(port: int, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise RuntimeError(f"nothing answered on port {port}")


def png_size(path) -> tuple[int, int]:
    return struct.unpack(">II", Path(path).read_bytes()[16:24])


@unittest.skipUnless(WEB, "the manager needs the web stack (requirements.txt)")
class GatewayE2E(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="comfyfarm-gateway-"))
        root = cls.tmp.as_posix()
        for seq in (SEQ, OTHER):
            work = cls.tmp / Path(seq.removeprefix("/rdo/")).parent
            work.mkdir(parents=True)
            for f in (1049, 1050):
                shutil.copy(REPO / "workflows/gpu_test/eval/input.png", work / (Path(seq).name % f))
        # fal.ai is outside Rodeo: the farm's policy allows it on xyz alone (decision 0058).
        (cls.tmp / "policy.yaml").write_text("""version: 1
contact: the CG supervisors
providers:
  - name: fal-on-xyz
    shows: [xyz]
    allow: [fal]
    reason: The fal.ai pilot's show
budgets:
  confirm_above: 0.1
  rules:
    - name: xyz-pilot
      shows: [xyz]
      cap: 1
      reason: The fal.ai pilot's budget
""", encoding="utf-8")
        cls.port, cls.fal_port = free_port(), free_port()
        (cls.tmp / "config.yaml").write_text(f"""
server: {{host: 127.0.0.1, port: {cls.port}}}
database: {root}/manager/farm.db
log_dir: {root}/logs
workflows_dir: {REPO.as_posix()}/workflows
workers: []
agents: {{token: {TOKEN}}}
paths:
  mappings: [{{posix: /rdo/shows, windows: "{root}/shows"}}]
  output_root: "{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"
providers: {{enabled: [fal]}}
policy: {{enabled: true, file: "{root}/policy.yaml"}}
provenance: {{enabled: true}}
preview: {{enabled: false}}
convert: {{scratch_dir: {root}/manager/scratch}}
""", encoding="utf-8")
        key = cls.tmp / "keys/fal_requests.key"
        key.parent.mkdir()
        key.write_text(KEY + "\n", encoding="utf-8")
        os.chmod(key, 0o600)
        cls.key = key
        billing = cls.tmp / "keys/fal_billing.key"           # its bill takes its own key (decision 0061)
        billing.write_text(BILLING_KEY + "\n", encoding="utf-8")
        os.chmod(billing, 0o600)
        (cls.tmp / "gateway.yaml").write_text(f"""
name: {GATEWAY}
manager: http://127.0.0.1:{cls.port}
token: {TOKEN}
log_dir: {root}/logs
paths:
  mappings: [{{posix: /rdo/shows, windows: "{root}/shows"}}]
convert: {{scratch_dir: {root}/gateway/scratch}}
heartbeat_wait: 2
retry_seconds: 0.5
gateway:
  enabled: true
  account: e2e
  runs_dir: {root}/gateway/runs
  poll_seconds: 0.2
  providers:
    fal:
      url: http://127.0.0.1:{cls.fal_port}
      keys: {{requests: keys/fal_requests.key, billing: keys/fal_billing.key}}
""", encoding="utf-8")
        cls.old_url = os.environ.get("COMFYFARM_URL")
        os.environ["COMFYFARM_URL"] = f"http://127.0.0.1:{cls.port}"
        cls.procs = {}
        cls.start("mock_fal", [str(REPO / "tools/mock_fal.py"), "--gateway", str(cls.tmp / "gateway.yaml"),
                               "--queue-seconds", "0.3", "--run-seconds", "0.5", "--bill"])
        wait_port(cls.fal_port)
        cls.start("manager", ["-m", "comfyfarm", "--config", str(cls.tmp / "config.yaml"), "manager"])
        wait_http(f"http://127.0.0.1:{cls.port}/api/v1/health")
        cls.start("gateway", ["-m", "comfyfarm", "agent", "--config", str(cls.tmp / "gateway.yaml")])
        cls.wait_gateway_ready()

    @classmethod
    def tearDownClass(cls):
        for p in cls.procs.values():
            if p.poll() is None:
                p.terminate()
                p.wait(10)
        if cls.old_url is None:
            os.environ.pop("COMFYFARM_URL", None)
        else:
            os.environ["COMFYFARM_URL"] = cls.old_url
        if os.environ.get("KEEP_E2E"):
            print(f"\nkept {cls.tmp}")
        else:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def start(cls, name, args):
        with open(cls.tmp / f"{name}.out", "a") as log:
            cls.procs[name] = subprocess.Popen([sys.executable, *args], cwd=str(REPO), stdout=log,
                                               stderr=subprocess.STDOUT)

    @classmethod
    def wait_gateway_ready(cls, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            ws = {w["name"]: w for w in farm.workers()}
            w = ws.get(GATEWAY)
            if w and w["online"] and w["connected"] and w["checked"]:
                return w
            time.sleep(0.2)
        raise RuntimeError(f"the gateway never became ready: {farm.workers()}\n"
                           f"{(cls.tmp / 'gateway.out').read_text(errors='replace')[-3000:]}")

    def wait_for(self, job_id, statuses=("done", "failed", "cancelled"), timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            j = farm.get(job_id)
            if j["status"] in statuses:
                return j
            time.sleep(0.15)
        self.fail(f"job {job_id[:8]} stuck in {farm.get(job_id)['status']}, wanted {statuses}")

    # ------------------------------------------------------------ tests
    def test_1_a_job_from_the_nuke_node(self):
        """The gate: Nuke's node submits Nano Banana 2 edit on the current frame, the gateway
        sends it to the stand-in, and the frame comes back on show storage."""
        nk = make_nuke()
        with mock.patch.dict(sys.modules, {"nuke": nk}), mock.patch.object(farm, "POSIX", True):
            for mod in ("comfyfarm_nuke", "comfyfarm_panel", "comfyfarm_node"):
                sys.modules.pop(mod, None)
            m = importlib.import_module("comfyfarm_node")
            try:
                def now(work, done):            # the farm answers in the main thread
                    try:
                        result, error = work(), None
                    except Exception as e:  # noqa: BLE001
                        result, error = None, e
                    done(result, error)
                with mock.patch.object(m, "_in_background", now), mock.patch.object(m, "track", lambda ids: None):
                    wf = next(w for w in farm.workflows() if w["name"] == "nano_banana2_edit_fal")
                    plate = nk.nodes.Read(name="Plate", file=SEQ)
                    plate["first"].setValue(1001)
                    plate["last"].setValue(1100)
                    node = m.create_node(wf, connect={"input": plate})
                    node["p_prompt"].setValue("remove the crane")
                    m.submit(node)
                    self.assertEqual(nk.messages, [])
                    records = m._records(node)
                    self.assertTrue(records, node["cf_note"].value())    # what the node says went wrong
                    jid = records[-1]["id"]
            finally:
                for mod in ("comfyfarm_nuke", "comfyfarm_panel", "comfyfarm_node"):
                    sys.modules.pop(mod, None)
        j = self.wait_for(jid)
        self.assertEqual(j["status"], "done", j.get("error"))
        self.assertEqual(j["worker"], GATEWAY)
        self.assertAlmostEqual(j["info"]["cost"]["amount"], 0.08)          # 1 image at 1K (decision 0059)
        out = j["outputs"]
        self.assertEqual((out["frames"], out["first"]), (1, 1050))          # numbered like the input frame
        frame = out["frame_pattern"] % 1050
        self.assertEqual(png_size(frame), (1024, 1024))                     # the stand-in's 1K grey square
        ran = json.loads(Path(out["provenance"]).read_text(encoding="utf-8"))["ran"]["runner"]
        self.assertEqual({k: ran[k] for k in ("backend", "provider", "endpoint", "account")},
                         {"backend": "api", "provider": "fal", "endpoint": "fal-ai/nano-banana-2/edit",
                          "account": "e2e"})
        said = (self.tmp / "mock_fal.out").read_text(errors="replace")
        self.assertIn("POST /fal-ai/nano-banana-2/edit -> 200", said)
        self.assertIn("GET /v1/models/pricing -> 200", said)               # its prices, read as it started
        self.assertIn(f"GET /fal-ai/nano-banana-2/requests/{ran['request_id']} -> 200", said)
        # In the ledger (decision 0060): charged at its estimate, with the request fal.ai had.
        self.assertEqual({k: j["spend"][k] for k in ("state", "amount", "request_id", "account", "show")},
                         {"state": "charged", "amount": 0.08, "request_id": ran["request_id"], "account": "e2e",
                          "show": "xyz"})
        self.assertEqual(j["info"]["cost"]["budget"]["rule"], "xyz-pilot")

    def test_2_a_job_fal_refuses(self):
        job = farm.submit("nano_banana2_edit_fal", {"input": f"{SEQ} 1049", "prompt": "MOCK_FAIL"})
        j = self.wait_for(job["id"])
        self.assertEqual((j["status"], j["error"]),
                         ("failed", "fal.ai couldn't run it (runner_server_error): MOCK_FAIL: the stand-in's "
                                    "model failed"))

    def test_2b_another_show_is_refused_with_the_rule_named(self):
        """Step 3's gate across processes: fal.ai on a show no rule allows it for."""
        with self.assertRaises(farm.FarmError) as e:
            farm.submit("nano_banana2_edit_fal", {"input": f"{OTHER} 1050", "prompt": "rain"})
        self.assertEqual(str(e.exception), "Show qqq isn't cleared to send work to fal.ai, outside Rodeo: no policy "
                                           "rule allows it. (Policy: providers.) Ask the CG supervisors if this is "
                                           "wrong.")

    def test_2c_a_costly_job_waits_for_a_yes(self):
        """budgets.confirm_above is $0.10 here: a 2K image ($0.12) is queued once it is said
        yes to, over HTTP, with its estimate in the farm's answer (decision 0060)."""
        params = {"input": f"{SEQ} 1049", "prompt": "rain", "resolution": "2K"}
        with self.assertRaises(farm.FarmError) as e:
            farm.submit("nano_banana2_edit_fal", params)
        self.assertIn("a job of $0.10 or more is submitted once you say yes", str(e.exception))
        self.assertEqual((e.exception.confirm["amount"], e.exception.confirm["confirm"]), (0.12, True))
        job = farm.submit("nano_banana2_edit_fal", params, confirm_cost=e.exception.confirm["amount"])
        self.assertEqual(job["spend"]["state"], "held")
        j = self.wait_for(job["id"])
        self.assertEqual((j["status"], j["spend"]["state"], j["spend"]["amount"]), ("done", "charged", 0.12))
        spend = {s["show"]: s for s in farm._request("GET", "/api/v1/policy")["spend_now"]}["xyz"]
        self.assertEqual((spend["cap"], spend["rule"]), (1, "xyz-pilot"))
        self.assertGreaterEqual(spend["charged"], 0.2)
        self.assertIn("function withYes", farm._request("GET", "/spend.js", raw=True))

    def test_2d_the_bill_read_into_the_ledger(self):
        """`comfyfarm spend reconcile` on the manager's host (decision 0061): the gateway reads the
        stand-in's bill with its billing key, and the jobs that finished are billed at what it
        charged. A top-up recorded the same way shows beside them."""
        def comfyfarm(*args):
            r = subprocess.run([sys.executable, "-m", "comfyfarm", "--config", str(self.tmp / "config.yaml"), *args],
                               cwd=str(REPO), capture_output=True, text=True, timeout=120)
            return r.returncode, r.stdout + r.stderr
        code, said = comfyfarm("spend", "reconcile")
        self.assertEqual(code, 0, said)
        self.assertIn("fal (e2e), the stand-in:", said)
        v = farm._request("GET", "/api/v1/spend")
        done = [r for r in v["rows"] if r["state"] == "billed"]
        self.assertEqual(sorted(r["actual"] for r in done), [0.08, 0.12])     # 1K and 2K, as the stand-in charged
        self.assertEqual({r["state"] for r in v["rows"]} - {"billed"}, {"charged"})   # MOCK_FAIL: within the grace
        code, said = comfyfarm("spend", "topup", "fal", "50", "--note", "IT, the pilot")
        self.assertEqual((code, said.strip()), (0, "Recorded: $50.00 on fal (e2e), IT, the pilot."))
        code, said = comfyfarm("spend")
        self.assertIn("fal.ai (e2e): topped up $50.00, the farm's jobs $0.28, balance $49.80", said)
        csv = farm._request("GET", "/api/v1/spend?format=csv&show=xyz", raw=True)
        self.assertTrue(csv.startswith("queued,show,user,job,job_name,workflow,"), csv[:200])
        self.assertEqual(len(csv.strip().splitlines()), 1 + len(v["rows"]))
        self.assertNotIn(BILLING_KEY, (self.tmp / "gateway.out").read_text(errors="replace"))

    def test_3_on_the_machines_tab(self):
        w = {x["name"]: x for x in farm.workers()}[GATEWAY]
        self.assertEqual((w["lanes"], w["max_inflight"]), ({"fal": 2}, 2))
        self.assertIn("function laneText", farm._request("GET", "/gateway.js", raw=True))
        self.assertIn('<script src="/gateway.js"></script>', farm._request("GET", "/", raw=True))

    def test_4_agent_check_on_the_gateway(self):
        r = subprocess.run([sys.executable, "-m", "comfyfarm", "agent", "--config", str(self.tmp / "gateway.yaml"),
                            "--check"], cwd=str(REPO), capture_output=True, text=True, timeout=120)
        self.assertIn(f"[ ok ] manager http://127.0.0.1:{self.port}: reachable, token accepted", r.stdout)
        self.assertIn(f"[ ok ] fal: 2 lane(s), sent to http://127.0.0.1:{self.fal_port}, its keys read (account e2e)",
                      r.stdout)
        self.assertIn("[ ok ] API definition nano_banana2_edit_fal: the gateway can send it", r.stdout)
        self.assertNotIn(KEY.partition(":")[2], r.stdout + r.stderr)

    def test_5_the_key_stays_on_the_gateway(self):
        """After the jobs above: in no process's log or output, the manager's database, the
        gateway's run folders or the outputs. Only the key file holds it."""
        secret = KEY.partition(":")[2].encode()
        for path in self.tmp.rglob("*"):
            if path.is_file() and path != self.key:
                self.assertNotIn(secret, path.read_bytes(), path)


if __name__ == "__main__":
    unittest.main()
