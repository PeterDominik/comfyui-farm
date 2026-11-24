"""What a job at an outside provider will cost (decision 0059), worked out when it is planned.

A definition's `cost` block says its provider's billing unit, the price of one unit as it was
reviewed, how many units a job is (a number, or a number param such as a video's seconds) and
what scales the price (`by`: a choice param and a factor for each choice, such as Nano Banana
2's resolution). The price itself is the provider's today: the gateway reads it from the
provider's pricing API every day and sends it with what it can run (`processes[name].price`).
Until it has, the reviewed price is used, and the estimate says so.

The ledger keeps US dollars. A price in another currency, or in another unit than the
definition's, is refused, never converted: the definition needs reviewing first. A price that
moved more than DRIFT from the reviewed one is used, with a note."""

from __future__ import annotations

from datetime import datetime

__all__ = ["DRIFT", "PriceError", "drift_note", "estimate", "money", "per"]

DRIFT = 0.10                    # a move this large since the review is noted


class PriceError(Exception):
    """A provider's price the farm won't estimate from: the definition needs reviewing."""


def money(x: float) -> str:
    """US dollars as people read them: $0.08, $1.50, $0.0075."""
    if abs(x - round(x, 2)) < 1e-9:
        return f"${x:,.2f}"
    return "$" + f"{x:.4f}".rstrip("0")


def per(unit: str) -> str:
    """'an image', 'a second': the price of one."""
    return ("an " if unit[:1].lower() in "aeiou" else "a ") + unit


def _when(t) -> str:
    try:
        return datetime.fromtimestamp(float(t)).astimezone().strftime("%d %b %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def estimate(wf, values: dict, price: dict | None = None, provider: str | None = None) -> dict:
    """A job's estimate: {amount, quantity, unit, unit_price, currency, factor, by, reviewed,
    source, read_at, drift, text}. `price`: the provider's as the gateway read it ({unit_price,
    unit, currency, at}), None until it has; `provider`: its label, for the text."""
    c = wf.cost
    who = provider or wf.provider
    reviewed = float(c["price"])
    unit = c["unit"]
    unit_price, source, read_at = reviewed, "definition", None
    if price:
        if (price.get("currency") or "USD") != "USD":
            raise PriceError(f"{who} now prices {wf.label} in {price.get('currency')}, and the farm's ledger is in "
                             f"US dollars: its definition needs reviewing before it runs again.")
        if price.get("unit") != unit:
            raise PriceError(f"{who} now bills {wf.label} per {price.get('unit')}, and its definition prices it per "
                             f"{unit}: the definition needs reviewing before it runs again.")
        unit_price, source, read_at = float(price["unit_price"]), "provider", price.get("at")
    q = c.get("quantity", 1)
    quantity = float(values.get(q["param"]) or 0) if isinstance(q, dict) else float(q)
    by = c.get("by")
    choice = str(values.get(by["param"])) if by else None
    factor = float({str(k): v for k, v in by["factors"].items()}.get(choice, 1)) if by else 1.0
    amount = round(unit_price * factor * quantity, 6)
    drift = (unit_price - reviewed) / reviewed
    units = f"{quantity:g} {unit}{'' if quantity == 1 else 's'}" + (f" at {choice}" if by else "")
    rate = f"{money(unit_price)} {per(unit)}" + (f", ×{factor:g} at {choice}" if by and factor != 1 else "")
    said = (f"{who}'s price, read {_when(read_at)}" if source == "provider"
            else f"the price its definition was reviewed at; {who}'s own isn't read yet")
    return {"amount": amount, "currency": "USD", "quantity": quantity, "unit": unit, "unit_price": unit_price,
            "factor": factor, "by": choice, "reviewed": reviewed, "source": source, "read_at": read_at,
            "drift": round(drift, 4),
            "text": f"About {money(amount)} at {who}: {units}, {rate} ({said})."}


def drift_note(wf, est: dict, provider: str | None = None) -> str | None:
    """What to say when the provider's price moved more than DRIFT since the definition's review."""
    if est["source"] != "provider" or abs(est["drift"]) <= DRIFT:
        return None
    who = provider or wf.provider
    way = "above" if est["drift"] > 0 else "below"
    return (f"{who}'s price for {wf.label} is now {money(est['unit_price'])} {per(est['unit'])}, "
            f"{abs(est['drift']) * 100:.0f}% {way} the {money(est['reviewed'])} its definition was reviewed at: the "
            f"estimate uses today's price. Its definition's cost needs reviewing.")
