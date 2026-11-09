"""tools/sg_ami.py, which makes ShotGrid's right-click menu match the farm's (chunk 13c): a
dry run changes nothing; --apply creates, changes and deletes Action Menu Items to match
GET /api/v1/shotgrid/menu (the old "Send to ComfyFarm" pair included); running it again
changes nothing; --remove takes them all away; the script's key is never printed; with an
admin script, or the pipeline's own client (--pipeline). Against a stand-in ShotGrid, a
stand-in rdo_publish_pipeline and the farm's real menu (built here, not fetched)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path

from comfyfarm.shotgrid import menu as M
from comfyfarm.workflows import WorkflowRegistry

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sg_ami", REPO / "tools" / "sg_ami.py")
ami = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ami)

SITE = "https://rodeofx.shotgrid.autodesk.com/"
FARM = "http://wrk1125l.rodeofx.com:9090"
AMI = FARM + "/api/v1/shotgrid/ami"
KEY = "s3cr3t-k3y"
REGISTRY = WorkflowRegistry(REPO / "workflows")


def menu(shows=None):
    pub = lambda wf: None if wf.textures else {"step": "lay"}        # noqa: E731
    return M.build(list(REGISTRY.workflows.values()), pub, {"raa": "all"} if shows is None else shows, FARM)


def field(kind="text", types=None):
    f = {"data_type": {"value": kind}, "properties": {}}
    if types:
        f["properties"]["valid_types"] = {"value": types}
    return f


# Rodeo's ActionMenuItem fields (Peter's schema_field_read, 2026-11-08)
SCHEMA = {n: field() for n in ("add_btn_behavior", "cached_display_name", "created_at", "created_by", "entity_type",
                               "folder", "id", "light_payload", "list_order", "open_in_overlay", "payload_content_type",
                               "poll_for_data_updates", "secret_token", "selection_required", "signature_algorithm",
                               "title", "updated_at", "updated_by", "url")}
SCHEMA.update(projects=field("multi_entity", ["Project"]), permissions_groups=field("multi_entity", ["PermissionRuleSet"]))


class FakeShotGrid:
    def __init__(self, schema=None):
        self.schema = dict(SCHEMA if schema is None else schema)
        # The pair made on 2026-11-08 (chunk 13), and somebody else's item.
        self.items = {374: {"title": "Send to ComfyFarm", "url": AMI, "entity_type": "Version", "selection_required": True,
                            "projects": [{"type": "Project", "id": 6449, "name": "raa"}]},
                      375: {"title": "Send to ComfyFarm", "url": AMI, "entity_type": "Playlist", "selection_required": True,
                            "projects": [{"type": "Project", "id": 6449, "name": "raa"}]},
                      12: {"title": "Rdo Sync", "url": "http://rdo-sync/ami", "entity_type": "Version"}}
        self.writes: list[tuple] = []
        self.next_id = 900

    def schema_field_read(self, entity):
        assert entity == "ActionMenuItem"
        return self.schema

    def find(self, entity, filters, fields=None, **kw):
        if entity == "ActionMenuItem":
            return [{k: v for k, v in dict(r, id=i, type=entity).items() if k in (fields or []) + ["id", "type"]}
                    for i, r in self.items.items()]
        if entity == "Project":
            known = {"raa": 6449, "bri3": 6351}
            return [{"type": "Project", "id": known[s], "name": s.upper(), "tank_name": s} for s in filters[0][2] if s in known]
        raise AssertionError(entity)

    def _named(self, data):
        return {k: ([dict(r, name=f"{r['type']} {r['id']}") for r in v] if isinstance(v, list) else v) for k, v in data.items()}

    def create(self, entity, data):
        self.writes.append(("create", data))
        self.next_id += 1
        self.items[self.next_id] = self._named(data)
        return {"type": entity, "id": self.next_id, **data}

    def update(self, entity, eid, data):
        self.writes.append(("update", eid, data))
        self.items[eid].update(self._named(data))

    def delete(self, entity, eid):
        self.writes.append(("delete", eid))
        del self.items[eid]


class Sync(unittest.TestCase):
    def setUp(self):
        self.sg = FakeShotGrid()
        self.menu = menu()

    def run_it(self, *argv, sg=None, pipeline=False):
        out, err = io.StringIO(), io.StringIO()
        sg = sg or self.sg

        def connect(site, name, key):
            self.assertEqual((site, name, key), (SITE, "comfyfarm_admin", KEY))
            return sg

        def nothing_asked(*a):
            raise AssertionError("no script or key is asked for with --pipeline")
        with contextlib.redirect_stderr(err):
            code = ami.main(["--farm", FARM, *(["--pipeline"] if pipeline else ["--site", SITE]), *argv],
                            env={} if pipeline else {"SG_ADMIN_SCRIPT_NAME": "comfyfarm_admin", "SG_ADMIN_SCRIPT_KEY": KEY},
                            ask_name=nothing_asked, ask_key=nothing_asked, connect=nothing_asked if pipeline else connect,
                            pipeline=lambda paths: sg, fetch=lambda farm: self.menu, out=out)
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(KEY, text)                                     # never printed
        return code, text

    def test_a_dry_run_changes_nothing(self):
        code, text = self.run_it()
        self.assertEqual((code, self.sg.writes), (0, []), text)
        self.assertIn("ComfyFarm Submit ▸\n  CHORD PBR maps from a diffuse texture  on Version  [raa]: would create", text)
        self.assertIn("ComfyFarm Submit + Publish ▸\n  LTX 2.5 alpha generation  on Version  [raa]: would create", text)
        self.assertIn("'Send to ComfyFarm' on Playlist: would delete ActionMenuItem 375", text)
        self.assertIn("Only in Open (a Version alone can't run them): LTX 2.5 layout to render (it also needs", text)
        self.assertIn("11 to create, 0 to change, 2 to delete, 0 as they are.\nNothing was changed in ShotGrid", text)

    def test_apply_then_again(self):
        code, text = self.run_it("--apply")
        self.assertEqual(code, 0, text)
        made = [w[1] for w in self.sg.writes if w[0] == "create"]
        self.assertEqual(len(made), 11)
        self.assertEqual(made[1], {"title": "LTX 2.5 alpha generation", "url": AMI + "?action=submit&workflow=ltx25_alpha_gen",
                                   "entity_type": "Version", "folder": "ComfyFarm Submit", "list_order": 101,
                                   "selection_required": True, "projects": [{"type": "Project", "id": 6449}]})
        self.assertEqual(sorted(w[1] for w in self.sg.writes if w[0] == "delete"), [374, 375])
        self.assertIn(12, self.sg.items)                                     # somebody else's: left alone
        self.assertIn("Done: 11 created, 0 changed, 2 deleted.", text)
        n = len(self.sg.writes)
        code, text = self.run_it("--apply")                                 # again: nothing to do
        self.assertEqual((code, len(self.sg.writes)), (0, n))
        self.assertIn("0 created, 0 changed, 0 deleted", text)
        # bri3 for LTX alpha only, and a workflow gone: changed in place, the gone one deleted
        self.menu = menu({"raa": "all", "bri3": ["ltx25_alpha_gen"]})
        self.menu["items"] = [i for i in self.menu["items"] if i["workflow"] != "wan22_i2i"]
        code, text = self.run_it("--apply")
        changed = [w for w in self.sg.writes[n:] if w[0] == "update"]
        self.assertEqual(len(changed), 4)                                    # LTX alpha's Submit, + Publish, Open ×2
        self.assertEqual(changed[0][2], {"projects": [{"type": "Project", "id": 6449}, {"type": "Project", "id": 6351}]})
        self.assertEqual(len([w for w in self.sg.writes[n:] if w[0] == "delete"]), 2)    # Wan's Open on Version, Playlist
        code, text = self.run_it("--remove")
        self.assertIn("Would delete ActionMenuItem", text)
        code, text = self.run_it("--remove", "--apply")
        self.assertEqual(list(self.sg.items), [12])

    def test_refused(self):
        self.menu = menu({"nope": "all"})
        code, text = self.run_it("--apply")
        self.assertEqual(code, 1)
        self.assertIn("No ShotGrid project has tank_name nope", text)
        self.menu = dict(menu(), problems=["show bri3: there's no workflow x"])
        code, text = self.run_it("--apply")
        self.assertIn("The farm's menu has problems", text)
        bare = FakeShotGrid({k: v for k, v in SCHEMA.items() if k != "folder"})
        self.menu = menu()
        code, text = self.run_it("--apply", sg=bare)
        self.assertEqual(code, 1)
        self.assertIn("no folder field, so it can't hold its folders", text)
        self.assertEqual(self.sg.writes + bare.writes, [])
        code, text = self.run_it("--apply")
        with self.assertRaisesRegex(ami.Stop, "give the manager's address"):
            ami.farm_base("wrk1125l:9090")

    def test_through_the_pipelines_client(self):
        sg = FakeShotGrid()
        sg.base_url, sg.config = SITE.rstrip("/"), types.SimpleNamespace(script_name="rdo_publish_pipeline", api_key=KEY)
        code, text = self.run_it(sg=sg, pipeline=True)
        self.assertEqual(code, 0, text)
        self.assertIn("ShotGrid https://rodeofx.shotgrid.autodesk.com through rdo_publish_pipeline, as rdo_publish_pipeline", text)
        code, text = self.run_it("--site", "https://rodeofx-staging.shotgrid.autodesk.com", "--apply", sg=sg, pipeline=True)
        self.assertEqual((code, sg.writes), (1, []))
        self.assertIn("not https://rodeofx-staging.shotgrid.autodesk.com: nothing done", text)
        sg.find = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"Fault: bad key {KEY}"))
        code, text = self.run_it(sg=sg, pipeline=True)
        self.assertIn("ShotGrid said: RuntimeError: Fault: bad key <key>", text)      # the pipeline's key isn't shown


class Pipeline(unittest.TestCase):
    def test_the_module_itself(self):
        def forget():
            for name in [m for m in sys.modules if m.startswith("rdo_publish_pipeline")]:
                del sys.modules[name]
        forget()
        with self.assertRaisesRegex(ami.Stop, "rdo_publish_pipeline can't be imported here.*--pipeline-path"):
            ami.pipeline_client([])
        with tempfile.TemporaryDirectory() as d:
            pkg = Path(d, "rdo_publish_pipeline")
            pkg.mkdir()
            (pkg / "__init__.py").write_bytes(b"")
            (pkg / "manager.py").write_bytes(
                b"import types\n"
                b"class Shotgun:\n    base_url = 'https://rodeofx.shotgrid.autodesk.com'\n"
                b"class ClientRegistry:\n"
                b"    def get_client(self):\n        return types.SimpleNamespace(sg=Shotgun())\n")
            self.addCleanup(forget)
            self.addCleanup(lambda: sys.path.remove(d) if d in sys.path else None)
            sg = ami.pipeline_client([d])
            self.assertEqual((type(sg).__name__, sg.base_url), ("Shotgun", "https://rodeofx.shotgrid.autodesk.com"))
            sys.modules["rdo_publish_pipeline.manager"].ClientRegistry.get_client = lambda self: 1 / 0
            with self.assertRaisesRegex(ami.Stop, "couldn't make its ShotGrid client: ZeroDivisionError"):
                ami.pipeline_client([d])

    def test_options(self):
        self.assertTrue(ami.parse_args(["--farm", FARM, "--pipeline-path", "/rdo/x"]).pipeline)
        self.assertFalse(ami.parse_args(["--farm", FARM]).pipeline)


if __name__ == "__main__":
    unittest.main()
