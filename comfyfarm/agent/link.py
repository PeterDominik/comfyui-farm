"""How an agent reaches the manager (protocol.py): in the same process, or over HTTP.

Both carry the same JSON messages and raise LinkError when the manager can't be reached
or refuses the call."""

from __future__ import annotations

import json
import os

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

    async def ping(self, msg: dict) -> dict:
        return _copy(self.desk.ping(_copy(msg)))

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
    """The manager's /api/v1/agent/... endpoints. `address` is the manager's URL, or the
    manager.json it writes in the farm's shared folder: read before each call (only when it
    changed), so an agent follows the manager when it moves."""

    def __init__(self, address: str, token: str = ""):
        self.address = address.strip()
        self.token = token
        self.from_file = not self.address.startswith(("http://", "https://"))
        self.url = self.address.rstrip("/") if not self.from_file else ""
        self.http: httpx.AsyncClient | None = None
        self._mtime: float | None = None

    @property
    def shown(self) -> str:
        return f"{self.url or '?'} (from {self.address})" if self.from_file else self.url

    async def _client(self) -> httpx.AsyncClient:
        if self.from_file:
            try:
                mtime = os.stat(self.address).st_mtime
                if mtime != self._mtime:
                    with open(self.address, encoding="utf-8") as fh:
                        url = str(json.load(fh).get("url") or "").rstrip("/")
                    if not url.startswith(("http://", "https://")):
                        raise ValueError("it has no url")
                    self._mtime = mtime
                    if url != self.url:
                        await self.close()
                        self.url = url
            except (OSError, ValueError, AttributeError) as e:
                if not self.url:
                    raise LinkError(f"can't read the manager's address from {self.address}: "
                                    f"{getattr(e, 'strerror', None) or e}") from None
                # Keep the address read last time (the share may be briefly unreachable).
        if self.http is None:
            headers = {"X-Agent-Token": self.token} if self.token else {}
            self.http = httpx.AsyncClient(base_url=self.url + "/api/v1/agent", timeout=30.0, headers=headers)
        return self.http

    async def _post(self, path: str, msg: dict, timeout: float = 30.0) -> dict:
        http = await self._client()
        try:
            r = await http.post(path, json=msg, timeout=timeout)
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

    async def ping(self, msg: dict) -> dict:
        """Reachable, and the token accepted? (`comfyfarm agent --check`; registers nothing.)"""
        return await self._post("/ping", msg)

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
        if self.http is not None:
            http, self.http = self.http, None
            await http.aclose()


__all__ = ["LocalLink", "HttpLink", "LinkError"]
