/* Per-strategy (txn group) performance: group list with P&L badges, P&L-over-
   time chart with an optional positions second axis (hidden by default), KPI
   strip and the group's trade list. */
"use strict";

const SG_TYPE_LABELS = { pair: "PAIR TRADE", option_strategy: "OPTION STRATEGY",
                         hedge: "HEDGE", normal: "NORMAL", custom: "CUSTOM" };
const SG_LEG_COLORS = ["#40c4ff", "#00c853", "#e040fb", "#ffd740", "#69f0ae", "#ff6e40"];

let sgChart = null;
let sgGroups = [];
let sgSelected = null;
let sgSeries = null;

function sgTypeLabel(t) { return SG_TYPE_LABELS[t] || "CUSTOM"; }

/* --------------------------------------------------------- group list ---- */
function renderGroupList() {
  const box = document.getElementById("sg-list");
  if (!sgGroups.length) {
    box.innerHTML = `<div class="dim" style="padding:8px">No strategy groups yet.<br><br>
      Group transactions on the RECORDS page (assign to group), or use
      GROUP FROM POSITIONS in the holdings table.</div>`;
    return;
  }
  box.innerHTML = sgGroups.map(g => `
    <div class="sg-item clickable${g.id === sgSelected ? " active" : ""}" data-gid="${g.id}">
      <div class="sg-item-top"><span class="amber">${esc(g.name)}</span>
        <button class="mini" data-menu="${g.id}" title="group actions">⋮</button></div>
      <div class="sg-item-top">
        <span><span class="dim">ABS</span> <span class="num">${fmt(g.abs_value_display)}</span>${g.fx_ok ? "" :
          " <span class='tag warn'>FX?</span>"}</span>
        <span><span class="dim">P&amp;L</span> <span class="num ${valClass(g.total_display)}">${fmt(g.total_display)}</span></span>
      </div>
    </div>`).join("");
  box.querySelectorAll(".sg-item").forEach(el =>
    el.addEventListener("click", () => selectGroup(+el.dataset.gid)));
  box.querySelectorAll("[data-menu]").forEach(btn => btn.addEventListener("click", e => {
    e.stopPropagation();
    const gid = +btn.dataset.menu;
    rowMenu(btn, [
      ["EDIT", () => editGroup(gid)],
      ["DELETE", () => deleteGroup(gid)],
    ]);
  }));
}

function editGroup(gid) {
  const g = sgGroups.find(x => x.id === gid);
  if (!g) return;
  openModal("EDIT STRATEGY GROUP", `
    <label>NAME<input id="sg-ed-name" value="${esc(g.name)}"></label>
    <label>TYPE<select id="sg-ed-type">${Object.entries(SG_TYPE_LABELS).map(([v, l]) =>
      `<option value="${v}"${v === g.group_type ? " selected" : ""}>${l}</option>`).join("")}
    </select></label>
    <label>NOTE<input id="sg-ed-note" value="${esc(g.note || "")}"></label>`,
    `<button onclick="closeModal()">CANCEL</button><button id="sg-ed-save">SAVE</button>`);
  document.getElementById("sg-ed-save").onclick = async () => {
    const name = document.getElementById("sg-ed-name").value.trim();
    if (!name) { toast("name is required", "error"); return; }
    await api(`/api/txn-groups/${gid}`, { method: "PUT", body: JSON.stringify({
      name, group_type: document.getElementById("sg-ed-type").value,
      note: document.getElementById("sg-ed-note").value.trim() }) });
    closeModal();
    toast("group updated", "ok");
    await load();
  };
}

async function deleteGroup(gid) {
  const g = sgGroups.find(x => x.id === gid);
  if (!g) return;
  const ok = await confirmDialog(
    `Delete strategy group "${g.name}"? Its ${g.txn_count} transaction(s) will be ` +
    `ungrouped (not deleted).`);
  if (!ok) return;
  await api(`/api/txn-groups/${gid}`, { method: "DELETE" });
  toast("group deleted", "ok");
  if (sgSelected === gid) { sgSelected = null; sgSeries = null; }
  await load();
}

/* --------------------------------------------------------------- KPIs ---- */
function renderKpis(g) {
  const strip = document.getElementById("sg-kpis");
  document.getElementById("sg-title").textContent = g ? g.name.toUpperCase() : "SELECT A GROUP";
  document.getElementById("sg-type").textContent = g ? " " + sgTypeLabel(g.group_type) : "";
  document.getElementById("sg-note").textContent = g && g.note ? g.note : "";
  if (!g) { strip.innerHTML = ""; return; }
  const dates = (g.members || []).map(m => String(m.txn_date).slice(0, 10));
  const openLegs = (g.assets || []).filter(a => a.asset_type !== "cash" &&
                                                Math.abs(a.net_qty) > 1e-9);
  const cards = [
    ["NET CASH (EX P&L)", fmt(g.net_cash_ex_pnl_display), valClass(g.net_cash_ex_pnl_display)],
    ["CASH FLOW (INCL P&L)", fmt(g.cash_display), valClass(g.cash_display)],
    ["OPEN VALUE", fmt(g.open_value_display), ""],
    ["ABS VALUE", fmt(g.abs_value_display), ""],
    ["TOTAL P&L", fmt(g.total_display), valClass(g.total_display)],
    ["TRADES", String(g.txn_count), ""],
    ["OPEN LEGS", String(openLegs.length), ""],
    ["FIRST TRADE", dates.length ? dates[0] : "\u2014", ""],
    ["LAST TRADE", dates.length ? dates[dates.length - 1] : "\u2014", ""],
  ];
  strip.innerHTML = cards.map(([label, value, cls]) =>
    `<div class="kpi"><div class="label" title="${label}">${label}</div>
      <div class="value ${cls}" title="${esc(value)}">${esc(value)}</div></div>`).join("");
}

/* -------------------------------------------------------------- chart ---- */
/* Expand axis ranges (never shrink) so 0 sits at the same height on both. */
function sgAlignedAxes(data0, data1) {
  const span = arrs => {
    let lo = Infinity, hi = -Infinity;
    arrs.forEach(a => (a || []).forEach(v => {
      v = +v;
      if (isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    }));
    if (lo === Infinity) { lo = 0; hi = 1; }
    return [Math.min(lo, 0), Math.max(hi, 0)];
  };
  let [m0, M0] = span(data0), [m1, M1] = span(data1);
  if (M0 === m0) M0 = m0 + 1;
  if (M1 === m1) M1 = m1 + 1;
  const f0 = M0 / (M0 - m0), f1 = M1 / (M1 - m1);   // fraction of axis above zero
  // target zero-fraction reachable by pure expansion on both axes
  const t = (f0 > 0 && f0 < 1) ? f0
    : (f1 > 0 && f1 < 1) ? f1
    : (f0 !== f1) ? 0.5 : f0;                        // one-sided in opposite directions
  const alignTo = (m, M) => {
    const f = M / (M - m);
    if (f < t) M = t * (-m) / (1 - t);
    else if (f > t) m = -M * (1 - t) / t;
    return [m, M];
  };
  if (t > 0 && t < 1) {
    [m0, M0] = alignTo(m0, M0);
    [m1, M1] = alignTo(m1, M1);
  }
  return [{ min: m0, max: M0 }, { min: m1, max: M1 }];
}

function renderChart() {
  if (!sgSeries || !sgSeries.dates.length) {
    sgChart.clear();
    return;
  }
  const posMode = document.getElementById("sg-pos-mode").value;
  // keep the zoom window across refreshes; saved window on first paint
  const prev = sgChart.getOption();
  const savedZoom = chartPrefs.get("strategy").zoom || {};
  const zoom = prev && prev.dataZoom && prev.dataZoom.length
    ? { start: prev.dataZoom[0].start, end: prev.dataZoom[0].end }
    : { start: savedZoom.start, end: savedZoom.end };

  const yAxis = [{ type: "value", scale: true }];
  const series = [withEndLabel({ type: "line", name: "P&L", data: sgSeries.pnl,
    showSymbol: false, lineStyle: { color: "#ffb000", width: 2 },
    areaStyle: { color: "rgba(255,176,0,0.08)" } }, "#ffb000", 2, sgChart)];
  if (posMode !== "off") {
    yAxis.push({ type: "value", scale: true, splitLine: { show: false } });
    if (posMode === "qty" || posMode === "legval_disp" || posMode === "legval_native") {
      const field = posMode === "qty" ? "qty"
        : posMode === "legval_disp" ? "value_display" : "value_native";
      (sgSeries.legs || []).forEach((leg, i) => {
        const c = SG_LEG_COLORS[i % SG_LEG_COLORS.length];
        const name = posMode === "qty" ? `${leg.ticker} QTY`
          : posMode === "legval_native" ? `${leg.ticker} VAL ${leg.currency || ""}`.trim()
          : `${leg.ticker} VAL`;
        series.push({ type: "line", name, data: leg[field],
          yAxisIndex: 1, showSymbol: false, step: posMode === "qty" ? "end" : false,
          lineStyle: { color: c, width: 1, type: "dotted" }, itemStyle: { color: c } });
      });
    } else {
      series.push({ type: "line", name: "OPEN VALUE", data: sgSeries.open_value,
        yAxisIndex: 1, showSymbol: false,
        lineStyle: { color: "#40c4ff", width: 1, type: "dotted" },
        itemStyle: { color: "#40c4ff" } });
    }
    const [ax0, ax1] = sgAlignedAxes(
      [sgSeries.pnl],
      series.filter(s => s.yAxisIndex === 1).map(s => s.data));
    Object.assign(yAxis[0], ax0);
    Object.assign(yAxis[1], ax1);
  }
  const opt = {
    tooltip: { trigger: "axis" },
    legend: { top: 2, left: 8 },
    grid: { left: 70, right: 96, top: 28, bottom: 40 },
    xAxis: { type: "category", data: sgSeries.dates.map(formatDisplayDate) },
    yAxis,
    dataZoom: [{ type: "inside", ...zoom },
               { type: "slider", height: 16, bottom: 4, ...zoom }],
    series,
  };
  tvExtras(opt);
  sgChart.setOption(opt, true);
}

/* ------------------------------------------------------------- trades ---- */
function flowCell(amount, ticker, label, party) {
  if (!ticker) return "<td class='subtle'>\u2014</td>";
  const who = label || party || "";
  return `<td class="num">${fmtQty(amount)} <span class="dim">${esc(ticker)}</span>` +
    (who ? `<br><span class="dim">${esc(who)}</span>` : "") + "</td>";
}

function renderTrades(txns) {
  document.getElementById("sg-count").textContent = `(${txns.length})`;
  document.querySelector("#tbl-sg-trades tbody").innerHTML = txns.map(t => `
    <tr>
      <td>${formatDisplayDateTime(t.txn_date)}</td>
      ${flowCell(t.outflow_amount, t.outflow_ticker, t.outflow_account_label, t.outflow_party)}
      ${flowCell(t.inflow_amount, t.inflow_ticker, t.inflow_account_label, t.inflow_party)}
      <td class="num">${t.fee_asset_id && t.fee_amount
        ? `${fmt(t.fee_amount)} <span class="dim">${esc(t.fee_ticker || "")}</span>`
        : "<span class='subtle'>\u2014</span>"}</td>
      <td>${esc(t.note || "")}</td>
    </tr>`).join("");
}

/* ---------------------------------------------------------- open legs ---- */
function renderOpenLegs(g) {
  const legs = g ? (g.assets || []).filter(a => a.asset_type !== "cash" &&
                                                Math.abs(a.net_qty) > 1e-9) : [];
  document.getElementById("sg-legs-count").textContent = `(${legs.length})`;
  document.querySelector("#tbl-sg-legs tbody").innerHTML = legs.length ? legs.map(a => `
    <tr>
      <td class="amber">${esc(a.ticker)}</td>
      <td class="subtle">${esc(a.asset_type)}</td>
      <td class="num">${fmtQty(a.net_qty)}</td>
      <td class="num">${a.entry_px != null ? fmt(a.entry_px) : "\u2014"}</td>
      <td class="num">${a.price_native != null ? fmt(a.price_native) : "\u2014"}</td>
      <td class="num">${fmt(a.value_display)}</td>
      <td class="num ${a.earning_display == null ? "" : valClass(a.earning_display)}">${
        a.earning_display != null ? fmt(a.earning_display) : "\u2014"}</td>
    </tr>`).join("") : `<tr><td colspan="7" class="subtle">no open legs</td></tr>`;
}

/* ---------------------------------------------------------- selection ---- */
async function selectGroup(gid) {
  sgSelected = gid;
  if (chartPrefs.get("strategy").gid !== gid) chartPrefs.set("strategy", { gid });
  renderGroupList();
  const g = sgGroups.find(x => x.id === gid) || null;
  renderKpis(g);
  renderOpenLegs(g);
  const [series, trades] = await Promise.all([
    api(`/api/txn-groups/${gid}/timeseries`),
    api(`/api/transactions?group_id=${gid}&limit=1000`),
  ]);
  if (sgSelected !== gid) return;  // user clicked another group meanwhile
  sgSeries = series;
  document.getElementById("sg-ccy").textContent = `(${series.display_currency})`;
  renderChart();
  renderTrades(trades);
}

async function load() {
  const data = await api("/api/txn-groups/summary");
  sgGroups = (data.groups || []).sort((a, b) =>
    (b.abs_value_display || 0) - (a.abs_value_display || 0));
  if (sgSelected == null) {
    const urlGid = +(new URLSearchParams(location.search).get("gid") || 0) || null;
    const savedGid = chartPrefs.get("strategy").gid;
    sgSelected = (urlGid && sgGroups.some(g => g.id === urlGid)) ? urlGid
      : sgGroups.some(g => g.id === savedGid) ? savedGid
      : (sgGroups.length ? sgGroups[0].id : null);
  } else if (!sgGroups.some(g => g.id === sgSelected)) {
    sgSelected = sgGroups.length ? sgGroups[0].id : null;
  }
  renderGroupList();
  if (sgSelected != null) {
    await selectGroup(sgSelected);
  } else {
    sgSeries = null;
    renderKpis(null);
    renderOpenLegs(null);
    renderChart();
    renderTrades([]);
  }
}

/* -------------------------------------------------------------- wiring --- */
sgChart = mkChart("sg-chart");
(async function init() {
  await chartPrefs.load();
  bindControlPersist("strategy", "sg-pos-mode", "pos_mode");
  document.getElementById("sg-pos-mode").addEventListener("change", renderChart);
  bindZoomPersist(sgChart, "strategy");
  document.getElementById("sg-refresh").addEventListener("click", load);
  poll(load, 60);
})();
