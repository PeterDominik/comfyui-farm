"""ComfyFarm node for Nuke: one workflow's submission form as a node in the script.

A ComfyFarm node is a Group built from the workflow's definition (its card on the farm,
the same schema the panel builds its Submit form from):

  inputs    one input arrow per image input of the workflow, the primary one first. Each
            takes a Read or Write node (through Dots), like the panel's "From selected"; a
            path typed into the input's "path" knob is used instead.
  knobs     one per parameter, on the ComfyFarm tab, with the farm's labels, defaults,
            ranges and help; then the job name and "Publish when done".
  Submit    queues a job from the knobs. Every Submit is a new version (the farm never
            overwrites), listed in "Version": pick one to see its frames.
  output    the picked version's frames once it is done; before that, the primary input
            passes through. A texture workflow shows one map at a time ("Map").
  Refresh   asks the farm about the node's jobs (a reopened script follows its running
            jobs again from here), and whether the workflow changed on the farm.
  Rebuild   rebuilds the knobs and inputs from the farm's current definition, keeping the
            values, the connections and the versions.

Made from the panel (Create node: the form as filled in, wired to the node the input came
from) or from the Tab menu: ComfyFarm > <provider> > <task> > <workflow>, for example
ComfyFarm > ComfyUI Local > Masking > SAM3 (the defaults, wired to the selected Read/Write
nodes). The menus are the farm's (decision 0054) for the session's show: a workflow the show
may not use is listed "(not on this show)", and picking it says why. The node's label names
its provider.

Layout of this file:
  the core     everything that doesn't need Nuke: knob specs from a workflow, knob values
               to request params, version records (unit-tested)
  the node     building, submitting, following and showing (needs Nuke)
"""

from __future__ import annotations

import json
import re
import threading
import time

import comfyfarm_client as farm
from comfyfarm_panel import PanelModel

NODE_TAG = "cf_workflow"          # the knob that makes a Group a ComfyFarm node
PARAM_PREFIX = "p_"               # knob names of the workflow's parameters
PATH_SUFFIX = "_path"             # an image input's path knob: p_<name>_path
PUB_KNOBS = ("cf_pub_show", "cf_pub_shot", "cf_pub_step", "cf_pub_template", "cf_pub_desc", "cf_pub_dry")
NO_VERSION = "(none yet)"
POLL_SECONDS = 3.0
STATUS_TILES = {"queued": 0x8F7FB5FF, "preparing": 0xB59A5AFF, "dispatched": 0x7F8FA5FF, "running": 0x5A8FB5FF,
                "postprocessing": 0x5A8FB5FF, "done": 0x5AA06EFF, "failed": 0xB5655AFF, "cancelled": 0x6E7378FF}
IDLE_TILE = 0x4A5A6AFF
STATUS_COLORS = {"done": "#93d6a6", "failed": "#f0a59c", "cancelled": "#a3a9b0"}


# ============================================================== the core (no Nuke)
def image_params(wf: dict) -> list[dict]:
    """The workflow's image inputs in input-arrow order: the primary first (input 0, the
    one that passes through), then the others as the workflow lists them."""
    ins = PanelModel.inputs(wf)
    primary = PanelModel.sequence_param(wf)
    return sorted(ins, key=lambda p: p["name"] != primary)


def knob_name(param: str) -> str:
    return PARAM_PREFIX + param


def path_knob(param: str) -> str:
    return PARAM_PREFIX + param + PATH_SUFFIX


def knob_specs(wf: dict) -> list[dict]:
    """One spec per parameter, in the workflow's order: what knob to make and how.
    kind: image (a path knob beside an input arrow), folder, string, text, int, float,
    bool, choice."""
    out = []
    kinds = {"sequence": "image", "output_folder": "folder", "string": "string", "text": "text",
             "int": "int", "float": "float", "bool": "bool", "choice": "choice"}
    for p in wf.get("params") or []:
        kind = kinds.get(p["type"], "string")
        label = (p.get("label") or p["name"]) + (" *" if p.get("required") else "")
        tip = p.get("help") or ""
        spec = {"kind": kind, "param": p["name"], "label": label, "default": p.get("default"),
                "knob": path_knob(p["name"]) if kind == "image" else knob_name(p["name"])}
        if kind == "image":
            one = p.get("takes") == "image"
            spec["takes_one"] = one
            spec["label"] = f"{p.get('label') or p['name']} path"
            tip = ("Leave empty to use the Read or Write connected to the '" + p["name"] + "' input"
                   + (" (at the current frame)" if one else " (its file and frame range)")
                   + ". A path or a ShotGrid Version link here is used instead.") + ("\n\n" + tip if tip else "")
        if kind == "folder":
            tip = (tip + "\n\n" if tip else "") + (p.get("placeholder") or "Empty = the show's renders folder")
        if kind == "choice":
            spec["choices"] = [str(c) for c in p.get("choices") or []]
        for k in ("min", "max"):
            if p.get(k) is not None:
                spec[k] = p[k]
        spec["tooltip"] = tip
        out.append(spec)
    return out


def knob_value(spec: dict, value):
    """A form value (the panel's text, a saved setting or the farm's default) as the knob
    takes it; unusable values fall back to the default, then to empty."""
    kind = spec["kind"]
    for v in (value, spec.get("default")):
        if v is None:
            continue
        try:
            if kind == "bool":
                return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            if kind == "int":
                return int(float(str(v).strip()))
            if kind == "float":
                return float(str(v).strip())
            if kind == "choice":
                if str(v) in spec.get("choices") or []:
                    return str(v)
                continue
            return str(v)
        except ValueError:
            continue
    if kind == "choice":
        return (spec.get("choices") or [""])[0]
    return {"bool": False, "int": 0, "float": 0.0}.get(kind, "")


def raw_from_knobs(wf: dict, values: dict, inputs: dict) -> dict:
    """The panel-style form values (PanelModel.params_from_form takes them) from the knob
    values ({param: value}) and the resolved image inputs ({param: farm input or None})."""
    raw = {}
    for spec in knob_specs(wf):
        name = spec["param"]
        if spec["kind"] == "image":
            raw[name] = inputs.get(name) or ""
        elif name in values:
            v = values[name]
            raw[name] = v if spec["kind"] in ("bool", "text", "choice") else ("" if v is None else str(v))
    return raw


def missing_inputs(wf: dict, inputs: dict) -> list[str]:
    """Required image inputs with nothing connected and no path, by label."""
    return [p.get("label") or p["name"] for p in image_params(wf) if p.get("required") and not inputs.get(p["name"])]


def version_tag(pattern: str | None) -> str | None:
    """'v002' from a farm file name (<source>.alpha_v002.%04d.png)."""
    m = re.findall(r"_v(\d{3,})", (pattern or "").replace("\\", "/").rsplit("/", 1)[-1])
    return "v" + m[-1] if m else None


def job_record(job: dict, posix: bool | None = None) -> dict:
    """What a node keeps of one of its jobs (saved with the script): enough to show its
    frames and status without asking the farm."""
    posix = farm.POSIX if posix is None else posix
    out = job.get("outputs") or {}
    info = job.get("info") or {}
    rec = {"id": job["id"], "short_id": job.get("short_id") or job["id"][:8], "status": job.get("status"),
           "progress": job.get("progress") or 0, "stage": job.get("stage"), "error": job.get("error"),
           "machine": PanelModel.machine_of(job), "finished_at": job.get("finished_at"),
           "created_at": job.get("created_at"),
           "output_dir": info.get("output_dir_posix") if posix else None}
    rec["output_dir"] = rec["output_dir"] or info.get("output_dir")
    pattern = farm.output_pattern(job, posix)
    if pattern:
        rec.update(pattern=pattern.replace("\\", "/"), first=out.get("first"), last=out.get("last"))
    if out.get("maps"):
        rec["maps"] = {ch: {"pattern": ((m.get("pattern_posix") if posix else None) or m["pattern"]).replace("\\", "/"),
                            "first": m.get("first"), "last": m.get("last"), "files": m.get("files")}
                       for ch, m in out["maps"].items()}
    rec["version"] = version_tag(rec.get("pattern")) or version_tag(
        next((m["pattern"] for m in (rec.get("maps") or {}).values()), None)) or version_tag(rec.get("output_dir"))
    pub = job.get("publish")
    if pub:
        rec["publish"] = {k: pub.get(k) for k in ("state", "show", "shot", "step", "tractor_id", "error")}
    elif info.get("publish_request"):
        rec["publish"] = {"state": "when done", **{k: info["publish_request"].get(k) for k in ("show", "shot", "step")}}
    return rec


def merge_record(records: list[dict], job: dict, posix: bool | None = None) -> list[dict]:
    """The node's records with this job's latest state (added at the end if it's new)."""
    rec = job_record(job, posix)
    out = [dict(r) for r in records]
    for i, r in enumerate(out):
        if r["id"] == rec["id"]:
            # A finished job's frames stay known even when a later answer lacks them.
            out[i] = {**r, **{k: v for k, v in rec.items() if v is not None}}
            return out
    return out + [rec]


def version_labels(records: list[dict]) -> list[str]:
    """The Version menu, oldest first: 'v002  a1b2c3d4  done' (unique: the job id is in it)."""
    if not records:
        return [NO_VERSION]
    return [f"{r.get('version') or '#' + str(i + 1)}  {r['short_id']}  {r.get('status')}"
            for i, r in enumerate(records)]


def active(rec: dict) -> bool:
    return rec.get("status") not in farm.FINISHED


def expiry(rec: dict, retention: dict | None) -> float | None:
    """When the farm deletes the version's frames: keep_days after it finished. None when
    the farm deletes nothing (retention off), the version hasn't finished, or it was sent to
    be published (that restarts the clock, and the published frames live elsewhere)."""
    if not retention or not retention.get("enabled") or rec.get("finished_at") is None:
        return None
    if (rec.get("publish") or {}).get("state") in ("dispatched", "dry_run"):
        return None
    return float(rec["finished_at"]) + float(retention.get("keep_days") or 3) * 86400


def status_text(rec: dict | None, retention: dict | None = None, now: float | None = None) -> str:
    """The node's status line for the picked version (Nuke's Text_Knob shows simple HTML)."""
    if not rec:
        return "Not submitted yet."
    now = time.time() if now is None else now
    st = rec.get("status") or ""
    name = f"<b>{rec.get('version') or rec['short_id']}</b> {rec['short_id']}"
    color = STATUS_COLORS.get(st)
    shown = f"<font color='{color}'>{st}</font>" if color else st
    out = f"{name}: {shown}"
    if st in ("running", "postprocessing") or (active(rec) and rec.get("progress")):
        out += f" {int(round((rec.get('progress') or 0) * 100))}%"
    if rec.get("machine") and active(rec):
        out += f" on {rec['machine']}"
    if rec.get("error"):
        out += f"<br><font color='#f0a59c'>{_html(rec['error'][:300])}</font>"
    pub = rec.get("publish")
    if pub:
        where = " ".join(str(pub.get(k)) for k in ("show", "shot", "step") if pub.get(k))
        out += f"<br>publish {pub.get('state')}: {where}"
        if pub.get("error"):
            out += f" <font color='#f0a59c'>{_html(pub['error'][:200])}</font>"
    if st == "done":
        until = expiry(rec, retention)
        if until is not None:
            if now >= until:
                out += "<br><font color='#f0a59c'>Expired: the farm has deleted these frames. Resubmit, or pick " \
                       "a published version.</font>"
            else:
                when = time.strftime("%a %d %b %H:%M", time.localtime(until))
                out += f"<br>Frames kept until {when} unless published."
    if rec.get("output_dir"):
        out += f"<br><font color='#888'>{_html(rec['output_dir'])}</font>"
    return out


def tile_color(rec: dict | None) -> int:
    return STATUS_TILES.get((rec or {}).get("status"), IDLE_TILE)


def node_label(wf_name: str, rec: dict | None, provider: str = "") -> str:
    """The node's label: its provider (ComfyUI Local), its workflow, the picked version."""
    head = f"{provider}\n{wf_name}" if provider else wf_name
    if not rec:
        return head
    return f"{head}\n{rec.get('version') or rec['short_id']} {rec.get('status')}"


def menu_entries(tree: list | None, workflows: list[dict]) -> list[tuple]:
    """The Tab menu under ComfyFarm: (path, workflow name, why the show may not use it or
    None) per command, path the submenus then the command's label: (ComfyUI Local, Masking,
    SAM3 ...). Without the farm's menus (an older farm), the workflows straight under
    ComfyFarm. Disabled workflows aren't listed; ones the show may not use are, marked."""
    if tree is None:
        return [((wf.get("label") or wf["name"],), wf["name"], None) for wf in workflows if wf.get("enabled", True)]
    out = []
    for p in tree:
        for t in p.get("tasks") or []:
            for w in t.get("workflows") or []:
                if not w.get("enabled", True):
                    continue
                refused = None if w.get("allowed", True) else (w.get("reason") or "The farm's policy refuses it.")
                out.append(((PanelModel.provider_text(p), t["label"], PanelModel.workflow_text(w)), w["name"],
                            refused))
    return out


def schema_key(wf: dict) -> str:
    """What a node is built from: its parameters, maps and whether it publishes. A change
    in any of these on the farm means Rebuild."""
    params = [{k: p.get(k) for k in ("name", "type", "label", "required", "default", "takes", "choices",
                                    "min", "max", "primary", "match")} for p in wf.get("params") or []]
    return json.dumps({"params": params, "maps": wf.get("maps"), "publish": bool(wf.get("publish"))},
                      sort_keys=True)


def shown_frames(rec: dict | None, channel: str | None = None) -> dict | None:
    """{pattern, first, last, gaps} of a finished version: its frames, or one map's
    (channel; else the first) for a texture job. gaps: UDIM tiles read as frames, so
    missing ones should read black. None until it's done."""
    if not rec or rec.get("status") != "done":
        return None
    maps = rec.get("maps")
    if maps:
        m = maps.get(channel) or next(iter(maps.values()))
        first, last = int(m.get("first") or 1), int(m.get("last") or m.get("first") or 1)
        return {"pattern": m["pattern"], "first": first, "last": last,
                "gaps": last - first + 1 > int(m.get("files") or 1)}
    if not rec.get("pattern"):
        return None
    first = int(rec.get("first") or 1)
    return {"pattern": rec["pattern"], "first": first, "last": int(rec.get("last") or first), "gaps": False}


def _html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ============================================================== the node (Nuke)
try:
    import nuke
    import comfyfarm_nuke as nk
except ImportError:   # the core above stays importable without Nuke (tests, scripts)
    nuke = nk = None

KNOB_CHANGED = "import comfyfarm_node\ncomfyfarm_node.knob_changed(nuke.thisNode(), nuke.thisKnob())"
_workflows: list[dict] = []          # the farm's workflows, for the Tab menu and Refresh
_catalog: list | None = None         # the farm's menus for the session's show (decision 0054), or None
_retention: dict | None = None       # the farm's retention settings (when frames expire)
_tracked: set[str] = set()           # job ids being followed
_lock = threading.Lock()
_poller: threading.Thread | None = None


def _button(name: str, label: str, call: str, startline: bool = False):
    k = nuke.PyScript_Knob(name, label, f"import comfyfarm_node\ncomfyfarm_node.{call}(nuke.thisNode())")
    if startline:
        k.setFlag(nuke.STARTLINE)
    return k


def _hidden(name: str, value: str = ""):
    k = nuke.String_Knob(name, name)
    k.setValue(value)
    k.setFlag(nuke.INVISIBLE)
    return k


def _divider(name: str, label: str = ""):
    return nuke.Text_Knob(name, label, "")


def is_farm_node(node) -> bool:
    return node is not None and NODE_TAG in node.knobs()


def _wf_of(node) -> dict:
    return json.loads(node["cf_schema"].value() or "{}")


def _records(node) -> list[dict]:
    try:
        return json.loads(node["cf_jobs"].value() or "[]")
    except ValueError:
        return []


def _set_records(node, records: list[dict]) -> None:
    node["cf_jobs"].setValue(json.dumps(records))


# ------------------------------------------------------------ building
def _make_param_knob(spec: dict):
    kind, name = spec["kind"], spec["knob"]
    if kind == "bool":
        k = nuke.Boolean_Knob(name, spec["label"])
        k.setFlag(nuke.STARTLINE)
    elif kind == "int":
        k = nuke.Int_Knob(name, spec["label"])
    elif kind == "float":
        k = nuke.Double_Knob(name, spec["label"])
        if spec.get("min") is not None and spec.get("max") is not None:
            k.setRange(float(spec["min"]), float(spec["max"]))
    elif kind == "choice":
        k = nuke.Enumeration_Knob(name, spec["label"], spec.get("choices") or [""])
    elif kind == "text":
        k = nuke.Multiline_Eval_String_Knob(name, spec["label"])
    elif kind == "folder":
        k = nuke.File_Knob(name, spec["label"])
    else:
        k = nuke.String_Knob(name, spec["label"])
    if spec.get("tooltip"):
        k.setTooltip(spec["tooltip"])
    return k


def _publish_options(wf_name: str) -> dict:
    try:
        return farm.publish_options(wf_name)
    except farm.FarmError as e:
        return {"why": str(e)}


def _add_knobs(node, wf: dict, values: dict, job_name: str, publish_on: bool, pub_values: dict | None,
               keep: dict | None = None, provider: str = "") -> None:
    """The ComfyFarm tab, top to bottom: workflow, parameters, job name, publish, actions,
    versions, status. keep: hidden state carried over by Rebuild."""
    keep = keep or {}
    added = []

    def add(k):
        node.addKnob(k)
        added.append(k.name())
        return k

    add(nuke.Tab_Knob("cf_tab", "ComfyFarm"))
    add(nuke.Text_Knob(NODE_TAG, "Workflow", f"<b>{_html(wf.get('label') or wf['name'])}</b>"))
    if wf.get("description"):
        add(nuke.Text_Knob("cf_description", "", _html(wf["description"].strip())))
    add(_hidden("cf_name", wf["name"]))
    add(_hidden("cf_provider", provider))      # its label's first line (decision 0054)
    add(_hidden("cf_schema", json.dumps(wf)))
    add(_hidden("cf_jobs", keep.get("cf_jobs", "[]")))
    add(_hidden("cf_knob_list"))
    add(_divider("cf_div_params", "Parameters"))
    for spec in knob_specs(wf):
        k = add(_make_param_knob(spec))
        k.setValue(knob_value(spec, values.get(spec["param"])))
    add(nuke.String_Knob("cf_job_name", "Job name", job_name or ""))
    node["cf_job_name"].setTooltip("Defaults to the shot")

    if wf.get("publish"):
        add(_divider("cf_div_publish", "Publish"))
        options = _publish_options(wf["name"])
        why = options.get("why")
        on = add(nuke.Boolean_Knob("cf_publish", "Publish when done"))
        on.setFlag(nuke.STARTLINE)
        on.setValue(bool(publish_on) and not why)
        on.setTooltip(why or "A Nuke template rendered with the frames on Tractor (Daiquiri), with a daily in "
                             "Artist Review. Also: 'Publish version' publishes the picked version now.")
        pv = pub_values or PanelModel.publish_prefill(None, PanelModel.rdo_context(), options)
        add(nuke.String_Knob("cf_pub_show", "Show", pv.get("show") or ""))
        add(nuke.String_Knob("cf_pub_shot", "Shot", pv.get("shot") or ""))
        steps = options.get("steps") or [""]
        step = add(nuke.Enumeration_Knob("cf_pub_step", "Step", steps))
        if pv.get("step") in steps:
            step.setValue(pv["step"])
        keys = options.get("templates") or []
        labels = [(options.get("labels") or {}).get(k) or PanelModel.template_label(k) for k in keys] or [""]
        tpl = add(nuke.Enumeration_Knob("cf_pub_template", "Nuke template", labels))
        want = pv.get("template") if pv.get("template") in keys else options.get("template")
        if want in keys:
            tpl.setValue(labels[keys.index(want)])
        add(_hidden("cf_pub_keys", json.dumps(keys)))
        add(nuke.String_Knob("cf_pub_desc", "Description", pv.get("description") or ""))
        node["cf_pub_desc"].setTooltip(options.get("description") or "Auto Publish")
        dry = add(nuke.Boolean_Knob("cf_pub_dry", "Dry run"))
        dry.setFlag(nuke.STARTLINE)
        dry.setValue(bool(options.get("dry_run") or pv.get("dry_run")))
        dry.setTooltip("Daiquiri checks it, nothing goes to Tractor")
        if why:
            add(nuke.Text_Knob("cf_pub_why", "",
                               f"<font color='#e0a24a'>Publishing isn't available: {_html(why)}</font>"))
        for name in PUB_KNOBS:
            node[name].setVisible(bool(on.value()))

    add(_divider("cf_div_actions"))
    add(_button("cf_submit", "Submit", "submit", startline=True))
    add(_button("cf_refresh", "Refresh", "refresh"))
    add(_button("cf_cancel", "Cancel", "cancel"))
    if wf.get("publish"):
        add(_button("cf_publish_now", "Publish version", "publish_version"))
    add(_button("cf_rebuild", "Rebuild", "rebuild"))
    node["cf_refresh"].setTooltip("Ask the farm about this node's jobs (and follow the running ones), and "
                                  "whether the workflow changed")
    node["cf_rebuild"].setTooltip("Rebuild the knobs and inputs from the farm's current workflow, keeping "
                                  "the values, connections and versions")
    node["cf_cancel"].setTooltip("Cancel the picked version, if it hasn't finished")
    add(_divider("cf_div_versions", "Result"))
    add(nuke.Enumeration_Knob("cf_version", "Version", [NO_VERSION]))
    node["cf_version"].setTooltip("Every Submit is a new version: the node shows the picked one's frames")
    if wf.get("maps"):
        add(nuke.Enumeration_Knob("cf_map", "Map", list(wf["maps"])))
    add(nuke.Text_Knob("cf_status", "Status", "Not submitted yet."))
    add(nuke.Text_Knob("cf_note", "", ""))
    node["cf_knob_list"].setValue(json.dumps(added))
    node["knobChanged"].setValue(KNOB_CHANGED)


def _build_inside(node, wf: dict) -> None:
    """Inside the group: an Input per image input (primary first), the result Read, a
    Switch between the primary input (until a version is done) and the Read, the Output."""
    node.begin()
    try:
        for n in list(nuke.allNodes()):
            nuke.delete(n)
        inputs = []
        for i, p in enumerate(image_params(wf)):
            inp = nuke.nodes.Input(name=p["name"])
            inp.setXYpos(i * 110, 0)
            inputs.append(inp)
        read = nuke.nodes.Read(name="Result")
        read.setXYpos(len(inputs) * 110 + 110, 0)
        show = nuke.nodes.Switch(name="Show")
        show.setXYpos(0, 120)
        if inputs:
            show.setInput(0, inputs[0])
        else:
            show.setInput(0, nuke.nodes.Constant(name="Empty"))
        show.setInput(1, read)
        out = nuke.nodes.Output(name="Output1")
        out.setInput(0, show)
        out.setXYpos(0, 220)
    finally:
        node.end()


def _unique_name(base: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", base)
    i = 1
    while nuke.toNode(f"{base}{i}") is not None:
        i += 1
    return f"{base}{i}"


def _file_nodes() -> list:
    return [n for n in nuke.allNodes(recurseGroups=False) if n.Class() in nk.SOURCE_CLASSES and "file" in n.knobs()]


def create_node(wf: dict, raw: dict | None = None, job_name: str = "", publish_on: bool = False,
                pub_values: dict | None = None, connect: dict | None = None, provider: str | None = None):
    """A ComfyFarm node for the workflow (its definition from the farm).
    raw: the form's values ({param: text}, the panel's); missing ones take the farm's defaults.
    connect: {param: node} for the input arrows. Without it, the selected Read/Write nodes
    (top of the DAG first) go to the inputs in order, and a typed input path that a Read or
    Write in the script reads is connected to it (the path stays when it says more, a
    range). provider: its provider's label, for the node's label (by default the Tab menu's).
    Returns the node, its panel open."""
    raw = dict(raw or {})
    provider = PanelModel.provider_of(_catalog, wf["name"]) if provider is None else provider
    ins = image_params(wf)
    selected = sorted(nuke.selectedNodes(), key=lambda n: (n.ypos(), n.xpos()))
    if connect is None:
        connect = {}
        typed = {p["name"]: (raw.get(p["name"]) or "").strip() for p in ins}
        candidates = [n for n in selected if n.Class() in nk.SOURCE_CLASSES] + _file_nodes()
        for p in ins:
            if not typed[p["name"]]:
                continue
            want = nk._key(typed[p["name"]])
            hit = next((n for n in candidates if nk._key(nuke.filename(n)) == want), None)
            if hit is not None:
                connect[p["name"]] = hit
                one = p.get("takes") == "image"
                if nk.node_sequence(hit, int(nuke.frame()) if one else None) == typed[p["name"]]:
                    raw[p["name"]] = ""       # the connection says the same: follow the node
        free = [n for n in selected if n.Class() in nk.SOURCE_CLASSES and n not in connect.values()]
        for p in ins:
            if p["name"] not in connect and not (raw.get(p["name"]) or "").strip() and free:
                connect[p["name"]] = free.pop(0)
    values = {s["param"]: raw.get(s["param"]) for s in knob_specs(wf)}
    for s in knob_specs(wf):
        if s["kind"] == "image":
            values[s["param"]] = raw.get(s["param"]) or ""
    node = nuke.nodes.Group(name=_unique_name(f"ComfyFarm_{wf['name']}_"))
    _build_inside(node, wf)
    _add_knobs(node, wf, values, job_name, publish_on, pub_values, provider=provider)
    _connect(node, wf, connect)
    anchor = connect.get(ins[0]["name"]) if ins else None
    anchor = anchor or (selected[0] if selected else None)
    if anchor is not None:
        node.setXYpos(anchor.xpos(), anchor.ypos() + 90)
    for n in nuke.selectedNodes():
        n.setSelected(False)
    node.setSelected(True)
    node["tile_color"].setValue(IDLE_TILE)
    node["label"].setValue(node_label(wf["name"], None, provider))
    try:
        node.showControlPanel()
    except Exception:  # noqa: BLE001 - no UI (terminal Nuke)
        pass
    return node


def _connect(node, wf: dict, connect: dict) -> None:
    for i, p in enumerate(image_params(wf)):
        src = connect.get(p["name"])
        if src is not None:
            node.setInput(i, src)


def create_from_menu(wf_name: str):
    """Tab menu: ComfyFarm > <provider> > <task> > <workflow>, with the farm's defaults. One
    the session's show may not use says why instead."""
    refused = next((why for _, name, why in menu_entries(_catalog, _workflows) if name == wf_name and why), None)
    if refused:
        nuke.message(f"ComfyFarm: {refused}")
        return None
    wf = next((w for w in _workflows if w["name"] == wf_name), None)
    if wf is None:
        try:
            wf = next((w for w in farm.workflows() if w["name"] == wf_name), None)
        except farm.FarmError as e:
            nuke.message(f"ComfyFarm: {e}")
            return None
    if wf is None:
        nuke.message(f"ComfyFarm: the farm has no workflow {wf_name} any more (ComfyFarm > Reload workflows).")
        return None
    return create_node(wf)


# ------------------------------------------------------------ reading the node
def _upstream_file_node(node, index: int):
    """The Read/Write feeding an input arrow, through Dots."""
    src = node.input(index)
    while src is not None and src.Class() == "Dot":
        src = src.input(0)
    if src is not None and src.Class() in nk.SOURCE_CLASSES and "file" in src.knobs():
        return src
    return src if src is None else False      # False: connected to something the farm can't read


def resolve_inputs(node, wf: dict) -> tuple[dict, list[str]]:
    """({param: farm input}, problems): each image input's path knob, else its connected
    Read/Write (a one-image input at the current frame)."""
    out, problems = {}, []
    for i, p in enumerate(image_params(wf)):
        typed = node[path_knob(p["name"])].value().strip()
        if typed:
            out[p["name"]] = typed
            continue
        src = _upstream_file_node(node, i)
        label = p.get("label") or p["name"]
        if src is False:
            problems.append(f"{label}: the '{p['name']}' input needs a Read or Write node (the farm reads files); "
                            f"render this branch first, or type a path.")
            continue
        if src is None:
            continue
        value = nk.node_sequence(src, int(nuke.frame()) if p.get("takes") == "image" else None)
        if value:
            out[p["name"]] = value
        else:
            problems.append(f"{label}: {src.name()} has no file.")
    return out, problems


def knob_values(node, wf: dict) -> dict:
    out = {}
    for s in knob_specs(wf):
        if s["kind"] == "image" or s["knob"] not in node.knobs():
            continue
        out[s["param"]] = node[s["knob"]].value()
    return out


def publish_values(node) -> dict | None:
    """The publish knobs as the panel's fields give them; None without them."""
    if "cf_publish" not in node.knobs():
        return None
    keys = json.loads(node["cf_pub_keys"].value() or "[]")
    idx = int(node["cf_pub_template"].getValue()) if keys else -1
    return {"show": node["cf_pub_show"].value(), "shot": node["cf_pub_shot"].value(),
            "step": node["cf_pub_step"].value(), "template": keys[idx] if 0 <= idx < len(keys) else "",
            "description": node["cf_pub_desc"].value(), "dry_run": bool(node["cf_pub_dry"].value())}


def _picked(node, records: list[dict] | None = None) -> dict | None:
    records = _records(node) if records is None else records
    if not records:
        return None
    i = int(node["cf_version"].getValue())
    return records[i] if 0 <= i < len(records) else records[-1]


# ------------------------------------------------------------ showing
def show(node, pick: int | None = None) -> None:
    """The Version menu, status, label, tile colour and the output's frames, from the
    node's records. pick: select that version (by index)."""
    records = _records(node)
    labels = version_labels(records)
    current = int(node["cf_version"].getValue()) if records else 0
    node["cf_version"].setValues(labels)
    if pick is not None:
        current = pick
    current = max(0, min(current, len(labels) - 1))
    node["cf_version"].setValue(current)
    rec = records[current] if records else None
    node["cf_status"].setValue(status_text(rec, _retention))
    provider = node["cf_provider"].value() if "cf_provider" in node.knobs() else ""   # older nodes: none
    node["label"].setValue(node_label(node["cf_name"].value(), rec, provider))
    node["tile_color"].setValue(tile_color(rec))
    channel = node["cf_map"].value() if "cf_map" in node.knobs() else None
    frames = shown_frames(rec, channel)
    with node:
        read, switch = nuke.toNode("Result"), nuke.toNode("Show")
        if read is None or switch is None:
            return
        if frames:
            read["file"].setValue(frames["pattern"])
            for k in ("first", "origfirst"):
                read[k].setValue(frames["first"])
            for k in ("last", "origlast"):
                read[k].setValue(frames["last"])
            read["on_error"].setValue("black" if frames["gaps"] else "error")
            switch["which"].setValue(1)
        else:
            switch["which"].setValue(0)


def _note(node, text: str, error: bool = False) -> None:
    color = "#f0a59c" if error else "#93d6a6"
    node["cf_note"].setValue(f"<font color='{color}'>{_html(text)}</font>" if text else "")


def knob_changed(node, knob) -> None:
    """knobChanged of a ComfyFarm node: a version or map picked, publishing switched."""
    if knob is None or not is_farm_node(node):
        return
    name = knob.name()
    if name in ("cf_version", "cf_map"):
        show(node)
    elif name == "cf_publish":
        for k in PUB_KNOBS:
            if k in node.knobs():
                node[k].setVisible(bool(knob.value()))


# ------------------------------------------------------------ talking to the farm
def _later(fn, *args) -> None:
    """Run fn in Nuke's main thread (UI and knobs are touched there only)."""
    nuke.executeInMainThread(fn, args=args)


def _in_background(work, done) -> None:
    def run():
        try:
            result, error = work(), None
        except Exception as e:  # noqa: BLE001 - shown on the node
            result, error = None, e
        _later(done, result, error)
    threading.Thread(target=run, daemon=True).start()


def _by_name(full_name: str):
    node = nuke.toNode(full_name)
    return node if is_farm_node(node) else None


def submit(node) -> None:
    """Submit: a new version from the knobs, as they are now."""
    wf = _wf_of(node)
    inputs, problems = resolve_inputs(node, wf)
    missing = missing_inputs(wf, inputs)
    if missing:
        problems.append("Connect a Read or Write (or type a path) for: " + ", ".join(missing))
    if problems:
        _note(node, " ".join(problems), True)
        nuke.message("ComfyFarm: " + "\n".join(problems))
        return
    raw = raw_from_knobs(wf, knob_values(node, wf), inputs)
    model = PanelModel(client=farm)
    model.workflows = [wf]
    publish = None
    if "cf_publish" in node.knobs() and node["cf_publish"].value():
        publish = PanelModel.publish_request(publish_values(node))
    name = node["cf_job_name"].value().strip() or None
    full = node.fullName()
    _note(node, "Submitting…")

    def done(job, error):
        n = _by_name(full)
        if n is None:
            return
        if error:
            _note(n, str(error), True)
            return
        records = merge_record(_records(n), job)
        _set_records(n, records)
        show(n, pick=len(records) - 1)
        _note(n, f"Queued {job.get('short_id')}.")
        track([job["id"]])
    _in_background(lambda: model.submit(wf["name"], raw, name=name, publish=publish), done)


def refresh(node) -> None:
    """Refresh: every version's state from the farm; the running ones are followed again;
    a workflow that changed on the farm is pointed out (Rebuild)."""
    ids = [r["id"] for r in _records(node)]
    name = node["cf_name"].value()
    full = node.fullName()
    _note(node, "Asking the farm…")

    def work():
        global _retention, _workflows
        jobs, gone = [], []
        for jid in ids:
            try:
                jobs.append(farm.get(jid))
            except farm.FarmError:
                gone.append(jid)
        try:
            _retention = farm.retention()
        except farm.FarmError:
            pass
        try:
            _workflows = farm.workflows()
        except farm.FarmError:
            pass
        return jobs, gone

    def done(result, error):
        n = _by_name(full)
        if n is None:
            return
        if error:
            _note(n, str(error), True)
            return
        jobs, gone = result
        records = _records(n)
        for job in jobs:
            records = merge_record(records, job)
        _set_records(n, records)
        show(n)
        track([j["id"] for j in jobs if j.get("status") not in farm.FINISHED])
        now = next((w for w in _workflows if w["name"] == name), None)
        notes = []
        if gone:
            notes.append(f"{len(gone)} version(s) are no longer on the farm (deleted after retention).")
        if now is None and _workflows:
            notes.append(f"The farm has no workflow {name} any more.")
        elif now is not None and schema_key(now) != schema_key(_wf_of(n)):
            notes.append("The workflow changed on the farm: Rebuild updates the knobs.")
        _note(n, " ".join(notes) or "Up to date.", bool(notes))
    _in_background(work, done)


def cancel(node) -> None:
    rec = _picked(node)
    if not rec or not active(rec):
        _note(node, "The picked version isn't running.", True)
        return
    full = node.fullName()

    def done(job, error):
        n = _by_name(full)
        if n is None:
            return
        if error:
            _note(n, str(error), True)
            return
        _set_records(n, merge_record(_records(n), job))
        show(n)
        _note(n, f"Cancelled {job.get('short_id')}.")
    _in_background(lambda: farm.cancel(rec["id"]), done)


def publish_version(node) -> None:
    """Publish version: the picked version, now, with the publish knobs."""
    rec = _picked(node)
    if not rec or rec.get("status") != "done":
        _note(node, "Pick a finished version to publish.", True)
        return
    values = publish_values(node) or {}
    full, jid = node.fullName(), rec["id"]
    _note(node, "Publishing…")

    def work():
        PanelModel(client=farm).publish(jid, values)
        return farm.get(jid)

    def done(job, error):
        n = _by_name(full)
        if n is None:
            return
        if error:
            _note(n, str(error), True)
            return
        _set_records(n, merge_record(_records(n), job))
        show(n)
        _note(n, f"Publish {(job.get('publish') or {}).get('state') or 'queued'}.")
        track([jid])                      # follow the publish to Tractor
    _in_background(work, done)


def rebuild(node) -> None:
    """Rebuild: knobs and inputs from the farm's current workflow, keeping the values that
    still fit, the connections (by input name) and the versions."""
    name = node["cf_name"].value()
    try:
        wf = next((w for w in farm.workflows() if w["name"] == name), None)
    except farm.FarmError as e:
        _note(node, str(e), True)
        return
    if wf is None:
        _note(node, f"The farm has no workflow {name} any more.", True)
        return
    old = _wf_of(node)
    values = knob_values(node, old)
    for p in image_params(old):
        values[p["name"]] = node[path_knob(p["name"])].value()
    sources = {p["name"]: node.input(i) for i, p in enumerate(image_params(old))}
    job_name = node["cf_job_name"].value()
    pub_on = bool(node["cf_publish"].value()) if "cf_publish" in node.knobs() else False
    pub = publish_values(node)
    keep = {"cf_jobs": node["cf_jobs"].value()}
    provider = PanelModel.provider_of(_catalog, name) or \
        (node["cf_provider"].value() if "cf_provider" in node.knobs() else "")
    picked = int(node["cf_version"].getValue())
    for k in reversed(json.loads(node["cf_knob_list"].value() or "[]")):
        if k in node.knobs():
            node.removeKnob(node[k])
    for i in range(node.inputs()):
        node.setInput(i, None)
    _build_inside(node, wf)
    _add_knobs(node, wf, values, job_name, pub_on, pub, keep, provider=provider)
    _connect(node, wf, sources)
    show(node, pick=picked)
    _note(node, "Rebuilt from the farm's workflow.")


# ------------------------------------------------------------ following jobs
def track(job_ids) -> None:
    """Follow these jobs until they (and their publishes) finish; one thread for all nodes."""
    global _poller
    with _lock:
        _tracked.update(job_ids)
        if _poller is None or not _poller.is_alive():
            _poller = threading.Thread(target=_poll_loop, daemon=True)
            _poller.start()


def _poll_loop() -> None:
    while True:
        time.sleep(POLL_SECONDS)
        with _lock:
            ids = list(_tracked)
        if not ids:
            return
        jobs = []
        for jid in ids:
            try:
                jobs.append(farm.get(jid))
            except farm.FarmError:
                continue
        if jobs:
            _later(apply_jobs, jobs)


def _still_moving(job: dict) -> bool:
    pub = job.get("publish") or {}
    return job.get("status") not in farm.FINISHED or pub.get("state") in ("queued", "dispatching")


def apply_jobs(jobs: list[dict]) -> None:
    """Main thread: the followed jobs' latest state onto every node that has them."""
    by_id = {j["id"]: j for j in jobs}
    for node in nuke.allNodes("Group", recurseGroups=True):
        if not is_farm_node(node):
            continue
        records = _records(node)
        hits = [r["id"] for r in records if r["id"] in by_id]
        if not hits:
            continue
        for jid in hits:
            records = merge_record(records, by_id[jid])
        _set_records(node, records)
        show(node)
    with _lock:
        for j in jobs:
            if not _still_moving(j):
                _tracked.discard(j["id"])


# ------------------------------------------------------------ the Tab menu
def load_workflows() -> list[dict]:
    global _workflows, _retention
    _workflows = farm.workflows()
    try:
        _retention = farm.retention()
    except farm.FarmError:
        pass
    return _workflows


def load_menus() -> tuple[list[dict], list | None]:
    """The farm's workflows (their forms), and its menus for the session's show, or None
    when the farm can't give them (an older farm: the plain list)."""
    global _catalog
    workflows = load_workflows()
    model = PanelModel(client=farm)
    _catalog = model.load_catalog(PanelModel.rdo_context().get("show"))
    return workflows, _catalog


def fill_menu(menu, workflows: list[dict], tree: list | None = None) -> None:
    """ComfyFarm: Reload workflows, then <provider> > <task> > <workflow> (Tab finds them by
    name)."""
    menu.clearMenu()
    menu.addCommand("Reload workflows", lambda: register_menu(menu))
    menu.addSeparator()
    subs = {}
    for path, name, _ in menu_entries(tree, workflows):
        parent = menu
        for i in range(len(path) - 1):
            if path[:i + 1] not in subs:
                subs[path[:i + 1]] = parent.addMenu(path[i])
            parent = subs[path[:i + 1]]
        parent.addCommand(path[-1], lambda n=name: create_from_menu(n))


def register_menu(menu=None) -> None:
    """Nodes (and so Tab) > ComfyFarm: filled from the farm in the background, so a farm
    that's down doesn't slow Nuke's start (Reload workflows asks again)."""
    if menu is None:
        menu = nuke.menu("Nodes").addMenu("ComfyFarm")
        menu.addCommand("Reload workflows", lambda: register_menu(menu))

    def done(result, error):
        if error:
            menu.clearMenu()
            menu.addCommand("Reload workflows (farm not reached)", lambda: register_menu(menu))
            return
        fill_menu(menu, *result)
    _in_background(load_menus, done)
