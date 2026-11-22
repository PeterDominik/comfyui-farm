"""The farm's policy (decisions 0007, 0043): `policy.yaml` in the bank folder, edited by the
farm's admins, read by the manager and checked when a job is planned.

It holds show rules (which shows may use the farm, and with which definitions) and licence
rules (which licences the models a definition uses may carry, on which shows: each
definition lists its models in its process.yaml, decision 0044). A job a rule refuses is
never planned, and the artist is told which rule and why.

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

    @model_validator(mode="after")
    def _names(self):
        seen = set()
        for r in [*self.shows, *self.licences]:
            if r.name in seen:
                raise ValueError(f"two rules are named {r.name}: each needs its own name")
            seen.add(r.name)
        return self


def parse(text: str, known: set[str] | None = None) -> PolicyFile:
    """policy.yaml's text, validated. ValueError says what is wrong, in words an admin can
    act on. `known`: the definitions this farm has; a rule naming another one is an error
    (a typo in a deny list would otherwise deny nothing)."""
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
    return pf


def _when(t: float | None) -> str | None:
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None


class Policy:
    """The policy in force, read from `path` when it changes. Thread-safe: plans run in
    threads. With `enabled` off it allows everything and reads nothing."""

    def __init__(self, enabled: bool, path: str | None, saved: Path, known: Callable[[], set[str]],
                 shown_path: str | None = None):
        self.enabled = enabled
        self.path = path
        self.shown_path = shown_path or path
        self.saved = saved                          # the last valid file, kept across restarts
        self.known = known
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
                rules = parse(text, self.known())
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
            log.info("policy: %s taken (%s, %d show rule(s))", self.shown_path, fingerprint, len(rules.shows))
            try:
                self.saved.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.saved.with_suffix(".tmp")
                tmp.write_text(json.dumps({"text": text, "fingerprint": fingerprint, "taken_at": t}),
                               encoding="utf-8")
                os.replace(tmp, self.saved)
            except OSError as e:
                log.warning("policy: couldn't keep a copy in %s: %s", self.saved, e)

    # ---------------------------------------------------------------- checking
    def check(self, definition: str, show: str | None, models: list[dict] | None = None) -> None:
        """Raise Refused if the policy in force doesn't allow `definition` for `show`.
        `models`: the definition's models with their licences (process.yaml), None when it
        doesn't list them."""
        if not self.enabled:
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
