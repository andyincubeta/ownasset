/* Holdings table: filters, grouping, sorting, CSV, column prefs, asset drawer.
   Shared by the HOME dashboard and the dedicated HOLDINGS page (standalone). */
"use strict";

const HOLDING_COLS = [
  ["asset_type", "TYPE"], ["ticker", "TICKER"], ["name", "NAME"],
  ["custodian", "CUSTODIAN"], ["account", "ACCOUNT"], ["quantity", "QTY"],
  ["price_native", "PRICE"], ["avg_cost", "BUY PRICE"], ["earning", "EARNING (NATIVE)"],
  ["earning_display", "EARNING (DISP)"],
  ["value_native", "VALUE (NATIVE)"], ["currency", "CCY"],
  ["fx_to_usd", "FX\u2192USD"], ["value_display", "VALUE (DISP)"], ["day_change_pct", "DAY %"],
  ["day_earn_pct", "DAY EARN %"],
  ["day_change_native", "DAY EARN (NATIVE)"], ["day_change_display", "DAY EARN (DISP)"],
  ["industry", "INDUSTRY"], ["sector", "SECTOR"], ["country", "COUNTRY"],
];
/* in-account display order for the default TYPE sort */
const ASSET_TYPE_ORDER = ["cash", "equity", "etf", "bond", "option", "future",
                          "crypto", "real_estate", "loan", "other"];
let prefs = {};
let holdingsData = [];
let groupsData = [];                    // summary.txn_groups (strategy groups)
let sortState = { key: "type_order", asc: true };
const expandedPositions = new Set();    // "asset:account" keys with open drill-down
const allCyclesShown = new Set();       // drill-downs showing closed cycles too
const tradesCache = new Map();          // posKey -> /position-trades response
let yahooEligibleTickers = new Set();
let yahooMapLoaded = false;
let yahooMapLoading = false;

function isYahooTickerEligible(ticker) {
  return yahooEligibleTickers.has(normalizeTicker(ticker));
}

async function loadYahooTickerMap(force = false) {
  if (!force && (yahooMapLoaded || yahooMapLoading)) return;
  yahooMapLoading = true;
  try {
    const assets = await api("/api/assets?all=1");
    yahooEligibleTickers = new Set(
      assets
        .filter(isYahooEligibleAsset)
        .map(a => normalizeTicker(a.ticker))
        .filter(Boolean));
    yahooMapLoaded = true;
    renderHoldings();
  } catch (_e) {
    yahooEligibleTickers = new Set();
  } finally {
    yahooMapLoading = false;
  }
}

async function loadPrefs() {
  const s = await api("/api/settings");
  try { prefs = JSON.parse(s.display_prefs || "{}"); } catch (_e) { prefs = {}; }
  if (typeof KPI_DEFS !== "undefined") {
    prefs.kpis = (prefs.kpis || KPI_DEFS.map(d => d[0]))
      .map(k => k === "cash_usable" ? "cash_total" : k);  // tile renamed when margin was removed
  }
  prefs.holding_cols = (prefs.holding_cols || HOLDING_COLS.map(d => d[0]))
    .map(c => c === "bank" ? "custodian" : c);  // migrate stale saved prefs
  if (!prefs.cols_v || prefs.cols_v < 5) {  // one-time: surface BUY PRICE + EARNING + DAY CHANGE columns
    if (!prefs.holding_cols.includes("avg_cost")) {
      const at = prefs.holding_cols.indexOf("price_native");
      prefs.holding_cols.splice(at >= 0 ? at + 1 : prefs.holding_cols.length, 0, "avg_cost");
    }
    if (!prefs.holding_cols.includes("earning")) {
      const at = prefs.holding_cols.indexOf("avg_cost");
      prefs.holding_cols.splice(at >= 0 ? at + 1 : prefs.holding_cols.length, 0, "earning");
    }
    if (!prefs.holding_cols.includes("earning_display")) {
      const at = prefs.holding_cols.indexOf("earning");
      prefs.holding_cols.splice(at >= 0 ? at + 1 : prefs.holding_cols.length, 0, "earning_display");
    }
    if (!prefs.holding_cols.includes("day_change_native")) {
      const at = prefs.holding_cols.indexOf("day_change_pct");
      prefs.holding_cols.splice(at >= 0 ? at + 1 : prefs.holding_cols.length, 0, "day_change_native");
    }
    if (!prefs.holding_cols.includes("day_change_display")) {
      const at = prefs.holding_cols.indexOf("day_change_native");
      prefs.holding_cols.splice(at >= 0 ? at + 1 : prefs.holding_cols.length, 0, "day_change_display");
    }
    prefs.cols_v = 5;
  }
  if (prefs.cols_v < 6) {  // one-time: surface the position-aware DAY EARN % column
    if (!prefs.holding_cols.includes("day_earn_pct")) {
      const at = prefs.holding_cols.indexOf("day_change_pct");
      prefs.holding_cols.splice(at >= 0 ? at + 1 : prefs.holding_cols.length, 0, "day_earn_pct");
    }
    prefs.cols_v = 6;
  }
  if (!prefs.holdings_group_mode) prefs.holdings_group_mode = "account";
}
async function savePrefs() {
  await api("/api/settings", { method: "POST", body: { display_prefs: JSON.stringify(prefs) } });
}

/* ------------------------------------------------ holdings (grouped) ---- */
function refreshFilterOptions() {
  const keep = (sel, opts) => {
    const cur = sel.value;
    sel.innerHTML = `<option value="">ALL</option>` +
      opts.map(o => `<option value="${esc(o.v)}">${esc(o.t)}</option>`).join("");
    sel.value = cur;
  };
  const uniq = (arr) => [...new Map(arr.map(x => [x.v, x])).values()];
  keep(document.getElementById("flt-account"),
    uniq(holdingsData.map(h => ({ v: h.account_id, t: h.account_label }))));
  keep(document.getElementById("flt-type"),
    uniq(holdingsData.map(h => ({ v: h.asset_type, t: h.asset_type.toUpperCase() }))));
  keep(document.getElementById("flt-ccy"),
    uniq(holdingsData.map(h => ({ v: h.currency, t: h.currency }))));
}

function filteredHoldings() {
  const acc = document.getElementById("flt-account").value;
  const type = document.getElementById("flt-type").value;
  const ccy = document.getElementById("flt-ccy").value;
  const q = document.getElementById("flt-q").value.trim().toLowerCase();
  return holdingsData.filter(h =>
    (!acc || String(h.account_id) === acc) &&
    (!type || h.asset_type === type) &&
    (!ccy || h.currency === ccy) &&
    (!q || [h.ticker, h.name, h.industry, h.sector, h.country, h.custodian, h.account]
      .some(v => String(v || "").toLowerCase().includes(q))));
}

function sortRows(rows) {
  const { key, asc } = sortState;
  if (key === "type_order") {
    const rank = t => { const i = ASSET_TYPE_ORDER.indexOf(t); return i < 0 ? ASSET_TYPE_ORDER.length : i; };
    return [...rows].sort((a, b) =>
      (asc ? 1 : -1) * (rank(a.asset_type) - rank(b.asset_type)) ||
      (b.value_display ?? -Infinity) - (a.value_display ?? -Infinity));
  }
  return [...rows].sort((a, b) => {
    const av = a[key], bv = b[key];
    if (typeof av === "number" || typeof bv === "number") {
      return (asc ? 1 : -1) * ((av ?? -Infinity) - (bv ?? -Infinity));
    }
    return (asc ? 1 : -1) * String(av ?? "").localeCompare(String(bv ?? ""));
  });
}

function posKey(h) { return h.asset_id + ":" + h.account_id; }

function holdDp() {
  const v = parseInt(prefs.holding_dp, 10);
  return Number.isFinite(v) ? v : 2;
}

/* group=none: one row per asset, summed across accounts */
function aggregateAcrossAccounts(rows) {
  const sum = (a, b) => (a == null && b == null ? null : (a || 0) + (b || 0));
  const byAsset = new Map();
  for (const h of rows) {
    let m = byAsset.get(h.asset_id);
    if (!m) {
      m = { ...h, groups: [...(h.groups || [])],
            _accounts: [h.account_label], _custodians: [h.custodian],
            _costQty: h.avg_cost != null ? Math.abs(h.quantity) : 0,
            _costSum: h.avg_cost != null ? h.avg_cost * Math.abs(h.quantity) : 0 };
      byAsset.set(h.asset_id, m);
      continue;
    }
    m._accounts.push(h.account_label);
    m._custodians.push(h.custodian);
    m.quantity += h.quantity;
    for (const f of ["value_native", "value_usd", "value_display", "earning",
                     "earning_display", "day_change_native", "day_change_display"]) {
      m[f] = sum(m[f], h[f]);
    }
    if (h.avg_cost != null) {
      m._costQty += Math.abs(h.quantity);
      m._costSum += h.avg_cost * Math.abs(h.quantity);
    }
    if (m.day_change_pct == null) m.day_change_pct = h.day_change_pct;
    if (m.day_earn_pct == null) m.day_earn_pct = h.day_earn_pct;
    const seen = new Set(m.groups.map(g => g.id));
    (h.groups || []).forEach(g => { if (!seen.has(g.id)) m.groups.push(g); });
  }
  return [...byAsset.values()].map(m => {
    if (m._accounts.length > 1) {
      m._multi = true;  // per-account drill-down is ambiguous on a merged row
      const uniq = a => [...new Set(a.filter(Boolean))].join(", ");
      m.account = m.account_label = uniq(m._accounts);
      m.custodian = uniq(m._custodians);
      m.avg_cost = m._costQty > 0 ? m._costSum / m._costQty : null;
    }
    return m;
  });
}

function groupMode() {
  const sel = document.getElementById("grp-mode");
  return sel ? sel.value : "account";
}

function groupBadgesHtml(h) {
  return (h.groups || []).map(g =>
    ` <span class="grp-badge" data-gid="${g.id}" title="${esc(groupTypeLabel(g.group_type))} — QTY ${fmtQty(g.qty)} — click for details">${esc(g.name)}</span>`).join("");
}

function holdingCellHtml(h, key) {
  let v = h[key], cls = "", dp = holdDp();
  if (key === "quantity") { v = fmtQty(v); cls = "num"; }
  else if (["earning", "earning_display", "day_change_native", "day_change_display"].includes(key)) {
    cls = "num " + (h[key] == null ? "" : pctClass(h[key])); v = fmt(h[key], dp);
  }
  else if (["price_native", "avg_cost", "value_native", "value_display"].includes(key)) { v = fmt(h[key], dp); cls = "num"; }
  else if (key === "fx_to_usd") { v = fmt(v, 4) + (h.fx_ok ? "" : " <span class='tag warn'>FX?</span>"); cls = "num"; }
  else if (key === "day_change_pct" || key === "day_earn_pct") { cls = "num " + pctClass(h[key]); v = fmtPct(h[key]); }
  else if (key === "ticker") {
    const k = posKey(h);
    v = (h._multi ? ""
        : `<button class="exp-btn" data-exp="${k}" title="show the buy/sell trades behind this position">${expandedPositions.has(k) ? "\u25be" : "\u25b8"}</button>`) +
        tickerWithYahooHtml(v, isYahooTickerEligible(v)) + groupBadgesHtml(h);
  }
  else v = esc(v);
  return `<td class="${cls}">${v}</td>`;
}

/* one holding row (+ its drill-down placeholder when expanded) */
function holdingRowHtml(h, cols, leading, extraCls = "") {
  const key = posKey(h);
  let html = `<tr class="clickable${extraCls}" data-aid="${h.asset_id}" data-acc="${h.account_id}">` +
    (leading ? "<td></td>" : "") +
    cols.map(([k]) => holdingCellHtml(h, k)).join("") + "</tr>";
  if (expandedPositions.has(key)) {
    html += `<tr class="pos-detail" data-pos="${key}"><td colspan="${cols.length + (leading ? 1 : 0)}">
      <div class="pos-detail-box dim">loading trades\u2026</div></td></tr>`;
  }
  return html;
}

/* a group leg shown as a holdings-style row: group-scoped qty/cost/P&L */
function legHolding(leg) {
  const base = holdingsData.find(h => h.asset_id === leg.asset_id &&
                                      h.account_id === leg.account_id) || {};
  return {
    ...base, asset_id: leg.asset_id, account_id: leg.account_id,
    ticker: leg.ticker, asset_type: leg.asset_type, currency: leg.currency,
    quantity: leg.qty, avg_cost: leg.entry_px, multiplier: leg.multiplier,
    price_native: leg.price_native != null ? leg.price_native : base.price_native,
    value_native: leg.value_native, value_display: leg.value_display, value_usd: null,
    earning: leg.earning, earning_display: leg.earning_display,
    groups: [],  // legs sit under their group header: no badges
  };
}

/* subtract grouped legs from a holding; null when nothing is left over */
function remainderHolding(h, legs, dropGids) {
  const grouped = legs.reduce((s, l) => s + l.qty, 0);
  const rem = h.quantity - grouped;
  if (Math.abs(rem) <= 1e-9) return null;
  const minus = (a, b) => (a == null || b == null ? null : a - b);
  const r = { ...h, quantity: rem, value_usd: null };
  for (const f of ["value_native", "value_display", "earning", "earning_display"]) {
    r[f] = legs.reduce((v, l) => minus(v, l[f]), h[f]);
  }
  if (dropGids) r.groups = (h.groups || []).filter(g => !dropGids.has(g.id));
  return r;
}

/* posKey -> legs across all strategy groups (for remainder math) */
function legsByPosition() {
  const map = new Map();
  for (const g of groupsData) {
    for (const l of g.legs || []) {
      const k = l.asset_id + ":" + l.account_id;
      if (!map.has(k)) map.set(k, []);
      map.get(k).push({ ...l, _gid: g.id });
    }
  }
  return map;
}

function strategyHeaderHtml(g, cols, collapsed, nested) {
  const isCollapsed = collapsed.has("g" + g.id);
  const r = x => (x == null ? "\u2014" : x.toFixed(4));
  const bits = [];
  if (g.pair) bits.push(`RATIO ${esc(g.pair.a_ticker)}/${esc(g.pair.b_ticker)} ` +
    `${r(g.pair.entry_ratio)} \u2192 ${r(g.pair.current_ratio)}`);
  if (g.group_type === "option_strategy") {
    bits.push(g.cash_display <= 0 ? `NET DEBIT ${fmt(-g.cash_display)}`
                                  : `NET CREDIT ${fmt(g.cash_display)}`);
  }
  bits.push(`COST ${fmt(g.entry_cost_display)}`);
  bits.push(`P&amp;L <span class="${valClass(g.total_display)}">${fmt(g.total_display)}</span>`);
  return `<tr class="grp-row strat-row${nested ? " nested" : ""}" data-gid="${g.id}">
    <td class="num">${fmt(g.open_value_display, holdDp())}</td>
    <td colspan="${cols.length}" class="grp-label"><span class="caret">${isCollapsed ? "\u25b8" : "\u25be"}</span>
      <span class="amber">${esc(g.name)}</span> <span class="tag">${groupTypeLabel(g.group_type)}</span>
      <span class="grp-sub">\u2014 ${bits.join(" \u00b7 ")}</span>
      <button class="mini" data-gd="${g.id}" title="group details, legs and member transactions">DETAILS</button></td></tr>`;
}

function renderStrategyMode(rows, cols, collapsed) {
  const visible = new Set(rows.map(posKey));
  const legMap = legsByPosition();
  let html = "";
  const gs = groupsData
    .filter(g => (g.legs || []).some(l => visible.has(l.asset_id + ":" + l.account_id)))
    .sort((a, b) => Math.abs(b.open_value_display || 0) - Math.abs(a.open_value_display || 0));
  for (const g of gs) {
    html += strategyHeaderHtml(g, cols, collapsed, false);
    if (collapsed.has("g" + g.id)) continue;
    html += (g.legs || []).map(l => holdingRowHtml(legHolding(l), cols, true, " leg-row")).join("");
  }
  const rem = [];
  for (const h of rows) {
    const legs = legMap.get(posKey(h));
    const rh = legs ? remainderHolding(h, legs, new Set(legs.map(l => l._gid))) : h;
    if (rh) rem.push(rh);
  }
  if (rem.length) {
    const isCollapsed = collapsed.has("ungrouped");
    const sub = rem.reduce((s, h) => s + (h.value_display || 0), 0);
    html += `<tr class="grp-row" data-ungrp="1"><td class="num">${fmt(sub, holdDp())}</td>
      <td colspan="${cols.length}" class="grp-label"><span class="caret">${isCollapsed ? "\u25b8" : "\u25be"}</span> UNGROUPED
      <span class="grp-sub">\u2014 ${rem.length} position${rem.length > 1 ? "s" : ""}</span></td></tr>`;
    if (!isCollapsed) html += sortRows(rem).map(h => holdingRowHtml(h, cols, true)).join("");
  }
  return html;
}

function renderAccountMode(rows, cols, collapsed) {
  const groups = new Map();
  rows.forEach(h => {
    if (!groups.has(h.account_id)) groups.set(h.account_id, { id: h.account_id, label: h.account_label, items: [] });
    groups.get(h.account_id).items.push(h);
  });
  // strategy groups whose legs all live in one account nest inside it
  const nestedByAcc = new Map();
  for (const g of groupsData) {
    const legs = g.legs || [];
    if (!legs.length) continue;
    const accs = new Set(legs.map(l => l.account_id));
    if (accs.size === 1) {
      const acc = legs[0].account_id;
      if (!nestedByAcc.has(acc)) nestedByAcc.set(acc, []);
      nestedByAcc.get(acc).push(g);
    }
  }
  const sortedGroups = [...groups.values()];
  sortedGroups.forEach(g => { g.subtotal = g.items.reduce((s, h) => s + (h.value_display || 0), 0); });
  sortedGroups.sort((a, b) => b.subtotal - a.subtotal);
  let html = "";
  for (const g of sortedGroups) {
    const isCollapsed = collapsed.has(g.id);
    html += `<tr class="grp-row" data-grp="${g.id}"><td class="num">${fmt(g.subtotal, holdDp())}</td>
      <td colspan="${cols.length}" class="grp-label"><span class="caret">${isCollapsed ? "\u25b8" : "\u25be"}</span> ${esc(g.label)}
      <span class="grp-sub">\u2014 ${g.items.length} position${g.items.length > 1 ? "s" : ""}</span></td></tr>`;
    if (isCollapsed) continue;
    const nested = nestedByAcc.get(g.id) || [];
    const nestedGids = new Set(nested.map(n => n.id));
    for (const ng of nested) {
      html += strategyHeaderHtml(ng, cols, collapsed, true);
      if (collapsed.has("g" + ng.id)) continue;
      html += (ng.legs || []).map(l => holdingRowHtml(legHolding(l), cols, true, " leg-row nested")).join("");
    }
    const rest = [];
    for (const h of g.items) {
      const legs = nested.flatMap(ng => (ng.legs || []).filter(l =>
        l.asset_id === h.asset_id && l.account_id === h.account_id));
      const rh = legs.length ? remainderHolding(h, legs, nestedGids) : h;
      if (rh) rest.push(rh);
    }
    html += sortRows(rest).map(h => holdingRowHtml(h, cols, true)).join("");
  }
  return html;
}

function renderHoldings() {
  if (!yahooMapLoaded && !yahooMapLoading) {
    loadYahooTickerMap();
  }
  // prefs.holding_cols is an ordered list: it drives both visibility and order
  const cols = prefs.holding_cols
    .map(key => HOLDING_COLS.find(c => c[0] === key))
    .filter(Boolean);
  const mode = groupMode();
  const leading = mode !== "none";
  const sortSel = document.getElementById("hold-sort");
  if (sortSel) sortSel.value =
    [...sortSel.options].some(o => o.value === sortState.key) ? sortState.key : "";
  const thead = document.querySelector("#tbl-holdings thead");
  const tbody = document.querySelector("#tbl-holdings tbody");
  thead.innerHTML = "<tr>" + (leading ? "<th>SUBTOTAL</th>" : "") + cols.map(([key, label]) =>
    `<th data-key="${key}">${label}${sortState.key === key ? (sortState.asc ? " \u25b2" : " \u25bc") : ""}</th>`).join("") + "</tr>";
  thead.querySelectorAll("th[data-key]").forEach(th => th.addEventListener("click", () => {
    const key = th.dataset.key;
    // 3-state cycle: desc -> asc -> back to the default order
    if (sortState.key === key) {
      if (!sortState.asc) sortState.asc = true;
      else sortState = { key: "type_order", asc: true };
    } else sortState = { key, asc: false };
    renderHoldings();
  }));

  const rows = filteredHoldings();
  const collapsed = new Set(prefs.holdings_collapsed || []);
  let html;
  if (mode === "strategy") html = renderStrategyMode(rows, cols, collapsed);
  else if (mode === "account") html = renderAccountMode(rows, cols, collapsed);
  else html = sortRows(aggregateAcrossAccounts(rows)).map(h => holdingRowHtml(h, cols, false)).join("");
  tbody.innerHTML = html;
  document.getElementById("holdings-count").textContent =
    rows.length + " / " + holdingsData.length + " POSITIONS";
  bindHoldingsRowEvents(tbody);
  tbody.querySelectorAll("tr.pos-detail").forEach(tr => fillPosDetail(tr, tr.dataset.pos));
}

function bindHoldingsRowEvents(tbody) {
  tbody.querySelectorAll("tr.clickable").forEach(tr => {
    tr.addEventListener("click", e => {
      if (e.target.closest(".ticker-link, .exp-btn, .grp-badge, button")) return;
      const h = holdingsData.find(x => x.asset_id === +tr.dataset.aid &&
                                       x.account_id === +tr.dataset.acc);
      if (h) showAssetDrawer(h);
    });
  });
  tbody.querySelectorAll(".exp-btn").forEach(btn => btn.addEventListener("click", e => {
    e.stopPropagation();
    const key = btn.dataset.exp;
    if (expandedPositions.has(key)) expandedPositions.delete(key);
    else expandedPositions.add(key);
    renderHoldings();
  }));
  tbody.querySelectorAll(".grp-badge").forEach(b => b.addEventListener("click", e => {
    e.stopPropagation();
    showGroupDrawer(+b.dataset.gid);
  }));
  tbody.querySelectorAll("button[data-gd]").forEach(b => b.addEventListener("click", e => {
    e.stopPropagation();
    showGroupDrawer(+b.dataset.gd);
  }));
  tbody.querySelectorAll("tr.grp-row td.grp-label").forEach(td => {
    td.addEventListener("click", async e => {
      if (e.target.closest("button")) return;
      const tr = td.parentElement;
      const id = tr.dataset.gid ? "g" + tr.dataset.gid
               : tr.dataset.ungrp ? "ungrouped" : +tr.dataset.grp;
      const set = new Set(prefs.holdings_collapsed || []);
      if (set.has(id)) set.delete(id); else set.add(id);
      prefs.holdings_collapsed = [...set];
      renderHoldings();
      await savePrefs();
    });
  });
}

/* ------------------------------------------- position trade drill-down --- */
function tradeRowHtml(t, data) {
  const cur = t.cycle === data.current_cycle;
  const side = t.side
    ? `<span class="${t.side === "buy" ? "green" : "red"}">${t.side.toUpperCase()}</span>`
    : `<span class="dim">XFER</span>`;
  return `<tr class="${cur ? "" : "dim"}">
    <td class="nowrap">${formatDisplayDateTime(t.txn_date)}${cur ? "" : ` <span class="tag">C${t.cycle + 1}</span>`}</td>
    <td>${side}</td>
    <td class="num ${t.delta >= 0 ? "green" : "red"}">${fmtQty(t.delta)}</td>
    <td class="num">${t.px != null ? fmt(t.px) : "\u2014"}</td>
    <td class="num">${t.fee_amount ? fmtQty(t.fee_amount) + " " + esc(t.fee_ticker || "") : ""}</td>
    <td class="num">${t.cash != null ? fmt(t.cash) : ""}</td>
    <td class="num">${fmtQty(t.qty_after)}</td>
    <td class="num">${t.avg_buy != null ? fmt(t.avg_buy) : "\u2014"}</td>
    <td class="num">${t.avg_sell != null ? fmt(t.avg_sell) : "\u2014"}</td>
    <td>${t.group_name ? `<span class="grp-badge" data-gid="${t.group_id}">${esc(t.group_name)}</span>` : ""}</td>
    <td class="subtle">${esc(t.note)}</td>
    <td class="center nowrap"><button class="mini" data-t-edit="${t.txn_id}">EDIT</button>
      <button class="mini" data-t-split="${t.txn_id}" title="split into two pro-rata parts">SPLIT</button></td>
  </tr>`;
}

async function fillPosDetail(tr, key) {
  const box = tr.querySelector(".pos-detail-box");
  let data = tradesCache.get(key);
  if (!data) {
    const [aid, acc] = key.split(":").map(Number);
    try {
      data = await api(`/api/dashboard/position-trades?asset_id=${aid}&account_id=${acc}`);
    } catch (_e) {
      box.textContent = "failed to load trade history";
      return;
    }
    tradesCache.set(key, data);
  }
  if (!document.body.contains(box)) return;  // table re-rendered meanwhile
  const showAll = allCyclesShown.has(key);
  const trades = showAll ? data.trades
    : data.trades.filter(t => t.cycle === data.current_cycle);
  const hidden = data.trades.length - trades.length;
  const sum = [
    `OPEN ${fmtQty(data.open_qty)}`,
    data.avg_buy != null ? `AVG BUY ${fmt(data.avg_buy)}` : "",
    data.avg_sell != null ? `AVG SELL ${fmt(data.avg_sell)}` : "",
    data.realized != null
      ? `REALIZED (CYCLE) <span class="${valClass(data.realized)}">${fmt(data.realized)}</span> ${esc(data.currency)}` : "",
    data.multiplier !== 1 ? `MULT \u00d7${data.multiplier}` : "",
  ].filter(Boolean).join(" \u00b7 ");
  box.classList.remove("dim");
  box.innerHTML = `
    <div class="pos-detail-head"><span class="amber">${esc(data.ticker)}</span>
      <span class="grp-sub">${sum}</span>
      ${hidden > 0 || showAll ? `<button class="mini" data-cycles="1">${showAll
        ? "CURRENT CYCLE ONLY" : `SHOW EARLIER CYCLES (${hidden})`}</button>` : ""}
      <span class="dim pos-detail-hint">BUY PRICE derives from these rows \u2014 EDIT or SPLIT a trade to correct it</span>
    </div>
    <table class="tbl pos-trades"><thead><tr>
      <th>DATE / TIME</th><th>SIDE</th><th>QTY \u0394</th><th>PRICE</th><th>FEE</th><th>CASH</th>
      <th>QTY AFTER</th><th>AVG BUY</th><th>AVG SELL</th><th>GROUP</th><th>NOTE</th><th></th>
    </tr></thead><tbody>${trades.length
      ? trades.map(t => tradeRowHtml(t, data)).join("")
      : `<tr><td colspan="12" class="dim">no transactions</td></tr>`}
    </tbody></table>`;
  const cyBtn = box.querySelector("button[data-cycles]");
  if (cyBtn) cyBtn.addEventListener("click", () => {
    if (allCyclesShown.has(key)) allCyclesShown.delete(key); else allCyclesShown.add(key);
    fillPosDetail(tr, key);
  });
  box.querySelectorAll(".grp-badge").forEach(b => b.addEventListener("click", e => {
    e.stopPropagation();
    showGroupDrawer(+b.dataset.gid);
  }));
  box.querySelectorAll("button[data-t-edit], button[data-t-split]").forEach(btn =>
    btn.addEventListener("click", async e => {
      e.stopPropagation();
      const txn = await api(`/api/transactions/${+(btn.dataset.tEdit || btn.dataset.tSplit)}`);
      const onSaved = async () => { tradesCache.delete(key); await reloadHoldings(); };
      if (btn.dataset.tEdit) editModal(txn, onSaved);
      else splitModal(txn, onSaved);
    }));
}

/* --------------------------------------------------- strategy groups ----- */
async function showGroupDrawer(gid) {
  const g = groupsData.find(x => x.id === gid);
  if (!g) {
    toast("group not found \u2014 try refreshing", "error");
    return;
  }
  let txns = [];
  try { txns = await api(`/api/transactions?group_id=${gid}&limit=1000`); } catch (_e) { /* api() toasted */ }
  const accLabel = accId => {
    const h = holdingsData.find(x => x.account_id === accId);
    return h ? h.account_label : "#" + accId;
  };
  const r = x => (x == null ? "\u2014" : x.toFixed(4));
  const stat = (k, v, cls = "") => `<tr><td class="subtle">${k}</td><td class="num ${cls}">${v}</td></tr>`;
  let statsRows =
    stat("TYPE", groupTypeLabel(g.group_type)) +
    (g.note ? stat("NOTE", esc(g.note)) : "") +
    stat("ENTRY COST (OPEN LEGS)", fmt(g.entry_cost_display)) +
    stat("OPEN VALUE", fmt(g.open_value_display)) +
    stat("CASH NET (ALL MEMBER TXNS)", fmt(g.cash_display), valClass(g.cash_display)) +
    stat("UNREALIZED (VS ENTRY)", fmt(g.unrealized_display), valClass(g.unrealized_display)) +
    stat("NET P&amp;L (CASH + OPEN)", fmt(g.total_display), valClass(g.total_display));
  if (g.pair) {
    statsRows += stat(`RATIO ${esc(g.pair.a_ticker)}/${esc(g.pair.b_ticker)} \u2014 ENTRY \u2192 NOW`,
      `${r(g.pair.entry_ratio)} \u2192 ${r(g.pair.current_ratio)}`);
    if (g.pair.weight_a != null) {
      statsRows += stat(`WEIGHTS ${esc(g.pair.a_ticker)} / ${esc(g.pair.b_ticker)}`,
        `${(g.pair.weight_a * 100).toFixed(1)}% / ${((1 - g.pair.weight_a) * 100).toFixed(1)}%`);
    }
  }
  const legRows = (g.legs || []).map(l => `<tr>
    <td>${esc(l.ticker)}</td><td class="subtle">${esc(accLabel(l.account_id))}</td>
    <td class="num ${l.qty >= 0 ? "green" : "red"}">${fmtQty(l.qty)}</td>
    <td class="num">${l.entry_px != null ? fmt(l.entry_px) : "\u2014"}</td>
    <td class="num">${l.price_native != null ? fmt(l.price_native) : "\u2014"}</td>
    <td class="num">${fmt(l.value_display)}</td>
    <td class="num ${l.earning_display == null ? "" : valClass(l.earning_display)}">${l.earning_display != null ? fmt(l.earning_display) : "\u2014"}</td>
  </tr>`).join("");
  const txnRows = txns.map(t => `<tr>
    <td class="subtle">${t.id}</td>
    <td class="nowrap">${formatDisplayDateTime(t.txn_date)}</td>
    <td class="num red">${t.outflow_asset_id ? fmtQty(t.outflow_amount) + " " + esc(t.outflow_ticker || "") : ""}</td>
    <td class="num green">${t.inflow_asset_id ? fmtQty(t.inflow_amount) + " " + esc(t.inflow_ticker || "") : ""}</td>
    <td class="subtle">${esc(t.note)}</td>
    <td class="center nowrap"><button class="mini" data-gt-edit="${t.id}">EDIT</button>
      <button class="mini" data-gt-split="${t.id}">SPLIT</button>
      <button class="mini danger" data-gt-un="${t.id}" title="remove from group (transaction is kept)">UNGROUP</button></td>
  </tr>`).join("");
  openDrawer(`${g.name} \u2014 STRATEGY GROUP`, `
    <div style="display:flex;gap:6px;margin-bottom:8px">
      <button class="mini primary" id="gd-page" title="open this strategy's performance page">STRATEGY PAGE</button>
      <button class="mini" id="gd-edit">EDIT NAME / TYPE / NOTE</button>
      <button class="mini danger" id="gd-dissolve" title="transactions are kept, just ungrouped">DISSOLVE GROUP</button>
    </div>
    <table class="tbl"><tbody>${statsRows}</tbody></table>
    <div class="amber" style="margin-top:10px">OPEN LEGS</div>
    <table class="tbl"><thead><tr><th>TICKER</th><th>ACCOUNT</th><th>QTY</th><th>ENTRY PX</th>
      <th>PRICE</th><th>VALUE (DISP)</th><th>P&amp;L (DISP)</th></tr></thead>
      <tbody>${legRows || `<tr><td colspan="7" class="dim">no open legs</td></tr>`}</tbody></table>
    <div class="amber" style="margin-top:10px">MEMBER TRANSACTIONS (${txns.length})</div>
    <table class="tbl"><thead><tr><th>ID</th><th>DATE / TIME</th><th>OUT</th><th>IN</th><th>NOTE</th><th></th></tr></thead>
      <tbody>${txnRows || `<tr><td colspan="6" class="dim">none</td></tr>`}</tbody></table>`);
  const d = document.getElementById("drawer");
  const reopen = async () => { await reloadHoldings(); showGroupDrawer(gid); };
  d.querySelector("#gd-page").addEventListener("click", () => {
    window.location = "/strategy-groups?gid=" + gid;
  });
  d.querySelector("#gd-edit").addEventListener("click", () => {
    openModal("EDIT GROUP", `<div class="form-row">
        <label class="fld">NAME<input id="gd-name" value="${esc(g.name)}"></label>
        <label class="fld">TYPE${groupTypeSelect("gd-type", g.group_type)}</label>
        <label class="fld" style="flex:1">NOTE<input id="gd-note" value="${esc(g.note || "")}" style="width:100%"></label>
      </div>`,
      `<button class="primary" id="gd-save">SAVE</button>`);
    document.getElementById("gd-save").onclick = async () => {
      await api(`/api/txn-groups/${gid}`, { method: "PUT", body: {
        name: document.getElementById("gd-name").value,
        group_type: document.getElementById("gd-type").value,
        note: document.getElementById("gd-note").value } });
      toast("saved", "ok");
      closeModal();
      await reopen();
    };
  });
  d.querySelector("#gd-dissolve").addEventListener("click", async () => {
    if (!await confirmDialog(`Dissolve group "${g.name}"? Its ${txns.length} transactions are kept, just ungrouped.`)) return;
    await api(`/api/txn-groups/${gid}`, { method: "DELETE" });
    toast("group dissolved", "ok");
    closeDrawer();
    await reloadHoldings();
  });
  d.querySelectorAll("button[data-gt-un]").forEach(btn => btn.addEventListener("click", async () => {
    await api("/api/txn-groups/unassign", { method: "POST", body: { txn_ids: [+btn.dataset.gtUn] } });
    toast("ungrouped", "ok");
    await reopen();
  }));
  d.querySelectorAll("button[data-gt-edit], button[data-gt-split]").forEach(btn =>
    btn.addEventListener("click", () => {
      const txn = txns.find(t => t.id === +(btn.dataset.gtEdit || btn.dataset.gtSplit));
      if (!txn) return;
      if (btn.dataset.gtEdit) editModal(txn, reopen);
      else splitModal(txn, reopen);
    }));
}

async function groupPositionsModal() {
  const candidates = holdingsData.filter(h => !["cash", "loan"].includes(h.asset_type));
  if (!candidates.length) {
    toast("no positions available to group", "error");
    return;
  }
  const groups = await api("/api/txn-groups");
  const rowsHtml = candidates.map(h => `<tr>
    <td class="center"><input type="checkbox" class="gp-sel" data-key="${posKey(h)}"></td>
    <td>${esc(h.ticker)}</td><td class="subtle">${esc(h.asset_type)}</td>
    <td class="subtle">${esc(h.account_label)}</td>
    <td class="num">${fmtQty(h.quantity)}</td>
    <td class="num">${fmt(h.value_display)}</td>
    <td>${(h.groups || []).map(g => `<span class="tag">${esc(g.name)}</span>`).join(" ")}</td>
  </tr>`).join("");
  openModal("GROUP POSITIONS INTO A STRATEGY",
    `<div class="dim" style="margin-bottom:6px">Each ticked position contributes the buy/sell transactions of its
       CURRENT open cycle. Works for pair trades, option strategies, hedges or any custom bundle.
       Transactions already in another group stay put (you'll get a note). Fine-tune membership later
       via the group's DETAILS drawer or the RECORDS page.</div>
     <div class="form-row">
       <label class="fld">ADD TO<select id="gp-sel-grp"><option value="">\u2014 NEW GROUP \u2014</option>${groups.map(g =>
         `<option value="${g.id}">${esc(g.name)} (${g.txn_count})</option>`).join("")}</select></label>
       <label class="fld">NEW GROUP NAME<input id="gp-name" placeholder="e.g. TSLA/F PAIR or NVDA CONDOR"></label>
       <label class="fld">TYPE${groupTypeSelect("gp-type")}</label>
       <label class="fld" style="flex:1">NOTE<input id="gp-note" style="width:100%"></label>
     </div>
     <input id="gp-q" placeholder="filter positions..." style="width:100%;margin:6px 0">
     <div style="max-height:320px;overflow:auto">
       <table class="tbl" id="gp-tbl"><thead><tr><th></th><th>TICKER</th><th>TYPE</th><th>ACCOUNT</th>
         <th>QTY</th><th>VALUE (DISP)</th><th>IN GROUPS</th></tr></thead><tbody>${rowsHtml}</tbody></table>
     </div>`,
    `<button class="primary" id="gp-save">GROUP TICKED POSITIONS</button>`);
  document.getElementById("gp-q").addEventListener("input", e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll("#gp-tbl tbody tr").forEach(tr => {
      tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none";
    });
  });
  document.getElementById("gp-save").onclick = async () => {
    const picked = [...document.querySelectorAll("#gp-tbl .gp-sel:checked")].map(cb => {
      const [aid, acc] = cb.dataset.key.split(":").map(Number);
      return { asset_id: aid, account_id: acc };
    });
    if (!picked.length) {
      toast("tick at least one position", "error");
      return;
    }
    const gid = +document.getElementById("gp-sel-grp").value;
    const body = gid ? { group_id: gid, positions: picked } : {
      name: document.getElementById("gp-name").value.trim(),
      group_type: document.getElementById("gp-type").value,
      note: document.getElementById("gp-note").value,
      positions: picked,
    };
    if (!gid && !body.name) {
      toast("enter a group name", "error");
      return;
    }
    const res = await api("/api/txn-groups/from-positions", { method: "POST", body });
    let msg = `${res.assigned} transactions grouped into "${res.name}"`;
    if (res.skipped.length) msg += ` \u2014 ${res.skipped.length} skipped (already in another group)`;
    toast(msg, res.skipped.length ? "" : "ok");
    closeModal();
    await reloadHoldings();
  };
}

async function reloadHoldings() {
  const data = await api("/api/dashboard/summary");
  setHoldingsData(data.holdings, data.txn_groups);
}

async function showAssetDrawer(h) {
  const detail = await api(`/api/assets/${h.asset_id}`);
  const info = detail.info || {};
  const core = { ...detail };
  delete core.info;
  const rows = [];
  const add = (k, v) => rows.push(`<tr><td class="subtle">${esc(k)}</td><td>${esc(
    typeof v === "number" ? fmt(v, 4) : v)}</td></tr>`);
  Object.entries(core).forEach(([k, v]) => {
    if (v !== null && typeof v !== "object") add(k, v);
  });
  if (detail.derivative) Object.entries(detail.derivative).forEach(([k, v]) => add("deriv." + k, v));
  Object.entries(info).sort().forEach(([k, v]) => { if (v !== null && v !== "DETAILS") add(k, v); });
  const canSrc = !["cash", "loan"].includes(detail.asset_type);
  const effSrc = !detail.is_yahoo_tracked ? "manual"
    : (detail.price_provider === "option_archive" ? "option_archive" : "yahoo");
  const srcOpts = ["manual", "yahoo"];
  if (detail.asset_type === "option") srcOpts.push("option_archive");
  const srcHtml = canSrc ? `
    <div class="form-row" style="margin-bottom:6px;align-items:end">
      <label class="fld">PRICE SOURCE<select id="drawer-src">${srcOpts.map(s =>
        `<option value="${s}" ${s === effSrc ? "selected" : ""}>${s.replace("_", " ").toUpperCase()}</option>`).join("")}
      </select></label>
      <button class="mini" id="drawer-src-apply">APPLY</button>
      <span class="dim" id="drawer-src-msg"></span>
    </div>` : "";
  openDrawer(`${h.ticker} — ALL FIELDS`, `${srcHtml}
    <input id="drawer-filter" placeholder="filter fields..." style="width:100%;margin-bottom:6px">
    <table class="tbl" id="drawer-tbl"><tbody>${rows.join("")}</tbody></table>`);
  if (canSrc) document.getElementById("drawer-src-apply").addEventListener("click", async () => {
    const src = document.getElementById("drawer-src").value;
    if (src === effSrc) return;
    const msg = document.getElementById("drawer-src-msg");
    msg.textContent = src === "option_archive" ? "fetching archive…" : "switching…";
    try {
      const res = await api(`/api/assets/${h.asset_id}/switch-source`, {
        method: "POST", body: { source: src } });
      toast(src === "option_archive"
        ? `${h.ticker}: ${res.bars} daily bars loaded from archive, snapshots resynced`
        : `${h.ticker}: price source set to ${src}`, "ok");
      msg.textContent = "done";
    } catch (_e) {
      msg.textContent = "";  // api() already toasted the error
    }
  });
  document.getElementById("drawer-filter").addEventListener("input", e => {
    const q = e.target.value.toLowerCase();
    document.querySelectorAll("#drawer-tbl tr").forEach(tr => {
      tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none";
    });
  });
}

function setHoldingsData(rows, groups) {
  holdingsData = rows;
  groupsData = groups || [];
  tradesCache.clear();  // fresh data: expanded drill-downs refetch
  refreshFilterOptions();
  renderHoldings();
}

/* columns checklist markup shared by the dashboard and holdings modals */
function colsChecklistHtml(defs, group, selected) {
  return defs.map(([id, label]) =>
    `<label style="display:flex;gap:4px;align-items:center;text-transform:none">
      <input type="checkbox" data-group="${group}" value="${id}" ${selected.includes(id) ? "checked" : ""}>${label}</label>`).join("");
}
function readChecklist(group) {
  return Array.from(document.querySelectorAll(`input[data-group="${group}"]:checked`)).map(i => i.value);
}

function bindHoldingsControls() {
  const gm = document.getElementById("grp-mode");
  gm.value = prefs.holdings_group_mode || "account";
  gm.addEventListener("change", async () => {
    prefs.holdings_group_mode = gm.value;
    renderHoldings();
    await savePrefs();
  });
  const dpSel = document.getElementById("hold-dp");
  if (dpSel) {
    dpSel.value = String(holdDp());
    dpSel.addEventListener("change", async () => {
      prefs.holding_dp = +dpSel.value;
      renderHoldings();
      await savePrefs();
    });
  }
  ["flt-account", "flt-type", "flt-ccy"].forEach(id =>
    document.getElementById(id).addEventListener("change", renderHoldings));
  document.getElementById("flt-q").addEventListener("input", renderHoldings);
  const sortSel = document.getElementById("hold-sort");
  if (sortSel) sortSel.addEventListener("change", () => {
    const key = sortSel.value;
    if (!key) return;
    sortState = { key, asc: key === "type_order" || key === "ticker" };
    renderHoldings();
  });
  document.getElementById("btn-group-sel").addEventListener("click", groupPositionsModal);
  document.getElementById("btn-holdings-csv").addEventListener("click", () => {
    window.location = "/api/dashboard/holdings.csv";
  });
}

/* ---------------------------------------------- standalone page mode ---- */
function holdingsColumnsModal() {
  const overlay = openModal("VISIBLE COLUMNS — tick to show, ▲▼ to reorder",
    colsOrderHtml(HOLDING_COLS, "holding_cols", prefs.holding_cols),
    `<button class="primary" id="cols-save">SAVE</button>`);
  bindColsOrder(overlay);
  document.getElementById("cols-save").onclick = async () => {
    prefs.holding_cols = readColsOrder("holding_cols");
    await savePrefs();
    closeModal();
    renderHoldings();
  };
}

/* self-init on the dedicated HOLDINGS page (dashboard.js drives HOME) */
if (document.getElementById("tbl-holdings") && !document.getElementById("kpi-strip")) {
  (async function initHoldingsPage() {
    await loadPrefs();
    bindHoldingsControls();
    document.getElementById("btn-columns").addEventListener("click", holdingsColumnsModal);
    poll(async () => {
      const data = await api("/api/dashboard/summary");
      setHoldingsData(data.holdings, data.txn_groups);
    }, 60);
  })();
}
