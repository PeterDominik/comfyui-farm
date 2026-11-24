// What jobs at outside providers cost (decisions 0059, 0060): the money in a job's row and
// details on the Queue tab, the question asked before a job of the policy's
// budgets.confirm_above or more is queued, and each show's spending on the Policy tab.
//
// A paid job's `info.cost` is its estimate when it was planned; `spend` is its row in the
// manager's ledger (GET /api/v1/jobs): held while it runs, then charged, unsure or released.
// index.html and policy.js draw them. Nothing here touches the page: withYes() asks through
// the `ask` it is given (the browser's confirm), so node runs the rest alone
// (tests/test_dashboard_spend.py).
"use strict";

// US dollars as people read them: $0.08, $1.50, $0.0075 (as manager/prices.py says them).
function money(x) {
  x = Number(x) || 0;
  if (Math.abs(x - Math.round(x * 100) / 100) < 1e-9)
    return "$" + x.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return "$" + x.toFixed(4).replace(/0+$/, "");
}

// What a ledger row's state means, as the job's details say it.
function spendState(state) {
  return {
    held: "Held against its show's cap until the job ends.",
    charged: "Charged: it reached the provider. The estimate stands until the provider's bill is read.",
    unsure: "Held: it may have reached the provider, and the farm can't tell. It counts against its show's cap " +
            "until the provider's bill shows whether it did.",
    released: "Released: it never reached the provider, so it cost nothing.",
  }[state] || "";
}

// A paid job's money in its Queue row: "$0.12", "$0.12?" when unsure, "released"; "" for a
// job that costs nothing.
function spendShort(j) {
  const c = j && j.info && j.info.cost;
  if (!c) return "";
  const s = j.spend;
  if (s && s.state === "released") return "released";
  return money(s ? s.amount : c.amount) + (s && s.state === "unsure" ? "?" : "");
}

// The Cost line in its details: the estimate as it was planned, then where the money is.
function spendLine(j) {
  const c = j && j.info && j.info.cost;
  if (!c) return "";
  return [c.text, j.spend ? spendState(j.spend.state) : ""].filter(Boolean).join(" ");
}

// The question asked before a job of budgets.confirm_above or more is queued, from the
// estimate the farm answered with (`confirm`).
function yesQuestion(c) {
  return [c.text, c.budget && c.budget.text, `Submit it for about ${money(c.amount)}?`].filter(Boolean).join("\n\n");
}

// Send a submit (or a Run again) once, and when the farm wants a yes first (it answers 409
// with `confirm`, its estimate), ask, then send it again with what was said yes to.
// send(yes): the request, with confirm_cost = yes when yes isn't null. ask(text): true for yes.
async function withYes(send, ask) {
  try {
    return await send(null);
  } catch (err) {
    const c = err && err.body && err.body.confirm;
    if (!c) throw err;
    if (!ask(yesQuestion(c))) throw new Error("Not submitted: it wasn't confirmed.");
    return await send(c.amount);
  }
}

// What a budgets rule's shows have spent or hold, from GET /api/v1/policy's spend_now:
// [{text, cls}] per show, cls warn past the rule's warn_at and err at its cap. `rule` null:
// the shows no rule names.
function budgetNow(rule, spendNow) {
  const mine = (spendNow || []).filter(s => (s.rule || null) === (rule || null));
  if (!mine.length) return [{ text: "nothing spent", cls: "muted" }];
  return mine.map(s => {
    const full = s.cap != null && s.amount >= s.cap - 1e-9;
    const warn = !full && s.cap && s.warn_at && s.amount >= s.warn_at * s.cap - 1e-9;
    const unsure = s.unsure ? `, ${money(s.unsure)} of it unsure` : "";
    return { text: `${s.show}: ${money(s.amount)}${s.cap != null ? ` of ${money(s.cap)}` : ""} (${s.jobs} job` +
                   `${s.jobs === 1 ? "" : "s"}${unsure})`, cls: full ? "err" : warn ? "warn" : "" };
  });
}
