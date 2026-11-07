"""Publishing through Daiquiri (comfyfarm.publish): templates and staging them, the
dispatchGraph command, and the manager's publisher against a stand-in dispatchGraph.
tests/templates/publish_default.nk is Peter's template for ltx25_alpha_gen."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.fileio.ocio import REPO
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.publish import dispatch as D
from comfyfarm.publish import nuke as N
from comfyfarm.publish.publisher import PublishError
from comfyfarm.workflows import ParamError, WorkflowRegistry

TEMPLATES = Path(__file__).resolve().parent / "templates"
TEMPLATE = TEMPLATES / "publish_default.nk"
OUT = "/rdo/shows/bri3/_project_ref/_cg/comfyui/renders/305046_0010/305046_0010.plate.alpha_v002.%04d.png"
PLATE = "/rdo/shows/bri3/.published/305046/305046_0010/plate/main/v3/305046_0010.plate.main.v3.%04d.exr"
POSIX = os.name != "nt"

# Stands in for the pipeline's dispatchGraph: records how it was called, answers like it.
FAKE_DISPATCH = textwrap.dedent("""\
    import json, os, sys
    args = sys.argv[1:]
    with open(os.environ.get("CALLS") or os.path.join(os.path.dirname(__file__), "calls.jsonl"), "a") as fh:
        fh.write(json.dumps(args) + "\\n")
    if "fail-please" in " ".join(args):
        print("ERROR    | rdo.__main__ | No entity named 305046_9999")
        sys.exit(2)
    print("INFO     | rdo.rdo_graph_core.core._engine.dispatch | Dispatched engine, #ID: 6501887.")
    print("INFO     | rdo.__main__ | jobTitle: [Nuke Render] bri3 - 305046_0010 - main - lay - update_and_render_nuke")
    """)


def nk(text: str) -> N.Template:
    return N.Template(text)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_peters_template(self):
        t = N.Template.read(TEMPLATE)
        self.assertEqual([(n.cls, n.name) for n in t.nodes],
                         [("Root", "/rdo/bank/comfy_farm/publishTemplates/Nuke/ltx25_alpha_gen/default.nk"),
                          ("Read", "Placeholder_Output_Comfyui"), ("Dot", "Dot1"), ("Clamp", "Clamp1"), ("Dot", "Dot2"),
                          ("Write", "WriteNukleus_1"), ("Viewer", "Viewer1")])
        self.assertEqual([n.name for n in t.writes()], ["WriteNukleus_1"])
        self.assertEqual(t.problems(["input"]), [])
        self.assertEqual(t.knob(t.output_reads()[0], "label"), "comfyfarm c89cadf3\nltx25_alpha_gen")

    def test_staging(self):
        dest = self.tmp / "staging" / "job" / "default.nk"
        got = N.stage(TEMPLATE, dest, job="0123456789abcdef", workflow="ltx25_alpha_gen",
                      output={"path": OUT, "first": 1001, "last": 1050}, inputs={"input": None},
                      colorspaces={"png": "color_picking", "exr": "rdo"})
        self.assertEqual(got["reads"], ["Placeholder_Output_Comfyui"])
        self.assertEqual(got["writes"], ["WriteNukleus_1"])
        t = N.Template.read(dest)
        read = t.output_reads()[0]
        for key, want in (("file", OUT), ("first", "1001"), ("last", "1050"), ("origfirst", "1001"),
                          ("origlast", "1050"), ("colorspace", "color_picking"), ("file_type", "png"),
                          ("label", "comfyfarm 01234567\nltx25_alpha_gen")):
            self.assertEqual(t.knob(read, key), want, key)
        self.assertEqual((t.knob(t.root, "first_frame"), t.knob(t.root, "last_frame"), t.knob(t.root, "frame")),
                         ("1001", "1050", "1001"))
        self.assertEqual(t.knob(t.root, "name"), str(dest))
        # Nothing but those knobs changed: the Write and the artist's nodes are as they were.
        before, after = TEMPLATE.read_text().splitlines(), dest.read_text().splitlines()
        orig = N.Template.read(TEMPLATE)
        w0, w1 = orig.find("Write")[0], t.find("Write")[0]
        self.assertEqual(before[w0.start:w0.end + 1], after[w1.start:w1.end + 1])
        self.assertEqual(before[:orig.root.start], after[:t.root.start])          # the header and layout
        changed = sorted(line.split()[0] for line in after if line not in before)
        self.assertEqual(changed, ["colorspace", "file", "label", "last", "last_frame", "name", "origlast"])
        self.assertEqual(len(after), len(before) + 1)                               # colorspace was added
        if POSIX:
            self.assertEqual(dest.stat().st_mode & 0o777, 0o644)

    def test_input_reads(self):
        text = TEMPLATE.read_text().replace("Dot {\n name Dot1", 'Read {\n inputs 0\n file /old/plate.%04d.exr\n'
                                            ' name Placeholder_Input_input\n xpos 0\n}\nDot {\n name Dot1')
        src = self.tmp / "two.nk"
        src.write_text(text)
        self.assertEqual(list(N.Template.read(src).input_reads()), ["input"])
        self.assertIn("reads input plate, which the workflow doesn't have",
                      " ".join(N.Template(text.replace("Placeholder_Input_input", "Placeholder_Input_plate"))
                               .problems(["input"])))
        dest = self.tmp / "out.nk"
        got = N.stage(src, dest, job="0123456789abcdef", workflow="ltx25_alpha_gen",
                      output={"path": OUT, "first": 1001, "last": 1050},
                      inputs={"input": {"path": PLATE, "first": 1001, "last": 1050, "kind": "exr"}},
                      colorspaces={"png": "color_picking", "exr": "rdo"})
        self.assertEqual(got["reads"], ["Placeholder_Output_Comfyui", "Placeholder_Input_input"])
        t = N.Template.read(dest)
        plate = t.input_reads()["input"][0]
        self.assertEqual((t.knob(plate, "file"), t.knob(plate, "colorspace"), t.knob(plate, "first")),
                         (PLATE, "rdo", "1001"))
        # A movie: its file, but its frame numbering is the template's.
        N.stage(src, dest, job="0123456789abcdef", workflow="w", output={"path": OUT, "first": 1001, "last": 1050},
                inputs={"input": {"path": "/x/plate.mov", "first": 1001, "last": 1050, "kind": "movie"}},
                colorspaces={})
        t = N.Template.read(dest)
        plate = t.input_reads()["input"][0]
        self.assertEqual((t.knob(plate, "file"), t.knob(plate, "first"), t.knob(plate, "file_type")),
                         ("/x/plate.mov", None, "mov"))
        # An input left empty keeps the template's file.
        got = N.stage(src, dest, job="0123456789abcdef", workflow="w", output={"path": OUT, "first": 1, "last": 2},
                      inputs={"input": None}, colorspaces={})
        self.assertIn("left empty", got["notes"][0])
        self.assertEqual(N.Template.read(dest).knob(N.Template.read(dest).input_reads()["input"][0], "file"),
                         "/old/plate.%04d.exr")

    def test_problems(self):
        no_read = TEMPLATE.read_text().replace("Placeholder_Output_Comfyui", "Read1")
        self.assertIn("no Read node named Placeholder_Output_Comfyui", N.Template(no_read).problems([])[0])
        with self.assertRaises(ValueError):
            src = self.tmp / "bad.nk"
            src.write_text(no_read)
            N.stage(src, self.tmp / "o.nk", job="x" * 16, workflow="w", output={"path": OUT}, inputs={}, colorspaces={})
        no_write = "Root {\n inputs 0\n}\nRead {\n inputs 0\n name Placeholder_Output_Comfyui\n}\n"
        self.assertEqual(N.Template(no_write).problems([]), ["it has no Write node, so nothing would be published"])

    def test_crlf_scripts_stay_crlf(self):
        src = self.tmp / "win.nk"
        src.write_bytes(TEMPLATE.read_text().replace("\n", "\r\n").encode())
        N.stage(src, self.tmp / "o.nk", job="x" * 16, workflow="w", output={"path": OUT, "first": 1, "last": 2},
                inputs={}, colorspaces={})
        data = (self.tmp / "o.nk").read_bytes()
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_values(self):
        self.assertEqual(N.nk_value("/rdo/x/a.%04d.png"), "/rdo/x/a.%04d.png")
        self.assertEqual(N.nk_value('a b [exec rm] "q" $x\nz'), '"a b \\[exec rm\\] \\"q\\" \\$x\\nz"')
        self.assertEqual(N.frames_text([1001, 1002, 1003]), "1001-1003")
        self.assertEqual(N.frames_text([1001, 1002, 1011]), "1001,1002,1011")
        self.assertEqual(N.frames_text([1050]), "1050")

    def test_template_list(self):
        folder = self.tmp / "ltx25_alpha_gen"
        for n in ("zeta.nk", "default.nk", "Alpha.nk", ".hidden.nk", "notes.txt"):
            (folder / n).parent.mkdir(parents=True, exist_ok=True)
            (folder / n).write_text("")
        (folder / ".staging").mkdir()
        self.assertEqual(N.templates(folder), ["default", "Alpha", "zeta"])
        self.assertEqual(N.templates(self.tmp / "none"), [])


class DispatchTests(unittest.TestCase):
    def test_command_quotes_everything(self):
        argv = D.command(["env", "-i", "HOME={home}", "USER={user}", "/bin/bash", "-lc"],
                         "dispatchGraph x --description {description} --entities {shot}",
                         {"description": "Auto Publish (it's $HOME; rm -rf /)", "shot": "305046_0010"})
        self.assertEqual(argv[:2], ["env", "-i"])
        self.assertTrue(argv[2].startswith("HOME=") and "{" not in argv[2])
        self.assertEqual(argv[-1], "dispatchGraph x --description 'Auto Publish (it'\"'\"'s $HOME; rm -rf /)' "
                                   "--entities 305046_0010")
        with self.assertRaises(ValueError):
            D.command([], "x {nope}", {"shot": "a"})

    def test_parse_peters_output(self):
        out = ("INFO     | rdo.rdo_graph_core.core._engine.dispatch | Dispatched engine, #ID: 6501887.\n"
               "INFO     | rdo.__main__ | jobTitle: [Nuke Render] bri3 - 305046_0010 - main - lay - update_and_render_nuke\n")
        self.assertEqual(D.parse(out, Config().publish.job_id_pattern),
                         {"tractor_id": "6501887",
                          "title": "[Nuke Render] bri3 - 305046_0010 - main - lay - update_and_render_nuke"})
        self.assertEqual(D.parse("nothing", r"#ID:\s*(\d+)"), {})

    def test_the_default_command(self):
        cmd = Config().publish.command
        self.assertIn("set_context.sh {show} && ", cmd)
        self.assertIn("rdo_rez_launcher.bash -a graph -c dispatchGraph {graph} --description", cmd)
        argv = D.command(Config().publish.shell, cmd, {"graph": "update_and_render_nuke", "show": "bri3",
                                                       "shot": "305046_0010", "step": "lay", "description": "Auto Publish",
                                                       "workfile": "/rdo/bank/x/default.nk", "frames": "1001-1084"})
        self.assertEqual(argv[-1], "source /rdo/rodeo/setup/rdo_rez_launcher/current/env/set_context.sh bri3 && "
                                   "/rdo/rodeo/setup/rdo_rez_launcher/current/rdo_rez_launcher.bash -a graph -c "
                                   "dispatchGraph update_and_render_nuke --description 'Auto Publish' --step lay "
                                   "--entities 305046_0010 --workfile /rdo/bank/x/default.nk --frameRange 1001-1084")


@unittest.skipUnless(POSIX, "publishing runs on the Linux manager (a POSIX shell)")
class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        templates = self.tmp / "publishTemplates" / "Nuke"
        (templates / "ltx25_alpha_gen").mkdir(parents=True)
        shutil.copy(TEMPLATE, templates / "ltx25_alpha_gen" / "default.nk")
        fake = self.tmp / "dispatchGraph.py"
        fake.write_text(FAKE_DISPATCH)
        self.calls = self.tmp / "calls.jsonl"
        cmd = (f"CALLS={self.calls} {sys.executable} {fake} update_and_render_nuke --description {{description}} "
               f"--step {{step}} --entities {{shot}} --workfile {{workfile}} --frameRange {{frames}} --show {{show}}")
        self.cfg = Config.model_validate({
            "workers": [], "database": str(self.tmp / "farm.db"),
            "publish": {"enabled": True, "dry_run": False, "templates": str(templates),
                        "staging": str(templates / ".staging"), "shell": ["/bin/sh", "-c"], "command": cmd,
                        "show_folder": str(self.tmp / "shows" / "{show}")}})
        (self.tmp / "shows" / "bri3").mkdir(parents=True)
        self.store = JobStore(self.tmp / "farm.db")
        self.addCleanup(self.store.close)
        self.farm = Farm(self.cfg, self.store, WorkflowRegistry(REPO / "workflows"), EventBus())
        self.pub = self.farm.publisher

    def done_job(self, **info) -> dict:
        jid = str(uuid.uuid4())
        return self.store.insert({
            "id": jid, "workflow": "ltx25_alpha_gen", "user": "jdoe", "name": "305046_0010", "status": "done",
            "priority": 0, "params": {}, "prompt": {}, "created_at": time.time(), "finished_at": time.time(),
            "info": {"show": "bri3", "shot": "305046_0010", "input": {"farm_path": PLATE, "first": 1001, "last": 1050,
                                                                       "kind": "exr"},
                     "input_posix": PLATE, "inputs": {}, **info},
            "outputs": {"frame_pattern_posix": OUT, "first": 1001, "last": 1050, "frames": 50}})

    def calls_made(self) -> list[list[str]]:
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_options(self):
        o = self.pub.options("ltx25_alpha_gen")
        self.assertEqual((o["why"], o["templates"], o["step"]), (None, ["default"], "lay"))
        self.assertIn("lay", o["steps"])
        self.assertIn("isn't published", self.pub.options("wan22_i2i")["why"])
        (Path(self.cfg.publish.templates) / "ltx25_alpha_gen" / "broken.nk").write_text("Root {\n}\n")
        self.assertIn("no Read node named", self.pub.options("ltx25_alpha_gen")["unusable"]["broken"])
        self.cfg.publish.enabled = False
        self.assertIn("isn't switched on", self.pub.options("ltx25_alpha_gen")["why"])

    def test_check(self):
        with self.assertRaises(ParamError) as e:                     # no /rdo/shows/<show>: not published to
            self.pub.check("ltx25_alpha_gen", {"show": "bc1"}, show="bri3", shot="305046_0010")
        self.assertIn("Show bc1 has no folder on disk", str(e.exception))
        r = self.pub.check("ltx25_alpha_gen", {}, show="bri3", shot="305046_0010")
        self.assertEqual(r, {"show": "bri3", "shot": "305046_0010", "step": "lay", "template": "default",
                             "description": "Auto Publish"})
        self.assertEqual(self.pub.check("ltx25_alpha_gen", {"step": "comp", "shot": "305046_0020"}, show="bri3",
                                        shot="305046_0010")["shot"], "305046_0020")
        for req, why in (({"step": "nope"}, "isn't one of the studio's"), ({"template": "other"}, "no template other"),
                         ({"shot": "a b"}, "isn't a shot code")):
            with self.assertRaises(ParamError) as e:
                self.pub.check("ltx25_alpha_gen", req, show="bri3", shot="305046_0010")
            self.assertIn(why, str(e.exception))
        with self.assertRaises(ParamError) as e:
            self.pub.check("ltx25_alpha_gen", {}, show="bri3", shot=None)
        self.assertIn("needs the shot", str(e.exception))

    def test_publish_a_finished_job(self):
        job = self.done_job()
        rec = self.pub.start_publish(job["id"], {"description": 'Looks "good"'}, "jdoe", wait=True)
        self.assertEqual(rec["state"], "dispatched", rec.get("error"))
        self.assertEqual((rec["tractor_id"], rec["title"][:13]), ("6501887", "[Nuke Render]"))
        self.assertEqual(rec["frames"], "1001-1050")
        staged = Path(rec["workfile"])
        self.assertEqual(staged, Path(self.cfg.publish.staging) / job["id"] / "default.nk")
        t = N.Template.read(staged)
        self.assertEqual(t.knob(t.output_reads()[0], "file"), OUT)
        [call] = self.calls_made()
        want = ["update_and_render_nuke", "--description", f'Looks "good" (ComfyFarm {job["id"][:8]}, jdoe)',
                "--step", "lay", "--entities", "305046_0010", "--workfile", str(staged), "--frameRange", "1001-1050",
                "--show", "bri3"]
        self.assertEqual(call, want)
        self.assertEqual(self.farm.annotate([self.store.get(job["id"])])[0]["publish"]["tractor_id"], "6501887")
        self.assertEqual(len(self.store.publishes(job["id"])), 1)

    def test_dry_run_and_failure(self):
        job = self.done_job()
        rec = self.pub.start_publish(job["id"], {}, "jdoe", dry_run=True, wait=True)
        self.assertEqual(rec["state"], "dry_run")
        self.assertEqual(self.calls_made()[-1][-1], "--dry-run")              # dispatchGraph's own dry run
        rec = self.pub.start_publish(job["id"], {"description": "fail-please"}, "jdoe", wait=True)
        self.assertEqual(rec["state"], "failed")
        self.assertIn("dispatchGraph failed (exit 2): ERROR", rec["error"])
        self.assertIn("No entity named", rec["output"])
        self.assertEqual([p["state"] for p in self.store.publishes(job["id"])], ["failed", "dry_run"])

    def test_only_finished_jobs(self):
        job = self.done_job()
        self.store.update(job["id"], status="running")
        with self.assertRaises(PublishError) as e:
            self.pub.start_publish(job["id"], {}, "jdoe")
        self.assertEqual(e.exception.status, 409)
        with self.assertRaises(PublishError):
            self.pub.start_publish("nope", {}, "jdoe")

    def test_publish_as_asked_at_submit(self):
        ok = self.done_job(publish_request={"show": "bri3", "shot": "305046_0010", "step": "comp",
                                            "template": "default", "description": "Auto Publish", "user": "ann"})
        bad = self.done_job(publish_request={"step": "comp", "template": "gone", "user": "ann"})
        dry = self.done_job(publish_request={"step": "lay", "user": "ann", "dry_run": True})
        self.done_job()                                                   # not asked for
        self.pub.publish_requested()
        self.assertEqual(self.store.publishes(dry["id"])[0]["state"], "dry_run")
        self.assertEqual(self.store.publishes(ok["id"])[0]["state"], "dispatched")
        calls = self.calls_made()
        self.assertEqual(sorted(c[4] for c in calls), ["comp", "lay"])
        self.assertEqual(sorted(c[-1] == "--dry-run" for c in calls), [False, True])
        failed = self.store.publishes(bad["id"])[0]
        self.assertEqual(failed["state"], "failed")
        self.assertIn("no template gone", failed["error"])
        self.pub.publish_requested()                                      # each is tried once
        self.assertEqual(len(self.calls_made()), 2)

    def test_staging_is_cleaned(self):
        root = Path(self.cfg.publish.staging)
        old, new, other = root / str(uuid.uuid4()), root / str(uuid.uuid4()), root / "keep-me"
        for d in (old, new, other):
            d.mkdir(parents=True)
        past = time.time() - 30 * 86400
        os.utime(old, (past, past))
        os.utime(other, (past, past))
        self.assertEqual(self.pub.clean_staging(), 1)
        self.assertEqual(sorted(p.name for p in root.iterdir()), sorted([new.name, "keep-me"]))


if __name__ == "__main__":
    unittest.main()
