"""Nuke side of the ComfyUI farm: the panel's helpers and the menu commands.

Install: add this folder to NUKE_PATH from the farm's shared folder (each artist once:
`python3 <share>/client/nuke/install_for_me.py`); the farm's address comes from the
manager.json next to it, or $COMFYFARM_URL. menu.py adds a ComfyFarm menu
and the ComfyFarm panel (also under Windows > Custom, and it can be saved in a
workspace layout like any other panel).

Artist workflow: prerender the plate to a PNG sequence, open the panel (ComfyFarm >
Open panel) and fill in the form on its Submit tab: the input path (typed, browsed, or
"From selected" for a Read/Write node), check where the frames will go, Submit. Jobs
are always submitted from the form. A Read node for the result appears next to the
source when the job is done. Nuke stays usable while jobs run.
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
def node_sequence(node) -> str | None:
    """The farm input for a Read/Write node: 'path first-last' (whole file for movies)."""
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
    return f"{path} {int(first)}-{int(last)}"


def selected_sequences() -> list[tuple[str, str]]:
    """(node name, farm input) for each selected Read/Write node, top to bottom in the DAG."""
    nodes = sorted(nuke.selectedNodes(), key=lambda n: (n.ypos(), n.xpos()))
    out = []
    for n in nodes:
        value = node_sequence(n)
        if value:
            out.append((n.name(), value))
    return out


def browse_sequence() -> str | None:
    """Nuke's file browser (shows sequences as 'name.####.png 1001-1100')."""
    value = nuke.getClipname("ComfyFarm input (PNG sequence or movhistory .mov)", multiple=False)
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
