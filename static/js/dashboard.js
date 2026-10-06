/* Home dashboard: KPIs, allocation bar + NAV line, pies, series chart.
   The holdings table logic lives in holdings.js (shared with /holdings). */
"use strict";

const KPI_DEFS = [
  ["nav", "NET ASSET VALUE"], ["cash_total", "CASH"],
  ["equities", "EQUITIES"],
  ["bonds", "BONDS"], ["derivatives", "DERIVATIVES"], ["crypto", "CRYPTO"],
  ["real_estate", "REAL ESTATE"], ["loans", "LOANS"], ["exposure", "GROSS EXPOSURE"],
];
let chAlloc, chInd, chCty, chSeries;
let summaryData = null;
let lastSeriesView = null;

function renderKpis(k) {
  const strip = document.getElementById("kpi-strip");
  strip.innerHTML = KPI_DEFS.filter(d => prefs.kpis.includes(d[0])).map(([key, label]) => {
    const v = k[key];
    const cls = key === "loans" ? (v < 0 ? "red" : "") : (key === "nav" ? valClass(v) : "");
    return `<div class="kpi"><div class="label" title="${label}">${label}</div>
      <div class="value ${cls}" title="${fmt(v)}">${fmt(v)}</div></div>`;
  }).join("");
}

/* ------------------------------------------ home section order/collapse - */
const HOME_SECTION_KEYS = ["kpis", "indices", "series", "holdings", "charts"];

function applyHomeLayout() {
  const wrap = document.getElementById("home-sections");
  const order = (prefs.home_sections || HOME_SECTION_KEYS)
    .filter(k => HOME_SECTION_KEYS.includes(k));
  HOME_SECTION_KEYS.forEach(k => { if (!order.includes(k)) order.push(k); });
  order.forEach(key => {
    const el = wrap.querySelector(`section[data-section="${key}"]`);
    if (el) wrap.appendChild(el);
  });
  const collapsed = new Set(prefs.home_collapsed || []);
  wrap.querySelectorAll("section[data-section]").forEach(sec => {
    sec.classList.toggle("collapsed", collapsed.has(sec.dataset.section));
    sec.querySelector("[data-sec-toggle]").addEventListener("click", async () => {
      const isCollapsed = sec.classList.toggle("collapsed");
      const set = new Set(prefs.home_collapsed || []);
      if (isCollapsed) set.add(sec.dataset.section);
      else {
        set.delete(sec.dataset.section);
        sec.querySelectorAll(".chart").forEach(resizeChartForElement);
      }
      prefs.home_collapsed = [...set];
      await savePrefs();
    });
  });
}

function renderAlloc(cat, nav) {
  const order = [["cash", "CASH"], ["equity", "EQUITY"], ["etf", "ETF"], ["bond", "BOND"],
    ["option", "OPTION"], ["future", "FUTURE"], ["crypto", "CRYPTO"],
    ["real_estate", "REAL EST"], ["loan", "LOAN"], ["other", "OTHER"]];
  const cats = order.filter(([k]) => Math.abs(cat[k] || 0) > 1e-9);
  const palette = BBG_THEME.color;
  chAlloc.setOption({
    tooltip: { trigger: "axis" },
    grid: { left: 80, right: 90, top: 24, bottom: 22 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: cats.map(c => c[1]) },
    series: [{
      type: "bar", data: cats.map(([k], i) => ({
        value: +(cat[k] || 0).toFixed(2),
        // one distinct colour per item; liabilities stay red
        itemStyle: { color: (cat[k] || 0) >= 0 ? palette[i % palette.length] : "#ff1744" },
      })),
      label: { show: true, position: "right", color: CHART_C.text, fontSize: chartFs(10),
               formatter: p => fmt(p.value) },
      markLine: {
        symbol: "none",
        lineStyle: { type: "dotted", color: "#00c853", width: 2 },
        label: { color: "#00c853", formatter: `NAV ${fmt(nav)}`, position: "insideEndTop" },
        data: [{ xAxis: +nav.toFixed(2) }],
      },
    }],
  }, true);
}

function pieOption(data) {
  return {
    tooltip: { trigger: "item", formatter: p => `${p.name}<br>${fmt(p.value)} (${p.percent}%)` },
    series: [{
      type: "pie", radius: ["34%", "72%"],
      label: { color: CHART_C.text, fontSize: chartFs(10), formatter: "{b}\n{d}%" },
      itemStyle: { borderColor: CHART_C.pieBorder, borderWidth: 1 },
      data: data.map(d => ({ name: d.name, value: d.value })),
    }],
  };
}

/* industry/country charts: long-exposure pie, net pie, or net bar (negatives) */
function renderEquityChart(chart, longRows, netRows, mode) {
  if (mode === "netbar") {
    const rows = [...netRows].reverse(); // desc input -> largest on top
    chart.setOption({
      tooltip: { trigger: "axis" },
      grid: { left: 110, right: 80, top: 10, bottom: 22 },
      xAxis: { type: "value" },
      yAxis: { type: "category", data: rows.map(r => r.name),
               axisLabel: { width: 100, overflow: "truncate" } },
      series: [{ type: "bar",
        data: rows.map(r => ({ value: r.value,
          itemStyle: { color: r.value >= 0 ? "#40c4ff" : "#ff1744" } })),
        label: { show: true, position: "right", color: CHART_C.text, fontSize: chartFs(10),
                 formatter: p => fmt(p.value) } }],
    }, true);
    return;
  }
  const rows = mode === "netpie" ? netRows.filter(r => r.value > 0) : longRows;
  chart.setOption(pieOption(rows), true);
}

function renderEquityCharts() {
  if (!summaryData) return;
  renderEquityChart(chInd, summaryData.industry_pie || [], summaryData.industry_net || [],
    document.getElementById("ind-mode").value);
  renderEquityChart(chCty, summaryData.country_pie || [], summaryData.country_net || [],
    document.getElementById("cty-mode").value);
}

/* ------------------------------------------------ holdings (grouped) ---- */
/* moved to holdings.js (shared with the dedicated /holdings page) */

function columnsModal() {
  const overlay = openModal("VISIBLE FIELDS",
    `<div class="amber" style="margin-bottom:4px">KPI TILES</div><div class="chk-grid">${colsChecklistHtml(KPI_DEFS, "kpis", prefs.kpis)}</div>
     <div class="amber" style="margin:8px 0 4px">HOLDINGS COLUMNS — tick to show, ▲▼ to reorder</div>${colsOrderHtml(HOLDING_COLS, "holding_cols", prefs.holding_cols)}`,
    `<button class="primary" id="cols-save">SAVE</button>`);
  bindColsOrder(overlay);
  document.getElementById("cols-save").onclick = async () => {
    prefs.kpis = readChecklist("kpis");
    prefs.holding_cols = readColsOrder("holding_cols");
    await savePrefs();
    closeModal();
    refresh();
  };
}

async function refresh() {
  const data = await api("/api/dashboard/summary");
  summaryData = data;
  document.getElementById("alloc-ccy").textContent = data.display_currency;
  renderKpis(data.kpis);
  renderAlloc(data.categories, data.kpis.nav);
  renderEquityCharts();
  setHoldingsData(data.holdings, data.txn_groups);
  const warn = document.getElementById("fx-warn");
  if (data.fx_warnings.length) {
    warn.classList.remove("hidden");
    document.getElementById("fx-warn-text").textContent =
      "DATA WARNINGS: " + data.fx_warnings.join(", ");
  } else warn.classList.add("hidden");
}

let benchCache = { key: "", data: null };

async function refreshIndexSparks() {
  renderIndexCards(document.getElementById("idx-strip"),
    await api("/api/dashboard/index-sparks"));
}

/* -------------------------------------------- value/index view toggle ---- */
let dashIdxSel = () => ["SP500"];

function seriesView() {
  return document.getElementById("series-view").dataset.view;
}

function setSeriesView(v) {
  const btn = document.getElementById("series-view");
  btn.dataset.view = v;
  btn.textContent = v.toUpperCase();
  btn.classList.toggle("on", v === "index");
}

function applySeriesView() {
  const idx = seriesView() === "index";
  document.getElementById("series-value-ctl").classList.toggle("hidden", idx);
  document.getElementById("series-index-ctl").classList.toggle("hidden", !idx);
}

/* zoom window kept across refreshes within one view; per-view saved window
   restored on first paint / after switching views */
function seriesZoom(savedKey) {
  const view = seriesView();
  const prev = chSeries.getOption();
  const saved = chartPrefs.get("dashboard")[savedKey] || {};
  const z = (lastSeriesView === view && prev && prev.dataZoom && prev.dataZoom.length)
    ? { start: prev.dataZoom[0].start, end: prev.dataZoom[0].end }
    : { start: saved.start, end: saved.end };
  lastSeriesView = view;
  return z;
}

async function initDashIndices() {
  dashIdxSel = await initIdxChecks("dash-indices", "dash-idx", "dashboard", "idx_indices",
                                   refreshSeries);
}

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

async function refreshIndexSeries() {
  const keys = dashIdxSel();
  const mode = document.getElementById("idx-mode").value;
  const q = new URLSearchParams();
  if (keys.length) q.set("indices", keys.join(","));
  const base = document.getElementById("idx-base").value;
  if (base) q.set("start", base);
  if (mode === "candle") q.set("mode", "candle");
  const data = await api("/api/performance/series?" + q);
  const opt = idxCompareOption(chSeries, data, seriesZoom("idx_zoom"), mode);
  if (!opt) {
    chSeries.clear();
    return;
  }
  tvExtras(opt);
  chSeries.setOption(opt, true);
}

async function refreshSeries() {
  if (seriesView() === "index") return refreshIndexSeries();
  const mode = document.getElementById("series-mode").value;
  const rebase = document.getElementById("rebase-date").value;
  const benchKey = document.getElementById("series-bench").value;
  const useBench = mode !== "candle" && !!benchKey;
  const q = new URLSearchParams({ mode });
  // with a benchmark both lines rebase client-side so they share one base
  if (rebase && !useBench) q.set("rebase", rebase);
  const s = await api("/api/dashboard/series?" + q);
  const live = mode !== "candle" && s.today ? s.today : null;
  const dates = live ? s.dates.concat([live.date]) : s.dates;
  const displayDates = dates.map(d => formatDisplayDate(d));

  let values = s.values;
  let liveVal = live ? live.value : null;
  let bench = null;
  if (useBench) {
    if (benchCache.key !== benchKey) {
      try {
        benchCache = { key: benchKey,
          data: await api("/api/dashboard/benchmark?symbol=" + encodeURIComponent(benchKey)) };
      } catch (_e) {
        benchCache = { key: "", data: null };  // api() already toasted the reason
        document.getElementById("series-bench").value = "";
      }
    }
    if (benchCache.data) {
      const closeByDate = new Map(benchCache.data.dates.map((d, i) => [d, benchCache.data.closes[i]]));
      bench = dates.map(d => closeByDate.get(d) ?? null);
      if (rebase) {
        // base date set -> both lines in index form (base = 100)
        let baseIdx = s.dates.findIndex(d => d >= rebase);
        if (baseIdx < 0) baseIdx = dates.findIndex((_d, i) => values[i] && bench[i] != null);
        if (baseIdx >= 0) {
          const bBase = bench[baseIdx] ?? bench.find((v, i) => i >= baseIdx && v != null);
          const pf = values[baseIdx] ? 100 / values[baseIdx] : null;
          const bf = bBase ? 100 / bBase : null;
          if (pf) {
            values = values.map(v => (v == null ? null : +(v * pf).toFixed(4)));
            if (liveVal != null) liveVal = +(liveVal * pf).toFixed(4);
          }
          if (bf) bench = bench.map(v => (v == null ? null : +(v * bf).toFixed(4)));
        }
      }
      // carry the last close to the final x so the index end label sits at the
      // right edge, aligned with the portfolio/live labels
      const lastIdx = bench.reduce((acc, v, i) => (v == null ? acc : i), -1);
      if (lastIdx >= 0) {
        for (let i = lastIdx + 1; i < bench.length; i++) bench[i] = bench[lastIdx];
      }
    }
  }

  // no base date -> raw values: index gets its own (right) axis
  const rawBench = !!bench && !rebase;

  // keep the user's zoom window across the 60s poll refreshes; fall back to
  // the saved window on the first paint after a reload
  const zoom = seriesZoom("zoom");
  const opt = {
    tooltip: { trigger: "axis" },
    grid: { left: 70, right: 96, top: 28, bottom: 40 },
    xAxis: { type: "category", data: displayDates },
    yAxis: rawBench
      ? [{ type: "value", scale: true },
         { type: "value", scale: true, splitLine: { show: false } }]
      : { type: "value", scale: true },
    dataZoom: [{ type: "inside", ...zoom },
               { type: "slider", height: 16, bottom: 4, ...zoom }],
  };
  tvExtras(opt, { yMin: document.getElementById("y-min").value,
                  yMax: document.getElementById("y-max").value });
  if (mode === "candle") {
    opt.series = [{ type: "candlestick", data: s.ohlc,
      itemStyle: { color: "#00c853", color0: "#ff1744",
                   borderColor: "#00c853", borderColor0: "#ff1744" } }];
  } else {
    // provisional today point (live price x quantity) drawn as a dashed bridge
    const dashed = dates.map(() => null);
    if (live) {
      dashed[dates.length - 1] = liveVal;
      if (values.length) dashed[dates.length - 2] = values[values.length - 1];
    }
    // live line direction vs last frozen close: green up / red down / neutral flat
    const lastFrozen = values.length ? values[values.length - 1] : null;
    const liveColor = (liveVal == null || lastFrozen == null || liveVal === lastFrozen)
      ? CHART_C.neutral : (liveVal > lastFrozen ? "#00c853" : "#ff1744");
    opt.series = [withEndLabel({ type: "line", name: "PORTFOLIO",
      data: live ? values.concat([null]) : values,
      showSymbol: false,
      lineStyle: { color: "#ffb000", width: 1.5 },
      areaStyle: { color: "rgba(255,176,0,0.08)" } }, "#ffb000", 2, chSeries)];
    // markers on days a disposable-income deposit landed
    const disp = await dispByDate();
    const dispMarks = [];
    s.dates.forEach((d, i) => {
      const amt = disp.get(String(d).slice(0, 10));
      if (amt && values[i] != null) dispMarks.push({ coord: [i, values[i]], value: amt });
    });
    if (dispMarks.length) {
      opt.series[0].markPoint = incomeMarkPoint(dispMarks);
    }
    if (live) opt.series.push(withEndLabel({ type: "line", name: "TODAY (LIVE)", data: dashed,
      showSymbol: true, symbolSize: 5,
      lineStyle: { color: liveColor, type: "dashed", width: 1.5 },
      itemStyle: { color: liveColor } }, liveColor, 2, chSeries));
    if (bench) opt.series.push(withEndLabel({ type: "line", name: benchKey,
      data: bench, showSymbol: false, connectNulls: true,
      yAxisIndex: rawBench ? 1 : 0,
      lineStyle: { color: "#40c4ff", width: 1.5 } }, "#40c4ff", 2, chSeries));
  }
  chSeries.setOption(opt, true);
}

(async function init() {
  chAlloc = mkChart("chart-alloc");
  chInd = mkChart("chart-industry");
  chCty = mkChart("chart-country");
  chSeries = mkChart("chart-series");
  await loadPrefs();
  await chartPrefs.load();
  applyHomeLayout();
  // restore saved chart view state, then persist future changes
  setSeriesView(chartPrefs.get("dashboard").view === "index" ? "index" : "value");
  bindControlPersist("dashboard", "series-mode", "mode");
  const benchSel = document.getElementById("series-bench");
  (await api("/api/performance/indices")).forEach(ix => benchSel.add(
    new Option(ix.name.toUpperCase(), ix.key)));
  bindControlPersist("dashboard", "series-bench", "bench");
  if (benchSel.selectedIndex < 0) benchSel.value = "";
  bindControlPersist("dashboard", "rebase-date", "rebase");
  bindControlPersist("dashboard", "y-min", "ymin");
  bindControlPersist("dashboard", "y-max", "ymax");
  bindControlPersist("dashboard", "idx-mode", "idx_mode");
  bindControlPersist("dashboard", "idx-base", "idx_base");
  bindControlPersist("dashboard", "ind-mode", "ind_mode");
  bindControlPersist("dashboard", "cty-mode", "cty_mode");
  // zoom saved under the active view's own key
  chSeries.on("datazoom", () => {
    const dz = chSeries.getOption().dataZoom;
    if (dz && dz.length) {
      chartPrefs.set("dashboard", {
        [seriesView() === "index" ? "idx_zoom" : "zoom"]:
          { start: dz[0].start, end: dz[0].end } });
    }
  });
  chSeries.on("restore", () => chartPrefs.set("dashboard",
    { [seriesView() === "index" ? "idx_zoom" : "zoom"]: null }));
  applySeriesView();
  await initDashIndices();
  document.getElementById("series-view").addEventListener("click", () => {
    setSeriesView(seriesView() === "index" ? "value" : "index");
    chartPrefs.set("dashboard", { view: seriesView() });
    applySeriesView();
    refreshSeries();
  });
  document.getElementById("series-mode").addEventListener("change", refreshSeries);
  document.getElementById("rebase-date").addEventListener("change", refreshSeries);
  document.getElementById("series-bench").addEventListener("change", refreshSeries);
  document.getElementById("y-min").addEventListener("change", refreshSeries);
  document.getElementById("y-max").addEventListener("change", refreshSeries);
  document.getElementById("idx-mode").addEventListener("change", refreshSeries);
  document.getElementById("idx-base").addEventListener("change", refreshSeries);
  document.getElementById("idx-base-clear").addEventListener("click", () => {
    document.getElementById("idx-base").value = "";
    chartPrefs.set("dashboard", { idx_base: "" });
    refreshSeries();
  });
  document.getElementById("series-reset").addEventListener("click", () => {
    document.getElementById("y-min").value = "";
    document.getElementById("y-max").value = "";
    if (seriesView() === "index") {
      document.getElementById("idx-base").value = "";
      chartPrefs.set("dashboard", { idx_zoom: null, idx_base: "" });
    } else {
      document.getElementById("series-bench").value = "";
      document.getElementById("rebase-date").value = "";
      chartPrefs.set("dashboard",
        { zoom: null, ymin: "", ymax: "", bench: "", rebase: "" });
    }
    lastSeriesView = null;  // force refreshSeries to drop the current zoom window
    refreshSeries();
  });
  document.getElementById("rebase-clear").addEventListener("click", () => {
    document.getElementById("rebase-date").value = "";
    chartPrefs.set("dashboard", { rebase: "" });
    refreshSeries();
  });
  document.getElementById("btn-columns").addEventListener("click", columnsModal);
  ["ind-mode", "cty-mode"].forEach(id =>
    document.getElementById(id).addEventListener("change", renderEquityCharts));
  bindHoldingsControls();
  poll(refresh, 60);
  poll(refreshSeries, 60);
  poll(refreshIndexSparks, 60);
})();
