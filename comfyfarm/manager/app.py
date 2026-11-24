"""The manager's REST + websocket API, the dashboard, and the endpoints agents check in
at (/api/v1/agent/..., protocol.py)."""

from __future__ import annotations

import asyncio
import html
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Config
from ..fileio.errors import SequenceError
from ..fileio.naming import default_output_dir_display
from ..publish.publisher import PublishError
from ..shotgrid import menu as SM
from ..shotgrid.menusync import MenuSync
from ..shotgrid import versions as SV
from ..definitions import DefinitionRegistry, ParamError
from ..protocol import now
from .db import ACTIVE, JobStore
from .desk import Desk
from .events import EventBus
from . import metrics as MX
from . import prom as PM
from . import share
from . import sweeper as SW
from . import usage as US
from .evals import EvalError
from .farm import Farm, FarmError

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


def _ami_page(message: str, link: tuple[str, str] = ("/", "Open the farm's dashboard"), title: str = "ComfyFarm") -> str:
    """The small page ShotGrid's right-click shows when it doesn't go straight to the
    dashboard: why, and a link on (`message` is HTML)."""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
<style>body {{ font: 15px/1.5 system-ui, sans-serif; background: #141517; color: #e6e6e6; margin: 0; padding: 48px 16px; }}
main {{ max-width: 620px; margin: 0 auto; }} h1 {{ font-size: 18px; }} a {{ color: #6fb3ff; }}
code {{ font-family: ui-monospace, Consolas, monospace; overflow-wrap: anywhere; }}</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{message}</p><p><a href="{html.escape(link[0])}">{html.escape(link[1])}</a></p></main></body></html>"""


@dataclass
class Identity:
    user: str
    admin: bool


class RolloutStart(BaseModel):
    release: str                        # a fingerprint, or "latest"
    machines: list[str] | None = None   # default: every machine the manager knows


class SyncStart(BaseModel):
    file: str                           # the version of the farm's environment file (its fingerprint)
    runner: str = "comfyui"             # whose environment: the machines' resident runner's
    machines: list[str] | None = None   # default: every machine the manager knows
    force: bool = False                 # discard local changes in its git checkouts


class EvalStart(BaseModel):
    definition: str                     # one with an eval: in its process.yaml
    machines: list[str] | None = None   # default: every machine checking in that may run it


class EnvironmentWant(BaseModel):
    release: str | None = None          # a fingerprint, "latest", or none: remove it
    machines: list[str] | None = None   # default: every machine the manager knows
    forget: bool = False                # stop managing it there: what is built stays as it is


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def from_this_host(host: str | None) -> bool:
    """A request from the manager's own machine. Until the farm has API keys, anyone could
    send a request with any user name (CLAUDE.md: identity), so what changes the machines'
    environments is taken only from here (decision 0030)."""
    return (host or "") in LOOPBACK or (host or "").startswith("::ffff:127.")


class JobRequest(BaseModel):
    workflow: str
    params: dict = Field(default_factory=dict)
    name: str | None = None
    priority: int = Field(0, ge=-10, le=10)
    show: str | None = None   # override when it can't be read from the input path
    shot: str | None = None
    user: str | None = None   # ignored when API keys are configured
    # Publish it when it's done: {show, shot, step, template, description}, all optional
    # (the manifest's step, the first template, the show and shot read from the input path).
    publish: dict | None = None


class TemplateSave(BaseModel):
    text: str                 # the whole Nuke script
    user: str | None = None   # ignored when API keys are configured


class TemplateRestore(BaseModel):
    version: str              # from the template's history
    user: str | None = None


class PublishRequest(BaseModel):
    show: str | None = None
    shot: str | None = None
    step: str | None = None
    template: str | None = None
    description: str | None = None
    dry_run: bool = False     # dispatchGraph --dry-run: Daiquiri checks it, nothing goes to Tractor
    wait: bool = False        # answer once it's dispatched (or failed), not as soon as it's queued
    user: str | None = None   # ignored when API keys are configured


class ShotGridQuery(BaseModel):
    """Batch Submit's ShotGrid query: names and picked values, never ShotGrid filters
    (comfyfarm.shotgrid.query builds those from what it offered)."""
    show: str
    sequences: list[str] = Field(default_factory=list)     # codes, or patterns (bpa*)
    shots: list[str] = Field(default_factory=list)
    name: str = ""                                          # words the code contains, any of them
    filters: dict[str, list[str] | str] = Field(default_factory=dict)   # status, step…: values picked, or a text
    since_days: int | None = None                           # created in the last N days
    latest: bool = True                                     # only the latest of each shot and name


class DrainRequest(BaseModel):
    drain: bool = True        # false: resume


def create_app(cfg: Config, local_agents: bool = True) -> FastAPI:
    """The manager. With local_agents (standalone), an agent for each of config.yaml's
    workers runs in this process too; agents on other machines can check in either way."""
    store = JobStore(cfg.resolve(cfg.database))
    # The heads only: how a definition wires its runner is checked by the agents, which
    # report one they find broken (docs/decisions/0020).
    registry = DefinitionRegistry(cfg.resolve(cfg.workflows_dir))
    events = EventBus()
    farm = Farm(cfg, store, registry, events)
    desk = Desk(farm)
    agents = []
    if local_agents:
        from ..agent.agent import Agent
        from ..agent.link import LocalLink
        from ..config import standalone_agent_config
        enabled = [w for w in cfg.workers if w.enabled]
        for w in enabled:
            # One worker converts into the scratch folder itself; several each get their own.
            scratch = cfg.convert.scratch_dir if len(enabled) == 1 else f"{cfg.convert.scratch_dir}/{w.name}"
            agents.append(Agent(standalone_agent_config(cfg, w, scratch), LocalLink(desk)))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(share.publish, cfg)   # manager.json and the client, in share.dir
        await farm.start()
        for a in agents:
            a.start()
        menu_sync.start()                             # ShotGrid's menu kept in step (shotgrid.menu_sync)
        yield
        await menu_sync.stop()
        for a in agents:
            await a.stop()
        await farm.stop()

    app = FastAPI(title="ComfyUI farm", version=__version__, lifespan=lifespan)
    app.state.farm = farm
    app.state.desk = desk
    app.state.agents = agents

    # ------------------------------------------------------------ errors
    @app.exception_handler(ParamError)
    @app.exception_handler(SequenceError)
    async def bad_request(_req: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FarmError)
    @app.exception_handler(PublishError)
    async def farm_error(_req: Request, exc: Exception):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    # ------------------------------------------------------------ auth
    def _user_for_key(key: str | None) -> str | None:
        return cfg.auth.api_keys.get(key) if key else None

    def identity(x_api_key: str | None = Header(None), x_user: str | None = Header(None)) -> Identity:
        if cfg.auth.api_keys:
            user = _user_for_key(x_api_key)
            if not user:
                raise HTTPException(401, "Missing or unknown API key (send it as the X-API-Key header).")
        else:
            user = (x_user or "anonymous").strip()[:64] or "anonymous"
        return Identity(user=user, admin=user in cfg.auth.admins)

    def _job_or_404(job_id: str) -> dict:
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found.")
        return job

    def _may_touch(ident: Identity, job: dict) -> None:
        if cfg.auth.api_keys and job["user"] != ident.user and not ident.admin:
            raise HTTPException(403, f"That job belongs to {job['user']}.")

    # ------------------------------------------------------------ routes
    # The dashboard's files are checked with the manager on every load ("no-cache"; they're
    # small). Without it a browser keeps showing the page from before a deploy for hours,
    # by its own guess at how long a file stays fresh.
    FRESH = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(HERE / "static" / "index.html", headers=FRESH)

    @app.get("/batch.js", include_in_schema=False)
    def batch_js():
        """The dashboard's Batch Submit tab (static/batch.js)."""
        return FileResponse(HERE / "static" / "batch.js", media_type="text/javascript", headers=FRESH)

    @app.get("/shotgrid.js", include_in_schema=False)
    def shotgrid_js():
        """The dashboard's From ShotGrid button, drop zone and bookmarklet (static/shotgrid.js)."""
        return FileResponse(HERE / "static" / "shotgrid.js", media_type="text/javascript", headers=FRESH)

    @app.get("/policy.js", include_in_schema=False)
    def policy_js():
        """The dashboard's Policy tab, read-only (static/policy.js)."""
        return FileResponse(HERE / "static" / "policy.js", media_type="text/javascript", headers=FRESH)

    @app.get("/gpu_history.js", include_in_schema=False)
    def gpu_history_js():
        """The Machines tab's GPU history graphs (static/gpu_history.js)."""
        return FileResponse(HERE / "static" / "gpu_history.js", media_type="text/javascript", headers=FRESH)

    @app.get("/gateway.js", include_in_schema=False)
    def gateway_js():
        """The Machines tab's gateway: its lanes (static/gateway.js)."""
        return FileResponse(HERE / "static" / "gateway.js", media_type="text/javascript", headers=FRESH)

    @app.get("/catalog.js", include_in_schema=False)
    def catalog_js():
        """The Submit tab's pickers, Provider > Task > Workflow (static/catalog.js)."""
        return FileResponse(HERE / "static" / "catalog.js", media_type="text/javascript", headers=FRESH)

    @app.get("/usage.js", include_in_schema=False)
    def usage_js():
        """The dashboard's Usage tab: jobs, GPU hours and failures (static/usage.js)."""
        return FileResponse(HERE / "static" / "usage.js", media_type="text/javascript", headers=FRESH)

    @app.get("/alerts.js", include_in_schema=False)
    def alerts_js():
        """The dashboard's Alerts tab and the alerts chip in its header (static/alerts.js)."""
        return FileResponse(HERE / "static" / "alerts.js", media_type="text/javascript", headers=FRESH)

    @app.get("/environments.js", include_in_schema=False)
    def environments_js():
        """The Machines tab's runner environments and rollouts, read-only (static/environments.js)."""
        return FileResponse(HERE / "static" / "environments.js", media_type="text/javascript", headers=FRESH)

    @app.get("/client.py", include_in_schema=False)
    def client_download():
        """Lets artists grab the Python client straight from the farm."""
        return FileResponse(REPO / "client" / "comfyfarm_client.py", filename="comfyfarm_client.py")

    @app.get("/api/v1/health")
    def health():
        return {"ok": True, "version": __version__,
                "auth": bool(cfg.auth.api_keys),
                "workers_online": farm.online_workers(),
                "queued": len(store.queued(with_prompt=False))}

    @app.get("/api/v1/workflows")
    def workflows(ident: Identity = Depends(identity)):
        out = []
        for wf in registry.workflows.values():
            s = wf.schema()
            for p in s["params"]:
                if p["type"] == "output_folder":   # shown greyed-out in the empty field
                    # Texture workflows version a folder per run; show the asset one (the usual case).
                    tpl = next((d for d in wf.textures.dirs if "{asset}" in d), wf.textures.dirs[0]) \
                        if wf.textures else None
                    p["placeholder"] = "Empty = " + default_output_dir_display(cfg.paths, wf.tag, tpl)
            s["worker_problems"] = {a.name: a.problems[wf.name] for a in list(desk.agents.values())
                                    if wf.name in a.problems}
            # Publish buttons: every video and image workflow, while publishing is on
            s["publish"] = farm.publisher.publish_info(wf)
            out.append(s)
        return {"workflows": out, "load_errors": registry.load_errors}

    @app.get("/api/v1/catalog")
    def catalog(show: str | None = None, hidden: bool = False, ident: Identity = Depends(identity)):
        """The menus every client draws, Provider > Task > Workflow, for a show: which of them
        the show may use, and why not (decision 0054). `hidden`: the farm's hidden ones too.
        A plain `def`: the policy may be read from the shared folder."""
        return farm.catalogue((show or "").strip()[:64] or None, hidden)

    @app.post("/api/v1/jobs", status_code=201)
    def submit(req: JobRequest, ident: Identity = Depends(identity)):
        # A plain `def` endpoint runs in a thread: validation reads the show storage.
        user = ident.user if cfg.auth.api_keys else ((req.user or ident.user).strip()[:64] or "anonymous")
        if req.priority > 0 and cfg.auth.api_keys and not ident.admin:
            raise HTTPException(403, "Only admins can submit with priority above 0.")
        job = farm.submit(req.workflow, req.params, user, name=req.name, priority=req.priority,
                          show=req.show, shot=req.shot, publish=req.publish)
        return farm.annotate([job])[0]

    @app.post("/api/v1/plan")
    def plan(req: JobRequest, ident: Identity = Depends(identity)):
        """Dry run: where a job with these params would write, without queuing it; with
        `publish`, also whether publishing it when done would be accepted (`publish`:
        {request} or {why})."""
        user = ident.user if cfg.auth.api_keys else ((req.user or ident.user).strip()[:64] or "anonymous")
        plan = farm.plan(req.workflow, req.params, show=req.show, shot=req.shot, user=user)   # as submit names them
        out = farm.plan_summary(plan)
        if req.publish is not None:
            out["publish"] = farm.publish_preview(plan, req.publish)
        return out

    @app.get("/api/v1/jobs")
    def list_jobs(user: str | None = None, status: str | None = None,
                  active: bool = False, limit: int = Query(200, ge=1, le=5000),
                  parent: str | None = None, ident: Identity = Depends(identity)):
        """Jobs and texture sets, newest first; `parent`: a set's batches, in order."""
        statuses = tuple(s.strip() for s in status.split(",")) if status else (ACTIVE if active else None)
        if parent:
            parent = _job_or_404(parent)["id"]
        return {"jobs": farm.annotate(store.list(user=user, statuses=statuses, limit=limit, parent=parent))}

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str, ident: Identity = Depends(identity)):
        return farm.annotate([_job_or_404(job_id)])[0]

    @app.get("/api/v1/jobs/{job_id}/events")
    def job_events(job_id: str, ident: Identity = Depends(identity)):
        """The job's timeline: each status it went through, where, and for how long."""
        return farm.timeline(_job_or_404(job_id)["id"])

    @app.delete("/api/v1/jobs/{job_id}")
    async def cancel_job(job_id: str, ident: Identity = Depends(identity)):
        job = _job_or_404(job_id)
        _may_touch(ident, job)
        return await farm.cancel(job["id"])

    @app.post("/api/v1/jobs/{job_id}/retry", status_code=201)
    def retry_job(job_id: str, ident: Identity = Depends(identity)):
        job = _job_or_404(job_id)
        _may_touch(ident, job)
        return farm.annotate([farm.retry(job["id"], ident.user if cfg.auth.api_keys else job["user"])])[0]

    # ------------------------------------------------------------ publishing (comfyfarm.publish)
    @app.get("/api/v1/workflows/{name}/publish")
    def publish_options(name: str, ident: Identity = Depends(identity)):
        """What a workflow can be published with: templates, steps, defaults, or why not."""
        return farm.publisher.options(name)

    # ---------------------------------------------------------------- ShotGrid (read only)
    # The publish fields' dropdowns. Plain `def`s: ShotGrid is asked in a thread.
    @app.get("/api/v1/shotgrid")
    def shotgrid_status(ident: Identity = Depends(identity)):
        """Whether the lookups work, or why not."""
        return farm.shotgrid.status()

    @app.get("/api/v1/shotgrid/projects")
    def shotgrid_projects(refresh: bool = False, ident: Identity = Depends(identity)):
        """The active shows: [{code, name}]; `refresh` reloads them from ShotGrid."""
        return farm.shotgrid.projects(refresh)

    @app.get("/api/v1/shotgrid/playlist")
    def shotgrid_playlist(link: str, ident: Identity = Depends(identity)):
        """Batch Submit: a Playlist's Versions in its order, each a row (its link as the input)."""
        return farm.batch_playlist(link)

    @app.get("/api/v1/shotgrid/versions")
    def shotgrid_versions(ids: str, ident: Identity = Depends(identity)):
        """Batch Submit: Versions picked in ShotGrid (ids, comma-separated), each a row."""
        return farm.batch_versions(ids)

    @app.get("/api/v1/shotgrid/query/options")
    def shotgrid_query_options(show: str, refresh: bool = False, ident: Identity = Depends(identity)):
        """Batch Submit's ShotGrid query: what it offers for a show (sequences, shots, the
        filters and their values), or why not; `refresh` asks ShotGrid again."""
        return farm.query_options(show, refresh)

    @app.post("/api/v1/shotgrid/query")
    def shotgrid_query(q: ShotGridQuery, ident: Identity = Depends(identity)):
        """Batch Submit: the Versions a query finds, each a row (its link as the input)."""
        return farm.batch_query(q.model_dump())

    def _menu_settings() -> dict:
        menu_file = Path(cfg.shotgrid.menu_file)
        return SM.load(menu_file if menu_file.is_absolute() else REPO / menu_file)

    def _menu(base_url: str) -> dict:
        s = _menu_settings()
        out = SM.build(list(registry.workflows.values()), farm.publisher.publish_info, s["shows"],
                       s["farm_url"] or base_url, s["menus"])
        out["problems"] = s["problems"] + out["problems"]
        out["publish"] = s["publish"]
        return out

    # ShotGrid's menu made to match by the manager itself (shotgrid.menu_sync): when it
    # starts, when the menu changes, daily; with the menu as it is without a request.
    menu_sync = MenuSync(cfg.shotgrid.menu_sync, lambda: _menu(""), REPO / "tools" / "sg_ami.py",
                         cfg.resolve(cfg.database).parent)

    @app.get("/api/v1/shotgrid/menu")
    def shotgrid_menu(request: Request):
        """ShotGrid's right-click menu as it should be (comfyfarm.shotgrid.menu): its items,
        which workflow goes in which folder and why, and how the manager's own syncing of it
        went (`sync`); tools/sg_ami.py makes ShotGrid's match it. Read only."""
        return {**_menu(str(request.base_url)), "sync": menu_sync.status()}

    @app.post("/api/v1/shotgrid/menu/sync")
    async def shotgrid_menu_sync(ident: Identity = Depends(identity)):
        """Make ShotGrid's menu match the farm's now (shotgrid.menu_sync), rather than at the
        next check: how it went."""
        if not cfg.shotgrid.menu_sync.enabled:
            raise FarmError("The manager doesn't sync ShotGrid's menu: shotgrid.menu_sync.enabled is off in its "
                            "config.yaml (run tools/sg_ami.py instead).", 409)
        if cfg.auth.api_keys and not ident.admin:
            raise HTTPException(403, "Only admins can sync ShotGrid's menu.")
        return {"sync": await menu_sync.check(force=True) or menu_sync.last, **menu_sync.status()}

    # ShotGrid's right-click (Action Menu Items, README: ShotGrid right-click): ShotGrid opens a
    # new tab POSTing the selection here. Open is sent on to the dashboard's deep link, Publish
    # when done ticked for a workflow that publishes (menu.yaml publish:); nothing is read or
    # changed here. With the Submit folders on (menu.yaml menus:), Submit on several Versions
    # opens Batch Submit to send them; on one it submits it, as the ShotGrid user who clicked
    # (else anonymous), then shows the job; when it can't, the page says why, with the form.
    @app.api_route("/api/v1/shotgrid/ami", methods=["GET", "POST"], include_in_schema=False)
    async def shotgrid_ami(request: Request):
        form = dict(request.query_params)
        if request.method == "POST":
            body = await request.body()
            if len(body) > 8 << 20:
                return HTMLResponse(_ami_page("That's more than ShotGrid should send: select fewer."), status_code=413)
            form.update({k: v for k, v in SV.form_fields(request.headers.get("content-type", ""), body).items()
                         if k not in ("workflow", "action")})       # those are the menu item's own
        if not (form.get("entity_type") or form.get("selected_ids")):
            return HTMLResponse(_ami_page("This is where ShotGrid's <b>ComfyFarm</b> right-click menu sends the Versions "
                                          "or Playlist you picked: it submits them, or opens them in the farm's dashboard."))
        try:
            p = SV.picked(form, cfg.shotgrid.url)
        except ValueError as e:
            return HTMLResponse(_ami_page(html.escape(str(e))), status_code=400)
        open_link = "/" + SV.deep_link({**p, "action": "open"})
        wf = registry.workflows.get(p["workflow"] or "")
        if p["workflow"] and wf is None:
            return HTMLResponse(_ami_page(f"There's no workflow <code>{html.escape(p['workflow'])}</code> on the farm "
                                          "(ShotGrid's menu is older than the farm's: sync it again)."), status_code=404)
        if p["action"] == "open" and wf is not None:
            p["publish"] = _menu_settings()["publish"] and farm.publisher.publish_info(wf) is not None
        one = p["kind"] == "Version" and len(p["ids"]) == 1
        if p["action"] == "open" or not one or wf is None:
            return RedirectResponse("/" + SV.deep_link(p), status_code=303)
        # Submit, one Version: straight to the queue.
        if request.method != "POST" or cfg.auth.api_keys:
            # Only ShotGrid's own POST submits (a link can't); with API keys, the dashboard asks for one.
            return RedirectResponse("/" + SV.deep_link(p), status_code=303)
        try:
            job = await asyncio.to_thread(farm.shotgrid_submit, wf.name, p["links"][0], p["user"], p["action"] == "publish")
        except (ParamError, SequenceError, FarmError, PublishError) as e:
            why = str(e) if str(e).startswith(wf.label) else f"{wf.label} wasn't submitted: {e}"
            return HTMLResponse(_ami_page(html.escape(why),
                                          (open_link, "Open it in the dashboard"), "ComfyFarm: not submitted"),
                                status_code=400)
        return RedirectResponse(f"/#queue?job={job['id']}", status_code=303)

    @app.get("/api/v1/shotgrid/projects/{show}/shots")
    def shotgrid_shots(show: str, refresh: bool = False, ident: Identity = Depends(identity)):
        """A show's shots [{code, sequence, status}] and sequences; `refresh` reloads them."""
        return farm.shotgrid.shots(show, refresh)

    @app.post("/api/v1/jobs/{job_id}/publish", status_code=201)
    def publish_job(job_id: str, req: PublishRequest, ident: Identity = Depends(identity)):
        """Publish a finished job through Daiquiri (update_and_render_nuke)."""
        job = _job_or_404(job_id)
        _may_touch(ident, job)
        user = ident.user if cfg.auth.api_keys else ((req.user or ident.user).strip()[:64] or job["user"])
        body = req.model_dump(exclude={"dry_run", "wait", "user"})
        return farm.publisher.start_publish(job["id"], body, user, dry_run=req.dry_run, wait=req.wait)

    @app.get("/api/v1/jobs/{job_id}/publishes")
    def job_publishes(job_id: str, ident: Identity = Depends(identity)):
        """A job's publishes, the latest first, with the command run and its output."""
        return {"publishes": store.publishes(_job_or_404(job_id)["id"])}

    # ---------------------------------------------------------------- publish templates
    # scope: a workflow's name, or _global (every workflow). Anyone may save; every
    # version is kept, so a bad save can be restored.
    def _who(ident: Identity, given: str | None) -> str:
        return ident.user if cfg.auth.api_keys else ((given or ident.user or "").strip()[:64] or "unknown")

    @app.get("/api/v1/publish/templates")
    def publish_templates(ident: Identity = Depends(identity)):
        """Every scope's templates: usable or why not, who saved each last and when."""
        return farm.publisher.template_overview()

    @app.get("/api/v1/publish/templates/{scope}/{name}/history")
    def publish_template_history(scope: str, name: str, ident: Identity = Depends(identity)):
        return {"scope": scope, "name": name, "history": farm.publisher.template_history(scope, name)}

    @app.get("/api/v1/publish/templates/{scope}/{name}/file")
    def publish_template_file(scope: str, name: str, version: str | None = None, ident: Identity = Depends(identity)):
        """The .nk: the one in place, or `version` from its history."""
        data = farm.publisher.template_file(scope, name, version)
        fname = f"{name}{'_' + version if version else ''}.nk"
        return Response(data, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    @app.post("/api/v1/publish/templates/{scope}/{name}", status_code=201)
    def save_publish_template(scope: str, name: str, req: TemplateSave, ident: Identity = Depends(identity)):
        """Save a Nuke script as a publish template (checked like publishing checks it)."""
        return farm.publisher.save_template(scope, name, req.text, _who(ident, req.user))

    @app.post("/api/v1/publish/templates/{scope}/{name}/restore")
    def restore_publish_template(scope: str, name: str, req: TemplateRestore, ident: Identity = Depends(identity)):
        """Put an earlier version back (it becomes the newest; nothing is lost)."""
        return farm.publisher.restore_template(scope, name, req.version, _who(ident, req.user))

    @app.post("/api/v1/jobs/{job_id}/retry-failed")
    def retry_failed(job_id: str, ident: Identity = Depends(identity)):
        """A texture set: run its failed and cancelled batches again, into the same folder."""
        job = _job_or_404(job_id)
        _may_touch(ident, job)
        return farm.annotate([farm.retry_failed(job["id"])])[0]

    @app.get("/api/v1/jobs/{job_id}/prompt")
    def job_prompt(job_id: str, ident: Identity = Depends(identity)):
        """The exact API JSON the machine that ran the job sent to ComfyUI, as it reported it;
        drop it on the ComfyUI canvas to debug. There once the job has started on a machine."""
        job = store.get(job_id, with_prompt=True)
        if not job:
            raise HTTPException(404, "Job not found.")
        return JSONResponse(farm.submitted(job), headers={
            "Content-Disposition": f'attachment; filename="{job["short_id"]}_api.json"'})

    @app.get("/api/v1/jobs/{job_id}/preview")
    def job_preview(job_id: str, key: str | None = None, x_api_key: str | None = Header(None)):
        # <video> tags can't send headers, so the key may come as ?key=
        if cfg.auth.api_keys and not _user_for_key(x_api_key or key):
            raise HTTPException(401, "Missing or unknown API key.")
        job = _job_or_404(job_id)
        path = (job.get("outputs") or {}).get("preview")
        if not path or not os.path.isfile(path):
            raise HTTPException(404, "No preview for this job.")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/v1/workers")
    def workers(ident: Identity = Depends(identity)):
        return {"workers": desk.summaries()}

    @app.get("/api/v1/environments")
    async def environments(ident: Identity = Depends(identity)):
        """Runner environments: the releases in the bank, what each machine has built, what it
        should have and what its environment manager is doing about it (decisions 0028-0030)."""
        releases = await asyncio.to_thread(share.releases, cfg)
        alive = [a for a in list(desk.agents.values()) if desk.alive(a.name)]
        return {"bank": cfg.share.dir or None, "releases": releases, "machines": desk.environments(),
                "wanted": {a.name: a.env_wanted for a in desk.agents.values() if a.env_wanted},
                "work": {a.name: a.env_work for a in alive if a.env_work},
                "rollouts": desk.rollouts.recent(10), "syncs": desk.syncs.recent(10)}

    def _here(request: Request, why: str = "Environments are changed from the manager's host only, until the farm "
                                            "has API keys: run `python -m comfyfarm env ...` there.") -> None:
        if not from_this_host(request.client.host if request.client else None):
            raise HTTPException(403, why)

    @app.get("/api/v1/rollouts")
    def rollouts(ident: Identity = Depends(identity)):
        """Environment rollouts, newest first (decision 0031)."""
        return {"rollouts": desk.rollouts.recent(50)}

    @app.post("/api/v1/environments/{name}/rollout")
    async def start_rollout(name: str, req: RolloutStart, request: Request, ident: Identity = Depends(identity)):
        """A release of the environment to the machines one at a time, the canary first (rollouts.order),
        each gated by its install's check. From the manager's host only (`env rollout` there)."""
        _here(request)
        try:
            release = await asyncio.to_thread(share.release_fingerprint, cfg, name, req.release)
            return desk.rollouts.start(name, release, req.machines, ident.user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/v1/environments/{name}/rollout/{action}")
    def change_rollout(name: str, action: str, request: Request, ident: Identity = Depends(identity)):
        """pause, resume, rollback or cancel the environment's open rollout (rollback: its last)."""
        _here(request)
        try:
            return desk.rollouts.act(name, action, ident.user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/v1/evals")
    def start_evals(req: EvalStart, request: Request, ident: Identity = Depends(identity)):
        """A definition's eval (decision 0053) on each machine named: a job pinned to it, which it
        takes even while drained. From the manager's host only (`comfyfarm eval` there)."""
        _here(request, "Evals are started from the manager's host only, until the farm has API keys: run "
                       "`python -m comfyfarm eval <definition>` there.")
        names = req.machines or sorted(a.name for a in desk.agents.values()
                                       if desk.usable(a) and desk.unmet(a, req.definition) is None)
        if not names:
            raise HTTPException(400, f"no machine checking in may run {req.definition}: name one with --machine")
        try:
            ids = desk.evals.begin_each(req.definition, names, "comfyfarm eval", ident.user)
        except EvalError as e:
            raise HTTPException(400, str(e)) from None
        return {"evals": desk.evals.results(ids)}

    @app.get("/api/v1/evals")
    def evals(jobs: str = Query(..., description="eval job ids, comma-separated"),
              ident: Identity = Depends(identity)):
        """How these evals went: each job's state, and what each check measured (decision 0053)."""
        return {"evals": desk.evals.results([j for j in jobs.split(",") if j.strip()][:50])}

    @app.get("/api/v1/syncs")
    def syncs(ident: Identity = Depends(identity)):
        """The syncs of a runner's environment the manager started, newest first (decision 0033)."""
        return {"syncs": desk.syncs.recent(20)}

    @app.post("/api/v1/syncs")
    def start_sync(req: SyncStart, request: Request, ident: Identity = Depends(identity)):
        """The machines' runner brought to the farm's environment file, one machine at a time,
        the canary first: each drained, synced by its agent, checked, then resumed. From the
        manager's host only (`comfyfarm comfyui sync --all` there, which names its file)."""
        _here(request)
        try:
            return desk.syncs.start(req.runner, req.file, req.machines, req.force, ident.user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/v1/syncs/cancel")
    def cancel_sync(request: Request, ident: Identity = Depends(identity)):
        """No next machine for the running sync. From the manager's host only."""
        _here(request)
        try:
            return desk.syncs.cancel(ident.user)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/v1/environments/{name}/want")
    async def want_environment(name: str, req: EnvironmentWant, request: Request,
                               ident: Identity = Depends(identity)):
        """What the machines should have of an environment: a release, none (removed), or
        forget (no longer managed). From the manager's host only (`env want` there)."""
        _here(request)
        release = None
        if not req.forget and req.release and req.release.lower() != "none":
            try:
                release = await asyncio.to_thread(share.release_fingerprint, cfg, name, req.release)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
        try:
            machines = desk.want_environment(name, release, req.machines, ident.user, forget=req.forget)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"name": name, "release": release, "forget": req.forget, "machines": machines}

    @app.get("/api/v1/job-metrics")
    def job_metrics(since: str | None = None, until: str | None = None, format: str = "json",
                    limit: int = Query(10000, ge=1, le=200000), ident: Identity = Depends(identity)):
        """One row per finished job, kept after the job (decision 0040): who ran what, for which
        show, on which machine and GPU, how it ended and how long each part took. `since` and
        `until`: when it finished, a date, a date and time (the manager's local time) or seconds
        since 1970. `format=csv` for a spreadsheet."""
        if format not in ("json", "csv"):
            raise HTTPException(400, "format is json or csv.")
        try:
            table = MX.rows(store.job_metrics(MX.parse_time(since), MX.parse_time(until), limit))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if format == "csv":
            return Response(MX.to_csv(table), media_type="text/csv; charset=utf-8",
                            headers={"Content-Disposition": 'attachment; filename="comfyfarm-job-metrics.csv"'})
        return {"jobs": table}

    @app.get("/api/v1/usage")
    def usage(days: int = Query(30, ge=1, le=366), by: str = "day", show: str | None = None,
              workflow: str | None = None, ident: Identity = Depends(identity)):
        """Jobs, GPU hours and failures over the last `days` (today included), from job_metrics
        (decision 0050): per day, show, workflow or machine (`by`), for one show or definition
        (`show`, `workflow`) or all."""
        try:
            return US.report(store, registry, days, by, show, workflow)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    started_at = now()

    @app.get("/metrics", include_in_schema=False)
    def metrics(ident: Identity = Depends(identity)):
        """The farm's numbers in Prometheus' text format (decision 0048): the queue, finished
        jobs (job_metrics), the machines and their GPUs, the policy and the sweeper."""
        return Response(PM.render(farm, desk, started_at), media_type=PM.CONTENT_TYPE)

    @app.get("/api/v1/alerts")
    def alerts(ident: Identity = Depends(identity)):
        """What needs a person (decision 0052): the rules, the alerts that hold, the latest
        cleared, and whether Google Chat is set (never its webhook) and its last post went."""
        return desk.alerts.view()

    @app.get("/api/v1/policy")
    def policy(ident: Identity = Depends(identity)):
        """The farm's policy (decisions 0043 to 0047): the file, the rules in force, why the file as
        it is now isn't in force (if it isn't), what is queued against each quota, and the models
        each definition lists."""
        return farm.policy_view()

    @app.get("/api/v1/sweeps")
    def sweeps(ident: Identity = Depends(identity)):
        """The retention sweeper's saved reports, newest first, with their totals (decision 0041)."""
        return {"enabled": cfg.retention.enabled, "keep_days": cfg.retention.keep_days, "at": cfg.retention.at,
                "sweeps": farm.sweeper.reports()}

    @app.get("/api/v1/sweeps/{name}")
    def sweep_report(name: str, format: str = "json", ident: Identity = Depends(identity)):
        """One report (its name, e.g. 2026-11-23, or `latest`): every expired job and the exact
        files the sweeper would delete. `format=text` to read it."""
        try:
            report = farm.sweeper.report(name)
        except (OSError, ValueError) as e:
            raise HTTPException(500, f"Can't read that report: {e}") from None
        if report is None:
            raise HTTPException(404, "No such sweep report." if name != "latest" else "No sweep has run yet.")
        if format == "text":
            return Response(SW.text(report), media_type="text/plain; charset=utf-8")
        return report

    @app.post("/api/v1/sweeps", status_code=201)
    async def run_sweep(request: Request, ident: Identity = Depends(identity)):
        """A sweep now, as the daily one: a dry run that deletes nothing, its report saved.
        From the manager's host only, like everything that will one day delete files."""
        _here(request, "A sweep is started from the manager's host only, until the farm has API keys: "
                       "curl -X POST http://localhost:<port>/api/v1/sweeps there.")
        return await asyncio.to_thread(farm.sweeper.run)

    @app.get("/api/v1/run-times")
    def run_times(ident: Identity = Depends(identity)):
        """How long each workflow takes on each GPU type (what the ETAs come from)."""
        return {"run_times": farm.run_times()}

    @app.get("/api/v1/workers/{name}/gpu-history")
    def gpu_history(name: str, hours: float = Query(24, gt=0, le=24 * 31), ident: Identity = Depends(identity)):
        """A machine's GPU use and memory over the last `hours` (decision 0049): one sample per
        GPU per step, use averaged, memory and jobs at their highest."""
        if name not in desk.agents and not store.gpu_samples(name, 0, 1e12):
            raise HTTPException(404, f"No machine called {name} has checked in.")
        return desk.gpu_history(name, hours)

    @app.post("/api/v1/workers/{name}/drain")
    def drain_worker(name: str, req: DrainRequest, ident: Identity = Depends(identity)):
        """Drain a machine (it finishes what it holds and takes nothing new), or resume it
        (drain: false). Lasts across manager restarts."""
        if cfg.auth.api_keys and not ident.admin:
            raise HTTPException(403, "Admins only.")
        if name not in desk.agents and name not in desk.drained:
            raise HTTPException(404, f"No machine called {name} has checked in.")
        return desk.set_drained(name, req.drain, by=ident.user)

    @app.post("/api/v1/workers/{name}/free-vram")
    def free_vram(name: str, ident: Identity = Depends(identity)):
        """Ask a machine's ComfyUI to unload its models and free its VRAM (decision 0023). Its
        agent does it when the farm has no job in it; the machine's `released` says how it went."""
        if cfg.auth.api_keys and not ident.admin:
            raise HTTPException(403, "Admins only.")
        if name not in desk.agents:
            raise HTTPException(404, f"No machine called {name} has checked in.")
        return desk.request_release(name, by=ident.user)

    @app.post("/api/v1/workers/check")
    async def check_workers(ident: Identity = Depends(identity)):
        """Agents send their ComfyUI's node definitions again (shortly; the reply shows
        the workers as they are now)."""
        if cfg.auth.api_keys and not ident.admin:
            raise HTTPException(403, "Admins only.")
        desk.request_nodes()
        return {"workers": desk.summaries()}

    # ------------------------------------------------------------ agents (protocol.py)
    def agent_auth(x_agent_token: str | None = Header(None)) -> None:
        if cfg.agents.token and x_agent_token != cfg.agents.token:
            raise HTTPException(401, "Unknown agent token (agents.token in the manager's config.yaml).")

    # async: the desk runs on the event loop, with the jobs' state.
    @app.post("/api/v1/agent/ping", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_ping(request: Request):
        return desk.ping(await request.json())

    @app.post("/api/v1/agent/hello", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_hello(request: Request):
        return desk.hello(await request.json())

    @app.post("/api/v1/agent/status", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_status(request: Request):
        return desk.status(await request.json())

    @app.post("/api/v1/agent/nodes", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_nodes(request: Request):
        return desk.nodes(await request.json())

    @app.post("/api/v1/agent/environment", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_environment(request: Request):
        return desk.environment(await request.json())

    @app.post("/api/v1/agent/heartbeat", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_heartbeat(request: Request):
        return await desk.heartbeat(await request.json())

    @app.post("/api/v1/agent/report", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_report(request: Request):
        return desk.report(await request.json())

    @app.websocket("/api/v1/events")
    async def events_ws(ws: WebSocket):
        if cfg.auth.api_keys and not _user_for_key(ws.query_params.get("key")):
            await ws.close(code=4401)
            return
        await ws.accept()
        q = events.subscribe()
        try:
            await ws.send_json({
                "type": "snapshot",
                "jobs": farm.annotate(store.list(limit=200)),
                "workers": desk.summaries(),
            })
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    event = {"type": "ping"}
                await ws.send_json(event)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        except Exception:  # noqa: BLE001 - client went away mid-send
            pass
        finally:
            events.unsubscribe(q)

    return app
