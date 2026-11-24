"""The API runner and its adapters (decisions 0055, 0056).

An API definition runs at an outside provider, on the farm's gateway: runner.py is the
runner (base.Runner), and each provider has an adapter here, named by its providers/<name>.yaml
(`adapter: fal` is fal.py). The runner knows jobs, the run folder and the farm's files;
an adapter knows one provider's HTTP API and its keys, which it sends to that provider and
nowhere else.

An adapter module has

    NEEDS               the keys (providers/<name>.yaml's keys) it uses now
    check(wf)           what's wrong with a definition for this provider: the same on every
                        machine (`broken`, decision 0020)
    Adapter(keys, url, media_seconds, transport)
      problems()        why it can't send anything (no key, a URL it may not use)
      upload(path, content_type) -> url      an input, where the provider can read it
      body(api, values, urls) -> dict        what is sent: fixed, sent and uploaded fields
      submit(endpoint, body) -> request      {"id": ...} and what it needs to follow it
      status(request) -> {"state": queued | running | done | failed, "position", "error",
                          "error_type", "metrics"}
      result(request) -> dict                what the provider says it made
      outputs(result) -> [url]               the files it made, in order
      cancel(request) -> removed | requested | completed | unknown
      download(url, dest)
      close()

Everything that goes wrong is an ApiError, whose message is for the artist. Its `kind` says
what the runner may do: `refused` (the provider said no: never sent again), `transient`
(asking again is safe) or `unknown` (the job may have reached the provider: never sent
again, so it is never paid for twice)."""

from __future__ import annotations

ADAPTERS = ("fal",)


class ApiError(Exception):
    """A provider's answer, or no answer, said for the artist."""

    def __init__(self, message: str, kind: str = "refused"):
        super().__init__(message)
        self.kind = kind                # refused | transient | unknown


def module(name: str):
    """An adapter's module by its name in providers/ (None: no such adapter). Imported when
    asked, so the farm needs none of them until a gateway uses one."""
    if name not in ADAPTERS:
        return None
    from importlib import import_module
    return import_module(f"{__name__}.{name}")


__all__ = ["ADAPTERS", "ApiError", "module"]
