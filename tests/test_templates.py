"""Publish templates as files (comfyfarm.publish.templates) and the manager's calls for
them: saving with every version kept, restoring, a file changed by hand, names and checks."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from comfyfarm.config import Config
from comfyfarm.fileio.ocio import REPO
from comfyfarm.manager.db import JobStore
from comfyfarm.manager.events import EventBus
from comfyfarm.manager.farm import Farm
from comfyfarm.publish import templates as T
from comfyfarm.publish.publisher import PublishError
from comfyfarm.workflows import ParamError, WorkflowRegistry

TEMPLATE = Path(__file__).resolve().parent / "templates" / "publish_default.nk"
TEXT = TEMPLATE.read_text()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = T.TemplateStore(self.root)

    def test_keys(self):
        self.assertEqual((T.key_of("_generic", "default"), T.key_of("ltx", "default")), ("_generic/default", "default"))
        self.assertEqual(T.split_key("_generic/over", "ltx"), ("_generic", "over"))
        self.assertEqual(T.split_key("default", "ltx"), ("ltx", "default"))
        self.assertEqual(T.label("_generic/default"), "default (generic)")
        for good in ("default", "alpha_over-plate.v2"):
            self.assertTrue(T.valid_name(good), good)
        for bad in ("", "../x", ".hidden", "_generic", "a b", "default.nk", "x" * 65):
            self.assertFalse(T.valid_name(bad), bad)

    def test_save_keeps_every_version(self):
        a, b = TEXT.encode(), TEXT.replace("Clamp1", "Clamp2").encode()
        first = self.store.save("ltx", "default", a, "ann")
        time.sleep(0.002)
        second = self.store.save("ltx", "default", b, "bob")
        self.assertEqual(self.store.path("ltx", "default").read_bytes(), b)
        hist = self.store.history("ltx", "default")
        self.assertEqual([(h["user"], h["action"], h["current"]) for h in hist],
                         [("bob", "save", True), ("ann", "save", False)])
        self.assertEqual(self.store.read("ltx", "default", first["version"]), a)
        self.assertEqual(self.store.info("ltx", "default")["saved_by"], "bob")
        # A rogue save is put back; the restore is a new version, nothing is lost.
        r = self.store.restore("ltx", "default", first["version"], "peter")
        self.assertEqual(self.store.path("ltx", "default").read_bytes(), a)
        hist = self.store.history("ltx", "default")
        self.assertEqual([(h["user"], h["action"]) for h in hist], [("peter", "restore"), ("bob", "save"), ("ann", "save")])
        self.assertEqual(r["note"], f"restored {first['version']}")
        self.assertTrue(hist[0]["current"])
        self.assertNotEqual(second["version"], first["version"])
        if os.name != "nt":
            self.assertEqual(self.store.path("ltx", "default").stat().st_mode & 0o777, 0o644)

    def test_a_file_put_there_by_hand_is_kept_first(self):
        dest = self.store.path("ltx", "default")
        dest.parent.mkdir(parents=True)
        dest.write_text(TEXT)                                   # Peter's, copied in before there was a history
        self.store.save("ltx", "default", b"Root {\n}\n", "ann")
        hist = self.store.history("ltx", "default")
        self.assertEqual([(h["user"], h["action"]) for h in hist], [("ann", "save"), ("(on disk)", "found")])
        self.assertEqual(self.store.read("ltx", "default", hist[1]["version"]).decode(), TEXT)
        dest.write_text(TEXT + "\n# edited by hand\n")          # changed outside the farm
        self.assertTrue(self.store.info("ltx", "default")["changed_on_disk"])
        self.store.save("ltx", "default", TEXT.encode(), "bob")
        self.assertEqual(self.store.history("ltx", "default")[1]["note"], "changed on disk")

    def test_checks(self):
        self.assertEqual(T.TemplateStore.text_problems("", None), ["the script is empty"])
        self.assertIn("doesn't look like a Nuke script", T.TemplateStore.text_problems("hello\n", None)[0])
        self.assertEqual(T.TemplateStore.text_problems(TEXT, None), [])
        with_ref = TEXT.replace("Dot {\n name Dot1", "Read {\n inputs 0\n name Placeholder_Input_reference\n}\nDot {\n name Dot1")
        self.assertEqual(T.TemplateStore.text_problems(with_ref, None), [])          # generic: inputs not checked
        self.assertIn("reference", T.TemplateStore.text_problems(with_ref, ["input"])[0])
        with self.assertRaises(FileNotFoundError):
            self.store.read("ltx", "default", "../../etc/passwd")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = Config.model_validate({"workers": [], "database": str(self.tmp / "f.db"),
                                          "publish": {"enabled": True, "templates": str(self.tmp / "Nuke")}})
        store = JobStore(self.tmp / "f.db")
        self.addCleanup(store.close)
        self.pub = Farm(self.cfg, store, WorkflowRegistry(REPO / "workflows"), EventBus()).publisher

    def test_save_list_restore(self):
        saved = self.pub.save_template("ltx25_alpha_gen", "default", TEXT, "ann")
        self.assertEqual((saved["key"], saved["user"]), ("default", "ann"))
        self.pub.save_template("_generic", "default", TEXT, "bob")
        over = self.pub.template_overview()
        scopes = {s["scope"]: s for s in over["scopes"]}
        self.assertEqual(list(scopes)[0], "_generic")
        self.assertNotIn("chord_pbr", scopes)                       # textures aren't published from the farm
        self.assertIn("wan22_i2i", scopes)
        row = scopes["ltx25_alpha_gen"]["templates"][0]
        self.assertEqual((row["name"], row["usable"], row["saved_by"], row["versions"]), ("default", True, "ann", 1))
        self.assertEqual(scopes["_generic"]["templates"][0]["key"], "_generic/default")
        self.assertEqual(self.pub.options("wan22_i2i")["template"], "_generic/default")
        hist = self.pub.template_history("ltx25_alpha_gen", "default")
        self.pub.save_template("ltx25_alpha_gen", "default", TEXT.replace("Clamp1", "Grade1"), "rogue")
        self.pub.restore_template("ltx25_alpha_gen", "default", hist[0]["version"], "peter")
        self.assertEqual(self.pub.template_file("ltx25_alpha_gen", "default").decode(), TEXT)
        self.assertEqual(len(self.pub.template_history("ltx25_alpha_gen", "default")), 3)

    def test_refused(self):
        cases = [(("ltx25_alpha_gen", "bad name", TEXT), ParamError, "letters, digits"),
                 (("ltx25_alpha_gen", "x", "Root {\n}\n"), ParamError, "no Read node named Placeholder_Output_Comfyui"),
                 (("chord_pbr", "x", TEXT), ParamError, "texture publisher"),
                 (("nope", "x", TEXT), PublishError, "no workflow nope")]
        for args, err, why in cases:
            with self.assertRaises(err) as e:
                self.pub.save_template(*args, "ann")
            self.assertIn(why, str(e.exception))
        with self.assertRaises(PublishError) as e:
            self.pub.template_file("ltx25_alpha_gen", "missing")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(PublishError):
            self.pub.restore_template("ltx25_alpha_gen", "default", "20260101-000000-000_x", "ann")
        self.cfg.publish.enabled = False
        with self.assertRaises(ParamError) as e:
            self.pub.save_template("ltx25_alpha_gen", "default", TEXT, "ann")
        self.assertIn("isn't switched on", str(e.exception))
        self.assertIn("isn't switched on", self.pub.template_overview()["why"])


if __name__ == "__main__":
    unittest.main()
