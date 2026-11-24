"""The farm's policy (decisions 0007, 0043): `policy.yaml` in the bank folder, edited by the
farm's admins, read by the manager and checked when a job is planned.

It holds show rules (which shows may use the farm, and with which definitions), licence
rules (which licences the models a definition uses may carry, on which shows: each
definition lists its models in its process.yaml, decision 0044), quotas (how much a show,
or a user on it, may have queued or running at once, decision 0045) and provider rules
(which shows may send work to which outside provider, decision 0058: none until a rule
allows it). A job a rule refuses is never planned, and the artist is told which rule and
why.

The file is read again when it changes. A file that can't be read or doesn't validate is
not taken: the last valid one stays in force (also across a restart: the manager keeps a
copy in its data folder), and the error is shown (`GET /api/v1/policy`). With policy on and
no valid file ever read, nothing is planned: a client's restriction must not lapse because
of a typo."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..fileio import paths as P
from ..protocol import now

log = logging.getLogger("comfyfarm")

__all__ = ["Policy", "PolicyFile", "Refused", "parse"]

STAT_SECONDS = 2.0          # the file is looked at no more often than this


class Refused(Exception):
    """A job the policy doesn't allow: the message names the rule and its reason."""

    def __init__(self, message: str, rule: str | None = None):
        super().__init__(message)
        self.rule = rule


class _Rule(BaseModel):
    """What every rule has: its name, the shows it is for (but not those in `except`), and
    the reason the artist reads."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    name: str = Field(min_length=1)                 # named when it refuses a job
    shows: list[str] = Field(min_length=1)          # show codes; patterns work (ret*; * is every job)
    except_: list[str] = Field(default_factory=list, alias="except")   # shows it isn't for, though `shows` names them
    reason: str = Field(min_length=1)               # what the artist reads

    def matches(self, show: str) -> bool:
        """`show` is "" for a job whose show isn't known: only `*` matches it."""
        s = show.lower()
        return (any(fnmatch.fnmatchcase(s, p.lower()) for p in self.shows)
                and not any(fnmatch.fnmatchcase(s, p.lower()) for p in self.except_))


class ShowRule(_Rule):
    """A rule for some shows: `deny` all definitions or some, or `allow` only some."""
    deny: Literal["all"] | list[str] | None = None
    allow: list[str] | None = None

    @field_validator("deny", mode="before")
    @classmethod
    def _deny(cls, v):
        if isinstance(v, str) and v != "all":
            raise ValueError(f"deny is all, or a list of definitions, not {v!r}")
        return v

    @model_validator(mode="after")
    def _one(self):
        if (self.deny is None) == (self.allow is None):
            raise ValueError(f"rule {self.name}: give either deny (all, or a list of definitions) or allow "
                             f"(the only definitions it may run)")
        return self

    def definitions(self) -> list[str]:
        return list(self.allow or []) + (list(self.deny) if isinstance(self.deny, list) else [])

    def refuses(self, definition: str) -> bool:
        if self.deny == "all":
            return True
        if isinstance(self.deny, list):
            return definition in self.deny
        return definition not in (self.allow or [])


class LicenceRule(_Rule):
    """A rule for the licences of the models a definition uses: `deny` some, or `allow` only
    some. Licences are compared without case: apache-2.0 is Apache-2.0."""
    deny: list[str] | None = None
    allow: list[str] | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.deny is None) == (self.allow is None):
            raise ValueError(f"rule {self.name}: give either deny (the licences it refuses) or allow (the only "
                             f"licences it takes)")
        return self

    def licences(self) -> list[str]:
        return list(self.deny or []) + list(self.allow or [])

    def offending(self, models: list[dict]) -> dict | None:
        """The first model whose licence this rule refuses."""
        deny = {x.strip().lower() for x in self.deny or []}
        allow = {x.strip().lower() for x in self.allow or []}
        for m in models:
            lic = str(m.get("licence", "")).strip().lower()
            if (self.deny is not None and lic in deny) or (self.allow is not None and lic not in allow):
                return m
        return None


class ProviderRule(_Rule):
    """A rule for where a show's jobs may run (decision 0058): `allow` some outside providers
    (off-site, providers/<name>.yaml), which no show may use until a rule allows it; or `deny`
    some providers, the farm's own too. A deny wins over an allow."""
    allow: list[str] | None = None
    deny: list[str] | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.deny is None) == (self.allow is None):
            raise ValueError(f"rule {self.name}: give either allow (the outside providers its shows may send work "
                             f"to) or deny (the providers they may not use)")
        return self

    def providers(self) -> list[str]:
        return list(self.allow or []) + list(self.deny or [])


class QuotaRule(_Rule):
    """At most `max_jobs` submissions and/or `max_frames` frames (tiles, for textures) queued or
    running at once, for its shows: each show its own (`per: show`), each user on those shows
    their own (`per: user`), or the shows together (`per: all`). A texture set is one
    submission; its frames are its unfinished batches' tiles."""
    per: Literal["show", "user", "all"] = "show"
    max_jobs: int | None = Field(None, ge=1)
    max_frames: int | None = Field(None, ge=1)

    def shows_text(self) -> str:
        """Its shows as people read them: every show but rnd*, shows cold, ret*."""
        names = "every show" if "*" in self.shows else f"shows {', '.join(self.shows)}"
        return names + (f" but {', '.join(self.except_)}" if self.except_ else "")

    @model_validator(mode="after")
    def _some(self):
        if self.max_jobs is None and self.max_frames is None:
            raise ValueError(f"rule {self.name}: give max_jobs, max_frames or both")
        return self


class PolicyFile(BaseModel):
    """What policy.yaml holds (version 1)."""
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    contact: str = ""                               # who to ask, named in every refusal
    other_shows: Literal["allow", "deny"] = "allow"  # shows no rule names
    unknown_show: Literal["allow", "deny"] = "allow"  # a job whose show can't be worked out
    shows: list[ShowRule] = Field(default_factory=list)
    licences: list[LicenceRule] = Field(default_factory=list)
    # A definition that doesn't list its models, where a licence rule is for its show.
    unlisted_models: Literal["allow", "deny"] = "allow"
    quotas: list[QuotaRule] = Field(default_factory=list)
    providers: list[ProviderRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _names(self):
        seen = set()
        for r in [*self.shows, *self.licences, *self.quotas, *self.providers]:
            if r.name in seen:
                raise ValueError(f"two rules are named {r.name}: each needs its own name")
            seen.add(r.name)
        return self


def parse(text: str, known: set[str] | None = None, providers: dict[str, bool] | None = None) -> PolicyFile:
    """policy.yaml's text, validated. ValueError says what is wrong, in words an admin can
    act on. `known`: the definitions this farm has; a rule naming another one is an error
    (a typo in a deny list would otherwise deny nothing). `providers`: the farm's providers
    (providers/), each with whether it is off-site; a provider rule naming another one is an
    error, as is one that allows a provider of the farm's own (only outside ones need it)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"not valid YAML: {' '.join(str(e).split())}") from None
    if not isinstance(data, dict):
        raise ValueError("it should be a mapping, starting with `version: 1`")
    try:
        pf = PolicyFile.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0]
        # Without pydantic's names for the branches of a union (literal['all'], list[str]).
        where = ".".join(str(x) for x in first["loc"] if not (isinstance(x, str) and "[" in x)) or "the file"
        more = f" (and {len(e.errors()) - 1} more)" if len(e.errors()) > 1 else ""
        raise ValueError(f"{where}: {first['msg'].removeprefix('Value error, ')}{more}") from None
    if known is not None:
        for r in pf.shows:
            unknown = sorted(set(r.definitions()) - known)
            if unknown:
                raise ValueError(f"rule {r.name} names {', '.join(unknown)}, which this farm doesn't have "
                                 f"(it has {', '.join(sorted(known))})")
    if providers is not None:
        for r in pf.providers:
            unknown = sorted(set(r.providers()) - set(providers))
            if unknown:
                raise ValueError(f"rule {r.name} names provider {', '.join(unknown)}, which this farm doesn't have "
                                 f"(it has {', '.join(sorted(providers))})")
            own = sorted(p for p in r.allow or [] if not providers[p])
            if own:
                raise ValueError(f"rule {r.name} allows {', '.join(own)}, the farm's own: only outside providers need "
                                 f"a rule that allows them (deny it to refuse it)")
    return pf


def _when(t: float | None) -> str | None:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None


class Policy:
    """The policy in force, read from `path` when it changes. Thread-safe: plans run in
    threads. With `enabled` off it allows everything and reads nothing."""

    def __init__(self, enabled: bool, path: str | None, saved: Path, known: Callable[[], set[str]],
                 shown_path: str | None = None, providers: Callable[[], dict[str, bool]] | None = None):
        self.enabled = enabled
        self.path = path
        self.shown_path = shown_path or path
        self.saved = saved                          # the last valid file, kept across restarts
        self.known = known
        self.providers = providers                  # the farm's providers, each off-site or not (providers/)
        self._lock = threading.Lock()
        self._seen: tuple | None = None             # (mtime, size) of the file last read
        self._looked = 0.0
        self.rules: PolicyFile | None = None
        self.fingerprint: str | None = None
        self.taken_at: float | None = None          # when the rules in force were read
        self.source: str | None = None              # "file", or "saved copy" after a restart
        self.error: str | None = None               # why the file as it is now isn't in force
        if enabled:
            self._restore()
            self.refresh(force=True)

    # ---------------------------------------------------------------- reading
    def _restore(self) -> None:
        """The last valid file, from the copy kept beside the database. Not checked against
        today's definitions: it was valid when it was taken, and a definition since removed
        from the repo must not throw out every rule."""
        try:
            kept = json.loads(self.saved.read_text(encoding="utf-8"))
            self.rules = parse(kept["text"])
            self.fingerprint, self.taken_at, self.source = kept["fingerprint"], kept["taken_at"], "saved copy"
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("policy: the saved copy %s can't be used: %s", self.saved, e)

    def refresh(self, force: bool = False) -> None:
        """Read the file again if it changed (at most every STAT_SECONDS)."""
        if not self.enabled:
            return
        with self._lock:
            t = now()
            if not force and t - self._looked < STAT_SECONDS:
                return
            self._looked = t
            try:
                st = os.stat(self.path)
            except OSError as e:
                if self._seen != ("missing",):
                    self._seen = ("missing",)
                    self.error = (f"{self.shown_path} can't be read ({e.strerror or e})"
                                  + (": the last valid version stays in force." if self.rules else "."))
                    log.warning("policy: %s", self.error)
                return
            if (st.st_mtime, st.st_size) == self._seen:
                return
            self._seen = (st.st_mtime, st.st_size)
            try:
                text = Path(self.path).read_text(encoding="utf-8")
                rules = parse(text, self.known(), self.providers() if self.providers else None)
            except (OSError, ValueError) as e:
                self.error = (f"{self.shown_path} isn't taken: {e}"
                              + (". The last valid version stays in force." if self.rules else "."))
                log.warning("policy: %s", self.error)
                return
            fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
            self.error = None
            if fingerprint == self.fingerprint:
                return
            self.rules, self.fingerprint, self.taken_at, self.source = rules, fingerprint, t, "file"
            log.info("policy: %s taken (%s, %d show rule(s), %d provider rule(s))", self.shown_path, fingerprint,
                     len(rules.shows), len(rules.providers))
            try:
                self.saved.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.saved.with_suffix(".tmp")
                tmp.write_text(json.dumps({"text": text, "fingerprint": fingerprint, "taken_at": t}),
                               encoding="utf-8")
                os.replace(tmp, self.saved)
            except OSError as e:
                log.warning("policy: couldn't keep a copy in %s: %s", self.saved, e)

    # ---------------------------------------------------------------- checking
    def check(self, definition: str, show: str | None, models: list[dict] | None = None, provider=None,
              any_show: bool = False) -> None:
        """Raise Refused if the policy in force doesn't allow `definition` for `show`.
        `models`: the definition's models with their licences (process.yaml), None when it
        doesn't list them. `provider`: where it runs (catalog.Provider: name, label, offsite);
        `any_show`: for menus drawn without a show, which allow an outside provider some rule
        allows on some show (the job's own show is checked when it is planned)."""
        if not self.enabled:
            self.offsite(definition, show, provider, any_show)
            return
        self.refresh()
        rules = self.rules
        if rules is None:
            raise Refused(f"The farm's policy can't be read, so no job can be planned until it is fixed: "
                          f"{self.error or 'no valid policy.yaml yet'}.", None)
        ask = f" Ask {rules.contact} if this is wrong." if rules.contact else ""
        if not show and rules.unknown_show == "deny":
            raise Refused("The farm's policy needs the show to be known: give a Show (or an input path "
                          f"inside a show), so its rules can be checked.{ask}", "unknown_show")
        where = f"for show {show}" if show else "without a known show"
        matched = [r for r in rules.shows if r.matches(show or "")]
        for r in matched:
            if r.refuses(definition):
                raise Refused(f"{definition} can't run {where}: {r.reason.rstrip('.')}. "
                              f"(Policy rule {r.name}.){ask}", r.name)
        if show and not matched and rules.other_shows == "deny":
            raise Refused(f"Show {show} isn't cleared to use the farm: no policy rule allows it. "
                          f"(Policy: other_shows is deny.){ask}", "other_shows")
        self.offsite(definition, show, provider, any_show)
        for r in (r for r in rules.licences if r.matches(show or "")):
            if models is None:
                if rules.unlisted_models == "deny":
                    raise Refused(f"{definition} can't run {where}: its process.yaml doesn't list its models and "
                                  f"their licences, which this show's policy checks. (Policy: unlisted_models is "
                                  f"deny.) Its models: need listing.{ask}", "unlisted_models")
                return
            bad = r.offending(models)
            if bad:
                raise Refused(f"{definition} can't run {where}: it uses {bad.get('file')}, licensed "
                              f"{bad.get('licence')}. {r.reason.rstrip('.')}. (Policy rule {r.name}.){ask}", r.name)

    def offsite(self, definition: str, show: str | None, provider, any_show: bool = False) -> None:
        """Raise Refused if the policy doesn't let `show` use `provider` (decision 0058). The
        farm's own providers are allowed unless a rule denies them. An outside one sends the
        show's images and prompts outside Rodeo, so it is refused unless the policy is on, the
        show is known and a rule allows it; a deny wins. Asked by check(), and alone for the
        farm's own reference jobs (evals), which have no show and skip the other rules."""
        if provider is None:
            return
        name, label, outside = provider.name, getattr(provider, "label", provider.name), bool(provider.offsite)
        if not self.enabled:
            if outside:
                raise Refused(f"{definition} runs on {label}, outside Rodeo, which the farm's policy must allow, and "
                              f"the policy is off (policy.enabled in the manager's config.yaml).", "providers")
            return
        self.refresh()
        rules = self.rules
        if rules is None:
            if not outside:
                return                  # check() has said so; an eval of the farm's own is aside
            raise Refused(f"The farm's policy can't be read, so no job can be planned until it is fixed: "
                          f"{self.error or 'no valid policy.yaml yet'}.", None)
        ask = f" Ask {rules.contact} if this is wrong." if rules.contact else ""
        where = f"for show {show}" if show else "without a known show"
        matched = [r for r in rules.providers if r.matches(show or "")]
        for r in matched:
            if name in (r.deny or []):
                raise Refused(f"{definition} can't run {where} on {label}: {r.reason.rstrip('.')}. "
                              f"(Policy rule {r.name}.){ask}", r.name)
        if not outside:
            return
        if not show:
            if any_show and any(name in (r.allow or []) for r in rules.providers):
                return
            raise Refused(f"{definition} runs on {label}, outside Rodeo, so the farm's policy needs to know the show "
                          f"it is for: give a Show, or an input path inside a show.{ask}", "providers")
        if not any(name in (r.allow or []) for r in matched):
            raise Refused(f"Show {show} isn't cleared to send work to {label}, outside Rodeo: no policy rule allows "
                          f"it. (Policy: providers.){ask}", "providers")

    def quota(self, show: str | None, user: str | None, frames: int, queued: Callable[[], list[dict]]) -> None:
        """Raise Refused if one more submission of `frames` frames would take a quota over.
        `queued()`: what is queued or running now, one {show, user, frames} per submission;
        only asked for when a quota is for this show. Checked at plan, after check()."""
        if not self.enabled or self.rules is None:
            return
        rules = self.rules
        ask = f" Ask {rules.contact} if this is wrong." if rules.contact else ""
        now_queued = None
        for r in rules.quotas:
            if not r.matches(show or "") or (r.per == "user" and not user):
                continue
            if now_queued is None:
                now_queued = queued()
            s = (show or "").lower()
            mine = [q for q in now_queued if r.matches(q.get("show") or "")
                    and (r.per != "show" or (q.get("show") or "").lower() == s)
                    and (r.per != "user" or q.get("user") == user)]
            who = {"show": f"show {show}" if show else "jobs without a known show",
                   "user": f"{user} on {r.shows_text()}", "all": r.shows_text()}[r.per]
            jobs, queued_frames = len(mine), sum(int(q.get("frames") or 0) for q in mine)
            if r.max_jobs is not None and jobs + 1 > r.max_jobs:
                raise Refused(f"There are already {jobs} job(s) queued or running for {who}, and the most is "
                              f"{r.max_jobs}: {r.reason.rstrip('.')}. (Policy rule {r.name}.) Submit again when "
                              f"some have finished.{ask}", r.name)
            if r.max_frames is not None and queued_frames + frames > r.max_frames:
                raise Refused(f"This job's {frames} frame(s) would take {who} to {queued_frames + frames} queued or "
                              f"running, and the most is {r.max_frames}: {r.reason.rstrip('.')}. (Policy rule "
                              f"{r.name}.) Submit again when some have finished, or with fewer frames.{ask}",
                              r.name)

    def usage(self, queued: list[dict]) -> dict[str, list[dict]]:
        """What is queued or running now against each quota, as it counts it: one entry per
        show, per user or for the shows together, the fullest first (the dashboard's Policy
        tab). `queued`: as for quota()."""
        out: dict[str, list[dict]] = {}
        if not self.enabled or self.rules is None:
            return out
        for r in self.rules.quotas:
            groups: dict[str, dict] = {}
            for q in queued:
                if not r.matches(q.get("show") or ""):
                    continue
                key = {"show": (q.get("show") or "").lower() or "(no show)", "user": q.get("user") or "?",
                       "all": "together"}[r.per]
                g = groups.setdefault(key, {"for": key, "jobs": 0, "frames": 0})
                g["jobs"] += 1
                g["frames"] += int(q.get("frames") or 0)
            out[r.name] = sorted(groups.values(), key=lambda g: (-g["jobs"], -g["frames"], g["for"]))
        return out

    def status(self) -> dict:
        """What GET /api/v1/policy shows: the file, the rules in force and any error."""
        self.refresh()
        r = self.rules
        return {"enabled": self.enabled, "file": self.shown_path, "error": self.error,
                "in_force": None if r is None else {
                    "fingerprint": self.fingerprint, "taken_at": _when(self.taken_at), "source": self.source,
                    **r.model_dump(by_alias=True)}}


def policy_path(cfg) -> tuple[str | None, str | None]:
    """policy.file as this machine reads it, and as people see it: a path of its own, or
    a name in the bank folder (share.dir)."""
    f = cfg.policy.file
    if not f:
        return None, None
    if f.startswith(("/", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", f):     # a path of its own
        local = P.to_farm(f, cfg.paths.mappings)
    elif cfg.share.dir:
        local = P.to_farm(cfg.share.dir, cfg.paths.mappings).rstrip("/") + "/" + f
    else:
        local = P.normalize(str(cfg.resolve(f)))
    return local, P.to_posix(local, cfg.paths.mappings) or local
