// Batch Submit (README, Batch Submit): many jobs from one table, one workflow per table.
//
// A source fills the rows: a ShotGrid Playlist now (each Version a row, its link the
// input); a ShotGrid query and Variations later. The columns are the workflow's own form
// fields, with a fill cell under each header (sets the column for the ticked rows) and
// any cell editable per row (changed ones are marked). The manager checks every row as
// the Submit form's "Will write" line does (POST /api/v1/plan), a few at a time.
//
// The table lives in this browser only (localStorage): it survives a reload, nobody else
// sees it. Sending the rows as jobs is the next step (chunk 12b).
//
// Loaded after the dashboard's own script, whose helpers it uses ($, esc, api, store,
// state, showTab). Its entry points: batchInit() once the workflows are loaded,
// batchShown() when the tab opens, batchDeepLink() for /#batch?playlist=…&workflow=….
"use strict";

const BATCH_KEY = "comfyfarm.batch";
const BATCH_CHECKS = 3;                    // rows checked at once
const batch = {
  workflow: "", link: "", playlist: null,  // the workflow's name; the playlist's link and {id, code, url, show}
  rows: [],                                // {key, version, values, ticked, touched, firstCheck, plan, error, checking, gen}
  fill: {},                                // param -> the value its column fills in
  loading: false, why: "", sg: null,       // sg: the manager's ShotGrid status
  queue: [], running: 0, timers: new Map(), checkedOnce: false,
};

const bWf = () => state.workflows.find(w => w.name === batch.workflow);
const bInputs = wf => wf.params.filter(p => p.type === "sequence");
const bPrimary = wf => { const i = bInputs(wf); return i.find(p => p.primary) || i[0]; };
const bDefault = p => p.type === "bool" ? !!p.default : (p.default ?? "");
const bRow = key => batch.rows.find(r => r.key === key);

// ------------------------------------------------------------- kept in this browser
let bSaveTimer = null;
function bWrite() {
  bSaveTimer = null;
  store.set(BATCH_KEY, JSON.stringify({
    workflow: batch.workflow, link: batch.link, playlist: batch.playlist, fill: batch.fill,
    rows: batch.rows.map(r => ({ key: r.key, version: r.version, values: r.values, ticked: r.ticked, touched: r.touched })),
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
  batch.fill = saved.fill || {};
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
  $("#plLink").value = batch.link;
  $("#plLoad").onclick = () => batchLoadPlaylist($("#plLink").value.trim());
  $("#plLink").onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); $("#plLoad").click(); } };
  $("#plClear").onclick = () => { batch.rows = []; batch.playlist = null; batch.why = ""; batchSave(); batchRender(); };
  bindTable();
  batchRender();
  api("/api/v1/shotgrid").then(s => { batch.sg = s; batchRender(); }).catch(() => {});
  batchDeepLink();
  if (!$("#tab-batch").hidden) batchShown();
}
function batchShown() {
  if (batch.checkedOnce || !batch.rows.length || !bWf()) return;
  batch.checkedOnce = true;                   // a table kept from before: check it again
  batch.rows.forEach(bCheck);
}
// /#batch?playlist=<link or id>&workflow=<name>: opened from ShotGrid or a message.
function batchDeepLink() {
  const q = typeof pendingBatch !== "undefined" ? pendingBatch : null;
  if (!q || !state.workflows.length) return;
  pendingBatch = null;
  if (q.get("workflow") && state.workflows.some(w => w.name === q.get("workflow"))) batchSetWorkflow(q.get("workflow"), false);
  if (q.get("playlist")) { $("#plLink").value = q.get("playlist"); batchLoadPlaylist(q.get("playlist")); }
}

function batchSetWorkflow(name, recheck = true) {
  const old = bWf(), wf = state.workflows.find(w => w.name === name);
  if (!wf || (old === wf && Object.keys(batch.fill).length)) return;
  const oldP = old && bPrimary(old), newP = bPrimary(wf);
  batch.workflow = name;
  $("#batchWf").value = name;
  batch.fill = Object.fromEntries(wf.params.map(p => [p.name, bDefault(p)]));
  for (const r of batch.rows) {                 // another workflow: its defaults, the same inputs
    const input = oldP ? r.values[oldP.name] : r.version?.url;
    r.values = { ...batch.fill, [newP.name]: input ?? "" };
    r.plan = null; r.error = null;
  }
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
    const r = await api("/api/v1/shotgrid/playlist?link=" + encodeURIComponent(link));
    const keep = new Map(batch.link === link ? batch.rows.map(x => [x.key, x]) : []);   // reloaded: keep the edits
    const p = bPrimary(wf);
    if (!Object.keys(batch.fill).length) batch.fill = Object.fromEntries(wf.params.map(x => [x.name, bDefault(x)]));
    batch.link = link; batch.playlist = r.playlist;
    batch.rows = r.rows.map(row => {
      const key = String(row.version.id), was = keep.get(key);
      return was ? { ...was, version: row.version, firstCheck: false }
        : { key, version: row.version, values: { ...batch.fill, [p.name]: row.input }, ticked: true, touched: false,
            firstCheck: true, plan: null, error: null, checking: false, gen: 0 };
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
    api("/api/v1/plan", { method: "POST", body: JSON.stringify({ workflow: wf.name, params: bParams(wf, row) }) })
      .then(plan => { if (gen === row.gen) { row.plan = plan; row.error = null; } })
      .catch(err => { if (gen === row.gen) { row.plan = null; row.error = err.message; } })
      .finally(() => {
        batch.running -= 1;
        if (gen === row.gen) {
          row.checking = false;
          if (row.firstCheck && !row.touched) row.ticked = !row.error;   // a row that can't run starts unticked
          row.firstCheck = false;
          batchSave();
        }
        bRenderRows(); bPump();
      });
  }
  bRenderFoot();
}

// ------------------------------------------------------------- the table
function bCell(p, value, attrs, title = "") {
  if (p.type === "bool") return `<input type="checkbox" ${value ? "checked" : ""} ${attrs}>`;
  if (p.type === "choice")
    return `<select ${attrs}>${(p.choices || []).map(c => `<option ${String(c) === String(value) ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  if (p.type === "int" || p.type === "float")
    return `<input type="number" value="${esc(value)}" step="${p.multiple_of || (p.type === "int" ? 1 : "any")}" ${p.min != null ? `min="${p.min}"` : ""} ${p.max != null ? `max="${p.max}"` : ""} ${attrs}>`;
  const kind = p.type === "sequence" ? "path" : p.type === "output_folder" ? "path" : p.type === "text" ? "text" : "word";
  return `<input class="mono b-${kind}" value="${esc(value)}" title="${esc(title || value)}" ${attrs}>`;
}
const bSame = (a, b) => String(a ?? "") === String(b ?? "");

function bWrites(row, groups) {
  const r = row.plan;
  if (!r) return "";
  const dir = r.output_dir_posix || r.output_dir || "";
  const tail = dir.split("/").filter(Boolean).pop() || dir;
  const v = r.version ? "v" + String(r.version).padStart(3, "0") : "";
  const n = groups.get(`${dir}|${r.version}`) || 1;
  const files = r.last_file && r.last_file !== r.first_file ? `${r.first_file} … ${r.last_file}` : r.first_file || "";
  const title = `${dir}/\n${files}` + (n > 1 ? `\n${n} rows go into this folder: they take ${v} and the next versions, in the order they're sent.` : "");
  return `<span class="mono" title="${esc(title)}">${esc(tail)}/ ${esc(v)}${n > 1 ? "+" : ""}</span>`;
}
function bState(row) {
  if (row.checking && !row.plan && !row.error) return '<span class="muted">checking…</span>';
  if (row.error) return `<span class="err">${esc(row.error)}</span>`;
  if (!row.plan) return "";
  const notes = row.plan.notes || [];
  return `<span class="ok">ready</span>${row.checking ? ' <span class="muted">…</span>' : ""}`
    + (notes.length ? ` <span class="warn" title="${esc(notes.join("\n"))}">⚠ ${esc(notes[0])}</span>` : "");
}
function bRowHtml(wf, row, i, groups) {
  const v = row.version || {}, primary = bPrimary(wf);
  const frames = v.frames ? (v.first != null ? `${v.first}-${v.last}` : "all") : v.movie ? "movie" : "—";
  const cells = wf.params.map(p => {
    const val = row.values[p.name] ?? "", changed = p.name !== primary.name && !bSame(val, batch.fill[p.name]);
    return `<td class="${changed ? "b-changed" : ""}">${bCell(p, val, `data-row="${esc(row.key)}" data-p="${esc(p.name)}"`)}</td>`;
  }).join("");
  return `<tr data-key="${esc(row.key)}" class="${row.ticked ? "" : "b-off"}">
    <td class="b-stick"><label class="b-shot"><input type="checkbox" data-tick="${esc(row.key)}" ${row.ticked ? "checked" : ""}>
      <span class="muted b-num">${i + 1}</span><b>${esc(v.shot || v.entity || "")}</b></label></td>
    <td class="b-ver">${v.url ? `<a href="${esc(v.url)}" target="_blank" rel="noopener" title="${esc((v.status ? "status " + v.status + "\n" : "") + (v.frames || v.movie || ""))}">${esc(v.code || "")}</a>` : ""}</td>
    <td class="mono">${esc(frames)}</td>
    ${cells}
    <td>${bWrites(row, groups)}</td>
    <td class="b-state">${bState(row)}</td></tr>`;
}
function bGroups() {                            // rows going into the same folder at the same version
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
  $("#plInfo").innerHTML = batch.loading ? '<span class="muted">Loading the playlist from ShotGrid…</span>'
    : batch.why ? `<span class="err">${esc(batch.why)}</span>`
    : sgWhy ? `<span class="warn">${esc(sgWhy)}</span>`
    : pl ? `<a href="${esc(pl.url)}" target="_blank" rel="noopener">${esc(pl.code)}</a> · ${batch.rows.length} Version${batch.rows.length === 1 ? "" : "s"}${pl.show ? ` · show <code>${esc(pl.show)}</code>` : ""}`
      + ` <button class="small" type="button" id="plReload" title="Load the playlist from ShotGrid again (keeps your edits)">↻</button>`
    : "";
  if ($("#plReload")) $("#plReload").onclick = () => batchLoadPlaylist(batch.link);
  const t = $("#batchTable");
  if (!wf || !batch.rows.length) {
    t.innerHTML = `<tbody><tr><td class="empty">Paste a ShotGrid Playlist link and Load: each of its Versions becomes a row of
      ${wf ? `<b>${esc(wf.label)}</b>` : "the workflow"}, its link the input, checked as the Submit form checks one job.
      Change any cell, or a whole column with the fill cell under its header. Nothing is sent from here yet.</td></tr></tbody>`;
    bRenderFoot();
    return;
  }
  const primary = bPrimary(wf), all = batch.rows.every(r => r.ticked);
  const head = wf.params.map(p => `<th title="${esc(p.help || "")}">${esc(p.label || p.name)}${p.required ? " *" : ""}</th>`).join("");
  const fills = wf.params.map(p => p.name === primary.name
    ? '<td class="muted b-fillnote">each row\'s Version</td>'
    : `<td>${bCell(p, batch.fill[p.name] ?? "", `data-fill="${esc(p.name)}"`, "Fill: sets this for every ticked row")}</td>`).join("");
  const groups = bGroups();
  t.innerHTML = `<thead><tr><th class="b-stick"><label class="b-shot"><input type="checkbox" id="bAll" ${all ? "checked" : ""} title="Tick or untick every row"> Shot</label></th>
      <th>Version</th><th>Frames</th>${head}<th>Writes</th><th>State</th></tr>
    <tr class="b-fill"><td class="b-stick muted">fill ↓</td><td></td><td></td>${fills}<td></td><td></td></tr></thead>
    <tbody>${batch.rows.map((r, i) => bRowHtml(wf, r, i, groups)).join("")}</tbody>`;
  // The inputs' links all start alike (https://<site>/detail/Version/…): show their ends.
  t.querySelectorAll(`input[data-p="${CSS.escape(primary.name)}"]`).forEach(el => { el.scrollLeft = el.scrollWidth; });
  bRenderFoot();
}
function bRenderRows() {                        // the rows only (checks finishing), keeping focus and typing
  const wf = bWf(), body = $("#batchTable tbody");
  if (!wf || !body || !batch.rows.length || !body.querySelector("tr[data-key]")) return batchRender();
  const groups = bGroups(), active = document.activeElement;
  batch.rows.forEach((r, i) => {
    const tr = body.querySelector(`tr[data-key="${CSS.escape(r.key)}"]`);
    if (!tr) return;
    tr.className = r.ticked ? "" : "b-off";
    tr.children[tr.children.length - 2].innerHTML = bWrites(r, groups);
    tr.lastElementChild.innerHTML = bState(r);
    if (!tr.contains(active)) tr.querySelector("[data-tick]").checked = r.ticked;
  });
  const all = $("#bAll");
  if (all) all.checked = batch.rows.every(r => r.ticked);
}
function bRenderFoot() {
  const rows = batch.rows, ticked = rows.filter(r => r.ticked);
  const ready = ticked.filter(r => r.plan && !r.error && !r.checking).length;
  const bad = ticked.filter(r => r.error).length, checking = rows.filter(r => r.checking).length;
  $("#batchFoot").innerHTML = !rows.length ? "" : `<span>${ready} of ${ticked.length} ticked row${ticked.length === 1 ? "" : "s"} ready`
    + (bad ? ` · <span class="err">${bad} can't run</span>` : "") + (checking ? ` · <span class="muted">checking ${checking}…</span>` : "")
    + `</span><span class="spacer"></span><span class="muted">Sending the rows to the farm comes next.</span>`
    + `<button class="primary" type="button" disabled title="Not yet: this table is for checking a batch">Submit ${ready} job${ready === 1 ? "" : "s"}</button>`;
}

function bindTable() {
  const t = $("#batchTable");
  if (t.dataset.bound) return;
  t.dataset.bound = "1";
  const val = el => el.type === "checkbox" ? el.checked : el.value;
  t.addEventListener("input", e => {
    const el = e.target;
    if (el.dataset.row && el.type !== "checkbox") {        // a cell: this row only
      const row = bRow(el.dataset.row);
      row.values[el.dataset.p] = val(el);
      el.title = el.value;
      el.closest("td").classList.toggle("b-changed", !bSame(row.values[el.dataset.p], batch.fill[el.dataset.p]));
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
      batchSave(); bRenderRows(); bRenderFoot(); return;
    }
    if (el.dataset.row && (el.type === "checkbox" || el.tagName === "SELECT")) {
      const row = bRow(el.dataset.row);
      row.values[el.dataset.p] = val(el);
      el.closest("td").classList.toggle("b-changed", !bSame(row.values[el.dataset.p], batch.fill[el.dataset.p]));
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
}
