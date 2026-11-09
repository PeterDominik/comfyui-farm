// From ShotGrid (README, ShotGrid right-click): three ways to bring a ShotGrid Version or
// Playlist to the farm, each ending in one of the dashboard's deep links:
//
//   one Version         /#submit?input=<its link>        the Submit form, its input filled
//   a Playlist          /#batch?playlist=<its link>      Batch Submit, a row per Version
//   several Versions    /#batch?versions=<id>,<id>,…     Batch Submit, a row each
//
// - The ComfyFarm button: a bookmarklet, dragged once onto the bookmarks bar. Clicked on a
//   ShotGrid page, it reads the page's address for the Version or Playlist open there.
// - A Version's or Playlist's name (a link) dragged from ShotGrid and dropped on this page.
// - ShotGrid's own right-click, an Action Menu Item (tools/sg_ami.py) that posts to
//   /api/v1/shotgrid/ami, which redirects here.
//
// Nothing is sent from here: the form or the table opens filled in, checked as ever.
// Loaded after the dashboard's own script, whose helpers it uses ($, esc, api, deepLink).
// Its entry point: sgInit() once the dashboard is up. The dialog with the ComfyFarm button
// opens at /#shotgrid (no button for it on the page).
"use strict";

// The button's code. It runs on the ShotGrid page, so it uses nothing of the dashboard's,
// and it's kept to what a bookmark can hold. `farm` is this dashboard's address. The page's
// address names what's open: an overlay (#Version_<id>), a detail page (/detail/Playlist/<id>),
// the Media Center (?type=Version&id=<id>, or its tree's …/Playlist/<id>).
function sgButton(farm) {
  const u = new URL(location.href), q = u.searchParams;
  let seen = "", m;
  const pick = (t, i) => {
    if (!t || !/^\d+$/.test(i || "")) return null;
    seen = seen || t;
    return t === "Version" || t === "Playlist" ? [t, i] : null;
  };
  const e = ((m = /#([A-Za-z]+)_(\d+)/.exec(u.hash)) && pick(m[1], m[2]))
    || ((m = /\/detail\/([A-Za-z]+)\/(\d+)/.exec(u.pathname)) && pick(m[1], m[2]))
    || pick(q.get("type") || q.get("entity_type"), q.get("id") || q.get("entity_id"))
    || ((m = /.*\/(Version|Playlist)\/(\d+)/.exec(q.get("tree_path") || "")) && pick(m[1], m[2]));
  if (!e) {
    alert("ComfyFarm: " + (seen ? "this page shows a " + seen + "; the farm takes a Version or a Playlist."
                                : "this page's address doesn't name a Version or a Playlist.")
          + "\n\nOpen the Version (or the Playlist) on its own page, or drag its name onto the farm's page.");
    return;
  }
  window.open(farm + (e[0] === "Version" ? "/#submit?input=" : "/#batch?playlist=")
              + encodeURIComponent(u.origin + "/detail/" + e[0] + "/" + e[1]), "comfyfarm");
}
// As a bookmark: the code percent-encoded (a bookmark's address loses its line breaks).
const sgButtonHref = () => "javascript:" + encodeURIComponent(`(${sgButton})(${JSON.stringify(location.origin)});`);

// ShotGrid links in dropped text (a dragged link, several, or text holding them):
// {versions: [link], playlists: [link]}, each once, as …/detail/<type>/<id>.
function sgLinks(text) {
  const found = { Version: [], Playlist: [] };
  for (const w of String(text || "").split(/\s+/)) {
    if (!/^https?:\/\//i.test(w)) continue;
    let u;
    try { u = new URL(w); } catch { continue; }
    const m = /(?:\/detail\/|#)(Version|Playlist)[/_](\d+)/.exec(u.pathname + u.hash);
    if (m) found[m[1]].push(`${u.origin}/detail/${m[1]}/${m[2]}`);
  }
  return { versions: [...new Set(found.Version)], playlists: [...new Set(found.Playlist)] };
}
// Where dropped links go: their deep link, or null.
function sgDropTarget(text) {
  const { versions, playlists } = sgLinks(text);
  if (playlists.length) return "#batch?playlist=" + encodeURIComponent(playlists[0]);
  if (versions.length === 1) return "#submit?input=" + encodeURIComponent(versions[0]);
  if (versions.length) return "#batch?versions=" + versions.map(v => v.split("/").pop()).join(",");
  return null;
}

// ------------------------------------------------------------- dropping a link on the page
// Anywhere but in a field, which takes the link as text (as ever).
const sgHint = () => $("#dropHint");
let sgHintTimer = null;
function sgShowHint(text, cls = "") {
  const h = sgHint();
  h.textContent = text; h.className = cls; h.hidden = false;
}
function sgHideHint(after = 0) {
  clearTimeout(sgHintTimer);
  sgHintTimer = setTimeout(() => { sgHint().hidden = true; }, after);
}
const sgField = el => el?.closest?.("input, textarea, select, [contenteditable]");
const sgIsLink = dt => [...(dt?.types || [])].includes("text/uri-list");
document.addEventListener("dragover", e => {
  if (!sgIsLink(e.dataTransfer) || sgField(e.target)) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = "copy";
  if (sgHint().hidden || sgHint().className === "err")
    sgShowHint("Drop a ShotGrid Version: the Submit form · a Playlist or several Versions: Batch Submit");
  sgHideHint(250);                              // dragover keeps coming while it's over the page
});
document.addEventListener("drop", e => {
  if (!sgIsLink(e.dataTransfer) || sgField(e.target)) return;
  e.preventDefault();
  const dt = e.dataTransfer;
  const text = (dt.getData("text/uri-list") || "").split(/\r?\n/).filter(l => l && !l.startsWith("#")).join("\n")
    || dt.getData("text/plain");
  const target = sgDropTarget(text);
  if (target) { sgHideHint(); deepLink(target); return; }
  sgShowHint("That's not a ShotGrid Version or Playlist link.", "err");
  sgHideHint(3000);
});

// ------------------------------------------------------------- the From ShotGrid dialog (/#shotgrid)
function sgOpen() {
  const dlg = $("#sgDialog");
  if (!dlg || dlg.open) return;
  $("#sgButtonMsg").textContent = "";
  dlg.showModal();
}
function sgInit() {
  const dlg = $("#sgDialog"), link = $("#sgButton");
  link.href = sgButtonHref();
  link.onclick = e => {                          // it's for the bookmarks bar, not for here
    e.preventDefault();
    $("#sgButtonMsg").textContent = "Drag it onto your bookmarks bar, then click it on a ShotGrid page.";
  };
  $("#sgFarm").textContent = location.origin;
  dlg.addEventListener("click", e => { if (e.target === dlg || e.target.dataset.close != null) dlg.close(); });
  if (state.sgDialog) { state.sgDialog = false; sgOpen(); }     // opened at /#shotgrid, before this loaded
}
