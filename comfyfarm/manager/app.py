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
from ..workflows import ParamError, WorkflowRegistry
from .db import ACTIVE, JobStore
from .desk import Desk
from .events import EventBus
from . import share
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
    registry = WorkflowRegistry(cfg.resolve(cfg.workflows_dir))
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
        plan = farm.plan(req.workflow, req.params, show=req.show, shot=req.shot)
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
        """The exact API JSON sent to ComfyUI; drop it on the ComfyUI canvas to debug."""
        job = store.get(job_id, with_prompt=True)
        if not job:
            raise HTTPException(404, "Job not found.")
        return JSONResponse(job["prompt"], headers={
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

    @app.get("/api/v1/run-times")
    def run_times(ident: Identity = Depends(identity)):
        """How long each workflow takes on each GPU type (what the ETAs come from)."""
        return {"run_times": farm.run_times()}

    @app.post("/api/v1/workers/{name}/drain")
    def drain_worker(name: str, req: DrainRequest, ident: Identity = Depends(identity)):
        """Drain a machine (it finishes what it holds and takes nothing new), or resume it
        (drain: false). Lasts across manager restarts."""
        if cfg.auth.api_keys and not ident.admin:
            raise HTTPException(403, "Admins only.")
        if name not in desk.agents and name not in desk.drained:
            raise HTTPException(404, f"No machine called {name} has checked in.")
        return desk.set_drained(name, req.drain, by=ident.user)

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
