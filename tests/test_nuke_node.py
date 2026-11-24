"""The ComfyFarm node (client/nuke/comfyfarm_node.py): its core without Nuke, and the node
itself with a stand-in Nuke that has Groups, knobs and inputs. The real Nuke API is only
there inside Nuke; these pin what the node does with it.

    python -m unittest tests.test_nuke_node -v
"""

import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
for sub in ("client", "client/nuke"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))
farm = importlib.import_module("comfyfarm_client")

SEQ = "/rdo/shows/xyz/work/abc_0010/abc_0010.pre.v1.%04d.png"
REF = "/rdo/shows/xyz/work/abc_0010/ref.%04d.png"

LAYOUT = {   # two inputs, the primary listed second: it must still be input 0
    "name": "ltx25_layout_render", "label": "LTX layout render", "description": "Layout to render.",
    "enabled": True, "maps": None, "publish": {"step": "ai"},
    "params": [
        {"name": "reference", "type": "sequence", "label": "Reference", "required": False, "takes": "image",
         "primary": False, "default": None, "help": ""},
        {"name": "input", "type": "sequence", "label": "Layout", "required": True, "takes": "images",
         "primary": True, "default": None, "help": "The layout playblast"},
        {"name": "prompt", "type": "text", "label": "Prompt", "required": False, "default": ""},
        {"name": "steps", "type": "int", "label": "Steps", "required": False, "default": 12, "min": 1, "max": 50},
        {"name": "cfg", "type": "float", "label": "CFG", "required": False, "default": 3, "min": 0, "max": 10},
        {"name": "mode", "type": "choice", "label": "Mode", "required": False, "default": "fast",
         "choices": ["fast", "best"]},
        {"name": "loop", "type": "bool", "label": "Loop", "required": False, "default": False},
        {"name": "output_path", "type": "output_folder", "label": "Output folder", "required": False,
         "default": None, "placeholder": "Empty = /rdo/shows/<show>/renders"},
    ]}
CHORD = {"name": "chord_pbr", "label": "CHORD PBR", "enabled": True, "maps": ["basecolor", "normal"],
         "publish": None,
         "params": [{"name": "input", "type": "sequence", "label": "Texture", "required": True, "takes": "images"},
                    {"name": "output_path", "type": "output_folder", "label": "Output folder"}]}
NODE_TREE = [   # GET /api/v1/catalog?show=bri3 (decision 0054)
    {"name": "comfyui-local", "label": "ComfyUI Local", "offsite": False, "tasks": [
        {"name": "video", "label": "Video", "workflows": [
            {"name": "ltx25_layout_render", "label": "LTX layout render", "enabled": True, "allowed": True}]},
        {"name": "textures", "label": "Textures", "workflows": [
            {"name": "chord_pbr", "label": "CHORD PBR", "enabled": True, "allowed": False,
             "reason": "chord_pbr can't run for show bri3: Not cleared. (Policy rule bri3-alpha-only.)"},
            {"name": "off", "label": "Off", "enabled": False, "allowed": True}]}]},
    {"name": "fal", "label": "fal.ai", "offsite": True, "tasks": [
        {"name": "image", "label": "Image", "workflows": [
            {"name": "nano", "label": "Nano Banana 2 edit", "enabled": True, "allowed": True}]}]}]
PUB_OPTIONS = {"steps": ["ai", "comp"], "step": "ai", "templates": ["ltx25_layout_render/default", "_global/default"],
               "template": "ltx25_layout_render/default", "labels": {}, "description": "Auto Publish"}


# ---------------------------------------------------------------- a stand-in Nuke
class Knob:
    def __init__(self, name, label="", value=""):
        self._name, self.label, self._v = name, label, value
        self.flags, self.visible, self.tooltip, self.range = set(), True, "", None

    def name(self):
        return self._name

    def value(self):
        return self._v

    def getValue(self):  # noqa: N802 (Nuke naming)
        return self._v

    def setValue(self, v):  # noqa: N802
        self._v = v

    def setFlag(self, f):  # noqa: N802
        self.flags.add(f)

    def setTooltip(self, t):  # noqa: N802
        self.tooltip = t

    def setVisible(self, v):  # noqa: N802
        self.visible = v

    def setRange(self, a, b):  # noqa: N802
        self.range = (a, b)


class Enum(Knob):
    def __init__(self, name, label, values):
        super().__init__(name, label)
        self.values, self._i = list(values), 0

    def value(self):
        return self.values[self._i] if self.values else ""

    def getValue(self):  # noqa: N802
        return float(self._i)

    def setValue(self, v):  # noqa: N802
        self._i = self.values.index(v) if isinstance(v, str) else int(v)

    def setValues(self, values):  # noqa: N802
        self.values = list(values)
        self._i = min(self._i, len(self.values) - 1)


class Node:
    def __init__(self, nk, cls, name, parent=None, **knobs):
        self.nk, self._cls, self._name, self.parent = nk, cls, name, parent
        self._knobs = {}
        for k in ("label", "tile_color", "knobChanged", "which", "on_error", "file", "first", "last", "origfirst",
                  "origlast"):
            self._knobs[k] = Knob(k, value=0 if k in ("which", "tile_color") else "")
        for k, v in knobs.items():
            self._knobs[k] = Knob(k, value=v)
        self._inputs, self.children, self.selected, self.x, self.y = {}, [], False, 0, 0
        self.panel_open = False

    def Class(self):  # noqa: N802
        return self._cls

    def name(self):
        return self._name

    def setName(self, n):  # noqa: N802
        self._name = n

    def fullName(self):  # noqa: N802
        return self._name if self.parent is None or self.parent is self.nk.root_node else \
            self.parent.fullName() + "." + self._name

    def knobs(self):
        return self._knobs

    def __getitem__(self, k):
        return self._knobs[k]

    def addKnob(self, k):  # noqa: N802
        assert k.name() not in self._knobs, k.name()
        self._knobs[k.name()] = k

    def removeKnob(self, k):  # noqa: N802
        del self._knobs[k.name()]

    def input(self, i):
        return self._inputs.get(i)

    def setInput(self, i, n):  # noqa: N802
        if n is None:
            self._inputs.pop(i, None)
        else:
            self._inputs[i] = n

    def inputs(self):
        if self._cls == "Group":
            return len([c for c in self.children if c.Class() == "Input"])
        return max(self._inputs) + 1 if self._inputs else 0

    def xpos(self):
        return self.x

    def ypos(self):
        return self.y

    def setXYpos(self, x, y):  # noqa: N802
        self.x, self.y = x, y

    def setSelected(self, v):  # noqa: N802
        self.selected = v

    def showControlPanel(self):  # noqa: N802
        self.panel_open = True

    def begin(self):
        self.nk.stack.append(self)

    def end(self):
        self.nk.stack.pop()

    def __enter__(self):
        self.begin()
        return self

    def __exit__(self, *a):
        self.end()


def make_nuke():
    n = types.ModuleType("nuke")
    n.STARTLINE, n.INVISIBLE = 1, 2
    n.messages = []
    n.root_node = Node(n, "Root", "root")
    n.stack = [n.root_node]
    n.current = 1050
    n.thisGroup = lambda: n.stack[-1]
    root = types.SimpleNamespace(firstFrame=lambda: 1001, lastFrame=lambda: 1100)
    n.root = lambda: root
    n.frame = lambda: n.current

    def all_nodes(cls=None, recurseGroups=False):  # noqa: N803 (Nuke naming)
        out, todo = [], list(n.stack[-1].children)
        while todo:
            x = todo.pop(0)
            if cls is None or x.Class() == cls:
                out.append(x)
            if recurseGroups and x.Class() == "Group":
                todo.extend(x.children)
        return out
    n.allNodes = all_nodes
    n.selectedNodes = lambda: [x for x in n.root_node.children if x.selected]

    def to_node(name):
        here = n.stack[-1]
        for part in name.split("."):
            here = next((c for c in here.children if c.name() == part), None)
            if here is None:
                return None
        return here
    n.toNode = to_node
    n.filename = lambda node: node["file"].value()
    n.message = n.messages.append
    n.asked, n.answer = [], True

    def ask(text):
        n.asked.append(text)
        return n.answer
    n.ask = ask
    n.delete = lambda node: node.parent.children.remove(node)
    n.executeInMainThread = lambda fn, args=(): fn(*args)

    def maker(cls):
        def make(name=None, **kw):
            parent = n.stack[-1]
            node = Node(n, cls, name or f"{cls}{len(parent.children) + 1}", parent, **kw)
            parent.children.append(node)
            return node
        return make
    n.nodes = types.SimpleNamespace(**{c: maker(c) for c in
                                       ("Group", "Input", "Output", "Read", "Write", "Switch", "Constant", "Dot",
                                        "Blur")})
    n.Tab_Knob = lambda name, label: Knob(name, label)
    n.Text_Knob = lambda name, label="", text="": Knob(name, label, text)
    n.String_Knob = lambda name, label="", value="": Knob(name, label, value)
    n.Multiline_Eval_String_Knob = lambda name, label="": Knob(name, label, "")
    n.File_Knob = lambda name, label="": Knob(name, label, "")
    n.Boolean_Knob = lambda name, label="": Knob(name, label, False)
    n.Int_Knob = lambda name, label="": Knob(name, label, 0)
    n.Double_Knob = lambda name, label="": Knob(name, label, 0.0)
    n.PyScript_Knob = lambda name, label, cmd: Knob(name, label, cmd)
    n.Enumeration_Knob = Enum
    return n


class FakeFarm:
    def __init__(self):
        self.submitted, self.published, self.cancelled, self.jobs, self.n = [], [], [], {}, 0
        self.yes_needed = None
        self.workflow_list = [LAYOUT, CHORD]
        self.tree, self.catalog_shows = NODE_TREE, []

    def publish_options(self, name):
        return dict(PUB_OPTIONS, workflow=name)

    def submit(self, workflow, params, name=None, priority=0, show=None, shot=None, publish=None,
               confirm_cost=None):
        if self.yes_needed and confirm_cost is None:     # it costs the policy's budgets.confirm_above or more
            raise farm.FarmError("This job costs about $6.00 at fal.ai, and a job of $5.00 or more is submitted once "
                                 "you say yes", self.yes_needed)
        self.n += 1
        jid = f"job{self.n:05d}-xxxx"
        job = {"id": jid, "short_id": jid[:8], "status": "queued", "workflow": workflow,
               "info": {"output_dir_posix": f"/rdo/out/v{self.n:03d}"}}
        self.submitted.append({"workflow": workflow, "params": params, "name": name, "publish": publish})
        self.jobs[jid] = job
        return job

    def finish(self, jid, v):
        self.jobs[jid].update(status="done", finished_at=1000.0, outputs={
            "frame_pattern": f"/rdo/out/abc_0010.render_v{v:03d}.%04d.png",
            "frame_pattern_posix": f"/rdo/out/abc_0010.render_v{v:03d}.%04d.png", "first": 1001, "last": 1010})
        return self.jobs[jid]

    def get(self, jid):
        if jid not in self.jobs:
            raise farm.FarmError("no such job")
        return self.jobs[jid]

    def cancel(self, jid):
        self.cancelled.append(jid)
        self.jobs[jid]["status"] = "cancelled"
        return self.jobs[jid]

    def publish(self, jid, **kw):
        self.published.append((jid, kw))
        self.jobs[jid]["publish"] = {"state": "queued", "show": kw.get("show"), "shot": kw.get("shot"),
                                     "step": kw.get("step")}
        return self.jobs[jid]["publish"]

    def workflows(self):
        return self.workflow_list

    def catalog(self, show=None, hidden=False):
        self.catalog_shows.append(show)
        if self.tree is None:
            raise farm.FarmError("404: Not Found")      # an older farm
        return {"show": show, "providers": self.tree, "errors": []}

    def retention(self):
        return {"enabled": True, "keep_days": 3, "at": "03:00"}


class Base(unittest.TestCase):
    def load(self):
        self.nuke = make_nuke()
        self.farm = FakeFarm()
        patches = [mock.patch.dict(sys.modules, {"nuke": self.nuke}), mock.patch.object(farm, "POSIX", True)]
        for name in ("publish_options", "submit", "get", "cancel", "publish", "workflows", "retention", "catalog"):
            patches.append(mock.patch.object(farm, name, getattr(self.farm, name)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for mod in ("comfyfarm_nuke", "comfyfarm_panel", "comfyfarm_node"):
            sys.modules.pop(mod, None)
            self.addCleanup(sys.modules.pop, mod, None)
        node_mod = importlib.import_module("comfyfarm_node")
        self.tracked = []

        def now(work, done):           # the farm answers at once, in the main thread
            try:
                result, error = work(), None
            except Exception as e:  # noqa: BLE001
                result, error = None, e
            done(result, error)
        for p in (mock.patch.object(node_mod, "_in_background", now),
                  mock.patch.object(node_mod, "track", lambda ids: self.tracked.extend(ids))):
            p.start()
            self.addCleanup(p.stop)
        return node_mod

    def read(self, name, path, first=1001, last=1010, y=0):
        r = self.nuke.nodes.Read(name=name, file=path)
        r["first"].setValue(first)
        r["last"].setValue(last)
        r.y = y
        return r

    def inside(self, node, name):
        return next(c for c in node.children if c.name() == name)

    def groups(self):
        return [c for c in self.nuke.root_node.children if c.Class() == "Group"]


class FakeMenu:
    """Nuke's menus: commands, separators and submenus, in order."""

    def __init__(self, name=""):
        self.name, self.items = name, []

    def clearMenu(self):  # noqa: N802 (Nuke naming)
        self.items = []

    def addCommand(self, label, fn=None):  # noqa: N802
        self.items.append((label, fn))

    def addSeparator(self):  # noqa: N802
        self.items.append(("-", None))

    def addMenu(self, label):  # noqa: N802
        sub = FakeMenu(label)
        self.items.append((label, sub))
        return sub

    def shape(self):
        return [(label, x.shape()) if isinstance(x, FakeMenu) else label for label, x in self.items]

    def command(self, *path):
        menu = self
        for label in path[:-1]:
            menu = next(x for name, x in menu.items if name == label)
        return next(x for name, x in menu.items if name == path[-1])


# ---------------------------------------------------------------- the core
class Core(Base):
    def setUp(self):
        self.m = self.load()

    def test_primary_input_is_input_zero(self):
        self.assertEqual([p["name"] for p in self.m.image_params(LAYOUT)], ["input", "reference"])

    def test_knob_specs(self):
        specs = {s["param"]: s for s in self.m.knob_specs(LAYOUT)}
        self.assertEqual({k: s["kind"] for k, s in specs.items()},
                         {"reference": "image", "input": "image", "prompt": "text", "steps": "int", "cfg": "float",
                          "mode": "choice", "loop": "bool", "output_path": "folder"})
        self.assertEqual(specs["input"]["knob"], "p_input_path")
        self.assertEqual(specs["steps"]["knob"], "p_steps")
        self.assertTrue(specs["reference"]["takes_one"])
        self.assertIn("current frame", specs["reference"]["tooltip"])
        self.assertIn("The layout playblast", specs["input"]["tooltip"])
        self.assertIn("/rdo/shows/<show>/renders", specs["output_path"]["tooltip"])
        self.assertEqual(specs["mode"]["choices"], ["fast", "best"])
        self.assertEqual((specs["cfg"]["min"], specs["cfg"]["max"]), (0, 10))

    def test_knob_values_from_the_form(self):
        specs = {s["param"]: s for s in self.m.knob_specs(LAYOUT)}
        self.assertEqual(self.m.knob_value(specs["steps"], "20"), 20)
        self.assertEqual(self.m.knob_value(specs["steps"], "twenty"), 12)       # unusable: the default
        self.assertEqual(self.m.knob_value(specs["cfg"], None), 3.0)
        self.assertEqual(self.m.knob_value(specs["mode"], "nope"), "fast")
        self.assertIs(self.m.knob_value(specs["loop"], "true"), True)
        self.assertEqual(self.m.knob_value(specs["output_path"], None), "")

    def test_versions(self):
        job = {"id": "abcdef12-3", "status": "done", "finished_at": 5.0,
               "outputs": {"frame_pattern_posix": "/o/x.render_v004.%04d.png", "first": 1001, "last": 1003}}
        recs = self.m.merge_record([], {"id": "abcdef12-3", "status": "running", "progress": 0.5})
        self.assertEqual(self.m.version_labels(recs), ["#1  abcdef12  running"])
        recs = self.m.merge_record(recs, job)
        self.assertEqual(len(recs), 1)
        self.assertEqual(self.m.version_labels(recs), ["v004  abcdef12  done"])
        self.assertEqual(self.m.shown_frames(recs[0]),
                         {"pattern": "/o/x.render_v004.%04d.png", "first": 1001, "last": 1003, "gaps": False})
        self.assertEqual(self.m.version_labels([]), [self.m.NO_VERSION])

    def test_texture_maps(self):
        job = {"id": "t1234567", "status": "done", "outputs": {"maps": {
            "basecolor": {"pattern_posix": "/o/a.basecolor_v001.<UDIM>.exr", "pattern": "x", "first": 1001,
                          "last": 1012, "files": 3},
            "normal": {"pattern": "/o/a.normal_v001.<UDIM>.exr", "first": 1001, "last": 1001, "files": 1}}}}
        rec = self.m.job_record(job, posix=True)
        self.assertEqual(rec["version"], "v001")
        self.assertEqual(self.m.shown_frames(rec, "normal")["pattern"], "/o/a.normal_v001.<UDIM>.exr")
        self.assertTrue(self.m.shown_frames(rec)["gaps"])          # 12 tiles' frames, 3 files: black between

    def test_status_and_expiry(self):
        rec = {"id": "x", "short_id": "abc12345", "status": "done", "finished_at": 0.0, "version": "v002",
               "output_dir": "/o"}
        on = {"enabled": True, "keep_days": 3}
        self.assertIn("Frames kept until", self.m.status_text(rec, on, now=3600))
        self.assertIn("Expired", self.m.status_text(rec, on, now=4 * 86400))
        self.assertNotIn("kept until", self.m.status_text(rec, {"enabled": False}, now=0))
        published = dict(rec, publish={"state": "dispatched", "shot": "abc_0010"})
        self.assertNotIn("Expired", self.m.status_text(published, on, now=4 * 86400))
        self.assertEqual(self.m.status_text(None), "Not submitted yet.")

    def test_schema_key_sees_a_changed_workflow(self):
        changed = json.loads(json.dumps(LAYOUT))
        changed["params"][3]["default"] = 20
        self.assertNotEqual(self.m.schema_key(LAYOUT), self.m.schema_key(changed))
        relabelled = dict(LAYOUT, description="other words")
        self.assertEqual(self.m.schema_key(LAYOUT), self.m.schema_key(relabelled))


# ---------------------------------------------------------------- the node
class TheNode(Base):
    def setUp(self):
        self.m = self.load()

    def test_from_the_panel(self):
        plate = self.read("Plate", SEQ, 1001, 1010)
        self.read("Ref", REF, 1, 100, y=50)
        raw = {"input": f"{SEQ} 1001-1010", "reference": f"{REF} 1020", "prompt": "a forest", "steps": "20",
               "cfg": "4.5", "mode": "best", "loop": True, "output_path": ""}
        pub = {"show": "xyz", "shot": "abc_0010", "step": "comp", "template": "_global/default",
               "description": "", "dry_run": True}
        node = self.m.create_node(LAYOUT, raw=raw, job_name="mine", publish_on=True, pub_values=pub)
        self.assertEqual(node.Class(), "Group")
        self.assertEqual(node.name(), "ComfyFarm_ltx25_layout_render_1")
        self.assertIs(node.input(0), plate)                    # the panel's input, by its path
        self.assertEqual(node["p_input_path"].value(), "")     # the Read says the same: follow it
        self.assertEqual(node["p_reference_path"].value(), f"{REF} 1020")   # says more than its Read
        self.assertEqual(node.input(1).name(), "Ref")
        self.assertEqual((node["p_prompt"].value(), node["p_steps"].value(), node["p_cfg"].value()),
                         ("a forest", 20, 4.5))
        self.assertEqual((node["p_mode"].value(), node["p_loop"].value()), ("best", True))
        self.assertEqual(node["p_cfg"].range, (0.0, 10.0))
        self.assertEqual(node["cf_job_name"].value(), "mine")
        self.assertTrue(node["cf_publish"].value())
        self.assertEqual((node["cf_pub_step"].value(), node["cf_pub_shot"].value()), ("comp", "abc_0010"))
        self.assertEqual(self.m.publish_values(node)["template"], "_global/default")
        self.assertTrue(node["cf_pub_show"].visible)
        self.assertEqual([c.name() for c in node.children if c.Class() == "Input"], ["input", "reference"])
        self.assertEqual(self.inside(node, "Show")["which"].value(), 0)    # passes the layout through for now
        self.assertTrue(node.panel_open and node.selected)
        self.assertIn("comfyfarm_node.knob_changed", node["knobChanged"].value())

    def test_from_the_tab_menu(self):
        a = self.read("A", SEQ, y=0)
        b = self.read("B", REF, y=40)
        b.selected = a.selected = True
        with mock.patch.object(self.m, "_workflows", [LAYOUT]):
            node = self.m.create_from_menu("ltx25_layout_render")
        self.assertIs(node.input(0), a)                        # top of the DAG to the primary
        self.assertIs(node.input(1), b)
        self.assertEqual(node["p_steps"].value(), 12)          # the farm's defaults
        self.assertFalse(node["cf_publish"].value())
        self.assertFalse(node["cf_pub_show"].visible)
        self.assertEqual((node.x, node.y), (a.x, a.y + 90))

    def test_panel_and_node_import_each_other_either_way(self):
        for mod in ("comfyfarm_panel", "comfyfarm_node"):
            sys.modules.pop(mod, None)
        panel = importlib.import_module("comfyfarm_panel")     # Nuke opens the panel first
        self.assertTrue(hasattr(panel.farm_node, "create_node"))

    def test_the_tab_menu_by_provider_then_task(self):
        """ComfyFarm > ComfyUI Local > Video > LTX layout render, the farm's menus for the
        session's show (decision 0054); Reload first; disabled ones left out."""
        menu = FakeMenu("ComfyFarm")
        with mock.patch.dict("os.environ", {"RDO_CURRENT_SHOW": "bri3"}):
            self.m.register_menu(menu)
        self.assertEqual(self.farm.catalog_shows, ["bri3"])
        self.assertEqual(menu.shape(), [
            "Reload workflows", "-",
            ("ComfyUI Local", [("Video", ["LTX layout render"]),
                               ("Textures", ["CHORD PBR  (not on this show)"])]),
            ("fal.ai  (off-site)", [("Image", ["Nano Banana 2 edit"])])])
        menu.command("ComfyUI Local", "Video", "LTX layout render")()
        self.assertEqual(self.groups()[-1]["cf_name"].value(), "ltx25_layout_render")
        menu.command("Reload workflows")()
        self.assertEqual(len(self.farm.catalog_shows), 2)

    def test_a_workflow_the_show_may_not_use_says_why(self):
        self.m.register_menu(FakeMenu("ComfyFarm"))
        made = len(self.groups())
        self.assertIsNone(self.m.create_from_menu("chord_pbr"))
        self.assertEqual(self.nuke.messages[-1],
                         "ComfyFarm: chord_pbr can't run for show bri3: Not cleared. (Policy rule bri3-alpha-only.)")
        self.assertEqual(len(self.groups()), made)

    def test_an_older_farm_lists_the_workflows_plainly(self):
        self.farm.tree = None
        menu = FakeMenu("ComfyFarm")
        self.m.register_menu(menu)
        self.assertEqual(menu.shape(), ["Reload workflows", "-", "LTX layout render", "CHORD PBR"])
        self.assertEqual(self.m.menu_entries(None, [LAYOUT, dict(CHORD, enabled=False)]),
                         [(("LTX layout render",), "ltx25_layout_render", None)])

    def test_a_farm_not_reached(self):
        menu = FakeMenu("ComfyFarm")
        with mock.patch.object(farm, "workflows", side_effect=farm.FarmError("down")):
            self.m.register_menu(menu)
        self.assertEqual(menu.shape(), ["Reload workflows (farm not reached)"])

    def test_the_node_label_names_its_provider(self):
        self.assertEqual(self.m.node_label("wan22_i2i", None), "wan22_i2i")
        self.assertEqual(self.m.node_label("wan22_i2i", None, "ComfyUI Local"), "ComfyUI Local\nwan22_i2i")
        self.assertEqual(self.m.node_label("wan22_i2i", {"version": "v003", "status": "done"}, "fal.ai"),
                         "fal.ai\nwan22_i2i\nv003 done")
        self.m.register_menu(FakeMenu("ComfyFarm"))                 # the menus, as at Nuke's start
        node = self.m.create_node(LAYOUT, connect={"input": self.read("Plate", SEQ)})
        self.assertEqual(node["label"].value(), "ComfyUI Local\nltx25_layout_render")
        self.assertEqual(node["cf_provider"].value(), "ComfyUI Local")
        mine = self.m.create_node(LAYOUT, provider="")              # the panel says
        self.assertEqual(mine["label"].value(), "ltx25_layout_render")
        self.m.submit(node)
        self.assertEqual(node["label"].value(), "ComfyUI Local\nltx25_layout_render\njob00001 queued")
        self.m._catalog = None                                    # the menus didn't load: the node keeps its own
        self.m.rebuild(node)
        self.assertEqual(node["cf_provider"].value(), "ComfyUI Local")
        self.assertTrue(node["label"].value().startswith("ComfyUI Local\n"))

    def test_submit_versions_and_result(self):
        plate = self.read("Plate", SEQ, 1001, 1010)
        dot = self.nuke.nodes.Dot()
        dot.setInput(0, plate)
        ref = self.read("Ref", REF, 1, 100)
        node = self.m.create_node(LAYOUT, connect={"input": dot, "reference": ref})
        node["p_prompt"].setValue("rain")
        self.m.submit(node)
        sent = self.farm.submitted[-1]
        self.assertEqual(sent["params"]["input"], f"{SEQ} 1001-1010")     # through the Dot
        self.assertEqual(sent["params"]["reference"], f"{REF} 1050")      # one image: the current frame
        self.assertEqual(sent["params"]["prompt"], "rain")
        self.assertEqual(sent["params"]["steps"], "12")
        self.assertIsNone(sent["publish"])
        self.assertEqual(self.tracked, ["job00001-xxxx"])
        self.assertIn("queued", node["label"].value())
        self.assertEqual(self.inside(node, "Show")["which"].value(), 0)

        self.m.apply_jobs([self.farm.finish("job00001-xxxx", 1)])
        read, switch = self.inside(node, "Result"), self.inside(node, "Show")
        self.assertEqual(read["file"].value(), "/rdo/out/abc_0010.render_v001.%04d.png")
        self.assertEqual((read["first"].value(), read["last"].value()), (1001, 1010))
        self.assertEqual(switch["which"].value(), 1)               # the node is the result now
        self.assertEqual(node["tile_color"].value(), self.m.STATUS_TILES["done"])

        self.m.submit(node)                                          # a second version
        self.m.apply_jobs([self.farm.finish("job00002-xxxx", 2)])
        self.assertEqual(node["cf_version"].values,
                         ["v001  job00001  done", "v002  job00002  done"])
        self.assertTrue(read["file"].value().endswith("render_v002.%04d.png"))
        node["cf_version"].setValue(0)                               # pick v001 again
        self.m.knob_changed(node, node["cf_version"])
        self.assertTrue(read["file"].value().endswith("render_v001.%04d.png"))

    def test_a_costly_job_is_asked_about_first(self):
        """A job of the policy's budgets.confirm_above or more (decision 0060): the farm answers
        with its estimate, the artist is asked, and it is sent again with the yes."""
        self.farm.yes_needed = {"amount": 6.0, "text": "About $6.00 at fal.ai.", "confirm": True}
        plate = self.read("Plate", SEQ, 1001, 1010)
        node = self.m.create_node(LAYOUT, connect={"input": plate})
        node["p_prompt"].setValue("rain")
        self.nuke.answer = False
        self.m.submit(node)
        self.assertEqual(self.nuke.asked, ["ComfyFarm: About $6.00 at fal.ai.\n\nSubmit it for about $6.00?"])
        self.assertEqual(self.farm.submitted, [])
        self.assertIn("Not submitted: it wasn't confirmed.", node["cf_note"].value())
        self.nuke.answer = True
        self.m.submit(node)
        self.assertEqual(len(self.farm.submitted), 1)
        self.assertEqual(self.tracked, ["job00001-xxxx"])

    def test_submit_refuses_what_the_farm_cant_read(self):
        blur = self.nuke.nodes.Blur()
        node = self.m.create_node(LAYOUT, connect={"input": blur})
        self.m.submit(node)
        self.assertEqual(self.farm.submitted, [])
        self.assertIn("needs a Read or Write", self.nuke.messages[-1])
        node.setInput(0, None)
        self.m.submit(node)
        self.assertIn("Layout", self.nuke.messages[-1])                # required and missing
        node["p_input_path"].setValue(f"{SEQ} 1001-1002")              # a typed path does
        self.m.submit(node)
        self.assertEqual(self.farm.submitted[-1]["params"]["input"], f"{SEQ} 1001-1002")

    def test_publish_when_done_and_now(self):
        node = self.m.create_node(LAYOUT, raw={"input": SEQ}, publish_on=True,
                                  pub_values={"show": "xyz", "shot": "abc_0010", "step": "ai"})
        self.m.submit(node)
        self.assertEqual(self.farm.submitted[-1]["publish"]["shot"], "abc_0010")
        self.assertEqual(self.farm.submitted[-1]["publish"]["template"], "ltx25_layout_render/default")
        self.m.publish_version(node)                                    # not done yet
        self.assertEqual(self.farm.published, [])
        self.m.apply_jobs([self.farm.finish("job00001-xxxx", 1)])
        node["cf_pub_step"].setValue("comp")
        self.m.publish_version(node)
        jid, kw = self.farm.published[-1]
        self.assertEqual((jid, kw["step"], kw["shot"]), ("job00001-xxxx", "comp", "abc_0010"))
        self.assertIn("publish queued", node["cf_status"].value())

    def test_cancel(self):
        node = self.m.create_node(LAYOUT, raw={"input": SEQ})
        self.m.submit(node)
        self.m.cancel(node)
        self.assertEqual(self.farm.cancelled, ["job00001-xxxx"])
        self.assertIn("cancelled", node["label"].value())

    def test_refresh_after_reopening(self):
        node = self.m.create_node(LAYOUT, raw={"input": SEQ})
        self.m.submit(node)
        self.tracked.clear()
        self.farm.jobs["job00001-xxxx"].update(status="running", progress=0.4)
        self.m.refresh(node)
        self.assertEqual(self.tracked, ["job00001-xxxx"])              # followed again
        self.assertIn("40%", node["cf_status"].value())
        self.assertIn("Up to date", node["cf_note"].value())
        changed = json.loads(json.dumps(LAYOUT))
        changed["params"].append({"name": "seed", "type": "int", "label": "Seed", "default": 7})
        self.farm.workflow_list = [changed]
        self.m.refresh(node)
        self.assertIn("Rebuild", node["cf_note"].value())

    def test_rebuild_keeps_values_connections_and_versions(self):
        plate, ref = self.read("Plate", SEQ), self.read("Ref", REF)
        node = self.m.create_node(LAYOUT, connect={"input": plate, "reference": ref})
        node["p_prompt"].setValue("snow")
        node["p_steps"].setValue(30)
        self.m.submit(node)
        changed = json.loads(json.dumps(LAYOUT))
        changed["params"].append({"name": "seed", "type": "int", "label": "Seed", "default": 7})
        self.farm.workflow_list = [changed]
        self.m.rebuild(node)
        self.assertEqual((node["p_prompt"].value(), node["p_steps"].value(), node["p_seed"].value()),
                         ("snow", 30, 7))
        self.assertIs(node.input(0), plate)
        self.assertIs(node.input(1), ref)
        self.assertEqual(len(self.m._records(node)), 1)
        self.assertEqual(len([c for c in node.children if c.Class() == "Read"]), 1)
        self.m.submit(node)
        self.assertEqual(self.farm.submitted[-1]["params"]["seed"], "7")

    def test_texture_node_shows_one_map(self):
        node = self.m.create_node(CHORD, raw={"input": SEQ})
        self.assertNotIn("cf_publish", node.knobs())                    # chord_pbr doesn't publish
        self.m.submit(node)
        job = self.farm.jobs["job00001-xxxx"]
        job.update(status="done", outputs={"maps": {
            "basecolor": {"pattern": "/o/t.basecolor_v001.%04d.exr", "first": 1001, "last": 1012, "files": 2},
            "normal": {"pattern": "/o/t.normal_v001.%04d.exr", "first": 1001, "last": 1001, "files": 1}}})
        self.m.apply_jobs([job])
        read = self.inside(node, "Result")
        self.assertEqual((read["file"].value(), read["on_error"].value()), ("/o/t.basecolor_v001.%04d.exr", "black"))
        node["cf_map"].setValue("normal")
        self.m.knob_changed(node, node["cf_map"])
        self.assertEqual(read["file"].value(), "/o/t.normal_v001.%04d.exr")


if __name__ == "__main__":
    unittest.main()
