"""tools/sg_ami.py, which puts "Send to ComfyFarm" into ShotGrid's right-click menu (an
Action Menu Item): a dry run changes nothing, --apply makes, changes or removes the items,
running it again doesn't double them, they're limited to shows or a group, and the
script's key is never printed; with an admin script, or the pipeline's own client
(--pipeline). Against a stand-in ShotGrid and a stand-in rdo_publish_pipeline only."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sg_ami", REPO / "tools" / "sg_ami.py")
ami = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ami)

SITE = "https://rodeofx.shotgrid.autodesk.com/"
FARM = "http://wrk1125l:9090"
URL = FARM + "/api/v1/shotgrid/ami"
KEY = "s3cr3t-k3y"


def field(kind="text", types=None):
    f = {"data_type": {"value": kind}, "properties": {}}
    if types:
        f["properties"]["valid_types"] = {"value": types}
    return f


SCHEMA = {"title": field(), "url": field("url"), "entity_type": field("list"), "selection_required": field("checkbox"),
          "folder": field(), "list_order": field("number"),
          "projects": field("multi_entity", ["Project"]), "permissions_groups": field("multi_entity", ["PermissionRuleSet"])}


class FakeShotGrid:
    def __init__(self, schema=None):
        self.schema = dict(SCHEMA if schema is None else schema)
        self.items: dict[int, dict] = {}
        self.writes: list[tuple] = []
        self.next_id = 900

    def schema_field_read(self, entity):
        assert entity == "ActionMenuItem"
        return self.schema

    def find(self, entity, filters, fields=None, **kw):
        if entity == "ActionMenuItem":
            return [dict(r, id=i) for i, r in self.items.items() if r.get("url") == filters[0][2]]
        if entity == "Project":
            known = {"bri3": 6351, "bc1": 7344}
            return [{"type": "Project", "id": known[s], "name": s.upper(), "tank_name": s} for s in filters[0][2] if s in known]
        raise AssertionError(entity)

    def find_one(self, entity, filters, fields=None):
        if entity == "PermissionRuleSet" and filters[0][0] == "display_name" and filters[0][2] == "Supervisors":
            return {"type": "PermissionRuleSet", "id": 44, "display_name": "Supervisors"}
        return None

    def _named(self, data):
        out = dict(data)
        for k in ("projects", "permissions_groups"):
            if k in out:
                out[k] = [dict(r, name=f"{r['type']} {r['id']}") for r in out[k]]
        return out

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


class Tool(unittest.TestCase):
    def setUp(self):
        self.sg = FakeShotGrid()
        self.seen: list[tuple] = []

    def run_it(self, *argv, env=None, sg=None):
        out, err = io.StringIO(), io.StringIO()
        sg = sg or self.sg

        def connect(site, name, key):
            self.seen.append((site, name, key))
            return sg
        with contextlib.redirect_stderr(err):
            code = ami.main(["--site", SITE, "--farm", FARM, *argv],
                            env={"SG_ADMIN_SCRIPT_NAME": "comfyfarm_admin", "SG_ADMIN_SCRIPT_KEY": KEY} if env is None else env,
                            ask_name=lambda p: "asked_name", ask_key=lambda p: KEY, connect=connect, out=out)
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(KEY, text)                                     # never printed
        return code, text

    def test_a_dry_run_changes_nothing(self):
        code, text = self.run_it("--show", "bri3")
        self.assertEqual(code, 0)
        self.assertEqual(self.sg.writes, [])
        self.assertIn("Would create one on Version", text)
        self.assertIn("Would create one on Playlist", text)
        self.assertIn("projects: BRI3 (Project 6351)", text)
        self.assertIn("Nothing was changed in ShotGrid. Run it again with --apply", text)
        self.assertEqual(self.seen, [(SITE, "comfyfarm_admin", KEY)])

    def test_apply_then_again(self):
        code, text = self.run_it("--show", "bri3", "--apply")
        self.assertEqual(code, 0, text)
        made = [w[1] for w in self.sg.writes]
        self.assertEqual([m["entity_type"] for m in made], ["Version", "Playlist"])
        self.assertEqual(made[0], {"title": "Send to ComfyFarm", "url": URL, "entity_type": "Version", "selection_required": True,
                                   "projects": [{"type": "Project", "id": 6351}], "permissions_groups": []})
        n = len(self.sg.writes)
        code, text = self.run_it("--show", "bri3", "--apply")              # again: nothing doubled, nothing to do
        self.assertEqual((code, len(self.sg.writes)), (0, n))
        self.assertIn("is already so", text)
        code, text = self.run_it("--show", "bri3", "--show", "bc1", "--apply")   # another show: changed in place
        self.assertEqual(self.sg.writes[n:], [("update", 901, {"projects": [{"type": "Project", "id": 6351}, {"type": "Project", "id": 7344}]}),
                                              ("update", 902, {"projects": [{"type": "Project", "id": 6351}, {"type": "Project", "id": 7344}]})])
        self.assertIn("Changing ActionMenuItem 901 on Version", text)
        self.assertEqual(len(self.sg.items), 2)
        code, text = self.run_it("--remove")                                # dry: nothing removed
        self.assertEqual(len(self.sg.items), 2)
        self.assertIn("Would delete ActionMenuItem 901", text)
        code, text = self.run_it("--remove", "--apply")
        self.assertEqual((code, self.sg.items), (0, {}))
        code, text = self.run_it("--remove", "--apply")
        self.assertIn("nothing to remove", text)

    def test_a_workflows_own_item_and_a_group(self):
        code, text = self.run_it("--group", "Supervisors", "--workflow", "ltx25_alpha_gen", "--types", "Version",
                                 "--folder", "ComfyFarm", "--apply")
        self.assertEqual(code, 0, text)
        (_, made), = self.sg.writes
        self.assertEqual((made["title"], made["url"], made["folder"], made["projects"], made["permissions_groups"]),
                         ("Send to ComfyFarm: ltx25_alpha_gen", URL + "?workflow=ltx25_alpha_gen", "ComfyFarm", [],
                          [{"type": "PermissionRuleSet", "id": 44}]))

    def test_refused(self):
        cases = [((), "Neither --show nor --group"),
                 (("--show", "nope"), "No ShotGrid project has tank_name nope"),
                 (("--group", "Nobody"), "No ShotGrid permission group is called 'Nobody'"),
                 (("--show", "bri3", "--types", "Shot"), "ComfyFarm takes Version and Playlist, not Shot"),
                 (("--show", "bri3", "--farm", "wrk1125l:9090"), "give the manager's address"),
                 (("--show", "bri3", "--workflow", "a b"), "isn't a workflow name")]
        for argv, why in cases:
            code, text = self.run_it(*argv, "--apply")
            self.assertEqual(code, 1, argv)
            self.assertIn(why, text)
        self.assertEqual(self.sg.writes, [])
        bare = FakeShotGrid({k: v for k, v in SCHEMA.items() if k not in ("projects", "permissions_groups")})
        code, text = self.run_it("--show", "bri3", "--apply", sg=bare)
        self.assertEqual(code, 1)
        self.assertIn("no projects field, so it can't be limited to bri3", text)
        code, text = self.run_it("--everywhere", "--apply", sg=bare)          # said so: everyone
        self.assertEqual((code, len(bare.writes)), (0, 2))
        self.assertNotIn("projects", bare.writes[0][1])

    def test_the_key_asked_for_and_kept_out_of_errors(self):
        code, text = self.run_it("--show", "bri3", env={"SG_ADMIN_SCRIPT_NAME": "comfyfarm_admin"})
        self.assertEqual((code, self.seen[-1]), (0, (SITE, "comfyfarm_admin", KEY)))      # asked for, not shown

        class Refusing(FakeShotGrid):
            def schema_field_read(self, entity):
                raise PermissionError(f"API read() denied for script comfyfarm_admin (key {KEY})")
        code, text = self.run_it("--show", "bri3", sg=Refusing())
        self.assertEqual(code, 1)
        self.assertIn("wouldn't show the ActionMenuItem fields", text)        # and the key isn't in it (run_it checks)



class Pipeline(unittest.TestCase):
    """--pipeline: the pipeline's own ShotGrid client (rdo_publish_pipeline, in rez or Maya)."""

    def fake(self, site=SITE):
        sg = FakeShotGrid()
        sg.base_url = site.rstrip("/")
        sg.config = types.SimpleNamespace(script_name="pipeline_admin", api_key=KEY)
        return sg

    def run_it(self, *argv, sg=None):
        out, err = io.StringIO(), io.StringIO()
        sg = sg or self.fake()
        self.sg = sg

        def nothing_asked(*a):
            raise AssertionError("no script or key is asked for with --pipeline")
        with contextlib.redirect_stderr(err):
            code = ami.main(["--farm", FARM, "--pipeline", *argv], env={}, ask_name=nothing_asked, ask_key=nothing_asked,
                            connect=nothing_asked, pipeline=lambda paths: sg, out=out)
        text = out.getvalue() + err.getvalue()
        self.assertNotIn(KEY, text)
        return code, text

    def test_through_the_pipelines_client(self):
        code, text = self.run_it("--show", "bri3")
        self.assertEqual(code, 0, text)
        self.assertIn("ShotGrid https://rodeofx.shotgrid.autodesk.com through rdo_publish_pipeline, as pipeline_admin", text)
        self.assertIn("Would create one on Version", text)
        self.assertEqual(self.sg.writes, [])
        code, text = self.run_it("--show", "bri3", "--apply", sg=self.sg)
        self.assertEqual([w[1]["entity_type"] for w in self.sg.writes], ["Version", "Playlist"])
        self.assertEqual(self.sg.writes[0][1]["projects"], [{"type": "Project", "id": 6351}])   # Rodeo's tank_name

    def test_another_site_and_errors(self):
        code, text = self.run_it("--show", "bri3", "--site", "https://rodeofx-staging.shotgrid.autodesk.com", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("rdo_publish_pipeline's ShotGrid is https://rodeofx.shotgrid.autodesk.com, not "
                      "https://rodeofx-staging.shotgrid.autodesk.com: nothing done", text)
        self.assertEqual(self.sg.writes, [])
        sg = self.fake()
        sg.find = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"Fault: bad key {KEY}"))
        code, text = self.run_it("--show", "bri3", sg=sg)
        self.assertEqual(code, 1)
        self.assertIn("ShotGrid said: RuntimeError: Fault: bad key <key>", text)   # the pipeline's key isn't shown either

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
            self.assertEqual(sys.path[0], d)
            sys.modules["rdo_publish_pipeline.manager"].ClientRegistry.get_client = lambda self: 1 / 0
            with self.assertRaisesRegex(ami.Stop, "couldn't make its ShotGrid client: ZeroDivisionError"):
                ami.pipeline_client([d])

    def test_pipeline_path_means_pipeline(self):
        self.assertTrue(ami.parse_args(["--farm", FARM, "--pipeline-path", "/rdo/x"]).pipeline)
        self.assertFalse(ami.parse_args(["--farm", FARM]).pipeline)


if __name__ == "__main__":
    unittest.main()
