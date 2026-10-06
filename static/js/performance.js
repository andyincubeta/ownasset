/* Performance page: stats strip, portfolio-vs-indices chart (rebased to 100,
   live tail dashed), and a year x month returns / P&L table.
   Index series start is clamped server-side to the first portfolio snapshot. */
"use strict";

const MONTH_LABELS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                      "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
const IDX_COLORS = ["#40c4ff", "#00c853", "#e040fb", "#ffd740", "#69f0ae", "#ff6e40"];

let perfChart = null;
let monthlyData = null;

function pct(v, dp = 2) {
  return (v === null || v === undefined) ? "—" : (v * 100).toFixed(dp) + "%";
}

/* ------------------------------------------------------------- stats ----- */
function renderStats(stats) {
  const strip = document.getElementById("perf-stats");
  if (!stats || stats.ytd === undefined) {
    strip.innerHTML = `<div class="dim">No snapshot history yet — snapshots build up daily, or REBUILD SNAPSHOTS on the DAILY page.</div>`;
    return;
  }
  const mLabel = m => m ? `${m.year}-${String(m.month).padStart(2, "0")}` : "";
  const cards = [
    ["YTD", pct(stats.ytd), valClass(stats.ytd)],
    ["1 YEAR", pct(stats.one_year), valClass(stats.one_year)],
    ["CAGR", pct(stats.cagr), valClass(stats.cagr)],
    [`TOTAL (${stats.months} MO)`, pct(stats.total), valClass(stats.total)],
    [`BEST ${mLabel(stats.best_month)}`, pct(stats.best_month && stats.best_month.ret), "green"],
    [`WORST ${mLabel(stats.worst_month)}`, pct(stats.worst_month && stats.worst_month.ret), "red"],
    ["MAX DRAWDOWN", pct(stats.max_drawdown), "red"],
  ];
  strip.innerHTML = cards.map(([label, value, cls]) =>
    `<div class="kpi"><div class="label" title="${label}">${label}</div>
      <div class="value ${cls || ""}">${value}</div></div>`).join("");
}

/* ----------------------------------------------------------- indices ----- */
async function loadIndices() {
  const list = await api("/api/performance/indices");
  const saved = chartPrefs.get("performance").indices;
  const isChecked = key => Array.isArray(saved) ? saved.includes(key) : key === "SP500";
  document.getElementById("perf-indices").innerHTML = list.map(ix =>
    `<label class="dim" style="cursor:pointer">
       <input type="checkbox" class="perf-idx" value="${ix.key}" ${isChecked(ix.key) ? "checked" : ""}>
       ${esc(ix.name.toUpperCase())}</label>`).join("");
  document.querySelectorAll(".perf-idx").forEach(cb => cb.addEventListener("change", () => {
    chartPrefs.set("performance", {
      indices: [...document.querySelectorAll(".perf-idx:checked")].map(c => c.value) });
    loadSeries();
  }));
}

function rangeStart() {
  const custom = document.getElementById("perf-start").value;
  if (custom) return custom;
  const btn = document.querySelector("#perf-range button.primary");
  const mode = btn ? btn.dataset.range : "all";
  if (mode === "all") return "";
  const d = new Date();
  d.setFullYear(d.getFullYear() - +mode);
  return d.toISOString().slice(0, 10);
}

/* ------------------------------------------------------------- chart ----- */
function seriesPair(name, dates, values, today, color, axisDates) {
  // solid frozen line + dashed bridge from last frozen point to the live one
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
    itemStyle: { color } }, color, 2, perfChart)];
  if (today) {
    out.push({ ...base, name: `${name} (LIVE)`, data: dashed, showSymbol: true, symbolSize: 4,
      lineStyle: { color, type: "dashed", width: 1.5 }, itemStyle: { color },
      tooltip: { show: false } });
  }
  return out;
}

async function loadSeries() {
  const keys = [...document.querySelectorAll(".perf-idx:checked")].map(cb => cb.value);
  const q = new URLSearchParams();
  if (keys.length) q.set("indices", keys.join(","));
  const start = rangeStart();
  if (start) q.set("start", start);
  const data = await api("/api/performance/series?" + q);
  const note = document.getElementById("perf-clamp-note");
  note.textContent = data.start
    ? (start && start < data.start
      ? `start clamped to ${data.start} — records begin there, indices start with your assets`
      : `start: ${data.start}`)
    : "no portfolio history yet";

  const pf = data.portfolio;
  const dateSet = new Set(pf.dates);
  if (pf.today) dateSet.add(pf.today.date);
  (data.indices || []).forEach(ix => {
    ix.dates.forEach(d => dateSet.add(d));
    if (ix.today) dateSet.add(ix.today.date);
  });
  const axisDates = [...dateSet].sort();

  let series = seriesPair("PORTFOLIO", pf.dates, pf.values, pf.today, "#ffb000", axisDates);
  (data.indices || []).forEach((ix, i) => {
    series = series.concat(seriesPair(ix.name.toUpperCase(), ix.dates, ix.values, ix.today,
      IDX_COLORS[i % IDX_COLORS.length], axisDates));
  });

  // keep the zoom window across refreshes; saved window on first paint
  const prev = perfChart.getOption();
  const savedZoom = chartPrefs.get("performance").zoom || {};
  const zoom = prev && prev.dataZoom && prev.dataZoom.length
    ? { start: prev.dataZoom[0].start, end: prev.dataZoom[0].end }
    : { start: savedZoom.start, end: savedZoom.end };
  const opt = {
    tooltip: { trigger: "axis", valueFormatter: v => (v == null ? "\u2014" : fmt(v)) },
    legend: { top: 2, left: 8 },
    grid: { left: 64, right: 96, top: 30, bottom: 40 },
    xAxis: { type: "category", data: axisDates.map(formatDisplayDate) },
    yAxis: { type: "value", scale: true },
    dataZoom: [{ type: "inside", ...zoom },
               { type: "slider", height: 16, bottom: 4, ...zoom }],
    series,
  };
  tvExtras(opt);
  perfChart.setOption(opt, true);
}

/* ------------------------------------------------------------- table ----- */
function renderMonthly() {
  if (!monthlyData) return;
  const mode = document.getElementById("perf-tbl-mode").value;
  const ccy = monthlyData.display_currency || "USD";
  const logv = r => (r === null || r === undefined || r <= -1) ? null : Math.log(1 + r);
  const pick = (ret, pnlv) => mode === "ret" ? ret : mode === "logret" ? logv(ret) : pnlv;
  const thead = document.querySelector("#tbl-perf thead");
  const tbody = document.querySelector("#tbl-perf tbody");
  thead.innerHTML = `<tr><th>YEAR</th>${MONTH_LABELS.map(m => `<th>${m}</th>`).join("")}
    <th>${mode === "pnl" ? `FY P&L ${esc(ccy)}` : mode === "logret" ? "FY LOG RETURN" : "FY RETURN"}</th></tr>`;
  tbody.innerHTML = monthlyData.years.map(y => {
    const cells = y.months.map(c => {
      if (!c) return `<td class="subtle center">—</td>`;
      const v = pick(c.ret, c.pnl);
      if (v === null || v === undefined) return `<td class="subtle center">—</td>`;
      const txt = mode === "pnl" ? fmt(v, 0) : pct(v, 1);
      const star = c.provisional ? "<span class='live-tag'>*</span>" : "";
      return `<td class="num ${valClass(v)}${c.provisional ? " provisional" : ""}">${txt}${star}</td>`;
    }).join("");
    const fy = pick(y.year_ret, y.year_pnl);
    const fyTxt = mode === "pnl" ? fmt(fy, 0) : pct(fy, 1);
    return `<tr${y.provisional ? " class='provisional'" : ""}>
      <td class="amber">${y.year}${y.provisional ? "*" : ""}</td>${cells}
      <td class="num ${valClass(fy)}"><b>${fy === null || fy === undefined ? "—" : fyTxt}</b></td></tr>`;
  }).join("");
}

async function loadMonthly() {
  monthlyData = await api("/api/performance/monthly");
  renderStats(monthlyData.stats || {});
  renderMonthly();
}

/* -------------------------------------------------------------- wiring --- */
document.querySelectorAll("#perf-range button").forEach(btn =>
  btn.addEventListener("click", () => {
    document.querySelectorAll("#perf-range button").forEach(b => b.classList.remove("primary"));
    btn.classList.add("primary");
    document.getElementById("perf-start").value = "";
    chartPrefs.set("performance", { range: btn.dataset.range, start: "" });
    loadSeries();
  }));
document.getElementById("perf-start").addEventListener("change", loadSeries);
document.getElementById("perf-tbl-mode").addEventListener("change", renderMonthly);
document.getElementById("perf-refresh").addEventListener("click", loadMonthly);

perfChart = mkChart("perf-chart");
(async function init() {
  await chartPrefs.load();
  const saved = chartPrefs.get("performance");
  if (saved.range) {
    document.querySelectorAll("#perf-range button").forEach(b =>
      b.classList.toggle("primary", b.dataset.range === saved.range));
  }
  bindControlPersist("performance", "perf-start", "start");
  bindControlPersist("performance", "perf-tbl-mode", "tbl_mode");
  bindZoomPersist(perfChart, "performance");
  await loadIndices();
  await Promise.all([loadSeries(), loadMonthly()]);
})();
// live tail follows the quote scheduler; table provisional month too
setInterval(loadSeries, 60000);
setInterval(loadMonthly, 300000);
