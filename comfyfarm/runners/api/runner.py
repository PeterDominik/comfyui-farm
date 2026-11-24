"""The API runner: API definitions at outside providers, on the farm's gateway (base.Runner,
decisions 0055, 0056).

The gateway is an agent with this runner alone. gateway.yaml says which providers it has
lanes for, how many, and the files their keys are in. Each provider's adapter (api/<name>.py)
speaks to it; this runner does the rest for every provider alike:

1. the inputs, as the agent prepared them (EXRs converted to PNG), are uploaded;
2. the job is sent once, with the definition's fixed fields, its sent params and the
   uploaded URLs. What is sent is in the run folder before it goes, and the request's id
   as soon as the provider gives it. Nothing is ever sent twice: a job the gateway can't
   tell was received fails, saying so, rather than being paid for again;
3. its status is asked every poll_seconds until it ends or its time limit passes;
4. what it made is downloaded at once into the output folder, as <frames_prefix>_<n>.<ext>,
   for the frames handler to rename (providers keep results only a while).

Each run has a folder (gateway.runs_dir/<attempt id>): job.json (its params and files),
request.json (what was sent and the request's id), result.json (what the provider said it
made) and exit.json (how it ended). No key is in any of them: keys are read from their
files when the gateway starts and stay inside the adapters.

After a restart the agent asks where each run is (locate): the run folder says whether it
was sent, and the provider how it is doing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from ... import catalog as C
from ...config import AgentConfig
from ...definitions import Definition, DefinitionRegistry
from ...fileio.ocio import REPO
from ..base import Host, RunRejected
from ..process import with_files
from . import ApiError, module

log = logging.getLogger("comfyfarm")

CONTRACT = 1                            # job.json's shape
KEEP_DAYS = 7                           # run folders left by failed runs, kept to look at
RESULT_TRIES = 6                        # asking for what it made, when the provider stumbles
MAGIC = {"png": (b"\x89PNG\r\n\x1a\n",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",),
         "webp": (b"RIFF",)}
# The job may have reached the provider, but the gateway can't tell: it is never sent again.
NOT_SENT_AGAIN = ("It wasn't sent again, so it is never paid for twice: look for it in {provider}'s request history, "
                  "and submit it again if it isn't there.")
LOST_ANSWER = ("The gateway lost {provider}'s answer while sending the job ({why}), so it can't tell whether "
               "{provider} got it. ")
RESTARTED = "The gateway stopped while sending the job to {provider}, so it can't tell whether {provider} got it. "


class ApiRegistry(DefinitionRegistry):
    """The API definitions in a folder: the gateway's."""
    RUNNER = "api"


@dataclass
class ApiRun:
    """A run's live progress (base.RunState)."""
    job_id: str
    started: float                      # when it was sent
    frac: float = 0.0
    stage: str | None = None
    last_push: float = 0.0
    running: bool = False
    ended: bool = False

    def progress(self) -> float:
        return min(max(self.frac, 0.0), 0.99)

    def nodes(self) -> int | None:
        return None


def _read_json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _head(path: str) -> bytes:
    with open(path, "rb") as f:         # closed before the file is renamed (Windows)
        return f.read(16)


def _short(result: dict) -> dict:
    """What a provider said it made, without the files themselves (sync_mode sends them inline)."""
    def walk(v):
        if isinstance(v, str) and v.startswith("data:"):
            return v.partition(",")[0] + ",..."
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v
    return walk(result)


def key_path(cfg: AgentConfig, raw: str) -> Path:
    """A key file as gateway.yaml names it: ~ is the account's home, a relative path is next to
    gateway.yaml."""
    return Path(cfg.resolve(os.path.expanduser(raw)))


def read_key(path: Path) -> tuple[str | None, str | None]:
    """A key from its file, and what's wrong with the file (None: nothing). A file that other
    users on this machine can read is refused: the key is an account's, with its money. A
    byte-order mark some editors put first is dropped; any other character a key can't have
    (it goes in an HTTP header, which is plain ASCII) is refused, saying so."""
    try:
        st = path.stat()
    except OSError:
        return None, f"the key file {path} doesn't exist"
    if os.name != "nt" and st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return None, f"the key file {path} can be read by other users on this machine: chmod 600 {path}"
    try:
        key = path.read_text(encoding="utf-8-sig").strip()
    except UnicodeDecodeError:
        return None, f"the key file {path} isn't plain text: save it again as plain text (UTF-8), the key alone"
    except OSError as e:
        return None, f"can't read the key file {path}: {e.strerror or e}"
    if not key or any(c.isspace() for c in key):
        return None, f"the key file {path} should hold the key alone, on one line"
    if not (key.isascii() and key.isprintable()):
        return None, (f"the key file {path} holds characters a key doesn't have (an editor may have added them): "
                      f"save it again as plain text, the key alone")
    return key, None


class ApiRunner:
    label = "the gateway"

    def __init__(self, cfg: AgentConfig, registry: ApiRegistry | None = None, catalog=None, transport=None):
        self.cfg = cfg
        self.gc = cfg.gateway
        self.name = cfg.name
        self.host: Host | None = None
        self.runs_dir = Path(cfg.resolve(self.gc.runs_dir))
        self._registry = registry
        self.abandoned: set[str] = set()
        self._follows: dict[str, asyncio.Task] = {}
        self.lanes: dict[str, int] = {}             # provider -> its jobs at once here
        self.adapters: dict[str, object] = {}       # provider -> its adapter, with its keys
        self.provider_problems: dict[str, list[str]] = {}
        self.problems: list[str] = []               # the gateway's own (gateway.yaml)
        self._setup(catalog or C.current(), transport)

    # ================================================================ gateway.yaml
    def _setup(self, catalog, transport) -> None:
        if not self.gc.enabled:
            self.problems.append(f"{self.name} isn't a gateway (gateway.enabled in its config)")
        if not self.gc.providers:
            self.problems.append("gateway.yaml lists no providers (providers: {fal: {...}})")
        for name, pc in self.gc.providers.items():
            p = catalog.providers.get(name)
            if p is None or p.runner != "api":
                api = sorted(n for n, x in catalog.providers.items() if x.runner == "api")
                self.problems.append(f"providers.{name} in gateway.yaml: providers/ has no outside provider {name} "
                                     f"(it has {', '.join(api) or 'none'})")
                continue
            self.lanes[name] = pc.lanes or p.lanes
            mod = module(p.adapter)
            if mod is None:
                self.provider_problems[name] = [f"this farm has no adapter {p.adapter} for {p.label} "
                                                f"(providers/{name}.yaml)"]
                continue
            probs, keys = [], {}
            unknown = sorted(set(pc.keys) - set(p.keys))
            if unknown:
                probs.append(f"keys.{unknown[0]} for {name} in gateway.yaml: {p.label} has keys "
                             f"{', '.join(p.keys)} (providers/{name}.yaml)")
            for k in p.keys:
                if k not in pc.keys:
                    if k in mod.NEEDS:
                        probs.append(f"there's no file for {p.label}'s {k} key (providers.{name}.keys.{k} in "
                                     f"gateway.yaml)")
                    continue
                key, why = read_key(key_path(self.cfg, pc.keys[k]))
                if why:
                    probs.append(why)
                else:
                    keys[k] = key
            adapter = mod.Adapter(keys, url=pc.url, media_seconds=pc.media_seconds, transport=transport)
            self.adapters[name] = adapter
            self.provider_problems[name] = probs + adapter.problems()

    def _problems_of(self, provider: str) -> list[str]:
        if provider not in self.lanes:
            return [f"{self.name} has no lanes for {provider} (providers.{provider} in gateway.yaml)"]
        return self.provider_problems.get(provider) or []

    # ================================================================ the agent's side
    def attach(self, host: Host) -> None:
        self.host = host

    def start(self) -> list:
        """Said once as the gateway starts (`agent --check` says them itself)."""
        for why in self.problems:
            log.warning("gateway %s: %s", self.name, why)
        for name, probs in self.provider_problems.items():
            for why in probs:
                log.warning("gateway %s: %s can't take jobs: %s", self.name, self._label(name), why)
        self._prune()
        return []

    async def close(self) -> None:
        for t in self._follows.values():
            t.cancel()
        await asyncio.gather(*self._follows.values(), return_exceptions=True)
        self._follows.clear()
        for a in self.adapters.values():
            await a.close()

    @property
    def ready(self) -> bool:
        return True                     # a run it can't send is refused at submit, saying why

    @property
    def max_runs(self) -> int | None:
        return sum(self.lanes.values()) or None

    def status(self) -> dict:
        usable = [p for p in self.lanes if p in self.adapters and not self.provider_problems.get(p)]
        why = self.problems + [f"{p}: {w}" for p in self.lanes for w in self._problems_of(p)]
        return {"online": bool(usable), "connected": bool(usable), "stats": None, "queue_remaining": None,
                "error": "; ".join(why) or None, "farm_nodes": None}

    def _definitions(self) -> dict:
        if self._registry is None:
            folder = self.cfg.workflows_dir
            self._registry = ApiRegistry(self.cfg.resolve(folder) if folder else Path(REPO) / "workflows")
        return self._registry.workflows

    def knows(self, process: str) -> bool:
        return process in self._definitions()

    def wiring(self, wf: Definition) -> list[str]:
        """What's wrong with an API definition for its provider's adapter, on any machine."""
        p = C.current().providers.get(wf.provider or "")
        mod = module(p.adapter) if p is not None and p.runner == "api" else None
        if mod is None:
            return [f"provider: {wf.provider} has no adapter on this farm"]
        problems = list(mod.check(wf))
        other = sorted(set(wf.writes) - {"frames"})
        if other:
            problems.append(f"writes.{other[0]}: an API definition writes frames only, for now")
        return problems

    def processes(self) -> dict:
        """Each API definition of this checkout, what this gateway lacks for it (no lanes for its
        provider, no key) and what's wrong with it for its provider."""
        return {name: {"digest": wf.digest, "problems": self._problems_of(wf.provider or ""),
                       "broken": self.wiring(wf), "expected_nodes": None}
                for name, wf in self._definitions().items()}

    async def release(self) -> dict:
        return {"ok": True, "before": None, "after": None, "error": None}     # it has no GPU

    def _folder(self, pid: str) -> Path:
        return self.runs_dir / pid

    def submitted(self, h) -> dict | None:
        """What the run sent: the provider, its endpoint, the request's id and the body."""
        return h.job.get("api_run")

    # ================================================================ a run
    async def submit(self, h) -> None:
        wf = self._definitions().get(h.job["workflow"])
        if wf is None:
            raise RunRejected(f"{self.name} has no API definition {h.job['workflow']}. Update the farm's code on it.")
        request = h.job.get("request") or {}
        if request.get("definition") != wf.digest:
            raise RunRejected(f"The definition {wf.name} on the gateway ({wf.digest}) and the one the manager planned "
                              f"the job with ({request.get('definition')}) differ. Update the farm's code on both; a "
                              f"change that was not committed on either one causes this too.")
        provider = wf.provider or ""
        broken = self.wiring(wf)
        if broken:
            raise RunRejected(f"{wf.name} can't run: {'; '.join(broken)}")
        why = self._problems_of(provider)
        if why:
            raise RunRejected(f"The gateway can't send jobs to {provider}: {'; '.join(why)}.")
        adapter = self.adapters[provider]
        values = request.get("values") or {}
        files = self._files(wf, request)
        run = self._folder(h.attempt)
        job = {"contract": CONTRACT, "job": h.id, "attempt": h.attempt, "definition": wf.name, "digest": wf.digest,
               "provider": provider, "account": self.gc.account, "endpoint": wf.api["endpoint"], "params": values,
               "files": files, "output_dir": request.get("out_dir"), "frames_prefix": request.get("frames_prefix"),
               "ext": wf.frames_ext, "seconds": self._seconds(wf)}
        await asyncio.to_thread(self._make_folder, run, job)
        h.pid = h.attempt
        h.rt = ApiRun(job_id=h.id, started=time.time())
        try:
            await self._upload_and_send(h, wf, adapter, provider, values, files, run)
        except RunRejected as e:
            await asyncio.to_thread(self._ended, h.attempt, {"reason": "refused", "error": str(e)})
            raise

    async def _upload_and_send(self, h, wf, adapter, provider: str, values: dict, files: dict, run: Path) -> None:
        label = self._label(provider)
        urls: dict[str, list[str]] = {}
        count = sum(len(v) for v in files.values())
        done = 0
        for param, paths in files.items():
            urls[param] = []
            for path in paths:
                done += 1
                h.rt.stage = f"uploading {'the input' if count == 1 else f'input {done}/{count}'} to {label}"
                await self.host.run_progress(h, h.rt.stage, force=True)
                urls[param].append(await self._upload(adapter, path, label))
        body = adapter.body(wf.api, values, urls)
        sent = {"provider": provider, "account": self.gc.account, "endpoint": wf.api["endpoint"], "body": body}
        await asyncio.to_thread(_write_json, run / "request.json", {**sent, "state": "sending", "at": time.time()})
        req = await self._send(h, adapter, wf.api["endpoint"], body, label)
        h.rt.started = time.time()
        await asyncio.to_thread(_write_json, run / "request.json",
                                {**sent, "state": "sent", "request": req, "at": h.rt.started})
        h.job["api_run"] = {**sent, "request_id": req["id"]}
        log.info("job %s sent to %s (%s): request %s", h.id[:8], label, wf.api["endpoint"], req["id"])
        self._follow_later(h)

    def _make_folder(self, run: Path, job: dict) -> None:
        shutil.rmtree(run, ignore_errors=True)          # an earlier try of the same attempt, never sent
        run.mkdir(parents=True)
        _write_json(run / "job.json", job)

    def _files(self, wf: Definition, request: dict) -> dict[str, list[str]]:
        """Each uploaded input's files, in the params' order, as the agent prepared them."""
        upload = wf.api.get("upload") or {}
        out = {}
        for param in wf.params:
            if param not in upload:
                continue
            seq = request.get("seq") if param == wf.sequence_param else (request.get("inputs") or {}).get(param)
            d = with_files(seq) if seq else None
            out[param] = list((d or {}).get("files") or [])
        return out

    async def _upload(self, adapter, path: str, label: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                 ".mp4": "video/mp4"}.get(ext)
        if ctype is None:
            raise RunRejected(f"{label} takes PNG, JPEG, WebP or MP4 files, and {os.path.basename(path)} isn't one. "
                              f"Render it as PNG or EXR (an EXR is converted to PNG first).")
        for attempt in range(3):
            try:
                return await adapter.upload(path, ctype)
            except ApiError as e:
                if e.kind == "refused" or attempt == 2:
                    raise RunRejected(f"Uploading {os.path.basename(path)} to {label} failed: {e}") from None
                await asyncio.sleep(2 ** attempt)

    async def _send(self, h, adapter, endpoint: str, body: dict, label: str) -> dict:
        """Send the job once. The call is shielded: a run stopped while it is on its way (the
        manager dropped it) still learns its request's id, to cancel it there."""
        t = asyncio.ensure_future(adapter.submit(endpoint, body))
        try:
            return await asyncio.shield(t)
        except asyncio.CancelledError:
            self.host.spawn(self._cancel_when_sent(t, adapter, h.attempt))
            raise
        except ApiError as e:
            if e.kind == "unknown":
                raise RunRejected(LOST_ANSWER.format(provider=label, why=e)
                                  + NOT_SENT_AGAIN.format(provider=label)) from None
            raise RunRejected(f"{label} didn't take the job: {e}") from None

    async def _cancel_when_sent(self, t: asyncio.Task, adapter, pid: str) -> None:
        try:
            req = await t
        except Exception:  # noqa: BLE001 - not sent, nothing to cancel
            return
        await adapter.cancel(req)
        log.info("gateway %s: cancelled request %s, of a run stopped as it was sent", self.name, req["id"])

    def _seconds(self, wf: Definition) -> float:
        minutes = (wf.limits or {}).get("max_minutes") if isinstance(wf.limits, dict) else None
        return float(minutes or self.gc.default_minutes) * 60

    @staticmethod
    def _label(provider: str) -> str:
        p = C.current().providers.get(provider)
        return p.label if p is not None else provider

    def _request(self, pid: str | None) -> dict | None:
        r = _read_json(self._folder(pid) / "request.json") if pid else None
        return r if r and r.get("state") == "sent" and isinstance(r.get("request"), dict) else None

    def _ended(self, pid: str, how: dict) -> None:
        run = self._folder(pid)
        if run.is_dir():
            _write_json(run / "exit.json", {**how, "at": time.time()})

    # ---------------------------------------------------------------- following it
    def _follow_later(self, h) -> None:
        old = self._follows.pop(h.pid, None)
        if old is not None and not old.done():
            old.cancel()
        t = asyncio.create_task(self._follow(h), name=f"api-{h.id[:8]}")
        self._follows[h.pid] = t
        t.add_done_callback(lambda done, pid=h.pid: self._followed(pid, done))

    def _followed(self, pid: str, task: asyncio.Task) -> None:
        if self._follows.get(pid) is task:
            del self._follows[pid]

    async def _follow(self, h) -> None:
        try:
            # The agent marks it dispatched as submit returns: its endings count only from then.
            while h.status == "preparing" and self.host.holds(h):
                await asyncio.sleep(0.01)
            await self._poll(h)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("gateway %s: following job %s failed", self.name, h.id[:8])
            await self._fail(h, f"The gateway lost track of the job: {type(e).__name__}: {e}")

    async def _poll(self, h) -> None:
        rt: ApiRun = h.rt
        r = self._request(h.pid)
        if r is None:
            await self._fail(h, f"The gateway has no record of sending the job ({self._folder(h.pid)}).")
            return
        provider, req = r["provider"], r["request"]
        adapter, label = self.adapters.get(provider), self._label(provider)
        if adapter is None:
            await self._fail(h, f"The gateway has no lanes for {provider} any more (gateway.yaml).")
            return
        wf = self._definitions().get(h.job.get("workflow"))
        deadline = rt.started + (self._seconds(wf) if wf else self.gc.default_minutes * 60)
        said_down = False
        while not rt.ended and h.pid not in self.abandoned and self.host.holds(h):
            try:
                st = await adapter.status(req)
            except ApiError as e:
                if e.kind == "refused":
                    await self._fail(h, f"{label}: {e}")
                    return
                if not said_down:
                    log.warning("gateway %s: job %s: %s (asking again)", self.name, h.id[:8], e)
                    said_down = True
                st = None
            if st is not None:
                said_down = False
                if st["state"] in ("done", "failed"):
                    await self._complete(h, adapter, req, st, label)
                    return
                if st["state"] == "running" and not rt.running:
                    rt.running = True
                    await self.host.run_started(h)
                pos = st.get("position")
                rt.stage = (f"running at {label}" if st["state"] == "running"
                            else f"in {label}'s queue" + (f" ({pos} ahead)" if pos else ""))
                rt.frac = 0.5 if rt.running else 0.05
                await self.host.run_progress(h, rt.stage)
            if time.time() > deadline:
                await adapter.cancel(req)
                minutes = (deadline - rt.started) / 60
                await self._fail(h, f"{label} didn't finish the job in {minutes:g} minutes, its time limit "
                                    f"(limits.max_minutes in its process.yaml, or gateway.default_minutes): it was "
                                    f"cancelled there." + (" The gateway couldn't reach it meanwhile." if st is None
                                                           else ""), reason="timeout")
                return
            await asyncio.sleep(self.gc.poll_seconds)

    async def _complete(self, h, adapter, req: dict, st: dict, label: str) -> None:
        """It ended at the provider: what it made, downloaded now; or why it didn't."""
        if st["state"] == "failed":
            if h.cancelling or st.get("error_type") == "client_cancelled":
                await self._interrupted(h)
            else:
                kind = f" ({st['error_type']})" if st.get("error_type") else ""
                await self._fail(h, f"{label} couldn't run it{kind}: {st.get('error')}")
            return
        if h.cancelling:
            await self._interrupted(h)
            return
        for attempt in range(RESULT_TRIES):
            try:
                result = await adapter.result(req)
                break
            except ApiError as e:
                if e.kind == "refused" or attempt == RESULT_TRIES - 1:
                    await self._fail(h, f"{label} finished, but {e}")
                    return
                await asyncio.sleep(min(30, 2 ** attempt))
        run = self._folder(h.pid)
        await asyncio.to_thread(_write_json, run / "result.json", _short(result))
        urls = adapter.outputs(result)
        if not urls:
            await self._fail(h, f"{label} finished, but returned no image.")
            return
        job = _read_json(run / "job.json") or {}
        out_dir, prefix, ext = job.get("output_dir"), job.get("frames_prefix"), (job.get("ext") or "png").lower()
        if not (out_dir and prefix):
            await self._fail(h, "The job has no output folder to download what it made into.")
            return
        h.rt.stage = f"downloading from {label}"
        await self.host.run_progress(h, h.rt.stage, force=True)
        for i, url in enumerate(urls, 1):
            dest = os.path.join(out_dir, f"{prefix}_{i:05d}.{ext}")
            part = dest + ".part"
            try:
                for attempt in range(3):
                    try:
                        await adapter.download(url, part)
                        break
                    except ApiError as e:
                        if e.kind == "refused" or attempt == 2:
                            raise
                        await asyncio.sleep(2 ** attempt)
                head = await asyncio.to_thread(_head, part)
                if not head.startswith(MAGIC.get(ext, (b"",))):
                    raise ApiError(f"image {i} isn't a {ext.upper()} file, which the definition writes "
                                   f"(writes.frames.ext; output_format in its runner block)")
                await asyncio.to_thread(os.replace, part, dest)
            except (ApiError, OSError) as e:
                await asyncio.to_thread(lambda p=part: os.path.exists(p) and os.remove(p))
                await self._fail(h, f"{label} finished, but the gateway couldn't download what it made: "
                                    f"{getattr(e, 'strerror', None) or e}")
                return
        ended = {"reason": "done", "request_id": req["id"], "images": len(urls), "metrics": st.get("metrics") or {}}
        await asyncio.to_thread(self._ended, h.pid, ended)
        h.rt.ended = True
        await self.host.run_succeeded(h, ended)

    async def _fail(self, h, message: str, reason: str = "failed") -> None:
        if h.rt is not None:
            if h.rt.ended:
                return
            h.rt.ended = True
        await asyncio.to_thread(self._ended, h.pid, {"reason": reason, "error": message})
        await self.host.run_failed(h, message)

    async def _interrupted(self, h) -> None:
        if h.rt is not None:
            h.rt.ended = True
        await asyncio.to_thread(self._ended, h.pid, {"reason": "cancelled"})
        await self.host.run_interrupted(h)

    # ---------------------------------------------------------------- the agent asks
    async def cancel(self, h) -> bool:
        """Cancel it at the provider. True: taken out of its queue, it never ran. Otherwise it
        ends as interrupted once the provider says it stopped."""
        r = self._request(h.pid)
        if r is None:
            return False                # not sent yet: the agent cancels it once it is
        adapter = self.adapters.get(r["provider"])
        said = await adapter.cancel(r["request"]) if adapter else "unknown"
        if said == "removed":
            if h.rt is not None:
                h.rt.ended = True
            await asyncio.to_thread(self._ended, h.pid, {"reason": "cancelled"})
            return True
        return False

    def give_up(self, pid: str) -> None:
        """The agent doesn't want it any more: stop following it, and cancel it at the provider."""
        self.abandoned.add(pid)
        t = self._follows.pop(pid, None)
        if t is not None and not t.done():
            t.cancel()
        r = self._request(pid)
        adapter = self.adapters.get(r["provider"]) if r else None
        if adapter is not None and self.host is not None and not (self._folder(pid) / "exit.json").exists():
            self.host.spawn(adapter.cancel(r["request"]))

    async def abandon(self, pid: str) -> None:
        self.give_up(pid)

    async def locate(self, h) -> tuple[str, dict | None]:
        run = self._folder(h.pid)
        if not (run / "job.json").exists():
            return "absent", None
        ended = _read_json(run / "exit.json")
        if ended is not None:
            return "finished", ended
        r = _read_json(run / "request.json")
        if r is None:
            return "absent", None       # never sent: it is prepared and sent again
        if r.get("state") != "sent":
            label = self._label(r.get("provider") or "")
            return "finished", {"reason": "unknown", "error": RESTARTED.format(provider=label)
                                + NOT_SENT_AGAIN.format(provider=label)}
        adapter = self.adapters.get(r.get("provider"))
        if adapter is None:
            return "finished", {"reason": "failed", "error": f"The gateway has no lanes for {r.get('provider')} any "
                                                             f"more (gateway.yaml), so it can't follow the job."}
        try:
            st = await adapter.status(r["request"])
        except ApiError as e:
            if e.kind == "refused":
                return "finished", {"reason": "failed", "error": f"{self._label(r['provider'])}: {e}"}
            return "running", None      # followed from here, asked again
        if st["state"] in ("done", "failed"):
            return "finished", {"reason": "completed", "status": st}
        return ("running" if st["state"] == "running" else "pending"), None

    def follow(self, h) -> None:
        r = _read_json(self._folder(h.pid) / "request.json") or {}
        h.rt = ApiRun(job_id=h.id, started=float(r.get("at") or time.time()), running=h.status == "running")
        if self.host is not None:
            self._follow_later(h)

    async def settle(self, h, ended: dict) -> None:
        if h.rt is None:
            h.rt = ApiRun(job_id=h.id, started=time.time())
        reason = ended.get("reason")
        r = _read_json(self._folder(h.pid) / "request.json") or {}
        label = self._label(r.get("provider") or "")
        if reason == "done":
            h.rt.ended = True
            await self.host.run_succeeded(h, ended)
        elif reason == "completed":
            adapter = self.adapters.get(r.get("provider"))
            if adapter is None:
                await self._fail(h, f"The gateway has no lanes for {r.get('provider')} any more (gateway.yaml).")
            else:
                await self._complete(h, adapter, r["request"], ended.get("status") or {}, label)
        elif reason == "cancelled":
            await self._interrupted(h)
        elif reason == "unknown":
            await self._fail(h, str(ended.get("error") or RESTARTED.format(provider=label)
                                    + NOT_SENT_AGAIN.format(provider=label)), reason="unknown")
        else:
            h.rt.ended = True           # its failure was said when it happened, or is said now
            await self.host.run_failed(h, str(ended.get("error") or f"It failed at {label}."))

    async def result(self, h) -> dict | None:
        return _read_json(self._folder(h.pid) / "exit.json")

    def warnings(self, h, result) -> list[str]:
        return []

    def preview(self, h, result) -> list:
        return []                       # an API definition doesn't make its own preview video

    def about(self, h) -> dict:
        """What ran it, for its provenance: the provider, its endpoint, the request and whose account."""
        r = _read_json(self._folder(h.pid) / "request.json") or {}
        return {"backend": "api", "provider": r.get("provider"), "endpoint": r.get("endpoint"),
                "request_id": (r.get("request") or {}).get("id"), "account": r.get("account")}

    def files(self, h, result, kind: str, spec: dict) -> list:
        return []                       # frames only: they are downloaded where they go

    async def fetch(self, entry, dest) -> None:
        await asyncio.to_thread(os.replace, entry, dest)

    async def forget(self, pid: str | None) -> None:
        """A run that went well leaves nothing behind; a failed one's folder is kept a while."""
        if not pid:
            return
        run = self._folder(pid)
        if (_read_json(run / "exit.json") or {}).get("reason") == "done":
            await asyncio.to_thread(shutil.rmtree, run, True)

    def _prune(self) -> None:
        """Folders of runs that ended days ago (failed ones are kept to look at)."""
        if not self.runs_dir.is_dir():
            return
        old = time.time() - KEEP_DAYS * 86400
        for run in self.runs_dir.iterdir():
            try:
                if run.is_dir() and (run / "exit.json").exists() and (run / "exit.json").stat().st_mtime < old:
                    shutil.rmtree(run, ignore_errors=True)
            except OSError:
                pass


__all__ = ["ApiRegistry", "ApiRun", "ApiRunner", "key_path", "read_key"]
