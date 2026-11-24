"""Alerts (decision 0052): what needs a person, checked every alerts.check_seconds.

The rules, each off at 0 (or false) in config.yaml's alerts:

- machine_silent_minutes: a machine that hasn't checked in that long. A drained machine is
  left out: draining is how a machine is taken out to be worked on. Machines that haven't
  checked in since the manager started are known from their GPU history (0049).
- job_waiting_minutes: jobs queued that long without a machine. One alert for them all.
- failures_in_a_row: a machine whose last that many jobs, done or failed, all failed.
- sweep_problems: the retention sweeper's latest report lists problems (0041).
- rollout_problems: a rollout stopped at a machine (0031), or a sync stopped at a machine it
  left drained (0033): each waits for a person.
- budgets: a show that has spent or holds its budget's warn_at of its cap at outside
  providers, or all of it (policy.yaml's budgets, decision 0060).

An alert is raised once, when its condition starts, and cleared when it ends; while it holds,
its message follows what it is about (how many jobs wait). Both are logged, kept in the
manager's database for the dashboard's Alerts tab, and, with alerts.google_chat, posted to
that Google Chat space: one message for each check that raised or cleared any.

The webhook is a secret (whoever has it may post to the space): it is never logged, nor
given by an endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime

from ..protocol import now
from . import share
from .prices import money

log = logging.getLogger("comfyfarm")

__all__ = ["Alerts", "post_google_chat", "KINDS"]

# Each rule's name on the dashboard.
KINDS = {"machine_silent": "Machine silent", "job_waiting": "Jobs waiting", "failures": "Failures in a row",
         "sweep": "Sweep problems", "rollout": "Rollout stopped", "sync": "Sync stopped", "budget": "Budget"}


def _when(t: float) -> str:
    """A time as the farm's people read it: Sun 22 Nov 23:10, the manager's local time."""
    return datetime.fromtimestamp(t).strftime("%a %d %b %H:%M").replace(" 0", " ")


def _minutes(m: float) -> str:
    return f"{m:g} min" if m < 120 else f"{m / 60:g} h"


def post_google_chat(url: str, text: str, timeout: float = 10.0) -> str | None:
    """Post a message to a Google Chat space through its webhook. None when it went, else why
    not (never with the webhook's address in it: it holds the key)."""
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=UTF-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200] if e.fp else ""
        return f"Google Chat answered {e.code} {e.reason}" + (f": {body}" if body else "")
    except (urllib.error.URLError, OSError) as e:
        return f"can't reach Google Chat: {getattr(e, 'reason', None) or e}"


class Alerts:
    def __init__(self, desk):
        self.desk = desk
        self.farm = desk.farm
        self.store = desk.store
        self.cfg = desk.cfg.alerts
        self.checked_at: float | None = None
        self.posted_at: float | None = None
        self.post_error: str | None = None
        self._pruned = 0.0

    # ---------------------------------------------------------------- the rules
    def conditions(self, t: float) -> dict[str, tuple[str, str | None, str]]:
        """What holds now: {key: (kind, subject, message)}."""
        c = self.cfg
        out: dict[str, tuple[str, str | None, str]] = {}
        if c.machine_silent_minutes > 0:
            seen = {a.name: a.last_seen for a in self.desk.agents.values() if a.last_seen}
            for m, at in self.store.machines_sampled().items():
                seen.setdefault(m, at)                 # not checked in since the manager started
            for m, at in sorted(seen.items()):
                if m not in self.desk.drained and t - at >= c.machine_silent_minutes * 60:
                    out[f"machine_silent:{m}"] = ("machine_silent", m, (
                        f"{m} hasn't checked in since {_when(at)}. Check that the machine is on and its agent "
                        f"running; drain it on the Machines tab while it is being worked on."))
        if c.job_waiting_minutes > 0:
            old = [j for j in self.store.queued() if t - j["created_at"] >= c.job_waiting_minutes * 60]
            if old:
                j = min(old, key=lambda j: j["created_at"])
                wf = self.farm.registry.workflows.get(j["workflow"])
                who = ", ".join(x for x in (wf.label if wf else j["workflow"], j["user"],
                                            (j.get("info") or {}).get("show")) if x)
                over = f"for a machine for over {_minutes(c.job_waiting_minutes)}"
                job = f"{j.get('name') or j['short_id']} ({who}), queued since {_when(j['created_at'])}."
                if self.farm.policy.enabled and self.farm.policy.rules is None:
                    why = "No valid policy is in force, so no queued job is given out: see the Policy tab."
                else:
                    why = self.desk.requires_note(j["workflow"]) or (
                        "The machines that can run it are busy, drained or offline: see the Machines tab.")
                out["job_waiting"] = ("job_waiting", None, (
                    f"{len(old)} jobs have waited {over}. The oldest: {job} {why}" if len(old) > 1
                    else f"A job has waited {over}: {job} {why}"))
        if c.failures_in_a_row > 0:
            for m, runs in sorted(self.store.last_outcomes(c.failures_in_a_row).items()):
                if len(runs) < c.failures_in_a_row or any(r["outcome"] != "failed" for r in runs):
                    continue
                labels = []
                for r in runs:
                    wf = self.farm.registry.workflows.get(r["workflow"])
                    if (wf.label if wf else r["workflow"]) not in labels:
                        labels.append(wf.label if wf else r["workflow"])
                job = self.store.get(runs[0]["job_id"]) or {}
                error = (job.get("error") or "").strip().splitlines()
                said = f": {error[0][:200]}" if error else ""
                out[f"failures:{m}"] = ("failures", m, (
                    f"The last {len(runs)} jobs on {m} failed ({', '.join(labels)}). The latest, "
                    f"{runs[0]['job_id'][:8]}{said}. Look at them on the Queue tab; if the machine is the "
                    f"problem, drain it on the Machines tab."))
        if c.sweep_problems:
            latest = self.farm.sweeper.reports()[:1]
            if latest and latest[0].get("problems"):
                r = latest[0]
                out["sweep"] = ("sweep", None, (
                    f"The retention sweep of {r['date']} lists {r['problems']} problem(s). Read its report "
                    f"(/api/v1/sweeps/{r['name']}?format=text, or data/sweeps/{r['name']}.json on the manager) "
                    f"before deletion is turned on."))
        if c.rollout_problems:
            for r in self.store.rollouts(limit=50):
                if r["state"] == "failed":
                    m = r["machines"][r["step"]]
                    out[f"rollout:{r['id']}"] = ("rollout", r["name"], (
                        f"The rollout of {r['name']} {str(r['release'])[:8]} stopped at {m}: {r.get('error')}. "
                        f"On the manager's host: comfyfarm env rollout {r['name']} --rollback (or --cancel)."))
            for s in self.store.syncs(limit=20):
                m = s["machines"][s["step"]] if s.get("machines") else None
                if s["state"] == "failed" and m and (self.desk.drained.get(m) or {}).get("by") == f"sync {s['id']}":
                    out[f"sync:{s['id']}"] = ("sync", m, (
                        f"The {s.get('runner') or 'runner'} sync stopped at {m}: {s.get('error')}. {m} stays "
                        f"drained: look at it, then resume it on the Machines tab."))
        if c.budgets:
            for s in self.farm.spend_now():
                cap, warn = s["cap"], s["warn_at"]
                if not cap or not warn or s["amount"] < warn * cap - 1e-9:
                    continue
                rule = f"policy rule {s['rule']}" if s["rule"] else "policy: budgets.other_shows"
                spent = f"{money(s['amount'])} of its {money(cap)} cap at outside providers ({rule})"
                out[f"budget:{s['show']}"] = ("budget", s["show"], (
                    f"Show {s['show']} has spent or holds {spent}: its jobs there are refused until the cap is raised "
                    f"in policy.yaml (budgets)." if s["amount"] >= cap - 1e-9 else
                    f"Show {s['show']} has spent or holds {spent}, {s['amount'] / cap:.0%}: raise the cap in "
                    f"policy.yaml (budgets) before it runs out, if it should go on."))
        return out

    # ---------------------------------------------------------------- raising, clearing
    def check(self, t: float | None = None) -> tuple[list[dict], list[dict]]:
        """Raise what started, clear what ended, follow what holds. Returns (raised, cleared)."""
        t = now() if t is None else t
        holds = self.conditions(t)
        open_ = {a["key"]: a for a in self.store.open_alerts()}
        raised, cleared = [], []
        for key, (kind, subject, message) in holds.items():
            a = open_.get(key)
            if a is None:
                raised.append(self.store.raise_alert(key, kind, subject, message, t))
                log.warning("alert: %s", message)
            elif a["message"] != message:
                self.store.alert_message(a["id"], message)
        for key, a in open_.items():
            if key not in holds:
                self.store.clear_alert(a["id"], t)
                cleared.append({**a, "cleared_at": t})
                log.info("alert cleared after %s: %s", _minutes(round((t - a["raised_at"]) / 60)), a["message"])
        self.checked_at = t
        if t - self._pruned >= 3600:
            self.store.prune_alerts(t - self.cfg.keep_days * 86400)
            self._pruned = t
        return raised, cleared

    def text(self, raised: list[dict], cleared: list[dict]) -> str:
        """One Google Chat message for a check's changes, with the way to the Alerts tab."""
        url = share.manager_url(self.desk.cfg)
        lines = [f"*ComfyFarm* ({url.split('://')[-1]})"]
        lines += [f"⚠️ {a['message']}" for a in raised]
        lines += [f"✅ Cleared after {_minutes(round((a['cleared_at'] - a['raised_at']) / 60))}: {a['message']}"
                  for a in cleared]
        lines.append(f"<{url}/#alerts|Alerts tab>")
        return "\n".join(lines)

    async def post(self, raised: list[dict], cleared: list[dict]) -> None:
        if not self.cfg.google_chat or not (raised or cleared):
            return
        self.post_error = await asyncio.to_thread(post_google_chat, self.cfg.google_chat, self.text(raised, cleared))
        self.posted_at = now()
        if self.post_error:
            log.warning("alerts: not posted to Google Chat: %s", self.post_error)

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.check_seconds)
            try:
                await self.post(*self.check())
            except Exception:  # noqa: BLE001
                log.exception("alerts: the check failed")

    # ---------------------------------------------------------------- the dashboard
    def view(self) -> dict:
        """GET /api/v1/alerts: the rules, whether Google Chat is set and how its last post went,
        the alerts that hold and the latest cleared."""
        c = self.cfg
        out = {"enabled": c.enabled, "check_seconds": c.check_seconds, "checked_at": self.checked_at,
               "rules": {"machine_silent_minutes": c.machine_silent_minutes,
                         "job_waiting_minutes": c.job_waiting_minutes, "failures_in_a_row": c.failures_in_a_row,
                         "sweep_problems": c.sweep_problems, "rollout_problems": c.rollout_problems,
                         "budgets": c.budgets},
               "google_chat": bool(c.google_chat), "posted_at": self.posted_at, "post_error": self.post_error,
               "kinds": KINDS, "active": [], "cleared": []}
        if c.enabled:
            out["active"] = self.store.open_alerts()
            out["cleared"] = self.store.cleared_alerts()
        return out
