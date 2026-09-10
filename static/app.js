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
    updateMotion(s.motion);
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

// ---------- system power ----------

$("btn-reboot").onclick = async () => {
  if (!confirm("Restart the Pi now? The camera stream will drop and come back in ~30–60 s.")) return;
  try {
    const r = await api("/api/system/reboot", "POST");
    toast(r.mock ? "Reboot (mock — nothing happens in dev)" : "Rebooting… the page will reconnect shortly.");
  } catch (e) { toast("Error: " + e.message); }
};

$("btn-shutdown").onclick = async () => {
  if (!confirm("Shut down the Pi now? It will power off and you'll have to power-cycle it by hand to bring it back.")) return;
  try {
    const r = await api("/api/system/shutdown", "POST");
    toast(r.mock ? "Shutdown (mock — nothing happens in dev)" : "Shutting down… you can close this tab.");
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

// ---------- motion detection ----------

const motionSens = $("motion-sensitivity");
let motionSensBusy = false;

function updateMotion(m) {
  if (!m) return;
  document.querySelectorAll("#motion-toggle button").forEach(b =>
    b.classList.toggle("active", (b.dataset.on === "1") === m.enabled));

  // meter: activity fill + threshold marker, scaled so the threshold sits mid-ish
  const scale = Math.max(0.05, m.threshold * 3);
  const pct = v => Math.min(100, (v / scale) * 100);
  const bar = $("motion-bar");
  bar.style.width = pct(m.activity).toFixed(0) + "%";
  bar.classList.toggle("hot", m.activity > m.threshold);
  $("motion-thresh").style.left = pct(m.threshold).toFixed(0) + "%";

  $("motion-status").textContent =
    !m.available ? "unavailable — install Pillow on the Pi"
    : !m.enabled ? "off"
    : m.recording ? "motion detected — recording"
    : "armed";

  if (!motionSensBusy && document.activeElement !== motionSens)
    motionSens.value = m.sensitivity;
}

document.querySelectorAll("#motion-toggle button").forEach(btn => {
  btn.onclick = async () => {
    try { updateMotion(await api("/api/motion", "POST", { enabled: btn.dataset.on === "1" })); }
    catch (e) { toast("Error: " + e.message); }
  };
});

let motionSensTimer = null;
motionSens.addEventListener("input", () => {
  motionSensBusy = true;
  if (!motionSensTimer) {
    motionSensTimer = setTimeout(async () => {
      motionSensTimer = null;
      try { await api("/api/motion", "POST", { sensitivity: +motionSens.value }); } catch (e) {}
    }, 200);
  }
});
motionSens.addEventListener("change", () => { motionSensBusy = false; });

// ---------- sensor history graphs ----------

let historySeconds = 3600;
let historyPoints = [];

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
    drawGraphs();
  } catch (e) {}
}

const PANELS = [
  { svg: "graph-level", key: "level", color: "var(--series-1)", ymax: pts => Math.max(1000, ...pts.map(p => p.level ?? 0)) * 1.08 },
  { svg: "graph-bright", key: "brightness", color: "var(--series-2)", ymax: () => 100 },
];
const PAD = { l: 6, r: 44, t: 6, b: 16 };

function drawGraphs() {
  for (const p of PANELS) drawPanel(p);
}

function drawPanel(p) {
  const svg = $(p.svg);
  const W = svg.clientWidth || 600, H = svg.clientHeight || 92;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = "";
  const pts = historyPoints.filter(q => q[p.key] != null);
  const plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
  const ns = "http://www.w3.org/2000/svg";
  const add = (tag, attrs, text) => {
    const el = document.createElementNS(ns, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    if (text) el.textContent = text;
    svg.appendChild(el);
    return el;
  };

  const now = Date.now() / 1000;
  const t0 = now - historySeconds;
  const ymax = p.ymax(pts.length ? pts : [{ [p.key]: 0 }]);
  const X = t => PAD.l + (t - t0) / historySeconds * plotW;
  const Y = v => PAD.t + plotH - (v / ymax) * plotH;

  // recessive grid: baseline + two hairlines with muted tick labels
  for (const frac of [0, 0.5, 1]) {
    const y = PAD.t + plotH * (1 - frac);
    add("line", { x1: PAD.l, x2: PAD.l + plotW, y1: y, y2: y,
      stroke: frac === 0 ? "#383835" : "#2c2c2a", "stroke-width": 1 });
    if (frac > 0) add("text", { x: W - PAD.r + 6, y: y + 4, fill: "#898781",
      "font-size": 10.5, "font-family": "system-ui, sans-serif" },
      String(Math.round(ymax * frac)));
  }
  // time ticks
  for (const frac of [0.02, 0.5, 0.98]) {
    const t = t0 + historySeconds * frac;
    const d = new Date(t * 1000);
    const label = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    add("text", { x: X(t), y: H - 3, fill: "#898781", "font-size": 10.5,
      "text-anchor": frac > 0.6 ? "end" : frac < 0.4 ? "start" : "middle",
      "font-family": "system-ui, sans-serif" }, label);
  }

  if (pts.length > 1) {
    const path = pts.map((q, i) =>
      `${i ? "L" : "M"}${X(q.t).toFixed(1)},${Y(q[p.key]).toFixed(1)}`).join("");
    add("path", { d: path, fill: "none", stroke: p.color, "stroke-width": 2,
      "stroke-linejoin": "round", "stroke-linecap": "round" });
    // direct label: latest value at the line's end
    const last = pts[pts.length - 1];
    add("text", { x: Math.min(X(last.t) + 5, W - 2), y: Y(last[p.key]) + 4,
      fill: "#c3c2b7", "font-size": 11, "font-family": "system-ui, sans-serif" },
      p.key === "brightness" ? Math.round(last[p.key]) + "%" : String(last[p.key]));
  } else {
    add("text", { x: W / 2, y: H / 2, fill: "#898781", "font-size": 12,
      "text-anchor": "middle", "font-family": "system-ui, sans-serif" },
      "collecting data…");
  }
  // crosshair placeholder (positioned on hover)
  add("line", { class: "crosshair", x1: -10, x2: -10, y1: PAD.t, y2: PAD.t + plotH,
    stroke: "#898781", "stroke-width": 1, "stroke-dasharray": "3,3" });
}

// shared crosshair + tooltip across both panels
const graphsBox = $("graphs");
graphsBox.addEventListener("mousemove", (ev) => {
  if (!historyPoints.length) return;
  const svg0 = $("graph-level");
  const rect = svg0.getBoundingClientRect();
  const x = ev.clientX - rect.left;
  const plotW = rect.width - PAD.l - PAD.r;
  const frac = Math.min(1, Math.max(0, (x - PAD.l) / plotW));
  const now = Date.now() / 1000;
  const tTarget = now - historySeconds + frac * historySeconds;
  let best = historyPoints[0];
  for (const q of historyPoints)
    if (Math.abs(q.t - tTarget) < Math.abs(best.t - tTarget)) best = q;

  for (const p of PANELS) {
    const svg = $(p.svg);
    const w = svg.clientWidth;
    const px = PAD.l + (best.t - (now - historySeconds)) / historySeconds * (w - PAD.l - PAD.r);
    const ch = svg.querySelector(".crosshair");
    if (ch) { ch.setAttribute("x1", px); ch.setAttribute("x2", px); }
  }
  const d = new Date(best.t * 1000);
  const tt = $("graph-tooltip");
  tt.innerHTML =
    `<div class="tt-time">${d.toLocaleTimeString()}</div>` +
    `<div class="row"><span class="chipdot" style="background:var(--series-1)"></span>level ${best.level}</div>` +
    `<div class="row"><span class="chipdot" style="background:var(--series-2)"></span>LED ${Math.round(best.brightness)}%</div>`;
  tt.classList.remove("hidden");
  const boxRect = graphsBox.getBoundingClientRect();
  let left = ev.clientX - boxRect.left + 14;
  if (left + 130 > boxRect.width) left -= 150;
  tt.style.left = left + "px";
  tt.style.top = (ev.clientY - boxRect.top + 10) + "px";
});
graphsBox.addEventListener("mouseleave", () => {
  $("graph-tooltip").classList.add("hidden");
  document.querySelectorAll(".crosshair").forEach(ch => {
    ch.setAttribute("x1", -10); ch.setAttribute("x2", -10);
  });
});

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
      const badge = item.processing ? '<span class="badge-video">converting…</span>'
        : item.type === "mp4" ? '<span class="badge-video">▶ mp4</span>'
        : item.type === "mjpeg" ? '<span class="badge-video">▶ mjpeg</span>'
        : "";
      const convertBtn = item.type === "mjpeg" && !item.processing
        ? '<button class="convert">convert to mp4</button>' : "";
      div.innerHTML =
        `<div class="gthumb"><img loading="lazy" src="/thumb/${encodeURIComponent(item.name)}" alt="${item.name}">` +
        badge + `</div>` +
        `<div class="gmeta">${fmtDate(item.mtime)}<span class="muted">${fmtBytes(item.size)}${dur}</span></div>` +
        `<div class="gactions">` +
        `<a href="/media/${encodeURIComponent(item.name)}?download=1">download</a>` +
        convertBtn +
        `<button class="danger">delete</button></div>`;
      if (item.processing) div.classList.add("processing");
      else div.querySelector("img").onclick = () => openModal(item);
      const cb = div.querySelector(".convert");
      if (cb) cb.onclick = async () => {
        try {
          await api(`/api/media/${encodeURIComponent(item.name)}/convert`, "POST");
          toast(`Converting ${item.name}…`);
          loadGallery();
        } catch (e) { toast("Error: " + e.message); }
      };
      div.querySelector(".danger").onclick = async () => {
        if (!confirm(`Delete ${item.name}?`)) return;
        try { await api(`/api/media/${encodeURIComponent(item.name)}`, "DELETE"); loadGallery(); }
        catch (e) { toast("Error: " + e.message); }
      };
      grid.appendChild(div);
    }
    ensureGalleryPolling(g.items);
  } catch (e) {
    $("gallery").innerHTML = '<span class="muted">Failed to load gallery.</span>';
  }
}

let galleryPollTimer = null;
function ensureGalleryPolling(items) {
  const anyProcessing = items.some(i => i.processing);
  if (anyProcessing && !galleryPollTimer) {
    galleryPollTimer = setInterval(loadGallery, 3000);
  } else if (!anyProcessing && galleryPollTimer) {
    clearInterval(galleryPollTimer);
    galleryPollTimer = null;
  }
}

function openModal(item) {
  const img = $("modal-img"), vid = $("modal-video");
  if (item.type === "mp4") {
    img.classList.add("hidden");
    vid.classList.remove("hidden");
    vid.src = `/media/${encodeURIComponent(item.name)}`;
    vid.play().catch(() => {});
    $("modal-caption").textContent = item.name;
  } else {
    vid.classList.add("hidden"); vid.pause?.(); vid.removeAttribute("src");
    img.classList.remove("hidden");
    img.src = item.type === "mjpeg"
      ? `/replay/${encodeURIComponent(item.name)}`
      : `/media/${encodeURIComponent(item.name)}`;
    $("modal-caption").textContent = item.name + (item.type === "mjpeg" ? " (replay)" : "");
  }
  $("modal").classList.remove("hidden");
}
$("modal").onclick = () => {
  $("modal").classList.add("hidden");
  $("modal-img").src = "";          // stop the replay stream
  const vid = $("modal-video");
  vid.pause?.(); vid.removeAttribute("src"); vid.load?.();
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape") $("modal").onclick();
});

// ---------- settings ----------

const settingsForm = $("settings-form");

async function loadSettings() {
  try {
    const cfg = await api("/api/config");
    for (const el of settingsForm.querySelectorAll("input, select")) {
      if (cfg[el.name] === undefined) continue;
      if (el.type === "checkbox") el.checked = !!cfg[el.name];
      else el.value = cfg[el.name];
    }
  } catch (e) {}
}

settingsForm.onsubmit = async (ev) => {
  ev.preventDefault();
  const body = {};
  for (const el of settingsForm.querySelectorAll("input, select"))
    body[el.name] = el.type === "checkbox" ? (el.checked ? 1 : 0) : el.value;
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
pollStatus();
loadHistory();
loadGallery();
loadSettings();
setInterval(pollStatus, 3000);
setInterval(loadHistory, 10000);
