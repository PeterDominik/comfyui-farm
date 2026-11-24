// The dashboard's alerts (README, Alerts; decision 0052): a chip in the header while any
// alert holds, on every tab, and the Alerts tab, shown while alerts are on, with the rules,
// the alerts that hold (what is wrong and what to do) and the latest cleared. Read-only: the
// rules are config.yaml's, and an alert clears when what it is about is fixed.
//
// From GET /api/v1/alerts: when the page loads, then every 30 s, and when the tab opens.
//
// Loaded after the dashboard's own script, whose helpers it uses ($, esc, api). Its entry
// point: alertsShown() when the tab opens. alertsChip(), alertsRules(), alertsBanner(),
// alertsNote() and alertsWhen() touch no page, so a test runs them alone
// (tests/test_dashboard_alerts.py).
"use strict";

const ALERTS_SECONDS = 30;                          // between reads, on any tab: the header's chip
const ALERT_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const ALERT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
let alertsAt = 0, alertsData = null, alertsError = "", alertsLoading = false;

// The header's chip: {text, title} while any alert holds, else null.
function alertsChip(v) {
  const n = v?.enabled ? (v.active || []).length : 0;
  if (!n) return null;
  return { text: `⚠ ${n} alert${n === 1 ? "" : "s"}`,
           title: (v.active || []).map(a => a.message).join("\n") };
}

// The rules that are on, as people read them.
function alertsRules(r) {
  if (!r) return [];
  return [
    r.machine_silent_minutes ? `a machine (not drained) hasn't checked in for ${r.machine_silent_minutes} min` : "",
    r.job_waiting_minutes ? `jobs have waited for a machine for ${r.job_waiting_minutes} min` : "",
    r.failures_in_a_row ? `a machine's last ${r.failures_in_a_row} jobs all failed` : "",
    r.sweep_problems ? "the retention sweeper's latest report lists problems" : "",
    r.rollout_problems ? "a rollout or a ComfyUI sync has stopped, waiting for a person" : "",
    r.budgets ? "a show has spent its budget's warn_at of its cap at outside providers, or all of it" : "",
    r.bills ? "a provider's bill couldn't be read, or its balance is low (ledger.balance_below)" : ""]
    .filter(Boolean);
}

// What the top of the tab says, if anything: alerts off, or Google Chat not reached.
function alertsBanner(v) {
  if (!v) return null;
  if (!v.enabled) return { cls: "muted", text: "Alerts are off (alerts.enabled in the manager's config.yaml)." };
  if (v.post_error) return { cls: "warn", text: `The last alerts weren't posted to Google Chat: ${v.post_error}. ` +
                                                "They are here and in the manager's log." };
  return null;
}

// A time as people read it, and how long ago: Sun 22 Nov 23:10 (25 min ago).
function alertsWhen(t, nowT) {
  if (!t) return "";
  const d = new Date(t * 1000), s = Math.max(0, nowT - t);
  const ago = s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago`
            : s < 172800 ? `${Math.floor(s / 3600)} h ago` : `${Math.floor(s / 86400)} days ago`;
  return `${ALERT_WEEKDAYS[d.getDay()]} ${d.getDate()} ${ALERT_MONTHS[d.getMonth()]} ` +
         `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} (${ago})`;
}

// How it is checked and where alerts go.
function alertsNote(v, nowT) {
  if (!v?.enabled) return "";
  const every = v.check_seconds === 60 ? "every minute" : `every ${v.check_seconds} s`;
  const last = v.checked_at ? `, last ${alertsWhen(v.checked_at, nowT)}` : ", not yet since the manager started";
  return `Checked ${every}${last}. ` + (v.google_chat ? "New and cleared alerts are posted to Google Chat."
                                                     : "Not posted anywhere else: alerts.google_chat isn't set.");
}

// ------------------------------------------------------------- the page
function renderAlerts() {
  const v = alertsData, t = Date.now() / 1000, kinds = v?.kinds || {};
  const chip = alertsChip(v);
  $("#alertChip").hidden = !chip;
  if (chip) { $("#alertChip").textContent = chip.text; $("#alertChip").title = chip.title; }
  $("#alertsTab").hidden = !v?.enabled;
  $("#alertCount").textContent = chip ? ` ${(v.active || []).length}` : "";
  if (!$("#tab-alerts")) return;
  const banner = alertsError ? { cls: "err", text: alertsError } : alertsBanner(v);
  $("#alertsBanner").hidden = !banner;
  $("#alertsBanner").innerHTML = banner ? `<span class="${banner.cls}">${esc(banner.text)}</span>` : "";
  $("#alertsNote").textContent = alertsNote(v, t);
  const rules = alertsRules(v?.rules);
  $("#alertsRules").textContent = !v?.enabled ? "" : rules.length ? `An alert is raised when ${rules.join("; or ")}.`
    : "Every rule is off (alerts in the manager's config.yaml).";
  const when = x => `<td class="muted nowrap">${esc(alertsWhen(x, t))}</td>`;
  const kind = (a, cls) => `<td class="${cls} nowrap">${esc(kinds[a.kind] || a.kind)}</td>`;
  const none = v?.enabled ? "Nothing needs a person." : "Alerts are off.";
  $("#alertsActive").innerHTML = (v?.active || []).map(a =>
    `<tr>${when(a.raised_at)}${kind(a, "err")}<td>${esc(a.message)}</td></tr>`).join("")
    || `<tr><td colspan="3" class="empty">${none}</td></tr>`;
  $("#alertsCleared").innerHTML = (v?.cleared || []).map(a =>
    `<tr>${when(a.raised_at)}${when(a.cleared_at)}${kind(a, "")}<td class="muted">${esc(a.message)}</td></tr>`).join("")
    || '<tr><td colspan="4" class="empty">None cleared yet.</td></tr>';
}

async function loadAlerts() {
  if (alertsLoading) return;
  alertsLoading = true;
  try {
    alertsData = await api("/api/v1/alerts");
    alertsError = "";
  } catch (err) {
    alertsError = `Can't read the alerts: ${err.message}`;
  }
  alertsAt = Date.now() / 1000;
  alertsLoading = false;
  renderAlerts();
}

function alertsShown() { loadAlerts(); }

if (typeof document !== "undefined" && document.getElementById("alertChip")) {
  loadAlerts();
  setInterval(() => {
    if (!document.hidden && Date.now() / 1000 - alertsAt >= ALERTS_SECONDS - 0.5) loadAlerts();
  }, 1000);
}
