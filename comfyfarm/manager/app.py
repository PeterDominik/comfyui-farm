"""The manager's REST + websocket API, the dashboard, and the endpoints agents check in
at (/api/v1/agent/..., protocol.py)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Config
from ..fileio.errors import SequenceError
from ..fileio.naming import default_output_dir_display
from ..workflows import ParamError, WorkflowRegistry
from .db import ACTIVE, JobStore
from .desk import Desk
from .events import EventBus
from .farm import Farm, FarmError

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


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
        await farm.start()
        for a in agents:
            a.start()
        yield
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
    async def farm_error(_req: Request, exc: FarmError):
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
    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(HERE / "static" / "index.html")

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
            out.append(s)
        return {"workflows": out, "load_errors": registry.load_errors}

    @app.post("/api/v1/jobs", status_code=201)
    def submit(req: JobRequest, ident: Identity = Depends(identity)):
        # A plain `def` endpoint runs in a thread: validation reads the show storage.
        user = ident.user if cfg.auth.api_keys else ((req.user or ident.user).strip()[:64] or "anonymous")
        if req.priority > 0 and cfg.auth.api_keys and not ident.admin:
            raise HTTPException(403, "Only admins can submit with priority above 0.")
        job = farm.submit(req.workflow, req.params, user, name=req.name, priority=req.priority,
                          show=req.show, shot=req.shot)
        return farm.annotate([job])[0]

    @app.post("/api/v1/plan")
    def plan(req: JobRequest, ident: Identity = Depends(identity)):
        """Dry run: where a job with these params would write, without queuing it."""
        return farm.plan_summary(farm.plan(req.workflow, req.params, show=req.show, shot=req.shot))

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
    @app.post("/api/v1/agent/hello", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_hello(request: Request):
        return desk.hello(await request.json())

    @app.post("/api/v1/agent/status", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_status(request: Request):
        return desk.status(await request.json())

    @app.post("/api/v1/agent/nodes", include_in_schema=False, dependencies=[Depends(agent_auth)])
    async def agent_nodes(request: Request):
        return desk.nodes(await request.json())

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
