"""Syncs: a runner's environment brought to the farm's file on the machines, one at a time,
the canary first, started from the manager's host (decision 0033). The pilot's one is
ComfyUI's: what `comfyfarm comfyui sync --apply` does by hand on a drained machine, done
by each machine's agent when the manager says. The manager names the version of the
environment file it wants (its host's checkout), and a machine whose checkout has another
refuses. The manager itself runs nothing: it drains, tells, waits and resumes.

Each machine in turn:

    draining   drained (unless it was already), until it holds no jobs; no time limit,
               since its jobs may be long
    told       its agent told to sync
    syncing    its agent stops ComfyUI, updates it, starts it again (it says how it goes)
    checking   its agent says it's done: until the manager has checked its ComfyUI again.
               A workflow it could run before and can't now stops the sync there
    evaluating with evals.gate (decision 0053): the evals of the runner's definitions it
               could run before, pinned to it while it is still drained. One that fails, or
               not all over within evals.minutes, stops the sync there
then it is resumed, unless it was drained before, and the next machine's turn comes. Told
and not done within rollouts.sync_minutes, it stops the sync there too.

    running    at machines[step]
    failed     stopped at a machine; it stays drained, for a person to look at (unless its
               agent refused before touching anything: then it is resumed)
    done       every machine was synced
    cancelled  stopped by hand: no next machine (one already told finishes, and stays drained)

One sync runs at a time, so it takes one machine out of service at a time. It is kept in
the manager's database, so it outlives a restart."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from ..protocol import now
from .evals import EvalError

log = logging.getLogger("comfyfarm")

TICK_SECONDS = 5.0
ASK_AGAIN_SECONDS = 30.0        # told, and its agent says nothing of it: told again (a lost reply)


class SyncError(ValueError):
    """A sync that can't be started or changed, said so a person can act on it."""


class Syncs:
    def __init__(self, desk):
        self.desk = desk
        self.store = desk.store
        self.cfg = desk.farm.cfg.rollouts

    # ---------------------------------------------------------------- reading
    def running(self) -> dict | None:
        return next((r for r in self.store.syncs(limit=20) if r["state"] == "running"), None)

    def recent(self, limit: int = 20) -> list[dict]:
        return self.store.syncs(limit=limit)

    # ---------------------------------------------------------------- changing
    def start(self, runner: str, file: str, machines: list[str] | None, force: bool, by: str | None) -> dict:
        """`file`: the version of the environment file the machines must have (its fingerprint,
        from the manager's host's checkout). Machines default to every one the manager knows."""
        if not file:
            raise SyncError("say which version of the farm's environment file to sync to (`comfyfarm comfyui sync` "
                            "on the manager's host names its own)")
        open_ = self.running()
        if open_:
            raise SyncError(f"sync {open_['id']} is running (at {open_['machines'][open_['step']]}): one machine out "
                            f"of service at a time. Wait for it, or cancel it")
        names = list(dict.fromkeys(machines)) if machines else sorted(self.desk.agents)
        unknown = [m for m in names if m not in self.desk.agents]
        if unknown:
            raise SyncError(f"no machine {', '.join(unknown)} (known: {', '.join(sorted(self.desk.agents)) or 'none'})")
        if not names:
            raise SyncError("no machine has checked in yet")
        r = {"id": uuid.uuid4().hex[:12], "runner": runner, "file": file, "force": bool(force),
             "machines": self.desk.rollouts.order(names), "step": 0, "phase": "draining", "state": "running",
             "error": None, "by": by, "started_at": now(), "step_started": None, "told_at": None, "asked_at": None,
             "drained_before": {}, "could_not": {}, "results": {},
             "evals": self.desk.evals.definitions(runner=runner) if self.desk.cfg.evals.gate else [],
             "eval_jobs": {}, "eval_started": None}
        log.info("sync %s: %s to the farm's environment %s on %s, one at a time (by %s)", r["id"], runner, file,
                 ", ".join(r["machines"]), by or "?")
        self._turn(r)
        self.store.save_sync(r)
        return r

    def cancel(self, by: str | None) -> dict:
        r = self.running()
        if r is None:
            raise SyncError("no sync is running")
        m = r["machines"][r["step"]]
        if r["phase"] == "draining":
            if not r["drained_before"].get(m):
                self.desk.set_drained(m, False, f"sync {r['id']} cancelled by {by or '?'}")
            r["error"] = f"cancelled by {by or '?'} before {m} was told"
        else:
            self.desk.evals.cancel((r.get("eval_jobs") or {}).get(m) or [])
            r["error"] = (f"cancelled by {by or '?'}: {m} was told already. It finishes its sync and stays drained: "
                          f"resume it on the Machines tab once its ComfyUI is back")
        r["state"] = "cancelled"
        log.info("sync %s: %s", r["id"], r["error"])
        self.store.save_sync(r)
        return r

    def _turn(self, r: dict) -> None:
        """The machine at r["step"] is next: drain it."""
        m = r["machines"][r["step"]]
        r["drained_before"][m] = m in self.desk.drained
        if m not in self.desk.drained:
            self.desk.set_drained(m, True, f"sync {r['id']}")
        r.update(phase="draining", step_started=now(), told_at=None, asked_at=None)

    def _tell(self, r: dict, a) -> None:
        a.want_sync = {"id": r["id"], "runner": r["runner"], "file": r["file"], "force": r["force"]}
        r["asked_at"] = now()
        self.desk.wake()

    # ---------------------------------------------------------------- going on
    def tick(self) -> None:
        r = self.running()
        if r is not None:
            before = json.dumps(r, sort_keys=True)
            self._step(r)
            if json.dumps(r, sort_keys=True) != before:
                self.store.save_sync(r)

    def _step(self, r: dict) -> None:
        m = r["machines"][r["step"]]
        a = self.desk.agents.get(m)
        limit = self.cfg.sync_minutes * 60
        if r["phase"] == "draining":
            if m not in self.desk.drained:
                return self._fail(r, f"{m} was resumed by hand while the sync waited for its jobs to end")
            if a is None or not self.desk.alive(m):
                if now() - r["step_started"] > limit:
                    self._fail(r, f"{m} isn't checking in (waited {self.cfg.sync_minutes:g} minutes)")
                return
            if self.store.held_by(m):
                return                  # its jobs end first, however long they take
            r["could_not"][m] = sorted(a.problems)
            self._tell(r, a)
            r.update(phase="told", told_at=now())
            log.info("sync %s: %s holds no jobs: told to sync its %s", r["id"], m, r["runner"])
            return
        said = (a.synced if a and isinstance(a.synced, dict) and a.synced.get("id") == r["id"] else None)
        if r["phase"] in ("told", "syncing"):
            if said:
                r["results"][m] = said
                if said.get("state") == "failed":     # refused before touching ComfyUI: nothing to look at there
                    return self._fail(r, f"{m}: {said.get('error') or 'its sync failed'}", resume=not said.get("steps"))
                r["phase"] = "checking" if said.get("state") == "done" else "syncing"
            elif a is not None and self.desk.alive(m) and now() - (r["asked_at"] or 0) > ASK_AGAIN_SECONDS:
                self._tell(r, a)        # its agent never heard, or restarted and forgot
        if r["phase"] == "checking" and a is not None and self.desk.usable(a):
            new = sorted(set(a.problems) - set(r["could_not"].get(m) or []))
            if new:
                return self._fail(r, f"{m}: after the sync it can't run {', '.join(new)}: "
                                     f"{'; '.join(a.problems[new[0]])}")
            if not r.get("evals"):
                self._next(r, m)
                return
            r["phase"] = "evaluating"
        if r["phase"] == "evaluating":
            return self._evaluate(r, m)
        if now() - (r["told_at"] or now()) > limit:
            where = {"told": "its agent says nothing of it", "syncing": f"it says {(said or {}).get('state')}",
                     "checking": "its ComfyUI isn't back and checked"}[r["phase"]]
            self._fail(r, f"{m} didn't finish within {self.cfg.sync_minutes:g} minutes ({where})")

    def _evaluate(self, r: dict, m: str) -> None:
        """The evals of the runner's definitions the machine could run before the sync, pinned
        to it while it is still drained (decision 0053): passed, the next machine; failed, the
        sync stops there."""
        ids = (r.get("eval_jobs") or {}).get(m)
        if ids is None:
            defs = [d for d in r["evals"] if d not in (r["could_not"].get(m) or [])]
            if not defs:
                return self._next(r, m)
            try:
                ids = self.desk.evals.begin(m, defs, f"sync {r['id']}", r.get("by"))
            except EvalError as e:
                return self._fail(r, f"{m}: {e}")
            r.setdefault("eval_jobs", {})[m], r["eval_started"] = ids, now()
            log.info("sync %s: %s is synced and checked: its evals are queued (%s)", r["id"], m, ", ".join(defs))
            return
        state, why = self.desk.evals.judge(ids)
        minutes = self.desk.cfg.evals.minutes
        if state == "running" and now() - (r.get("eval_started") or now()) > minutes * 60:
            self.desk.evals.cancel(ids)
            state, why = "failed", f"its evals didn't finish within {minutes:g} minutes (evals.minutes)"
        if state == "failed":
            return self._fail(r, f"{m}: {why}")
        if state == "passed":
            self._next(r, m)

    def _next(self, r: dict, m: str) -> None:
        if not r["drained_before"].get(m):
            self.desk.set_drained(m, False, f"sync {r['id']}")
        log.info("sync %s: %s is synced and checked%s", r["id"], m,
                 "" if r["drained_before"].get(m) else ": it takes jobs again")
        if r["step"] + 1 >= len(r["machines"]):
            r.update(state="done", phase=None)
            log.info("sync %s: every machine is synced", r["id"])
            return
        r["step"] += 1
        self._turn(r)

    def _fail(self, r: dict, why: str, resume: bool = False) -> None:
        """Stopped at this machine. It stays drained for a person to look at, unless its agent
        refused before touching anything (`resume`) and it wasn't drained before."""
        m = r["machines"][r["step"]]
        if resume and not r["drained_before"].get(m):
            self.desk.set_drained(m, False, f"sync {r['id']}")
            why += f" (its ComfyUI wasn't touched: {m} takes jobs again)"
        r["state"], r["error"] = "failed", why
        log.warning("sync %s stopped at %s: %s", r["id"], m, why)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("syncs: going on failed")
