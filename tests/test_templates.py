"""Publish templates as files (comfyfarm.publish.templates) and the manager's calls for
them: saving with every version kept, restoring, a file changed by hand, names and checks,
and the global templates' first name (_generic) still working."""

from __future__ import annotations

import asyncio
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
        self.assertEqual((T.key_of("_global", "default"), T.key_of("ltx", "default")), ("_global/default", "default"))
        self.assertEqual(T.split_key("_global/over", "ltx"), ("_global", "over"))
        self.assertEqual(T.split_key("default", "ltx"), ("ltx", "default"))
        self.assertEqual(T.label("_global/default"), "default (global)")
        # What the global ones were called at first is still understood.
        self.assertEqual(T.split_key("_generic/over", "ltx"), ("_global", "over"))
        self.assertEqual((T.label("_generic/over"), T.canonical("_generic/over"), T.canonical("default")),
                         ("over (global)", "_global/over", "default"))
        self.assertEqual((T.scope_of("_generic"), T.scope_of("ltx"), T.key_of("_generic", "x")), ("_global", "ltx", "_global/x"))
        for good in ("default", "alpha_over-plate.v2"):
            self.assertTrue(T.valid_name(good), good)
        for bad in ("", "../x", ".hidden", "_global", "a b", "default.nk", "x" * 65):
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
        self.assertEqual(T.TemplateStore.text_problems(with_ref, None), [])          # global: inputs not checked
        self.assertIn("reference", T.TemplateStore.text_problems(with_ref, ["input"])[0])
        with self.assertRaises(FileNotFoundError):
            self.store.read("ltx", "default", "../../etc/passwd")

    def test_generic_is_moved_to_global(self):
        old = self.root / "_generic"
        old.mkdir()
        (old / "default.nk").write_text(TEXT)
        self.store.save("_generic", "over", TEXT.encode(), "ann")         # saved with history, before the rename
        moved = self.store.move_legacy()
        self.assertEqual(len(moved), 2, moved)
        self.assertFalse(old.exists())
        self.assertEqual(self.store.names("_global"), ["default", "over"])
        self.assertEqual([h["user"] for h in self.store.history("_global", "over")], ["ann"])
        self.assertTrue(self.store.history("_global", "over")[0]["current"])
        self.assertEqual(self.store.move_legacy(), [])                    # once
        # Both there (made by hand since): neither is touched, the log says so.
        old.mkdir()
        (old / "x.nk").write_text(TEXT)
        self.assertIn("left for you to merge", self.store.move_legacy()[0])
        self.assertTrue((old / "x.nk").exists())


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
        self.pub.save_template("_global", "default", TEXT, "bob")
        over = self.pub.template_overview()
        scopes = {s["scope"]: s for s in over["scopes"]}
        self.assertEqual(list(scopes)[0], "_global")
        self.assertEqual(scopes["_global"]["label"], "Global (every workflow)")
        self.assertNotIn("chord_pbr", scopes)                       # textures aren't published from the farm
        self.assertIn("wan22_i2i", scopes)
        row = scopes["ltx25_alpha_gen"]["templates"][0]
        self.assertEqual((row["name"], row["usable"], row["saved_by"], row["versions"]), ("default", True, "ann", 1))
        self.assertEqual(scopes["_global"]["templates"][0]["key"], "_global/default")
        self.assertEqual(self.pub.options("wan22_i2i")["template"], "_global/default")
        hist = self.pub.template_history("ltx25_alpha_gen", "default")
        self.pub.save_template("ltx25_alpha_gen", "default", TEXT.replace("Clamp1", "Grade1"), "rogue")
        self.pub.restore_template("ltx25_alpha_gen", "default", hist[0]["version"], "peter")
        self.assertEqual(self.pub.template_file("ltx25_alpha_gen", "default").decode(), TEXT)
        self.assertEqual(len(self.pub.template_history("ltx25_alpha_gen", "default")), 3)
        # A Nuke still open from before the rename saves to _generic: it lands in _global.
        saved = self.pub.save_template("_generic", "over", TEXT, "carl")
        self.assertEqual((saved["scope"], saved["key"]), ("_global", "_global/over"))
        self.assertEqual(self.pub.template_history("_generic", "over")[0]["user"], "carl")
        self.assertFalse((self.tmp / "Nuke" / "_generic").exists())

    def test_the_manager_moves_generic_at_start(self):
        old = self.tmp / "Nuke" / "_generic"
        old.mkdir(parents=True)
        (old / "default.nk").write_text(TEXT)

        async def start_stop():
            await self.pub.start()
            await self.pub.stop()
        with self.assertLogs("comfyfarm", "INFO") as logs:
            asyncio.run(start_stop())
        self.assertIn("moved", "".join(logs.output))
        self.assertEqual(self.pub.options("wan22_i2i")["template"], "_global/default")

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
