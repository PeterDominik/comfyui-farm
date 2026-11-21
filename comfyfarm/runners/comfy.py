"""Thin client for one ComfyUI server's HTTP API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx


class ComfyRejected(Exception):
    """ComfyUI refused the prompt (validation failed). body = ComfyUI's JSON reply."""

    def __init__(self, body: dict):
        super().__init__(str(body.get("error")))
        self.body = body


def describe_rejection(body: dict, titles: dict[str, str]) -> str:
    err = body.get("error")
    msg = err.get("message") if isinstance(err, dict) else str(err or "Prompt rejected")
    parts = [msg]
    for node_id, ne in (body.get("node_errors") or {}).items():
        for e in ne.get("errors", []):
            detail = e.get("details") or e.get("message") or ""
            parts.append(f"{titles.get(node_id, node_id)} ({ne.get('class_type', '?')}): {detail}")
    return " | ".join(parts)


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base, timeout=timeout)

    def ws_url(self, client_id: str) -> str:
        scheme = "wss" if self.base.startswith("https") else "ws"
        return f"{scheme}://{self.base.split('://', 1)[1]}/ws?clientId={client_id}"

    async def close(self) -> None:
        await self.http.aclose()

    async def system_stats(self) -> dict:
        r = await self.http.get("/system_stats", timeout=5)
        r.raise_for_status()
        return r.json()

    async def free(self, unload_models: bool = True, free_memory: bool = True) -> None:
        """Ask ComfyUI to unload its models and empty torch's cache. It does so once its queue
        is idle; /system_stats then shows torch holding less (torch_vram_total)."""
        r = await self.http.post("/free", json={"unload_models": unload_models, "free_memory": free_memory})
        r.raise_for_status()

    async def object_info(self) -> dict:
        r = await self.http.get("/object_info", timeout=120)
        r.raise_for_status()
        return r.json()

    async def submit(self, prompt: dict, prompt_id: str, client_id: str,
                     extra: dict | None = None) -> tuple[str, dict]:
        """Returns (prompt_id, node_errors). ComfyUI still queues a prompt when only some
        of its outputs fail validation, so a non-empty node_errors needs handling."""
        body = {"prompt": prompt, "prompt_id": prompt_id, "client_id": client_id,
                "extra_data": extra or {}}
        r = await self.http.post("/prompt", json=body)
        if r.status_code == 400:
            raise ComfyRejected(r.json())
        r.raise_for_status()
        data = r.json()
        # Older ComfyUI versions ignore a client-supplied id and mint their own.
        return data.get("prompt_id") or prompt_id, data.get("node_errors") or {}

    async def queue(self) -> tuple[set[str], set[str]]:
        r = await self.http.get("/queue")
        r.raise_for_status()
        q = r.json()
        running = {item[1] for item in q.get("queue_running", [])}
        pending = {item[1] for item in q.get("queue_pending", [])}
        return running, pending

    async def history(self, prompt_id: str) -> dict | None:
        r = await self.http.get(f"/history/{prompt_id}")
        r.raise_for_status()
        return r.json().get(prompt_id)

    async def delete_history(self, prompt_ids: list[str]) -> None:
        await self.http.post("/history", json={"delete": prompt_ids})

    async def delete_queued(self, prompt_ids: list[str]) -> None:
        r = await self.http.post("/queue", json={"delete": prompt_ids})
        r.raise_for_status()

    async def interrupt(self, prompt_id: str) -> None:
        # Targeted interrupt: ComfyUI only stops it if this prompt is the one running.
        r = await self.http.post("/interrupt", json={"prompt_id": prompt_id})
        r.raise_for_status()

    async def download(self, filename: str, subfolder: str, kind: str, dest: Path) -> None:
        params = {"filename": filename, "subfolder": subfolder, "type": kind}
        tmp = dest.with_name(dest.name + ".part")
        async with self.http.stream("GET", "/view", params=params, timeout=300) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                async for chunk in r.aiter_bytes(1 << 20):
                    await asyncio.to_thread(fh.write, chunk)   # may be a network share
        tmp.replace(dest)
