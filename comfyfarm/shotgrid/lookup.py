"""Shows, sequences and shots from ShotGrid, for the publish fields' dropdowns.

Connecting: the manager's own read-only API script, SG_SCRIPT_NAME and SG_SCRIPT_KEY
(shotgrid.script_name_env / script_key_env). They're taken from the manager's
environment, or else from shotgrid.env_file (~/.config/comfyfarm/shotgrid.env, KEY=value
lines, readable only by its owner), which the manager reads itself so it works however
it's started (cron, farmctl, by hand). The key is never sent anywhere but ShotGrid.

What's offered:
  shows      Projects matching shotgrid.project_filters (sg_status is Active): the show
             code (shotgrid.show_field, tank_name) and the name
  shots      a show's Shots, leaving out the statuses in shotgrid.hidden_statuses
             (hld omt hdn void): code, sequence, status. The sequences are those of its
             shots; they only narrow the shot list down.

The lists are cached (projects_ttl, shots_ttl) and reloaded on demand (the refresh
buttons, when a shot was just created), at most once per refresh_hold seconds. When
ShotGrid isn't set up or doesn't answer, every call says why and the fields stay free
text: publishing never depends on ShotGrid. A ShotGrid that didn't answer is left alone
for retry_seconds.

check() is what publishing asks before it dispatches: a shot that isn't in the show is
refused (after reloading the show's shots once, in case it's new)."""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from pathlib import Path

log = logging.getLogger("comfyfarm.shotgrid")

__all__ = ["ShotGrid", "Unavailable", "read_env_file"]


class Unavailable(Exception):
    """ShotGrid can't be asked right now (the message says why)."""


def read_env_file(path) -> dict[str, str]:
    """KEY=value lines (# comments, `export KEY=value` and quotes allowed)."""
    out = {}
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


class ShotGrid:
    """The manager's ShotGrid: cached lists, and the publish check. `connect` (tests):
    a function returning a shotgun_api3.Shotgun-like object (find, find_one). `keep`:
    which show codes to offer (the farm: those with a show folder on disk)."""

    def __init__(self, cfg, connect=None, keep=None):
        self.cfg = cfg
        self._connect = connect
        self._keep = keep
        self._sg = None
        self._lock = threading.Lock()           # one ShotGrid call at a time: a connection isn't thread-safe
        self._projects: tuple[float, list[dict]] | None = None
        self._shots: dict[str, tuple[float, list[dict]]] = {}
        self._forced: dict[str, float] = {}
        self._down_until = 0.0
        self._down_why: str | None = None
        self._warned_mode = False

    # ---------------------------------------------------------------- set up?
    def credentials(self) -> tuple[str | None, str | None]:
        name = os.environ.get(self.cfg.script_name_env)
        key = os.environ.get(self.cfg.script_key_env)
        if not (name and key) and self.cfg.env_file:
            path = Path(self.cfg.env_file).expanduser()
            values = read_env_file(path)
            name, key = name or values.get(self.cfg.script_name_env), key or values.get(self.cfg.script_key_env)
            if key and os.name != "nt" and not self._warned_mode:
                try:
                    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                        log.warning("%s can be read by others: chmod 600 it (it holds the ShotGrid key)", path)
                except OSError:
                    pass
                self._warned_mode = True
        return name, key

    def why(self) -> str | None:
        """Why ShotGrid isn't used right now, or None when it is."""
        why = self.setup_why()
        if why:
            return why
        if time.time() < self._down_until:
            return self._down_why
        return None

    def setup_why(self) -> str | None:
        """Why ShotGrid isn't set up for the manager (off, no shotgun_api3, no script), or
        None. The dashboard hides the ShotGrid parts then, rather than warn."""
        if not self.cfg.enabled:
            return "ShotGrid lookups are off (shotgrid.enabled in the manager's config.yaml)."
        if self._connect is None:
            try:
                import shotgun_api3  # noqa: F401
            except ImportError:
                return "shotgun_api3 isn't installed in the manager's Python (README: ShotGrid lookups)."
            name, key = self.credentials()
            if not (name and key):
                return (f"The manager has no ShotGrid script: {self.cfg.script_name_env} and "
                        f"{self.cfg.script_key_env} aren't set, in its environment or {self.cfg.env_file}.")
        return None

    def status(self) -> dict:
        why = self.why()
        return {"enabled": why is None, "set_up": self.setup_why() is None, "why": why, "url": self.cfg.url,
                "projects_at": self._projects[0] if self._projects else None,
                "shows_cached": sorted(self._shots)}

    # ---------------------------------------------------------------- asking ShotGrid
    def _conn(self):
        if self._sg is None:
            if self._connect is not None:
                self._sg = self._connect()
            else:
                import shotgun_api3
                name, key = self.credentials()
                self._sg = shotgun_api3.Shotgun(self.cfg.url, script_name=name, api_key=key, connect=False)
                try:
                    self._sg.config.timeout_secs = self.cfg.timeout
                except AttributeError:
                    pass
        return self._sg

    def _ask(self, method: str, *args, **kwargs):
        with self._lock:
            try:
                return getattr(self._conn(), method)(*args, **kwargs)
            except Exception as e:                   # anything: ShotGrid is an aid, never a reason to fail
                self._sg = None
                msg = " ".join(str(e).split())[:200]
                self._down_why = (f"ShotGrid didn't answer ({type(e).__name__}{': ' + msg if msg else ''}); "
                                  f"trying again in {self.cfg.retry_seconds:.0f} s.")
                self._down_until = time.time() + self.cfg.retry_seconds
                log.warning("ShotGrid %s failed: %s", method, self._down_why)
                raise Unavailable(self._down_why) from None

    def _fresh(self, cached, ttl: float, key: str, refresh: bool) -> bool:
        """Whether a cached list can be used: not too old, and no refresh asked for (a
        refresh is honoured once per refresh_hold seconds)."""
        if cached is None:
            return False
        if refresh and time.time() - self._forced.get(key, 0) >= self.cfg.refresh_hold:
            self._forced[key] = time.time()
            return False
        return time.time() - cached[0] < ttl

    # ---------------------------------------------------------------- the lists
    def projects(self, refresh: bool = False) -> dict:
        """{"projects": [{code, name}], "fetched_at", "why"}: the active shows."""
        why = self.why()
        cached = self._projects
        if why:
            return {"projects": self._public(cached), "fetched_at": cached[0] if cached else None, "why": why,
                    "set_up": self.setup_why() is None}
        if not self._fresh(cached, self.cfg.projects_ttl, "projects", refresh):
            try:
                rows = self._ask("find", "Project", self.cfg.project_filters, ["name", "tank_name", "code"])
            except Unavailable as e:
                return {"projects": self._public(cached), "fetched_at": cached[0] if cached else None, "why": str(e),
                        "set_up": True}
            found = []
            for r in rows or []:
                code = r.get(self.cfg.show_field) or r.get("tank_name") or r.get("code")
                if code and (self._keep is None or self._keep(str(code))):
                    found.append({"code": str(code), "name": str(r.get("name") or code), "id": r.get("id")})
            found.sort(key=lambda p: p["code"].lower())
            cached = self._projects = (time.time(), found)
        return {"projects": self._public(cached), "fetched_at": cached[0], "why": None, "set_up": True}

    @staticmethod
    def _public(cached) -> list[dict]:
        return [{"code": p["code"], "name": p["name"]} for p in (cached[1] if cached else [])]

    def _project(self, show: str, refresh: bool = False) -> dict | None:
        self.projects(refresh)
        want = show.strip().lower()
        return next((p for p in (self._projects[1] if self._projects else []) if p["code"].lower() == want), None)

    def shots(self, show: str, refresh: bool = False) -> dict:
        """{"show", "sequences", "shots": [{code, sequence, status}], "fetched_at", "why"}."""
        out = {"show": show, "sequences": [], "shots": [], "fetched_at": None, "why": None, "known_show": None,
               "set_up": self.setup_why() is None}
        why = self.why()
        key = show.strip().lower()
        cached = self._shots.get(key)
        if why:
            return self._shot_list(out, cached, why)
        if refresh:
            self.projects(refresh=True)      # a new show, too
        project = self._project(show)
        if project is None:
            if self.why():
                return self._shot_list(out, cached, self.why())
            out["known_show"] = False
            out["why"] = f"{show} isn't one of the active shows in ShotGrid."
            return out
        out["show"] = project["code"]
        if not self._fresh(cached, self.cfg.shots_ttl, "shots:" + key, refresh):
            filters = [["project", "is", {"type": "Project", "id": project["id"]}]]
            if self.cfg.hidden_statuses:
                filters.append(["sg_status_list", "not_in", list(self.cfg.hidden_statuses)])
            try:
                rows = self._ask("find", "Shot", filters, ["code", "sg_sequence", "sg_status_list"])
            except Unavailable as e:
                return self._shot_list(out, cached, str(e))
            found = [{"code": str(r["code"]), "sequence": (r.get("sg_sequence") or {}).get("name"),
                      "status": r.get("sg_status_list")} for r in rows or [] if r.get("code")]
            found.sort(key=lambda s: s["code"].lower())
            cached = self._shots[key] = (time.time(), found)
        out["known_show"] = True
        return self._shot_list(out, cached, None)

    @staticmethod
    def _shot_list(out: dict, cached, why: str | None) -> dict:
        shots = cached[1] if cached else []
        out.update(shots=shots, sequences=sorted({s["sequence"] for s in shots if s["sequence"]}, key=str.lower),
                   fetched_at=cached[0] if cached else None, why=why)
        return out

    # ---------------------------------------------------------------- publishing
    def check(self, show: str, shot: str) -> str | None:
        """Why a publish can't go to this shot, or None (also when ShotGrid can't be asked:
        Daiquiri has the last word)."""
        if self.why():
            return None
        data = self.shots(show)
        if data["known_show"] is False:
            # Not an active show: it's refused only when it isn't in ShotGrid at all.
            try:
                found = self._ask("find_one", "Project", [[self.cfg.show_field, "is", show]], ["id"])
            except Unavailable:
                return None
            return None if found else f"There's no show {show} in ShotGrid."
        if data["why"]:
            return None
        want = shot.strip().lower()
        if any(s["code"].lower() == want for s in data["shots"]):
            return None
        data = self.shots(show, refresh=True)          # created since the list was loaded?
        if data["why"] or any(s["code"].lower() == want for s in data["shots"]):
            return None
        project = self._project(show)
        try:                                             # there, with a status the lists leave out
            found = self._ask("find_one", "Shot", [["project", "is", {"type": "Project", "id": project["id"]}],
                                                   ["code", "is", shot]], ["sg_status_list"])
        except Unavailable:
            return None
        return None if found else f"There's no shot {shot} in {data['show']} in ShotGrid."
