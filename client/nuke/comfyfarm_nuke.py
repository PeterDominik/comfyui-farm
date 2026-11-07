"""Nuke side of the ComfyUI farm: the panel's helpers and the menu commands.

Install: add this folder to NUKE_PATH from the farm's shared folder (each artist once:
`python3 <share>/client/nuke/install_for_me.py`); the farm's address comes from the
manager.json next to it, or $COMFYFARM_URL. menu.py adds a ComfyFarm menu
and the ComfyFarm panel (also under Windows > Custom, and it can be saved in a
workspace layout like any other panel).

Artist workflow: open the panel (ComfyFarm > Open panel) and fill in the form on its
Submit tab: each input's path (typed, browsed, or "From selected" for a Read/Write node:
an image sequence, EXRs included, a still or a movie), check where the frames will go,
Submit. Jobs
are always submitted from the form. A Read node for the result appears next to the
source when the job is done. Nuke stays usable while jobs run.

Publish templates (the scripts publishing renders): ComfyFarm > Save as Nuke Publish
Template publishes the saved script for a workflow or for every workflow (global); ComfyFarm >
Open Nuke Publish Template opens a copy of one to edit, which saves back to the same template.
Every version is kept (the dashboard's Nuke Publish Templates tab restores one).
"""

from __future__ import annotations

import os
import re

import nuke

import comfyfarm_client as farm

PANEL_ID = "com.rodeofx.comfyfarm"
# Nuke evaluates this text in its own __main__ namespace when it builds the panel, where
# comfyfarm_panel was never imported, so the expression imports it itself.
PANEL_WIDGET = "__import__('comfyfarm_panel').ComfyFarmPanel"
MOVIE_EXTENSIONS = (".mov", ".mp4", ".m4v", ".mxf")
SOURCE_CLASSES = ("Read", "Write", "WriteGeo", "DeepRead")

_panel = None


# ---------------------------------------------------------------- selection -> input
def node_sequence(node, frame: int | None = None) -> str | None:
    """The farm input for a Read/Write node: 'path first-last' (whole file for movies).
    With `frame` (workflows that take one image): 'path frame' for a sequence."""
    if node.Class() not in SOURCE_CLASSES or "file" not in node.knobs():
        return None
    path = nuke.filename(node)
    if not path:
        return None
    path = path.replace("\\", "/")
    if os.path.splitext(path)[1].lower() in MOVIE_EXTENSIONS:
        # Movies are numbered by their timecode on the farm (slate dropped in movhistory),
        # which isn't what a Read shows by default, so send the whole movie.
        return path
    if node.Class().startswith("Write"):
        if "use_limit" in node.knobs() and node["use_limit"].value():
            first, last = int(node["first"].value()), int(node["last"].value())
        else:
            first, last = nuke.root().firstFrame(), nuke.root().lastFrame()
    else:
        first, last = int(node["first"].value()), int(node["last"].value())
    if not re.search(r"%0?\d*d|#+", path):   # a single still
        return path
    if frame is not None:
        return f"{path} {int(frame)}"
    return f"{path} {int(first)}-{int(last)}"


def selected_sequences(one_frame: bool = False) -> list[tuple[str, str]]:
    """(node name, farm input) for each selected Read/Write node, top to bottom in the DAG.
    `one_frame`: at the current frame, for workflows that take one image."""
    nodes = sorted(nuke.selectedNodes(), key=lambda n: (n.ypos(), n.xpos()))
    frame = int(nuke.frame()) if one_frame else None
    out = []
    for n in nodes:
        value = node_sequence(n, frame)
        if value:
            out.append((n.name(), value))
    return out


def selection_hint(label: str) -> str:
    """Why "From selected" found nothing to use, for the panel's message."""
    names = [n.name() for n in nuke.selectedNodes()]
    if not names:
        return f"Nothing is selected: select the Read or Write node for {label}, then click From selected."
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return (f"{shown} {'is' if len(names) == 1 else 'are'} not a Read or Write node with a file: select the one "
            f"for {label}, then click From selected.")


def browse_sequence() -> str | None:
    """Nuke's file browser (shows sequences as 'name.####.png 1001-1100')."""
    value = nuke.getClipname("ComfyFarm input (image sequence, image or movie)", multiple=False)
    return value.replace("\\", "/") if value else None


def browse_folder() -> str | None:
    value = nuke.getFilename("ComfyFarm output folder", "*/")
    return value.replace("\\", "/").rstrip("/") if value else None


# ---------------------------------------------------------------- results -> Read
def _key(path: str | None) -> str:
    """Compare paths regardless of slashes and frame padding style."""
    path = (path or "").replace("\\", "/").split(" ")[0]
    return re.sub(r"%0?\d*d|#+|@+", "#", path).lower()


def _source_node(job: dict, near: str | None):
    if near and nuke.toNode(near) is not None:
        return nuke.toNode(near)
    info = job.get("info") or {}
    wanted = {_key((info.get("input") or {}).get("raw")), _key(info.get("input_posix"))} - {""}
    for n in nuke.allNodes(recurseGroups=False):
        if n.Class() in SOURCE_CLASSES and "file" in n.knobs() and _key(nuke.filename(n)) in wanted:
            return n
    return None


def _place_right_of(src, node) -> None:
    taken = {(n.xpos(), n.ypos()) for n in nuke.allNodes(recurseGroups=False) if n is not node}
    x = src.xpos() + 120
    while (x, src.ypos()) in taken:   # earlier results of the same source sit there
        x += 120
    node.setXYpos(x, src.ypos())


def create_map_reads(job: dict, near: str | None = None) -> list:
    """Texture jobs (CHORD): one Read per map. UDIM tiles are read as frames
    (1001, 1002, 1011, ...), so scrubbing steps through the tiles; missing tiles are black."""
    out = job.get("outputs") or {}
    reads, new, posix = [], [], farm.POSIX
    for channel, m in out["maps"].items():
        path = ((m.get("pattern_posix") if posix else None) or m["pattern"]).replace("\\", "/")
        existing = next((n for n in nuke.allNodes("Read", recurseGroups=False)
                         if _key(n["file"].value()) == _key(path)), None)
        if existing is not None:
            reads.append(existing)
            continue
        first, last = int(m.get("first") or 1), int(m.get("last") or m.get("first") or 1)
        kw = {"on_error": "black"} if last - first + 1 > int(m.get("files") or 1) else {}
        read = nuke.nodes.Read(file=path, first=first, last=last, origfirst=first, origlast=last, **kw)
        read["label"].setValue(f"comfyfarm {job['short_id']}\n{channel}")
        reads.append(read)
        new.append(read)
    src = _source_node(job, near)
    if src is not None:
        for read in new:   # left to right, in map order
            _place_right_of(src, read)
    for n in nuke.selectedNodes():
        n.setSelected(False)
    for read in reads:
        read.setSelected(True)
    return reads


def create_read(job: dict, near: str | None = None):
    """A Read node for a finished job, next to the node its input came from.
    Selects the existing one instead if the frames are already read in.
    Texture jobs get one Read per map (create_map_reads)."""
    pattern = farm.output_pattern(job)
    out = job.get("outputs") or {}
    if job.get("status") == "done" and out.get("maps"):
        reads = create_map_reads(job, near)
        return reads[0] if reads else None
    if job.get("status") != "done" or not pattern:
        nuke.message(f"ComfyFarm: job {job.get('short_id')} has no finished frames ({job.get('status')}).")
        return None
    pattern = pattern.replace("\\", "/")
    for n in nuke.allNodes("Read", recurseGroups=False):
        if _key(n["file"].value()) == _key(pattern):
            _select_only(n)
            return n
    first, last = int(out.get("first") or 1), int(out.get("last") or out.get("first") or 1)
    read = nuke.nodes.Read(file=pattern, first=first, last=last, origfirst=first, origlast=last)
    read["label"].setValue(f"comfyfarm {job['short_id']}\n{job.get('workflow', '')}")
    src = _source_node(job, near)
    if src is not None:
        _place_right_of(src, read)
    _select_only(read)
    return read


def _select_only(node) -> None:
    for n in nuke.selectedNodes():
        n.setSelected(False)
    node.setSelected(True)


# ---------------------------------------------------------------- panel
def register_panel() -> None:
    """Windows > Custom > ComfyFarm, and restoring the panel from saved workspaces."""
    from nukescripts import panels
    panels.registerWidgetAsPanel(PANEL_WIDGET, "ComfyFarm", PANEL_ID)


def show_panel():
    """Open the ComfyFarm panel (or bring it to the front) next to the Properties bin."""
    global _panel
    from nukescripts import panels
    if _panel is None:
        _panel = panels.registerWidgetAsPanel(PANEL_WIDGET, "ComfyFarm", PANEL_ID, True)
    pane = nuke.getPaneFor("Properties.1") or nuke.getPaneFor("DAG.1")
    _panel.addToPane(pane)
    return _panel


# ---------------------------------------------------------------- publish templates
OUTPUT_READ = "Placeholder_Output_Comfyui"
GLOBAL = "_global"
LEGACY = "_generic"          # what the global templates were called at first
TEMPLATE_COPIES = os.path.join(os.path.expanduser("~"), ".nuke", "comfyfarm_templates")


def scope_label(scope: str) -> str:
    return "global" if scope in (GLOBAL, LEGACY) else scope


def workflow_from_label(label: str | None) -> str | None:
    """The workflow a farm Read names in its label ("comfyfarm <job>\n<workflow>")."""
    lines = (label or "").splitlines()
    if len(lines) >= 2 and lines[0].startswith("comfyfarm "):
        return lines[1].strip() or None
    return None


def template_from_filename(path: str | None) -> tuple[str, str] | None:
    """(scope, name) of a copy made by Open Nuke Publish Template: <scope>__<name>.nk."""
    m = re.match(r"(.+?)__([A-Za-z0-9][A-Za-z0-9_.-]*)\.nk$", os.path.basename(path or ""))
    if not m:
        return None
    return (GLOBAL if m.group(1) == LEGACY else m.group(1)), m.group(2)


def template_target(scopes: list[str], script: str | None, output_label: str | None) -> tuple[str, str]:
    """Where Save as Nuke Publish Template suggests saving: the template this script was
    opened from, else the workflow its output Read came from, else global; named default."""
    opened = template_from_filename(script)
    if opened and opened[0] in scopes:
        return opened
    wf = workflow_from_label(output_label)
    return (wf if wf in scopes else GLOBAL), "default"


def _output_read():
    """The Read named Placeholder_Output_Comfyui, or the one selected Read once renamed so
    (asked first). None when there's neither."""
    node = nuke.toNode(OUTPUT_READ)
    if node is not None and node.Class() == "Read":
        return node
    reads = [n for n in nuke.selectedNodes() if n.Class() == "Read"]
    if len(reads) == 1 and nuke.ask(f"This script has no Read named {OUTPUT_READ}, the one the job's frames "
                                    f"go into.\n\nRename the selected Read ({reads[0].name()}) to {OUTPUT_READ}?"):
        reads[0].setName(OUTPUT_READ)
        return reads[0]
    return None


def save_as_template() -> None:
    """Menu command: publish this script, as saved, as a publish template for a workflow or
    for every workflow (global). Anyone may; the one it replaces stays in its history."""
    path = nuke.root().name()
    if not path or path == "Root":
        nuke.message("Save the script first (File > Save), then save it as a publish template.")
        return
    read = _output_read()
    if read is None:
        nuke.message(f"A publish template needs a Read named {OUTPUT_READ}: the job's frames go into it.\n"
                     "Select that Read (it's renamed for you), or name it so.")
        return
    if nuke.root().modified():
        if not nuke.ask("The script has unsaved changes. Save it now? The template is the saved script."):
            return
        nuke.scriptSave()
    try:
        overview = farm.publish_templates()
    except farm.FarmError as e:
        nuke.message(f"ComfyFarm: {e}")
        return
    if overview.get("why"):
        nuke.message(f"ComfyFarm: {overview['why']}")
        return
    scopes = [s["scope"] for s in overview.get("scopes") or []]
    scope, name = template_target(scopes, path, read["label"].value())
    shown = [scope_label(scope)] + [scope_label(s) for s in scopes if s != scope]
    p = nuke.Panel("Save as Nuke Publish Template")
    p.addEnumerationPulldown("For", " ".join(shown))
    p.addSingleLineInput("Name", name)
    if not p.show():
        return
    scope = GLOBAL if p.value("For") == "global" else p.value("For")
    name = (p.value("Name") or "").strip()
    existing = next((s.get("templates") or [] for s in overview["scopes"] if s["scope"] == scope), [])
    if any(t["name"] == name for t in existing) and not nuke.ask(
            f"Replace {name} ({scope_label(scope)})?\n\nThe one in place stays in its history: "
            "the dashboard's Nuke Publish Templates tab can restore it."):
        return
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        text = fh.read()
    try:
        r = farm.save_template(scope, name, text)
    except farm.FarmError as e:
        nuke.message(f"ComfyFarm: {e}")
        return
    nuke.message(f"Published as {name} ({scope_label(scope)}).\nVersion {r.get('version')}; publishing uses it "
                 "from now on (↻ next to the template list reloads an open form).")


def open_template() -> None:
    """Menu command: open a copy of a publish template to edit. Save as Nuke Publish
    Template then offers to save it back to the same one."""
    try:
        overview = farm.publish_templates()
    except farm.FarmError as e:
        nuke.message(f"ComfyFarm: {e}")
        return
    if overview.get("why"):
        nuke.message(f"ComfyFarm: {overview['why']}")
        return
    choices = [f"{scope_label(s['scope'])}/{t['name']}" for s in overview.get("scopes") or []
               for t in s.get("templates") or []]
    if not choices:
        nuke.message("ComfyFarm: there are no publish templates yet (ComfyFarm > Save as Nuke Publish Template).")
        return
    p = nuke.Panel("Open Nuke Publish Template")
    p.addEnumerationPulldown("Template", " ".join(choices))
    if not p.show():
        return
    shown, name = p.value("Template").split("/", 1)
    scope = GLOBAL if shown == "global" else shown
    try:
        text = farm.template_text(scope, name)
    except farm.FarmError as e:
        nuke.message(f"ComfyFarm: {e}")
        return
    os.makedirs(TEMPLATE_COPIES, exist_ok=True)
    dest = os.path.join(TEMPLATE_COPIES, f"{scope}__{name}.nk")
    with open(dest, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    nuke.scriptOpen(dest)


def load_job() -> None:
    """Menu command: Read node for a finished job, by its id."""
    job_id = nuke.getInput("ComfyFarm job id")
    if not job_id:
        return
    try:
        job = farm.get(job_id.strip())
    except farm.FarmError as e:
        nuke.message(str(e))
        return
    if job["status"] != "done":
        nuke.message(farm.describe(job))
        return
    create_read(job)
