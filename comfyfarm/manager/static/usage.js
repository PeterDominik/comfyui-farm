// The dashboard's Usage tab (README, Usage; decision 0050).
//
// Jobs, GPU hours and failures over the last 7, 30 or 90 days, per day, show, definition or
// machine, for every show and definition or for one of each. From job_metrics, so the
// numbers outlive the jobs. A job here is a run: a job, or one batch of a texture set.
// Clicking a show or a definition shows it per day. Read-only.
//
// From GET /api/v1/usage: when the tab opens, when a choice changes, then every 60 s while
// it is open.
//
// Loaded after the dashboard's own script, whose helpers it uses ($, esc, api, store). Its
// entry point: usageShown() when the tab opens. usageLabel(), usageHours(), usageHoursTip(),
// usageFailed(), usageWait(), usageBar(), usageTotal() and usageQuery() touch no page, so a
// test runs them alone (tests/test_dashboard_usage.py).
"use strict";

const USAGE_SECONDS = 60;                           // between reads while the tab is open
const USAGE_COLUMN = { day: "Day", show: "Show", workflow: "Definition", machine: "Machine" };
const USAGE_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const USAGE_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
let usageAt = 0, usageData = null, usageError = "", usageLoading = false;
const usageChoice = { days: 30, by: "day", show: null, workflow: null };

// A row's first cell: Sun 22 Nov for a day, its label otherwise.
function usageLabel(row, by) {
  if (by !== "day") return row.label;
  const [y, m, d] = row.key.split("-").map(Number);
  return `${USAGE_WEEKDAYS[new Date(y, m - 1, d).getDay()]} ${d} ${USAGE_MONTHS[m - 1]}`;
}

// GPU hours as people read them: 0, <0.1, 12.5, 1,204.
function usageHours(seconds) {
  const h = (seconds || 0) / 3600;
  if (!h) return "0";
  if (h < 0.1) return "<0.1";
  return h < 100 ? h.toFixed(1) : Math.round(h).toLocaleString("en-US");
}

// The GPU hours cell's tooltip: how many of them went on runs that didn't finish done.
function usageHoursTip(row) {
  const all = `${usageHours(row.gpu_seconds)} GPU hours, from each run's start on the GPU until it finished`;
  return row.lost_seconds ? `${all}; ${usageHours(row.lost_seconds)} of them on runs that failed or were cancelled`
                          : all;
}

// Failed runs, with their share of the runs.
function usageFailed(row) {
  return row.failed ? `${row.failed} (${Math.round(100 * row.failed / row.jobs)}%)` : "0";
}

// The average wait to be given to a machine: 45s, 4 min, 1.5 h; — when none was.
function usageWait(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return Math.round(seconds) + "s";
  return seconds < 5940 ? Math.round(seconds / 60) + " min" : (seconds / 3600).toFixed(1) + " h";
}

// The bar's length, 0 to 100, against the row with the most GPU hours.
function usageBar(row, rows) {
  const top = Math.max(0, ...rows.map(r => r.gpu_seconds || 0));
  return top ? Math.round(1000 * (row.gpu_seconds || 0) / top) / 10 : 0;
}

// The line above the table: what it covers, and its totals.
function usageTotal(u) {
  const t = u.total, label = u.workflows.find(w => w.name === u.workflow)?.label || u.workflow;
  const scope = [u.show == null ? "" : `show ${u.show || "(none)"}`, u.workflow == null ? "" : label]
    .filter(Boolean).join(", ");
  const frames = t.frames.toLocaleString("en-US");
  return `${scope ? scope + ": " : ""}${t.jobs} job${t.jobs === 1 ? "" : "s"}, ` +
         `${usageHours(t.gpu_seconds)} GPU hours, ` +
         `${usageFailed(t)} failed, ${t.cancelled} cancelled, ${frames} frames done, ` +
         `since ${usageLabel({ key: u.since }, "day")}`;
}

// The request for the choices: days, per what, and the show and definition if one is picked.
function usageQuery(c) {
  let q = `/api/v1/usage?days=${c.days}&by=${c.by}`;
  if (c.show != null) q += `&show=${encodeURIComponent(c.show)}`;
  if (c.workflow != null) q += `&workflow=${encodeURIComponent(c.workflow)}`;
  return q;
}

// ------------------------------------------------------------- the page
function renderUsage() {
  if (!$("#tab-usage")) return;
  const u = usageData, c = usageChoice;
  const seg = (id, v) => $(id).querySelectorAll("button")
    .forEach(b => b.classList.toggle("on", b.dataset.v === String(v)));
  seg("#usageDays", c.days);
  seg("#usageBy", c.by);
  const pick = (id, items, current, every) => {
    const list = [...items];
    if (current != null && !list.some(i => i.value === current)) {     // picked, though nothing of it is in the span
      list.push({ value: current, text: current || "no show" });
    }
    $(id).innerHTML = `<option value="-1">${every}</option>` + list.map((i, n) =>
      `<option value="${n}"${i.value === current ? " selected" : ""}>${esc(i.text)}</option>`).join("");
    $(id).dataset.values = JSON.stringify(list.map(i => i.value));
  };
  pick("#usageShow", (u?.shows || []).map(s => ({ value: s, text: s || "no show" })), c.show, "every show");
  pick("#usageWorkflow", (u?.workflows || []).map(w => ({ value: w.name, text: w.label })), c.workflow,
       "every definition");
  $("#usageNote").textContent = usageError || (u ? usageTotal(u) : "");
  $("#usageNote").className = "envhelp" + (usageError ? " err" : "");
  $("#usageCsv").href = u ? `/api/v1/job-metrics?format=csv&since=${u.since}` : "/api/v1/job-metrics?format=csv";
  $("#usageHead").textContent = USAGE_COLUMN[u?.by || c.by];
  const rows = u?.rows || [], by = u?.by || c.by, opens = by === "show" || by === "workflow";
  const empty = u ? "No jobs finished in these days." : usageError ? "" : "Loading…";
  $("#usageRows").innerHTML = rows.length ? rows.map((r, n) => {
    const name = esc(usageLabel(r, by)), tip = esc(usageHoursTip(r));
    const key = by === "workflow" && r.label !== r.key ? ` <span class="muted mono">${esc(r.key)}</span>` : "";
    const cls = `${opens ? "row" : ""}${r.jobs ? "" : " muted"}`, title = opens ? ` title="${name} per day"` : "";
    return `<tr class="${cls}" data-n="${n}"${title}>
      <td>${name}${key}</td><td class="num">${r.jobs}</td><td class="num">${r.done}</td>
      <td class="num${r.failed ? " err" : ""}">${usageFailed(r)}</td><td class="num">${r.cancelled}</td>
      <td class="num" title="${tip}">${usageHours(r.gpu_seconds)}</td>
      <td title="${tip}"><div class="ubar"><i style="width:${usageBar(r, rows)}%"></i></div></td>
      <td class="num">${r.frames.toLocaleString("en-US")}</td><td class="num">${usageWait(r.wait_seconds)}</td></tr>`;
  }).join("") : `<tr><td colspan="9" class="empty">${empty}</td></tr>`;
}

async function loadUsage() {
  if (usageLoading) return;
  usageLoading = true;
  $("#usageTable")?.classList.add("loading");
  try {
    usageData = await api(usageQuery(usageChoice));
    usageError = "";
  } catch (err) {
    usageError = `Can't read the usage: ${err.message}`;
  }
  usageAt = Date.now() / 1000;
  usageLoading = false;
  $("#usageTable")?.classList.remove("loading");
  renderUsage();
}

function usageShown() { loadUsage(); }

if (typeof document !== "undefined" && document.getElementById("tab-usage")) {
  try {                                             // the span and the grouping are kept, not the show or definition
    const kept = JSON.parse(store.get("comfyfarm.usage") || "{}");
    if ([7, 30, 90].includes(kept.days)) usageChoice.days = kept.days;
    if (USAGE_COLUMN[kept.by]) usageChoice.by = kept.by;
  } catch { /* none kept */ }
  const changed = () => {
    store.set("comfyfarm.usage", JSON.stringify({ days: usageChoice.days, by: usageChoice.by }));
    renderUsage();
    loadUsage();
  };
  const seg = (id, key, cast) => $(id).addEventListener("click", e => {
    const b = e.target.closest("button");
    if (b) { usageChoice[key] = cast(b.dataset.v); changed(); }
  });
  seg("#usageDays", "days", Number);
  seg("#usageBy", "by", String);
  for (const [id, key] of [["#usageShow", "show"], ["#usageWorkflow", "workflow"]]) {
    $(id).addEventListener("change", e => {
      const n = Number(e.target.value);
      usageChoice[key] = n < 0 ? null : JSON.parse(e.target.dataset.values || "[]")[n] ?? null;
      changed();
    });
  }
  $("#usageRows").addEventListener("click", e => {      // a show or a definition, per day
    const tr = e.target.closest("tr.row"), r = tr && usageData?.rows[Number(tr.dataset.n)];
    if (!r) return;
    usageChoice[usageData.by === "show" ? "show" : "workflow"] = r.key;
    usageChoice.by = "day";
    changed();
  });
  setInterval(() => {
    const tab = document.getElementById("tab-usage");
    if (!tab || tab.hidden || document.hidden) return;
    if (Date.now() / 1000 - usageAt >= USAGE_SECONDS - 0.5) loadUsage();
  }, 1000);
}
