"""fal.ai, for the API runner (decision 0056): its queue for jobs, its CDN for inputs.

    submit   POST {queue}/{endpoint}                              the request's id
    status   GET  {queue}/{owner}/{app}/requests/{id}/status
    result   GET  {queue}/{owner}/{app}/requests/{id}
    cancel   PUT  {queue}/{owner}/{app}/requests/{id}/cancel
    upload   POST {rest}/storage/auth/token?storage_type=fal-cdn-v3, then POST {cdn}/files/upload
    prices   GET  {api}/v1/models/pricing?endpoint_id=a,b         each endpoint's unit price (0059)
    describe GET  {api}/v1/models?endpoint_id=e&expand=openapi-3.0  an endpoint's name and schema (0065)
    bill     GET  {api}/v1/models/billing-events?start=&end=      what fal.ai charged, per request (0061)
    balance  GET  {api}/v1/account/billing?expand=credits         the account's credit balance (0061)

Every call but the upload itself carries the requests key (`Authorization: Key ...`), but
the bill's and the balance's, which carry the billing key (fal.ai's BILLING preset): without
one the bill isn't read, and the manager's ledger keeps its estimates. The
status, result and cancel URLs are made here from the endpoint's owner and app and the
request's id, never taken from an answer: the key goes to fal.ai's own hosts only, and a run
is followed again after a restart from its id alone. Results are public files on fal.ai's
CDN, downloaded without the key.

What the gateway uploads and what fal.ai makes for it are kept media_seconds (gateway.yaml),
asked for with X-Fal-Object-Lifecycle-Preference on the upload and on the job. Each job is
also sent with X-Fal-Store-IO: 0, so fal.ai keeps neither its inputs nor its outputs in the
account's request history (unless keep_payloads), and x-app-fal-disable-fallback, so fal.ai
never runs it on another endpoint than the reviewed definition's (decision 0062).

The gateway sends jobs to fal.ai itself only with `live: true` for fal in gateway.yaml, which
phase 11's step 5 brought (decision 0062): fal.ai charges for them. Otherwise every address
must be on this machine, a stand-in such as tools/mock_fal.py, as in every test."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import ApiError

QUEUE, REST, CDN, API = "https://queue.fal.run", "https://rest.fal.ai", "https://v3.fal.media", "https://api.fal.ai"
NEEDS = ("requests",)                   # the billing key is for the bill alone (decision 0061)
BILL_PAGE = 1000                        # billing events a page; fal.ai gives up to 10,000
BILL_PAGES = 200                        # pages a read, at most: a read never runs on forever
LIFECYCLE = "X-Fal-Object-Lifecycle-Preference"
LIFECYCLE_UPLOAD = "X-Fal-Object-Lifecycle"     # fal.ai's own clients send both with an upload
STORE_IO = "X-Fal-Store-IO"                     # "0": the job's inputs and outputs aren't kept in its request history
NO_FALLBACK = "x-app-fal-disable-fallback"      # "true": never run on another endpoint than the one asked
CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                 ".mp4": "video/mp4"}
FORMATS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp"}    # writes.frames.ext -> output_format
MAX_UPLOAD = 100 * 2**20                # fal.ai's own client sends bigger files in parts; the gateway doesn't
MAX_DOWNLOAD = 2 * 2**30
REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,100}")
OFF = ("the gateway sends jobs to fal.ai itself only with providers.fal.live: true in gateway.yaml, and fal.ai "
       "charges for them; until then it uses a stand-in on this machine: set providers.fal.url to it, e.g. "
       "http://127.0.0.1:8189 (python tools/mock_fal.py --gateway gateway.yaml)")
BOTH = ("providers.fal.url in gateway.yaml names a stand-in, and providers.fal.live: true sends jobs to fal.ai "
        "itself: leave url empty to send them to fal.ai, or set live: false for the stand-in")


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


def _iso(t: float, up: bool = False) -> str:
    """A time as fal.ai takes it, to the second: rounded up for the end of a time (it is
    exclusive), so a charge in its last second is read."""
    return datetime.fromtimestamp(math.ceil(t) if up else math.floor(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(e) -> dict | None:
    """One billing event as the ledger takes it, or None for one it can't read."""
    if not isinstance(e, dict) or not (isinstance(e.get("request_id"), str) and REQUEST_ID.fullmatch(e["request_id"])):
        return None
    try:
        at = datetime.fromisoformat(str(e.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    nano, total = e.get("cost_estimate_nano_usd"), e.get("cost_total")
    if isinstance(nano, (int, float)) and not isinstance(nano, bool) and nano >= 0:
        cost = nano / 1e9
    elif isinstance(total, (int, float)) and not isinstance(total, bool) and total >= 0:
        cost = float(total)
    else:
        return None

    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return {"request_id": e["request_id"], "endpoint": str(e.get("endpoint_id") or ""), "at": at,
            "quantity": num(e.get("quantity")), "unit": str(e["unit"]) if e.get("unit") else None,
            "unit_price": num(e.get("unit_price")), "cost": round(cost, 9)}


def _until(text) -> float:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time() + 600


class Adapter:
    """fal.ai, as one gateway's keys reach it. The key is held here and sent in the
    Authorization header to fal.ai's queue and REST hosts: nothing it returns or logs holds it."""

    def __init__(self, keys: dict, url: str = "", media_seconds: int = 3600, transport=None, timeout: float = 60.0,
                 live: bool = False, keep_payloads: bool = False):
        self._key = str(keys.get("requests") or "")
        self._billing = str(keys.get("billing") or "")
        base = url.rstrip("/")
        self.stand_in = bool(base)
        self.live = bool(live)                  # fal.ai itself, which charges (gateway.yaml, decision 0062)
        self.keep_payloads = bool(keep_payloads)
        self.queue, self.rest, self.cdn, self.api = (base, base, base, base) if base else (QUEUE, REST, CDN, API)
        self.media_seconds = int(media_seconds)
        self._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(timeout, connect=10.0),
                                         follow_redirects=False)
        self._token: tuple[str, str, float] | None = None      # the CDN's: its header, its address, until when

    def __repr__(self) -> str:
        return f"<fal.ai adapter {self.queue}>"

    def problems(self) -> list[str]:
        """Why it can't send anything. Its keys are the runner's to check (NEEDS)."""
        if self.live:
            return [BOTH] if self.stand_in else []
        return [] if self.stand_in and local(self.queue) else [OFF]

    def bill_problems(self) -> list[str]:
        """Why it can't read fal.ai's bill, beyond what stops it sending anything."""
        return [] if self._billing else [
            "there's no file for fal.ai's billing key (providers.fal.keys.billing in gateway.yaml, a key with "
            "fal.ai's BILLING permissions), so the gateway can't read fal.ai's bill: the ledger keeps its estimates"]

    # ---------------------------------------------------------------- calls
    def _may_send(self, url: str, to: str | None = None) -> bool:
        """Whether a call may go to this address: `to` key (a key goes with it: fal.ai's queue,
        REST and API hosts), cdn (the upload token, or a result's file: its CDN) or None."""
        parts = urlsplit(url)
        if self.stand_in or not self.live:
            return self.stand_in and not self.live and local(url) and (
                to is None or parts.netloc == urlsplit(self.queue).netloc)
        if parts.scheme != "https":
            return False
        if to == "key":
            return parts.netloc in (urlsplit(self.queue).netloc, urlsplit(self.rest).netloc, urlsplit(self.api).netloc)
        if to == "cdn":
            host = parts.hostname or ""
            return host == "fal.media" or host.endswith(".fal.media")
        return True

    def _scrub(self, text: str) -> str:
        for key in (self._key, self._billing):
            if key:
                text = text.replace(key, "***")
        return text

    async def _call(self, method: str, url: str, auth: str | None = None, to: str | None = None, **kw):
        if not self._may_send(url, to):
            why = (BOTH if self.live and self.stand_in else OFF if not self.live
                   else "it isn't one of fal.ai's addresses")
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

    def _refusal(self, r: httpx.Response, doing: str, key: str = "requests") -> ApiError:
        detail = self._scrub(_detail(r))[:600]
        code = r.status_code
        if code in (401, 403):
            needs = " (it needs fal.ai's BILLING permissions)" if key == "billing" else ""
            return ApiError(f"fal.ai refused the gateway's key while {doing} ({code}): check keys.{key} in "
                            f"gateway.yaml on the gateway{needs}")
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

    def _job_headers(self) -> dict:
        """Sent with each job (decision 0062): how long its files are kept, its inputs and outputs
        out of the request history unless keep_payloads, and the reviewed endpoint only."""
        h = {LIFECYCLE: self._lifecycle(), NO_FALLBACK: "true"}
        if not self.keep_payloads:
            h[STORE_IO] = "0"
        return h

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
        """fal.ai's CDN token (asked with the key) and where to upload with it: the CDN's own
        address, as fal.ai's Python client uploads (the token's base_url is checked to be the CDN's)."""
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
        self._token = (f"{kind} {token}", self.cdn, _until(d.get("expires_at")))
        return self._token[0], self._token[1]

    async def upload(self, path: str, content_type: str) -> str:
        name = os.path.basename(path)
        size = os.path.getsize(path)
        if size > MAX_UPLOAD:
            raise ApiError(f"{name} is {size / 2**20:.0f} MB; the gateway uploads files of up to "
                           f"{MAX_UPLOAD // 2**20} MB")
        data = await asyncio.to_thread(Path(path).read_bytes)
        headers = {"Content-Type": content_type, LIFECYCLE: self._lifecycle(), LIFECYCLE_UPLOAD: self._lifecycle(),
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
        isn't sent: fal.ai uses its default. A choice with a value in runner.values sends that
        value (decision 0069), such as an image size's width and height."""
        out = dict(api.get("fixed") or {})
        mapped = api.get("values") or {}
        for param, field in (api.get("send") or {}).items():
            v = values.get(param)
            if v is not None and v != "":
                out[field] = mapped[param].get(str(v), v) if param in mapped else v
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
                                     headers=self._job_headers())
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
        if not self._may_send(url, "cdn"):
            why = (BOTH if self.live and self.stand_in else OFF if not self.live
                   else "fal.ai's files are on its CDN, over https (*.fal.media)")
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

    async def prices(self, endpoints: list[str]) -> dict[str, dict]:
        """Each endpoint's price as fal.ai's pricing API gives it: {endpoint: {unit_price, unit,
        currency}}. An endpoint it doesn't price is left out."""
        out = {}
        for i in range(0, len(endpoints), 50):          # 50 endpoints a call, at most
            batch = endpoints[i:i + 50]
            r = await self._call("GET", f"{self.api}/v1/models/pricing", auth=self._keyed(), to="key",
                                 params={"endpoint_id": ",".join(batch)})
            if not r.is_success:
                raise self._refusal(r, "reading its prices")
            for p in self._json(r, "reading its prices").get("prices") or []:
                if not isinstance(p, dict) or p.get("endpoint_id") not in batch:
                    continue
                price = p.get("unit_price")
                if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0:
                    continue
                out[p["endpoint_id"]] = {"unit_price": float(price), "unit": str(p.get("unit") or ""),
                                         "currency": str(p.get("currency") or "")}
        return out

    async def describe(self, endpoint: str) -> dict:
        """fal.ai's description of an endpoint, for `comfyfarm fal draft` (decision 0065): its
        name, category, status and licence type (`metadata`), and an OpenAPI schema of what it
        takes and returns (`openapi`). Free, and never a job. The model API is public; the
        requests key goes with it, as with every call to fal.ai, for its higher rate limit."""
        doing = f"reading its description of {endpoint}"
        r = await self._call("GET", f"{self.api}/v1/models", auth=self._keyed(), to="key",
                             params={"endpoint_id": endpoint, "expand": "openapi-3.0"})
        if not r.is_success:
            raise self._refusal(r, doing)
        found = [m for m in self._json(r, doing).get("models") or []
                 if isinstance(m, dict) and m.get("endpoint_id") == endpoint]
        if not found:
            raise ApiError(f"fal.ai has no endpoint {endpoint}: check its id on the model's page (fal.ai/models)",
                           "refused")
        return found[0]

    async def ask(self, endpoints: list[str]) -> list[tuple[bool, str]]:
        """Two questions fal.ai answers for free, for `agent --check` (decision 0062): its prices,
        with the requests key, and the balance, with the billing key. (ok, what it said) each."""
        out = []
        try:
            got = await self.prices(endpoints)
            priced = ", ".join(f"{e} ${p['unit_price']:g}, billed in {p['unit']}" for e, p in sorted(got.items()))
            out.append((True, f"fal.ai took the requests key and gave its prices: {priced or 'none of them'}"))
        except ApiError as e:
            out.append((False, f"the requests key: {e}"))
        if self._billing:
            try:
                b = await self.balance()
                out.append((True, "fal.ai took the billing key: the account's balance is "
                                  + (f"${b['amount']:,.2f}" if b else "not given")))
            except ApiError as e:
                out.append((False, f"the billing key: {e}"))
        return out

    # ---------------------------------------------------------------- the bill (decision 0061)
    async def bill(self, since: float, until: float) -> list[dict]:
        """What fal.ai charged the account between since and until (epoch seconds), one event per
        charge: {request_id, endpoint, at, quantity, unit, unit_price, cost}, cost in US dollars
        after discounts. Every page is read, or ApiError: a bill read in part is no bill."""
        doing = "reading its bill"
        params = {"start": _iso(since), "end": _iso(until, up=True), "limit": BILL_PAGE}
        out: list[dict] = []
        for _ in range(BILL_PAGES):
            r = await self._call("GET", f"{self.api}/v1/models/billing-events", auth=f"Key {self._billing}",
                                 to="key", params=params)
            if not r.is_success:
                raise self._refusal(r, doing, "billing")
            d = self._json(r, doing)
            for e in d.get("billing_events") or []:
                event = _event(e)
                if event is not None:
                    out.append(event)
            cursor = d.get("next_cursor")
            if not cursor:
                return out
            params = {**params, "cursor": str(cursor)}
        raise ApiError(f"fal.ai's bill ran over {BILL_PAGES} pages of {BILL_PAGE}: read a shorter time", "transient")

    async def balance(self) -> dict | None:
        """The account's credit balance, {amount, currency}; None when fal.ai doesn't give one."""
        r = await self._call("GET", f"{self.api}/v1/account/billing", auth=f"Key {self._billing}", to="key",
                             params={"expand": "credits"})
        if not r.is_success:
            raise self._refusal(r, "reading its balance", "billing")
        credits = self._json(r, "reading its balance").get("credits")
        amount = credits.get("current_balance") if isinstance(credits, dict) else None
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None
        return {"amount": float(amount), "currency": str(credits.get("currency") or "")}

    async def close(self) -> None:
        await self._client.aclose()
