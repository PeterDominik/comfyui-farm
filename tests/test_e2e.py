"""End-to-end: real gateway + mock ComfyUI as subprocesses, driven through the
artist client. Covers the happy path, cancels, failures, restarts of either side,
and a missing node pack.

    python -m unittest tests.test_e2e -v
"""

import importlib
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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "client"))
farm = importlib.import_module("comfyfarm_client")
sys.path.insert(0, str(REPO / "tests"))
from test_textures import exr_header, exr_sized, png_bytes

from comfyfarm.fileio.tools import find_tool

# The farm's own lookup, so the fixtures are made with the ffmpeg that requirements.txt
# installs (imageio-ffmpeg) when the machine has none on PATH, as on a CI runner.
FFMPEG = find_tool("ffmpeg") or ["ffmpeg"]

SEQ = "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi/abc_0010.pre.v1.%04d.png"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    raise RuntimeError(f"{url} never came up")


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="comfyfarm-e2e-"))
        plates = cls.tmp / "shows/xyz/.published/abc/abc_0010/render.comp/pre/v1/main/hi"
        plates.mkdir(parents=True)
        numeric = cls.tmp / "shows/ply/.published/15454/15454_656551/render.comp/pre/v1/main/hi"
        numeric.mkdir(parents=True)
        for f in range(1001, 1004):
            (numeric / f"15454_656551.pre.v1.{f}.png").write_bytes(b"x")
        loose = cls.tmp / "shows/ply/work/plates"
        loose.mkdir(parents=True)
        (loose / "plate.1001.png").write_bytes(b"x")
        for f in range(1001, 1013):
            (plates / f"abc_0010.pre.v1.{f}.png").write_bytes(b"x")
        cls.make_conversion_fixtures()
        cls.make_texture_fixtures()
        cls.comfy_port, cls.gw_port = free_port(), free_port()
        root = cls.tmp.as_posix()
        (cls.tmp / "config.yaml").write_text(f"""
server: {{host: 127.0.0.1, port: {cls.gw_port}}}
database: {root}/data/farm.db
log_dir: {root}/logs
workflows_dir: {REPO.as_posix()}/workflows
workers: [{{name: mock, url: "http://127.0.0.1:{cls.comfy_port}", max_inflight: 2}}]
paths:
  mappings: [{{posix: /rdo/shows, windows: "{root}/shows"}}]
  output_root: "{root}/shows/{{show}}/_project_ref/_cg/comfyui/renders"
farm: {{reconcile_seconds: 1.5, health_seconds: 0.5, lost_after_seconds: 60}}
ocio: {{config: "{root}/ocio/config.ocio"}}
convert:
  scratch_dir: {root}/scratch
  frame_workers: 3
  tools: {{oiiotool: ["{Path(sys.executable).as_posix()}", "{(REPO / 'tests/fake_oiiotool.py').as_posix()}"]}}
""")
        os.environ["FAKE_OIIOTOOL_LOG"] = str(cls.tmp / "oiiotool.log")
        os.environ["COMFYFARM_URL"] = f"http://127.0.0.1:{cls.gw_port}"
        cls.comfy = cls.gw = None
        cls.start_comfy()
        cls.start_gateway()

    @classmethod
    def make_conversion_fixtures(cls):
        # Stand-in for the repo's OCIO config (ocio/config.ocio).
        (cls.tmp / "ocio").mkdir()
        (cls.tmp / "ocio/config.ocio").write_text(
            "ocio_profile_version: 2\nroles:\n  scene_linear: ACES - ACEScg\n  color_picking: Output - sRGB\n")
        shot = cls.tmp / "shows/xyz/.published/abc/abc_0010"
        exr = shot / "plate/v1"
        exr.mkdir(parents=True)
        for name, frames in (("plate", range(1001, 1006)), ("slow", range(1001, 1004)), ("broken", range(1001, 1003))):
            for f in frames:
                (exr / f"abc_0010.{name}.v1.{f}.exr").write_bytes(b"x")
        (shot / "edit").mkdir()
        subprocess.run([*FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24", "-frames:v", "12",
                        "-timecode", "00:00:41:17", "-c:v", "prores_ks", str(shot / "edit/abc_0010_ref.mov")], check=True)
        subprocess.run([*FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=25", "-frames:v", "4",
                        "-c:v", "prores_ks", str(cls.tmp / "shows/ply/work/notc.mov")], check=True)
        # A shot work area: /shows/<show>/<seq>/<shot number>/movhistory/
        movhistory = cls.tmp / "shows/xyz/abc/0010/movhistory"
        movhistory.mkdir(parents=True)
        subprocess.run([*FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24", "-frames:v", "6",
                        "-timecode", "00:00:41:16", "-c:v", "prores_ks",     # slate at 1000, plate 1001-1005
                        str(movhistory / "abc_0010.ingest.mp01.rdo-nwb.distort.compPlate.v2.mov")], check=True)

    @classmethod
    def make_texture_fixtures(cls):
        tex = cls.tmp / "shows/xyz/_asset/rockA/cg/textures"
        for sub, names in (("src", ["rockA_diffuse.1001.png", "rockA_diffuse.1002.png", "rockA_diffuse.1011.png"]),
                           ("photo", ["cliff.jpg"]),
                           ("exr", ["rockA_color.1001.exr", "rockA_color.1002.exr"]),
                           ("mixed", ["rockA_diffuse.1001.png", "rockA_normal.1001.png"])):
            (tex / sub).mkdir(parents=True)
            for n in names:
                (tex / sub / n).write_bytes(b"x")
        # More tiles than CHORD's batch (4): a set. Only the headers are read (the sizes).
        (tex / "set").mkdir()
        for u in range(1001, 1011):
            (tex / "set" / f"rockB_diffuse.{u}.png").write_bytes(png_bytes(2, (2048, 2048)))
        (tex / "setexr").mkdir()
        for u in range(1001, 1007):   # 1005 starts the second batch and has no R, G, B
            data = exr_header(["diffuse.B", "diffuse.G", "diffuse.R"], (0, 0, 2047, 2047)) if u == 1005 \
                else exr_sized(2048, 2048)
            (tex / "setexr" / f"rockB_color.{u}.exr").write_bytes(data)
        # Larger than CHORD's work_resolution (1024). Only the header is read (the size).
        (tex / "big").mkdir()
        (tex / "big" / "rockA_big.png").write_bytes(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                                                    + struct.pack(">IIBBBBB", 4096, 2048, 8, 2, 0, 0, 0) + b"\0" * 4)

    def oiiotool_calls(self):
        try:
            return [json.loads(line) for line in (self.tmp / "oiiotool.log").read_text().splitlines()]
        except FileNotFoundError:
            return []

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("FAKE_OIIOTOOL_LOG", None)   # its folder goes below; later tests run oiiotool too
        for p in (cls.gw, cls.comfy):
            if p and p.poll() is None:
                p.terminate()
                p.wait(10)
        if os.environ.get("KEEP_E2E"):
            print(f"\nkept {cls.tmp}")
        else:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------ process control
    @classmethod
    def start_comfy(cls, *extra):
        with open(cls.tmp / "mock.log", "a") as log:
            cls.comfy = subprocess.Popen(
                [sys.executable, str(REPO / "tools/mock_comfyui.py"), "--port", str(cls.comfy_port),
                 "--output", str(cls.tmp / "comfy_output"), "--delay", "0.06", *extra],
                stdout=log, stderr=subprocess.STDOUT)
        wait_http(f"http://127.0.0.1:{cls.comfy_port}/system_stats")

    @classmethod
    def stop_comfy(cls):
        cls.comfy.kill()   # a crash, not a clean shutdown (SIGKILL; TerminateProcess on Windows)
        cls.comfy.wait(10)

    @classmethod
    def start_gateway(cls):
        with open(cls.tmp / "gateway.log", "a") as log:
            cls.gw = subprocess.Popen(
                [sys.executable, "-m", "comfyfarm", "--config", str(cls.tmp / "config.yaml"), "serve"],
                cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT)
        wait_http(f"http://127.0.0.1:{cls.gw_port}/api/v1/health")
        cls.wait_worker_ready()

    @classmethod
    def wait_worker_ready(cls, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            ws = farm._request("GET", "/api/v1/workers")["workers"]
            if ws and ws[0]["online"] and ws[0]["connected"]:
                return ws[0]
            time.sleep(0.2)
        raise RuntimeError("worker never became ready")

    # ------------------------------------------------------------ helpers
    def submit(self, rng="1001-1012", **params):
        params.setdefault("input", f"{SEQ} {rng}" if rng else SEQ)
        return farm.submit("ltx25_alpha_gen", params)

    def wait_for(self, job_id, statuses, timeout=40):
        end = time.time() + timeout
        while time.time() < end:
            j = farm.get(job_id)
            if j["status"] in statuses:
                return j
            time.sleep(0.15)
        self.fail(f"job {job_id[:8]} stuck in {farm.get(job_id)['status']}, wanted {statuses}")

    def outputs(self, job):
        return sorted(os.listdir(job["info"]["output_dir"]))

    # ------------------------------------------------------------ tests
    def test_01_happy_path(self):
        job = self.submit("1003-1010")
        self.assertEqual(job["status"], "queued")
        self.assertEqual((job["info"]["input"]["first"], job["info"]["input"]["last"]), (1003, 1010))
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        out = done["outputs"]
        self.assertEqual((out["frames"], out["first"], out["last"]), (8, 1003, 1010))
        self.assertTrue(out["frame_pattern_posix"].startswith("/rdo/shows/xyz/_project_ref/"))
        names = self.outputs(done)
        self.assertIn("abc_0010.pre.v1.alpha_v001.1003.png", names)   # named after the source
        # The agent's preview movie of the frames, next to them, played by the dashboard.
        self.assertIn("abc_0010.pre.v1.alpha_v001_preview.mp4", names, done["warnings"])
        self.assertTrue(out["preview_posix"].endswith("/abc_0010/abc_0010.pre.v1.alpha_v001_preview.mp4"))
        with urllib.request.urlopen(f"{os.environ['COMFYFARM_URL']}/api/v1/jobs/{job['id']}/preview") as r:
            self.assertEqual((r.headers["Content-Type"], r.read(12)[4:8]), ("video/mp4", b"ftyp"))
        self.assertTrue(done["info"]["output_dir_posix"].endswith("/comfyui/renders/abc_0010"))
        self.assertFalse([n for n in names if "_raw" in n], "raw frames should be renamed")
        prompt = farm._request("GET", f"/api/v1/jobs/{job['id']}/prompt")
        self.assertEqual(prompt["5516:4832"]["inputs"]["noise_seed"], 42)   # not on the form: as exported
        self.assertEqual(prompt["9104"]["inputs"]["frames"], "1003-1010")    # ComfyFarm Load Images: exact frames
        self.assertTrue(prompt["9104"]["inputs"]["path"].endswith("/abc_0010.pre.v1.%04d.png"))
        self.assertEqual(done["progress"], 1.0)

    def test_02_versions_and_cancel_waiting_jobs(self):
        a, b, c = self.submit(), self.submit(), self.submit()
        self.assertEqual([j["info"]["version"] for j in (a, b, c)],
                         [a["info"]["version"], a["info"]["version"] + 1, a["info"]["version"] + 2])
        self.assertEqual(len({j["info"]["output_dir"] for j in (a, b, c)}), 1)   # same shot folder
        self.wait_for(b["id"], ("dispatched", "running"))     # b sits in ComfyUI's own queue
        cb = farm.cancel(b["id"])
        cc = farm.cancel(c["id"])                             # c still in the gateway queue
        self.assertEqual(cc["status"], "cancelled")
        self.assertEqual(self.wait_for(b["id"], ("cancelled", "done", "failed"))["status"], "cancelled", cb)
        self.assertEqual(self.wait_for(a["id"], ("done", "failed"))["status"], "done")

    def test_03_cancel_running(self):
        job = self.submit()
        self.wait_for(job["id"], ("running",))
        time.sleep(0.5)
        farm.cancel(job["id"])
        self.assertEqual(self.wait_for(job["id"], ("cancelled", "done", "failed"))["status"], "cancelled")

    def test_04_failure_reports_node_and_traceback(self):
        # The mock fails any node with MOCK_FAIL in a text input: here OUTPUT_PATH, which gets
        # the output folder.
        (self.tmp / "shows/xyz/_project_ref/_cg/fail").mkdir(parents=True, exist_ok=True)
        job = self.submit(output_path="/rdo/shows/xyz/_project_ref/_cg/fail/MOCK_FAIL")
        failed = self.wait_for(job["id"], ("failed", "done"))
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Mock failure requested", failed["error"])
        self.assertIn("PrimitiveString (node 9093", failed["error"])
        self.assertTrue(failed["info"].get("traceback"))

    def test_045_picked_folder_is_root_with_shot_folder(self):
        comfy = self.tmp / "shows/ply/_project_ref/_cg/comfy"
        comfy.mkdir(parents=True)
        root = "/rdo/shows/ply/_project_ref/_cg/comfy/test"
        shot_dir = comfy / "test/abc_0010"
        # Dry run: nothing reserved; <root>/<shot>/<source>.alpha_v001.<frame>.png
        plan = farm._request("POST", "/api/v1/plan", {"workflow": "ltx25_alpha_gen",
                                                       "params": {"input": f"{SEQ} 1001-1003", "output_path": root}})
        self.assertEqual((plan["output_dir_posix"], plan["first_file"], plan["last_file"], plan["version"]),
                         (root + "/abc_0010", "abc_0010.pre.v1.alpha_v001.1001.png",
                          "abc_0010.pre.v1.alpha_v001.1003.png", 1))
        job = self.submit("1001-1003", output_path=root)
        # Second job before the first has written anything: v001 is reserved -> v002, same folder.
        nxt = self.submit("1001-1003", output_path=root)
        self.assertEqual((job["info"]["version"], nxt["info"]["version"]), (1, 2))
        self.assertEqual(job["info"]["output_dir_posix"], nxt["info"]["output_dir_posix"])
        for j in (job, nxt):
            self.assertEqual(self.wait_for(j["id"], ("done", "failed"))["status"], "done")
        names = sorted(os.listdir(shot_dir))
        self.assertEqual(names, sorted([f"abc_0010.pre.v1.alpha_v00{v}.{f}.png" for v in (1, 2) for f in (1001, 1002, 1003)]
                                       + [f"abc_0010.pre.v1.alpha_v00{v}_preview.mp4" for v in (1, 2)]))
        # Files exist now -> Run again is v003, still in the shot folder.
        again = farm.retry(job["id"])
        self.assertEqual((again["info"]["version"], again["info"]["output_dir_posix"]), (3, root + "/abc_0010"))
        self.wait_for(again["id"], ("done", "failed"))
        # Versions are per source: a different input in the same shot folder starts at v001.
        other = self.tmp / "shows/xyz/.published/abc/abc_0010/render.comp/pre/v2/main/hi"
        other.mkdir(parents=True)
        (other / "abc_0010.pre.v2.1001.png").write_bytes(b"x")
        o = farm.submit("ltx25_alpha_gen", {"input": "/rdo/shows/xyz/.published/abc/abc_0010/render.comp/pre/v2/"
                                                     "main/hi/abc_0010.pre.v2.%04d.png", "output_path": root})
        self.assertEqual(o["info"]["version"], 1)
        self.wait_for(o["id"], ("done", "failed"))
        with self.assertRaisesRegex(farm.FarmError, "Can't write into .published"):
            self.submit(output_path="/rdo/shows/xyz/.published/abc/abc_0010/comfy")
        with self.assertRaisesRegex(farm.FarmError, "doesn't exist"):
            self.submit(output_path="/rdo/shows/xyz/not_there/test")
        with self.assertRaisesRegex(farm.FarmError, "must be under /rdo/shows"):
            self.submit(output_path="/tmp/x")
        wfs = {w["name"]: w for w in farm.workflows()}
        p = {x["name"]: x for x in wfs["ltx25_alpha_gen"]["params"]}
        self.assertTrue(p["output_path"]["placeholder"].endswith("/comfyui/renders/{shot}"))

    def test_0455_one_image_workflow(self):
        """Wan I2I: one frame of a sequence, through ComfyFarm Load Image, numbered like it."""
        with self.assertRaisesRegex(farm.FarmError, "takes one image.*Put the frame to use after it"):
            farm.submit("wan22_i2i", {"input": SEQ})
        job = farm.submit("wan22_i2i", {"input": f"{SEQ} 1007", "prompt": "fog", "noise": 0.25})
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        # The shot's folder also holds the LTX tests' frames: just this workflow's.
        self.assertEqual([n for n in self.outputs(done) if ".i2i_" in n], ["abc_0010.pre.v1.i2i_v001.1007.png"])
        prompt = farm._request("GET", f"/api/v1/jobs/{job['id']}/prompt")
        self.assertTrue(prompt["254"]["inputs"]["path"].endswith("/abc_0010.pre.v1.%04d.png 1007"))
        self.assertEqual((prompt["255"]["inputs"]["value"], prompt["210"]["inputs"]["value"]), ("fog", 0.25))
        wan = {w["name"]: w for w in farm.workflows()}["wan22_i2i"]
        self.assertEqual(next(p for p in wan["params"] if p["name"] == "input")["takes"], "image")
        # One frame of a movie, numbered from its timecode (00:00:41:17 at 24 fps = 1001).
        mov = "/rdo/shows/xyz/.published/abc/abc_0010/edit/abc_0010_ref.mov"
        with self.assertRaisesRegex(farm.FarmError, r"a movie of 12 frames, 1001-1012"):
            farm.submit("wan22_i2i", {"input": mov})
        job = farm.submit("wan22_i2i", {"input": f"{mov} 1005"})
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertIn("abc_0010_ref.i2i_v001.1005.png", self.outputs(done))

    def test_0456_two_inputs(self):
        """LTX layout to render: the layout sequence (primary) and a reference frame."""
        with self.assertRaisesRegex(farm.FarmError, "Reference frame takes one image, and .* gives more"):
            farm.submit("ltx25_layout_render", {"input": f"{SEQ} 1001-1004", "reference": SEQ, "prompt": "x"})
        job = farm.submit("ltx25_layout_render", {"input": f"{SEQ} 1001-1004", "reference": f"{SEQ} 1009",
                                                  "prompt": "a castle"})
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual([n for n in self.outputs(done) if ".render_" in n],
                         [f"abc_0010.pre.v1.render_v001.{f}.png" for f in (1001, 1002, 1003, 1004)]
                         + ["abc_0010.pre.v1.render_v001_preview.mp4"])
        self.assertFalse([w for w in done["warnings"] if "output frames" in w])   # as many as the layout
        prompt = farm._request("GET", f"/api/v1/jobs/{job['id']}/prompt")
        self.assertTrue(prompt["9060"]["inputs"]["path"].endswith("/abc_0010.pre.v1.%04d.png 1009"))
        self.assertEqual(prompt["9061"]["inputs"]["frames"], "1001-1004")

    def test_046_numeric_shot_default_destination(self):
        job = farm.submit("ltx25_alpha_gen", {"input": "/rdo/shows/ply/.published/15454/15454_656551/render.comp/"
                                                       "pre/v1/main/hi/15454_656551.pre.v1.%d.png"})
        self.assertEqual(job["info"]["shot"], "15454_656551")
        self.assertTrue(job["info"]["output_dir_posix"].endswith(
            "/shows/ply/_project_ref/_cg/comfyui/renders/15454_656551"))
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual((done["status"], done["outputs"]["first"], done["outputs"]["last"]), ("done", 1001, 1003))
        # Not a publish path and no Output folder: refuse instead of guessing a shot.
        with self.assertRaisesRegex(farm.FarmError, "no shot from the input path"):
            farm.submit("ltx25_alpha_gen", {"input": "/rdo/shows/ply/work/plates/plate.%04d.png"})
        ok = farm.submit("ltx25_alpha_gen", {"input": "/rdo/shows/ply/work/plates/plate.%04d.png",
                                             "output_path": "/rdo/shows/ply/work/plates_alpha"})
        self.assertEqual(ok["name"], "plate")
        # No shot to make a folder from: straight into the output folder, and the job says so.
        self.assertTrue(ok["info"]["output_dir_posix"].endswith("/work/plates_alpha"))
        self.assertIn("No shot in the input path", " ".join(ok["info"]["notes"]))
        self.assertEqual(self.wait_for(ok["id"], ("done", "failed"))["status"], "done")
        self.assertTrue((self.tmp / "shows/ply/work/plates_alpha/plate.alpha_v001.1001.png").is_file())

    def test_047_exr_input_converted_with_repo_ocio(self):
        src = "/rdo/shows/xyz/.published/abc/abc_0010/plate/v1/abc_0010.plate.v1.%04d.exr"
        plan = farm._request("POST", "/api/v1/plan", {"workflow": "ltx25_alpha_gen", "params": {"input": f"{src} 1002-1005"}})
        c = plan["convert"]
        self.assertEqual((plan["input_kind"], c["from"], c["to"]), ("exr", "scene_linear", "color_picking"))
        self.assertTrue(c["config"].endswith("/ocio/config.ocio"))
        job = farm.submit("ltx25_alpha_gen", {"input": f"{src} 1002-1005"})
        self.assertEqual(job["status"], "queued")   # converted on the worker that takes it
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual((done["outputs"]["frames"], done["outputs"]["first"], done["outputs"]["last"]), (4, 1002, 1005))
        out = self.tmp / "shows/xyz/_project_ref/_cg/comfyui/renders/abc_0010"
        self.assertTrue((out / "abc_0010.plate.v1.alpha_v001.1002.png").is_file())
        calls = [c for c in self.oiiotool_calls() if "plate" in " ".join(c["args"])]
        self.assertEqual(len(calls), 4)
        a = calls[0]["args"]   # a Nuke Write set to "default (color_picking)"
        i = a.index("--colorconvert")
        self.assertEqual(a[i + 1:i + 3], ["scene_linear", "color_picking"])
        self.assertEqual(a[a.index("--colorconfig") + 1], (self.tmp / "ocio/config.ocio").as_posix())
        self.assertIn("--croptofull", a)
        self.assertEqual(a[a.index("-d") + 1], "uint8")
        self.assertFalse((self.tmp / "scratch" / job["id"]).exists(), "scratch frames should be cleaned up")

    def test_048_movie_numbered_from_timecode(self):
        mov = "/rdo/shows/xyz/.published/abc/abc_0010/edit/abc_0010_ref.mov"
        plan = farm._request("POST", "/api/v1/plan", {"workflow": "ltx25_alpha_gen", "params": {"input": mov}})
        self.assertEqual((plan["convert"]["timecode"], plan["convert"]["start"], plan["convert"]["end"]),
                         ("00:00:41:17", 1001, 1012))
        self.assertEqual((plan["first_file"], plan["last_file"]),
                         ("abc_0010_ref.alpha_v001.1001.png", "abc_0010_ref.alpha_v001.1012.png"))
        job = farm.submit("ltx25_alpha_gen", {"input": f"{mov} 1004-1007"})
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual((done["outputs"]["frames"], done["outputs"]["first"], done["outputs"]["last"]), (4, 1004, 1007))
        out = self.tmp / "shows/xyz/_project_ref/_cg/comfyui/renders/abc_0010"
        self.assertTrue((out / "abc_0010_ref.alpha_v001.1004.png").is_file())
        with self.assertRaisesRegex(farm.FarmError, "outside the movie: the movie runs 1001-1012"):
            farm.submit("ltx25_alpha_gen", {"input": f"{mov} 990-1005"})
        # No timecode: numbered from the default, and the job says so.
        nt = farm.submit("ltx25_alpha_gen", {"input": "/rdo/shows/ply/work/notc.mov",
                                             "output_path": "/rdo/shows/ply/work/notc_alpha"})
        self.assertIn("no timecode", " ".join(nt["warnings"]))
        done = self.wait_for(nt["id"], ("done", "failed"))
        self.assertEqual((done["status"], done["outputs"]["first"], done["outputs"]["last"]), ("done", 1001, 1004))

    def test_0485_movie_from_shot_movhistory(self):
        mov = "/rdo/shows/xyz/abc/0010/movhistory/abc_0010.ingest.mp01.rdo-nwb.distort.compPlate.v2.mov"
        plan = farm._request("POST", "/api/v1/plan", {"workflow": "ltx25_alpha_gen", "params": {"input": mov}})
        self.assertEqual((plan["convert"]["slate_frame"], plan["convert"]["start"], plan["frames"]), (1000, 1001, 5))
        job = farm.submit("ltx25_alpha_gen", {"input": mov})
        self.assertEqual((job["info"]["show"], job["info"]["seq"], job["info"]["shot"]), ("xyz", "abc", "abc_0010"))
        self.assertTrue(job["info"]["output_dir_posix"].endswith("/xyz/_project_ref/_cg/comfyui/renders/abc_0010"))
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual((done["outputs"]["frames"], done["outputs"]["first"], done["outputs"]["last"]), (5, 1001, 1005))
        out = self.tmp / "shows/xyz/_project_ref/_cg/comfyui/renders/abc_0010"
        self.assertTrue((out / "abc_0010.ingest.mp01.rdo-nwb.distort.compPlate.v2.alpha_v001.1001.png").is_file())

    def test_0486_nuke_panel_model(self):
        """The Nuke panel's logic, against the real gateway (the Qt part needs Nuke)."""
        sys.path.insert(0, str(REPO / "client/nuke"))
        panel = importlib.import_module("comfyfarm_panel")
        m = panel.PanelModel()
        m.load_workflows()
        wf = m.workflow("ltx25_alpha_gen")
        self.assertEqual((m.sequence_param(wf), m.output_param(wf)), ("input", "output_path"))
        # Form values as the widgets hold them: text, blanks for "use the default". "seed" is a
        # field the workflow no longer has (a panel built before the manifest changed): it's
        # left out, not sent and refused.
        raw = {"input": f"{SEQ} 1001-1004", "output_path": "  ", "seed": " 11 "}
        text = m.destination_html(m.plan("ltx25_alpha_gen", raw))
        self.assertIn("Will write</b> 4 frames", text)
        self.assertIn("/comfyui/renders/abc_0010/</code>", text)
        job = m.submit("ltx25_alpha_gen", raw, source_node="Read3")
        finished, end = [], time.time() + 40
        while not finished and time.time() < end:
            jobs, finished = m.poll(mine=True, active_only=True)
            time.sleep(0.2)
        self.assertEqual([(j["id"], j["status"]) for j in finished], [(job["id"], "done")])   # once,
        self.assertEqual(m.poll(mine=True, active_only=True)[1], [])                          # not twice
        prompt = farm._request("GET", f"/api/v1/jobs/{job['id']}/prompt")     # as the machine that ran it built it
        self.assertEqual(prompt["5516:4832"]["inputs"]["noise_seed"], 42)
        self.assertEqual(m.sources[job["id"]], "Read3")
        row = m.row(finished[0], labels=m.labels())
        self.assertEqual(row[panel.COLUMNS.index("Status")], "done")
        self.assertRegex(row[1], r"abc_0010  v\d{3}")
        self.assertEqual(row[2:4], [wf["label"], "mock"])     # which workflow, on which machine
        mov = "/rdo/shows/xyz/abc/0010/movhistory/abc_0010.ingest.mp01.rdo-nwb.distort.compPlate.v2.mov"
        self.assertIn("first frame (1000) is a slate", m.destination_html(m.plan("ltx25_alpha_gen", {"input": mov})))
        with self.assertRaises(farm.FarmError):
            m.plan("ltx25_alpha_gen", {"input": "/rdo/shows/xyz/nope.%04d.png 1-3"})

    def test_049_conversion_cancel_and_failure(self):
        base = "/rdo/shows/xyz/.published/abc/abc_0010/plate/v1/abc_0010"
        slow = farm.submit("ltx25_alpha_gen", {"input": f"{base}.slow.v1.%04d.exr"})
        end = time.time() + 10
        while "converting" not in (farm.get(slow["id"])["stage"] or "") and time.time() < end:
            time.sleep(0.1)
        self.assertEqual(farm.get(slow["id"])["status"], "preparing")
        self.assertEqual(farm.cancel(slow["id"])["status"], "cancelled")
        time.sleep(0.5)
        self.assertFalse((self.tmp / "scratch" / slow["id"]).exists())
        broken = farm.submit("ltx25_alpha_gen", {"input": f"{base}.broken.v1.%04d.exr"})
        failed = self.wait_for(broken["id"], ("failed", "done"))
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Converting the input failed: oiiotool failed on abc_0010.broken.v1.", failed["error"])
        self.assertIn("bad EXR header", failed["error"])
        with self.assertRaisesRegex(farm.FarmError, "Missing frames"):
            farm.submit("ltx25_alpha_gen", {"input": f"{base}.plate.v1.%04d.exr 1001-1009"})

    def test_0495_chord_texture_maps(self):
        """CHORD: a folder of UDIM tiles in, five maps out as TEX-named EXRs in a versioned folder."""
        tex = "/rdo/shows/xyz/_asset/rockA/cg/textures"
        channels = ["color01", "nrm02", "bmp02", "spcr01", "metal01"]
        plan = farm._request("POST", "/api/v1/plan", {"workflow": "chord_pbr", "params": {"input": f"{tex}/src"}})
        self.assertEqual((plan["what"], plan["maps"], plan["asset"]),
                         ("15 EXRs (5 maps × 3 UDIM tiles)", channels, "rockA"))
        self.assertEqual((plan["first_file"], plan["last_file"]), ("acescg_map1_noTile_defState_color01.1001.0000.exr",
                                                                   "acescg_map1_noTile_defState_color01.1011.0000.exr"))
        self.assertTrue(plan["output_dir_posix"].endswith("/xyz/_project_ref/_cg/comfyui/renders/rockA/"
                                                          "rockA_diffuse.chord_v001"), plan["output_dir_posix"])
        job = farm.submit("chord_pbr", {"input": f"{tex}/src", "state": "damaged"})
        nxt = farm.submit("chord_pbr", {"input": f"{tex}/src/rockA_diffuse.%04d.png 1002-1011", "description": "moss"})
        self.assertEqual((job["info"]["version"], nxt["info"]["version"]), (1, 2))   # v001 is promised to job
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        out = Path(done["info"]["output_dir"])
        self.assertEqual(sorted(os.listdir(out)), sorted(f"acescg_map1_noTile_damaged_{c}.{u}.0000.exr"
                                                         for c in channels for u in (1001, 1002, 1011)))
        o = done["outputs"]
        self.assertEqual((o["files"], o["frames"], o["first"], o["last"]), (15, 3, 1001, 1011))
        self.assertTrue(o["frame_pattern_posix"].endswith("/acescg_map1_noTile_damaged_color01.%04d.0000.exr"))
        self.assertEqual(list(o["maps"]), channels)
        self.assertFalse((self.tmp / "scratch" / job["id"]).exists(), "raw map images should be cleaned up")
        prompt = farm._request("GET", f"/api/v1/jobs/{job['id']}/prompt")
        self.assertTrue(prompt["28"]["inputs"]["output_path"].endswith(f"/scratch/{job['id']}/maps"))
        self.assertEqual(prompt["28"]["inputs"]["filename_prefix"], f"_cf{job['short_id']}_bmp02")
        # Height is a bump map: 32-bit float; base colour goes color_picking -> scene_linear;
        # data maps keep their values.
        calls = [c["args"] for c in self.oiiotool_calls() if f"_cf{job['short_id']}_" in " ".join(c["args"])]
        self.assertEqual(len(calls), 15)
        bump = next(a for a in calls if "_bmp02_" in " ".join(a))
        self.assertEqual((bump[bump.index("-d") + 1], "--colorconvert" in bump), ("float", False))
        # The mock's map images are greyscale PNGs, as CHORD's height/roughness/metalness are:
        # copied into R, G, B, not asked for channels they don't have (oiiotool would write 0).
        self.assertEqual(bump[bump.index("--ch") + 1], "R=Y,G=Y,B=Y")
        self.assertFalse([c["warnings"] for c in self.oiiotool_calls()
                          if f"_cf{job['short_id']}_" in " ".join(c["args"]) and c.get("warnings")])
        color = next(a for a in calls if "_color01_" in " ".join(a))
        i = color.index("--colorconvert")   # a Nuke Read set to "default (color_picking)"
        self.assertEqual((color[i + 1:i + 3], color[color.index("-d") + 1]), (["color_picking", "scene_linear"], "half"))

        nd = self.wait_for(nxt["id"], ("done", "failed"))
        self.assertEqual(nd["status"], "done", nd.get("error"))
        self.assertEqual(sorted(os.listdir(nd["info"]["output_dir"]))[:2],
                         ["acescg_map1_noTile_defState_bmp02-moss.1002.0000.exr",
                          "acescg_map1_noTile_defState_bmp02-moss.1011.0000.exr"])
        self.assertTrue(nd["info"]["output_dir"].endswith("rockA_diffuse.chord_v002"))

        # One photo: UDIM 1001, noTile by default (the manifest's default); tile: auto makes
        # a single texture 'tile'.
        photo = farm.submit("chord_pbr", {"input": f"{tex}/photo/cliff.jpg"})
        pd = self.wait_for(photo["id"], ("done", "failed"))
        self.assertEqual(pd["status"], "done", pd.get("error"))
        self.assertIn("acescg_map1_noTile_defState_metal01.1001.0000.exr", os.listdir(pd["info"]["output_dir"]))
        self.assertTrue(pd["info"]["output_dir_posix"].endswith("/rockA/cliff.chord_v001"))
        auto = farm.submit("chord_pbr", {"input": f"{tex}/photo/cliff.jpg", "tile": "auto"})
        ad = self.wait_for(auto["id"], ("done", "failed"))
        self.assertEqual(ad["status"], "done", ad.get("error"))
        self.assertIn("acescg_map1_tile_defState_metal01.1001.0000.exr", os.listdir(ad["info"]["output_dir"]))
        self.assertTrue(ad["info"]["output_dir_posix"].endswith("/rockA/cliff.chord_v002"))

        # EXR tiles are converted like any EXR input: linear -> color_picking PNGs.
        exr = farm.submit("chord_pbr", {"input": f"{tex}/exr"})
        self.assertEqual(exr["status"], "queued")
        ed = self.wait_for(exr["id"], ("done", "failed"))
        self.assertEqual(ed["status"], "done", ed.get("error"))
        self.assertEqual(len(os.listdir(ed["info"]["output_dir"])), 10)
        conv = [c["args"] for c in self.oiiotool_calls() if "rockA_color.1002.exr" in " ".join(c["args"])][0]
        i = conv.index("--colorconvert")
        self.assertEqual(conv[i + 1:i + 3], ["scene_linear", "color_picking"])

        # Larger than the manifest's work_resolution (1024): scaled into scratch first, and
        # the workflow reads it from there. The maps are still named after the texture.
        big_input = {"input": f"{tex}/big/rockA_big.png"}
        plan = farm._request("POST", "/api/v1/plan", {"workflow": "chord_pbr", "params": big_input})
        self.assertEqual(plan["convert"], {"kind": "image", "work_resolution": 1024, "scaled": "4096×2048 → 1024×512"})
        big = farm.submit("chord_pbr", big_input)
        self.assertEqual(big["status"], "queued")
        bd = self.wait_for(big["id"], ("done", "failed"))
        self.assertEqual(bd["status"], "done", bd.get("error"))
        self.assertIn("acescg_map1_noTile_defState_color01.1001.0000.exr", os.listdir(bd["info"]["output_dir"]))
        self.assertTrue(bd["info"]["output_dir"].endswith("rockA_big.chord_v001"))
        conv = [c["args"] for c in self.oiiotool_calls() if c["args"][-1].endswith(f"/{big['id']}/rockA_big.0001.png")]
        self.assertEqual(conv[0][conv[0].index("--resize") + 1], "1024x0")
        prompt = farm._request("GET", f"/api/v1/jobs/{big['id']}/prompt")
        self.assertIn(f"/scratch/{big['id']}/", prompt["17"]["inputs"]["path"])

        with self.assertRaisesRegex(farm.FarmError, "one 1001 tile"):
            farm.submit("chord_pbr", {"input": f"{tex}/src", "tile": "tile"})
        with self.assertRaisesRegex(farm.FarmError, "2 different textures"):
            farm.submit("chord_pbr", {"input": f"{tex}/mixed"})
        with self.assertRaisesRegex(farm.FarmError, "not movies"):
            farm.submit("chord_pbr", {"input": "/rdo/shows/xyz/.published/abc/abc_0010/edit/abc_0010_ref.mov"})
        with self.assertRaisesRegex(farm.FarmError, "UV set 'uv1' won't do"):
            farm.submit("chord_pbr", {"input": f"{tex}/src", "uvset": "uv1"})

    def test_0497_texture_set_in_batches(self):
        """More tiles than the manifest's batch: one set the artist sees, batches of 4 tiles
        that run on their own into one folder, and failed batches retried in place."""
        tex = "/rdo/shows/xyz/_asset/rockA/cg/textures"
        plan = farm._request("POST", "/api/v1/plan", {"workflow": "chord_pbr", "params": {"input": f"{tex}/set"}})
        self.assertEqual((plan["batches"], plan["what"]), (3, "50 EXRs (5 maps × 10 UDIM tiles) in 3 batches of up to 4"))
        s = farm.submit("chord_pbr", {"input": f"{tex}/set"})
        self.assertEqual((s["kind"], s["info"]["set"]["batches"]), ("set", 3))
        mine = [j["id"] for j in farm.jobs(limit=500)]
        batches = farm.batches(s["id"])
        self.assertIn(s["id"], mine)
        self.assertFalse({b["id"] for b in batches} & set(mine))      # batches aren't listed as jobs
        done = self.wait_for(s["id"], ("done", "failed"), timeout=60)
        self.assertEqual((done["status"], done["stage"]), ("done", "10/10 UDIMs"), done.get("error"))
        out = Path(done["output_dir"])
        self.assertTrue(str(out).endswith("rockB_diffuse.chord_v001"))
        self.assertEqual(len(os.listdir(out)), 50)
        o = done["outputs"]
        self.assertEqual((o["files"], o["frames"], o["first"], o["last"]), (50, 10, 1001, 1010))
        self.assertTrue(o["maps"]["color01"]["pattern_posix"].endswith("/acescg_map1_noTile_defState_color01.%04d.0000.exr"))
        batches = farm.batches(s["id"])
        self.assertEqual([b["status"] for b in batches], ["done"] * 3)
        prompt = farm._request("GET", f"/api/v1/jobs/{batches[2]['id']}/prompt")
        self.assertIn(f"/scratch/{batches[2]['id']}/", prompt["17"]["inputs"]["path"])
        self.assertEqual(prompt["17"]["inputs"]["frames"], "1009-1010")
        scaled = [c["args"] for c in self.oiiotool_calls() if "/set/rockB_diffuse." in " ".join(c["args"])]
        self.assertEqual(len(scaled), 10)
        self.assertFalse((self.tmp / "scratch" / batches[0]["id"]).exists())

        # EXR tiles; the second batch can't be converted, the first still writes its maps.
        e = farm.submit("chord_pbr", {"input": f"{tex}/setexr"})
        ed = self.wait_for(e["id"], ("done", "failed"), timeout=60)
        self.assertEqual(ed["status"], "failed")
        self.assertIn("1 of 2 batches didn't finish (UDIMs 1005–1006): Converting the input failed: "
                      "rockB_color.1005.exr has no R, G, B", ed["error"])
        self.assertEqual(len(os.listdir(ed["output_dir"])), 20)
        with self.assertRaisesRegex(farm.FarmError, "one batch of a set"):
            farm.retry(farm.batches(e["id"])[1]["id"])
        fixed = self.tmp / "shows/xyz/_asset/rockA/cg/textures/setexr/rockB_color.1005.exr"
        fixed.write_bytes(exr_sized(2048, 2048))
        r = farm.retry_failed(e["id"])
        self.assertEqual(r["status"], "running")
        ed = self.wait_for(e["id"], ("done", "failed"), timeout=60)
        self.assertEqual((ed["status"], ed["outputs"]["files"]), ("done", 30), ed.get("error"))
        self.assertEqual(len(os.listdir(ed["output_dir"])), 30)               # the same folder

    def test_0498_drain_and_resume(self):
        """Drained from the dashboard (its HTTP call): no new jobs until resumed."""
        r = farm._request("POST", "/api/v1/workers/mock/drain", {"drain": True})
        self.assertEqual(r, {"worker": "mock", "drained": True})
        try:
            w = {x["name"]: x for x in farm._request("GET", "/api/v1/workers")["workers"]}["mock"]
            self.assertIsNotNone(w["drained"])
            job = farm.submit("ltx25_alpha_gen", {"input": f"{SEQ} 1001-1002"})
            time.sleep(1.5)
            self.assertEqual(farm.get(job["id"])["status"], "queued")
        finally:
            farm._request("POST", "/api/v1/workers/mock/drain", {"drain": False})
        self.assertEqual(self.wait_for(job["id"], ("done", "failed"))["status"], "done")
        with self.assertRaisesRegex(farm.FarmError, "No machine called nobody has checked in"):
            farm._request("POST", "/api/v1/workers/nobody/drain", {"drain": True})

    def test_0498b_free_vram(self):
        """Free VRAM on the Machines tab (its HTTP call): the agent asks its ComfyUI to unload,
        and the machine then says what it held before and after."""
        self.assertEqual(farm._request("POST", "/api/v1/workers/mock/free-vram"), {"worker": "mock", "asked": True})
        deadline = time.time() + 20
        while time.time() < deadline:
            w = {x["name"]: x for x in farm._request("GET", "/api/v1/workers")["workers"]}["mock"]
            if w.get("released"):
                break
            time.sleep(0.2)
        r = w["released"]
        self.assertEqual((r["ok"], r["error"]), (True, None), r)
        self.assertLess(r["after"], 2**30)
        with self.assertRaisesRegex(farm.FarmError, "No machine called nobody has checked in"):
            farm._request("POST", "/api/v1/workers/nobody/free-vram")

    def test_0499_run_times(self):
        """Finished runs are timed per workflow and GPU type, and queued jobs get an ETA and
        the machine expected to take them."""
        rt = farm._request("GET", "/api/v1/run-times")["run_times"]
        ltx = [r for r in rt if r["workflow"] == "ltx25_alpha_gen"]
        self.assertEqual(len(ltx), 1, rt)
        self.assertGreaterEqual(ltx[0]["runs"], 1)
        self.assertGreater(ltx[0]["seconds"], 0)
        farm._request("POST", "/api/v1/workers/mock/drain", {"drain": True})    # it stays queued
        try:
            job = farm.submit("ltx25_alpha_gen", {"input": f"{SEQ} 1001-1002"})
            self.assertEqual((job["next_worker"], job["eta_seconds"]), (None, None))   # no machine takes jobs
            plan = farm._request("POST", "/api/v1/plan", {"workflow": "ltx25_alpha_gen",
                                                           "params": {"input": f"{SEQ} 1001-1002"}})
            self.assertIsNone(plan["eta"])
        finally:
            farm._request("POST", "/api/v1/workers/mock/drain", {"drain": False})
        plan = farm._request("POST", "/api/v1/plan", {"workflow": "ltx25_alpha_gen",
                                                       "params": {"input": f"{SEQ} 1001-1002"}})
        self.assertEqual(plan["eta"]["workers"], ["mock"])
        q = farm.get(job["id"])
        if q["status"] == "queued":
            self.assertEqual(q["next_worker"], "mock")
        self.assertEqual(self.wait_for(job["id"], ("done", "failed"))["status"], "done")

    def test_0500_environment(self):
        """Each worker's ComfyUI environment reaches the dashboard. In standalone the agent
        doesn't know ComfyUI's folder: it says so instead of comparing."""
        deadline = time.time() + 30
        while True:
            w = {x["name"]: x for x in farm._request("GET", "/api/v1/workers")["workers"]}["mock"]
            if w.get("environment") or time.time() > deadline:
                break
            time.sleep(0.5)
        env = w["environment"]
        self.assertIsNotNone(env, w)
        self.assertEqual(env["differs"], [])
        self.assertIn("ComfyUI's folder isn't known", " ".join(env["notes"]))

    def test_05005_runner_environments(self):
        """What each machine has built of the runner environments reaches the manager (decision
        0029); with no bank (share.dir) there are no releases to list."""
        deadline = time.time() + 30
        while True:
            envs = farm._request("GET", "/api/v1/environments")
            if "mock" in envs["machines"] or time.time() > deadline:
                break
            time.sleep(0.5)
        self.assertEqual((envs["bank"], envs["releases"]), (None, []))
        self.assertIsInstance(envs["machines"].get("mock"), dict, envs)
        w = {x["name"]: x for x in farm._request("GET", "/api/v1/workers")["workers"]}["mock"]
        self.assertEqual(w["environments"], envs["machines"]["mock"])

    def test_0501_publishing_off(self):
        """Publishing is off until publish.enabled: no Publish buttons, and asking says why."""
        wf = {w["name"]: w for w in farm.workflows()}["ltx25_alpha_gen"]
        self.assertIsNone(wf["publish"])
        opts = farm.publish_options("ltx25_alpha_gen")
        self.assertEqual((opts["step"], opts["templates"]), ("ai", []))
        self.assertIn("isn't switched on", opts["why"])
        self.assertIn("isn't switched on", farm.publish_options("wan22_i2i")["why"])     # Wan publishes too (chunk 9)
        self.assertIn("texture publisher", farm.publish_options("chord_pbr")["why"])
        with self.assertRaisesRegex(farm.FarmError, "isn't switched on"):
            farm.submit("ltx25_alpha_gen", {"input": f"{SEQ} 1001-1002"}, publish={})
        done = next(j for j in farm.jobs(limit=500) if j["status"] == "done" and j["workflow"] == "ltx25_alpha_gen")
        self.assertIsNone(done["publish"])
        with self.assertRaisesRegex(farm.FarmError, "isn't switched on"):
            farm.publish(done["id"], dry_run=True)
        self.assertEqual(farm.publishes(done["id"]), [])
        # Templates can't be published either, and the overview says why.
        self.assertIn("isn't switched on", farm.publish_templates()["why"])
        with self.assertRaisesRegex(farm.FarmError, "isn't switched on"):
            farm.save_template("_global", "default", "Root {\n}\n")
        # ShotGrid lookups are off too: the fields are plain text.
        sg = farm.shotgrid_projects(refresh=True)
        self.assertEqual((sg["projects"], sg["set_up"]), ([], False))
        self.assertIn("shotgrid.enabled", sg["why"])
        self.assertEqual(farm.shotgrid_shots("bri3")["shots"], [])
        with self.assertRaisesRegex(farm.FarmError, "lookups are off"):      # Batch Submit's playlists too
            farm.shotgrid_playlist("https://rodeofx.shotgrid.autodesk.com/detail/Playlist/265342")
        r = farm.plan("ltx25_alpha_gen", {"input": SEQ}, publish={"step": "lay"})   # a row's check, with its publish
        self.assertIn("isn't switched on", r["publish"]["why"])
        base = os.environ["COMFYFARM_URL"]                                     # the dashboard and its Batch Submit tab
        with urllib.request.urlopen(f"{base}/") as r:
            self.assertIn('<script src="/batch.js">', r.read().decode())
            self.assertEqual(r.headers["Cache-Control"], "no-cache")              # a deploy shows on the next load
        with urllib.request.urlopen(f"{base}/batch.js") as r:
            self.assertEqual(r.headers["Content-Type"].split(";")[0], "text/javascript")
            self.assertEqual(r.headers["Cache-Control"], "no-cache")
            self.assertIn("function batchInit", r.read().decode())
        with urllib.request.urlopen(f"{base}/shotgrid.js") as r:                # the From ShotGrid dialog, the drop zone
            self.assertEqual(r.headers["Cache-Control"], "no-cache")
            self.assertIn("function sgButton", r.read().decode())
        with self.assertRaisesRegex(farm.FarmError, "lookups are off"):      # picked Versions need the lookups
            farm.shotgrid_versions([4865327, 4865328])
        # Batch Submit's ShotGrid query (chunk 14) too: what a show offers says why not, and
        # a query (names and picked values, never ShotGrid filters) is refused with it.
        with urllib.request.urlopen(f"{base}/api/v1/shotgrid/query/options?show=bri3") as r:
            opts = json.loads(r.read())
        self.assertEqual((opts["filters"], opts["shots"], opts["set_up"]), ([], [], False))
        self.assertIn("shotgrid.enabled", opts["why"])

        def query(body: dict):
            req = urllib.request.Request(f"{base}/api/v1/shotgrid/query", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req)
            return e.exception.code, e.exception.read().decode()
        code, why = query({"show": "bri3", "sequences": ["bpa"], "name": "mp01", "since_days": 7,
                           "filters": {"status": ["rev"], "department": "light"}})
        self.assertEqual(code, 503)
        self.assertIn("The ShotGrid query needs the manager's ShotGrid lookups", why)
        self.assertEqual(query({"show": "bri3", "shots": ["bpa;drop"]})[0], 400)       # checked before ShotGrid's asked
        self.assertEqual(query({"sequences": ["bpa"]})[0], 422)                          # no show
        # ShotGrid's right-click (an Action Menu Item) posts the selection; it's sent on to the
        # dashboard's deep link, which needs no lookups to make.
        def ami(body: bytes | None, query: str = "", ctype: str = "application/x-www-form-urlencoded"):
            class Stay(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            req = urllib.request.Request(f"{base}/api/v1/shotgrid/ami{query}", data=body, headers={"Content-Type": ctype})
            try:
                with urllib.request.build_opener(Stay).open(req) as r:
                    return r.status, r.headers.get("Location"), r.read().decode()
            except urllib.error.HTTPError as e:
                return e.code, e.headers.get("Location"), e.read().decode()
        site = "https://rodeofx.shotgrid.autodesk.com"
        status, where, _ = ami(b"user_login=ann&entity_type=Version&selected_ids=4865327&ids=4865327%2C4865328"
                               b"&server_hostname=rodeofx.shotgrid.autodesk.com&cols=code&project_id=6351")
        self.assertEqual((status, urllib.parse.unquote(where)), (303, f"/#submit?user=ann&input={site}/detail/Version/4865327"))
        status, where, _ = ami(b"entity_type=Version&selected_ids=4865327%2C4865328", "?workflow=ltx25_alpha_gen")
        self.assertEqual((status, where), (303, "/#batch?workflow=ltx25_alpha_gen&versions=4865327,4865328"))
        status, where, _ = ami(b'--b\r\nContent-Disposition: form-data; name="entity_type"\r\n\r\nPlaylist\r\n'
                               b'--b\r\nContent-Disposition: form-data; name="selected_ids"\r\n\r\n265346\r\n--b--\r\n',
                               ctype="multipart/form-data; boundary=b")
        self.assertEqual((status, urllib.parse.unquote(where)), (303, f"/#batch?playlist={site}/detail/Playlist/265346"))
        status, where, page = ami(b"entity_type=Shot&selected_ids=258776")
        self.assertEqual((status, where), (400, None))
        self.assertIn("ComfyFarm takes Versions and Playlists; that was a Shot.", page)
        self.assertIn('href="/"', page)
        status, _, page = ami(b"entity_type=Version&selected_ids=1&server_hostname=evil.example.com")
        self.assertIn("came from evil.example.com", page)
        self.assertEqual(status, 400)
        status, _, page = ami(None)                                         # opened by hand: what it's for
        self.assertEqual(status, 200)
        self.assertIn("<b>ComfyFarm</b> right-click menu", page)
        self.assertEqual(ami(None, "?entity_type=Version&selected_ids=7")[:2], (303, "/#submit?input=" + urllib.parse.quote(
            f"{site}/detail/Version/7", safe="")))
        # The menu (chunk 13c): deploy/shotgrid/menu.yaml's shows, ComfyFarm Open ▸ with every
        # workflow that has an image input (the Submit folders are off there).
        with urllib.request.urlopen(f"{base}/api/v1/shotgrid/menu") as r:
            menu = json.loads(r.read())
        self.assertEqual((menu["farm_url"], menu["folders"], menu["publish"]),
                         ("http://wrk1125l.rodeofx.com:9090", ["ComfyFarm Open"], True))
        self.assertEqual(sorted({(i["entity_type"], i["workflow"]) for i in menu["items"]}),
                         [(t, w) for t in ("Playlist", "Version")
                          for w in ("chord_pbr", "ltx25_alpha_gen", "ltx25_layout_render", "sam3_ltx_alpha", "wan22_i2i")])
        self.assertEqual((menu["sync"]["enabled"], menu["sync"]["last"]), (False, None))  # the manager doesn't sync it here
        req = urllib.request.Request(f"{base}/api/v1/shotgrid/menu/sync", data=b"", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 409)
        self.assertIn("shotgrid.menu_sync.enabled is off", e.exception.read().decode())
        ltx = next(w for w in menu["workflows"] if w["name"] == "ltx25_alpha_gen")
        self.assertEqual((ltx["submit"], ltx["publish"]), (True, False))      # it could submit; publishing is off here
        # The Submit folders, when they're on: one Version is submitted there and then. Here
        # ShotGrid's lookups are off, so the page says why, with the form to fix it.
        sel = b"entity_type=Version&selected_ids=4865327&user_login=ann"
        status, _, page = ami(sel, "?action=submit&workflow=ltx25_alpha_gen")
        self.assertEqual(status, 400)
        self.assertIn("LTX 2.5 alpha generation wasn&#x27;t submitted: ", page)
        self.assertIn("needs the manager&#x27;s ShotGrid lookups", page)
        self.assertIn('href="/#submit?workflow=ltx25_alpha_gen&amp;user=ann&amp;input=https%3A%2F%2Frodeofx', page)
        status, _, page = ami(sel, "?action=submit&workflow=ltx25_layout_render")
        self.assertIn("can&#x27;t be submitted from a Version alone: it also needs Reference frame; it needs Prompt", page)
        status, _, page = ami(sel, "?action=publish&workflow=ltx25_alpha_gen")
        self.assertIn("publishing isn&#x27;t switched on", page)
        status, where, _ = ami(None, "?action=submit&workflow=ltx25_alpha_gen&entity_type=Version&selected_ids=7")
        self.assertEqual((status, where.split("?")[0]), (303, "/#submit"))   # a link (GET) never submits
        status, where, _ = ami(b"entity_type=Version&selected_ids=11%2C12&user_login=ann", "?action=publish&workflow=ltx25_alpha_gen")
        self.assertEqual((status, where), (303, "/#batch?workflow=ltx25_alpha_gen&user=ann&publish=1&send=1&versions=11,12"))
        status, _, page = ami(sel, "?action=open&workflow=gone_wf")
        self.assertEqual(status, 404)
        self.assertIn("sync it again", page)
        self.assertEqual(len([j for j in farm.jobs(limit=500) if j["user"] == "ann" and "Version" in str(j["params"])]), 0)

    def test_05_bad_input_rejected_at_submit(self):
        with self.assertRaisesRegex(farm.FarmError, "Missing frames.*1013"):
            self.submit("1001-1014")
        with self.assertRaisesRegex(farm.FarmError,
                                    r"Unknown parameter\(s\): shorter_side\. Known: input, output_path"):
            self.submit(shorter_side=1088)     # not on the form: fixed in the workflow
        with self.assertRaisesRegex(farm.FarmError, "Can't use '.dpx' files"):
            farm.submit("ltx25_alpha_gen", {"input": "/rdo/shows/xyz/a.%04d.dpx 1-2"})

    def test_06_gateway_restart_mid_job(self):
        job = self.submit()
        self.wait_for(job["id"], ("running",))
        self.gw.terminate()
        self.gw.wait(15)
        time.sleep(5)    # the job finishes in ComfyUI while the gateway is down
        self.start_gateway()
        done = self.wait_for(job["id"], ("done", "failed"))
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["outputs"]["frames"], 12)

    def test_07_comfyui_crash_mid_job_requeues(self):
        job = self.submit()
        self.wait_for(job["id"], ("running",))
        self.stop_comfy()
        time.sleep(1.5)
        self.start_comfy()
        done = self.wait_for(job["id"], ("done", "failed"), timeout=60)
        self.assertEqual(done["status"], "done", done.get("error"))
        self.assertEqual(done["attempts"], 2)
        self.assertTrue(any("lost the job" in w for w in done["warnings"]))

    def test_08_missing_node_pack_fails_fast(self):
        self.stop_comfy()
        self.start_comfy("--drop-class", "LTXPadFrames")
        try:
            time.sleep(2.5)   # gateway notices the restart and re-reads node definitions
            worker = self.wait_worker_ready()
            self.assertIn("ltx25_alpha_gen", worker["problems"])
            job = self.submit()
            failed = self.wait_for(job["id"], ("failed", "done"))
            self.assertEqual(failed["status"], "failed")
            self.assertIn("LTXPadFrames is not installed", failed["error"])
        finally:
            self.stop_comfy()
            self.start_comfy()
            time.sleep(2.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
