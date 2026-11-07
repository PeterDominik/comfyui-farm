"""ComfyFarm panel for Nuke: submit and track farm jobs without the web browser.

The panel builds its form from the farm's workflow definitions, so it shows the same
fields as the web dashboard and stays in sync when a workflow changes. Artists
prerender the plate to a PNG sequence (or use a movhistory .mov), then, on the Submit tab:

  1. fill in the form: the input with "From selected" (a Read/Write node), "Browse…", or a
     pasted path
  2. check the destination line (exact folder and file names, like the web form)
  3. Submit. The Queue tab follows the job (which workflow, on which machine); a Read node
     is created when it finishes. The Machines tab shows the farm's GPU machines.

Publishing (workflows that publish, when the farm has it switched on): "Publish when done"
on the Submit tab, or Publish… on a finished job in the Queue (right-click). The show and
shot start as the input path's, else the Nuke session's (RDO_CURRENT_SHOW / _SEQUENCE /
_SHOT), the step as the session's RDO_CURRENT_STEP; all can be changed. Show and shot
suggest from ShotGrid (through the farm; ↻ reloads), the sequence narrows the shots down.

Layout of this file:
  PanelModel      everything the panel does, without Qt or Nuke (unit-tested)
  ComfyFarmPanel  the Qt widget (PySide6 in Nuke 16, PySide2 in Nuke 13-15)

Outside Nuke, `python comfyfarm_panel.py` opens it as a window (needs PySide).
"""

from __future__ import annotations

import getpass
import html
import os
import threading
import time

try:
    import comfyfarm_client as farm
except ImportError:   # run straight from a checkout: python client/nuke/comfyfarm_panel.py
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import comfyfarm_client as farm

STATUS_COLORS = {"preparing": "#ecc98a", "queued": "#cdbdf0", "dispatched": "#b8c6d8", "running": "#9fcdf2",
                 "postprocessing": "#9fcdf2", "done": "#93d6a6", "failed": "#f0a59c", "cancelled": "#a3a9b0"}
COLUMNS = ["Job", "Shot / name", "Workflow", "Machine", "User", "Status", "Progress", "Wait", "Submitted", "Took",
           "Publish"]
STATUS_COL, PROGRESS_COL, PUBLISH_COL = COLUMNS.index("Status"), COLUMNS.index("Progress"), COLUMNS.index("Publish")
MACHINE_COLUMNS = ["#", "Machine", "Status", "GPU", "VRAM free", "Jobs", "Running", "ComfyUI", "Agent", "Last seen"]
MACHINE_STATUS_COL = 2
ACTIVE = ("queued", "preparing", "dispatched", "running", "postprocessing")
ON_MACHINE = ("preparing", "dispatched", "running", "postprocessing")
# The Nuke session's pipeline context: where a publish goes when the input path doesn't say.
RDO_ENV = {"show": "RDO_CURRENT_SHOW", "sequence": "RDO_CURRENT_SEQUENCE", "shot": "RDO_CURRENT_SHOT",
           "step": "RDO_CURRENT_STEP"}
PUBLISH_STATES = {"queued": ("publish queued", "#a3a9b0"), "dispatching": ("dispatching…", "#ecc98a"),
                  "dispatched": ("sent to Tractor", "#93d6a6"), "dry_run": ("dry run passed", "#93d6a6"),
                  "failed": ("publish failed", "#f0a59c")}
SG_KEEP = 300.0     # seconds the panel keeps ShotGrid lists (the farm caches them too; ↻ reloads)


def _ago(t: float | None, now: float) -> str:
    if not t:
        return ""
    s = now - t
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _dur(s: float | None) -> str:
    if s is None:
        return ""
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    m = s // 60
    return f"{m}m {s % 60:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def _about(s: float | None) -> str:
    """An estimate, as rough as it is: 45s, 4 min, 1.5 h."""
    if s is None:
        return ""
    if s < 60:
        return f"{max(5, int(round(s / 5)) * 5)}s"
    return f"{round(s / 60)} min" if s < 5940 else f"{s / 3600:.1f} h"


def eta_text(plan_eta: dict | None) -> str:
    """The plan's estimate: about how long the job takes, where, and when it'll be done
    behind the queue (from past runs of the workflow on those GPUs)."""
    if not plan_eta or plan_eta.get("run") is None:
        return ""
    out = f"Takes about {_about(plan_eta['run'])} on {', '.join(plan_eta.get('workers') or [])}"
    if abs((plan_eta.get("done_in") or 0) - plan_eta["run"]) > 30:
        out += f"; done in about {_about(plan_eta['done_in'])}"
    return out + "."


def _gpu_name(worker: dict) -> str:
    """'cuda:0 NVIDIA GeForce RTX 3090 : cudaMallocAsync' -> 'NVIDIA GeForce RTX 3090'."""
    name = ((worker.get("gpu") or {}).get("name") or "").split(" : ")[0]
    return name.split(" ", 1)[1] if name.startswith("cuda:") and " " in name else name


def _gb(b) -> str:
    return f"{b / 2**30:.1f} GB" if b else ""


class PanelModel:
    """What the panel does, independent of Qt and Nuke."""

    def __init__(self, client=farm, user: str | None = None):
        self.client = client
        self.user = user or getpass.getuser()
        self.workflows: list[dict] = []
        self.session_jobs: dict[str, str] = {}   # id -> last status, for jobs submitted here
        self.sources: dict[str, str] = {}        # id -> Nuke node the input came from
        self._pub_options: dict[str, dict] = {}  # workflow -> its publish options
        self._sg_projects: tuple[float, dict] | None = None
        self._sg_shots: dict[str, tuple[float, dict]] = {}

    # ------------------------------------------------------------ workflows / form
    def load_workflows(self) -> list[dict]:
        self.workflows = self.client.workflows()
        return self.workflows

    def workflow(self, name: str) -> dict | None:
        return next((w for w in self.workflows if w["name"] == name), None)

    @staticmethod
    def inputs(wf: dict) -> list[dict]:
        """The workflow's image inputs, in form order."""
        return [p for p in wf.get("params") or [] if p["type"] == "sequence"]

    @staticmethod
    def sequence_param(wf: dict) -> str | None:
        """The primary input: it names the outputs (an older farm says nothing: the first)."""
        ins = PanelModel.inputs(wf)
        return next((p["name"] for p in ins if p.get("primary")), None) or (ins[0]["name"] if ins else None)

    @staticmethod
    def takes_one(wf: dict, name: str | None = None) -> bool:
        """The input (default: the primary) takes one image: a sequence's frame is named
        (the current frame)."""
        name = name or PanelModel.sequence_param(wf)
        return any(p["name"] == name and p.get("takes") == "image" for p in PanelModel.inputs(wf))

    @staticmethod
    def output_param(wf: dict) -> str | None:
        return next((p["name"] for p in wf["params"] if p["type"] == "output_folder"), None)

    @staticmethod
    def params_from_form(wf: dict, raw: dict) -> dict:
        """Form values -> request params. Empty fields are left out (the farm applies its
        defaults); numbers go as text and the farm validates them."""
        out = {}
        for p in wf["params"]:
            name = p["name"]
            if name not in raw:
                continue
            value = raw[name]
            if p["type"] == "bool":
                out[name] = bool(value)
                continue
            if value is None:
                continue
            if p["type"] == "choice":
                out[name] = next((c for c in p.get("choices") or [] if str(c) == str(value)), value)
                continue
            text = value if p["type"] == "text" else str(value).strip()
            if text.strip() == "":
                continue
            out[name] = text
        return out

    # ------------------------------------------------------------ actions
    def plan(self, wf_name: str, raw: dict) -> dict:
        return self.client.plan(wf_name, self.params_from_form(self.workflow(wf_name), raw))

    def submit(self, wf_name: str, raw: dict, name: str | None = None, source_node: str | None = None,
               publish: dict | None = None) -> dict:
        """Queue a job; `publish`: publish it when it's done (publish_request())."""
        kw = {"publish": publish} if publish is not None else {}
        job = self.client.submit(wf_name, self.params_from_form(self.workflow(wf_name), raw), name=name or None, **kw)
        self.session_jobs[job["id"]] = job["status"]
        if source_node:
            self.sources[job["id"]] = source_node
        return job

    def poll(self, mine: bool = True, active_only: bool = True, limit: int = 200) -> tuple[list[dict], list[dict]]:
        """(jobs to show, session jobs that finished since the last poll)."""
        jobs = self.client.jobs(user=self.user if mine else None, active=active_only, limit=limit)
        by_id = {j["id"]: j for j in jobs}
        finished = []
        for jid, last in list(self.session_jobs.items()):
            job = by_id.get(jid)
            if job is None:
                try:
                    job = self.client.get(jid)
                except farm.FarmError:
                    continue
            if job["status"] in farm.FINISHED:
                if last not in farm.FINISHED:
                    finished.append(job)
                del self.session_jobs[jid]
            else:
                self.session_jobs[jid] = job["status"]
        return jobs, finished

    # ------------------------------------------------------------ publishing
    def publishes_jobs(self, wf_name: str | None) -> bool:
        """Whether a workflow's jobs can be published (the farm says so per workflow)."""
        return bool((self.workflow(wf_name) or {}).get("publish"))

    def publish_options(self, wf_name: str, refresh: bool = False) -> dict:
        """The workflow's templates, steps and defaults for publishing, or `why` it can't be."""
        if refresh or wf_name not in self._pub_options:
            try:
                self._pub_options[wf_name] = self.client.publish_options(wf_name)
            except farm.FarmError as e:
                return {"why": str(e)}
        return self._pub_options[wf_name]

    @staticmethod
    def rdo_context(env=None) -> dict:
        """The Nuke session's pipeline context (RDO_CURRENT_*), what's set of it. A shot
        given without its sequence (0010) becomes <sequence>_<shot>."""
        env = os.environ if env is None else env
        out = {k: (env.get(v) or "").strip() for k, v in RDO_ENV.items()}
        out = {k: v for k, v in out.items() if v}
        seq, shot = out.get("sequence"), out.get("shot")
        if seq and shot and not shot.lower().startswith(seq.lower()):
            out["shot"] = f"{seq}_{shot}"
        return out

    @staticmethod
    def publish_prefill(plan: dict | None, context: dict, options: dict, last: dict | None = None) -> dict:
        """What the publish fields start with. Show and shot: the input path's (the frames
        are that shot's), else the Nuke session's; `last`: a finished job's earlier publish
        or request, first. Sequence: the session's, when the shot is the session's. Step:
        the earlier one, else the session's when the studio has it, else the workflow's."""
        plan, last = plan or {}, last or {}
        steps = options.get("steps") or []
        out = {"show": last.get("show") or plan.get("show") or context.get("show") or "",
               "shot": last.get("shot") or plan.get("shot") or context.get("shot") or ""}
        same = out["shot"] and out["shot"] == context.get("shot")
        out["sequence"] = context.get("sequence", "") if same else ""
        out["step"] = next((s for s in (last.get("step"), context.get("step")) if s and s in steps),
                           options.get("step") or (steps[0] if steps else ""))
        out["template"] = last.get("template") or ""
        desc = last.get("description") or ""
        out["description"] = "" if desc == options.get("description") else desc
        return out

    @staticmethod
    def publish_request(values: dict) -> dict:
        """Field values -> the farm's publish request. Empty fields are the farm's to fill:
        the input path's show and shot, the workflow's step, its first template, Auto Publish."""
        out = {k: (str(values.get(k) or "").strip() or None) for k in ("show", "shot", "step", "template", "description")}
        out["dry_run"] = bool(values.get("dry_run"))
        return out

    def publish(self, job_id: str, values: dict) -> dict:
        """Publish a finished job: the publish record, queued (the Queue follows it)."""
        r = self.publish_request(values)
        return self.client.publish(job_id, step=r["step"], show=r["show"], shot=r["shot"], template=r["template"],
                                   description=r["description"], dry_run=r["dry_run"])

    def publishes(self, job_id: str) -> list:
        return self.client.publishes(job_id)

    @staticmethod
    def publish_cell(job: dict) -> tuple[str, str, str]:
        """(text, colour, tooltip) for the Queue's Publish column."""
        p, ask = job.get("publish"), (job.get("info") or {}).get("publish_request")
        if not p:
            if not ask or job.get("status") in ("failed", "cancelled"):
                return "", "", ""
            where = " · ".join(x for x in (ask.get("show"), ask.get("shot"), ask.get("step")) if x)
            return ("when done" + (" (dry run)" if ask.get("dry_run") else ""), "#a3a9b0",
                    f"Publishes to {where} with {ask.get('template')}.nk when it's done")
        label, color = PUBLISH_STATES.get(p.get("state"), (p.get("state") or "", "#a3a9b0"))
        text = f"Tractor {p['tractor_id']}" if p.get("state") == "dispatched" and p.get("tractor_id") else label
        tip = [label + (f": Tractor job {p['tractor_id']}" if p.get("tractor_id") else ""), p.get("title") or "",
               " · ".join(x for x in (p.get("show"), p.get("shot"), p.get("step")) if x)
               + (f" with {p['template']}.nk" if p.get("template") else ""),
               p.get("error") or "", *(p.get("notes") or [])]
        return text, color, "\n".join(t for t in tip if t)

    @staticmethod
    def publish_log_text(publishes: list[dict]) -> str:
        """Every publish of a job, latest first: what went where, the command, Daiquiri's output."""
        if not publishes:
            return "Not published yet."
        blocks = []
        for p in publishes:
            label = PUBLISH_STATES.get(p.get("state"), (p.get("state") or "",))[0]
            lines = [f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(p.get('created_at') or 0))}  "
                     f"{label}{'  Tractor job ' + str(p['tractor_id']) if p.get('tractor_id') else ''}"
                     f"{'  by ' + p['user'] if p.get('user') else ''}",
                     f"to {' · '.join(x for x in (p.get('show'), p.get('shot'), p.get('step')) if x)} "
                     f"with {p.get('template')}.nk, frames {p.get('frames') or 'forceInput'}"]
            for key, title in (("title", "job"), ("description", "description"), ("error", "error"),
                               ("workfile", "script"), ("command", "command")):
                if p.get(key):
                    lines.append(f"{title}: {p[key]}")
            lines += [f"note: {n}" for n in p.get("notes") or []]
            if p.get("output"):
                lines += ["output:", p["output"].rstrip()]
            blocks.append("\n".join(lines))
        return ("\n\n" + "-" * 60 + "\n\n").join(blocks)

    # ------------------------------------------------------------ ShotGrid (through the farm)
    def sg_projects(self, refresh: bool = False) -> dict:
        """The shows to suggest: {"projects": [{code, name}], "why", "set_up"}."""
        now = time.time()
        if refresh or self._sg_projects is None or now - self._sg_projects[0] > SG_KEEP:
            try:
                data = self.client.shotgrid_projects(refresh=refresh)
            except farm.FarmError as e:
                data = {"projects": [], "why": str(e), "set_up": True}
            self._sg_projects = (now, data)
        return self._sg_projects[1]

    def sg_shots(self, show: str, refresh: bool = False) -> dict:
        """A show's shots to suggest: {"shots": [{code, sequence, status}], "sequences", "why", "set_up"}."""
        key, now = show.strip().lower(), time.time()
        had = self._sg_shots.get(key)
        if refresh or had is None or now - had[0] > SG_KEEP:
            try:
                data = self.client.shotgrid_shots(show.strip(), refresh=refresh)
            except farm.FarmError as e:
                data = {"show": show, "shots": [], "sequences": [], "why": str(e), "set_up": True}
            had = self._sg_shots[key] = (now, data)
        return had[1]

    @staticmethod
    def shot_codes(shots: list[dict], sequence: str = "") -> list[str]:
        """The shot codes to suggest, narrowed to a sequence when one is given."""
        s = (sequence or "").strip().lower()
        return [x["code"] for x in shots if not s or (x.get("sequence") or "").lower() == s]

    @staticmethod
    def sequence_of(shots: list[dict], shot: str) -> str | None:
        want = (shot or "").strip().lower()
        return next((x.get("sequence") for x in shots if x["code"].lower() == want), None)

    @staticmethod
    def sg_note(data: dict, now: float | None = None) -> tuple[str, bool]:
        """(the line under the shot field, whether it's a warning)."""
        if not data or data.get("set_up") is False:
            return "", False
        if data.get("why"):
            return data["why"], True
        n = len(data.get("shots") or [])
        return (f"{n} shot{'' if n == 1 else 's'} in {data.get('show')} · from ShotGrid "
                f"{_ago(data.get('fetched_at'), now or time.time())}", False)

    # ------------------------------------------------------------ text for the view
    @staticmethod
    def destination_html(plan: dict, labels: dict | None = None) -> str:
        """The 'Will write …' preview, as the web dashboard shows it (`labels`: input
        name -> its label, for the other inputs)."""
        e = html.escape
        files = f"<code>{e(plan['first_file'])}</code>"
        if plan.get("last_file") and plan["last_file"] != plan["first_file"]:
            files += f" … <code>{e(plan['last_file'])}</code>"
        frames = plan.get("what") or (f"{plan['frames']} frames" if plan.get("frames") else "frames")
        out = (f"<b style='color:#93d6a6'>Will write</b> {e(frames)} to "
               f"<code>{e(plan.get('output_dir_posix') or plan.get('output_dir') or '')}/</code><br>as {files}")
        if plan.get("maps"):
            out += "<br>Maps: " + " ".join(f"<code>{e(m)}</code>" for m in plan["maps"])
        c = plan.get("convert") or {}
        scaled = (f" and scaled {e(c['scaled'])} (work_resolution {c.get('work_resolution')})"
                  if c.get("scaled") else "")
        if c.get("kind") == "exr":
            out += (f"<br>EXR converted first: <code>{e(c.get('from', ''))}</code> → "
                    f"<code>{e(c.get('to', ''))}</code> with <code>{e(c.get('config', ''))}</code>{scaled}")
        elif c.get("kind") == "image":
            out += (f"<br>Scaled first: {e(c.get('scaled') or '')} (work_resolution "
                    f"{c.get('work_resolution')}), no colour change")
        elif c.get("kind") == "movie":
            out += (f"<br>Movie: {e(str(c.get('codec') or ''))} {c.get('width') or '?'}×{c.get('height') or '?'} "
                    f"at {round(c.get('fps') or 0, 3):g} fps, ")
            out += (f"timecode <code>{e(c['timecode'])}</code> → frames {c['start']}-{c['end']}" if c.get("timecode")
                    else f"frames {c['start']}-{c['end']}")
            if c.get("slate_frame") is not None:
                out += f", first frame ({c['slate_frame']}) is a slate and is skipped"
        if (plan.get("version") or 1) > 1:
            out += (f"<br><span style='color:#e0a24a'>Earlier versions already exist there (or are queued), "
                    f"so this run is v{plan['version']:03d}.</span>")
        if eta_text(plan.get("eta")):
            out += "<br>" + e(eta_text(plan.get("eta")))
        for note in plan.get("notes") or []:
            out += f"<br><span style='color:#e0a24a'>{e(note)}</span>"
        for name, x in (plan.get("inputs") or {}).items():     # the other inputs
            label = e((labels or {}).get(name) or name)
            if not x:
                out += f"<br>{label}: none"
            elif x.get("first") is None:
                out += f"<br>{label}: an image"
            else:
                out += f"<br>{label}: " + (f"frame {x['first']}" if x["first"] == x["last"]
                                            else f"frames {x['first']}-{x['last']}")
        if not plan.get("picked") and plan.get("shot"):
            out += f"<br>Shot <code>{e(plan['shot'])}</code>, show <code>{e(plan.get('show') or '')}</code>."
        elif not plan.get("picked") and plan.get("asset"):
            out += f"<br>Asset <code>{e(plan['asset'])}</code>, show <code>{e(plan.get('show') or '')}</code>."
        return out

    def labels(self) -> dict[str, str]:
        """Workflow name -> the label artists see."""
        return {w["name"]: w.get("label") or w["name"] for w in self.workflows}

    @staticmethod
    def machine_of(job: dict) -> str:
        """Which machine runs (or ran) the job: a texture set's batches each have their own."""
        if job.get("kind") == "set":
            return "per batch"
        if job.get("status") == "queued" and job.get("next_worker"):
            return f"→ {job['next_worker']}"      # expected to take it
        return job.get("worker") or ""

    @staticmethod
    def row(job: dict, now: float | None = None, labels: dict | None = None) -> list[str]:
        now = now or time.time()
        status = job["status"]
        start = job.get("started_at") or job.get("dispatched_at")
        took = _dur(job["finished_at"] - start) if job.get("finished_at") and start else (
            _dur(now - job["started_at"]) if job.get("started_at") else "")
        eta = job.get("eta_seconds")
        eta = None if eta is None or status not in ACTIVE else ("soon" if eta < 5 else f"~{_about(eta)}")
        if status == "queued":      # a texture set has no place of its own: its batches queue
            pos = f"#{job['queue_position']}" if job.get("queue_position") else ""
            wait = " · ".join(x for x in (pos, eta and (eta if eta == "soon" else f"done in {eta}")) if x)
        else:
            wait = (eta if eta == "soon" else f"{eta} left") if eta else ""
        if status == "failed":
            progress = job.get("error") or ""
        elif status in ("running", "postprocessing", "dispatched", "done"):
            progress = f"{round((job.get('progress') or 0) * 100)}%  {job.get('stage') or ''}".strip()
        else:
            progress = job.get("stage") or ""
        name = job.get("name") or ""
        version = (job.get("info") or {}).get("version")
        if version:
            name += f"  v{version:03d}"
        workflow = (labels or {}).get(job.get("workflow")) or job.get("workflow") or ""
        return [job.get("short_id") or job["id"][:8], name, workflow, PanelModel.machine_of(job), job.get("user", ""),
                status, progress, wait, _ago(job.get("created_at"), now), took, PanelModel.publish_cell(job)[0]]

    # ------------------------------------------------------------ machines
    @staticmethod
    def machine_state(w: dict) -> tuple[str, str]:
        """(what the machine is doing, a colour) for the Machines tab."""
        nodes = w.get("farm_nodes") or {}       # its ComfyUI runs other farm nodes than its checkout
        if nodes.get("state") == "restarting":
            return ("restarting ComfyUI (new farm nodes)", "#e0a24a")
        if nodes.get("state") == "syncing":     # comfyfarm comfyui sync is updating it
            return ("updating ComfyUI (sync)", "#e0a24a")
        if not w.get("online"):
            return ("offline", "#e0675b")
        if not w.get("connected"):
            return ("ComfyUI connecting", "#e0a24a")
        if nodes:
            return ({"waiting": "new farm nodes: restarting ComfyUI when idle"}.get(nodes.get("state"),
                                                                                    "farm nodes out of date"), "#e0a24a")
        if w.get("drained"):                     # from the dashboard: finishes what it holds
            held = w.get("held") or 0
            return (f"draining ({held} job{'s' if held > 1 else ''} left)" if held else "drained: no new jobs",
                    "#e0a24a")
        if w.get("checked") is False:
            return ("checking nodes", "#e0a24a")
        if w.get("problems"):
            return ("can't run " + ", ".join(sorted(w["problems"])), "#e0a24a")
        return ("busy" if w.get("inflight") else "ready", "#93d6a6")

    @staticmethod
    def machine_row(w: dict, jobs: list[dict], now: float | None = None) -> list[str]:
        now = now or time.time()
        gpu = w.get("gpu") or {}
        vram = f"{_gb(gpu.get('vram_free'))} of {_gb(gpu.get('vram_total'))}" if gpu.get("vram_total") else ""
        running = [j.get("name") or j.get("short_id") or "" for j in jobs
                   if j.get("worker") == w["name"] and j.get("status") in ON_MACHINE]
        rank = w.get("rank")
        return ["" if rank is None else str(rank), w["name"], PanelModel.machine_state(w)[0], _gpu_name(w), vram,
                f"{w.get('inflight', 0)}/{w.get('max_inflight', 0)}", ", ".join(running),
                w.get("comfyui_version") or "", (w.get("agent_version") or "").split("+")[-1],
                _ago(w.get("last_seen"), now)]

    @staticmethod
    def machines(workers: list[dict]) -> list[dict]:
        """In the order they get jobs (rank): a job goes to the first free one that can run it."""
        return sorted(workers, key=lambda w: (w.get("rank") if w.get("rank") is not None else 10**9, w.get("name") or ""))

    @staticmethod
    def online(workers: list[dict]) -> int:
        """Machines taking jobs (some may lack a workflow's nodes: see the Machines tab)."""
        return sum(1 for w in workers if w.get("online") and w.get("connected") and w.get("checked") is not False
                   and not w.get("farm_nodes") and not w.get("drained"))

    @staticmethod
    def summary(health: dict, workers: list[dict], user: str) -> str:
        """One line above the tabs: how many machines are online, the queue, who you are."""
        up = PanelModel.online(workers)
        color = "#93d6a6" if workers and up == len(workers) else "#e0a24a"
        return (f"<span style='color:{color}'>●</span> {up} of {len(workers)} machines online  ·  "
                f"{health.get('queued', 0)} queued  ·  you are <b>{html.escape(user)}</b>")


# ============================================================== Qt view
try:
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError:
        from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:   # no Qt: the model above is still importable (tests, scripts)
    QtCore = QtGui = QtWidgets = None

try:
    import nuke  # noqa: F401
    import comfyfarm_nuke as nk
except ImportError:
    nuke = nk = None


if QtWidgets is not None:

    class _Relay(QtCore.QObject):
        """Delivers results from worker threads to the UI thread."""
        finished = QtCore.Signal(object, object, object)   # callback, result, error

    def _suggesting(combo):
        """An editable combo box that suggests from its items as you type (anywhere in the
        name, any case) and takes anything typed."""
        combo.setEditable(True)
        combo.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        combo.setMaxVisibleItems(20)
        c = combo.completer()
        if c is not None:
            c.setCompletionMode(QtWidgets.QCompleter.PopupCompletion)
            c.setCaseSensitivity(QtCore.Qt.CaseInsensitive)
            c.setFilterMode(QtCore.Qt.MatchContains)
        return combo

    def _set_items(combo, items):
        """New items, keeping what's typed."""
        text = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        combo.setEditText(text)
        combo.blockSignals(False)

    class PublishFields(QtWidgets.QWidget):
        """Where a publish goes and how: show, sequence (narrows the shots down), shot (↻
        reloads the lists), step, template, description, dry run. Show and shot suggest
        from ShotGrid, through the farm; without it they're plain text."""

        def __init__(self, panel, options: dict, values: dict, parent=None):
            super().__init__(parent)
            self.panel, self.model, self.options = panel, panel.model, options
            self.touched: set[str] = set()     # typed in: no longer follows the input path
            self._shots: list[dict] = []
            self._n = 0
            form = QtWidgets.QFormLayout(self)
            form.setContentsMargins(0, 0, 0, 0)
            self.show_box = _suggesting(QtWidgets.QComboBox())
            self.show_box.setEditText(values.get("show") or "")
            self.show_box.lineEdit().setPlaceholderText("from the input path")
            self.seq_box = _suggesting(QtWidgets.QComboBox())
            self.seq_box.setEditText(values.get("sequence") or "")
            self.seq_box.lineEdit().setPlaceholderText("all (narrows the shot list)")
            self.shot_box = _suggesting(QtWidgets.QComboBox())
            self.shot_box.setEditText(values.get("shot") or "")
            self.shot_box.lineEdit().setPlaceholderText("from the input path")
            self.refresh_btn = QtWidgets.QToolButton()
            self.refresh_btn.setText("↻")
            self.refresh_btn.setToolTip("Reload the shows and shots from ShotGrid (a shot was just created)")
            shot_row = QtWidgets.QHBoxLayout()
            shot_row.setContentsMargins(0, 0, 0, 0)
            shot_row.addWidget(self.shot_box, 1)
            shot_row.addWidget(self.refresh_btn)
            self.note = QtWidgets.QLabel("")
            self.note.setWordWrap(True)
            self.step_box = QtWidgets.QComboBox()
            self.step_box.addItems(options.get("steps") or [])
            step = values.get("step") or options.get("step")
            if step and self.step_box.findText(step) >= 0:
                self.step_box.setCurrentIndex(self.step_box.findText(step))
            self.template_box = QtWidgets.QComboBox()
            self.template_box.addItems(options.get("templates") or [])
            if values.get("template") and self.template_box.findText(values["template"]) >= 0:
                self.template_box.setCurrentIndex(self.template_box.findText(values["template"]))
            unusable = options.get("unusable") or {}
            self.template_box.setToolTip(f"In {options.get('folder')}/" + "".join(
                f"\nNot usable: {n}.nk ({w})" for n, w in unusable.items()))
            self.desc_edit = QtWidgets.QLineEdit(values.get("description") or "")
            self.desc_edit.setPlaceholderText(options.get("description") or "Auto Publish")
            self.desc_edit.setMaxLength(500)
            forced = bool(options.get("dry_run"))
            self.dry_check = QtWidgets.QCheckBox("Dry run: Daiquiri checks it, nothing goes to Tractor")
            self.dry_check.setChecked(forced or bool(values.get("dry_run")))
            self.dry_check.setEnabled(not forced)
            if forced:
                self.dry_check.setToolTip("publish.dry_run is on in the farm's config")
            self.seq_label = QtWidgets.QLabel("Sequence")
            form.addRow("Show", self.show_box)
            form.addRow(self.seq_label, self.seq_box)
            form.addRow("Shot", shot_row)
            form.addRow("", self.note)
            form.addRow("Step", self.step_box)
            form.addRow("Nuke template", self.template_box)
            form.addRow("Description", self.desc_edit)
            form.addRow("", self.dry_check)
            self._set_up(False)                # shown once the farm says ShotGrid is there
            self.show_timer = QtCore.QTimer(self)
            self.show_timer.setSingleShot(True)
            self.show_timer.timeout.connect(lambda: self.load_shots(False))
            self.show_box.lineEdit().textEdited.connect(self._show_typed)
            self.show_box.activated.connect(lambda *_: (self.touched.add("show"), self.load_shots(False)))
            self.seq_box.editTextChanged.connect(lambda *_: self._fill_shots())
            self.shot_box.lineEdit().textEdited.connect(lambda *_: (self.touched.add("shot"), self._seq_of_shot()))
            self.shot_box.activated.connect(lambda *_: (self.touched.add("shot"), self._seq_of_shot()))
            self.refresh_btn.clicked.connect(lambda *_: self.reload(True))
            self.reload(False)

        def _set_up(self, on: bool):
            for w in (self.seq_label, self.seq_box, self.refresh_btn):
                w.setVisible(on)

        def _show_typed(self, *_):
            self.touched.add("show")
            self.show_timer.start(500)

        def values(self) -> dict:
            return {"show": self.show_box.currentText(), "shot": self.shot_box.currentText(),
                    "step": self.step_box.currentText(), "template": self.template_box.currentText(),
                    "description": self.desc_edit.text(), "dry_run": self.dry_check.isChecked()}

        def prefill(self, plan: dict):
            """The input path's show and shot, into the fields not typed in."""
            changed = False
            for key, box in (("show", self.show_box), ("shot", self.shot_box)):
                if key not in self.touched and plan.get(key) and box.currentText() != plan[key]:
                    box.setEditText(plan[key])
                    changed = True
            if changed:
                self.load_shots(False)

        # ShotGrid, through the farm (in the background)
        def reload(self, refresh: bool):
            if refresh:
                self.refresh_btn.setEnabled(False)
                self.note.setText("Reloading from ShotGrid…")

            def done(data, error):
                if error:
                    data = {"projects": [], "why": str(error), "set_up": True}
                self._set_up(data.get("set_up") is not False)
                _set_items(self.show_box, [p["code"] for p in data.get("projects") or []])
                self.load_shots(refresh)
            self.panel._async(lambda: self.model.sg_projects(refresh), done)

        def load_shots(self, refresh: bool):
            show = self.show_box.currentText().strip()
            self._n += 1
            n = self._n
            if not show:
                self._shots = []
                self._fill_shots()
                self.note.setText("")
                self.refresh_btn.setEnabled(True)
                return

            def done(data, error):
                self.refresh_btn.setEnabled(True)
                if n != self._n:
                    return                     # a newer show was asked for
                if error:
                    data = {"show": show, "shots": [], "why": str(error), "set_up": True}
                if data.get("set_up") is False:
                    self.note.setText("")
                    return
                self._shots = data.get("shots") or []
                _set_items(self.seq_box, data.get("sequences") or [])
                self._fill_shots()
                self._seq_of_shot()
                text, warn = PanelModel.sg_note(data)
                self.note.setText(f"<span style='color:{'#e0a24a' if warn else '#8a929c'}'>{html.escape(text)}</span>")
            self.panel._async(lambda: self.model.sg_shots(show, refresh), done)

        def _fill_shots(self):
            _set_items(self.shot_box, PanelModel.shot_codes(self._shots, self.seq_box.currentText()))

        def _seq_of_shot(self):
            seq = PanelModel.sequence_of(self._shots, self.shot_box.currentText())
            if seq and self.seq_box.currentText().strip().lower() != seq.lower():
                self.seq_box.setEditText(seq)

    class ComfyFarmPanel(QtWidgets.QWidget):
        instances: list = []

        def __init__(self, parent=None):
            super().__init__(parent)
            ComfyFarmPanel.instances.append(self)
            self.model = PanelModel()
            self.settings = QtCore.QSettings("ComfyFarm", "NukePanel")
            if not farm.base_url() and self.settings.value("farm_url"):
                farm.set_url(str(self.settings.value("farm_url")))
            self.relay = _Relay(self)
            self.relay.finished.connect(self._deliver, QtCore.Qt.QueuedConnection)
            self.fields: dict[str, tuple[dict, object]] = {}
            self.name_edit = None
            self._jobs: dict[str, dict] = {}       # shown in the table, by id
            self._selected_node: tuple[str, str] | None = None   # (node, input) from "From selected"
            self._workers: list[dict] = []
            self._plan_seq = 0
            self._plan: dict | None = None             # the destination preview's answer
            self.pub_check = self.pub_fields = self.pub_host = None   # "Publish when done"
            self._polling = False
            self._polls = 0
            self._build()
            self.plan_timer = QtCore.QTimer(self)
            self.plan_timer.setSingleShot(True)
            self.plan_timer.timeout.connect(self._update_plan)
            self.poll_timer = QtCore.QTimer(self)
            self.poll_timer.setInterval(3000)
            self.poll_timer.timeout.connect(lambda: self._poll())
            self.poll_timer.start()
            self._connect()

        # -------------------------------------------------------- async plumbing
        def _async(self, fn, callback):
            def work():
                try:
                    result, error = fn(), None
                except Exception as e:  # noqa: BLE001 - shown in the panel
                    result, error = None, e
                try:
                    self.relay.finished.emit(callback, result, error)
                except RuntimeError:   # the panel was closed while the request ran
                    pass
            threading.Thread(target=work, daemon=True).start()

        def _deliver(self, callback, result, error):
            try:
                callback(result, error)
            except RuntimeError:   # the panel was closed while the request ran
                pass

        # -------------------------------------------------------- layout
        def _build(self):
            outer = QtWidgets.QVBoxLayout(self)
            outer.setContentsMargins(6, 6, 6, 6)

            # One line above the tabs: the farm in short, its address, the dashboard.
            head = QtWidgets.QHBoxLayout()
            self.status_label = QtWidgets.QLabel("Connecting…")
            self.status_label.setTextFormat(QtCore.Qt.RichText)
            self.url_edit = QtWidgets.QLineEdit(farm.base_url() or "")
            self.url_edit.setPlaceholderText("http://comfyfarm01:8000")
            self.url_edit.setToolTip("Farm address (normally from the shared folder's manager.json, or $COMFYFARM_URL)")
            self.url_edit.editingFinished.connect(self._url_changed)
            dash = QtWidgets.QPushButton("Dashboard")
            dash.setToolTip("Open the web dashboard")
            dash.clicked.connect(lambda *_: self._open_dashboard())
            head.addWidget(self.status_label, 1)
            head.addWidget(QtWidgets.QLabel("Farm:"))
            head.addWidget(self.url_edit)
            head.addWidget(dash)
            outer.addLayout(head)

            self.tabs = QtWidgets.QTabWidget()
            outer.addWidget(self.tabs, 1)

            # Submit: the workflow's form.
            submit = QtWidgets.QWidget()
            tl = QtWidgets.QVBoxLayout(submit)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel("Workflow:"))
            self.wf_combo = QtWidgets.QComboBox()
            self.wf_combo.currentIndexChanged.connect(self._workflow_changed)
            row.addWidget(self.wf_combo, 1)
            tl.addLayout(row)
            self.desc_label = QtWidgets.QLabel("")
            self.desc_label.setWordWrap(True)
            tl.addWidget(self.desc_label)
            self.form_host = QtWidgets.QWidget()
            self.form = QtWidgets.QFormLayout(self.form_host)
            self.form.setContentsMargins(0, 0, 0, 0)
            tl.addWidget(self.form_host)
            self.dest_label = QtWidgets.QLabel("")
            self.dest_label.setTextFormat(QtCore.Qt.RichText)
            self.dest_label.setWordWrap(True)
            self.dest_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self.dest_label.setStyleSheet("QLabel { border-left: 2px solid #555; padding: 4px 8px; }")
            tl.addWidget(self.dest_label)
            actions = QtWidgets.QHBoxLayout()
            self.submit_btn = QtWidgets.QPushButton("Submit")
            self.submit_btn.clicked.connect(lambda *_: self._submit())
            self.msg_label = QtWidgets.QLabel("")
            self.msg_label.setWordWrap(True)
            self.msg_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            actions.addWidget(self.submit_btn)
            actions.addWidget(self.msg_label, 1)
            tl.addLayout(actions)
            self.autoread_check = QtWidgets.QCheckBox("Create a Read node when my jobs finish")
            self.autoread_check.setChecked(str(self.settings.value("auto_read", "true")) == "true" and nk is not None)
            self.autoread_check.setEnabled(nk is not None)
            self.autoread_check.toggled.connect(lambda v: self.settings.setValue("auto_read", "true" if v else "false"))
            tl.addWidget(self.autoread_check)
            tl.addStretch(1)
            scroll = QtWidgets.QScrollArea()   # a long form scrolls instead of squeezing
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
            scroll.setWidget(submit)
            self.tabs.addTab(scroll, "Submit")

            # Queue: everyone's jobs, which workflow, on which machine.
            queue = QtWidgets.QWidget()
            bl = QtWidgets.QVBoxLayout(queue)
            filt = QtWidgets.QHBoxLayout()
            self.mine_check = QtWidgets.QCheckBox("Only mine")
            self.mine_check.setChecked(False)   # show everyone's jobs, like the dashboard
            self.active_check = QtWidgets.QCheckBox("Only active")
            self.active_check.setChecked(False)
            refresh = QtWidgets.QPushButton("Refresh")
            for w in (self.mine_check, self.active_check):
                w.toggled.connect(lambda _v: self._poll(force=True))
            refresh.clicked.connect(lambda *_: self._poll(force=True))
            filt.addWidget(self.mine_check)
            filt.addWidget(self.active_check)
            filt.addStretch(1)
            filt.addWidget(refresh)
            bl.addLayout(filt)
            self.table = QtWidgets.QTreeWidget()
            self.table.setColumnCount(len(COLUMNS))
            self.table.setHeaderLabels(COLUMNS)
            self.table.setRootIsDecorated(False)
            self.table.setUniformRowHeights(True)
            self.table.setAlternatingRowColors(True)
            self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self._job_menu)
            self.table.itemDoubleClicked.connect(lambda item, _c: self._read_for(item))
            bl.addWidget(self.table, 1)
            self.tabs.addTab(queue, "Queue")

            # Machines: the farm's GPU machines and what they're doing.
            machines = QtWidgets.QWidget()
            ml = QtWidgets.QVBoxLayout(machines)
            self.machines = QtWidgets.QTreeWidget()
            self.machines.setColumnCount(len(MACHINE_COLUMNS))
            self.machines.setHeaderLabels(MACHINE_COLUMNS)
            self.machines.setRootIsDecorated(False)
            self.machines.setUniformRowHeights(True)
            self.machines.setAlternatingRowColors(True)
            ml.addWidget(self.machines, 1)
            self.machines_note = QtWidgets.QLabel("")
            self.machines_note.setWordWrap(True)
            self.machines_note.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            ml.addWidget(self.machines_note)
            self.tabs.addTab(machines, "Machines")

            self.tabs.setCurrentIndex(int(self.settings.value("tab", 0) or 0))
            self.tabs.currentChanged.connect(self._tab_changed)

        def _tab_changed(self, index):
            self.settings.setValue("tab", index)
            self._poll(force=True)

        # -------------------------------------------------------- farm / workflows
        def _connect(self):
            self.status_label.setText("Connecting…")
            self._async(lambda: (farm.health(), farm.workers(), self.model.load_workflows()), self._connected)

        def _connected(self, result, error):
            if error:
                self.status_label.setText(f"<span style='color:#e0675b'>{html.escape(str(error))}</span>")
                return
            health, workers, workflows = result
            self._show_status(health, workers)
            current = self.wf_combo.currentData() or self.settings.value("workflow")
            self.wf_combo.blockSignals(True)
            self.wf_combo.clear()
            for wf in workflows:
                self.wf_combo.addItem(wf["label"] + ("" if wf["enabled"] else "  (disabled)"), wf["name"])
            idx = max(0, self.wf_combo.findData(current)) if current else 0
            self.wf_combo.setCurrentIndex(idx)
            self.wf_combo.blockSignals(False)
            self._workflow_changed()
            self._poll(force=True)

        def _show_status(self, health, workers):
            self._workers = workers
            self.status_label.setText(PanelModel.summary(health, workers, self.model.user))
            self.tabs.setTabText(2, f"Machines ({PanelModel.online(workers)}/{len(workers)} online)")
            self._fill_machines()

        def _fill_machines(self):
            now = time.time()
            jobs = list(self._jobs.values())
            self.machines.clear()
            notes = []
            for w in PanelModel.machines(self._workers):
                item = QtWidgets.QTreeWidgetItem(PanelModel.machine_row(w, jobs, now))
                state, color = PanelModel.machine_state(w)
                item.setForeground(MACHINE_STATUS_COL, QtGui.QBrush(QtGui.QColor(color)))
                tip = (w.get("farm_nodes") or {}).get("message") or w.get("error") or \
                    "; ".join(f"{wf}: {'; '.join(p)}" for wf, p in (w.get("problems") or {}).items())
                skips = "; ".join(f"not for {wf} ({why})" for wf, why in (w.get("skips") or {}).items())
                tip = "\n".join(t for t in (tip, skips) if t)
                item.setToolTip(MACHINE_STATUS_COL, tip)
                item.setToolTip(0, "The order machines get jobs in: a job goes to the first free one that can run it")
                self.machines.addTopLevelItem(item)
                if w.get("farm_nodes"):
                    notes.append(f"<b>{html.escape(w['name'])}</b>: {html.escape(w['farm_nodes'].get('message') or '')}")
                elif w.get("problems"):
                    notes.append(f"<b>{html.escape(w['name'])}</b> can't run "
                                 + "; ".join(f"{html.escape(wf)} ({html.escape(p[0])})" for wf, p in w["problems"].items()))
                elif w.get("error") and not w.get("online"):
                    notes.append(f"<b>{html.escape(w['name'])}</b>: {html.escape(w['error'])}")
            for col in range(len(MACHINE_COLUMNS)):
                self.machines.resizeColumnToContents(col)
            self.machines_note.setText("<br>".join(notes) if notes else
                                       ("" if self._workers else "No machines have checked in."))

        def _url_changed(self):
            url = self.url_edit.text().strip().rstrip("/")
            if url == (farm.base_url() or ""):
                return
            farm.set_url(url or None)
            self.settings.setValue("farm_url", url)
            self._connect()

        def _open_dashboard(self):
            import webbrowser
            if farm.base_url():
                webbrowser.open(farm.base_url() + "/")

        def _wf(self):
            return self.model.workflow(self.wf_combo.currentData()) if self.wf_combo.count() else None

        def _workflow_changed(self, *_):
            wf = self._wf()
            keep = {p["type"]: w.text() for p, w in self.fields.values() if p["type"] in ("sequence", "output_folder")}
            while self.form.rowCount():
                self.form.removeRow(0)
            self.fields.clear()
            if not wf:
                return
            self.settings.setValue("workflow", wf["name"])
            self.desc_label.setText(wf.get("description") or "")
            if wf.get("problems") or wf.get("worker_problems"):
                probs = list(wf.get("problems") or []) + [p for ps in (wf.get("worker_problems") or {}).values() for p in ps]
                self.desc_label.setText((wf.get("description") or "") + "\n\nProblems: " + "; ".join(probs))
            for p in wf["params"]:
                widget, row = self._field(wf, p)
                label = p["label"] + (" *" if p.get("required") else "")
                self.form.addRow(label, row)
                if p.get("help"):
                    widget.setToolTip(p["help"])
                self.fields[p["name"]] = (p, widget)
                if keep.get(p["type"]):
                    widget.setText(keep[p["type"]])
            self.name_edit = QtWidgets.QLineEdit()
            self.name_edit.setPlaceholderText("defaults to the shot")
            self.form.addRow("Job name", self.name_edit)
            self.pub_check = self.pub_fields = self.pub_host = None
            if self.model.publishes_jobs(wf["name"]):
                self._add_publish_option(wf)
            self.submit_btn.setEnabled(bool(wf.get("enabled", True)))
            self._schedule_plan()

        def _add_publish_option(self, wf):
            """"Publish when done": the publish fields, once the farm has said what the
            workflow can be published with."""
            check = QtWidgets.QCheckBox("Publish when done")
            check.setToolTip("A Nuke template rendered with the frames on Tractor (Daiquiri), with a daily "
                             "in Artist Review")
            host = QtWidgets.QWidget()
            QtWidgets.QVBoxLayout(host).setContentsMargins(12, 0, 0, 0)
            host.setVisible(False)
            check.setEnabled(False)
            check.toggled.connect(host.setVisible)
            self.form.addRow("", check)
            self.form.addRow("", host)
            self.pub_check, self.pub_host = check, host

            def done(options, error):
                if self.pub_check is not check:
                    return                     # another workflow was picked meanwhile
                why = str(error) if error else options.get("why")
                if why:
                    check.setText("Publish when done (not available)")
                    check.setToolTip(why)
                    return
                values = PanelModel.publish_prefill(self._plan, PanelModel.rdo_context(), options)
                self.pub_fields = PublishFields(self, options, values)
                host.layout().addWidget(self.pub_fields)
                check.setEnabled(True)
            self._async(lambda: self.model.publish_options(wf["name"], refresh=True), done)

        def _field(self, wf, p):
            """(value widget, row widget or layout) for one parameter."""
            key = f"last/{wf['name']}/{p['name']}"
            saved = self.settings.value(key)
            t = p["type"]
            if t == "bool":
                w = QtWidgets.QCheckBox()
                w.setChecked(str(saved) == "true" if saved is not None else bool(p.get("default")))
                w.toggled.connect(lambda v, k=key: self.settings.setValue(k, "true" if v else "false"))
                return w, w
            if t == "choice":
                w = QtWidgets.QComboBox()
                w.addItems([str(c) for c in p.get("choices") or []])
                current = saved if saved is not None else p.get("default")
                if current is not None and w.findText(str(current)) >= 0:
                    w.setCurrentIndex(w.findText(str(current)))
                w.currentTextChanged.connect(lambda v, k=key: self.settings.setValue(k, v))
                if p.get("naming"):   # in the output file names: refresh the preview
                    w.currentTextChanged.connect(self._schedule_plan)
                return w, w
            if t == "text":
                w = QtWidgets.QPlainTextEdit(str(saved if saved is not None else p.get("default") or ""))
                w.setFixedHeight(54)
                w.textChanged.connect(lambda k=key, w=w: self.settings.setValue(k, w.toPlainText()))
                return w, w
            w = QtWidgets.QLineEdit()
            if t in ("sequence", "output_folder"):
                # Never pre-filled from last time: a path from another shot would send
                # renders to the wrong place.
                one = p.get("takes") == "image"
                matched = next((x for x in PanelModel.inputs(self._wf() or {}) if x["name"] == p.get("match")), None)
                w.setPlaceholderText(p.get("placeholder") or (
                    "" if t != "sequence" else
                    f"/rdo/shows/…/shot.step.%04d.png (at the {matched.get('label') or matched['name']}'s frames)"
                    if matched else "/rdo/shows/…/shot.step.%04d.png 1050" if one else
                    "/rdo/shows/…/shot.step.%04d.png 1001-1100"))
                w.textChanged.connect(self._schedule_plan)
                row = QtWidgets.QHBoxLayout()
                row.addWidget(w, 1)
                if t == "sequence":
                    sel = QtWidgets.QPushButton("From selected")
                    sel.setToolTip("Use the selected Read or Write node's file at the current frame" if one else
                                   "Use the selected Read or Write node's file and frame range")
                    sel.setEnabled(nk is not None)
                    sel.clicked.connect(lambda *_, n=p["name"]: self.fill_from_selection(n))
                    row.addWidget(sel)
                browse = QtWidgets.QPushButton("Browse…")
                browse.clicked.connect(lambda _c=False, w=w, t=t: self._browse(w, t))
                row.addWidget(browse)
                return w, row
            w.setText(str(saved if saved is not None else ("" if p.get("default") is None else p["default"])))
            w.textChanged.connect(lambda v, k=key: self.settings.setValue(k, v))
            if p.get("naming"):
                w.textChanged.connect(self._schedule_plan)
            return w, w

        def _browse(self, widget, kind):
            if nk is not None:
                value = nk.browse_sequence() if kind == "sequence" else nk.browse_folder()
            elif kind == "sequence":
                value = QtWidgets.QFileDialog.getOpenFileName(self, "Input")[0]
            else:
                value = QtWidgets.QFileDialog.getExistingDirectory(self, "Output folder")
            if value:
                widget.setText(value)

        def fill_from_selection(self, name: str | None = None):
            """Fill an input field (default: the primary's) from the selected Read/Write node
            (the form is still the artist's to check and submit)."""
            if nk is None:
                return
            wf = self._wf() or {"params": []}
            seq = name or PanelModel.sequence_param(wf)
            found = nk.selected_sequences(one_frame=PanelModel.takes_one(wf, seq))   # one image: the current frame
            if not found or not seq:
                label = next((i.get("label") or i["name"] for i in PanelModel.inputs(wf) if i["name"] == seq), "the input")
                self._message(nk.selection_hint(label), True)
                return
            self.fields[seq][1].setText(found[0][1])
            if seq == PanelModel.sequence_param(wf):
                self._selected_node = found[0]          # the result's Read goes next to the primary's node
            if len(found) > 1:
                self._message(f"{len(found)} nodes selected: used {found[0][0]}.")

        def _raw(self) -> dict:
            raw = {}
            for name, (p, w) in self.fields.items():
                if p["type"] == "bool":
                    raw[name] = w.isChecked()
                elif p["type"] == "choice":
                    raw[name] = w.currentText()
                elif p["type"] == "text":
                    raw[name] = w.toPlainText()
                else:
                    raw[name] = w.text()
            return raw

        # -------------------------------------------------------- destination preview
        def _schedule_plan(self, *_):
            if getattr(self, "_msg_error", False):   # the artist is changing the form: an old error no longer applies
                self.msg_label.setText("")
                self._msg_error = False
            self.plan_timer.start(450)

        def _update_plan(self):
            wf = self._wf()
            if not wf:
                return
            raw = self._raw()
            seq = PanelModel.sequence_param(wf)
            if seq and not raw.get(seq, "").strip():
                self.dest_label.setText("")
                return
            self._plan_seq += 1
            n = self._plan_seq
            self.dest_label.setText("Checking…")

            def done(result, error):
                if n != self._plan_seq:
                    return   # a newer request is on its way
                if error:
                    self.dest_label.setText(f"<span style='color:#e0675b'>{html.escape(str(error))}</span>")
                else:
                    self._plan = result
                    self.dest_label.setText(PanelModel.destination_html(
                        result, {i["name"]: i.get("label") or i["name"] for i in PanelModel.inputs(wf)}))
                    if self.pub_fields is not None:
                        self.pub_fields.prefill(result)     # the input path's show and shot
            self._async(lambda: self.model.plan(wf["name"], raw), done)

        # -------------------------------------------------------- submit
        def _message(self, text, error=False):
            color = "#e0675b" if error else "#93d6a6"
            self.msg_label.setText(f"<span style='color:{color}'>{html.escape(text)}</span>")
            self._msg_error = error

        def _submit(self):
            wf = self._wf()
            if not wf:
                return
            raw, name = self._raw(), self.name_edit.text().strip() if self.name_edit else ""
            seq = PanelModel.sequence_param(wf)
            node = self._selected_node[0] if self._selected_node and seq and \
                raw.get(seq, "").strip() == self._selected_node[1] else None
            publish = None
            if self.pub_check is not None and self.pub_check.isChecked() and self.pub_fields is not None:
                publish = PanelModel.publish_request(self.pub_fields.values())   # checked with the job
            self.submit_btn.setEnabled(False)
            self.msg_label.setText("Submitting…")

            def done(job, error):
                self.submit_btn.setEnabled(True)
                if error:
                    self._message(str(error), True)
                    return
                info = job.get("info") or {}
                pub = info.get("publish_request")
                self._message(f"Queued {job['short_id']} → {info.get('output_dir_posix') or info.get('output_dir')}"
                              + (f" · publishes to {pub.get('shot')} {pub.get('step')} when done"
                                 + (" (dry run)" if pub.get("dry_run") else "") if pub else ""))
                self._schedule_plan()   # that version is taken now
                self._poll(force=True)
            self._async(lambda: self.model.submit(wf["name"], raw, name, source_node=node, publish=publish), done)

        # -------------------------------------------------------- jobs
        def _poll(self, force=False):
            # Hidden tab: still follow jobs submitted from here (for their Read nodes).
            if self._polling or (not force and not self.isVisible() and not self.model.session_jobs):
                return
            self._polling = True
            self._polls += 1
            header = force or self._polls % 5 == 0 or self.tabs.currentIndex() == 2   # machines: every poll
            mine, active = self.mine_check.isChecked(), self.active_check.isChecked()

            def work():
                jobs, finished = self.model.poll(mine=mine, active_only=active, limit=150)
                status = (farm.health(), farm.workers()) if header else None
                return jobs, finished, status

            def done(result, error):
                self._polling = False
                if error:
                    self.status_label.setText(f"<span style='color:#e0675b'>{html.escape(str(error))}</span>")
                    return
                jobs, finished, status = result
                self._fill_table(jobs)
                if status:
                    self._show_status(*status)   # after the jobs: the Machines tab lists what each runs
                for job in finished:
                    self._job_finished(job)
            self._async(work, done)

        def _fill_table(self, jobs):
            """Update rows in place, so scrolling and selection survive the refresh."""
            now = time.time()
            labels = self.model.labels()
            items = {}
            for i in range(self.table.topLevelItemCount()):
                item = self.table.topLevelItem(i)
                items[item.data(0, QtCore.Qt.UserRole)] = item
            wanted = {j["id"] for j in jobs}
            for jid, item in list(items.items()):
                if jid not in wanted:
                    self.table.takeTopLevelItem(self.table.indexOfTopLevelItem(item))
                    del items[jid]
            added = False
            for idx, job in enumerate(jobs):
                item = items.get(job["id"])
                if item is None:
                    item = QtWidgets.QTreeWidgetItem()
                    item.setData(0, QtCore.Qt.UserRole, job["id"])
                    self.table.insertTopLevelItem(idx, item)
                    added = True
                elif self.table.indexOfTopLevelItem(item) != idx:
                    self.table.takeTopLevelItem(self.table.indexOfTopLevelItem(item))
                    self.table.insertTopLevelItem(idx, item)
                for col, text in enumerate(PanelModel.row(job, now, labels)):
                    item.setText(col, text)
                item.setForeground(STATUS_COL, QtGui.QBrush(QtGui.QColor(STATUS_COLORS.get(job["status"], "#d9dde2"))))
                item.setToolTip(PROGRESS_COL, job.get("error") or "")
                _text, color, tip = PanelModel.publish_cell(job)
                item.setForeground(PUBLISH_COL, QtGui.QBrush(QtGui.QColor(color or "#d9dde2")))
                item.setToolTip(PUBLISH_COL, tip)
            self._jobs = {j["id"]: j for j in jobs}
            active = sum(1 for j in jobs if j["status"] in ACTIVE)
            self.tabs.setTabText(1, f"Queue ({active} active)" if active else "Queue")
            if added:
                for col in range(len(COLUMNS)):
                    if col != PROGRESS_COL:
                        self.table.resizeColumnToContents(col)

        def _job_finished(self, job):
            if job["status"] == "done":
                out = job.get("outputs") or {}
                where = (job.get("info") or {}).get("output_dir_posix") if out.get("maps") else out.get("frame_pattern_posix")
                self._message(f"{job['short_id']} finished: {where}")
                if self.autoread_check.isChecked() and nk is not None:
                    nk.create_read(job, near=self.model.sources.get(job["id"]))
            elif job["status"] == "failed":
                self._message(f"{job['short_id']} failed: {job.get('error')}", True)
            if nk is not None:
                nuke.tprint(f"[comfyfarm] {farm.describe(job)}")

        def _job_of(self, item):
            return self._jobs.get(item.data(0, QtCore.Qt.UserRole)) if item else None

        def _read_for(self, item):
            job = self._job_of(item)
            if job and job["status"] == "done" and nk is not None:
                nk.create_read(job, near=self.model.sources.get(job["id"]))

        def _job_menu(self, pos):
            job = self._job_of(self.table.itemAt(pos))
            if not job:
                return
            menu = QtWidgets.QMenu(self)
            read = menu.addAction("Create Read node")
            read.setEnabled(job["status"] == "done" and nk is not None)
            read.triggered.connect(lambda *_: nk.create_read(job, near=self.model.sources.get(job["id"])))
            copy = menu.addAction("Copy output path")
            info = job.get("info") or {}
            path = (info.get("output_dir_posix") if (job.get("outputs") or {}).get("maps") else None) \
                or farm.output_pattern(job) or info.get("output_dir_posix") or ""
            copy.setEnabled(bool(path))
            copy.triggered.connect(lambda *_: QtWidgets.QApplication.clipboard().setText(path))
            reuse = menu.addAction("Use this job's input")
            reuse.triggered.connect(lambda *_: self._reuse(job))
            if self.model.publishes_jobs(job.get("workflow")) or job.get("publish"):
                menu.addSeparator()
                pub = menu.addAction("Publish…")
                pub.setEnabled(job["status"] == "done" and job.get("kind") != "set"
                               and self.model.publishes_jobs(job.get("workflow")))
                pub.triggered.connect(lambda *_: self._publish_dialog(job))
                log = menu.addAction("Publish log")
                log.setEnabled(bool(job.get("publish")))
                log.triggered.connect(lambda *_: self._publish_log(job))
            menu.addSeparator()
            cancel = menu.addAction("Cancel job")
            cancel.setEnabled(job["status"] in ("preparing", "queued", "dispatched", "running"))
            cancel.triggered.connect(lambda *_: self._async(lambda: farm.cancel(job["id"]),
                                                         lambda _r, e: self._after_action(e)))
            again = menu.addAction("Run again")
            again.setEnabled(job["status"] in farm.FINISHED)
            again.triggered.connect(lambda *_: self._async(lambda: self._retry(job["id"]),
                                                        lambda _r, e: self._after_action(e)))
            if job.get("kind") == "set":   # a texture set: rerun only what didn't finish, same folder
                failed = menu.addAction("Retry failed batches")
                failed.setEnabled(job["status"] in ("failed", "cancelled") or "failed" in (job.get("stage") or ""))
                failed.triggered.connect(lambda *_: self._async(lambda: farm.retry_failed(job["id"]),
                                                             lambda _r, e: self._after_action(e)))
            if job.get("error"):
                err = menu.addAction("Show error")
                err.triggered.connect(lambda *_: QtWidgets.QMessageBox.warning(self, "ComfyFarm", job["error"]))
            run = getattr(menu, "exec", None) or getattr(menu, "exec_")
            run(self.table.viewport().mapToGlobal(pos))

        # -------------------------------------------------------- publishing a finished job
        def _publish_dialog(self, job):
            """Publish… : the fields, started from the job (its earlier publish, its shot,
            the Nuke session's step), then dispatch."""
            def opened(options, error):
                why = str(error) if error else options.get("why")
                if why:
                    QtWidgets.QMessageBox.warning(self, "ComfyFarm", why)
                    return
                info = job.get("info") or {}
                last = job.get("publish") or info.get("publish_request")
                values = PanelModel.publish_prefill({"show": info.get("show"), "shot": info.get("shot")},
                                                    PanelModel.rdo_context(), options, last)
                dlg = QtWidgets.QDialog(self)
                dlg.setWindowTitle(f"Publish {job.get('short_id') or job['id'][:8]} · {job.get('name') or ''}")
                dlg.setMinimumWidth(520)
                lay = QtWidgets.QVBoxLayout(dlg)
                out = job.get("outputs") or {}
                frames = f" ({out['first']}-{out['last']})" if out.get("first") is not None else ""
                about = QtWidgets.QLabel(
                    f"The template is copied with its Placeholder_Output_Comfyui Read on this job's frames{frames}, "
                    f"then Daiquiri renders its Writes on Tractor and makes a daily in Artist Review.")
                about.setWordWrap(True)
                lay.addWidget(about)
                fields = PublishFields(self, options, values, dlg)
                lay.addWidget(fields)
                msg = QtWidgets.QLabel("")
                msg.setWordWrap(True)
                lay.addWidget(msg)
                buttons = QtWidgets.QDialogButtonBox()
                go = buttons.addButton("Publish", QtWidgets.QDialogButtonBox.AcceptRole)
                buttons.addButton(QtWidgets.QDialogButtonBox.Cancel)
                buttons.rejected.connect(dlg.reject)
                lay.addWidget(buttons)

                def publish(*_):
                    chosen = fields.values()
                    go.setEnabled(False)
                    msg.setText("Staging the template…")

                    def done(rec, err):
                        if err:
                            go.setEnabled(True)
                            msg.setText(f"<span style='color:#e0675b'>{html.escape(str(err))}</span>")
                            return
                        dlg.accept()
                        self._message(f"{job.get('short_id') or ''} publish {rec.get('state')}: {rec.get('show')} "
                                      f"{rec.get('shot')} {rec.get('step')}" + (" (dry run)" if chosen["dry_run"] else ""))
                        self._poll(force=True)
                    self._async(lambda: self.model.publish(job["id"], chosen), done)
                go.clicked.connect(publish)
                dlg.open()
            self._async(lambda: self.model.publish_options(job["workflow"], refresh=True), opened)

        def _publish_log(self, job):
            """Every publish of the job: where it went, the command, Daiquiri's output."""
            def done(publishes, error):
                if error:
                    self._message(str(error), True)
                    return
                dlg = QtWidgets.QDialog(self)
                dlg.setWindowTitle(f"Publishes of {job.get('short_id') or job['id'][:8]} · {job.get('name') or ''}")
                dlg.resize(820, 520)
                lay = QtWidgets.QVBoxLayout(dlg)
                text = QtWidgets.QPlainTextEdit(PanelModel.publish_log_text(publishes))
                text.setReadOnly(True)
                lay.addWidget(text)
                close = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
                close.rejected.connect(dlg.reject)
                lay.addWidget(close)
                dlg.open()
            self._async(lambda: self.model.publishes(job["id"]), done)

        def _retry(self, job_id):
            job = farm.retry(job_id)
            self.model.session_jobs[job["id"]] = job["status"]
            return job

        def _after_action(self, error):
            if error:
                self._message(str(error), True)
            self._poll(force=True)

        def _reuse(self, job):
            seq = PanelModel.sequence_param(self._wf() or {"params": []})
            info = job.get("info") or {}
            src = (info.get("input") or {}).get("raw")
            if seq and src and seq in self.fields:
                self.fields[seq][1].setText(src)

        def closeEvent(self, event):  # noqa: N802 (Qt naming)
            self.poll_timer.stop()
            super().closeEvent(event)


def main() -> int:
    import sys
    if QtWidgets is None:
        print("PySide6 or PySide2 is needed to open the panel outside Nuke.")
        return 1
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    w = ComfyFarmPanel()
    w.setWindowTitle("ComfyFarm")
    w.resize(900, 700)
    w.show()
    run = getattr(app, "exec", None) or getattr(app, "exec_")
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
