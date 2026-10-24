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

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
function fmtBytes(b) {
  if (b >= 1 << 30) return (b / (1 << 30)).toFixed(1) + " GB";
  if (b >= 1 << 20) return (b / (1 << 20)).toFixed(1) + " MB";
  return (b / 1024).toFixed(0) + " KB";
}
function fmtLux(v) {
  if (v == null) return "–";
  return v < 10 ? v.toFixed(1) : v < 1000 ? Math.round(v).toString() : (v / 1000).toFixed(1) + "k";
}

// ---------- tabs ----------

const tabButtons = document.querySelectorAll("#tabs button");
function showView(name) {
  tabButtons.forEach(b => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach(v =>
    v.classList.toggle("active", v.id === "view-" + name));
  if (name === "live") drawGraph();
  if (name === "gallery") loadGallery();
  history.replaceState(null, "", "#" + name);
}
tabButtons.forEach(b => b.onclick = () => showView(b.dataset.view));

// ---------- status bar ----------

let recStartedLocal = null;

async function pollStatus() {
  try {
    const s = await api("/api/status");
    const temp = $("chip-temp");
    temp.textContent = `\u{1F321}️ ${s.cpu_temp != null ? s.cpu_temp + " °C" : "n/a"}`;
    temp.classList.toggle("warn", s.cpu_temp != null && s.cpu_temp >= 75);
    $("chip-wifi").textContent = `\u{1F4F6} ${s.wifi_dbm != null ? s.wifi_dbm + " dBm" : "n/a"}`;
    $("chip-disk").textContent = `\u{1F4BE} ${fmtBytes(s.disk.free)}`;
    $("chip-uptime").textContent = `⏳ ${fmtUptime(s.uptime)}`;
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
  btn.textContent = on ? "⏹" : "⏺";
  btn.title = on ? "Stop recording" : "Start recording";
  recStartedLocal = on ? Date.now() - (rec.duration || 0) * 1000 : null;
}

setInterval(() => {
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
      toast(`Saved clip (${r.duration}s)`);
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
    await api("/api/snapshot", "POST");
    toast("Snapshot saved");
    loadGallery();
  } catch (e) { toast("Error: " + e.message); }
};

// ---------- lights ----------

const slider = $("brightness-slider");
let sliderBusy = false;
let cfg = {};

function explainLed(led) {
  const b = Math.round(led.brightness);
  if (led.mode === "manual") return `Manual: LEDs fixed at ${b} %. Light sensor still logging.`;
  if (led.level == null) return "Waiting for the light sensor…";
  if (cfg.lux_off != null && led.level >= cfg.lux_off && b === 0)
    return `Daylight: ${fmtLux(led.level)} lux is above ${cfg.lux_off}, LEDs off.`;
  if (cfg.lux_full != null && led.level <= cfg.lux_full)
    return `Dark: ${fmtLux(led.level)} lux, LEDs at max (${b} %).`;
  return `Dusk: ${fmtLux(led.level)} lux → LEDs at ${b} %.`;
}

function updateLed(led) {
  document.querySelectorAll("#mode-toggle button").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === led.mode));
  const b = Math.round(led.brightness);
  $("light-level").textContent = fmtLux(led.level);
  $("led-brightness").textContent = b + "%";
  $("led-explain").textContent = explainLed(led);
  const badge = $("ir-badge");
  badge.textContent = b > 0 ? `\u{1F526} IR ${b} %` : "☀️ Daylight";
  badge.classList.toggle("on", b > 0);
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

// ---------- history graph (lux on a log scale + LED % as an area) ----------

let historySeconds = 3600;
let historyPoints = [];
const PAD = { l: 4, r: 40, t: 8, b: 18 };
const LOG_MIN = -2; // 0.01 lux

document.querySelectorAll("#range-toggle button").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll("#range-toggle button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    historySeconds = +btn.dataset.seconds;
    loadHistory();
  };
});

async function loadHistory() {
  try {
    historyPoints = await api(`/api/history?seconds=${historySeconds}`);
    drawGraph();
  } catch (e) {}
}

let geom = null; // shared with the hover handler

function drawGraph() {
  const svg = $("graph");
  const W = svg.clientWidth, H = svg.clientHeight;
  if (!W || !H) return; // view hidden
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = "";
  const ns = "http://www.w3.org/2000/svg";
  const add = (tag, attrs, text) => {
    const el = document.createElementNS(ns, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    if (text) el.textContent = text;
    svg.appendChild(el);
    return el;
  };
  const font = { "font-size": 10.5, "font-family": "system-ui, sans-serif", fill: "#8a8880" };

  const pts = historyPoints;
  const plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
  const t0 = Date.now() / 1000 - historySeconds;
  const maxLux = Math.max(1, ...pts.map(p => p.level ?? 0));
  const logMax = Math.max(2, Math.ceil(Math.log10(maxLux)));
  const X = t => PAD.l + (t - t0) / historySeconds * plotW;
  const Ylux = v => PAD.t + plotH - (Math.log10(Math.max(v, 0.01)) - LOG_MIN) / (logMax - LOG_MIN) * plotH;
  const Ypct = v => PAD.t + plotH - v / 100 * plotH;
  geom = { X, t0, plotW };

  // grid: one line per decade of lux
  for (let e = LOG_MIN; e <= logMax; e++) {
    const y = Ylux(10 ** e);
    add("line", { x1: PAD.l, x2: PAD.l + plotW, y1: y, y2: y,
      stroke: e === LOG_MIN ? "#3a3a37" : "#262624", "stroke-width": 1 });
    if (e > LOG_MIN && (logMax - LOG_MIN <= 5 || e % 2 === 0))
      add("text", { ...font, x: W - PAD.r + 6, y: y + 4 },
        10 ** e >= 1000 ? (10 ** e / 1000) + "k" : String(10 ** e));
  }
  for (const f of [0, 0.5, 1]) {
    const t = t0 + historySeconds * f;
    const d = new Date(t * 1000);
    add("text", { ...font, x: X(t), y: H - 4,
      "text-anchor": f === 1 ? "end" : f === 0 ? "start" : "middle" },
      `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`);
  }

  if (pts.length < 2) {
    add("text", { ...font, x: W / 2, y: H / 2, "text-anchor": "middle", "font-size": 12 }, "collecting data…");
    return;
  }

  // LED % as a soft area behind the lux line
  const ledLine = pts.map((q, i) => `${i ? "L" : "M"}${X(q.t).toFixed(1)},${Ypct(q.brightness).toFixed(1)}`).join("");
  add("path", { d: `${ledLine}L${X(pts[pts.length - 1].t).toFixed(1)},${Ypct(0)}L${X(pts[0].t).toFixed(1)},${Ypct(0)}Z`,
    fill: "var(--series-2)", "fill-opacity": 0.18, stroke: "none" });
  add("path", { d: ledLine, fill: "none", stroke: "var(--series-2)", "stroke-width": 1.5, "stroke-opacity": 0.8 });

  const luxLine = pts.filter(q => q.level != null)
    .map((q, i) => `${i ? "L" : "M"}${X(q.t).toFixed(1)},${Ylux(q.level).toFixed(1)}`).join("");
  add("path", { d: luxLine, fill: "none", stroke: "var(--series-1)", "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round" });

  add("line", { id: "crosshair", x1: -10, x2: -10, y1: PAD.t, y2: PAD.t + plotH,
    stroke: "#8a8880", "stroke-width": 1, "stroke-dasharray": "3,3" });
}

const graphBox = $("graph-box");
graphBox.addEventListener("mousemove", (ev) => {
  if (!historyPoints.length || !geom) return;
  const rect = graphBox.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const tTarget = geom.t0 + Math.min(1, Math.max(0, (x - PAD.l) / geom.plotW)) * historySeconds;
  let best = historyPoints[0];
  for (const q of historyPoints)
    if (Math.abs(q.t - tTarget) < Math.abs(best.t - tTarget)) best = q;
  const ch = $("crosshair");
  if (ch) { ch.setAttribute("x1", geom.X(best.t)); ch.setAttribute("x2", geom.X(best.t)); }
  const tt = $("graph-tooltip");
  tt.innerHTML =
    `<div class="tt-time">${new Date(best.t * 1000).toLocaleTimeString()}</div>` +
    `<div class="row"><span class="chipdot" style="background:var(--series-1)"></span>${fmtLux(best.level)} lux</div>` +
    `<div class="row"><span class="chipdot" style="background:var(--series-2)"></span>LEDs ${Math.round(best.brightness)} %</div>`;
  tt.classList.remove("hidden");
  let left = x + 14;
  if (left + 120 > rect.width) left = x - 130;
  tt.style.left = left + "px";
  tt.style.top = Math.max(0, ev.clientY - rect.top - 30) + "px";
});
graphBox.addEventListener("mouseleave", () => {
  $("graph-tooltip").classList.add("hidden");
  const ch = $("crosshair");
  if (ch) { ch.setAttribute("x1", -10); ch.setAttribute("x2", -10); }
});

// ---------- gallery ----------

let galleryItems = [];
let galleryFilter = "all";

function fmtDate(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString([], { day: "numeric", month: "short" }) + ", " +
    d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

document.querySelectorAll("#gallery-filter button").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll("#gallery-filter button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    galleryFilter = btn.dataset.filter;
    renderGallery();
  };
});

async function loadGallery() {
  try {
    const g = await api("/api/gallery");
    galleryItems = g.items;
    $("gallery-count").textContent = g.items.length || "";
    $("gallery-disk").textContent =
      `${fmtBytes(g.disk.free)} free of ${fmtBytes(g.disk.total)}`;
    renderGallery();
  } catch (e) {
    $("gallery").innerHTML = '<div class="empty">Could not load the gallery.</div>';
  }
}

function renderGallery() {
  const grid = $("gallery");
  const items = galleryItems.filter(i => galleryFilter === "all" || i.type === galleryFilter);
  grid.innerHTML = "";
  if (!items.length) {
    grid.innerHTML = '<div class="empty">Nothing here yet. Take a snapshot or record a clip from the Live tab.</div>';
    return;
  }
  for (const item of items) {
    const name = encodeURIComponent(item.name);
    const div = document.createElement("div");
    div.className = "gitem";
    const dur = item.duration ? ` · ${Math.round(item.duration)} s` : "";
    div.innerHTML =
      `<div class="gthumb"><img loading="lazy" src="/thumb/${name}" alt="">` +
      (item.type === "video" ? `<span class="badge-video">▶ video${dur}</span>` : "") + `</div>` +
      `<div class="gfoot"><div class="gmeta">${fmtDate(item.mtime)}<span class="muted">${fmtBytes(item.size)}</span></div>` +
      `<a href="/media/${name}?download=1" title="Download">⬇️</a>` +
      `<button class="danger" title="Delete">\u{1F5D1}️</button></div>`;
    div.querySelector(".gthumb").onclick = () => openModal(item);
    div.querySelector(".danger").onclick = async () => {
      if (!confirm(`Delete ${item.name}?`)) return;
      try { await api(`/api/media/${name}`, "DELETE"); loadGallery(); }
      catch (e) { toast("Error: " + e.message); }
    };
    grid.appendChild(div);
  }
}

function openModal(item) {
  const name = encodeURIComponent(item.name);
  $("modal-img").src = item.type === "video" ? `/replay/${name}` : `/media/${name}`;
  $("modal-caption").textContent = fmtDate(item.mtime) + (item.type === "video" ? " (replay)" : "");
  $("modal").classList.remove("hidden");
}
$("modal").onclick = () => {
  $("modal").classList.add("hidden");
  $("modal-img").src = ""; // stops the replay stream
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape") $("modal").onclick();
});

// ---------- settings ----------

const settingsForm = $("settings-form");

async function loadSettings() {
  try {
    cfg = await api("/api/config");
    for (const input of settingsForm.querySelectorAll("input"))
      if (cfg[input.name] !== undefined) input.value = cfg[input.name];
  } catch (e) {}
}

settingsForm.onsubmit = async (ev) => {
  ev.preventDefault();
  const body = {};
  for (const input of settingsForm.querySelectorAll("input")) body[input.name] = input.value;
  try {
    const r = await api("/api/config", "POST", body);
    cfg = r.config;
    $("settings-msg").textContent = "Saved ✓";
    setTimeout(() => $("settings-msg").textContent = "", 2000);
  } catch (e) {
    const errs = e.data && e.data.errors;
    $("settings-msg").textContent = errs
      ? Object.entries(errs).map(([k, v]) => `${k}: ${v}`).join("; ")
      : "Error: " + e.message;
  }
};

// ---------- boot ----------

new ResizeObserver(drawGraph).observe(graphBox);
loadSettings().then(pollStatus);
loadHistory();
loadGallery();
setInterval(pollStatus, 3000);
setInterval(loadHistory, 10000);
const start = location.hash.slice(1);
if (["live", "gallery", "settings"].includes(start)) showView(start);
