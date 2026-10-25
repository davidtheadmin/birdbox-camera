"use strict";
const $ = (id) => document.getElementById(id);

async function api(url, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || res.statusText);
    err.data = data;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2500);
}

// ---------- status bar ----------

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function fmtBytes(b) {
  if (b >= 1 << 30) return (b / (1 << 30)).toFixed(1) + " GB";
  if (b >= 1 << 20) return (b / (1 << 20)).toFixed(1) + " MB";
  return (b / 1024).toFixed(0) + " KB";
}

let recStartedLocal = null; // local clock reference for the duration ticker

async function pollStatus() {
  try {
    const s = await api("/api/status");
    $("chip-uptime").textContent = `⏳ ${fmtUptime(s.uptime)}`;
    $("chip-temp").textContent = `\u{1F321}️ ${s.cpu_temp != null ? s.cpu_temp + "°C" : "n/a"}`;
    $("chip-wifi").textContent = `\u{1F4F6} ${s.wifi_dbm != null ? s.wifi_dbm + " dBm" : "n/a"}`;
    $("chip-disk").textContent = `\u{1F4BE} ${fmtBytes(s.disk.free)} free`;
    $("chip-mock").classList.toggle("hidden", !(s.mock.gpio || s.mock.camera));
    updateLed(s.led);
    updateRec(s.recording);
  } catch (e) { /* transient; keep last values */ }
}

// ---------- recording ----------

function updateRec(rec) {
  const on = !!rec.recording;
  $("chip-rec").classList.toggle("hidden", !on);
  $("rec-overlay").classList.toggle("hidden", !on);
  const btn = $("btn-record");
  btn.classList.toggle("recording", on);
  btn.innerHTML = on ? "⏹ Stop" : "⏺ Record";
  recStartedLocal = on ? Date.now() - rec.duration * 1000 : null;
}

setInterval(() => { // smooth duration ticker between polls
  if (recStartedLocal === null) return;
  const s = Math.floor((Date.now() - recStartedLocal) / 1000);
  const txt = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  $("rec-time").textContent = txt;
  $("chip-rec").textContent = `⏺ REC ${txt}`;
}, 500);

$("btn-record").onclick = async () => {
  try {
    if (recStartedLocal !== null) {
      const r = await api("/api/record/stop", "POST");
      toast(`Saved ${r.saved} (${r.duration}s)`);
      updateRec({ recording: false });
      loadGallery();
    } else {
      await api("/api/record/start", "POST");
      updateRec({ recording: true, duration: 0 });
    }
  } catch (e) { toast("Error: " + e.message); }
};

$("btn-snapshot").onclick = async () => {
  try {
    const r = await api("/api/snapshot", "POST");
    toast(`Saved ${r.saved}`);
    loadGallery();
  } catch (e) { toast("Error: " + e.message); }
};

// ---------- LED control ----------

const slider = $("brightness-slider");
let sliderBusy = false;   // user is dragging — don't overwrite from polls
let ledMode = "auto";

function updateLed(led) {
  ledMode = led.mode;
  document.querySelectorAll("#mode-toggle button").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === led.mode));
  $("led-brightness").textContent = led.brightness.toFixed(0) + "%";
  $("light-level").textContent = led.level != null ? led.level : "–";
  slider.disabled = led.mode !== "manual";
  if (!sliderBusy) {
    slider.value = led.mode === "manual" ? led.manual_brightness : led.brightness;
    $("slider-value").textContent = Math.round(slider.value) + "%";
  }
}

document.querySelectorAll("#mode-toggle button").forEach(btn => {
  btn.onclick = async () => {
    try { updateLed(await api("/api/led", "POST", { mode: btn.dataset.mode })); }
    catch (e) { toast("Error: " + e.message); }
  };
});

let sliderSendTimer = null;
slider.addEventListener("input", () => {
  sliderBusy = true;
  $("slider-value").textContent = slider.value + "%";
  if (!sliderSendTimer) {
    sliderSendTimer = setTimeout(async () => {
      sliderSendTimer = null;
      try { await api("/api/led", "POST", { brightness: +slider.value }); } catch (e) {}
    }, 150);
  }
});
slider.addEventListener("change", async () => {
  sliderBusy = false;
  try { updateLed(await api("/api/led", "POST", { brightness: +slider.value })); }
  catch (e) { toast("Error: " + e.message); }
});

// ---------- sensor history graphs ----------
// View window [viewEnd - viewSpan, viewEnd]. Mouse wheel zooms around the
// cursor, dragging pans; "following" keeps the right edge at now.

const MIN_SPAN = 5 * 60;          // 5 min
const MAX_SPAN = 14 * 86400;      // 14 days (what the Pi keeps)
let viewSpan = 3600;
let viewEnd = Date.now() / 1000;
let following = true;
let historyPoints = [];
let loadTimer = null;
let loadSeq = 0;

function viewStart() { return viewEnd - viewSpan; }

function setRangeButtons() {
  document.querySelectorAll("#range-toggle button").forEach(b =>
    b.classList.toggle("active", following && +b.dataset.seconds === Math.round(viewSpan)));
  $("btn-live").classList.toggle("hidden", following);
  const fmt = t => {
    const d = new Date(t * 1000);
    const sameDay = d.toDateString() === new Date().toDateString();
    return (sameDay ? "" : d.toLocaleDateString([], { day: "numeric", month: "short" }) + " ") +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  };
  $("graph-range").textContent = `${fmt(viewStart())} – ${following ? "now" : fmt(viewEnd)}`;
}

function changeView(span, end, follow) {
  const now = Date.now() / 1000;
  viewSpan = Math.min(MAX_SPAN, Math.max(MIN_SPAN, span));
  viewEnd = Math.min(now, Math.max(now - MAX_SPAN + viewSpan, end));
  following = follow || viewEnd >= now - viewSpan * 0.01;
  if (following) viewEnd = now;
  setRangeButtons();
  drawGraphs();
  clearTimeout(loadTimer);
  loadTimer = setTimeout(loadHistory, 120);
}

document.querySelectorAll("#range-toggle button").forEach(btn => {
  btn.onclick = () => changeView(+btn.dataset.seconds, Date.now() / 1000, true);
});
$("btn-live").onclick = () => changeView(viewSpan, Date.now() / 1000, true);

async function loadHistory() {
  const seq = ++loadSeq;
  if (following) viewEnd = Date.now() / 1000;
  const w = $("graph-level").clientWidth || 600;
  try {
    const pts = await api(`/api/history?start=${viewStart().toFixed(0)}&end=${viewEnd.toFixed(0)}&points=${Math.round(w / 2)}`);
    if (seq !== loadSeq) return; // a newer request is on its way
    historyPoints = pts;
    setRangeButtons();
    drawGraphs();
  } catch (e) {}
}

const PANELS = [
  { svg: "graph-level", key: "level", color: "var(--series-1)", log: true,
    ymax: pts => 10 ** Math.max(2, Math.ceil(Math.log10(Math.max(1, ...pts.map(p => p.level ?? 0))))) },
  { svg: "graph-bright", key: "brightness", color: "var(--series-2)", ymax: () => 100 },
];
const PAD = { l: 6, r: 44, t: 6, b: 16 };

function drawGraphs() {
  for (const p of PANELS) drawPanel(p);
}

function timeLabel(t) {
  const d = new Date(t * 1000);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (viewSpan > 86400 * 1.5) return d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " + hm;
  return hm;
}

function drawPanel(p) {
  const svg = $(p.svg);
  const W = svg.clientWidth || 600, H = svg.clientHeight || 92;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = "";
  const t0 = viewStart();
  const pts = historyPoints.filter(q => q[p.key] != null && q.t >= t0 - viewSpan * 0.02 && q.t <= viewEnd + viewSpan * 0.02);
  const plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
  const ns = "http://www.w3.org/2000/svg";
  const add = (tag, attrs, text) => {
    const el = document.createElementNS(ns, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    if (text) el.textContent = text;
    svg.appendChild(el);
    return el;
  };

  const ymax = p.ymax(pts.length ? pts : [{ [p.key]: 0 }]);
  const X = t => PAD.l + (t - t0) / viewSpan * plotW;
  // log panels run from 0.01 lux to ymax; linear panels from 0 to ymax
  const LOG_MIN = -2;
  const yfrac = v => p.log
    ? (Math.log10(Math.max(v, 0.01)) - LOG_MIN) / (Math.log10(ymax) - LOG_MIN)
    : v / ymax;
  const Y = v => PAD.t + plotH - yfrac(v) * plotH;
  const tick = f => p.log
    ? String(+(10 ** (LOG_MIN + f * (Math.log10(ymax) - LOG_MIN))).toPrecision(1))
    : String(Math.round(ymax * f));

  // recessive grid: baseline + two hairlines with muted tick labels
  for (const frac of [0, 0.5, 1]) {
    const y = PAD.t + plotH * (1 - frac);
    add("line", { x1: PAD.l, x2: PAD.l + plotW, y1: y, y2: y,
      stroke: frac === 0 ? "#383835" : "#2c2c2a", "stroke-width": 1 });
    if (frac > 0) add("text", { x: W - PAD.r + 6, y: y + 4, fill: "#898781",
      "font-size": 10.5, "font-family": "system-ui, sans-serif" }, tick(frac));
  }
  // time ticks
  for (const frac of [0.02, 0.5, 0.98]) {
    const t = t0 + viewSpan * frac;
    add("text", { x: X(t), y: H - 3, fill: "#898781", "font-size": 10.5,
      "text-anchor": frac > 0.6 ? "end" : frac < 0.4 ? "start" : "middle",
      "font-family": "system-ui, sans-serif" }, timeLabel(t));
  }

  const clip = add("clipPath", { id: "clip-" + p.svg });
  clip.appendChild(svg.lastChild.ownerDocument.createElementNS(ns, "rect"));
  for (const [k, v] of Object.entries({ x: PAD.l, y: 0, width: plotW, height: H }))
    clip.firstChild.setAttribute(k, v);

  if (pts.length > 1) {
    // break the line where there is a gap (Pi was off)
    const gap = Math.max(180, viewSpan / 100);
    const path = pts.map((q, i) =>
      `${i && q.t - pts[i - 1].t <= gap ? "L" : "M"}${X(q.t).toFixed(1)},${Y(q[p.key]).toFixed(1)}`).join("");
    add("path", { d: path, fill: "none", stroke: p.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round", "clip-path": `url(#clip-${p.svg})` });
    if (following) {
      const last = pts[pts.length - 1];
      add("text", { x: Math.min(X(last.t) + 5, W - 2), y: Math.max(PAD.t + 22, Y(last[p.key]) + 4),
        fill: "#c3c2b7", "font-size": 11, "font-family": "system-ui, sans-serif" },
        p.key === "brightness" ? Math.round(last[p.key]) + "%"
          : last[p.key] >= 1000 ? (last[p.key] / 1000).toFixed(1) + "k"
          : last[p.key] >= 10 ? String(Math.round(last[p.key])) : last[p.key].toFixed(1));
    }
  } else {
    add("text", { x: W / 2, y: H / 2, fill: "#898781", "font-size": 12,
      "text-anchor": "middle", "font-family": "system-ui, sans-serif" },
      following && viewSpan <= 3600 ? "collecting data…" : "no data in this range");
  }
  add("line", { class: "crosshair", x1: -10, x2: -10, y1: PAD.t, y2: PAD.t + plotH,
    stroke: "#898781", "stroke-width": 1, "stroke-dasharray": "3,3" });
}

// --- hover: shared crosshair + tooltip ---
const graphsBox = $("graphs");
function plotGeom() {
  const rect = $("graph-level").getBoundingClientRect();
  return { rect, plotW: rect.width - PAD.l - PAD.r };
}
function showTooltip(ev) {
  if (!historyPoints.length) return;
  const { rect, plotW } = plotGeom();
  const frac = Math.min(1, Math.max(0, (ev.clientX - rect.left - PAD.l) / plotW));
  const tTarget = viewStart() + frac * viewSpan;
  let best = historyPoints[0];
  for (const q of historyPoints)
    if (Math.abs(q.t - tTarget) < Math.abs(best.t - tTarget)) best = q;
  for (const p of PANELS) {
    const svg = $(p.svg);
    const px = PAD.l + (best.t - viewStart()) / viewSpan * (svg.clientWidth - PAD.l - PAD.r);
    const ch = svg.querySelector(".crosshair");
    if (ch) { ch.setAttribute("x1", px); ch.setAttribute("x2", px); }
  }
  const d = new Date(best.t * 1000);
  const tt = $("graph-tooltip");
  tt.innerHTML =
    `<div class="tt-time">${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${d.toLocaleTimeString()}</div>` +
    `<div class="row"><span class="chipdot" style="background:var(--series-1)"></span>${best.level ?? "–"} lux</div>` +
    `<div class="row"><span class="chipdot" style="background:var(--series-2)"></span>LED ${Math.round(best.brightness)}%</div>`;
  tt.classList.remove("hidden");
  const boxRect = graphsBox.getBoundingClientRect();
  let left = ev.clientX - boxRect.left + 14;
  if (left + 140 > boxRect.width) left -= 160;
  tt.style.left = left + "px";
  tt.style.top = (ev.clientY - boxRect.top + 10) + "px";
}
function hideTooltip() {
  $("graph-tooltip").classList.add("hidden");
  document.querySelectorAll(".crosshair").forEach(ch => {
    ch.setAttribute("x1", -10); ch.setAttribute("x2", -10);
  });
}

// --- zoom (wheel) ---
graphsBox.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const { rect, plotW } = plotGeom();
  const frac = Math.min(1, Math.max(0, (ev.clientX - rect.left - PAD.l) / plotW));
  const anchor = viewStart() + frac * viewSpan;
  const factor = Math.pow(1.0015, ev.deltaY);       // smooth for trackpads too
  const span = Math.min(MAX_SPAN, Math.max(MIN_SPAN, viewSpan * factor));
  const end = anchor + (1 - frac) * span;
  changeView(span, end, following && frac > 0.9);
}, { passive: false });

// --- pan (drag) and pinch on touch ---
const pointers = new Map();
let drag = null;
graphsBox.addEventListener("pointerdown", (ev) => {
  pointers.set(ev.pointerId, ev.clientX);
  graphsBox.setPointerCapture(ev.pointerId);
  drag = { x: ev.clientX, end: viewEnd, span: viewSpan,
           pinch: pointers.size === 2 ? Math.abs([...pointers.values()][0] - [...pointers.values()][1]) : null };
  graphsBox.classList.add("dragging");
  hideTooltip();
});
graphsBox.addEventListener("pointermove", (ev) => {
  if (!drag) { if (ev.pointerType === "mouse") showTooltip(ev); return; }
  pointers.set(ev.pointerId, ev.clientX);
  const { plotW } = plotGeom();
  if (pointers.size === 2 && drag.pinch) {
    const [a, b] = [...pointers.values()];
    const dist = Math.max(10, Math.abs(a - b));
    changeView(drag.span * drag.pinch / dist, drag.end, false);
    return;
  }
  const dt = (ev.clientX - drag.x) / plotW * drag.span;
  changeView(drag.span, drag.end - dt, false);
});
function endDrag(ev) {
  pointers.delete(ev.pointerId);
  if (pointers.size === 0) { drag = null; graphsBox.classList.remove("dragging"); }
}
graphsBox.addEventListener("pointerup", endDrag);
graphsBox.addEventListener("pointercancel", endDrag);
graphsBox.addEventListener("mouseleave", hideTooltip);
graphsBox.addEventListener("dblclick", () => changeView(viewSpan, Date.now() / 1000, true));

// ---------- gallery ----------

function fmtDate(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

async function loadGallery() {
  try {
    const g = await api("/api/gallery");
    $("gallery-disk").textContent =
      `${fmtBytes(g.disk.free)} free of ${fmtBytes(g.disk.total)} (${g.disk.used_percent}% used)`;
    const grid = $("gallery");
    grid.innerHTML = "";
    if (!g.items.length) {
      grid.innerHTML = '<span class="muted">No captures yet — take a snapshot or record a clip.</span>';
      return;
    }
    for (const item of g.items) {
      const div = document.createElement("div");
      div.className = "gitem";
      const dur = item.duration ? ` · ${item.duration.toFixed(0)}s` : "";
      div.innerHTML =
        `<div class="gthumb"><img loading="lazy" src="/thumb/${encodeURIComponent(item.name)}" alt="${item.name}">` +
        (item.type === "video" ? '<span class="badge-video">▶ video</span>' : "") + `</div>` +
        `<div class="gmeta">${fmtDate(item.mtime)}<span class="muted">${fmtBytes(item.size)}${dur}</span></div>` +
        `<div class="gactions">` +
        `<a href="/media/${encodeURIComponent(item.name)}?download=1">download</a>` +
        `<button class="danger">delete</button></div>`;
      div.querySelector("img").onclick = () => openModal(item);
      div.querySelector(".danger").onclick = async () => {
        if (!confirm(`Delete ${item.name}?`)) return;
        try { await api(`/api/media/${encodeURIComponent(item.name)}`, "DELETE"); loadGallery(); }
        catch (e) { toast("Error: " + e.message); }
      };
      grid.appendChild(div);
    }
  } catch (e) {
    $("gallery").innerHTML = '<span class="muted">Failed to load gallery.</span>';
  }
}

function openModal(item) {
  const img = $("modal-img");
  img.src = item.type === "video"
    ? `/replay/${encodeURIComponent(item.name)}`
    : `/media/${encodeURIComponent(item.name)}`;
  $("modal-caption").textContent = item.name +
    (item.type === "video" ? " (replay)" : "");
  $("modal").classList.remove("hidden");
}
$("modal").onclick = () => {
  $("modal").classList.add("hidden");
  $("modal-img").src = ""; // stop the replay stream
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape") $("modal").onclick();
});

// ---------- settings ----------

const settingsForm = $("settings-form");

async function loadSettings() {
  try {
    const cfg = await api("/api/config");
    for (const input of settingsForm.querySelectorAll("input"))
      if (cfg[input.name] !== undefined) input.value = cfg[input.name];
  } catch (e) {}
}

settingsForm.onsubmit = async (ev) => {
  ev.preventDefault();
  const body = {};
  for (const input of settingsForm.querySelectorAll("input"))
    body[input.name] = input.value;
  try {
    const r = await api("/api/config", "POST", body);
    $("settings-msg").textContent = "saved ✓";
    setTimeout(() => $("settings-msg").textContent = "", 2000);
  } catch (e) {
    const errs = e.data && e.data.errors;
    $("settings-msg").textContent = errs
      ? Object.entries(errs).map(([k, v]) => `${k}: ${v}`).join("; ")
      : "error: " + e.message;
  }
};

// ---------- boot ----------

window.addEventListener("resize", drawGraphs);
new ResizeObserver(drawGraphs).observe($("graphs"));
pollStatus();
loadHistory();
loadGallery();
loadSettings();
setInterval(pollStatus, 3000);
setInterval(() => { if (following) loadHistory(); }, 10000);
setRangeButtons();
