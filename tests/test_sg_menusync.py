"""The manager keeping ShotGrid's right-click menu in step (comfyfarm.shotgrid.menusync):
when it syncs (started, the menu changed, daily, asked) and when it doesn't (nothing
changed, a menu with problems, no farm_url, an empty menu), what it runs, what it makes of
the tool's output; and the whole way through the real tools/sg_ami.py in a shell, with a
stand-in rdo_publish_pipeline and ShotGrid."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from comfyfarm.config import MenuSyncConfig
from comfyfarm.shotgrid import menu as M
from comfyfarm.shotgrid.menusync import MenuSync, counts, digest
from comfyfarm.workflows import WorkflowRegistry

REPO = Path(__file__).resolve().parent.parent
REGISTRY = WorkflowRegistry(REPO / "workflows")
FARM = "http://wrk1125l.rodeofx.com:9090"
DONE = "…\nDone: 8 created, 0 changed, 2 deleted. Reload ShotGrid's page to see them.\n"


def menu(shows=None, farm=FARM):
    out = M.build(list(REGISTRY.workflows.values()), lambda wf: None if wf.textures else {"step": "lay"},
                  {"raa": "all"} if shows is None else shows, farm)
    out["farm_url"] = farm or None
    return out


class Runner:
    def __init__(self):
        self.calls, self.answer = [], (0, DONE)

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        return self.answer


class When(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.menu = menu()
        self.run_ = Runner()
        self.sync = MenuSync(MenuSyncConfig(enabled=True), lambda: self.menu, REPO / "tools" / "sg_ami.py", self.dir,
                             run=self.run_)

    def check(self, **kw):
        return asyncio.run(self.sync.check(**kw))

    def test_synced_when_due(self):
        r = self.check(reason="the manager started")
        self.assertEqual((r["ok"], r["created"], r["deleted"], r["reason"]), (True, 8, 2, "the manager started"))
        argv, timeout = self.run_.calls[0]
        self.assertEqual(argv[:6], ["env", "-i", f"HOME={os.path.expanduser('~')}", argv[3], argv[4], "/bin/bash"])
        # As tried by hand on wrk1125l: the show's context, then rez by its full path (an alias in a terminal).
        self.assertEqual(argv[-1], "source /rdo/rodeo/setup/rdo_rez_launcher/current/env/set_context.sh raa && "
                         "/rdo/software/rez/rez_installations/linux/rocky8/current/bin/rez/rez env rdo_publish_pipeline -- "
                         "python " + shlex.quote(str(REPO / "tools" / "sg_ami.py"))
                         + " --pipeline --menu-json " + shlex.quote(str(self.dir / "shotgrid_menu.json")) + " --apply")
        self.assertEqual(json.loads((self.dir / "shotgrid_menu.json").read_text())["items"], self.menu["items"])
        self.assertIsNone(self.check())                                           # nothing changed: nothing run
        self.assertEqual(len(self.run_.calls), 1)
        self.menu = menu({"raa": ["ltx25_alpha_gen"]})                            # menu.yaml edited
        self.assertEqual(self.check()["reason"], "the menu changed")
        self.sync.synced_at = time.time() - 25 * 3600                             # a day on
        self.assertEqual(self.check()["reason"], "every 24 h")
        self.assertEqual(self.check(force=True)["reason"], "asked")
        self.assertEqual(len(self.run_.calls), 4)

    def test_failed_is_tried_again(self):
        self.run_.answer = (1, "ShotGrid https://rodeofx… through rdo_publish_pipeline\nNo ShotGrid project has tank_name raa.\n")
        r = self.check()
        self.assertEqual((r["ok"], r["exit"], r["why"]), (False, 1, "No ShotGrid project has tank_name raa."))
        self.run_.answer = (0, DONE)
        self.assertTrue(self.check()["ok"])                                       # the next check runs it again
        self.run_.answer = (0, "11 to create …\nNothing was changed in ShotGrid. Run it again with --apply to do it.\n")
        r = self.check(force=True)                                                # a command without --apply
        self.assertEqual((r["ok"], r["why"]), (False, "Nothing was changed in ShotGrid. Run it again with --apply to do it."))
        self.run_.answer = (124, "partial\n(stopped after 600 s)")
        self.assertIn("didn't finish in time", self.check(force=True)["why"])
        self.run_.answer = (127, "env: rez: No such file or directory")
        self.assertIn("the command wasn't found", self.check(force=True)["why"])

    def test_not_synced(self):
        for m, why in ((dict(menu(), problems=["show bri3: there's no workflow x"]), "the menu has problems"),
                       (menu(farm=""), "has no farm_url"),
                       (dict(menu(), items=[]), "the menu came out empty")):
            self.menu = m
            r = self.check(force=True)
            self.assertFalse(r["ok"])
            self.assertIn(why, r["why"])
        self.assertEqual(self.run_.calls, [])
        self.menu = menu({})                                                      # no shows: every item goes, but
        self.assertIn("no show for the pipeline's context", self.check()["why"])  # the context needs one
        self.sync.cfg = MenuSyncConfig(enabled=True, show="bri3")
        self.assertTrue(self.check()["ok"])
        self.assertIn("set_context.sh bri3 &&", self.run_.calls[-1][0][-1])

    def test_a_command_naming_something_else(self):
        self.sync.cfg = MenuSyncConfig(enabled=True, command="python {tool} --menu-json {menu} --site {site}")
        r = self.check()
        self.assertEqual(r["exit"], 2)
        self.assertIn("shotgrid.menu_sync.command uses {site}", r["why"])

    def test_counts_and_digest(self):
        self.assertEqual(counts(DONE), {"created": 8, "changed": 0, "deleted": 2})
        self.assertIsNone(counts("Nothing was changed"))
        self.assertEqual(digest(menu()), digest(json.loads(json.dumps(menu()))))
        self.assertNotEqual(digest(menu()), digest(menu(farm="https://comfyfarm.rodeofx.com")))

    def test_started_with_the_manager_only_when_on(self):
        async def go(on):
            s = MenuSync(MenuSyncConfig(enabled=on), lambda: self.menu, REPO / "tools" / "sg_ami.py", self.dir, run=self.run_)
            s.start()
            started = s.task is not None
            await s.stop()
            return started
        self.assertEqual((asyncio.run(go(False)), asyncio.run(go(True))), (False, True))


FAKE_PIPELINE = '''
import json, os, types
SITE = "https://rodeofx.shotgrid.autodesk.com"
STORE = os.environ["FAKE_SG_STORE"]

class Shotgun:
    base_url = SITE
    config = types.SimpleNamespace(script_name="rdo_publish_pipeline", api_key="k")

    def _items(self):
        return json.load(open(STORE)) if os.path.exists(STORE) else {}

    def _save(self, items):
        json.dump(items, open(STORE, "w"))

    def schema_field_read(self, entity):
        return {n: {} for n in ("title", "url", "entity_type", "folder", "list_order", "selection_required", "projects")}

    def find(self, entity, filters, fields=None, **kw):
        if entity == "Project":
            return [{"type": "Project", "id": 6449, "name": "raa", "tank_name": "raa"}]
        return [dict(v, id=int(k), type=entity) for k, v in self._items().items()]

    def create(self, entity, data):
        items = self._items()
        new = str(900 + len(items))
        items[new] = data
        self._save(items)
        return {"id": int(new)}

    def update(self, entity, eid, data):
        items = self._items()
        items[str(eid)].update(data)
        self._save(items)

    def delete(self, entity, eid):
        items = self._items()
        del items[str(eid)]
        self._save(items)

class ClientRegistry:
    def get_client(self):
        return types.SimpleNamespace(sg=Shotgun())
'''


@unittest.skipIf(os.name == "nt", "a bash login shell, as on the manager (Linux)")
class AllTheWay(unittest.TestCase):
    def test_the_real_tool_in_a_shell(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "pkg" / "rdo_publish_pipeline").mkdir(parents=True)
        (d / "pkg" / "rdo_publish_pipeline" / "__init__.py").write_bytes(b"")
        (d / "pkg" / "rdo_publish_pipeline" / "manager.py").write_text(FAKE_PIPELINE, encoding="utf-8")
        store = d / "sg.json"
        # The 2026-11-08 pair, to be removed.
        store.write_text(json.dumps({"374": {"title": "Send to ComfyFarm", "url": FARM + "/api/v1/shotgrid/ami",
                                             "entity_type": "Version"}}), encoding="utf-8")
        cfg = MenuSyncConfig(enabled=True, shell=["/bin/bash", "-c"],
                             command=f"FAKE_SG_STORE={shlex.quote(str(store))} {shlex.quote(sys.executable)} {{tool}} "
                                     f"--pipeline-path {shlex.quote(str(d / 'pkg'))} --menu-json {{menu}} --apply")
        sync = MenuSync(cfg, menu, REPO / "tools" / "sg_ami.py", d / "data")
        r = asyncio.run(sync.check(reason="the manager started"))
        self.assertTrue(r["ok"], r["output"])
        self.assertEqual((r["created"], r["changed"], r["deleted"]), (8, 0, 1))
        items = json.loads(store.read_text())
        self.assertEqual(len(items), 8)
        self.assertEqual({v["folder"] for v in items.values()}, {"ComfyFarm Open"})
        r = asyncio.run(sync.check(force=True))                                  # again: nothing to do
        self.assertEqual((r["ok"], r["created"], r["changed"], r["deleted"]), (True, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
