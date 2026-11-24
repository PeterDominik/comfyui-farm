"""The spend ledger and the caps (decision 0060): what jobs at outside providers cost, by show.

A job that costs money (its plan has an estimate, decision 0059) is checked against its
show's cap (policy.yaml's `budgets:`) when it is planned, and holds its estimate against that
cap from the moment it is queued. The check and the hold are one transaction
(JobStore.insert), made under the farm's submit lock, so two submits can't both take the last
of a cap. A cap is the show's for its life: what its jobs spent stays spent.

When the job ends (JobStore._changed, which every change to a job passes through), its row
says what became of the money:

- charged: the provider had it: it is done, or its request's id is known. The estimate stands
  until the provider's bill is read (step 4's third pull request).
- released: it certainly never reached the provider: it never left the queue, or its gateway
  said nothing was sent. It counts for nothing.
- unsure: it may have reached the provider and the farm can't tell (an answer lost as it was
  sent, a gateway gone silent). Held like a charge until the bill shows whether it did.

The ledger never guesses down: a row counts against its show's cap at its estimate until it
is released. For the same reason a paid job is never queued again once it may have reached
its provider (it would be paid for twice): it fails, and says to look in the provider's
request history (Farm.lost)."""

from __future__ import annotations

from .policy import Refused
from .prices import money

__all__ = ["EPS", "STATES", "confirm_text", "fits", "needs_yes", "paid", "row", "settled"]

EPS = 1e-9                  # amounts are rounded to millionths of a dollar (prices.estimate)
STATES = ("held", "charged", "unsure", "released")


def paid(job: dict | None) -> bool:
    """Whether a job costs money: it was estimated when it was planned (decision 0059)."""
    return bool(job and (job.get("info") or {}).get("cost"))


def row(job_id: str, wf, cost: dict, show: str, user: str, t: float) -> dict:
    """The job's row in the ledger as it is queued: its estimate, held against its show's cap."""
    return {"job_id": job_id, "show": show.lower(), "user": user, "provider": wf.provider, "workflow": wf.name,
            "fingerprint": wf.digest, "endpoint": (wf.api or {}).get("endpoint"), "quantity": cost["quantity"],
            "unit": cost["unit"], "unit_price": cost["unit_price"], "currency": cost["currency"],
            "estimate": cost["amount"], "amount": cost["amount"], "state": "held", "created_at": t}


def fits(budget: dict, show: str, spent: float, amount: float, label: str) -> dict:
    """Whether a job of `amount` fits in `show`'s cap with `spent` already spent or held
    there: what the plan says of the cap ({show, cap, spent, left, rule, warn_at, text}), or
    Refused, naming the rule."""
    cap, rule, reason = float(budget["cap"]), budget["rule"], (budget.get("reason") or "").rstrip(".")
    ask = f" Ask {budget['contact']} if it should." if budget.get("contact") else ""
    where = f"Policy rule {rule}" if rule else f"Policy: budgets.other_shows is {money(cap)}"
    if cap <= EPS:
        why = f"{reason}. ({where}.)" if rule else f"no budgets rule gives it a cap. ({where}.)"
        raise Refused(f"Show {show} can't spend money at outside providers: {why}{ask}", rule or "budgets")
    left = round(cap - spent, 6)
    if amount > left + EPS:
        why = f" {reason}." if rule else ""
        raise Refused(f"This job's {money(amount)} at {label} doesn't fit in show {show}'s cap: {money(spent)} of its "
                      f"{money(cap)} is spent or held, so {money(max(left, 0.0))} is left.{why} ({where}.) The cap is "
                      f"raised in policy.yaml (budgets).{ask}", rule or "budgets")
    after = round(left - amount, 6)
    return {"show": show, "cap": cap, "spent": round(spent, 6), "left": after, "rule": rule,
            "warn_at": budget["warn_at"],
            "text": f"Show {show} has {money(after)} left of its {money(cap)} cap after it."}


def needs_yes(budget: dict, amount: float) -> bool:
    """Whether a job of `amount` is submitted only once the artist says yes (budgets.confirm_above,
    a courtesy: the cap is what is enforced)."""
    above = budget.get("confirm_above")
    return above is not None and amount + EPS >= float(above)


def confirm_text(cost: dict, label: str, above: float) -> str:
    return (f"This job costs about {money(cost['amount'])} at {label}, and a job of {money(above)} or more is "
            f"submitted once you say yes: submit it again, confirming. (Policy: budgets.confirm_above.)")


def settled(spend: dict, before: str, status: str) -> str:
    """What a held row becomes when its job ends with `status` (done, failed, cancelled),
    `before` being the job's status until then: charged, released or unsure."""
    if status == "done" or spend.get("request_id"):
        return "charged"
    if before == "queued" or spend.get("reached") == "no":
        return "released"
    return "unsure"
