// Batch Submit (README, Batch Submit): many jobs from one table, one workflow per table.
//
// A source fills the rows: a ShotGrid Playlist now (each Version a row, its link the
// input), or Versions picked in ShotGrid (its right-click on a selection, Version links
// pasted or dropped); a ShotGrid query and Variations later. The columns are the workflow's own form
// fields, then (for a workflow that publishes) the publish ones, each with a fill cell
// under its header (sets the column for the ticked rows); any cell is editable per row
// (changed ones are outlined). The manager checks every row as the Submit form's "Will
// write" line does, its publish included (POST /api/v1/plan), a few at a time.
//
// Submit sends the ticked ready rows as ordinary jobs, one request each, in the table's
// order (so rows going into the same folder take the next versions in that order), asking
// first when there are more than 20; a refused row doesn't stop the others. A sent row shows its job live,
// and the log keeps what was sent.
//
// The table and the log live in this browser only (localStorage): they survive a reload,
// nobody else sees them.
//
// Loaded after the dashboard's own script, whose helpers it uses ($, esc, api, store,
// state, showTab, upsert, render, pubOptions, pubMark, tplLabel). Its entry points:
// batchInit() once the workflows are loaded, batchShown() when the tab opens,
// batchDeepLink() for /#batch?playlist=…&workflow=… or ?versions=<id>,<id>…, batchJobs()
// when jobs change.
"use strict";

const BATCH_KEY = "comfyfarm.batch";
const BATCH_CHECKS = 3;                    // rows checked at once
const BATCH_LOG = 200;                     // log lines kept
const BATCH_ASK_OVER = 20;                 // more jobs than this at once: asks first
const batch = {
  workflow: "", link: "", playlist: null,  // the workflow's name; the source ("versions:<ids>" for picked
                                           // Versions, else the playlist's link) and {code, url, show, missing}
  rows: [],                                // {key, version, values, ticked, touched, firstCheck, plan, error, checking, gen, job}
  fill: {},                                // column -> the value it fills in
  pub: {},                                 // workflow -> its publish options ({why} when it can't publish)
  log: [],                                 // {t, shot, text, cls, job}, newest first
  loading: false, why: "", sg: null,       // sg: the manager's ShotGrid status
  queue: [], running: 0, timers: new Map(), checkedOnce: false,
  sending: null, footMsg: "",              // sending: {done, of, stop}
};

const bWf = () => state.workflows.find(w => w.name === batch.workflow);
const bInputs = wf => wf.params.filter(p => p.type === "sequence");
const bPrimary = wf => { const i = bInputs(wf); return i.find(p => p.primary) || i[0]; };
const bDefault = p => p.type === "bool" ? !!p.default : (p.default ?? "");
const bRow = key => batch.rows.find(r => r.key === key);
const bVer = n => n ? "v" + String(n).padStart(3, "0") : "";
const bTail = dir => (dir || "").split("/").filter(Boolean).pop() || dir || "";

// The publish columns, for a workflow that publishes: each row to its own Version's shot.
function bPubCols(wf) {
  const o = batch.pub[wf.name];
  if (!wf.publish || !o || o.why) return [];
  return [
    { name: "$publish", type: "bool", label: "Publish", default: false,
      help: "Publish it when it's done: a Nuke template rendered with the frames on Tractor (Daiquiri), to the row's own show and shot" },
    { name: "$step", type: "choice", label: "Step", choices: o.steps, default: o.step },
    { name: "$template", type: "choice", label: "Nuke template", choices: o.templates, default: o.template,
      labels: Object.fromEntries(o.templates.map(k => [k, (o.labels || {})[k] || tplLabel(k)])) },
    { name: "$description", type: "string", label: "Description", default: "", placeholder: o.description },
    { name: "$dry", type: "bool", label: "Dry run", default: !!o.dry_run, locked: !!o.dry_run,
      help: o.dry_run ? "publish.dry_run is on in the manager's config" : "Daiquiri checks it, nothing goes to Tractor" },
  ];
}
const bCols = wf => [...wf.params, ...bPubCols(wf)];
function bPublish(row) {                        // the row's publish request, or null
  const v = row.values;
  if (!v.$publish) return null;
  return { step: v.$step || null, template: v.$template || null, description: String(v.$description || "").trim() || null,
           dry_run: !!v.$dry };
}
function bLoadPub(wf) {
  if (!wf?.publish || batch.pub[wf.name] !== undefined) return;
  batch.pub[wf.name] = null;                    // asking
  pubOptions(wf.name).then(o => { batch.pub[wf.name] = o; }).catch(err => { batch.pub[wf.name] = { why: err.message }; })
    .finally(() => {
      if (bWf() !== wf) return;
      for (const c of bPubCols(wf)) {           // the new columns' defaults, where nothing's set
        if (!(c.name in batch.fill)) batch.fill[c.name] = bDefault(c);
        for (const r of batch.rows) if (!(c.name in r.values)) r.values[c.name] = batch.fill[c.name];
      }
      batchRender();
      bApplyWanted();
    });
}

// ------------------------------------------------------------- kept in this browser
let bSaveTimer = null;
function bWrite() {
  bSaveTimer = null;
  store.set(BATCH_KEY, JSON.stringify({
    workflow: batch.workflow, link: batch.link, playlist: batch.playlist, fill: batch.fill, log: batch.log,
    rows: batch.rows.map(r => ({ key: r.key, version: r.version, values: r.values, ticked: r.ticked, touched: r.touched, job: r.job || null })),
  }));
}
function batchSave() {                          // soon (typing saves once it pauses), and when the page goes
  clearTimeout(bSaveTimer);
  bSaveTimer = setTimeout(bWrite, 300);
}
window.addEventListener("pagehide", () => { if (bSaveTimer) { clearTimeout(bSaveTimer); bWrite(); } });
function batchRestore() {
  let saved = null;
  try { saved = JSON.parse(store.get(BATCH_KEY) || "null"); } catch { saved = null; }
  if (!saved) return;
  batch.workflow = saved.workflow || ""; batch.link = saved.link || ""; batch.playlist = saved.playlist || null;
  batch.fill = saved.fill || {}; batch.log = saved.log || [];
  batch.rows = (saved.rows || []).map(r => ({ ...r, firstCheck: false, plan: null, error: null, checking: false, gen: 0 }));
}

// ------------------------------------------------------------- set up
function batchInit() {
  const sel = $("#batchWf");
  const usable = state.workflows.filter(w => w.enabled && bPrimary(w));
  if (!batch.restored) { batchRestore(); batch.restored = true; }
  if (!usable.some(w => w.name === batch.workflow)) batch.workflow = (usable.find(w => w.publish) || usable[0] || {}).name || "";
  sel.innerHTML = usable.map(w => `<option value="${esc(w.name)}">${esc(w.label)}</option>`).join("");
  sel.value = batch.workflow;
  sel.onchange = () => batchSetWorkflow(sel.value);
  $("#plLink").value = bPicked(batch.link) ? "" : batch.link;
  $("#plLoad").onclick = () => batchLoadPlaylist(bSource($("#plLink").value));
  $("#plLink").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); $("#plLoad").click(); } };
  $("#plClear").onclick = () => { batch.rows = []; batch.playlist = null; batch.why = ""; batchSave(); batchRender(); };
  bindTable();
  bLoadPub(bWf());
  batchRender();
  bRenderLog();
  api("/api/v1/shotgrid").then(s => { batch.sg = s; batchRender(); }).catch(() => {});
  batchDeepLink();
  if (!$("#tab-batch").hidden) batchShown();
}
function batchShown() {
  for (const r of batch.rows)                   // sent from here before: their jobs, if the dashboard doesn't have them
    if (r.job && !state.jobs.has(r.job.id))
      api(`/api/v1/jobs/${encodeURIComponent(r.job.id)}`).then(j => { upsert(j); bRenderRows(); }).catch(() => {});
  if (batch.checkedOnce || !batch.rows.length || !bWf()) { bRenderRows(); return; }
  batch.checkedOnce = true;                     // a table kept from before: check it again
  batch.rows.forEach(bCheck);
}
function batchJobs() {                          // jobs changed (live events): the sent rows' state
  if (!$("#tab-batch").hidden && batch.rows.some(r => r.job)) bRenderRows();
}
// /#batch?playlist=<link or id>&workflow=<name>, or ?versions=<id>,<id>,…: opened from
// ShotGrid (its right-click, the ComfyFarm button, a link dropped on the page) or a message.
// publish=1 ticks Publish on every row; send=1 (ShotGrid's Submit on several Versions)
// asks to send the ready rows as soon as they're checked.
function batchDeepLink() {
  const q = typeof pendingBatch !== "undefined" ? pendingBatch : null;
  if (!q || !state.workflows.length) return;
  pendingBatch = null;
  batch.wantPublish = q.get("publish") === "1";
  batch.autoSend = q.get("send") === "1";
  batch.footMsg = "";
  if (q.get("workflow") && state.workflows.some(w => w.name === q.get("workflow"))) batchSetWorkflow(q.get("workflow"), false);
  bApplyWanted();
  if (q.get("playlist")) { $("#plLink").value = q.get("playlist"); batchLoadPlaylist(q.get("playlist")); }
  else if (q.get("versions")) { $("#plLink").value = ""; batchLoadPlaylist("versions:" + q.get("versions")); }
}
// Publish asked for by the deep link (ShotGrid's Open ticks it, Submit + Publish too): ticked
// on every row once the publish columns are there. A workflow that can't publish: left as it
// is, except that Submit + Publish then says so and sends nothing.
function bApplyWanted() {
  const wf = bWf();
  if (!batch.wantPublish || !wf) return bMaybeSend();
  const o = batch.pub[wf.name];
  if (wf.publish && (o === undefined || o === null)) return;          // still asking
  batch.wantPublish = false;
  if (!wf.publish || o.why) {
    if (batch.autoSend) {
      batch.autoSend = false;
      batch.footMsg = `Not sent: ${wf.label} can't be published (${(o && o.why) || "publishing is off"}).`;
      bRenderFoot();
    }
    return;
  }
  batch.fill.$publish = true;
  for (const r of batch.rows) if (!r.job) { r.values.$publish = true; }
  batchSave(); batchRender();
  batch.rows.forEach(bCheck);
}
// Send asked for by the deep link (ShotGrid's Submit on several Versions): once every row
// is loaded and checked, the usual confirm for the ready ones.
function bMaybeSend() {
  const wf = bWf();
  if (!batch.autoSend || !wf || batch.loading || batch.wantPublish || !batch.rows.length) return;
  if (batch.rows.some(r => r.checking) || batch.queue.length || batch.running) return;
  batch.autoSend = false;
  const ready = batch.rows.filter(r => r.ticked && bReady(r));
  if (!ready.length) { batch.footMsg = "Not sent: none of the rows can run (each says why)."; bRenderFoot(); return; }
  setTimeout(() => bSend(ready), 50);                                  // after the table's drawn
}

// What the source field holds: a Playlist (link or id) as it is; Version links (one or
// more, pasted or dropped there) as the picked Versions they are.
const bPicked = link => /^versions:/.test(link || "");
function bSource(text) {
  text = String(text || "").trim();
  if (/^\d+$/.test(text) || /(?:\/detail\/Playlist\/|#Playlist_)\d+/i.test(text)) return text;
  const ids = [...text.matchAll(/(?:\/detail\/Version\/|#Version_)(\d+)/gi)].map(m => m[1]);
  return ids.length ? "versions:" + [...new Set(ids)].join(",") : text;
}

function batchSetWorkflow(name, recheck = true) {
  const old = bWf(), wf = state.workflows.find(w => w.name === name);
  if (!wf || (old === wf && Object.keys(batch.fill).length)) return;
  const oldP = old && bPrimary(old), newP = bPrimary(wf);
  batch.workflow = name;
  $("#batchWf").value = name;
  batch.fill = Object.fromEntries(bCols(wf).map(p => [p.name, bDefault(p)]));
  for (const r of batch.rows) {                 // another workflow: its defaults, the same inputs
    const input = oldP ? r.values[oldP.name] : r.version?.url;
    r.values = { ...batch.fill, [newP.name]: input ?? "" };
    r.plan = null; r.error = null;
  }
  bLoadPub(wf);
  batchSave(); batchRender();
  if (recheck) batch.rows.forEach(bCheck);
}

// ------------------------------------------------------------- the playlist
async function batchLoadPlaylist(link) {
  if (!link) { batch.why = "Paste a ShotGrid Playlist link first."; batchRender(); return; }
  const wf = bWf();
  if (!wf) return;
  batch.loading = true; batch.why = ""; batchRender();
  try {
    const r = bPicked(link) ? await api("/api/v1/shotgrid/versions?ids=" + encodeURIComponent(link.slice(9)))
      : await api("/api/v1/shotgrid/playlist?link=" + encodeURIComponent(link));
    const keep = new Map(batch.link === link ? batch.rows.map(x => [x.key, x]) : []);   // reloaded: keep the edits
    const p = bPrimary(wf);
    if (!Object.keys(batch.fill).length) batch.fill = Object.fromEntries(bCols(wf).map(x => [x.name, bDefault(x)]));
    batch.link = link; batch.playlist = r.playlist || { ...r.source, url: null };
    batch.rows = r.rows.map(row => {
      const key = String(row.version.id), was = keep.get(key);
      return was ? { ...was, version: row.version, firstCheck: false }
        : { key, version: row.version, values: { ...batch.fill, [p.name]: row.input }, ticked: true, touched: false,
            firstCheck: true, plan: null, error: null, checking: false, gen: 0, job: null };
    });
    batch.checkedOnce = true;
    batchSave();
    batch.rows.forEach(bCheck);
  } catch (err) {
    batch.why = err.message;
  } finally {
    batch.loading = false; batchRender();
  }
}

// ------------------------------------------------------------- checking the rows
function bParams(wf, row) {                     // as the Submit form sends them
  const params = {};
  for (const p of wf.params) {
    let v = row.values[p.name];
    if (p.type === "bool") v = !!v;
    else if ((p.type === "int" || p.type === "float") && v !== "" && v != null) v = Number(v);
    if (v !== "" && v != null) params[p.name] = v;
  }
  return params;
}
const bProblem = row => row.error || (row.plan?.publish?.why ? `Publish: ${row.plan.publish.why}` : "");
const bReady = row => !!row.plan && !bProblem(row) && !row.checking;
function bCheck(row) {
  row.gen += 1; row.checking = true;
  if (!batch.queue.includes(row)) batch.queue.push(row);
  bPump();
}
function bCheckSoon(row) {                      // after typing: once it stops for a moment
  clearTimeout(batch.timers.get(row.key));
  batch.timers.set(row.key, setTimeout(() => bCheck(row), 600));
}
function bPump() {
  while (batch.running < BATCH_CHECKS && batch.queue.length) {
    const row = batch.queue.shift(), gen = row.gen, wf = bWf();
    if (!wf || !batch.rows.includes(row)) continue;
    batch.running += 1;
    const body = { workflow: wf.name, params: bParams(wf, row), publish: bPublish(row) };
    api("/api/v1/plan", { method: "POST", body: JSON.stringify(body) })
      .then(plan => { if (gen === row.gen) { row.plan = plan; row.error = null; } })
      .catch(err => { if (gen === row.gen) { row.plan = null; row.error = err.message; } })
      .finally(() => {
        batch.running -= 1;
        if (gen === row.gen) {
          row.checking = false;
          if (row.firstCheck && !row.touched) row.ticked = !bProblem(row);   // a row that can't run starts unticked
          row.firstCheck = false;
          batchSave();
        }
        bRenderRows(); bPump(); bMaybeSend();
      });
  }
  bRenderFoot();
}

// ------------------------------------------------------------- sending
async function bSend(rows) {
  const wf = bWf();
  if (!wf || !rows.length || batch.sending) return;
  if (!state.auth && !$("#user").value.trim()) {
    batch.footMsg = "Enter your user name (top right) first."; bRenderFoot(); return;
  }
  const pubN = rows.filter(r => bPublish(r)).length;
  if (rows.length > BATCH_ASK_OVER && !confirm(`Submit ${rows.length} jobs of ${wf.label} to the farm?`
      + (pubN ? `\n${pubN === rows.length ? "All publish" : pubN + " publish"} when done.` : "")))
    return;
  batch.footMsg = "";
  batch.sending = { done: 0, of: rows.length, stop: false };
  bRenderRows(); bRenderFoot();
  for (const row of rows) {                     // one at a time, in the table's order
    if (batch.sending.stop) { bLog("", `Stopped: ${rows.length - batch.sending.done} not sent.`, "warn"); break; }
    const shot = row.version?.shot || row.plan?.shot || "";
    try {
      const body = { workflow: wf.name, params: bParams(wf, row), name: null };
      const pub = bPublish(row);
      if (pub) body.publish = pub;
      const job = await api("/api/v1/jobs", { method: "POST", body: JSON.stringify(body) });
      upsert(job);
      row.job = { id: job.id, short_id: job.short_id, version: job.info?.version || null, at: Date.now() / 1000 };
      row.ticked = false; row.touched = true; row.error = null;
      bCheck(row);                              // its Writes: the version after the one just taken
      const pr = job.info?.publish_request;
      bLog(shot, `job ${job.short_id} queued → ${bTail(job.info?.output_dir_posix || job.info?.output_dir)}/ ${bVer(job.info?.version)}`
        + (pr ? `, publishes when done (${[pr.step, tplLabel(pr.template || "")].filter(Boolean).join(" · ")}${pr.dry_run ? ", dry run" : ""})` : ""),
        "ok", job.id);
    } catch (err) {
      row.error = err.message;                  // stays ticked, says why
      bLog(shot, `refused: ${err.message}`, "err");
    }
    batch.sending.done += 1;
    batchSave(); bRenderRows(); bRenderFoot();
  }
  batch.sending = null;
  bRenderRows(); bRenderFoot();
  scheduleRender();
}
function bLog(shot, text, cls = "", job = null) {
  batch.log.unshift({ t: Date.now() / 1000, shot, text, cls, job });
  batch.log.length = Math.min(batch.log.length, BATCH_LOG);
  batchSave(); bRenderLog();
}

// ------------------------------------------------------------- the table
function bCell(p, value, attrs, title = "") {
  if (p.type === "bool") return `<input type="checkbox" ${value ? "checked" : ""} ${p.locked ? "disabled" : ""} ${attrs}>`;
  if (p.type === "choice")
    return `<select ${attrs}>${(p.choices || []).map(c => `<option value="${esc(c)}" ${String(c) === String(value) ? "selected" : ""}>${esc(p.labels?.[c] ?? c)}</option>`).join("")}</select>`;
  if (p.type === "int" || p.type === "float")
    return `<input type="number" value="${esc(value)}" step="${p.multiple_of || (p.type === "int" ? 1 : "any")}" ${p.min != null ? `min="${p.min}"` : ""} ${p.max != null ? `max="${p.max}"` : ""} ${attrs}>`;
  const kind = p.type === "sequence" || p.type === "output_folder" ? "path" : p.type === "text" ? "text" : "word";
  return `<input class="mono b-${kind}" value="${esc(value)}" title="${esc(title || value)}" ${p.placeholder ? `placeholder="${esc(p.placeholder)}"` : ""} ${attrs}>`;
}
const bSame = (a, b) => String(a ?? "") === String(b ?? "");

function bWrites(row, groups) {
  const r = row.plan;
  if (!r) return "";
  const dir = r.output_dir_posix || r.output_dir || "";
  const v = bVer(r.version), n = groups.get(`${dir}|${r.version}`) || 1;
  const files = r.last_file && r.last_file !== r.first_file ? `${r.first_file} … ${r.last_file}` : r.first_file || "";
  const title = `${dir}/\n${files}` + (n > 1 ? `\n${n} rows go into this folder: they take ${v} and the next versions, in the order they're sent.` : "");
  return `<span class="mono" title="${esc(title)}">${esc(bTail(dir))}/ ${esc(v)}${n > 1 ? "+" : ""}</span>`;
}
function bJob(row) {                            // a sent row: its job, live
  if (!row.job) return "";
  const j = state.jobs.get(row.job.id), status = j?.status || "queued";
  const pct = j && ["running", "postprocessing"].includes(status) ? ` ${Math.round((j.progress || 0) * 100)}%` : "";
  return `<div class="b-job">sent <span class="badge s-${esc(status)}">${esc(status)}</span>${pct}
    <a href="#" data-showjob="${esc(row.job.id)}" title="Open it in the queue">${esc(row.job.short_id)}</a>${row.job.version ? ` <span class="muted">${bVer(row.job.version)}</span>` : ""}${j ? pubMark(j) : ""}
    ${status === "failed" && j?.error ? `<span class="err">${esc(j.error)}</span>` : ""}</div>`;
}
function bState(row) {
  const problem = bProblem(row);
  let s = "";
  if (row.checking && !row.plan && !row.error) s = '<span class="muted">checking…</span>';
  else if (problem) s = `<span class="err">${esc(problem)}</span>`;
  else if (row.plan) {
    const notes = row.plan.notes || [], pr = row.plan.publish?.request;
    s = `<span class="ok">ready</span>${row.checking ? ' <span class="muted">…</span>' : ""}`
      + (pr ? ` <span class="muted" title="Publishes when done">→ ${esc([pr.show, pr.shot, pr.step].join(" · "))}${pr.dry_run ? " (dry run)" : ""}</span>` : "")
      + (notes.length ? ` <span class="warn" title="${esc(notes.join("\n"))}">⚠ ${esc(notes[0])}</span>` : "");
  }
  if (bReady(row) && !batch.sending)
    s += ` <button class="small" type="button" data-send="${esc(row.key)}" title="Submit this row now">${row.job ? "Submit again" : "Submit"}</button>`;
  return s + bJob(row);
}
function bRowHtml(wf, row, i, groups) {
  const v = row.version || {}, primary = bPrimary(wf);
  const frames = v.frames ? (v.first != null ? `${v.first}-${v.last}` : "all") : v.movie ? "movie" : "—";
  const cells = bCols(wf).map(p => {
    const val = row.values[p.name] ?? "", changed = p.name !== primary.name && !bSame(val, batch.fill[p.name]);
    return `<td class="${changed ? "b-changed" : ""}${p.name.startsWith("$") ? " b-pub" : ""}">${bCell(p, val, `data-row="${esc(row.key)}" data-p="${esc(p.name)}"`)}</td>`;
  }).join("");
  return `<tr data-key="${esc(row.key)}" class="${row.ticked || row.job ? "" : "b-off"}">
    <td class="b-stick"><label class="b-shot"><input type="checkbox" data-tick="${esc(row.key)}" ${row.ticked ? "checked" : ""}>
      <span class="muted b-num">${i + 1}</span><b>${esc(v.shot || v.entity || "")}</b></label></td>
    <td class="b-ver">${v.url ? `<a href="${esc(v.url)}" target="_blank" rel="noopener" title="${esc((v.status ? "status " + v.status + "\n" : "") + (v.frames || v.movie || ""))}">${esc(v.code || "")}</a>` : ""}</td>
    <td class="mono">${esc(frames)}</td>
    ${cells}
    <td>${bWrites(row, groups)}</td>
    <td class="b-state b-end">${bState(row)}</td></tr>`;
}
function bGroups() {                            // ticked rows going into the same folder at the same version
  const g = new Map();
  for (const r of batch.rows) if (r.ticked && r.plan) {
    const k = `${r.plan.output_dir_posix || r.plan.output_dir}|${r.plan.version}`;
    g.set(k, (g.get(k) || 0) + 1);
  }
  return g;
}
function batchRender() {
  const wf = bWf(), pl = batch.playlist, sg = batch.sg;
  const sgWhy = sg && sg.set_up === false ? sg.why : "";
  const picked = bPicked(batch.link), missing = pl?.missing || [];
  $("#plInfo").innerHTML = batch.loading ? `<span class="muted">Loading ${picked ? "the Versions" : "the playlist"} from ShotGrid…</span>`
    : batch.why ? `<span class="err">${esc(batch.why)}</span>`
    : sgWhy ? `<span class="warn">${esc(sgWhy)}</span>`
    : pl ? (pl.url ? `<a href="${esc(pl.url)}" target="_blank" rel="noopener">${esc(pl.code)}</a> · ${batch.rows.length} Version${batch.rows.length === 1 ? "" : "s"}`
                   : `<b>${esc(pl.code)}</b>`)
      + (pl.show ? ` · show <code>${esc(pl.show)}</code>` : "")
      + (missing.length ? ` · <span class="warn" title="${esc(missing.join(", "))}">${missing.length} not in ShotGrid</span>` : "")
      + ` <button class="small" type="button" id="plReload" title="Load ${picked ? "these Versions" : "the playlist"} from ShotGrid again (keeps your edits)">↻</button>`
    : "";
  if ($("#plReload")) $("#plReload").onclick = () => batchLoadPlaylist(batch.link);
  const t = $("#batchTable");
  if (!wf || !batch.rows.length) {
    t.innerHTML = `<tbody><tr><td class="empty">Paste a ShotGrid Playlist link and Load: each of its Versions becomes a row of
      ${wf ? `<b>${esc(wf.label)}</b>` : "the workflow"}, its link the input, checked as the Submit form checks one job.
      Change any cell, or a whole column with the fill cell under its header, then Submit the ticked rows.
      <span class="muted">Versions picked in ShotGrid come here too: its right-click on several, or Version links pasted above.</span></td></tr></tbody>`;
    bRenderFoot();
    return;
  }
  const primary = bPrimary(wf), all = batch.rows.every(r => r.ticked), cols = bCols(wf);
  const head = cols.map(p => `<th class="${p.name.startsWith("$") ? "b-pub" : ""}" title="${esc(p.help || "")}">${esc(p.label || p.name)}${p.required ? " *" : ""}</th>`).join("");
  const fills = cols.map(p => p.name === primary.name
    ? '<td class="muted b-fillnote">each row\'s Version</td>'
    : `<td class="${p.name.startsWith("$") ? "b-pub" : ""}">${bCell(p, batch.fill[p.name] ?? "", `data-fill="${esc(p.name)}"`, "Fill: sets this for every ticked row")}</td>`).join("");
  const groups = bGroups();
  t.innerHTML = `<thead><tr><th class="b-stick"><label class="b-shot"><input type="checkbox" id="bAll" ${all ? "checked" : ""} title="Tick or untick every row"> Shot</label></th>
      <th>Version</th><th>Frames</th>${head}<th>Writes</th><th class="b-end">State</th></tr>
    <tr class="b-fill"><td class="b-stick muted">fill ↓</td><td></td><td></td>${fills}<td></td><td class="b-end"></td></tr></thead>
    <tbody>${batch.rows.map((r, i) => bRowHtml(wf, r, i, groups)).join("")}</tbody>`;
  // The inputs' links all start alike (https://<site>/detail/Version/…): show their ends.
  t.querySelectorAll(`input[data-p="${CSS.escape(primary.name)}"]`).forEach(el => { el.scrollLeft = el.scrollWidth; });
  bRenderFoot();
}
function bRenderRows() {                        // the rows' state only (checks, jobs), keeping focus and typing
  const wf = bWf(), body = $("#batchTable tbody");
  if (!wf || !body || !batch.rows.length || !body.querySelector("tr[data-key]")) return batchRender();
  const groups = bGroups(), active = document.activeElement;
  batch.rows.forEach(r => {
    const tr = body.querySelector(`tr[data-key="${CSS.escape(r.key)}"]`);
    if (!tr) return;
    tr.className = r.ticked || r.job ? "" : "b-off";
    tr.children[tr.children.length - 2].innerHTML = bWrites(r, groups);
    if (!tr.lastElementChild.contains(active)) tr.lastElementChild.innerHTML = bState(r);
    if (!tr.contains(active)) tr.querySelector("[data-tick]").checked = r.ticked;
  });
  const all = $("#bAll");
  if (all) all.checked = batch.rows.every(r => r.ticked);
  bRenderFoot();
}
function bRenderFoot() {
  const rows = batch.rows, ticked = rows.filter(r => r.ticked), wf = bWf();
  const ready = ticked.filter(bReady), bad = ticked.filter(r => bProblem(r)).length;
  const checking = rows.filter(r => r.checking).length, sent = rows.filter(r => r.job).length;
  const o = wf && batch.pub[wf.name], s = batch.sending;
  if (!rows.length) { $("#batchFoot").innerHTML = ""; return; }
  $("#batchFoot").innerHTML = s
    ? `<span>Sending ${s.done + 1 > s.of ? s.of : s.done + 1} of ${s.of}…</span><span class="spacer"></span>
       <button type="button" id="bStop" title="Send no more rows (the ones sent stay queued)">Stop</button>`
    : `<span>${ready.length} of ${ticked.length} ticked row${ticked.length === 1 ? "" : "s"} ready`
      + (bad ? ` · <span class="err">${bad} can't run</span>` : "") + (checking ? ` · <span class="muted">checking ${checking}…</span>` : "")
      + (sent ? ` · <span class="muted">${sent} sent</span>` : "") + `</span>`
      + (o?.why && wf.publish ? ` <span class="muted" title="${esc(o.why)}">· no publishing: ${esc(o.why)}</span>` : "")
      + `<span class="spacer"></span>${batch.footMsg ? `<span class="err">${esc(batch.footMsg)}</span>` : ""}`
      + `<button class="primary" type="button" id="bSendAll" ${ready.length && !checking ? "" : "disabled"}
          title="${checking ? "Wait for the checks to finish" : "Submit the ticked ready rows, one job each, in this order"}">Submit ${ready.length} job${ready.length === 1 ? "" : "s"}</button>`;
  if ($("#bStop")) $("#bStop").onclick = () => { if (batch.sending) batch.sending.stop = true; };
  if ($("#bSendAll")) $("#bSendAll").onclick = () => bSend(batch.rows.filter(r => r.ticked && bReady(r)));
}
function bRenderLog() {
  const el = $("#batchLog");
  if (!el) return;
  const time = t => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  el.innerHTML = !batch.log.length ? "" : `<div class="b-loghead"><b>Log</b><span class="spacer"></span>
      <button class="small" type="button" id="bLogClear" title="Empty the log (the jobs stay)">Clear</button></div>`
    + batch.log.map(l => `<div class="b-logline"><span class="muted mono">${esc(time(l.t))}</span> <b>${esc(l.shot || "")}</b>
        <span class="${esc(l.cls || "")}">${esc(l.text)}</span>${l.job ? ` <a href="#" data-showjob="${esc(l.job)}">open</a>` : ""}</div>`).join("");
  if ($("#bLogClear")) $("#bLogClear").onclick = () => { batch.log = []; batchSave(); bRenderLog(); };
}

function bindTable() {
  const t = $("#batchTable");
  if (t.dataset.bound) return;
  t.dataset.bound = "1";
  const val = el => el.type === "checkbox" ? el.checked : el.value;
  const changed = (el, row) => el.closest("td").classList.toggle("b-changed", !bSame(row.values[el.dataset.p], batch.fill[el.dataset.p]));
  t.addEventListener("input", e => {
    const el = e.target;
    if (el.dataset.row && el.type !== "checkbox") {        // a cell: this row only
      const row = bRow(el.dataset.row);
      row.values[el.dataset.p] = val(el);
      el.title = el.value;
      changed(el, row);
      batchSave(); bCheckSoon(row);
    }
  });
  t.addEventListener("change", e => {
    const el = e.target, wf = bWf();
    if (el.id === "bAll") {
      batch.rows.forEach(r => { r.ticked = el.checked; r.touched = true; });
      batchSave(); batchRender(); return;
    }
    if (el.dataset.tick) {
      const row = bRow(el.dataset.tick);
      row.ticked = el.checked; row.touched = true;
      batchSave(); bRenderRows(); return;
    }
    if (el.dataset.row && (el.type === "checkbox" || el.tagName === "SELECT")) {
      const row = bRow(el.dataset.row);
      row.values[el.dataset.p] = val(el);
      changed(el, row);
      batchSave(); bCheck(row); return;
    }
    if (el.dataset.fill && wf) {                            // the column, for every ticked row
      const name = el.dataset.fill, v = val(el);
      batch.fill[name] = v;
      const hit = batch.rows.filter(r => r.ticked);
      hit.forEach(r => { r.values[name] = v; });
      batchSave(); batchRender(); hit.forEach(bCheck);
    }
  });
  const clicks = e => {
    const a = e.target.closest("[data-showjob], [data-send]");
    if (!a) return;
    e.preventDefault();
    if (a.dataset.send) { const row = bRow(a.dataset.send); if (row) bSend([row]); return; }
    state.open.add(a.dataset.showjob); showTab("queue"); render();
  };
  t.addEventListener("click", clicks);
  $("#batchLog").addEventListener("click", clicks);
}
