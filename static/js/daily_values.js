/* Daily end values: frozen date+TOTAL columns, grouped asset columns
   (currencies -> equity/etf -> others -> options last), cell-mode and
   decimals switches, CSV download. Today's row is provisional (live price x
   quantity) and refreshes every 60s; the chart draws it as a dashed bridge. */
"use strict";

let dvChart = null;
let lastDvView = null;
let dvIdxSel = () => ["SP500"];

function dvView() {
  return document.getElementById("dv-view").dataset.view;
}

function setDvView(v) {
  const btn = document.getElementById("dv-view");
  btn.dataset.view = v;
  btn.textContent = v.toUpperCase();
  btn.classList.toggle("on", v === "index");
}

function applyDvView() {
  const idx = dvView() === "index";
  document.getElementById("dv-value-ctl").classList.toggle("hidden", idx);
  document.getElementById("dv-index-ctl").classList.toggle("hidden", !idx);
}

/* zoom kept across refreshes within one view; per-view saved window on
   first paint / after switching views */
function dvZoom(savedKey) {
  const view = dvView();
  const prev = dvChart.getOption();
  const saved = chartPrefs.get("daily")[savedKey] || {};
  const z = (lastDvView === view && prev && prev.dataZoom && prev.dataZoom.length)
    ? { start: prev.dataZoom[0].start, end: prev.dataZoom[0].end }
    : { start: saved.start, end: saved.end };
  lastDvView = view;
  return z;
}

async function loadIndexChart() {
  const keys = dvIdxSel();
  const q = new URLSearchParams();
  if (keys.length) q.set("indices", keys.join(","));
  const days = +document.getElementById("dv-days").value;
  if (days > 0) {
    const d = new Date();
    d.setDate(d.getDate() - days);
    q.set("start", d.toISOString().slice(0, 10));
  }
  const data = await api("/api/performance/series?" + q);
  const opt = idxCompareOption(dvChart, data, dvZoom("idx_zoom"));
  if (!opt) {
    dvChart.clear();
    return;
  }
  tvExtras(opt);
  dvChart.setOption(opt, true);
}

const GROUP_LABELS = { currency: "CURRENCY", equity: "EQUITY / ETF",
                       other: "OTHERS", option: "OPTIONS" };

/* disposable-income deposits, summed per calendar day (display ccy) */
let dispCache = null;
async function dispByDate() {
  if (!dispCache) {
    dispCache = new Map();
    try {
      const d = await api("/api/disposable");
      (d.entries || []).forEach(e => {
        const k = String(e.txn_date).slice(0, 10);
        dispCache.set(k, (dispCache.get(k) || 0) + (e.amount_display || 0));
      });
    } catch (_e) { /* markers are cosmetic; chart still renders */ }
  }
  return dispCache;
}

/* long OCC tickers wrap to three lines so option columns stay narrow */
function optHeader(ticker) {
  const t = String(ticker || "");
  const n = Math.ceil(t.length / 3) || 1;
  return [t.slice(0, n), t.slice(n, 2 * n), t.slice(2 * n)]
    .filter(Boolean).map(esc).join("<br>");
}

async function load() {
  const mode = document.getElementById("dv-mode").value;
  const days = document.getElementById("dv-days").value;
  const dp = parseInt(document.getElementById("dv-dp").value, 10);
  const data = await api(`/api/history/daily-values?mode=${mode}&days=${days}`);
  const thead = document.querySelector("#tbl-dv thead");
  const tbody = document.querySelector("#tbl-dv tbody");

  const cols = data.columns;
  const spans = [];
  cols.forEach(c => {
    const last = spans[spans.length - 1];
    if (last && last.group === c.group) last.count += 1;
    else spans.push({ group: c.group, count: 1 });
  });
  let lastCurrency = -1;
  cols.forEach((c, i) => { if (c.group === "currency") lastCurrency = i; });

  thead.innerHTML =
    `<tr><th class="col-date" rowspan="2">DATE</th>` +
    `<th class="col-total" rowspan="2">TOTAL ${esc(data.display_currency)}</th>` +
    spans.map(s =>
      `<th colspan="${s.count}" class="grp-head${s.group === "currency" ? " grp-end" : ""}">` +
      `${GROUP_LABELS[s.group] || esc(String(s.group).toUpperCase())}</th>`).join("") +
    "</tr><tr>" +
    cols.map((c, i) => {
      const cls = [c.group === "option" ? "th-opt" : "", i === lastCurrency ? "grp-end" : ""]
        .filter(Boolean).join(" ");
      return `<th${cls ? ` class="${cls}"` : ""} title="${esc(c.name)}">` +
        `${c.group === "option" ? optHeader(c.ticker) : esc(c.ticker)}</th>`;
    }).join("") + "</tr>";

  tbody.innerHTML = data.rows.map(r =>
    `<tr${r.provisional ? " class='provisional'" : ""}><td>${formatDisplayDate(r.date)}` +
    (r.provisional ? " <span class='live-tag'>LIVE</span>" : "") + "</td>" +
    r.cells.map((v, i) =>
      `<td class="num${i - 1 === lastCurrency ? " grp-end" : ""}">` +
      `${v === null ? "<span class='subtle'>—</span>" : fmt(v, dp)}</td>`).join("") +
    "</tr>").join("");

  // sticky offsets: date-column width + group header row height
  requestAnimationFrame(() => {
    const tbl = document.getElementById("tbl-dv");
    const firstTh = thead.querySelector("th");
    if (firstTh) tbl.style.setProperty("--freeze-w", firstTh.getBoundingClientRect().width + "px");
    const row1 = thead.querySelector("tr");
    if (row1) tbl.style.setProperty("--thead1-h", row1.getBoundingClientRect().height + "px");
  });

  if (dvView() === "index") {
    await loadIndexChart();
    return;
  }

  // chart: frozen totals solid; provisional today as a dashed bridge segment
  const asc = [...data.rows].reverse();
  const frozen = asc.map(r => (r.provisional ? null : r.cells[0]));
  const dashed = asc.map(() => null);
  const p = asc.findIndex(r => r.provisional);
  if (p >= 0) {
    dashed[p] = asc[p].cells[0];
    if (p > 0) dashed[p - 1] = asc[p - 1].cells[0];
  }
  // live line direction vs last frozen close: green up / red down / neutral flat
  const liveVal = p >= 0 ? asc[p].cells[0] : null;
  const lastFrozen = p > 0 ? asc[p - 1].cells[0] : null;
  const liveColor = (liveVal == null || lastFrozen == null || liveVal === lastFrozen)
    ? CHART_C.neutral : (liveVal > lastFrozen ? "#00c853" : "#ff1744");
  // keep the zoom window across the 60s poll refreshes; fall back to the
  // saved window on the first paint after a reload
  const zoom = dvZoom("zoom");
  // markers on days a disposable-income deposit landed
  const disp = await dispByDate();
  const dispMarks = [];
  asc.forEach((r, i) => {
    const amt = disp.get(String(r.date).slice(0, 10));
    if (amt && r.cells[0] != null) {
      dispMarks.push({ coord: [i, r.cells[0]], value: amt });
    }
  });
  const totalSeries = withEndLabel({ type: "line", name: "TOTAL", showSymbol: false, data: frozen,
    lineStyle: { color: "#ffb000" }, areaStyle: { color: "rgba(255,176,0,0.08)" } },
    "#ffb000", 2, dvChart);
  if (dispMarks.length) {
    totalSeries.markPoint = incomeMarkPoint(dispMarks);
  }
  const opt = {
    tooltip: { trigger: "axis" },
    grid: { left: 80, right: 96, top: 28, bottom: 36 },
    xAxis: { type: "category", data: asc.map(r => formatDisplayDate(r.date)) },
    yAxis: { type: "value", scale: true },
    dataZoom: [{ type: "inside", ...zoom }, { type: "slider", height: 14, bottom: 2, ...zoom }],
    series: [
      totalSeries,
      withEndLabel({ type: "line", name: "TODAY (LIVE)", data: dashed, showSymbol: true, symbolSize: 5,
        lineStyle: { color: liveColor, type: "dashed", width: 1.5 },
        itemStyle: { color: liveColor } }, liveColor, 2, dvChart),
    ],
  };
  tvExtras(opt, { yMin: document.getElementById("dv-ymin").value,
                  yMax: document.getElementById("dv-ymax").value });
  dvChart.setOption(opt, true);
}

document.getElementById("dv-mode").addEventListener("change", load);
document.getElementById("dv-dp").addEventListener("change", load);
document.getElementById("dv-days").addEventListener("change", load);
document.getElementById("dv-ymin").addEventListener("change", load);
document.getElementById("dv-ymax").addEventListener("change", load);
document.getElementById("dv-reset").addEventListener("click", () => {
  document.getElementById("dv-ymin").value = "";
  document.getElementById("dv-ymax").value = "";
  chartPrefs.set("daily", dvView() === "index"
    ? { idx_zoom: null } : { zoom: null, ymin: "", ymax: "" });
  lastDvView = null;  // force load() to drop the current zoom window
  load();
});
document.getElementById("dv-csv").addEventListener("click", () => {
  const mode = document.getElementById("dv-mode").value;
  window.location = `/api/history/daily-values.csv?mode=${mode}`;
});
document.getElementById("dv-rebuild").addEventListener("click", async () => {
  if (!await confirmDialog("Rebuild all snapshots from transactions × price history? " +
      "This recomputes every daily row.")) return;
  const res = await api("/api/history/snapshots/rebuild", { method: "POST", body: {} });
  toast(`rebuilt ${res.rows} snapshot rows`, "ok");
  load();
});

dvChart = mkChart("dv-chart");
(async () => {
  await chartPrefs.load();
  setDvView(chartPrefs.get("daily").view === "index" ? "index" : "value");
  bindControlPersist("daily", "dv-mode", "mode");
  bindControlPersist("daily", "dv-days", "days");
  bindControlPersist("daily", "dv-dp", "dp");
  bindControlPersist("daily", "dv-ymin", "ymin");
  bindControlPersist("daily", "dv-ymax", "ymax");
  // zoom saved under the active view's own key
  dvChart.on("datazoom", () => {
    const dz = dvChart.getOption().dataZoom;
    if (dz && dz.length) {
      chartPrefs.set("daily", { [dvView() === "index" ? "idx_zoom" : "zoom"]:
        { start: dz[0].start, end: dz[0].end } });
    }
  });
  dvChart.on("restore", () => chartPrefs.set("daily",
    { [dvView() === "index" ? "idx_zoom" : "zoom"]: null }));
  applyDvView();
  dvIdxSel = await initIdxChecks("dv-indices", "dv-idx", "daily", "idx_indices", load);
  document.getElementById("dv-view").addEventListener("click", () => {
    setDvView(dvView() === "index" ? "value" : "index");
    chartPrefs.set("daily", { view: dvView() });
    applyDvView();
    load();
  });
  poll(load, 60);
})();
