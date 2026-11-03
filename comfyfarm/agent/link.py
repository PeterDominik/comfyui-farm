"""How an agent reaches the manager (protocol.py): in the same process, or over HTTP.

Both carry the same JSON messages and raise LinkError when the manager can't be reached
or refuses the call."""

from __future__ import annotations

import json

import httpx

from ..protocol import LinkError


def _copy(x):
    """What goes over the wire: JSON, so neither side keeps a reference to the other's data."""
    return json.loads(json.dumps(x))


class LocalLink:
    """The manager's desk in this process (`standalone`)."""

    url = "in this process"

    def __init__(self, desk):
        self.desk = desk

    async def hello(self, msg: dict) -> dict:
        return _copy(self.desk.hello(_copy(msg)))

    async def status(self, msg: dict) -> dict:
        return _copy(self.desk.status(_copy(msg)))

    async def nodes(self, msg: dict) -> dict:
        return _copy(self.desk.nodes(msg))     # object_info is large and only read: not copied

    async def heartbeat(self, msg: dict) -> dict:
        return _copy(await self.desk.heartbeat(_copy(msg)))

    async def report(self, msg: dict) -> dict:
        return _copy(self.desk.report(_copy(msg)))

    async def close(self) -> None:
        pass


class HttpLink:
    """The manager's /api/v1/agent/... endpoints."""

    def __init__(self, url: str, token: str = ""):
        self.url = url.rstrip("/")
        headers = {"X-Agent-Token": token} if token else {}
        self.http = httpx.AsyncClient(base_url=self.url + "/api/v1/agent", timeout=30.0, headers=headers)

    async def _post(self, path: str, msg: dict, timeout: float = 30.0) -> dict:
        try:
            r = await self.http.post(path, json=msg, timeout=timeout)
        except httpx.HTTPError as e:
            raise LinkError(f"{self.url}: {type(e).__name__}: {e}") from None
        if r.status_code == 401:
            raise LinkError(f"{self.url} refused the agent token (token in agent.yaml must be the manager's "
                            f"agents.token)")
        if r.status_code >= 400:
            raise LinkError(f"{self.url}{path}: HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            raise LinkError(f"{self.url}{path}: not a JSON reply") from None

    async def hello(self, msg: dict) -> dict:
        return await self._post("/hello", msg)

    async def status(self, msg: dict) -> dict:
        return await self._post("/status", msg)

    async def nodes(self, msg: dict) -> dict:
        return await self._post("/nodes", msg, timeout=120.0)

    async def heartbeat(self, msg: dict) -> dict:
        return await self._post("/heartbeat", msg, timeout=float(msg.get("wait") or 0) + 30.0)

    async def report(self, msg: dict) -> dict:
        return await self._post("/report", msg)

    async def close(self) -> None:
        await self.http.aclose()


__all__ = ["LocalLink", "HttpLink", "LinkError"]
