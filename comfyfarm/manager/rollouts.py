"""Rollouts: a release of a runner environment going to the machines one at a time, the
canary first, each gated by its install's check, with pause and rollback (decision 0031).

A rollout tells one machine at a time to have the release (desk.want_environment, decision
0030), and moves on only when that machine reports the release current and checked
(decision 0029). If the machine says the install failed, or doesn't finish within
rollouts.step_minutes, the rollout stops there: the machines after it never hear of the
release. A broken release stops at the canary.

    running      at machines[step]: told to install, waiting for it to report
    paused       stopped by hand before the next machine; resume goes on
    failed       stopped where a machine failed; roll back, or cancel
    done         every machine has it
    rolled_back  the machines it reached are told to go back to what they had
    cancelled    stopped by hand; the machines keep what they have

One rollout of an environment is open (running, paused or failed) at a time. It is kept in
the manager's database, so it outlives a restart. Nothing here builds or runs anything:
the agents' environment managers do (agent/envman.py).

With evals.gate (decision 0053), a rollout also runs the evals of the definitions run in the
environment (`evals`, fixed when it starts) on each machine before it goes on. Then each
machine is drained when its turn starts, so no artist's job runs with the release before it
passed; its jobs finish first. Once it has the release, its evals are queued, pinned to it;
when they pass it takes jobs again (unless it was drained before) and the rollout goes on.
A failed eval stops the rollout there, the machine left drained: rolling back gives it its
jobs again with the release it had."""

from __future__ import annotations

import asyncio
import logging
import uuid

from ..protocol import now
from .evals import EvalError

log = logging.getLogger("comfyfarm")

OPEN = ("running", "paused", "failed")
TICK_SECONDS = 5.0


class RolloutError(ValueError):
    """A rollout that can't be started or changed, said so a person can act on it."""


class Rollouts:
    def __init__(self, desk):
        self.desk = desk
        self.store = desk.store
        self.cfg = desk.farm.cfg.rollouts

    # ---------------------------------------------------------------- reading
    def open(self, name: str) -> dict | None:
        return next((r for r in self.store.rollouts(name) if r["state"] in OPEN), None)

    def recent(self, limit: int = 20) -> list[dict]:
        return self.store.rollouts(limit=limit)

    def order(self, machines: list[str]) -> list[str]:
        """rollouts.order first, in that order (the canary first); then the rest by name."""
        listed = [m for m in self.cfg.order if m in machines]
        return listed + sorted(m for m in machines if m not in listed)

    def _have(self, machine: str, name: str) -> str | None:
        """The release the machine has current, as it reported it."""
        a = self.desk.agents.get(machine)
        env = ((a.envs or {}).get(name) or {}) if a else {}
        cur = env.get("current")
        return cur if cur and ((env.get("builds") or {}).get(cur) or {}).get("ok") else None

    # ---------------------------------------------------------------- changing
    def start(self, name: str, release: str, machines: list[str] | None, by: str | None) -> dict:
        if self.open(name):
            raise RolloutError(f"a rollout of {name} is open ({self.open(name)['state']}): resume, roll it back or "
                               f"cancel it first")
        # Every GPU machine, unless named: the gateway has no runner environments (decision 0056).
        names = list(machines) if machines else sorted(m for m, a in self.desk.agents.items() if not a.lanes)
        unknown = [m for m in names if m not in self.desk.agents]
        if unknown:
            raise RolloutError(f"no machine {', '.join(unknown)} (known: {', '.join(sorted(self.desk.agents))})")
        gateway = next((m for m in names if self.desk.agents[m].lanes), None)
        if gateway:
            raise RolloutError(f"{gateway} is the gateway: it sends jobs to outside providers and builds no runner "
                               f"environments")
        if not names:
            raise RolloutError("no machine has checked in yet")
        order = self.order(names)
        gate = self.desk.evals.definitions(environment=name) if self.desk.cfg.evals.gate else []
        r = {"id": uuid.uuid4().hex[:12], "name": name, "release": release, "machines": order,
             "before": {m: self._have(m, name) for m in order}, "step": 0, "state": "running", "error": None,
             "by": by, "started_at": now(), "step_started": now(),
             "evals": gate, "eval_jobs": {}, "eval_started": None, "drained_before": {}}
        log.info("rollout %s: %s %s to %s, %s first (by %s)", r["id"], name, release, ", ".join(order), order[0],
                 by or "?")
        self._tell(r)
        self.store.save_rollout(r)
        return r

    def act(self, name: str, action: str, by: str | None) -> dict:
        r = self.open(name) if action != "rollback" else (self.open(name) or self._last(name))
        if r is None:
            raise RolloutError(f"no rollout of {name} to {action}")
        if action == "pause":
            if r["state"] != "running":
                raise RolloutError(f"the rollout of {name} is {r['state']}, not running")
            r["state"] = "paused"
        elif action == "resume":
            if r["state"] != "paused":
                raise RolloutError(f"the rollout of {name} is {r['state']}: only a paused one goes on (a failed one "
                                   f"is rolled back or cancelled)")
            r["state"], r["step_started"] = "running", now()
            self._tell(r)
        elif action == "cancel":
            self.desk.evals.cancel(self._eval_ids(r))
            if r["state"] != "failed":          # a machine that failed its eval stays drained for a person
                self._resume_all(r)
            r["state"] = "cancelled"
        elif action == "rollback":
            if r["state"] == "rolled_back":
                raise RolloutError(f"the last rollout of {name} is rolled back already")
            reached = r["machines"][:r["step"] + 1] if r["state"] != "done" else r["machines"]
            for m in reached:
                if m in self.desk.agents:
                    self.desk.want_environment(name, r["before"].get(m), [m], f"rollback {r['id']} by {by or '?'}")
            self.desk.evals.cancel(self._eval_ids(r))
            self._resume_all(r)                 # back to the release they had: they take jobs again
            r["state"] = "rolled_back"
        else:
            raise RolloutError(f"{action} isn't something a rollout does (pause, resume, rollback, cancel)")
        log.info("rollout %s of %s: %s (by %s)", r["id"], name, r["state"], by or "?")
        self.store.save_rollout(r)
        return r

    def _last(self, name: str) -> dict | None:
        rows = self.store.rollouts(name, limit=1)
        return rows[0] if rows else None

    def _tell(self, r: dict) -> None:
        m = r["machines"][r["step"]]
        self.desk.want_environment(r["name"], r["release"], [m], f"rollout {r['id']}")
        if r.get("evals"):                      # no artist's job with the release before its evals pass
            r.setdefault("drained_before", {}).setdefault(m, m in self.desk.drained)
            if m not in self.desk.drained:
                self.desk.set_drained(m, True, f"rollout {r['id']}")

    # ---------------------------------------------------------------- evals (decision 0053)
    def _eval_ids(self, r: dict) -> list[str]:
        return [i for ids in (r.get("eval_jobs") or {}).values() for i in ids]

    def _resume(self, r: dict, m: str) -> None:
        """Takes jobs again, if this rollout drained it and nobody has since."""
        if not (r.get("drained_before") or {}).get(m, True) \
                and (self.desk.drained.get(m) or {}).get("by") == f"rollout {r['id']}":
            self.desk.set_drained(m, False, f"rollout {r['id']}")

    def _resume_all(self, r: dict) -> None:
        for m in r.get("drained_before") or {}:
            self._resume(r, m)

    def _evaluate(self, r: dict, m: str) -> str:
        """The machine's evals: queued once it has the release, then judged. running, passed or
        failed (the rollout stopped)."""
        ids = (r.get("eval_jobs") or {}).get(m)
        if ids is None:
            try:
                ids = self.desk.evals.begin(m, r["evals"], f"rollout {r['id']}", r.get("by"))
            except EvalError as e:
                self._fail(r, f"{m}: {e}")
                return "failed"
            r.setdefault("eval_jobs", {})[m], r["eval_started"] = ids, now()
            log.info("rollout %s: %s has %s %s: its evals are queued (%s)", r["id"], m, r["name"], r["release"],
                     ", ".join(r["evals"]))
            self.store.save_rollout(r)
            return "running"
        state, why = self.desk.evals.judge(ids)
        minutes = self.desk.cfg.evals.minutes
        if state == "running" and now() - (r.get("eval_started") or now()) > minutes * 60:
            self.desk.evals.cancel(ids)
            state, why = "failed", f"its evals didn't finish within {minutes:g} minutes (evals.minutes)"
        if state == "failed":
            self._fail(r, f"{m}: {why}. {m} stays drained: look at it, then roll back (it takes jobs again with "
                          f"what it had) or cancel")
        return state

    # ---------------------------------------------------------------- going on
    def tick(self) -> None:
        for r in [r for r in self.store.rollouts() if r["state"] == "running"]:
            self._step(r)

    def _step(self, r: dict) -> None:
        m = r["machines"][r["step"]]
        a = self.desk.agents.get(m)
        if self._have(m, r["name"]) == r["release"]:
            if r.get("evals"):
                if self._evaluate(r, m) != "passed":
                    return
                self._resume(r, m)
            log.info("rollout %s: %s has %s %s", r["id"], m, r["name"], r["release"])
            r["step"] += 1
            if r["step"] >= len(r["machines"]):
                r["state"], r["step"] = "done", len(r["machines"]) - 1
                log.info("rollout %s: %s %s is on every machine", r["id"], r["name"], r["release"])
            else:
                r["step_started"] = now()
                self._tell(r)
            self.store.save_rollout(r)
            return
        work = ((a.env_work or {}).get(r["name"]) if a else None) or {}
        if work.get("want") == r["release"] and work.get("state") in ("failed", "off"):
            self._fail(r, f"{m}: {work.get('error') or work.get('state')}")
        elif now() - (r["step_started"] or now()) > self.cfg.step_minutes * 60:
            seen = "isn't checking in" if not self.desk.alive(m) else f"says {work.get('state') or 'nothing'}"
            self._fail(r, f"{m} didn't have it within {self.cfg.step_minutes:g} minutes ({seen})")

    def _fail(self, r: dict, why: str) -> None:
        r["state"], r["error"] = "failed", why
        log.warning("rollout %s of %s %s stopped at %s: %s", r["id"], r["name"], r["release"],
                    r["machines"][r["step"]], why)
        self.store.save_rollout(r)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("rollouts: going on failed")
