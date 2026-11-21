"""The farm's shared folder (`share:` in config.yaml), e.g. /rdo/bank/comfy_farm:

    manager.json     where the manager is: written at every start
    client/          the repo's client/ (comfyfarm_client.py, nuke/), copied at every start

Artists put <dir>/client/nuke on NUKE_PATH; the client finds manager.json in the folder
above its own, and agents can name the file instead of an address (`manager:` in
agent.yaml). So no config holds a machine name, the manager can move by restarting it
somewhere else, and the client artists run always matches the manager. Everything here
is readable by everyone."""

from __future__ import annotations

import filecmp
import json
import logging
import os
import socket
import uuid
from pathlib import Path

from ..config import Config
from ..fileio import paths as P
from ..fileio.outputs import makedirs_readable, readable
from ..protocol import build, now

log = logging.getLogger("comfyfarm")

ADDRESS_FILE = "manager.json"
REPO = Path(__file__).resolve().parent.parent.parent
_SKIP = {"__pycache__"}


def manager_url(cfg: Config) -> str:
    """The address artists and agents use: share.url, else this machine's name and port."""
    if cfg.share.url:
        return cfg.share.url.rstrip("/")
    host = cfg.server.host
    if host in ("", "0.0.0.0", "::"):
        host = socket.gethostname()
    return f"http://{host}:{cfg.server.port}"


def share_dir(cfg: Config) -> str | None:
    """share.dir as this machine sees it (a studio path works on Windows too)."""
    return P.to_farm(cfg.share.dir, cfg.paths.mappings).rstrip("/") if cfg.share.dir else None


def _write(path: str, data: bytes) -> None:
    """Replace path with data in one step, so a reader never sees half a file."""
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    readable(path)


def _sync(src: Path, dst: str) -> int:
    """Mirror src into dst (files that changed, and nothing that's gone). Returns the
    number of files written."""
    makedirs_readable(dst)
    written = 0
    names = {n for n in os.listdir(src) if n not in _SKIP and not n.endswith((".pyc", ".pyo"))}
    for n in sorted(names):
        s, d = src / n, f"{dst}/{n}"
        if s.is_dir():
            written += _sync(s, d)
        elif not (os.path.isfile(d) and filecmp.cmp(s, d, shallow=False)):
            _write(d, s.read_bytes())
            written += 1
    for n in os.listdir(dst):
        if n in names or n in _SKIP:
            continue
        d = f"{dst}/{n}"
        if os.path.isfile(d) and not os.path.islink(d):
            os.remove(d)                # a file the client no longer has
    return written


def publish(cfg: Config, client_dir: Path | None = None) -> dict | None:
    """Write manager.json and copy the client into share.dir. A shared folder that can't be
    written is logged, not fatal: the farm runs, artists just don't find it yet."""
    d = share_dir(cfg)
    if not d:
        return None
    info = {"url": manager_url(cfg), "version": build(), "host": socket.gethostname(), "pid": os.getpid(),
            "started": now(), "config": P.normalize(str(cfg.base_dir))}
    try:
        makedirs_readable(d)
        _write(f"{d}/{ADDRESS_FILE}", (json.dumps(info, indent=2) + "\n").encode())
        n = _sync(client_dir or REPO / "client", f"{d}/client")
    except OSError as e:
        log.error("shared folder %s: %s. Artists and agents that read %s won't find the manager.",
                  d, e.strerror or e, ADDRESS_FILE)
        return None
    log.info("shared folder %s: manager.json -> %s%s", d, info["url"], f"; {n} client file(s) updated" if n else "")
    return info


__all__ = ["ADDRESS_FILE", "manager_url", "share_dir", "publish"]


def releases(cfg: Config) -> list[dict]:
    """The runner environments' releases in the bank (share.dir), newest first (decision
    0028). Read from each release.json; installing one checks it."""
    if not cfg.share.dir:
        return []
    from ..envs import release as R
    try:
        return R.listed(Path(cfg.share.dir))
    except OSError as e:
        log.warning("can't read the releases in %s: %s", cfg.share.dir, e)
        return []


def release_fingerprint(cfg: Config, name: str, which: str) -> str:
    """A release in the bank by fingerprint or "latest", checked against its hashes before any
    machine is told to install it (decision 0030). ReleaseError says what is wrong."""
    from ..envs import release as R
    if not cfg.share.dir:
        raise R.ReleaseError("the manager has no bank (share.dir in config.yaml), so no releases")
    return R.find(Path(cfg.share.dir), name, which).fingerprint
