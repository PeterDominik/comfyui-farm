"""Nuke integration for the ComfyUI farm.

Install: put this file and ../comfyfarm_client.py on NUKE_PATH (or in ~/.nuke), set
COMFYFARM_URL in the environment, and add the lines from menu.py to your menu.py.

Select one or more Read nodes, run "ComfyFarm > Alpha from selected Read", fill in
the prompt/seed, and a Read node for each result appears next to its source when the
job finishes. Nuke stays usable while the jobs run.
"""

from __future__ import annotations

import threading

import nuke  # noqa: E402  (only importable inside Nuke)

import comfyfarm_client as farm

WORKFLOW = "ltx25_alpha_gen"


def _reads():
    return [n for n in nuke.selectedNodes() if n.Class() in ("Read", "DeepRead")]


def _ask(defaults: dict) -> dict | None:
    p = nuke.Panel("ComfyFarm: alpha generation")
    p.addMultilineTextInput("Positive prompt", defaults.get("positive_prompt", ""))
    p.addMultilineTextInput("Negative prompt", defaults.get("negative_prompt", ""))
    p.addSingleLineInput("Seed", str(defaults.get("seed", 42)))
    p.addBooleanCheckBox("Enhance prompt", bool(defaults.get("enhance_prompt", False)))
    if not p.show():
        return None
    try:
        seed = int(p.value("Seed"))
    except ValueError:
        nuke.message("Seed must be a whole number.")
        return None
    return {"positive_prompt": p.value("Positive prompt"), "negative_prompt": p.value("Negative prompt"),
            "seed": seed, "enhance_prompt": bool(p.value("Enhance prompt"))}


def _make_read(job: dict, source_name: str) -> None:
    src = nuke.toNode(source_name)
    out = job["outputs"]
    r = nuke.nodes.Read(file=farm.output_pattern(job), first=out["first"], last=out["last"],
                        origfirst=out["first"], origlast=out["last"])
    r["label"].setValue(f"comfyfarm {job['short_id']}\n{job['workflow']}")
    if src is not None:
        r.setXYpos(src.xpos() + 120, src.ypos())


def _watch(job: dict, source_name: str) -> None:
    def update(j):
        nuke.executeInMainThread(nuke.tprint, args=(f"[comfyfarm] {farm.describe(j).splitlines()[0]}",))
    try:
        final = farm.wait(job["id"], poll=5, on_update=update)
    except farm.FarmError as e:
        nuke.executeInMainThread(nuke.message, args=(f"ComfyFarm: lost track of {job['short_id']}: {e}",))
        return
    if final["status"] == "done":
        nuke.executeInMainThread(_make_read, args=(final, source_name))
    elif final["status"] == "failed":
        nuke.executeInMainThread(nuke.message, args=(f"ComfyFarm job {final['short_id']} failed:\n{final['error']}",))


_last: dict = {}


def submit_selected(workflow: str = WORKFLOW) -> None:
    reads = _reads()
    if not reads:
        nuke.message("Select one or more Read nodes first.")
        return
    params = _ask(_last)
    if params is None:
        return
    _last.update(params)
    lines = []
    for r in reads:
        path = nuke.filename(r)
        first, last = int(r["first"].value()), int(r["last"].value())
        try:
            job = farm.submit(workflow, dict(params, input=f"{path} {first}-{last}"), name=r.name())
        except farm.FarmError as e:
            lines.append(f"{r.name()}: NOT submitted - {e}")
            continue
        info = job["info"]
        lines.append(f"{r.name()}: {job['short_id']} queued (#{job.get('queue_position')}) -> "
                     f"{info.get('output_dir_posix') or info.get('output_dir')}")
        threading.Thread(target=_watch, args=(job, r.name()), daemon=True).start()
    nuke.message("ComfyFarm\n\n" + "\n".join(lines))


def load_job() -> None:
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
    _make_read(job, "")
