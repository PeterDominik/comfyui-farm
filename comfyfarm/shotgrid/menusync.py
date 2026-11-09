"""ShotGrid's right-click menu kept in step with the farm's by the manager (README: ShotGrid
right-click), so nobody has to run tools/sg_ami.py from Maya after a change.

The manager can't create Action Menu Items itself (its own ShotGrid script is read only),
and the pipeline's client that can (rdo_publish_pipeline) lives in the studio's rez
environment, not in the farm's Python. So, as publishing does with dispatchGraph, it runs
a command in a clean login shell (shotgrid.menu_sync.shell / .command):

    rez env rdo_publish_pipeline -- python <repo>/tools/sg_ami.py --pipeline \\
        --menu-json <data>/shotgrid_menu.json --apply

with the menu written to that file first (GET /api/v1/shotgrid/menu's), so the tool needn't
ask the manager for it. The tool only touches items posting to the farm, and makes them
match: created, changed, deleted.

When: shortly after the manager starts, then every check_minutes when the menu differs
from the one last synced (menu.yaml edited, a workflow added or changed), and every
every_hours anyway (an item changed by hand in ShotGrid). POST
/api/v1/shotgrid/menu/sync runs it now. A menu with problems, without a farm_url, or
with no items while it names shows (the workflows didn't load?) isn't synced: the
status says why. A failed run is tried again at the next check."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable

from ..publish import dispatch

log = logging.getLogger("comfyfarm.shotgrid.menusync")

__all__ = ["MenuSync", "digest", "counts"]

START_DELAY = 20.0                      # seconds after the manager starts
OUTPUT_KEPT = 4000                      # characters of the tool's output kept in the status


def digest(menu: dict) -> str:
    """What makes ShotGrid's items: the farm's address and the items."""
    return hashlib.sha1(json.dumps({"farm_url": menu.get("farm_url"), "items": menu.get("items")},
                                   sort_keys=True).encode()).hexdigest()


def counts(output: str) -> dict | None:
    """{created, changed, deleted} from the tool's last line ("Done: 2 created, 0 changed,
    1 deleted."), or None."""
    m = re.search(r"Done: (\d+) created, (\d+) changed, (\d+) deleted", output or "")
    return {"created": int(m[1]), "changed": int(m[2]), "deleted": int(m[3])} if m else None


class MenuSync:
    """`menu()` gives the menu as GET /api/v1/shotgrid/menu does; `tool` is sg_ami.py;
    `folder` where the menu's JSON is written; `run` runs an argv (dispatch.run: tests)."""

    def __init__(self, cfg, menu: Callable[[], dict], tool: Path, folder: Path, run=dispatch.run):
        self.cfg = cfg
        self.menu = menu
        self.tool = Path(tool)
        self.file = Path(folder) / "shotgrid_menu.json"
        self._run = run
        self.synced: str | None = None          # the digest of the menu last synced
        self.synced_at = 0.0
        self.running = False
        self.last: dict | None = None           # {at, reason, ok, why, created, changed, deleted, output}
        self.task: asyncio.Task | None = None

    def status(self) -> dict:
        return {"enabled": self.cfg.enabled, "running": self.running, "last": self.last,
                "synced_at": self.synced_at or None, "check_minutes": self.cfg.check_minutes}

    def start(self) -> None:
        if self.cfg.enabled and self.task is None:
            self.task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - going away
                pass
            self.task = None

    async def _loop(self) -> None:
        await asyncio.sleep(START_DELAY)
        reason = "the manager started"
        while True:
            try:
                await self.check(reason=reason)
            except Exception:  # noqa: BLE001 - never stop the loop
                log.exception("ShotGrid menu sync failed")
            reason = None
            await asyncio.sleep(max(self.cfg.check_minutes, 0.5) * 60)

    def _skip(self, reason: str, why: str) -> dict:
        if not (self.last and self.last.get("why") == why):     # said once, not every check
            log.warning("ShotGrid menu not synced: %s", why)
        self.last = {"at": time.time(), "reason": reason, "ok": False, "why": why}
        return self.last

    async def check(self, force: bool = False, reason: str | None = None) -> dict | None:
        """Sync when it's due (or `force`): the last result, or None when nothing was due."""
        if self.running:
            return self.last
        menu = self.menu()
        h = digest(menu)
        changed = h != self.synced
        daily = time.time() - self.synced_at >= self.cfg.every_hours * 3600
        if not (force or changed or daily):
            return None
        reason = reason or ("asked" if force else "the menu changed" if changed else f"every {self.cfg.every_hours:g} h")
        if menu.get("problems"):
            return self._skip(reason, "the menu has problems: " + "; ".join(menu["problems"]))
        if not menu.get("farm_url"):
            return self._skip(reason, "deploy/shotgrid/menu.yaml has no farm_url (the address ShotGrid's menu posts to)")
        if menu.get("shows") and not menu.get("items"):
            return self._skip(reason, "the menu came out empty though it names shows (did the workflows load?)")
        return await self._sync(menu, h, reason)

    async def _sync(self, menu: dict, h: str, reason: str) -> dict:
        self.running = True
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(menu, indent=1), encoding="utf-8")
            argv = dispatch.command(self.cfg.shell, self.cfg.command,
                                    {"tool": self.tool, "menu": self.file, "farm": menu["farm_url"]},
                                    "shotgrid.menu_sync.command")
            log.info("ShotGrid menu sync (%s): %s", reason, argv[-1])
            code, output = await asyncio.to_thread(self._run, argv, self.cfg.timeout)
        except ValueError as e:                          # the command names a value it doesn't have
            code, output = 2, str(e)
        finally:
            self.running = False
        n = counts(output) if code == 0 else None
        ok = code == 0 and n is not None
        self.last = {"at": time.time(), "reason": reason, "ok": ok, "exit": code, **(n or {}),
                     "why": None if ok else _why(code, output), "output": (output or "")[-OUTPUT_KEPT:]}
        if ok:
            self.synced, self.synced_at = h, time.time()
            log.info("ShotGrid menu synced: %d created, %d changed, %d deleted", n["created"], n["changed"], n["deleted"])
        else:
            log.warning("ShotGrid menu sync failed (exit %s): %s", code, self.last["why"])
        return self.last


def _why(code: int, output: str) -> str:
    """The gist of a failed run: its last meaningful line."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip() and "DeprecationWarning" not in ln]
    tail = lines[-1] if lines else ""
    if code == 124:
        return "it didn't finish in time" + (f" ({tail})" if tail else "")
    if code == 127:
        return f"the command wasn't found ({tail})"
    return tail[:400] or f"exit {code}, no output"
