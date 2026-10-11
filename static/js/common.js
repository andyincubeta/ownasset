/* Shared helpers: API, formatting, charts theme, modals, polling, clock, currency. */
"use strict";

/* ------------------------------------------------------------- fetch ---- */
async function api(path, opts = {}) {
  if (opts.body instanceof FormData) {
    // Let browser set multipart boundaries for file upload requests.
  } else if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_e) { /* empty body */ }
  if (!resp.ok) {
    const msg = (data && data.error) ? data.error : `HTTP ${resp.status}`;
    toast(msg, "error");
    throw new Error(msg);
  }
  return data;
}

/* ------------------------------------------------------------ format ---- */
function fmt(n, dp = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "\u2014";
  return Number(n).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}
function fmtQty(n) {
  if (n === null || n === undefined) return "\u2014";
  return Number(n).toLocaleString("en-US", { maximumFractionDigits: 6 });
}
function fmtPct(n, dp = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "\u2014";
  const s = Number(n) >= 0 ? "+" : "";
  return s + Number(n).toFixed(dp) + "%";
}
function pctClass(n) { return n > 0 ? "green" : (n < 0 ? "red" : "dim"); }
function valClass(n) { return n >= 0 ? "green" : "red"; }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ----------------------------------------------------- formula input ---- */
// Hand-written parser on purpose: user text must never reach eval/Function.
function evalAmount(str) {
  const s = String(str ?? "").replace(/[,\s]/g, "");
  if (!s || s.length > 100 || !/^[0-9.+\-*/()]+$/.test(s)) return NaN;
  let i = 0, depth = 0;
  const fail = () => { throw new Error("bad formula"); };
  const expr = () => {
    let v = term();
    while (s[i] === "+" || s[i] === "-") v = s[i++] === "+" ? v + term() : v - term();
    return v;
  };
  const term = () => {
    let v = factor();
    while (s[i] === "*" || s[i] === "/") v = s[i++] === "*" ? v * factor() : v / factor();
    return v;
  };
  const factor = () => {
    if (s[i] === "-") { i++; return -factor(); }
    if (s[i] === "+") { i++; return factor(); }
    if (s[i] === "(") {
      if (++depth > 20) fail();
      i++;
      const v = expr();
      if (s[i++] !== ")") fail();
      depth--;
      return v;
    }
    const m = /^(\d+\.?\d*|\.\d+)/.exec(s.slice(i));
    if (!m) fail();
    i += m[0].length;
    return parseFloat(m[0]);
  };
  try {
    const v = expr();
    // toPrecision trims float noise, e.g. (12*0.8-1)*20 -> 172 not 172.00000000000003
    return i === s.length && Number.isFinite(v) ? parseFloat(v.toPrecision(15)) : NaN;
  } catch (_e) {
    return NaN;
  }
}

/* Read an amount/formula input; blank -> 0, invalid -> throws (callers toast e.message). */
function readAmount(id) {
  const el = document.getElementById(id);
  const raw = el.value.trim();
  if (!raw) return 0;
  const v = evalAmount(raw);
  if (Number.isNaN(v) || v < 0) {
    el.classList.add("bad");
    throw new Error(Number.isNaN(v)
      ? `invalid amount: ${raw.slice(0, 40)}`
      : `amount cannot be negative: ${raw.slice(0, 40)}`);
  }
  return v;
}

function _formulaHint(el) {
  let hint = el.nextElementSibling;
  if (!hint || !hint.classList.contains("formula-hint")) {
    hint = document.createElement("span");
    hint.className = "formula-hint";
    el.after(hint);
  }
  return hint;
}

function _formulaPreview(el) {
  const raw = el.value.trim();
  const isFormula = /[+\-*/()]/.test(raw.replace(/^[-+]/, ""));
  const v = raw ? evalAmount(raw) : 0;
  el.classList.toggle("bad", Number.isNaN(v) || v < 0);
  _formulaHint(el).textContent = !raw ? "" : Number.isNaN(v) ? "invalid"
    : v < 0 ? "cannot be negative" : isFormula ? `= ${fmtQty(v)}` : "";
  return v;
}

function _formulaCommit(el) {
  const v = _formulaPreview(el);
  if (el.value.trim() && !Number.isNaN(v)) {
    el.value = String(v);
    _formulaHint(el).textContent = "";
  }
}

// Delegated so inputs inside dynamically built modals work too.
document.addEventListener("input", e => {
  if (e.target.matches?.("input[data-formula]")) _formulaPreview(e.target);
});
document.addEventListener("focusout", e => {
  if (e.target.matches?.("input[data-formula]")) _formulaCommit(e.target);
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.matches?.("input[data-formula]")) _formulaCommit(e.target);
});

function normalizeTicker(value) {
  return String(value ?? "").trim().toUpperCase();
}

function buildYahooQuoteUrl(ticker) {
  const t = normalizeTicker(ticker);
  if (!t) return "";
  return "https://finance.yahoo.com/quote/" + encodeURIComponent(t);
}

function isYahooEligibleAsset(asset) {
  if (!asset || !asset.is_yahoo_tracked) return false;
  const provider = String(asset.price_provider || "yahoo").trim().toLowerCase();
  return provider === "yahoo";
}

function yahooLinkHtml(ticker, enabled = true, provider = "Yahoo Finance") {
  const t = normalizeTicker(ticker);
  if (!enabled || !t) return "";
  const href = buildYahooQuoteUrl(t);
  if (!href) return "";
  const providerText = String(provider || "Yahoo Finance");
  const openText = `Open ${t} on ${providerText}`;
  return `<a class="ticker-link" href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="${esc(openText)}" aria-label="${esc(openText)}" onclick="event.stopPropagation()">↗</a>`;
}

function tickerWithYahooHtml(ticker, enabled = true) {
  const t = normalizeTicker(ticker);
  if (!t) return "";
  const link = yahooLinkHtml(t, enabled);
  return link
    ? `<span class="ticker-inline"><span>${esc(t)}</span>${link}</span>`
    : esc(t);
}

function displayUtcOffsetHours() {
  const raw = document.body?.dataset?.displayUtcOffset ?? "0";
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

function utcDateFromValue(value) {
  if (value instanceof Date) return new Date(value.getTime());
  if (typeof value === "number") return new Date(value * 1000);
  const s = String(value ?? "").trim();
  if (!s) return new Date(NaN);
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return new Date(`${s}T00:00:00Z`);
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s)) {
    return new Date(s.replace(" ", "T") + (s.endsWith("Z") ? "" : "Z"));
  }
  return new Date(s);
}

function shiftUtcDate(value) {
  const d = utcDateFromValue(value);
  d.setUTCHours(d.getUTCHours() + displayUtcOffsetHours());
  return d;
}

function formatDisplayDate(value) {
  return shiftUtcDate(value).toISOString().slice(0, 10);
}

function formatDisplayDateTime(value) {
  return shiftUtcDate(value).toISOString().slice(0, 19).replace("T", " ");
}

function formatDisplayDateTimeLocal(value) {
  return shiftUtcDate(value).toISOString().slice(0, 16);
}

function displayDateTimeLocalToUtcString(value) {
  const s = String(value ?? "").trim();
  if (!s) return "";
  const normalized = s.length === 16 ? s + ":00" : s;
  const d = new Date(normalized.replace(" ", "T") + "Z");
  if (Number.isNaN(d.getTime())) return s;
  d.setUTCHours(d.getUTCHours() - displayUtcOffsetHours());
  return d.toISOString().slice(0, 19).replace("T", " ");
}

/* ------------------------------------------------------------- toast ---- */
function toast(msg, kind = "") {
  const box = document.getElementById("toast");
  const el = document.createElement("div");
  el.className = "toast-item " + kind;
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/* ------------------------------------------------------------- modal ---- */
function openModal(title, bodyHtml, footHtml = "") {
  closeModal();
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.id = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-head"><span>${esc(title)}</span><span class="x" onclick="closeModal()">✕</span></div>
      <div class="modal-body">${bodyHtml}</div>
      ${footHtml ? `<div class="modal-foot">${footHtml}</div>` : ""}
    </div>`;
  overlay.addEventListener("mousedown", e => { if (e.target === overlay) closeModal(); });
  document.body.appendChild(overlay);
  return overlay;
}
function closeModal() {
  const el = document.getElementById("modal-overlay");
  if (el) el.remove();
}
function confirmDialog(msg) {
  return new Promise(resolve => {
    openModal("CONFIRM", `<div>${esc(msg)}</div>`,
      `<button id="cf-no">CANCEL</button><button id="cf-yes" class="danger">CONFIRM</button>`);
    document.getElementById("cf-yes").onclick = () => { closeModal(); resolve(true); };
    document.getElementById("cf-no").onclick = () => { closeModal(); resolve(false); };
  });
}
async function rebuildSnapshots(onDone) {
  if (!await confirmDialog("Rebuild all snapshots from transactions × price history? " +
      "This recomputes every daily row.")) return;
  const res = await api("/api/history/snapshots/rebuild", { method: "POST", body: {} });
  toast(`rebuilt ${res.rows} snapshot rows`, "ok");
  if (onDone) onDone();
}

/* --------------------------------------------------------- row menu ----- */
function rowMenu(anchorEl, items) {
  document.querySelectorAll(".menu-pop").forEach(m => m.remove());
  const pop = document.createElement("div");
  pop.className = "menu-pop";
  items.forEach(([label, fn]) => {
    const it = document.createElement("div");
    it.textContent = label;
    it.onclick = () => { pop.remove(); fn(); };
    pop.appendChild(it);
  });
  const r = anchorEl.getBoundingClientRect();
  pop.style.left = Math.max(4, r.right - 110 + window.scrollX) + "px";
  pop.style.top = (r.bottom + window.scrollY + 2) + "px";
  document.body.appendChild(pop);
  const closer = e => { if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener("mousedown", closer); } };
  setTimeout(() => document.addEventListener("mousedown", closer), 0);
}

/* ------------------------------------------------------------ drawer ---- */
let _drawerCloser = null;
function openDrawer(title, html) {
  const d = document.getElementById("drawer");
  d.innerHTML = `<div class="drawer-head"><span>${esc(title)}</span>
    <span class="clickable" onclick="closeDrawer()">✕</span></div>
    <div class="panel-body">${html}</div>`;
  d.classList.add("open");
  if (!_drawerCloser) {
    // ignore clicks in modals/menus spawned from the drawer
    _drawerCloser = e => {
      if (!d.contains(e.target) && !e.target.closest(".modal-overlay, .menu-pop")) closeDrawer();
    };
    setTimeout(() => document.addEventListener("mousedown", _drawerCloser), 0);
  }
}
function closeDrawer() {
  document.getElementById("drawer").classList.remove("open");
  if (_drawerCloser) { document.removeEventListener("mousedown", _drawerCloser); _drawerCloser = null; }
}

/* ------------------------------------------------------------ charts ---- */
/* user font scale (inline --font-scale on <html>) x screen base (--fs-base vs 12px) */
const FONT_SCALE = (() => {
  const css = getComputedStyle(document.documentElement);
  const v = parseFloat(css.getPropertyValue("--font-scale"));
  const base = parseFloat(css.getPropertyValue("--fs-base"));
  return (Number.isFinite(v) && v > 0 ? v : 1) * (Number.isFinite(base) && base > 0 ? base / 12 : 1);
})();
function chartFs(px) { return Math.round(px * FONT_SCALE); }

/* chart palette follows the page theme (data-theme on <html>) */
const UI_THEME = document.documentElement.dataset.theme === "light" ? "light" : "dark";
const CHART_C = UI_THEME === "light" ? {
  bg: "#ffffff", text: "#1c1c1c", dim: "#5f5f5f", axis: "#9a9a9a",
  split: "#e4e4e4", tipBg: "#ffffff", labelBg: "#ffffff", pieBorder: "#ffffff",
  neutral: "#1c1c1c",
} : {
  bg: "#0d0d0d", text: "#d8d8d8", dim: "#8a8a8a", axis: "#444",
  split: "#1c1c1c", tipBg: "#121212", labelBg: "#121212", pieBorder: "#000",
  neutral: "#ffffff",
};

const BBG_THEME = {
  animation: false,  // charts paint complete, no draw-in sweep
  backgroundColor: CHART_C.bg,
  textStyle: { color: CHART_C.text, fontFamily: "Consolas, monospace", fontSize: chartFs(11) },
  color: ["#ffb000", "#40c4ff", "#00c853", "#ff1744", "#e040fb", "#ffd740", "#69f0ae", "#ff6e40"],
  axisPointer: { lineStyle: { color: "#ffb000" } },
  categoryAxis: { axisLine: { lineStyle: { color: CHART_C.axis } }, splitLine: { show: false },
                  axisLabel: { color: CHART_C.dim, fontSize: chartFs(10) } },
  valueAxis: { axisLine: { lineStyle: { color: CHART_C.axis } },
               splitLine: { lineStyle: { color: CHART_C.split } },
               axisLabel: { color: CHART_C.dim, fontSize: chartFs(10) } },
  legend: { textStyle: { color: CHART_C.text, fontSize: chartFs(10) } },
  tooltip: { backgroundColor: CHART_C.tipBg, borderColor: "#ffb000",
             textStyle: { color: CHART_C.text, fontSize: chartFs(11) } },
};
if (window.echarts) echarts.registerTheme("bbg", BBG_THEME);

function mkChart(elOrId) {
  const el = typeof elOrId === "string" ? document.getElementById(elOrId) : elOrId;
  const chart = echarts.init(el, "bbg");
  window.addEventListener("resize", () => chart.resize());
  return chart;
}

/* TradingView-style interactions: wheel zoom (inside), area-select zoom +
   one-click reset + save-png (toolbox), optional fixed y range. */
function tvExtras(opt, cfg = {}) {
  opt.toolbox = {
    right: 6, top: 2, itemSize: 13, itemGap: 6,
    iconStyle: { borderColor: CHART_C.dim },
    emphasis: { iconStyle: { borderColor: "#ffb000" } },
    feature: {
      dataZoom: { title: { zoom: "AREA ZOOM", back: "UNDO ZOOM" } },
      restore: { title: "RESET" },
      saveAsImage: { title: "SAVE PNG", backgroundColor: CHART_C.bg },
    },
  };
  if (!opt.dataZoom) {
    opt.dataZoom = [{ type: "inside" }, { type: "slider", height: 16, bottom: 4 }];
  }
  // only touch the y range when the caller manages it (empty input = auto);
  // otherwise keep any min/max already set (e.g. sgAlignedAxes zero alignment)
  const y = Array.isArray(opt.yAxis) ? opt.yAxis[0] : opt.yAxis;
  if (y && ("yMin" in cfg || "yMax" in cfg)) {
    const num = v => (v !== "" && v !== null && v !== undefined && Number.isFinite(+v)) ? +v : undefined;
    y.min = num(cfg.yMin);
    y.max = num(cfg.yMax);
  }
  return opt;
}

/* live value pinned right of the line end; overlapping labels shift apart.
   pinChart: anchor the label at the chart's right edge so labels of series
   ending on different dates still align vertically. */
function withEndLabel(series, color, dp = 2, pinChart = null) {
  series.endLabel = {
    show: true, color, fontSize: chartFs(10), fontWeight: "bold",
    backgroundColor: CHART_C.labelBg, borderColor: color, borderWidth: 1,
    padding: [1, 4], distance: 6,
    formatter: p => {
      const v = Array.isArray(p.value) ? p.value[p.value.length - 1] : p.value;
      return (v === null || v === undefined) ? "" : fmt(v, dp);
    },
  };
  series.labelLayout = pinChart
    ? () => ({ x: pinChart.getWidth() - 4, align: "right", moveOverlap: "shiftY" })
    : { moveOverlap: "shiftY" };
  return series;
}

/* disposable-income deposit markers: small triangle + compact amount label */
function incomeMarkPoint(marks) {
  return {
    symbol: "triangle", symbolSize: chartFs(8), symbolOffset: [0, chartFs(6)],
    itemStyle: { color: "#40c4ff" },
    label: { show: true, position: "bottom", distance: 2, color: CHART_C.text,
             fontSize: chartFs(9), formatter: p => "+" + fmt(p.data.value, 0) },
    tooltip: { formatter: p => "DISPOSABLE INCOME +" + fmt(p.data.value, 2) },
    data: marks,
  };
}

function resizeChartForElement(chartEl) {
  if (!chartEl || !window.echarts) return;
  const inst = echarts.getInstanceByDom(chartEl);
  if (inst) inst.resize();
}

/* ------------------------------------- portfolio-vs-indices (TWR) chart -- */
/* Chart option for /api/performance/series data: growth-of-100 portfolio
   (deposits/withdrawals stripped) + benchmark lines, live tails dashed.
   Returns null when there is no portfolio history yet. */
const CHART_IDX_COLORS = ["#40c4ff", "#00c853", "#e040fb", "#ffd740", "#69f0ae", "#ff6e40"];

function idxCompareOption(chart, data, zoom, mode) {
  const pf = data.portfolio;
  if (!pf || !pf.dates.length) return null;
  const dateSet = new Set(pf.dates);
  if (pf.today) dateSet.add(pf.today.date);
  (data.indices || []).forEach(ix => {
    ix.dates.forEach(d => dateSet.add(d));
    if (ix.today) dateSet.add(ix.today.date);
  });
  const axisDates = [...dateSet].sort();
  // solid frozen line + dashed bridge from last frozen point to the live one
  const pair = (name, dates, values, today, color) => {
    const at = Object.fromEntries(dates.map((d, i) => [d, values[i]]));
    const solid = axisDates.map(d => (d in at ? at[d] : null));
    const dashed = axisDates.map(() => null);
    if (today && dates.length) {
      const lastIdx = axisDates.indexOf(dates[dates.length - 1]);
      const liveIdx = axisDates.indexOf(today.date);
      if (lastIdx >= 0) dashed[lastIdx] = values[values.length - 1];
      if (liveIdx >= 0) dashed[liveIdx] = today.value;
    }
    const base = { type: "line", connectNulls: true, showSymbol: false };
    const out = [withEndLabel({ ...base, name, data: solid,
      lineStyle: { color, width: name === "PORTFOLIO" ? 2 : 1.2 },
      itemStyle: { color } }, color, 2, chart)];
    if (today) {
      out.push({ ...base, name: `${name} (LIVE)`, data: dashed, showSymbol: true, symbolSize: 4,
        lineStyle: { color, type: "dashed", width: 1.5 }, itemStyle: { color },
        tooltip: { show: false } });
    }
    return out;
  };
  let series;
  if (mode === "candle" && pf.ohlc && pf.ohlc.length) {
    const barAt = Object.fromEntries(pf.dates.map((d, i) => [d, pf.ohlc[i]]));
    series = [{ type: "candlestick", name: "PORTFOLIO",
      data: axisDates.map(d => (d in barAt ? barAt[d] : null)),
      itemStyle: { color: "#00c853", color0: "#ff1744",
                   borderColor: "#00c853", borderColor0: "#ff1744" } }];
  } else {
    series = pair("PORTFOLIO", pf.dates, pf.values, pf.today, "#ffb000");
  }
  (data.indices || []).forEach((ix, i) => {
    series = series.concat(pair(ix.name.toUpperCase(), ix.dates, ix.values, ix.today,
      CHART_IDX_COLORS[i % CHART_IDX_COLORS.length]));
  });
  return {
    tooltip: { trigger: "axis", valueFormatter: v => (v == null ? "\u2014" : fmt(v)) },
    legend: { top: 2, left: 8 },
    grid: { left: 70, right: 96, top: 28, bottom: 40 },
    xAxis: { type: "category", data: axisDates.map(formatDisplayDate) },
    yAxis: { type: "value", scale: true },
    dataZoom: [{ type: "inside", ...zoom },
               { type: "slider", height: 16, bottom: 4, ...zoom }],
    series,
  };
}

/* benchmark multi-select for an index-compare chart, folded into a dropdown
   button (the index list will grow); selection saved under `prefsKey` in the
   page's chart prefs (default: S&P 500 only). Returns a getter for the
   currently selected keys. */
async function initIdxChecks(containerId, cls, page, prefsKey, onChange) {
  const list = await api("/api/performance/indices");
  const saved = chartPrefs.get(page)[prefsKey];
  const selected = new Set((Array.isArray(saved) ? saved : ["SP500"])
    .filter(k => list.some(ix => ix.key === k)));
  const box = document.getElementById(containerId);
  box.innerHTML = "";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "mini";
  const relabel = () => { btn.textContent = `INDICES (${selected.size}) \u25be`; };
  relabel();
  box.appendChild(btn);
  let pop = null;
  const close = () => { if (pop) { pop.remove(); pop = null; } };
  btn.addEventListener("click", () => {
    if (pop) { close(); return; }
    document.querySelectorAll(".menu-pop").forEach(m => m.remove());
    pop = document.createElement("div");
    pop.className = "menu-pop idx-pop";
    pop.innerHTML = list.map(ix =>
      `<label><input type="checkbox" class="${cls}" value="${ix.key}"
         ${selected.has(ix.key) ? "checked" : ""}> ${esc(ix.name.toUpperCase())}</label>`).join("");
    const r = btn.getBoundingClientRect();
    pop.style.left = Math.max(4, r.left + window.scrollX) + "px";
    pop.style.top = (r.bottom + window.scrollY + 2) + "px";
    document.body.appendChild(pop);
    pop.querySelectorAll("input").forEach(cb => cb.addEventListener("change", () => {
      if (cb.checked) selected.add(cb.value); else selected.delete(cb.value);
      chartPrefs.set(page, { [prefsKey]: [...selected] });
      relabel();
      onChange();
    }));
    const closer = e => {
      if (pop && !pop.contains(e.target) && e.target !== btn) {
        close();
        document.removeEventListener("mousedown", closer);
      }
    };
    setTimeout(() => document.addEventListener("mousedown", closer), 0);
  });
  return () => [...selected];
}


async function enterChartFullscreen(chartEl) {
  if (!chartEl) return;
  if (typeof chartEl.requestFullscreen !== "function") {
    toast("fullscreen not supported in this browser", "error");
    return;
  }
  if (document.fullscreenElement && document.fullscreenElement !== chartEl) {
    await document.exitFullscreen();
  }
  chartEl.classList.add("chart-fullscreen");
  try {
    await chartEl.requestFullscreen();
    resizeChartForElement(chartEl);
  } catch (_e) {
    chartEl.classList.remove("chart-fullscreen");
    toast("fullscreen request blocked", "error");
  }
}

async function toggleChartFullscreen(chartEl) {
  if (!chartEl) return;
  if (document.fullscreenElement === chartEl) {
    await document.exitFullscreen();
    return;
  }
  await enterChartFullscreen(chartEl);
}

/* generic fullscreen for non-chart elements (tables / whole panels) */
async function toggleElementFullscreen(el) {
  if (!el) return;
  if (document.fullscreenElement === el) {
    await document.exitFullscreen();
    return;
  }
  if (typeof el.requestFullscreen !== "function") {
    toast("fullscreen not supported in this browser", "error");
    return;
  }
  if (document.fullscreenElement) await document.exitFullscreen();
  el.classList.add("el-fullscreen");
  try {
    await el.requestFullscreen();
  } catch (_e) {
    el.classList.remove("el-fullscreen");
    toast("fullscreen request blocked", "error");
  }
}

document.addEventListener("click", e => {
  const btn = e.target.closest("[data-fullscreen-el]");
  if (!btn) return;
  const el = document.getElementById(btn.dataset.fullscreenEl);
  if (!el) {
    toast("element not found", "error");
    return;
  }
  toggleElementFullscreen(el);
});

document.addEventListener("click", e => {
  const btn = e.target.closest("[data-fullscreen-chart]");
  if (!btn) return;
  const chartId = btn.dataset.fullscreenChart;
  const chartEl = document.getElementById(chartId);
  if (!chartEl) {
    toast("chart not found", "error");
    return;
  }
  toggleChartFullscreen(chartEl);
});

document.addEventListener("fullscreenchange", () => {
  const fsEl = document.fullscreenElement;
  if (fsEl && fsEl.classList && fsEl.classList.contains("chart")) {
    resizeChartForElement(fsEl);
    return;
  }
  if (fsEl && fsEl.classList && fsEl.classList.contains("el-fullscreen")) return;
  document.querySelectorAll(".chart.chart-fullscreen").forEach(el => {
    el.classList.remove("chart-fullscreen");
    resizeChartForElement(el);
  });
  document.querySelectorAll(".el-fullscreen").forEach(el => el.classList.remove("el-fullscreen"));
});

/* ----------------------------------------------------------- polling ---- */
function poll(fn, seconds) {
  fn();
  const id = setInterval(() => { if (!document.hidden) fn(); }, seconds * 1000);
  return () => clearInterval(id);
}

/* ---------------------------------------------- per-chart view prefs ---- */
/* Server-persisted chart UI state (zoom window, mode, benchmark, ...):
   one `chart_prefs` settings key, namespaced per page. */
const chartPrefs = {
  data: null,
  _t: null,
  async load() {
    if (this.data !== null) return this.data;
    try {
      const s = await api("/api/settings");
      this.data = JSON.parse(s.chart_prefs || "{}") || {};
    } catch (_e) { this.data = {}; }
    return this.data;
  },
  get(page) { return (this.data && this.data[page]) || {}; },
  set(page, patch) {
    if (!this.data) this.data = {};
    this.data[page] = Object.assign({}, this.data[page], patch);
    clearTimeout(this._t);
    this._t = setTimeout(() => {
      api("/api/settings", { method: "POST",
           body: { chart_prefs: JSON.stringify(this.data) } }).catch(() => {});
    }, 600);
  },
};

/* persist user zoom (drag/wheel/slider) + toolbox RESET for one chart */
function bindZoomPersist(chart, page) {
  chart.on("datazoom", () => {
    const dz = chart.getOption().dataZoom;
    if (dz && dz.length) {
      chartPrefs.set(page, { zoom: { start: dz[0].start, end: dz[0].end } });
    }
  });
  chart.on("restore", () => chartPrefs.set(page, { zoom: null }));
}

/* restore a form control from prefs, then persist future changes */
function bindControlPersist(page, id, key, onRestore) {
  const el = document.getElementById(id);
  if (!el) return;
  const saved = chartPrefs.get(page)[key];
  if (saved !== undefined && saved !== null && String(saved) !== "") {
    el.value = saved;  // a select without a matching option stays unchanged
  }
  if (onRestore) onRestore(el);
  el.addEventListener("change", () => chartPrefs.set(page, { [key]: el.value }));
}

/* ------------------------------------------------------------- clock ---- */
function tickClock() {
  const el = document.getElementById("clock");
  if (el) el.textContent = formatDisplayDateTime(new Date());
}
tickClock();  // render immediately so the topbar never reflows on first tick
setInterval(tickClock, 1000);

/* ------------------------------------------------- currency selector ---- */
document.getElementById("display-ccy").addEventListener("change", async e => {
  await api("/api/settings", { method: "POST", body: { display_currency: e.target.value } });
  location.reload();
});
function displayCcy() { return document.getElementById("display-ccy").value; }

/* ------------------------------------------------ font size selector ---- */
document.getElementById("font-size-sel").addEventListener("change", async e => {
  await api("/api/settings", { method: "POST", body: { font_size: e.target.value } });
  location.reload();
});

/* --------------------------------------------- light/dark mode toggle --- */
document.getElementById("theme-toggle").addEventListener("click", async () => {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  await api("/api/settings", { method: "POST", body: { ui_theme: next } });
  location.reload();  // charts re-init with the matching palette
});

/* topbar wraps at large text sizes; keep dependent offsets in sync */
function syncTopbarHeight() {
  const tb = document.getElementById("topbar");
  if (tb) document.documentElement.style.setProperty("--topbar-h", tb.offsetHeight + "px");
}
window.addEventListener("resize", syncTopbarHeight);
syncTopbarHeight();

/* --------------------------------------------- responsive nav collapse -- */
/* When the links overflow the topbar, hide them behind the hamburger and
   show them as a dropdown instead of a horizontal scrollbar. */
const navTopbar = document.getElementById("topbar");
const navList = navTopbar ? navTopbar.querySelector("nav") : null;
const navToggle = document.getElementById("nav-toggle");

function syncNavCollapse() {
  if (!navTopbar || !navList || !navToggle) return;
  // measure in the expanded state within the same frame (no visible flicker)
  navTopbar.classList.remove("nav-collapsed");
  const overflow = navList.scrollWidth > navList.clientWidth + 1;
  navTopbar.classList.toggle("nav-collapsed", overflow);
  if (!overflow) navTopbar.classList.remove("nav-open");
  syncTopbarHeight();
}

if (navToggle) {
  navToggle.addEventListener("click", e => {
    e.stopPropagation();
    navTopbar.classList.toggle("nav-open");
  });
  document.addEventListener("click", e => {
    if (navTopbar.classList.contains("nav-open") && !navList.contains(e.target)) {
      navTopbar.classList.remove("nav-open");
    }
  });
  window.addEventListener("resize", syncNavCollapse);
  syncNavCollapse();
}

/* ------------------------------------------- F1-F12 page hotkeys ------ */
document.addEventListener("keydown", e => {
  if (e.ctrlKey || e.altKey || e.metaKey || e.repeat) return;
  if (!/^F([1-9]|1[0-2])$/.test(e.key)) return;
  const link = document.querySelector(`#topbar nav a[data-hotkey="${e.key}"]`);
  if (!link) return;
  e.preventDefault();
  window.location.href = link.href;
});

/* ------------------------------------------------------- table sort ----- */
/* 3-state header sort: 1st click asc, 2nd desc, 3rd restores the unsorted order */
function makeSortable(table) {
  table.querySelectorAll("th").forEach((th, idx) => {
    th.addEventListener("click", () => {
      const tbody = table.querySelector("tbody");
      const rows = Array.from(tbody.rows);
      // remember the render order (recaptured after a re-render detaches old rows)
      if (!table._origRows || !table._origRows.every(r => r.isConnected)) {
        table._origRows = rows.slice();
      }
      const prev = th.dataset.asc;
      table.querySelectorAll("th").forEach(h => delete h.dataset.asc);
      if (prev === "0") {  // third click: back to unsorted
        table._origRows.forEach(r => tbody.appendChild(r));
        return;
      }
      const asc = prev !== "1";
      th.dataset.asc = asc ? "1" : "0";
      rows.sort((a, b) => {
        const av = a.cells[idx]?.dataset.v ?? a.cells[idx]?.textContent ?? "";
        const bv = b.cells[idx]?.dataset.v ?? b.cells[idx]?.textContent ?? "";
        const an = parseFloat(av), bn = parseFloat(bv);
        if (!Number.isNaN(an) && !Number.isNaN(bn)) return asc ? an - bn : bn - an;
        return asc ? String(av).localeCompare(bv) : String(bv).localeCompare(av);
      });
      rows.forEach(r => tbody.appendChild(r));
    });
  });
}

/* --------------------------------------------------- select helpers ----- */
function fillSelect(sel, items, valueKey, labelFn, selected) {
  sel.innerHTML = items.map(it =>
    `<option value="${esc(it[valueKey])}" ${String(it[valueKey]) === String(selected) ? "selected" : ""}>${esc(labelFn(it))}</option>`).join("");
}

/* ------------------------------------------------ index value cards ----- */
/* value + day change per benchmark index (HOME section) */
function renderIndexCards(container, list) {
  if (!container) return;
  const keep = new Set(list.map(ix => ix.key));
  container.querySelectorAll(".idx-card").forEach(c => {
    if (!keep.has(c.dataset.idx)) c.remove();
  });
  list.forEach(ix => {
    let card = container.querySelector(`[data-idx="${ix.key}"]`);
    if (!card) {
      card = document.createElement("div");
      card.className = "idx-card";
      card.dataset.idx = ix.key;
      card.innerHTML = `<div class="idx-head">
          <span class="idx-name" title="${esc(ix.name)}">${esc(ix.name)}</span>
          <span class="idx-val mono-num"></span>
          <span class="idx-chg mono-num"></span></div>`;
    }
    container.appendChild(card);
    const dirCls = ix.change > 0 ? "green" : (ix.change < 0 ? "red" : "");
    const val = card.querySelector(".idx-val");
    val.textContent = ix.current == null ? "\u2014" : fmt(ix.current);
    val.className = "idx-val mono-num " + dirCls;
    const chg = card.querySelector(".idx-chg");
    chg.textContent = ix.change_pct == null ? ""
      : `${ix.change > 0 ? "+" : ""}${fmt(ix.change)} (${fmtPct(ix.change_pct)})`;
    chg.className = "idx-chg mono-num " + dirCls;
  });
}

/* ------------------------------------------- ordered column editor ----- */
/* checkbox = visible, ▲▼ = position; shared by holdings/records column modals */
function colsOrderHtml(defs, group, selected) {
  const byKey = new Map(defs.map(d => [d[0], d[1]]));
  const ordered = selected.filter(k => byKey.has(k))
    .concat(defs.map(d => d[0]).filter(k => !selected.includes(k)));
  return `<div class="col-order" data-group="${group}">` + ordered.map(k => `
    <div class="col-order-row" data-key="${k}">
      <label><input type="checkbox" ${selected.includes(k) ? "checked" : ""}> ${byKey.get(k)}</label>
      <span class="spacer"></span>
      <button class="mini" data-mv="-1" title="move up">▲</button>
      <button class="mini" data-mv="1" title="move down">▼</button>
    </div>`).join("") + `</div>`;
}
function bindColsOrder(root) {
  root.addEventListener("click", e => {
    const btn = e.target.closest("button[data-mv]");
    if (!btn) return;
    const row = btn.closest(".col-order-row");
    if (+btn.dataset.mv < 0) {
      if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
    } else if (row.nextElementSibling) {
      row.parentNode.insertBefore(row.nextElementSibling, row);
    }
  });
}
function readColsOrder(group) {
  return [...document.querySelectorAll(`.col-order[data-group="${group}"] .col-order-row`)]
    .filter(r => r.querySelector("input").checked)
    .map(r => r.dataset.key);
}
