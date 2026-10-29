"""A fake ComfyUI for testing the gateway without a GPU.

Speaks the same HTTP + websocket API as ComfyUI (the parts the gateway uses), "runs"
prompts node by node with progress events, actually lists the input folder the way
VHS Load Images (Path) does, and writes placeholder PNG frames / an MP4 where the
real save nodes would.

    python tools/mock_comfyui.py --port 8188 --output ./mock_output [--delay 0.05]

To make a job fail: put MOCK_FAIL in any text input, or use seed 666.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import sys
import time
import uuid
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

REPO = Path(__file__).resolve().parent.parent
OUTPUT_CLASSES = {"PreviewAny", "VHS_VideoCombine", "Image Save", "ComfyFarmSaveImage", "easy clearCacheAll",
                  "SaveImage"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".ppm", ".bmp", ".pgm", ".tif", ".tiff", ".webp"}


def tiny_png() -> bytes:
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b""))


def build_object_info() -> dict:
    oi: dict = {}
    for tpl in REPO.glob("workflows/*/workflow_api.json"):
        for node in json.loads(tpl.read_text(encoding="utf-8")).values():
            ct = node["class_type"]
            spec = oi.setdefault(ct, {"input": {"required": {}, "optional": {}},
                                      "output_node": ct in OUTPUT_CLASSES, "name": ct})
            for k, v in node["inputs"].items():
                base = k.split(".", 1)[0]
                if isinstance(v, str) and k.endswith("_name"):
                    spec["input"]["required"].setdefault(base, [[], {}])[0].append(v)
                else:
                    spec["input"]["required"].setdefault(base, ["*", {}])
    return oi


def ancestors_of_outputs(prompt: dict) -> list[str]:
    order, seen = [], set()

    def visit(nid):
        if nid in seen or nid not in prompt:
            return
        seen.add(nid)
        for v in prompt[nid]["inputs"].values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                visit(v[0])
        order.append(nid)

    for nid, n in prompt.items():
        if n["class_type"] in OUTPUT_CLASSES:
            visit(nid)
    return order


class Interrupted(Exception):
    pass


class Mock:
    def __init__(self, output: Path, delay: float):
        self.output = output
        self.delay = delay
        self.object_info = build_object_info()
        self.queue: list[dict] = []
        self.running: dict | None = None
        self.history: dict = {}
        self.sockets: dict[str, WebSocket] = {}
        self.number = 0
        self.interrupt_flag = False
        self.wake = asyncio.Event()

    async def send(self, kind: str, data: dict, client_id: str | None) -> None:
        targets = [self.sockets[client_id]] if client_id in self.sockets else (
            list(self.sockets.values()) if client_id is None else [])
        for ws in targets:
            try:
                await ws.send_text(json.dumps({"type": kind, "data": data}))
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> dict:
        return {"exec_info": {"queue_remaining": len(self.queue) + (1 if self.running else 0)}}

    async def run_forever(self) -> None:
        while True:
            if not self.queue:
                self.wake.clear()
                await self.wake.wait()
                continue
            item = self.queue.pop(0)
            self.running = item
            self.interrupt_flag = False
            try:
                await self.execute(item)
            finally:
                self.running = None
                await self.send("status", {"status": self.status()}, None)
                await self.send("executing", {"node": None, "prompt_id": item["id"]}, item["client"])

    def check(self):
        if self.interrupt_flag:
            raise Interrupted

    async def execute(self, item: dict) -> None:
        pid, cid, prompt = item["id"], item["client"], item["prompt"]
        await self.send("execution_start", {"prompt_id": pid}, cid)
        await self.send("execution_cached", {"nodes": [], "prompt_id": pid}, cid)
        outputs, executed, values = {}, [], {}
        node_id = None
        try:
            for node_id in ancestors_of_outputs(prompt):
                self.check()
                node = prompt[node_id]
                ct, inp = node["class_type"], node["inputs"]
                await self.send("executing", {"node": node_id, "display_node": node_id, "prompt_id": pid}, cid)
                await asyncio.sleep(self.delay)
                if any(isinstance(v, str) and "MOCK_FAIL" in v for v in inp.values()) \
                        or (ct == "RandomNoise" and inp.get("noise_seed") == 666):
                    raise RuntimeError("Mock failure requested")
                if ct == "SamplerCustomAdvanced":
                    for step in range(1, 9):
                        self.check()
                        await asyncio.sleep(self.delay)
                        await self.send("progress", {"value": step, "max": 8, "prompt_id": pid, "node": node_id}, cid)
                elif ct == "VHS_LoadImagesPath":
                    d = inp["directory"].strip()
                    names = [n for n in sorted(os.listdir(d))
                             if os.path.splitext(n)[1].lower() in IMG_EXT and os.path.isfile(os.path.join(d, n))]
                    names = names[inp.get("skip_first_images", 0):]
                    if inp.get("image_load_cap"):
                        names = names[: inp["image_load_cap"]]
                    if not names:
                        raise FileNotFoundError(f"No images could be loaded from directory '{d}'.")
                    values["frames"] = len(names)
                elif ct in ("Image Save", "ComfyFarmSaveImage"):
                    out = inp["output_path"] if isinstance(inp["output_path"], str) else str(self.output)
                    os.makedirs(out, exist_ok=True)
                    for i in range(values.get("frames", 1)):
                        Path(out, f"{inp['filename_prefix']}_{i + 1:04d}.png").write_bytes(tiny_png())
                elif ct == "VHS_VideoCombine":
                    sub, _, name = inp["filename_prefix"].replace("\\", "/").rpartition("/")
                    folder = self.output / sub
                    folder.mkdir(parents=True, exist_ok=True)
                    fname = f"{name}_00001.mp4"
                    (folder / fname).write_bytes(b"\x00\x00\x00\x18ftypmp42mock")
                    outputs[node_id] = {"gifs": [{"filename": fname, "subfolder": sub, "type": "output",
                                                  "format": "video/h264-mp4", "fullpath": str(folder / fname)}]}
                    await self.send("executed", {"node": node_id, "output": outputs[node_id], "prompt_id": pid}, cid)
                executed.append(node_id)
        except Interrupted:
            msg = {"prompt_id": pid, "node_id": node_id, "node_type": prompt[node_id]["class_type"],
                   "executed": executed}
            await self.send("execution_interrupted", msg, None)
            self.history[pid] = {"prompt": [0, pid, prompt, {}, []], "outputs": outputs,
                                 "status": {"status_str": "error", "completed": False,
                                            "messages": [["execution_interrupted", msg]]}}
            return
        except Exception as e:  # noqa: BLE001
            msg = {"prompt_id": pid, "node_id": node_id, "node_type": prompt[node_id]["class_type"],
                   "executed": executed, "exception_message": str(e), "exception_type": type(e).__name__,
                   "traceback": [f"Traceback (mock): {e}\n"], "current_inputs": {}, "current_outputs": []}
            await self.send("execution_error", msg, cid)
            self.history[pid] = {"prompt": [0, pid, prompt, {}, []], "outputs": outputs,
                                 "status": {"status_str": "error", "completed": False,
                                            "messages": [["execution_error", msg]]}}
            return
        await self.send("execution_success", {"prompt_id": pid}, cid)
        self.history[pid] = {"prompt": [0, pid, prompt, {}, []], "outputs": outputs,
                             "status": {"status_str": "success", "completed": True,
                                        "messages": [["execution_success", {"prompt_id": pid}]]}}


def create_app(output: Path, delay: float, drop_classes: list[str] | None = None) -> FastAPI:
    m = Mock(output, delay)
    for c in drop_classes or []:   # simulate a missing custom node pack
        m.object_info.pop(c, None)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(m.run_forever())
        yield
        task.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.get("/system_stats")
    def system_stats():
        return {"system": {"comfyui_version": "mock", "python_version": sys.version.split()[0]},
                "devices": [{"name": "Mock RTX 5090", "type": "cuda", "vram_total": 34_000_000_000,
                             "vram_free": 30_000_000_000}]}

    @app.get("/object_info")
    def object_info():
        return m.object_info

    @app.post("/prompt")
    async def post_prompt(req: Request):
        body = await req.json()
        prompt = body.get("prompt") or {}
        missing = {nid: {"errors": [{"message": "Missing node", "details": n["class_type"]}],
                         "class_type": n["class_type"]}
                   for nid, n in prompt.items() if n.get("class_type") not in m.object_info}
        if missing:
            return JSONResponse({"error": {"type": "invalid_prompt", "message": "Cannot execute because a node is missing"},
                                 "node_errors": missing}, status_code=400)
        pid = body.get("prompt_id") or str(uuid.uuid4())
        m.number += 1
        m.queue.append({"id": pid, "client": body.get("client_id"), "prompt": prompt, "number": m.number})
        m.wake.set()
        await m.send("status", {"status": m.status()}, None)
        return {"prompt_id": pid, "number": m.number, "node_errors": {}}

    @app.get("/queue")
    def get_queue():
        def row(i):
            return [i["number"], i["id"], {}, {}, []]
        return {"queue_running": [row(m.running)] if m.running else [],
                "queue_pending": [row(i) for i in m.queue]}

    @app.post("/queue")
    async def post_queue(req: Request):
        body = await req.json()
        if body.get("clear"):
            m.queue.clear()
        for pid in body.get("delete", []):
            m.queue[:] = [i for i in m.queue if i["id"] != pid]
        return Response(status_code=200)

    @app.post("/interrupt")
    async def interrupt(req: Request):
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            body = {}
        pid = body.get("prompt_id")
        if m.running and (not pid or m.running["id"] == pid):
            m.interrupt_flag = True
        return Response(status_code=200)

    @app.get("/history/{pid}")
    def history_one(pid: str):
        return {pid: m.history[pid]} if pid in m.history else {}

    @app.post("/history")
    async def history_post(req: Request):
        body = await req.json()
        if body.get("clear"):
            m.history.clear()
        for pid in body.get("delete", []):
            m.history.pop(pid, None)
        return Response(status_code=200)

    @app.get("/view")
    def view(filename: str, subfolder: str = "", type: str = "output"):  # noqa: A002
        path = (m.output / subfolder / filename).resolve()
        if m.output.resolve() not in path.parents or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path)

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        cid = socket.query_params.get("clientId") or uuid.uuid4().hex
        m.sockets[cid] = socket
        try:
            await socket.send_text(json.dumps({"type": "status", "data": {"status": m.status(), "sid": cid}}))
            while True:
                await socket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            if m.sockets.get(cid) is socket:
                del m.sockets[cid]

    app.state.mock = m
    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8188)
    ap.add_argument("--output", default="mock_output")
    ap.add_argument("--delay", type=float, default=0.05, help="seconds per node / sampler step")
    ap.add_argument("--drop-class", action="append", default=[], help="pretend this node class isn't installed")
    a = ap.parse_args()
    out = Path(a.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    print(f"mock ComfyUI on http://{a.host}:{a.port}  output: {out}  started {time.strftime('%H:%M:%S')}")
    uvicorn.run(create_app(out, a.delay, a.drop_class), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
