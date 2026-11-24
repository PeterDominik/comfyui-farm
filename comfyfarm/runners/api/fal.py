"""fal.ai, for the API runner (decision 0056): its queue for jobs, its CDN for inputs.

    submit   POST {queue}/{endpoint}                              the request's id
    status   GET  {queue}/{owner}/{app}/requests/{id}/status
    result   GET  {queue}/{owner}/{app}/requests/{id}
    cancel   PUT  {queue}/{owner}/{app}/requests/{id}/cancel
    upload   POST {rest}/storage/auth/token?storage_type=fal-cdn-v3, then POST {cdn}/files/upload

Every call but the upload itself carries the requests key (`Authorization: Key ...`). The
status, result and cancel URLs are made here from the endpoint's owner and app and the
request's id, never taken from an answer: the key goes to fal.ai's own hosts only, and a run
is followed again after a restart from its id alone. Results are public files on fal.ai's
CDN, downloaded without the key.

What the gateway uploads and what fal.ai makes for it are kept media_seconds (gateway.yaml),
asked for with X-Fal-Object-Lifecycle-Preference on the upload and on the job.

Until phase 11's step 5 (after step 4's ledger and caps) the gateway sends nothing to fal.ai
itself: REAL_SERVICE is off, and every address must be on this machine, a stand-in such as
tools/mock_fal.py. Step 5 switches it on in a reviewed change."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import ApiError

QUEUE, REST, CDN = "https://queue.fal.run", "https://rest.fal.ai", "https://v3.fal.media"
REAL_SERVICE = False                    # phase 11's step 5 turns this on
NEEDS = ("requests",)                   # the billing key comes with the ledger (step 4)
LIFECYCLE = "X-Fal-Object-Lifecycle-Preference"
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                 ".mp4": "video/mp4"}
FORMATS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp"}    # writes.frames.ext -> output_format
MAX_UPLOAD = 100 * 2**20                # fal.ai's own client sends bigger files in parts; the gateway doesn't
MAX_DOWNLOAD = 2 * 2**30
REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,100}")
OFF = ("the farm doesn't send jobs to fal.ai itself yet: that comes with phase 11's step 5, after the ledger and "
       "caps. Until then the gateway uses a stand-in on this machine: set providers.fal.url in gateway.yaml to it, "
       "e.g. http://127.0.0.1:8189 (python tools/mock_fal.py --port 8189)")


def local(url: str) -> bool:
    """An address on this machine."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check(wf) -> list[str]:
    """What's wrong with an API definition for fal.ai, whichever machine looks."""
    problems = []
    api = wf.api or {}
    # fal.ai names a field that takes several URLs ..._urls, and one that takes one ..._url.
    fed: dict[str, list[str]] = {}
    for param, field in (api.get("upload") or {}).items():
        fed.setdefault(field, []).append(param)
    for field, params in fed.items():
        many = len(params) > 1 or any(getattr(wf.ports.get(p), "takes", "images") == "images" for p in params)
        if field.endswith("_url") and many:
            problems.append(f"runner.upload: {field} takes one URL, and {' and '.join(params)} can give more "
                            f"(fal.ai names a field that takes several ..._urls)")
    ext = (wf.frames_ext or "").lower()
    if not ext:
        problems.append("writes: an API definition for fal.ai writes frames, e.g. writes: {frames: {ext: png}}")
    elif ext not in FORMATS:
        problems.append(f"writes.frames.ext: fal.ai makes png, jpeg or webp images, not {ext}")
    else:
        fmt = (api.get("fixed") or {}).get("output_format")
        if fmt is None and FORMATS[ext] != "png":
            problems.append(f"runner.fixed: add output_format: {FORMATS[ext]}, as writes.frames.ext is {ext}")
        elif fmt is not None and FORMATS.get(str(fmt).lower()) != FORMATS[ext]:
            problems.append(f"runner.fixed.output_format is {fmt} and writes.frames.ext is {ext}: make them the same")
    if "output_format" in (api.get("send") or {}).values():
        problems.append("runner.send: output_format is the farm's (runner.fixed, as writes.frames.ext), "
                        "not the artist's")
    return problems


def _detail(r: httpx.Response) -> str:
    """What fal.ai said was wrong, from its answer's detail: one line per problem."""
    try:
        d = r.json()
    except ValueError:
        return ""
    detail = d.get("detail") if isinstance(d, dict) else None
    if isinstance(detail, list):
        lines = []
        for item in detail[:5]:
            if not isinstance(item, dict):
                continue
            loc = ".".join(str(x) for x in item.get("loc") or [] if x != "body")
            msg = str(item.get("msg") or item.get("type") or "?")
            if item.get("type") == "content_policy_violation":
                msg += " (fal.ai's content check)"
            lines.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(lines)
    if isinstance(detail, str):
        return detail
    if isinstance(d, dict) and isinstance(d.get("error"), str):
        return d["error"]
    return ""


def _until(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 600


class Adapter:
    """fal.ai, as one gateway's keys reach it. The key is held here and sent in the
    Authorization header to fal.ai's queue and REST hosts: nothing it returns or logs holds it."""

    def __init__(self, keys: dict, url: str = "", media_seconds: int = 3600, transport=None, timeout: float = 60.0):
        self._key = str(keys.get("requests") or "")
        base = url.rstrip("/")
        self.stand_in = bool(base)
        self.queue, self.rest, self.cdn = (base, base, base) if base else (QUEUE, REST, CDN)
        self.media_seconds = int(media_seconds)
        self._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout, connect=10.0),
                                         follow_redirects=False)
        self._token: tuple[str, str, float] | None = None      # the CDN's: its header, its address, until when

    def __repr__(self) -> str:
        return f"<fal.ai adapter {self.queue}>"

    def problems(self) -> list[str]:
        """Why it can't send anything. Its keys are the runner's to check (NEEDS)."""
        return [] if REAL_SERVICE or (self.stand_in and local(self.queue)) else [OFF]

    # ---------------------------------------------------------------- calls
    def _may_send(self, url: str, to: str | None = None) -> bool:
        """Whether a call may go to this address: `to` key (the requests key goes with it), cdn
        (the upload token does) or None (nothing secret: a result's file)."""
        parts = urlsplit(url)
        if self.stand_in or not REAL_SERVICE:
            return self.stand_in and local(url) and (to is None or parts.netloc == urlsplit(self.queue).netloc)
        if parts.scheme != "https":
            return False
        if to == "key":
            return parts.netloc in (urlsplit(self.queue).netloc, urlsplit(self.rest).netloc)
        if to == "cdn":
            host = parts.hostname or ""
            return host == "fal.media" or host.endswith(".fal.media")
        return True

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***") if self._key else text

    async def _call(self, method: str, url: str, auth: str | None = None, to: str | None = None, **kw):
        if not self._may_send(url, to):
            why = OFF if not REAL_SERVICE else "it isn't one of fal.ai's addresses"
            raise ApiError(f"the gateway won't send anything to {urlsplit(url).netloc or url}: {why}")
        headers = dict(kw.pop("headers", None) or {})
        if auth:
            headers["Authorization"] = auth
        try:
            return await self._client.request(method, url, headers=headers, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            raise ApiError(f"can't reach fal.ai at {urlsplit(url).netloc}: {self._scrub(str(e))[:200]}",
                           "transient") from None
        except httpx.TimeoutException:
            raise ApiError(f"fal.ai at {urlsplit(url).netloc} didn't answer in time", "unknown") from None
        except httpx.HTTPError as e:
            raise ApiError(f"the connection to fal.ai failed ({type(e).__name__})", "unknown") from None

    def _refusal(self, r: httpx.Response, doing: str) -> ApiError:
        detail = self._scrub(_detail(r))[:600]
        code = r.status_code
        if code in (401, 403):
            return ApiError(f"fal.ai refused the gateway's key while {doing} ({code}): check keys.requests in "
                            f"gateway.yaml on the gateway")
        if code == 429:
            return ApiError(f"fal.ai is busy (429, too many requests) while {doing}", "transient")
        if code >= 500:
            return ApiError(f"fal.ai had a problem while {doing} ({code}{': ' + detail if detail else ''})",
                            "transient")
        if detail:
            return ApiError(f"fal.ai refused it: {detail}")
        return ApiError(f"fal.ai answered {code} while {doing}")

    def _keyed(self) -> str:
        return f"Key {self._key}"

    def _lifecycle(self) -> str:
        return json.dumps({"expiration_duration_seconds": self.media_seconds})

    @staticmethod
    def _json(r: httpx.Response, doing: str) -> dict:
        try:
            d = r.json()
        except ValueError:
            d = None
        if not isinstance(d, dict):
            raise ApiError(f"fal.ai's answer while {doing} wasn't what the gateway reads (not JSON)", "transient")
        return d

    def _url(self, request: dict) -> str:
        owner, app = request["endpoint"].split("/")[:2]
        return f"{self.queue}/{owner}/{app}/requests/{request['id']}"

    # ---------------------------------------------------------------- inputs
    async def _cdn(self) -> tuple[str, str]:
        """fal.ai's CDN token (asked with the key) and where to upload with it."""
        if self._token and self._token[2] > time.time() + 60:
            return self._token[0], self._token[1]
        r = await self._call("POST", f"{self.rest}/storage/auth/token?storage_type=fal-cdn-v3", auth=self._keyed(),
                             to="key", json={})
        if not r.is_success:
            raise self._refusal(r, "asking for an upload token")
        d = self._json(r, "asking for an upload token")
        token, kind, base = d.get("token"), d.get("token_type"), d.get("base_url")
        if not (isinstance(token, str) and isinstance(kind, str) and isinstance(base, str) and token):
            raise ApiError("fal.ai's upload token wasn't what the gateway reads", "transient")
        base = base.rstrip("/")
        if not self._may_send(base, "cdn"):
            raise ApiError(f"fal.ai said to upload to {urlsplit(base).netloc or base}, which isn't its CDN")
        self._token = (f"{kind} {token}", base, _until(d.get("expires_at")))
        return self._token[0], self._token[1]

    async def upload(self, path: str, content_type: str) -> str:
        name = os.path.basename(path)
        size = os.path.getsize(path)
        if size > MAX_UPLOAD:
            raise ApiError(f"{name} is {size / 2**20:.0f} MB; the gateway uploads files of up to "
                           f"{MAX_UPLOAD // 2**20} MB")
        data = await asyncio.to_thread(Path(path).read_bytes)
        headers = {"Content-Type": content_type, LIFECYCLE: self._lifecycle(),
                   "X-Fal-File-Name": name.encode("ascii", "replace").decode("ascii")}
        for again in (True, False):
            auth, base = await self._cdn()
            r = await self._call("POST", f"{base}/files/upload", auth=auth, to="cdn", content=data, headers=headers)
            if r.status_code == 401 and again:
                self._token = None      # it ran out: ask for another, once
                continue
            break
        if not r.is_success:
            raise self._refusal(r, f"uploading {name}")
        url = self._json(r, f"uploading {name}").get("access_url")
        if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
            raise ApiError(f"fal.ai's CDN didn't say where it put {name}", "transient")
        return url

    @staticmethod
    def body(api: dict, values: dict, urls: dict) -> dict:
        """What fal.ai is sent: the fixed fields, each sent param under its field, and each
        uploaded field: the URLs of its inputs in the params' order (`urls` is in that order),
        a list for an ..._urls field, one URL for an ..._url one. An empty param or input
        isn't sent: fal.ai uses its default."""
        out = dict(api.get("fixed") or {})
        for param, field in (api.get("send") or {}).items():
            v = values.get(param)
            if v is not None and v != "":
                out[field] = v
        fields: dict[str, list[str]] = {}
        for param, us in urls.items():
            fields.setdefault(api["upload"][param], []).extend(us)
        for field, us in fields.items():
            if us:
                out[field] = us[0] if field.endswith("_url") else us
        return out

    # ---------------------------------------------------------------- the job
    async def submit(self, endpoint: str, body: dict) -> dict:
        """Put the job in fal.ai's queue. Asked again only when it certainly wasn't sent (no
        connection) or fal.ai said it was too busy to take it."""
        doing = "sending the job"
        for attempt in range(3):
            try:
                r = await self._call("POST", f"{self.queue}/{endpoint}", auth=self._keyed(), to="key", json=body,
                                     headers={LIFECYCLE: self._lifecycle()})
            except ApiError as e:
                if e.kind == "transient" and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise
            if r.status_code == 429 and attempt < 2:
                await asyncio.sleep(2 ** attempt)
                continue
            break
        if not r.is_success:
            e = self._refusal(r, doing)
            e.kind = "refused"          # fal.ai didn't take it
            raise e
        rid = self._json(r, doing).get("request_id")
        if not (isinstance(rid, str) and REQUEST_ID.fullmatch(rid)):
            raise ApiError("fal.ai took the job but its answer had no request id the gateway reads", "unknown")
        return {"id": rid, "endpoint": endpoint}

    async def status(self, request: dict) -> dict:
        r = await self._call("GET", f"{self._url(request)}/status", auth=self._keyed(), to="key")
        if r.status_code == 404:
            raise ApiError(f"fal.ai has no request {request['id']}: it belongs to another account, or the keys "
                           f"changed")
        if not r.is_success:
            raise self._refusal(r, "asking how the job is doing")
        d = self._json(r, "asking how the job is doing")
        st = d.get("status")
        if st == "IN_QUEUE":
            pos = d.get("queue_position")
            return {"state": "queued", "position": pos if isinstance(pos, int) else None}
        if st == "COMPLETED":
            if d.get("error"):
                return {"state": "failed", "error": self._scrub(str(d["error"]))[:600],
                        "error_type": str(d.get("error_type") or "")}
            metrics = d.get("metrics") if isinstance(d.get("metrics"), dict) else {}
            return {"state": "done", "metrics": metrics}
        return {"state": "running"}     # IN_PROGRESS, or a state this gateway doesn't know yet

    async def result(self, request: dict) -> dict:
        doing = "fetching what it made"
        r = await self._call("GET", self._url(request), auth=self._keyed(), to="key")
        if r.status_code == 400 and "progress" in _detail(r).lower():
            raise ApiError("fal.ai hasn't finished it yet", "transient")
        if not r.is_success:
            raise self._refusal(r, doing)
        return self._json(r, doing)

    @staticmethod
    def outputs(result: dict) -> list[str]:
        files = []
        for key in ("images", "image"):
            v = result.get(key)
            for x in v if isinstance(v, list) else [v] if isinstance(v, dict) else []:
                if isinstance(x, dict) and isinstance(x.get("url"), str):
                    files.append(x["url"])
        return files

    async def cancel(self, request: dict) -> str:
        try:
            r = await self._call("PUT", f"{self._url(request)}/cancel", auth=self._keyed(), to="key")
        except ApiError:
            return "unknown"
        return {200: "removed", 202: "requested", 400: "completed"}.get(r.status_code, "unknown")

    async def download(self, url: str, dest: str) -> None:
        if url.startswith("data:"):     # sync_mode: the file in the answer itself
            head, _, data = url.partition(",")
            if ";base64" not in head:
                raise ApiError("fal.ai sent a file the gateway can't read (a data: URL not in base64)")
            raw = base64.b64decode(data)
            await asyncio.to_thread(Path(dest).write_bytes, raw)
            return
        if not self._may_send(url):
            why = OFF if not REAL_SERVICE else "fal.ai's files are on https"
            raise ApiError(f"the gateway won't download from {urlsplit(url).netloc or url}: {why}")
        try:
            async with self._client.stream("GET", url) as r:
                if not r.is_success:
                    await r.aread()
                    raise self._refusal(r, "downloading what it made")
                got = 0
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        got += len(chunk)
                        if got > MAX_DOWNLOAD:
                            raise ApiError(f"what fal.ai made is over {MAX_DOWNLOAD // 2**30} GB; the gateway "
                                           f"stopped downloading it")
                        f.write(chunk)
        except httpx.HTTPError as e:
            raise ApiError(f"downloading what fal.ai made failed ({type(e).__name__})", "transient") from None

    async def close(self) -> None:
        await self._client.aclose()
