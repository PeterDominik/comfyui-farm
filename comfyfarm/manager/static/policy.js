// The dashboard's Policy tab (README, Policy; decisions 0043 to 0047).
//
// Read-only: the policy.yaml in force (its show rules, licence rules, quotas, provider rules
// and budgets), why the file as it is now isn't taken if it isn't, what is queued against each
// quota, what each show has spent against its cap (budgetNow, spend.js), and the models each
// definition lists with their licences. The policy is edited in the bank
// folder, checked first with `comfyfarm policy check`, so nothing here sends anything.
//
// Everything comes from GET /api/v1/policy: read when the page loads (the tab shows only
// when policy is on), when the tab opens, then every 15 s while it is open.
//
// Loaded after the dashboard's own script, whose helpers it uses ($, esc, api). Its entry
// point: policyShown() when the tab opens. policyBanner(), ruleShows(), ruleDoes(),
// quotaNow() and modelsText() say what to show and touch no page, so a test runs them
// alone (tests/test_dashboard_policy.py).
"use strict";

const POLICY_SECONDS = 15;          // between reads while the tab is open
let policyAt = 0, policyData = null, policyError = "";

// What the top of the tab says, if anything: the policy off, never read, or not taken.
function policyBanner(p) {
  if (!p) return null;
  if (!p.enabled) return { cls: "muted", text: "Policy is off (policy.enabled in the manager's config.yaml): " +
                                               "every show may use the farm." };
  if (!p.in_force) return { cls: "err", text: "No valid policy has been read, so no job can be planned until " +
                                             `${p.file || "policy.yaml"} is fixed. ${p.error || ""}`.trim() };
  if (p.error) return { cls: "warn", text: p.error };
  return null;
}

// Where the rules in force came from, and when.
function policyNote(p) {
  const f = p?.in_force;
  if (!f) return p?.file ? `From ${p.file}` : "";
  return `From ${p.file}, taken ${f.taken_at || "?"} (${String(f.fingerprint || "").slice(0, 8)})` +
         (f.source === "saved copy"
           ? ", from the copy the manager kept: the file hasn't been read since it started" : "");
}

// A rule's shows as people read them: every show but rnd*, guem2609, ret*.
function ruleShows(r) {
  const names = (r.shows || []).includes("*") ? "every show" : (r.shows || []).join(", ");
  return names + ((r.except || []).length ? ` but ${r.except.join(", ")}` : "");
}

// What a rule does. kind: "show", "licence", "quota" or "provider".
function ruleDoes(r, kind) {
  if (kind === "provider") {
    return r.allow ? `May send work to ${r.allow.join(", ")}` : `May not use ${(r.deny || []).join(", ")}`;
  }
  if (kind === "quota") {
    const most = [r.max_jobs ? `${r.max_jobs} job${r.max_jobs === 1 ? "" : "s"}` : "",
                  r.max_frames ? `${r.max_frames} frames` : ""].filter(Boolean).join(" and ");
    const per = { show: "for each show", user: "for each user, over these shows", all: "for these shows together" };
    return `At most ${most} queued or running, ${per[r.per] || per.show}`;
  }
  const what = kind === "licence" ? "licences " : "";
  if (r.deny === "all") return "Refuses every definition";
  if (r.deny) return `Refuses ${what}${r.deny.join(", ")}`;
  return `Only ${what}${(r.allow || []).join(", ")}`;
}

// What is queued against a quota now: [{text, full}] per show, user or the shows together.
function quotaNow(r, groups) {
  if (!(groups || []).length) return [{ text: "nothing queued", full: false }];
  return groups.map(g => {
    const parts = [];
    if (r.max_jobs) parts.push(`${g.jobs} of ${r.max_jobs} job${r.max_jobs === 1 ? "" : "s"}`);
    if (r.max_frames) parts.push(`${g.frames} of ${r.max_frames} frames`);
    const full = (r.max_jobs && g.jobs >= r.max_jobs) || (r.max_frames && g.frames >= r.max_frames);
    return { text: `${g.for === "together" ? "" : g.for + ": "}${parts.join(", ")}`, full: !!full };
  });
}

// A definition's models as the policy sees them: {text, cls}.
function modelsText(models) {
  if (models === null || models === undefined) return { text: "not listed (unlisted_models decides)", cls: "muted" };
  if (!models.length) return { text: "no model weights", cls: "muted" };
  return { text: models.map(m => `${m.file} (${m.licence})`).join(", "), cls: "" };
}

function renderPolicy() {
  const tab = $("#tab-policy");
  if (!tab) return;
  const p = policyData, f = p?.in_force;
  const banner = policyError ? { cls: "err", text: policyError } : policyBanner(p);
  $("#policyBanner").hidden = !banner;
  $("#policyBanner").innerHTML = banner ? `<span class="${banner.cls}">${esc(banner.text)}</span>` : "";
  $("#policyNote").textContent = policyNote(p);
  $("#policyDefaults").innerHTML = f ? [
    ["Shows no rule names", f.other_shows], ["A job whose show isn't known", f.unknown_show],
    ["A definition that doesn't list its models", f.unlisted_models], ["Who to ask", f.contact || "—"]]
    .map(([k, v]) => `<span class="muted">${esc(k)}:</span> <b>${esc(v)}</b>`).join(" · ") : "";
  const rows = (rules, kind) => (rules || []).map(r => `<tr><td class="mono">${esc(r.name)}</td>
    <td>${esc(ruleShows(r))}</td><td>${esc(ruleDoes(r, kind))}</td><td class="muted">${esc(r.reason)}</td></tr>`);
  const table = (id, rules, kind, none) => {
    const html = rows(rules, kind);
    $(id).innerHTML = html.length ? html.join("") : `<tr><td colspan="4" class="empty">${esc(none)}</td></tr>`;
  };
  table("#policyShows", f?.shows, "show", "No show rules: every show may use every definition.");
  table("#policyLicences", f?.licences, "licence", "No licence rules.");
  table("#policyProviders", f?.providers, "provider", "No provider rules: no show may send work outside Rodeo.");
  renderBudgets(f?.budgets, p?.spend_now);
  const quotas = f?.quotas || [];
  $("#policyQuotas").innerHTML = quotas.length ? quotas.map(r => `<tr><td class="mono">${esc(r.name)}</td>
    <td>${esc(ruleShows(r))}</td><td>${esc(ruleDoes(r, "quota"))}</td>
    <td>${quotaNow(r, p.quotas_now?.[r.name])
      .map(n => `<div class="${n.full ? "warn" : ""}">${esc(n.text)}</div>`).join("")}</td>
    <td class="muted">${esc(r.reason)}</td></tr>`).join("")
    : '<tr><td colspan="5" class="empty">No quotas.</td></tr>';
  $("#policyModels").innerHTML = (p?.definitions || []).map(d => {
    const m = modelsText(d.models);
    return `<tr><td>${esc(d.label)} <span class="muted mono">${esc(d.name)}</span></td>
      <td class="${m.cls}">${esc(m.text)}</td></tr>`;
  }).join("") || '<tr><td colspan="2" class="empty">No definitions.</td></tr>';
}

// The Budgets section (decision 0060): each rule's cap and what its shows spent or hold, then
// the shows no rule names. Without a budgets: section no show may spend money.
function renderBudgets(b, spendNow) {
  if (!$("#policyBudgets")) return;
  $("#policyBudgetDefaults").innerHTML = b ? [
    ["Shows no rule names", money(b.other_shows)], ["A yes is asked for a job of", b.confirm_above == null ? "never"
      : money(b.confirm_above) + " or more"], ["Alert at", Math.round(b.warn_at * 100) + "% of a cap"]]
    .map(([k, v]) => `<span class="muted">${esc(k)}:</span> <b>${esc(v)}</b>`).join(" · ") : "";
  const now = (rule) => budgetNow(rule, spendNow).map(n => `<div class="${n.cls}">${esc(n.text)}</div>`).join("");
  const rows = (b?.rules || []).map(r => `<tr><td class="mono">${esc(r.name)}</td><td>${esc(ruleShows(r))}</td>
    <td>${esc(money(r.cap))}${r.warn_at
      ? ` <span class="muted">(alert at ${Math.round(r.warn_at * 100)}%)</span>` : ""}</td>
    <td>${now(r.name)}</td><td class="muted">${esc(r.reason)}</td></tr>`);
  if (b && (spendNow || []).some(s => !s.rule)) {
    rows.push(`<tr><td class="muted">other_shows</td><td>shows no rule names</td><td>${esc(money(b.other_shows))}</td>
      <td>${now(null)}</td><td></td></tr>`);
  }
  const none = b ? "No budgets rules: no show may spend more than other_shows."
                 : "No budgets: section: no show may spend money at outside providers.";
  $("#policyBudgets").innerHTML = rows.length ? rows.join("") : `<tr><td colspan="5" class="empty">${none}</td></tr>`;
}

async function loadPolicy() {
  try {
    policyData = await api("/api/v1/policy");
    policyError = "";
  } catch (err) {
    policyError = `Can't read the policy: ${err.message}`;
  }
  policyAt = Date.now() / 1000;
  const button = $("#policyTab");
  if (button) button.hidden = !policyData?.enabled;
  renderPolicy();
}

function policyShown() { loadPolicy(); }

if (typeof document !== "undefined" && document.getElementById("tab-policy")) {
  loadPolicy();                                       // the tab shows only when policy is on
  setInterval(() => {
    const tab = document.getElementById("tab-policy");
    if (!tab || tab.hidden || document.hidden) return;
    if (Date.now() / 1000 - policyAt >= POLICY_SECONDS - 0.5) loadPolicy();
  }, 1000);
}
