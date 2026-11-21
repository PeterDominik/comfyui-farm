"""The agent's environment manager (decision 0030): makes this machine's runner environments
what the manager says it should have.

The manager sends, in a check-in reply, what this machine should have of each environment
it manages: a release's fingerprint, or none (remove it). An environment it doesn't name is
left as it is. For each one, the manager here:

- installs the release from the bank when its current build isn't it: copied to the
  machine, checked against its hashes, built beside the current build, checked, and only
  then made current (envs/release.py, envs/build.py). The build before stays as previous,
  so going back to it is a switch, not a build;
- removes it, when told to;
- waits while a job is running in it, so a run never loses its environment;
- doesn't try a release that failed here again until the manager says something else.

One thing at a time, off the event loop. What it is doing goes with the agent's status
(`env_work`), and what it has built with `environments` (decision 0029). Off unless
agent.yaml's envs.managed is on: then it only says that it is off."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from ..envs import build as B
from ..envs import release as R
from ..envs import spec as S
from ..fileio import wire as W
from ..protocol import now

log = logging.getLogger("comfyfarm")


class EnvManager:
    LOOK_SECONDS = 60.0                 # looked at again this often, and at once when told

    def __init__(self, agent):
        self.agent = agent
        self.cfg = agent.cfg
        self.root = Path(self.cfg.resolve(self.cfg.envs.dir))
        self.wanted: dict[str, str | None] = {}
        self.hash: str | None = None    # which version of `wanted` this is (the manager's name for it)
        self.bank_said: str | None = None
        self.work: dict[str, dict] = {}     # name -> what it is doing about it, or why it can't
        self.failed: dict[str, str] = {}    # name -> the release that failed here
        self._wake: asyncio.Event | None = None

    # ---------------------------------------------------------------- told
    def want(self, msg: dict) -> None:
        """The manager's `environments` (a check-in reply): {wanted, hash, bank}."""
        wanted = {str(k): (str(v) if v else None) for k, v in (msg.get("wanted") or {}).items()}
        # A release that failed is tried again only once the manager has asked for something else.
        self.failed = {k: v for k, v in self.failed.items() if wanted.get(k) == v}
        self.work = {k: v for k, v in self.work.items() if k in wanted and v.get("want") == wanted[k]}
        self.wanted, self.hash, self.bank_said = wanted, msg.get("hash"), msg.get("bank")
        log.info("agent %s: the manager wants %s", self.agent.name,
                 ", ".join(f"{k} {v or 'removed'}" for k, v in sorted(wanted.items())) or "no environment managed")
        self.poke()

    def poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def bank(self) -> Path | None:
        """Where releases are: the folder of the manager.json this agent finds the manager
        through; else the manager's share.dir, as this machine sees it."""
        manager = self.cfg.manager.strip()
        if manager and not manager.startswith(("http://", "https://")):
            return Path(manager).parent
        if self.bank_said:
            return Path(W.localize(self.bank_said, self.agent.view))
        return None

    def in_use(self) -> set[str]:
        """Environments a job of this agent is running in (the process runner's)."""
        used: set[str] = set()
        for r in self.agent.runners:
            used |= set(getattr(r, "environments_in_use", lambda: ())())
        return used

    # ---------------------------------------------------------------- doing
    async def loop(self) -> None:
        self._wake = asyncio.Event()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.LOOK_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.apply()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("agent %s: managing its environments failed", self.agent.name)

    async def apply(self) -> None:
        """Each wanted environment, one at a time."""
        for name, release in sorted(self.wanted.items()):
            if name not in self.wanted or self.wanted[name] != release:
                continue                # told something else meanwhile
            await self._one(name, release)

    def _done(self, name: str, release: str | None) -> bool:
        have = B.builds(self.root, name)
        if release is None:
            return not (self.root / name).exists()
        return have.current == release and bool(have.info.get(release, {}).get("ok"))

    async def _say(self, name: str, entry: dict | None) -> None:
        """What it is doing about `name` (None: nothing, it is as wanted), sent with the status."""
        before = self.work.get(name)
        if entry is None:
            self.work.pop(name, None)
        else:
            self.work[name] = entry
        if before != entry:
            await self.agent.refresh_environments()
            if self.agent.registered:
                await self.agent.send_status()

    async def _one(self, name: str, release: str | None) -> None:
        if await asyncio.to_thread(self._done, name, release):
            await self._say(name, None)
            return
        if not self.cfg.envs.managed:
            await self._say(name, {"want": release, "state": "off", "error": "envs.managed is off in this machine's "
                                   "agent.yaml: it installs nothing the manager asks for"})
            return
        if self.failed.get(name) == release and release is not None:
            return                      # said already; tried again when the manager asks for something else
        if name in self.in_use():
            await self._say(name, {"want": release, "state": "waiting", "since": now(),
                                   "error": "a job is running in it: done once it ends"})
            return
        if release is None:
            await self._say(name, {"want": None, "state": "removing", "since": now()})
            await asyncio.to_thread(B.remove, self.root, name)
            log.info("agent %s removed the environment %s, as the manager asked", self.agent.name, name)
            await self._say(name, None)
            return
        await self._install(name, release)

    async def _install(self, name: str, release: str) -> None:
        bank = self.bank()
        if bank is None:
            self.failed[name] = release
            await self._say(name, {"want": release, "state": "failed", "at": now(),
                                   "error": "this agent can't find the bank: its manager is a URL and the manager "
                                            "has no share.dir"})
            return
        await self._say(name, {"want": release, "state": "installing", "since": now()})
        stage = self.root / ".releases" / name / release
        lines: list[str] = []
        try:
            rel = await asyncio.to_thread(R.find, bank, name, release)
            spec = await asyncio.to_thread(R.fetch, rel, stage)
            uv = await asyncio.to_thread(B.get_uv, self.root, list(self.cfg.envs.uv) or None)
            origin = {"release": rel.fingerprint, "published_at": rel.published_at, "commit": rel.meta.get("commit")}
            await asyncio.to_thread(B.install, spec, self.root, uv, False, lines.append, origin)
        except (R.ReleaseError, B.BuildError, S.SpecError, OSError) as e:
            self.failed[name] = release
            log.warning("agent %s: installing %s %s failed: %s", self.agent.name, name, release, e)
            await self._say(name, {"want": release, "state": "failed", "at": now(), "error": str(e)[-1500:]})
            return
        finally:
            await asyncio.to_thread(shutil.rmtree, stage, True)
        log.info("agent %s: %s %s is installed and current (%s)", self.agent.name, name, release,
                 lines[-1] if lines else "")
        await self._say(name, None)
