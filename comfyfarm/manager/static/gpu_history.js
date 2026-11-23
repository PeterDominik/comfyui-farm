// The Machines tab's GPU history (README, GPU history; decisions 0049, 0050).
//
// A graph for each GPU of each machine over the last hour, 6 hours, day or week: its use
// (nvidia-smi, gpu_probe) and its memory in use as a share of the card, one scale (0 to
// 100%), with the times the machine held farm jobs shaded. A break in a line is a time the
// machine wasn't checking in. Pointing at a graph (or the arrow keys on it) gives that
// time's numbers. Read-only.
//
// From GET /api/v1/workers/{name}/gpu-history, one call per machine: when the tab opens,
// when the span changes, then every 60 s while the tab is open. The panel shows once a
// machine has checked in and the manager keeps history (gpu_history.days above 0).
//
// Loaded after the dashboard's own script, whose helpers it uses ($, esc, api, state,
// store, gpuName). Its entry point: gpuShown() when the Machines tab opens. gpuSeries(),
// gpuPath(), gpuBands(), gpuTicks(), gpuNearest(), gpuReadout(), gpuSummary() and gpuSvg()
// touch no page, so a test runs them alone (tests/test_dashboard_gpu.py).
"use strict";

const GPU_SECONDS = 60;                             // between reads while the tab is open
const GPU_HEIGHT = 132;                             // a graph's height, px
const GPU_PAD = { left: 34, right: 10, top: 8, bottom: 20 };
const GPU_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const GPU_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
let gpuAt = 0, gpuData = new Map(), gpuHours = 24, gpuLoading = false, gpuCards = [];

const gpuPad2 = n => String(n).padStart(2, "0");
const gpuGb = b => b == null ? "?" : (b / 1e9).toFixed(1);

// Each GPU's samples, oldest first: [{gpu, samples}], by index.
function gpuSeries(samples) {
  const by = new Map();
  for (const s of samples || []) {
    if (!by.has(s.gpu)) by.set(s.gpu, []);
    by.get(s.gpu).push(s);
  }
  return [...by.entries()].sort((a, b) => a[0] - b[0])
    .map(([gpu, ss]) => ({ gpu, samples: ss.sort((a, b) => a.at - b.at) }));
}

// A sample's memory in use as a share of its card, 0 to 100, or null.
function gpuMem(s) {
  return s.used != null && s.total ? Math.min(100, 100 * s.used / s.total) : null;
}

// Samples further apart than this have a gap between them: the machine wasn't checking in.
function gpuGap(step) {
  return 2.5 * Math.max(step || 60, 60);
}

// Where a time and a share go. box: {t0, t1, x0, x1, y0, y1}, y0 the top (100%).
const gpuX = (b, t) => b.x0 + (b.x1 - b.x0) * (t - b.t0) / (b.t1 - b.t0);
const gpuY = (b, v) => b.y1 - (b.y1 - b.y0) * v / 100;

// The SVG path of `field` ("util" or "mem") through the samples: broken at gaps and where
// it wasn't read. A lone sample is a zero-length line, which its round cap shows as a dot.
function gpuPath(samples, field, box, step) {
  const gap = gpuGap(step), parts = [];
  let prev = null;
  for (const s of samples) {
    const v = field === "mem" ? gpuMem(s) : s.util;
    if (v == null) { prev = null; continue; }
    const point = `${gpuX(box, s.at).toFixed(1)} ${gpuY(box, v).toFixed(1)}`;
    if (prev && s.at - prev.at <= gap) parts[parts.length - 1].push(point);
    else parts.push([point]);
    prev = s;
  }
  return parts.map(p => "M" + p[0] + (p.length > 1 ? p.slice(1).map(q => "L" + q).join("") : "l0 0")).join("");
}

// Where the machine held farm jobs: [{x, w}] across the plot, joined where they touch.
function gpuBands(samples, box, step) {
  const gap = gpuGap(step), every = Math.max(step || 60, 60), out = [];
  samples.forEach((s, i) => {
    if (!(s.jobs > 0)) return;
    const next = samples[i + 1];
    const end = next && next.at - s.at <= gap ? next.at : s.at + every;
    const x = gpuX(box, Math.max(s.at, box.t0)), x2 = gpuX(box, Math.min(end, box.t1));
    const last = out[out.length - 1];
    if (last && Math.abs(last.x + last.w - x) < 0.5) last.w = +(x2 - last.x).toFixed(1);
    else out.push({ x: +x.toFixed(1), w: +(x2 - x).toFixed(1) });
  });
  return out.filter(b => b.w > 0);
}

// The times along the bottom, in local time, about seven at most: [{t, label}]. Hours
// (21:00), a day's start as the day (Mon 23).
function gpuTicks(t0, t1) {
  const steps = [900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400];
  const step = steps.find(s => (t1 - t0) / s <= 7) || 86400 * Math.ceil((t1 - t0) / 86400 / 7);
  const day = d => `${GPU_WEEKDAYS[d.getDay()]} ${d.getDate()}`, out = [];
  const d = new Date(t0 * 1000);
  d.setHours(0, 0, 0, 0);                           // from local midnight: ticks fall on the hour
  if (step >= 86400) {
    for (; d.getTime() / 1000 <= t1; d.setDate(d.getDate() + step / 86400)) {
      if (d.getTime() / 1000 >= t0) out.push({ t: d.getTime() / 1000, label: day(d) });
    }
    return out;
  }
  for (let t = d.getTime() / 1000; t <= t1; t += step) {
    if (t < t0) continue;
    const x = new Date(t * 1000);
    const hm = `${gpuPad2(x.getHours())}:${gpuPad2(x.getMinutes())}`;
    out.push({ t, label: hm === "00:00" ? day(x) : hm });
  }
  return out;
}

// The sample nearest time t, if one is close enough to stand for it: its index, or -1.
function gpuNearest(samples, t, step) {
  if (!samples.length) return -1;
  let lo = 0, hi = samples.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (samples[mid].at < t) lo = mid + 1; else hi = mid;
  }
  const i = lo > 0 && t - samples[lo - 1].at < samples[lo].at - t ? lo - 1 : lo;
  return Math.abs(samples[i].at - t) <= gpuGap(step) / 2 ? i : -1;
}

// What a sample says, for the line under its graph. `minutes`: how many minutes each point
// averages, when they're averaged (a long span); 0 for samples as they were taken.
function gpuReadout(s, minutes) {
  const d = new Date(s.at * 1000);
  const when = `${GPU_WEEKDAYS[d.getDay()]} ${d.getDate()} ${GPU_MONTHS[d.getMonth()]} ` +
               `${gpuPad2(d.getHours())}:${gpuPad2(d.getMinutes())}`;
  const parts = [
    s.util == null ? "use not read" : `use ${Math.round(s.util)}%${minutes ? " on average" : ""}`,
    s.used == null ? "memory not read" : `memory ${minutes ? "up to " : ""}${gpuGb(s.used)} of ${gpuGb(s.total)} GB`,
    s.jobs ? `${minutes ? "up to " : ""}${s.jobs} farm job${s.jobs > 1 ? "s" : ""}` : "no farm job"];
  return `${when}${minutes ? `, ${minutes} min` : ""}: ${parts.join(", ")}`;
}

// One line for above a graph: the span's average use, its highest memory, and how long the
// machine held farm jobs.
function gpuSummary(samples, step) {
  if (!samples.length) return "no samples in these hours";
  const util = samples.filter(s => s.util != null), mem = samples.filter(s => s.used != null);
  const parts = [util.length ? `use ${Math.round(util.reduce((n, s) => n + s.util, 0) / util.length)}% on average`
                             : "use not read (gpu_probe is off)"];
  if (mem.length) {
    const top = mem.reduce((a, s) => s.used > a.used ? s : a);
    parts.push(`memory up to ${gpuGb(top.used)} of ${gpuGb(top.total)} GB`);
  }
  const held = samples.filter(s => s.jobs > 0).length * Math.max(step || 60, 60);
  parts.push(!held ? "no farm jobs" : held < 3600 ? `farm jobs ${Math.round(held / 60)} min`
                                    : `farm jobs ${(held / 3600).toFixed(1)} h`);
  return parts.join(" · ");
}

// A graph's insides, w px wide: the 0, 50 and 100% lines, the jobs, use and memory, the
// times along the bottom, and the pointer's line and dots (hidden until pointed at).
function gpuSvg(samples, w, t0, t1, step) {
  const box = { t0, t1, x0: GPU_PAD.left, x1: w - GPU_PAD.right, y0: GPU_PAD.top, y1: GPU_HEIGHT - GPU_PAD.bottom };
  const grid = [0, 50, 100].map(v => {
    const y = gpuY(box, v);
    return `<line class="ggrid" x1="${box.x0}" x2="${box.x1}" y1="${y}" y2="${y}"/>` +
           `<text x="${box.x0 - 6}" y="${y + 3.5}" text-anchor="end">${v ? v + "%" : "0"}</text>`;
  });
  const bands = gpuBands(samples, box, step).map(b =>
    `<rect class="gjobs" x="${b.x}" y="${box.y0}" width="${b.w}" height="${box.y1 - box.y0}"/>`);
  const ticks = gpuTicks(t0, t1).map(k => {
    const x = Math.min(box.x1 - 14, Math.max(box.x0 + 14, gpuX(box, k.t)));
    return `<text x="${x.toFixed(1)}" y="${GPU_HEIGHT - 5}" text-anchor="middle">${k.label}</text>`;
  });
  return [...bands, ...grid,
    `<path class="gline gmem" d="${gpuPath(samples, "mem", box, step)}"/>`,
    `<path class="gline guse" d="${gpuPath(samples, "util", box, step)}"/>`, ...ticks,
    `<line class="gcross" x1="0" x2="0" y1="${box.y0}" y2="${box.y1}" visibility="hidden"/>`,
    `<circle class="gdot gmem" r="4" visibility="hidden"/><circle class="gdot guse" r="4" visibility="hidden"/>`,
    `<rect class="ghit" x="${box.x0}" y="0" width="${box.x1 - box.x0}" height="${GPU_HEIGHT}"/>`].join("");
}

// ------------------------------------------------------------- the page
// Each graph drawn to its width, again only when that changes (the window resized).
function gpuDraw() {
  document.querySelectorAll("#gpuCharts svg.gpusvg").forEach(svg => {
    const c = gpuCards[svg.dataset.i], w = Math.max(200, Math.round(svg.clientWidth || 400));
    if (c.w === w) return;
    c.w = w;
    c.box = { t0: c.t0, t1: c.t1, x0: GPU_PAD.left, x1: w - GPU_PAD.right, y0: GPU_PAD.top,
              y1: GPU_HEIGHT - GPU_PAD.bottom };
    svg.setAttribute("viewBox", `0 0 ${w} ${GPU_HEIGHT}`);
    svg.innerHTML = gpuSvg(c.samples, w, c.t0, c.t1, c.step);
    if (c.at >= 0) gpuPoint(svg, c.at);              // still pointed at
  });
}

function renderGpuHistory() {
  const panel = $("#gpuPanel");
  if (!panel) return;
  const machines = [...state.workers.values()]
    .sort((a, b) => (a.rank ?? 1e9) - (b.rank ?? 1e9) || a.name.localeCompare(b.name));
  const kept = [...gpuData.values()].filter(h => h && !h.error);
  panel.hidden = !machines.length || !gpuData.size || (kept.length > 0 && kept.every(h => !(h.days > 0)));
  if (panel.hidden) return;
  $("#gpuSpan").querySelectorAll("button").forEach(b => b.classList.toggle("on", Number(b.dataset.v) === gpuHours));
  const t1 = gpuAt, t0 = t1 - gpuHours * 3600;
  gpuCards = [];
  $("#gpuCharts").innerHTML = machines.map(m => {
    const h = gpuData.get(m.name);
    if (!h) return "";
    if (h.error) return `<div class="gpucard"><div class="gpuhead"><b>${esc(m.name)}</b></div>
      <div class="gpuread err">Can't read its GPU history: ${esc(h.error)}</div></div>`;
    const series = gpuSeries(h.samples);
    const minutes = h.step_seconds > 1.5 * h.sample_seconds ? Math.round(h.step_seconds / 60) : 0;
    return (series.length ? series : [{ gpu: 0, samples: [] }]).map(s => {
      const i = gpuCards.push({ samples: s.samples, step: h.step_seconds, minutes, t0, t1, at: -1 }) - 1;
      const card = (m.gpus || []).find(g => g.index === s.gpu)?.name || gpuName(m);
      const summary = gpuSummary(s.samples, h.step_seconds);
      return `<div class="gpucard"><div class="gpuhead"><b>${esc(m.name)}</b>${series.length > 1 ? ` GPU ${s.gpu}` : ""}
        <span class="muted">${esc(card)}</span><span class="gsum muted">${esc(summary)}</span></div>
        <svg class="gpusvg" data-i="${i}" tabindex="0" role="img" aria-label="${esc(`${m.name}: ${summary}`)}"
          preserveAspectRatio="none"></svg><div class="gpuread" aria-live="polite"></div></div>`;
    }).join("");
  }).join("");
  gpuDraw();
}

// The pointer's (or the arrow keys') sample on a graph: its line, dots and numbers.
function gpuPoint(svg, i) {
  const c = gpuCards[svg.dataset.i], read = svg.nextElementSibling;
  const s = c && i >= 0 ? c.samples[i] : null;
  c.at = s ? i : -1;
  const cross = svg.querySelector(".gcross"), dots = svg.querySelectorAll(".gdot");
  if (!s) {
    cross?.setAttribute("visibility", "hidden");
    dots.forEach(d => d.setAttribute("visibility", "hidden"));
    read.textContent = "";
    return;
  }
  const x = gpuX(c.box, s.at).toFixed(1);
  cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
  dots.forEach(d => {
    const v = d.classList.contains("gmem") ? gpuMem(s) : s.util;
    d.setAttribute("visibility", v == null ? "hidden" : "visible");
    if (v != null) { d.setAttribute("cx", x); d.setAttribute("cy", gpuY(c.box, v).toFixed(1)); }
  });
  read.textContent = gpuReadout(s, c.minutes);
}

async function loadGpuHistory() {
  if (gpuLoading) return;
  gpuLoading = true;
  $("#gpuCharts")?.classList.add("loading");
  const names = [...state.workers.keys()], hours = gpuHours;
  const got = await Promise.all(names.map(n =>
    api(`/api/v1/workers/${encodeURIComponent(n)}/gpu-history?hours=${hours}`).catch(err => ({ error: err.message }))));
  gpuData = new Map(names.map((n, i) => [n, got[i]]));
  gpuAt = Date.now() / 1000;
  gpuLoading = false;
  $("#gpuCharts")?.classList.remove("loading");
  renderGpuHistory();
}

function gpuShown() { if (state.workers.size) loadGpuHistory(); }

if (typeof document !== "undefined" && document.getElementById("gpuPanel")) {
  gpuHours = Number(store.get("comfyfarm.gpuHours")) || 24;
  $("#gpuSpan").addEventListener("click", e => {
    const v = Number(e.target.closest("button")?.dataset.v);
    if (!v || v === gpuHours) return;
    gpuHours = v;
    store.set("comfyfarm.gpuHours", String(v));
    $("#gpuSpan").querySelectorAll("button").forEach(b => b.classList.toggle("on", Number(b.dataset.v) === v));
    loadGpuHistory();
  });
  const charts = $("#gpuCharts");
  const at = (svg, e) => {
    const c = gpuCards[svg.dataset.i], r = svg.getBoundingClientRect();
    const x = (e.clientX - r.left) * (c.box.x1 + GPU_PAD.right) / r.width;
    return gpuNearest(c.samples, c.t0 + (x - c.box.x0) / (c.box.x1 - c.box.x0) * (c.t1 - c.t0), c.step);
  };
  charts.addEventListener("pointermove", e => {
    const svg = e.target.closest("svg.gpusvg");
    if (svg) gpuPoint(svg, at(svg, e));
  });
  charts.addEventListener("pointerout", e => {
    const svg = e.target.closest("svg.gpusvg");
    if (svg && !svg.contains(e.relatedTarget)) gpuPoint(svg, -1);
  });
  charts.addEventListener("keydown", e => {
    const svg = e.target.closest("svg.gpusvg"), c = svg && gpuCards[svg.dataset.i];
    if (!c || !c.samples.length || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const last = c.samples.length - 1, i = c.at < 0 ? last : c.at;
    gpuPoint(svg, { ArrowLeft: Math.max(0, i - 1), ArrowRight: Math.min(last, i + 1), Home: 0, End: last }[e.key]);
  });
  charts.addEventListener("focusout", e => { if (e.target.matches?.("svg.gpusvg")) gpuPoint(e.target, -1); });
  if (window.ResizeObserver) {
    let queued = false;
    new ResizeObserver(() => {
      if (queued || !gpuCards.length) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; gpuDraw(); });
    }).observe(charts);
  }
  setInterval(() => {
    const tab = document.getElementById("tab-machines");
    if (!tab || tab.hidden || document.hidden || !state.workers.size) return;
    if (Date.now() / 1000 - gpuAt >= GPU_SECONDS - 0.5 || !gpuData.size) loadGpuHistory();
  }, 1000);
}
