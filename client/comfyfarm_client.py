"""ComfyUI farm client. Standard library only, so it runs inside Nuke, Houdini, Maya
or any Python 3.7+ without installing anything. Grab it from the farm itself:

    http://<farm-machine>:8000/client.py

Settings (environment variables):
    COMFYFARM_URL      e.g. http://comfyfarm01:8000   (required)
    COMFYFARM_API_KEY  only if the farm is configured with API keys

From Python:
    import comfyfarm_client as farm
    job = farm.submit("ltx25_alpha_gen", {"input": "/rdo/shows/.../plate.%04d.png 1001-1158",
                                          "positive_prompt": "..."})
    job = farm.wait(job["id"], on_update=lambda j: print(j["status"], j["progress"]))
    print(job["outputs"]["frame_pattern_posix"])

From a shell:
    python comfyfarm_client.py workflows
    python comfyfarm_client.py submit ltx25_alpha_gen input="/rdo/shows/.../x.%04d.png 1001-1158" seed=7 --wait
    python comfyfarm_client.py status <job id>
    python comfyfarm_client.py list [--mine]
    python comfyfarm_client.py cancel <job id>
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FINISHED = ("done", "failed", "cancelled")


class FarmError(Exception):
    pass


def _base() -> str:
    url = os.environ.get("COMFYFARM_URL", "").rstrip("/")
    if not url:
        raise FarmError("Set COMFYFARM_URL to the farm address, e.g. http://comfyfarm01:8000")
    return url


def _request(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    headers = {"Accept": "application/json", "X-User": getpass.getuser()}
    key = os.environ.get("COMFYFARM_API_KEY")
    if key:
        headers["X-API-Key"] = key
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(_base() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        raise FarmError(detail if isinstance(detail, str) else f"HTTP {e.code}: {detail or e.reason}") from None
    except urllib.error.URLError as e:
        raise FarmError(f"Can't reach the farm at {_base()}: {e.reason}") from None


# ---------------------------------------------------------------- API
def workflows() -> list:
    return _request("GET", "/api/v1/workflows")["workflows"]


def submit(workflow: str, params: dict, name: str | None = None, priority: int = 0,
           show: str | None = None, shot: str | None = None) -> dict:
    """Queue a job. Raises FarmError with a readable message if the input is unusable."""
    return _request("POST", "/api/v1/jobs", {"workflow": workflow, "params": params, "name": name,
                                             "priority": priority, "show": show, "shot": shot,
                                             "user": getpass.getuser()})


def get(job_id: str) -> dict:
    return _request("GET", "/api/v1/jobs/" + urllib.parse.quote(job_id))


def jobs(user: str | None = None, active: bool = False, limit: int = 50) -> list:
    q = {"limit": limit}
    if user:
        q["user"] = user
    if active:
        q["active"] = "true"
    return _request("GET", "/api/v1/jobs?" + urllib.parse.urlencode(q))["jobs"]


def cancel(job_id: str) -> dict:
    return _request("DELETE", "/api/v1/jobs/" + urllib.parse.quote(job_id))


def retry(job_id: str) -> dict:
    return _request("POST", f"/api/v1/jobs/{urllib.parse.quote(job_id)}/retry")


def wait(job_id: str, poll: float = 3.0, timeout: float | None = None, on_update=None) -> dict:
    """Block until the job finishes. on_update(job) is called whenever it changes.
    In a DCC, call this from a thread, not the UI thread."""
    start, last = time.time(), None
    while True:
        job = get(job_id)
        key = (job["status"], round(job.get("progress") or 0, 2), job.get("stage"))
        if on_update and key != last:
            on_update(job)
        last = key
        if job["status"] in FINISHED:
            return job
        if timeout and time.time() - start > timeout:
            raise FarmError(f"Timed out waiting for {job_id}")
        time.sleep(poll)


def output_pattern(job: dict, posix: bool | None = None) -> str | None:
    """Frame pattern of a finished job, as this machine sees it."""
    out = job.get("outputs") or {}
    if posix is None:
        posix = os.name != "nt"
    return out.get("frame_pattern_posix" if posix else "frame_pattern") or out.get("frame_pattern")


def describe(job: dict) -> str:
    pct = int(round((job.get("progress") or 0) * 100))
    s = f"{job['short_id']}  {job['status']:<14} {pct:>3}%  {job.get('name') or ''}  ({job['user']})"
    if job["status"] == "queued" and job.get("queue_position"):
        s += f"  #{job['queue_position']} in queue"
    if job.get("eta_seconds") and job["status"] not in FINISHED:
        s += f"  ~{job['eta_seconds'] // 60}m{job['eta_seconds'] % 60:02d}s"
    if job.get("error"):
        s += f"\n    error: {job['error']}"
    for w in job.get("warnings") or []:
        s += f"\n    warning: {w}"
    if job["status"] == "done":
        s += f"\n    frames: {output_pattern(job)}  {job['outputs'].get('first')}-{job['outputs'].get('last')}"
    return s


# ---------------------------------------------------------------- CLI
def _parse_value(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "workflows":
            for wf in workflows():
                print(f"{wf['name']}  {'' if wf['enabled'] else '(disabled) '}{wf['label']}")
                for p in wf["params"]:
                    req = " (required)" if p["required"] else f" = {p['default']!r}"
                    print(f"    {p['name']:<18} {p['type']:<9}{req}")
        elif cmd == "submit":
            wf, flags = args[0], [a for a in args[1:] if a.startswith("--")]
            params = dict(a.split("=", 1) for a in args[1:] if "=" in a and not a.startswith("--"))
            params = {k: (v if k == "input" else _parse_value(v)) for k, v in params.items()}
            job = submit(wf, params)
            print(describe(job))
            print(f"    output: {job['info'].get('output_dir_posix') or job['info'].get('output_dir')}")
            if "--wait" in flags:
                job = wait(job["id"], on_update=lambda j: print(describe(j).splitlines()[0]))
                print(describe(job))
                return 0 if job["status"] == "done" else 1
        elif cmd == "status":
            print(describe(get(args[0])))
        elif cmd == "list":
            for j in jobs(user=getpass.getuser() if "--mine" in args else None, active="--all" not in args):
                print(describe(j))
        elif cmd == "cancel":
            print(describe(cancel(args[0])))
        elif cmd == "retry":
            print(describe(retry(args[0])))
        else:
            print(f"unknown command {cmd}\n{__doc__}")
            return 2
    except FarmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
