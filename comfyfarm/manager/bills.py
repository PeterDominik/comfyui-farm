"""The providers' bills, read into the spend ledger (decision 0061).

The billing keys stay on the gateway (decision 0056), so the manager can't read a bill
itself: it asks. When a read is wanted (each night at ledger.at with ledger.nightly, or
asked from the manager's host: `comfyfarm spend reconcile`), the next check-in of each
gateway is answered with `bill: {since, until}`. The gateway reads each of its providers'
bills for that time with its billing key, every page, and the balance, and sends each one
back (`bill`). The store applies it to the ledger in one transaction (JobStore.apply_bill):
each row the bill charges is billed at what it charged, an unsure row takes a charge it can
be placed with, and a row the bill should show by now and doesn't is billed $0 (charged) or
released (unsure). A bill that couldn't be read changes nothing, and says why.

The time read reaches back to the oldest row not settled by a bill yet (an hour before it was
queued), and at least a day before the last read: a missed night is read the next. A
provider's bill reaches back 90 days at most.

Top-ups are money an admin says was put on a provider account (`comfyfarm spend topup`): the
Spend tab shows them beside what the farm's jobs spent, and the provider's own balance."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from .. import catalog as C
from ..protocol import now
from .prices import money

log = logging.getLogger("comfyfarm")

__all__ = ["Bills", "BillError", "csv_text", "days"]

DAY = 86400.0
REACH = 89 * DAY                # a provider's bill is read 90 days back at most (fal.ai's)


class BillError(Exception):
    """A request about bills the manager refuses: what is wrong, said for the admin."""


class Bills:
    def __init__(self, desk):
        self.desk = desk
        self.store = desk.store
        self.cfg = desk.cfg.ledger
        self.wanted_at: float | None = None         # when a read was last wanted
        self.wanted_by: str | None = None
        # Each read wanted is numbered: two can be wanted in the same tick of a clock (Windows').
        self.wants = 0
        self.asked: dict[str, int] = {}             # gateway -> the want it was given

    # ---------------------------------------------------------------- when
    def next_run(self, t: float) -> float:
        """The next ledger.at after t; now if today's was missed (the manager was down)."""
        hh, mm = (int(x) for x in self.cfg.at.split(":"))
        today = datetime.fromtimestamp(t).replace(hour=hh, minute=mm, second=0, microsecond=0)
        if today.timestamp() <= t:
            last = self.store.bill_reads(None, 1)
            if max(self.wanted_at or 0, last[0]["at"] if last else 0) < today.timestamp():
                return t
            today += timedelta(days=1)
        return today.timestamp()

    async def loop(self) -> None:
        while True:
            await asyncio.sleep(max(0.0, self.next_run(now()) - now()))
            self.want("the nightly read (ledger.at)")
            await asyncio.sleep(60)                 # past this minute, so it is asked once

    def want(self, by: str) -> list[str]:
        """Ask every gateway checking in for its providers' bills, at its next check-in. The
        gateways that will be asked."""
        self.wanted_at, self.wanted_by = now(), by
        self.wants += 1
        gateways = sorted(a.name for a in self.desk.agents.values() if a.lanes and self.desk.alive(a.name))
        log.info("ledger: the providers' bills are wanted (%s): %s", by,
                 ", ".join(gateways) or "no gateway checking in")
        self.desk.wake()
        return gateways

    def window(self, providers) -> tuple[float, float]:
        """The time to read: back to the oldest row not settled by a bill, and a day before the
        last read that went through, whichever is earlier; 90 days at most."""
        t = now()
        since = t - DAY
        for p in providers:
            oldest = self.store.oldest_unbilled(p)
            if oldest is not None:
                since = min(since, oldest - 3600)
            last = next((r for r in self.store.bill_reads(p, 20) if not r["error"]), None)
            if last is not None:
                since = min(since, last["until"] - DAY)
        return max(since, t - REACH), t

    def instructions(self, a) -> dict | None:
        """For a gateway's check-in reply: `bill`, once for each read wanted."""
        if not self.wants or not a.lanes or self.asked.get(a.name) == self.wants:
            return None
        self.asked[a.name] = self.wants
        since, until = self.window(a.lanes)
        return {"since": since, "until": until}

    # ---------------------------------------------------------------- what came back
    def received(self, gateway: str, msg: dict) -> dict:
        """One provider's bill from a gateway (protocol.py: bill), applied to the ledger."""
        provider, account = str(msg.get("provider") or ""), (str(msg["account"]) if msg.get("account") else None)
        p = C.current().providers.get(provider)
        if p is None or not p.offsite:
            return {"ok": False, "error": f"no outside provider {provider!r}"}
        try:
            since, until = float(msg["since"]), float(msg["until"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "a bill says the time it covers: since and until"}
        stand_in = bool(msg.get("stand_in"))
        if msg.get("error"):
            rec = self.store.record_bill_error(provider, account, gateway, since, until, str(msg["error"])[:1000],
                                               stand_in)
            log.warning("ledger: %s couldn't read %s's bill: %s", gateway, p.label, rec["error"])
            return {"ok": True, "read": rec}
        events = [e for e in (_event(x) for x in msg.get("events") or []) if e is not None]
        balance = msg.get("balance") if isinstance(msg.get("balance"), dict) else None
        rec = self.store.apply_bill(provider, account, gateway, since, until, events, balance, stand_in,
                                    self.cfg.grace_hours * 3600)
        log.info("ledger: %s's bill%s from %s: %d charge(s); %d job(s) billed, %d billed nothing, %d released%s",
                 p.label, " (the stand-in's)" if stand_in else "", gateway, rec["events"], rec["matched"],
                 rec["zeroed"], rec["released"],
                 f"; balance {money(rec['balance'])}" if rec.get("balance") is not None else "")
        return {"ok": True, "read": rec}

    # ---------------------------------------------------------------- top-ups
    def topup(self, provider: str, amount: float, account: str | None, note: str | None, by: str | None) -> dict:
        p = C.current().providers.get(provider)
        if p is None or not p.offsite:
            outside = sorted(n for n, x in C.current().providers.items() if x.offsite)
            raise BillError(f"{provider} isn't an outside provider (providers/: {', '.join(outside) or 'none'})")
        if not amount:
            raise BillError("a top-up is an amount in US dollars, e.g. 50 (a negative one corrects an earlier one)")
        if not account:
            known = sorted({a["account"] for a in self.store.accounts() if a["provider"] == provider})
            if len(known) != 1:
                raise BillError(f"name the {p.label} account the money went on (--account, as gateway.yaml's "
                                f"gateway.account" + (f"; the ledger knows {', '.join(known)})" if known else ")"))
            account = known[0]
        rec = self.store.add_topup(provider, account, float(amount), (note or "").strip()[:500] or None, by)
        log.info("ledger: %s recorded a top-up of %s on %s (%s)", by or "?", money(rec["amount"]), p.label, account)
        return rec

    # ---------------------------------------------------------------- the Spend tab
    def accounts(self) -> list[dict]:
        """Each provider account: what was topped up, what the farm's jobs spent, the provider's
        balance and its last bill read."""
        out = []
        for a in self.store.accounts():
            p = C.current().providers.get(a["provider"])
            reads = [r for r in self.store.bill_reads(a["provider"], 50) if r["account"] in (a["account"], None)]
            ok = next((r for r in reads if not r["error"]), None)
            out.append({**a, "label": p.label if p else a["provider"], "last_read": reads[0] if reads else None,
                        "balance": ok["balance"] if ok else None, "currency": ok["currency"] if ok else None,
                        "balance_at": ok["at"] if ok else None, "stand_in": bool(ok and ok["stand_in"]),
                        "bill": self.store.bill_totals(a["provider"], a["account"])})
        return out

    def view(self, show: str | None = None, since: float | None = None, until: float | None = None,
             limit: int = 500) -> dict:
        """GET /api/v1/spend."""
        return {"enabled": bool(self.desk.cfg.providers.enabled),
                "reading": {"nightly": self.cfg.nightly, "at": self.cfg.at, "grace_hours": self.cfg.grace_hours,
                            "wanted_at": self.wanted_at, "wanted_by": self.wanted_by},
                "shows": self.desk.farm.spend_now(), "accounts": self.accounts(),
                "rows": self.store.spend_rows(show, since, until, limit), "topups": self.store.topups(50),
                "reads": self.store.bill_reads(None, 10)}


def days(since: str | None, until: str | None) -> tuple[float | None, float | None]:
    """Two dates (YYYY-MM-DD, the manager's local time) as the times from the first day's
    start to the last day's end. ValueError says which is wrong."""
    out = []
    for name, v, extra in (("since", since, 0), ("until", until, 1)):
        if not v:
            out.append(None)
            continue
        try:
            d = datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"{name} is a date like 2026-11-24, not {v!r}") from None
        out.append((d + timedelta(days=extra)).timestamp())
    return out[0], out[1]


CSV_COLUMNS = ("queued", "show", "user", "job", "job_name", "workflow", "provider", "account", "endpoint", "request_id",
               "quantity", "unit", "unit_price", "estimate", "billed", "counts", "state", "ended", "billed_at")


def csv_text(rows: list[dict]) -> str:
    """The ledger's rows as a spreadsheet reads them, in US dollars, times in the manager's
    local time."""
    import csv
    import io

    def when(t):
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else ""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        w.writerow([when(r["created_at"]), r["show"], r["user"], r["job_id"], r.get("job_name") or "", r["workflow"],
                    r["provider"], r["account"] or "", r["endpoint"] or "", r["request_id"] or "", r["quantity"],
                    r["unit"] or "", r["unit_price"], r["estimate"], "" if r["actual"] is None else r["actual"],
                    r["amount"], r["state"], when(r["settled_at"]), when(r["billed_at"])])
    return out.getvalue()


def _event(x) -> dict | None:
    """A charge as a gateway sent it, checked: never trusted beyond its shape."""
    if not isinstance(x, dict) or not isinstance(x.get("request_id"), str) or not x["request_id"]:
        return None
    try:
        at, cost = float(x["at"]), float(x["cost"])
    except (KeyError, TypeError, ValueError):
        return None
    if cost < 0:
        return None

    def num(v):
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return {"request_id": x["request_id"][:100], "endpoint": str(x.get("endpoint") or "")[:200] or None, "at": at,
            "quantity": num(x.get("quantity")), "unit": str(x["unit"])[:50] if x.get("unit") else None,
            "unit_price": num(x.get("unit_price")), "cost": round(cost, 9)}
