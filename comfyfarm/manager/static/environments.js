// Runner environments on the Machines tab (README, Runner environments; decision 0032).
//
// Read-only: what each machine has built of each environment, what the manager has it
// have, what its environment manager is doing about it, the rollouts, and the ComfyUI
// syncs the manager runs. Environments change on the manager's host only (`comfyfarm env
// want`, `env rollout`, `comfyui sync --all`), until the farm has API keys (decisions
// 0030, 0031, 0033), so nothing here sends anything.
//
// Each machine's part comes live with its status (the dashboard's worker events: its
// `environments`, `env_wanted` and `env_work`). The bank's releases, the rollouts and the
// syncs come from GET /api/v1/environments: read when the tab opens, then every 15 s while
// it is open, every 5 s while a rollout or a sync is running.
//
// Loaded after the dashboard's own script, whose helpers it uses ($, esc, ago, api, state).
// Its entry points: renderEnvironments() when machines change, envsShown() when the tab
// opens. envCell(), rolloutSteps() and syncSteps() say what to show and touch no page, so
// a test runs them alone (tests/test_dashboard_envs.py).
"use strict";

const ENVS_SLOW = 15;      // seconds between reads of the releases and rollouts while the tab is open
const ENVS_FAST = 5;       // the same while a rollout is running
const ROLLING = ["running", "paused", "failed"];   // a rollout still open (manager/rollouts.py)

function envShort(fp) { return (fp || "").slice(0, 8); }

// One release's origin, said for a tooltip.
function envOrigin(o) {
  if (!o) return "";
  if (o.release) return `from the bank's release ${envShort(o.release)}` +
                        (o.published_at ? `, published ${o.published_at}` : "") +
                        (o.commit ? `, commit ${o.commit.slice(0, 10)}` : "");
  return o.checkout ? `built from the checkout (${o.checkout}) with env install, not from a release` : "";
}

// What one machine has of one environment, said as a cell: {text, cls, note, tip, error}.
// `latest` is the newest release of it in the bank (null: none). The colour says whether it
// is as the manager wants: green as wanted (or as built, when the manager has no say), the
// accent while it is on its way, red when its install failed. An older release that is what
// the manager wants is fine: it says "older" and stays green.
function envCell(w, name, latest) {
  const envs = w.environments;                 // null: it isn't checking in, or reports none
  const has = envs ? envs[name] : null;
  const cur = has ? has.current || null : null;
  const build = cur ? (has.builds || {})[cur] || {} : {};
  const wanted = w.env_wanted || {};
  const says = Object.prototype.hasOwnProperty.call(wanted, name);
  const want = says ? wanted[name] : undefined;       // a release, or null: it shouldn't have it
  const work = (w.env_work || {})[name];
  const tip = [];
  if (cur) {
    tip.push(`current ${cur}` + (build.built_at ? `, built ${build.built_at}` : "") +
             (build.build_seconds != null ? ` in ${build.build_seconds} s` : ""));
    const from = envOrigin(build.origin);
    if (from) tip.push(from);
    if (build.copied) tip.push("copied from uv's cache, not hard-linked: it takes its full size on this disk");
  }
  if (has && has.previous) tip.push(`previous ${has.previous}: wanting it again switches back without building`);
  if (says) tip.push(want ? `the manager wants ${want}` : "the manager wants it removed");
  else if (cur) tip.push("the manager doesn't manage it here: it stays as built");
  const out = (text, cls, extra = {}) => ({ text, cls, note: "", tip: tip.join("\n"), error: "", ...extra });
  const older = cur && latest && cur !== latest ? "older" : "";
  if (latest && cur && cur !== latest) tip.push(`the bank's latest is ${latest}`);

  if (work) {
    const to = work.want ? envShort(work.want) : "";
    const since = work.since || work.at;
    if (since) tip.push(`${work.state} since ${new Date(since * 1000).toLocaleString()}`);
    if (work.error && work.state === "waiting") tip.push(work.error);   // the cell says it: not listed below
    if (work.state === "installing") return out(`installing ${to}`, "warn");
    if (work.state === "removing") return out("removing", "warn");
    if (work.state === "waiting") return out(`${to || "removing"}: waits for its job`, "warn");
    if (work.state === "failed") return out(`${to} failed`, "err", { error: work.error || "the install failed" });
    if (work.state === "off") return out(`not managed`, "warn", { error: work.error || "" });
    return out(work.state || "?", "warn");
  }
  if (!envs) return out(w.online === false ? "offline" : "not reported", "muted",
                        { note: says && want ? `wants ${envShort(want)}` : "" });
  if (says && want !== cur && !(want === null && !has)) {
    // Told, and its environment manager hasn't begun (it hears at its next check-in).
    return out(want ? `→ ${envShort(want)}` : "→ remove", "warn", { note: cur ? `has ${envShort(cur)}` : "" });
  }
  if (cur && build.ok) return out(`${envShort(cur)} ✓`, "ok", { note: older });
  if (cur) return out(`${envShort(cur)} not checked`, "err");
  return out("—", "muted");
}

// A rollout's machines in its order, each marked: [{machine, mark, cls, tip}].
function rolloutSteps(r) {
  const before = r.before || {};
  return (r.machines || []).map((m, i) => {
    const step = (mark, cls, tip) => ({ machine: m, mark, cls, tip });
    const back = before[m] ? `told to go back to ${envShort(before[m])}` : "told to remove it (it had none before)";
    if (r.state === "done") return step("✓", "ok", "has it, checked");
    if (r.state === "rolled_back") return i <= r.step ? step("↩", "muted", back) : step("–", "muted", "never told");
    if (i < r.step) return step("✓", "ok", "has it, checked");
    if (i > r.step) return step(r.state === "cancelled" ? "–" : "○", "muted", "not told yet");
    if (r.state === "running") {
      return step("…", "warn", (r.eval_jobs || {})[m] ? "has it: its evals run (Queue tab), then the rollout goes on"
                                                      : "told: the rollout goes on once it has it checked");
    }
    if (r.state === "paused") return step("‖", "warn", "paused here: resume goes on");
    if (r.state === "failed") return step("✗", "err", "stopped here: roll back, or cancel");
    return step("■", "muted", "cancelled here: it keeps what it has");
  });
}

// A ComfyUI sync's machines in its order, each marked: [{machine, mark, cls, tip}] (manager/syncs.py).
const SYNC_PHASE = { draining: "drained: waiting for its jobs to end", told: "told to sync",
                     syncing: "syncing its ComfyUI", checking: "its ComfyUI is back: being checked",
                     evaluating: "checked: its evals run (Queue tab)" };
function syncSteps(s) {
  return (s.machines || []).map((m, i) => {
    const res = (s.results || {})[m] || {};
    const step = (mark, cls, tip) => ({ machine: m, mark, cls, tip });
    const said = [res.note, ...(res.differs || []).map(d => `still different: ${d}`)].filter(Boolean).join("\n");
    if (s.state === "done" || i < s.step) return step("✓", "ok", said || "synced and checked: it takes jobs again");
    if (i > s.step) return step(s.state === "running" ? "○" : "–", "muted",
                                s.state === "running" ? "not its turn yet" : "never touched");
    if (s.state === "running") {
      return step("…", "warn", [SYNC_PHASE[s.phase] || s.phase, res.step].filter(Boolean).join(": "));
    }
    if (s.state === "failed") return step("✗", "err", "stopped here: see the error");
    return step("■", "muted", "cancelled here");
  });
}

// --------------------------------------------------------------- the page
const ENV_STATE = { running: "warn", paused: "warn", failed: "err", done: "ok", rolled_back: "muted",
                    cancelled: "muted" };
let envsAt = 0;            // when the releases and rollouts were last read (seconds)

function envNames() {
  const names = new Set();
  for (const r of state.envs?.releases || []) names.add(r.name);
  for (const w of state.workers.values()) {
    for (const k of Object.keys(w.environments || {})) names.add(k);
    for (const k of Object.keys(w.env_wanted || {})) names.add(k);
    for (const k of Object.keys(w.env_work || {})) names.add(k);
  }
  return [...names].sort();
}

// An environment's column head: its name and the bank's latest release of it.
function envHead(name, rels) {
  const mine = rels.filter(r => r.name === name), l = mine[0];        // newest first
  if (!l) return `<th class="envcol" title="no release in the bank">${esc(name)}` +
                 `<div class="envsub">no release</div></th>`;
  const tip = [l.description, `latest ${l.fingerprint}, published ${l.published_at} by ${l.published_by}` +
               (l.commit ? `, commit ${l.commit.slice(0, 10)}` : ""), `${mine.length} release(s) in the bank`];
  return `<th class="envcol" title="${esc(tip.filter(Boolean).join("\n"))}">${esc(name)}` +
         `<div class="envsub">latest ${esc(envShort(l.fingerprint))}</div></th>`;
}

function envRow(w, names, latest, errors) {
  const cells = names.map(n => {
    const c = envCell(w, n, latest(n));
    if (c.error) errors.push([w.name, n, c.error, c.cls]);
    const note = c.note ? ` <span class="envsub">${esc(c.note)}</span>` : "";
    return `<td class="mono ${c.cls}" title="${esc(c.tip)}">${esc(c.text)}${note}</td>`;
  });
  return `<tr><td><b>${esc(w.name)}</b></td>${cells.join("")}<td></td></tr>`;
}

function rolloutRow(r) {
  const steps = rolloutSteps(r).map(s =>
    `<span class="${s.cls}" style="white-space:nowrap" title="${esc(s.tip)}">${s.mark} ${esc(s.machine)}</span>`);
  const error = r.error ? `<tr><td></td><td colspan="6" class="enverr ${r.state === "failed" ? "err" : "muted"}">` +
                          `${esc(r.error)}</td></tr>` : "";
  return `<tr><td><b>${esc(r.name)}</b></td><td class="mono" title="${esc(r.release)}">${esc(envShort(r.release))}</td>
    <td class="${ENV_STATE[r.state] || ""}">${esc(r.state.replace("_", " "))}</td>
    <td>${steps.join('<span class="muted"> → </span>')}</td><td class="muted">${esc(r.by || "?")}</td>
    <td class="muted">${ago(r.started_at)}</td><td class="muted">${ago(r.updated_at)}</td></tr>${error}`;
}

// A sync: a row like a rollout's, then what each machine it reached still differs in.
function syncRow(s) {
  const steps = syncSteps(s).map(x =>
    `<span class="${x.cls}" style="white-space:nowrap" title="${esc(x.tip)}">${x.mark} ${esc(x.machine)}</span>`);
  const notes = (s.machines || []).flatMap(m => {
    const res = (s.results || {})[m];
    return res && (res.differs || []).length ? [`${m}: still different, by hand: ${res.differs.join("; ")}`] : [];
  });
  const below = [s.error, ...notes].filter(Boolean);
  return `<tr><td style="white-space:nowrap"><b>ComfyUI sync</b></td>
    <td class="mono" title="the farm's environment file: ${esc(s.file)}">${esc(envShort(s.file))}</td>
    <td class="${ENV_STATE[s.state] || ""}">${esc(s.state)}</td>
    <td>${steps.join('<span class="muted"> → </span>')}</td><td class="muted">${esc(s.by || "?")}</td>
    <td class="muted">${ago(s.started_at)}</td><td class="muted">${ago(s.updated_at)}</td></tr>` +
    (below.length ? `<tr><td></td><td colspan="6" class="enverr ${s.state === "failed" ? "err" : "muted"}">` +
                    `${esc(below.join("\n"))}</td></tr>` : "");
}

function renderEnvironments() {
  const panel = $("#envsPanel"), rolling = $("#rolloutsPanel");
  if (!panel) return;
  const names = envNames(), rollouts = state.envs?.rollouts || [], syncs = state.envs?.syncs || [];
  panel.hidden = !names.length && !state.envsError;
  rolling.hidden = panel.hidden && !rollouts.length && !syncs.length;
  if (panel.hidden && rolling.hidden) return;
  const rels = state.envs?.releases || [];
  const latest = n => rels.find(r => r.name === n)?.fingerprint || null;
  // In the Machines table's order.
  const ws = [...state.workers.values()].sort((a, b) =>
    (a.rank ?? 1e9) - (b.rank ?? 1e9) || a.name.localeCompare(b.name));
  $("#envsNote").innerHTML = state.envsError ? `<span class="err">${esc(state.envsError)}</span>`
    : state.envs?.bank ? `Releases from <code>${esc(state.envs.bank)}/environments</code>`
    : state.envs ? "The manager has no bank (share.dir), so no releases" : "";
  $("#envsHead").innerHTML = `<tr><th>Machine</th>${names.map(n => envHead(n, rels)).join("")}<th></th></tr>`;
  const errors = [];
  $("#envsBody").innerHTML = ws.length ? ws.map(w => envRow(w, names, latest, errors)).join("")
    : `<tr><td colspan="${names.length + 2}" class="empty">No machines have checked in.</td></tr>`;
  $("#envsErrors").innerHTML = errors.map(([m, n, e, cls]) =>
    `<div class="enverr ${cls === "err" ? "err" : "warn"}"><b>${esc(m)} · ${esc(n)}</b>: ${esc(e)}</div>`).join("");
  // Rollouts and syncs together: open ones first, then newest first.
  const rows = [...rollouts.map(r => ({ r, open: ROLLING.includes(r.state), html: rolloutRow })),
                ...syncs.map(r => ({ r, open: r.state === "running", html: syncRow }))]
    .sort((a, b) => (b.open - a.open) || (b.r.started_at || 0) - (a.r.started_at || 0));
  $("#rollouts").innerHTML = rows.length ? rows.map(x => x.html(x.r)).join("")
    : '<tr><td colspan="7" class="empty">No rollouts yet.</td></tr>';
}

async function loadEnvironments() {
  try {
    state.envs = await api("/api/v1/environments");
    state.envsError = "";
  } catch (err) {
    state.envsError = `Can't read the environments: ${err.message}`;
  }
  envsAt = Date.now() / 1000;
  renderEnvironments();
}

function envsShown() { loadEnvironments(); }

setInterval(() => {
  const tab = typeof document !== "undefined" && document.getElementById("tab-machines");
  if (!tab || tab.hidden || document.hidden) return;
  const fast = [...(state.envs?.rollouts || []), ...(state.envs?.syncs || [])].some(r => r.state === "running");
  if (Date.now() / 1000 - envsAt >= (fast ? ENVS_FAST : ENVS_SLOW) - 0.5) loadEnvironments();
}, 1000);
