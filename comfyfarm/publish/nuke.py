"""Publish templates: the Nuke scripts Daiquiri's update_and_render_nuke renders.

A template is an ordinary .nk an artist saves in the farm's bank,
<publish.templates>/<workflow>/<name>.nk, built around Read nodes with set names:

  Placeholder_Output_Comfyui    the job's output frames
  Placeholder_Input_<param>     the job's input <param> (e.g. Placeholder_Input_input for
                                the plate), optional

and at least one pipeline Write (WriteNukleus) that publishes. For each job the manager
copies the template to the staging folder and points those Reads at the job's files:
file, frame range, file type, colorspace (by extension: PNG color_picking, EXR rdo) and
a label naming the job, and sets the script's frame range. Nothing else in the script
changes, so whatever the artist put between the Reads and the Write stays.

The .nk is edited as text: top-level nodes are "<Class> {" ... "}" blocks with one knob
per line, which is all that's touched."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["OUTPUT_READ", "INPUT_READ", "Template", "templates", "stage", "nk_value", "frames_text"]

OUTPUT_READ = "Placeholder_Output_Comfyui"
INPUT_READ = "Placeholder_Input_"
_NODE = re.compile(r"^(\w+) \{\s*$")
_PLAIN = re.compile(r"[A-Za-z0-9_./%#:+@,=-]+")
# Read file types, by extension
FILE_TYPES = {"png": "png", "exr": "exr", "jpg": "jpeg", "jpeg": "jpeg", "tif": "tiff", "tiff": "tiff",
              "dpx": "dpx", "mov": "mov", "mp4": "mov"}


def nk_value(v) -> str:
    """A knob value as a .nk writes it: bare when it can be, else quoted with Tcl's
    special characters escaped (a [ would otherwise be evaluated)."""
    v = str(v)
    if _PLAIN.fullmatch(v):
        return v
    for a, b in (("\\", "\\\\"), ('"', '\\"'), ("[", "\\["), ("]", "\\]"), ("$", "\\$"), ("{", "\\{"),
                 ("}", "\\}"), ("\n", "\\n")):
        v = v.replace(a, b)
    return f'"{v}"'


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        v = re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), v[1:-1])
    return v


def frames_text(frames: list[int]) -> str:
    """Frame numbers as Daiquiri's frameRange takes them: 1001-1084, or 1001,1002,1011."""
    frames = sorted(set(frames))
    if not frames:
        return ""
    if frames == list(range(frames[0], frames[-1] + 1)):
        return f"{frames[0]}-{frames[-1]}" if len(frames) > 1 else str(frames[0])
    return ",".join(str(f) for f in frames)


@dataclass
class Node:
    cls: str
    start: int          # the "<Class> {" line
    end: int            # its closing "}"
    name: str | None


class Template:
    """A .nk, its top-level nodes, and the knob edits staging makes."""

    def __init__(self, text: str, path: str = ""):
        self.path = path
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.lines = text.splitlines()
        self.nodes = self._parse()

    @classmethod
    def read(cls, path) -> "Template":
        # newline="": a script saved on Windows keeps its CRLFs
        with open(path, encoding="utf-8", errors="replace", newline="") as fh:
            return cls(fh.read(), str(path))

    def _parse(self) -> list[Node]:
        nodes, i = [], 0
        while i < len(self.lines):
            m = _NODE.match(self.lines[i])
            if not m:
                i += 1
                continue
            j = i + 1
            while j < len(self.lines) and self.lines[j].rstrip() != "}":
                j += 1
            node = Node(m[1], i, j, None)
            node.name = self.knob(node, "name")
            nodes.append(node)
            i = j + 1
        return nodes

    def text(self) -> str:
        return self.newline.join(self.lines) + self.newline

    # ---------------------------------------------------------------- knobs
    def knob(self, node: Node, key: str) -> str | None:
        prefix = f" {key} "
        for line in self.lines[node.start + 1:node.end]:
            if line.startswith(prefix):
                return _unquote(line[len(prefix):])
        return None

    def set(self, node: Node, key: str, value) -> None:
        """Set a knob: replaced where it is, else added before the node's name (after
        `inputs` and `file`, which Nuke reads first)."""
        line = f" {key} {nk_value(value)}"
        prefix = f" {key} "
        for i in range(node.start + 1, node.end):
            if self.lines[i].startswith(prefix):
                self.lines[i] = line
                return
        at = next((i for i in range(node.start + 1, node.end) if self.lines[i].startswith(" name ")), node.end)
        self.lines.insert(at, line)
        self._shift(at)

    def _shift(self, at: int) -> None:
        for n in self.nodes:
            if n.start >= at:
                n.start += 1
            if n.end >= at:
                n.end += 1

    # ---------------------------------------------------------------- what it holds
    def find(self, cls: str | None = None, name: str | None = None) -> list[Node]:
        return [n for n in self.nodes if (cls is None or n.cls == cls) and (name is None or n.name == name)]

    @property
    def root(self) -> Node | None:
        return next(iter(self.find("Root")), None)

    def output_reads(self) -> list[Node]:
        return self.find("Read", OUTPUT_READ)

    def input_reads(self) -> dict[str, list[Node]]:
        out: dict[str, list[Node]] = {}
        for n in self.find("Read"):
            if n.name and n.name.startswith(INPUT_READ) and len(n.name) > len(INPUT_READ):
                out.setdefault(n.name[len(INPUT_READ):], []).append(n)
        return out

    def writes(self) -> list[Node]:
        """The Writes that publish: pipeline ones (WriteNukleus: a Write with rdo knobs),
        or any Write when there's none of those."""
        writes = [n for n in self.nodes if n.cls == "Write" or n.cls.startswith("Write")]
        rdo = [n for n in writes if any(" rdo_profile" in line or "rdo_profile " in line
                                        for line in self.lines[n.start + 1:n.end])]
        return rdo or writes

    def problems(self, inputs) -> list[str]:
        """Why this template can't publish a job of a workflow with these inputs (None:
        any workflow, so its input Reads aren't checked)."""
        out = []
        if not self.output_reads():
            out.append(f"it has no Read node named {OUTPUT_READ} (the one the job's output goes into)")
        unknown = sorted(set(self.input_reads()) - set(inputs)) if inputs is not None else []
        if unknown:
            out.append(f"it reads input{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}, which the workflow "
                       f"doesn't have ({', '.join(inputs) or 'none'}): Read nodes {INPUT_READ}<input>")
        if not self.writes():
            out.append("it has no Write node, so nothing would be published")
        return out

    # ---------------------------------------------------------------- staging
    def point_read(self, node: Node, path: str, first: int | None, last: int | None, label: str,
                   colorspaces: dict[str, str], movie: bool = False) -> None:
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        self.set(node, "file", path)
        if ext in FILE_TYPES:
            self.set(node, "file_type", FILE_TYPES[ext])
        if first is not None and not movie:        # a movie's frames are left to the template
            for key, v in (("first", first), ("last", last if last is not None else first),
                           ("origfirst", first), ("origlast", last if last is not None else first)):
                self.set(node, key, v)
            self.set(node, "origset", "true")
        if colorspaces.get(ext):
            self.set(node, "colorspace", colorspaces[ext])
        self.set(node, "label", label)

    def frame_range(self, first: int, last: int, name: str | None = None) -> None:
        r = self.root
        if r is None:
            return
        self.set(r, "first_frame", first)
        self.set(r, "last_frame", last)
        self.set(r, "frame", first)
        if name:
            self.set(r, "name", name)


def templates(folder) -> list[str]:
    """The templates of one workflow (their names, default first)."""
    try:
        names = sorted(p.stem for p in Path(folder).iterdir()
                       if p.suffix == ".nk" and p.is_file() and not p.name.startswith("."))
    except OSError:
        return []
    return sorted(names, key=lambda n: (n != "default", n.lower()))


def stage(template, dest, *, job: str, workflow: str, output: dict, inputs: dict[str, dict | None],
          colorspaces: dict[str, str]) -> dict:
    """Copy a template to `dest` with its placeholder Reads pointed at the job's files.
    output: {path, first, last}; inputs: {param: {path, first, last, kind} or None}.
    Returns what was set, for the publish record."""
    t = Template.read(template)
    probs = t.problems(inputs)
    if probs:
        raise ValueError(f"{Path(template).name} can't be used: " + "; ".join(probs))
    label = f"comfyfarm {job[:8]}\n{workflow}"
    for node in t.output_reads():
        t.point_read(node, output["path"], output.get("first"), output.get("last"), label, colorspaces)
    notes, wired = [], [OUTPUT_READ]
    for name, nodes in t.input_reads().items():
        src = inputs.get(name)
        if not src or not src.get("path"):
            notes.append(f"input {name} was left empty: {INPUT_READ}{name} keeps the template's file")
            continue
        movie = src.get("kind") == "movie"
        if movie:
            notes.append(f"input {name} is a movie: {INPUT_READ}{name} reads it, its frame numbering is the template's")
        for node in nodes:
            t.point_read(node, src["path"], src.get("first"), src.get("last"), f"comfyfarm {job[:8]}\ninput {name}",
                         colorspaces, movie=movie)
        wired.append(INPUT_READ + name)
    if output.get("first") is not None:
        t.frame_range(output["first"], output.get("last") or output["first"], str(dest))
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_text(t.text(), encoding="utf-8", newline="")
    try:
        os.chmod(dest, 0o644)                   # Tractor's blades read it as another user
    except OSError:
        pass
    return {"reads": wired, "notes": notes, "writes": [n.name for n in t.writes()]}
