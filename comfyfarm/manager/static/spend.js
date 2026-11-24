// What jobs at outside providers cost (decisions 0059 to 0061): the money in a job's row and
// details on the Queue tab, the question asked before a job of the policy's
// budgets.confirm_above or more is queued, each show's spending on the Policy tab, and the
// Spend tab: each show against its cap, each provider account (its balance, its top-ups, its
// last bill read), the ledger's rows with a CSV, and the top-ups.
//
// A paid job's `info.cost` is its estimate when it was planned; `spend` is its row in the
// manager's ledger (GET /api/v1/jobs): held while it runs, then charged, unsure or released,
// and billed once the provider's bill is read. index.html and policy.js draw the first three;
// the Spend tab is drawn here (renderSpend, from GET /api/v1/spend, read when the tab opens
// and every 30 s while it is open). The rest touches no page: withYes() asks through the `ask`
// it is given (the browser's confirm), so node runs them alone (tests/test_dashboard_spend.py).
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
    billed: "Billed: what the provider's bill charged for it.",
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

// Where a ledger row's money is, in words: its state, and what the bill said.
function rowState(s) {
  if (!s) return "";
  if (s.state === "billed")
    return s.actual ? `Billed ${money(s.actual)} on the provider's bill.`
                    : "Billed $0.00: the provider's bill charges nothing for it.";
  if (s.state === "released" && s.billed_at) return "Released: the provider's bill shows no request of it.";
  return spendState(s.state);
}

// The Cost line in its details: the estimate as it was planned, then where the money is.
function spendLine(j) {
  const c = j && j.info && j.info.cost;
  if (!c) return "";
  return [c.text, rowState(j.spend)].filter(Boolean).join(" ");
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

// ------------------------------------------------------------- the Spend tab
const SPEND_SECONDS = 30;           // between reads while the tab is open
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
let spendAt = 0, spendData = null, spendError = "", spendLoading = false;

// A time as the farm's people read it: 24 Nov 05:00 (this browser's time).
function spendWhen(t) {
  if (!t) return "never";
  const d = new Date(t * 1000), two = n => String(n).padStart(2, "0");
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

// A show's row: its cap, what it has spent or holds and how, and what is left.
function showCells(s) {
  const left = s.cap == null ? null : s.cap - s.amount;
  const full = left != null && left <= 1e-9, warn = !full && s.cap && s.warn_at && s.amount >= s.warn_at * s.cap - 1e-9;
  return { show: s.show, cap: s.cap == null ? "no cap" : money(s.cap), amount: money(s.amount),
           billed: money(s.billed || 0), charged: money(s.charged || 0), unsure: money(s.unsure || 0),
           held: money(s.held || 0), left: left == null ? "" : money(Math.max(left, 0)),
           cls: full ? "err" : warn ? "warn" : "" };
}

// What a read of a provider's bill found, or why it couldn't be read.
function readText(r) {
  if (!r) return "not read yet";
  const when = spendWhen(r.at), whose = r.stand_in ? " (the stand-in's bill, which takes no money)" : "";
  if (r.error) return `${when}: not read: ${r.error}`;
  const n = (k, one) => `${k} ${one}${k === 1 ? "" : "s"}`;
  return `${when}${whose}: ${n(r.events, "charge")}; ${n(r.matched, "job")} billed, ${r.zeroed} billed nothing, ` +
         `${r.released} released`;
}

// A provider account's row.
function accountCells(a) {
  const bal = a.balance == null ? "not read" : money(a.balance) + (a.stand_in ? " (stand-in)" : "");
  const other = a.bill && a.bill.other_charges
    ? `${money(a.bill.other)} (${a.bill.other_charges} charge${a.bill.other_charges === 1 ? "" : "s"} ` +
      "no job of the farm's is for)"
    : money(0);
  return { account: `${a.label} · ${a.account}`, balance: bal, balanceAt: a.balance_at ? spendWhen(a.balance_at) : "",
           topped: money(a.topped_up), spent: money(a.spent), other, read: readText(a.last_read),
           cls: a.last_read && a.last_read.error ? "err" : "" };
}

// A ledger row's cells: what it was estimated at, what the bill charged, where its money is.
function ledgerCells(r) {
  return { when: spendWhen(r.created_at), show: r.show, job: r.job_name || r.job_id.slice(0, 8), user: r.user,
           estimate: money(r.estimate), billed: r.actual == null ? "" : money(r.actual), counts: money(r.amount),
           state: rowState(r) };
}

// The CSV of the rows shown: one show (or all) between two dates (both days in).
function csvHref(show, since, until) {
  const q = [["format", "csv"], ["show", show], ["since", since], ["until", until]].filter(([, v]) => v)
    .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  return `/api/v1/spend?${q}`;
}

// When bills are read, for the top of the tab.
function readingNote(v) {
  const r = v && v.reading;
  if (!r) return "";
  const when = r.nightly ? `The providers' bills are read each night at ${r.at} (ledger.at)`
                         : "The providers' bills are read when asked (ledger.nightly is off)";
  return `${when}: comfyfarm spend reconcile on the manager's host reads them now. A job its bill doesn't show ` +
         `${r.grace_hours} hours after it ended is taken as not charged.`;
}

function renderSpend() {
  const v = spendData;
  const tabButton = typeof document !== "undefined" && document.getElementById("spendTab");
  if (tabButton) tabButton.hidden = !(v && v.enabled);
  if (!$("#tab-spend")) return;
  $("#spendBanner").hidden = !spendError;
  $("#spendBanner").innerHTML = spendError ? `<span class="err">${esc(spendError)}</span>` : "";
  $("#spendNote").textContent = readingNote(v);
  const num = x => `<td class="num">${esc(x)}</td>`;
  $("#spendShows").innerHTML = ((v && v.shows) || []).map(s => {
    const c = showCells(s);
    return `<tr><td class="mono">${esc(c.show)}</td>${num(c.cap)}<td class="num ${c.cls}">${esc(c.amount)}</td>` +
           `${num(c.billed)}${num(c.charged)}${num(c.unsure)}${num(c.held)}${num(c.left)}</tr>`;
  }).join("") || '<tr><td colspan="8" class="empty">No job has cost anything yet.</td></tr>';
  $("#spendAccounts").innerHTML = ((v && v.accounts) || []).map(a => {
    const c = accountCells(a);
    return `<tr><td>${esc(c.account)}</td><td class="num" title="${esc(c.balanceAt)}">${esc(c.balance)}</td>` +
           `${num(c.topped)}${num(c.spent)}<td>${esc(c.other)}</td><td class="${c.cls}">${esc(c.read)}</td></tr>`;
  }).join("") || '<tr><td colspan="6" class="empty">No provider account yet.</td></tr>';
  const label = w => (typeof wfLabel === "function" ? wfLabel(w) : w);
  $("#spendRows").innerHTML = ((v && v.rows) || []).map(r => {
    const c = ledgerCells(r);
    return `<tr><td class="muted nowrap">${esc(c.when)}</td><td class="mono">${esc(c.show)}</td>` +
           `<td>${esc(c.job)}</td><td>${esc(c.user)}</td><td>${esc(label(r.workflow))}</td>${num(c.estimate)}` +
           `${num(c.billed)}<td class="muted">${esc(c.state)}</td></tr>`;
  }).join("") || '<tr><td colspan="8" class="empty">No jobs for these dates.</td></tr>';
  $("#spendTopups").innerHTML = ((v && v.topups) || []).map(t =>
    `<tr><td class="muted nowrap">${esc(spendWhen(t.at))}</td><td>${esc(t.provider)} · ${esc(t.account)}</td>` +
    `${num(money(t.amount))}<td>${esc(t.note || "")}</td><td class="muted">${esc(t.by || "")}</td></tr>`).join("")
    || '<tr><td colspan="5" class="empty">None recorded: comfyfarm spend topup fal 50 on the manager\'s ' +
       "host.</td></tr>";
  const shows = [...new Set(((v && v.shows) || []).map(s => s.show))];
  const pick = $("#spendShow"), was = pick.value;
  pick.innerHTML = '<option value="">every show</option>' + shows.map(x => `<option>${esc(x)}</option>`).join("");
  pick.value = shows.includes(was) ? was : "";
  $("#spendCsv").href = csvHref(pick.value, $("#spendSince").value, $("#spendUntil").value);
}

async function loadSpend() {
  if (spendLoading) return;
  spendLoading = true;
  try {
    const f = $("#spendShow"), q = [["show", f && f.value], ["since", $("#spendSince") && $("#spendSince").value],
                                    ["until", $("#spendUntil") && $("#spendUntil").value]]
      .filter(([, x]) => x).map(([k, x]) => `${k}=${encodeURIComponent(x)}`).join("&");
    spendData = await api("/api/v1/spend" + (q ? "?" + q : ""));
    spendError = "";
  } catch (err) {
    spendError = `Can't read the ledger: ${err.message}`;
  }
  spendAt = Date.now() / 1000;
  spendLoading = false;
  renderSpend();
}

function spendShown() { loadSpend(); }

if (typeof document !== "undefined" && document.getElementById && typeof window !== "undefined") {
  document.addEventListener("DOMContentLoaded", () => {
    if (!document.getElementById("tab-spend")) return;
    for (const id of ["spendShow", "spendSince", "spendUntil"]) document.getElementById(id).onchange = loadSpend;
    loadSpend();                                          // the tab shows when an outside provider is on
    setInterval(() => {
      const tab = document.getElementById("tab-spend");
      if (!tab || tab.hidden || document.hidden) return;
      if (Date.now() / 1000 - spendAt >= SPEND_SECONDS - 0.5) loadSpend();
    }, 1000);
  });
}
