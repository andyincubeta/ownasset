/* Transaction records blotter: filters, per-row edit/split, bulk delete via SEL column.
   Parties are accounts (amber) or external labels (dim).
   Edit/split modals live in txn_modals.js (shared with holdings drill-down). */
"use strict";

const RECORDS_COLS = [
  ["sel", "SEL"], ["id", "ID"], ["txn_date", "DATE / TIME"],
  ["outflow_party", "OUTFLOW PARTY"], ["outflow_amount", "OUTFLOW AMOUNT"], ["outflow_ticker", "OUTFLOW UNIT"],
  ["inflow_party", "INFLOW PARTY"], ["inflow_amount", "INFLOW AMOUNT"], ["inflow_ticker", "INFLOW UNIT"],
  ["fee", "FEE"], ["note", "NOTE"], ["group", "GROUP"], ["imported", "IMP"],
  ["updated_at", "UPDATED"], ["actions", "ACTIONS"],
];
let accounts = [], assets = [], rows = [];
let prefs = {};
let yahooEligibleTickers = new Set();
let groupsSummaryCache = null;

async function loadPrefs() {
  const s = await api("/api/settings");
  try { prefs = JSON.parse(s.display_prefs || "{}"); } catch (_e) { prefs = {}; }
  prefs.records_cols = prefs.records_cols || RECORDS_COLS.map(d => d[0]);
  // saved prefs predating the sel/group columns get them injected
  if (!prefs.records_cols.includes("sel")) prefs.records_cols.unshift("sel");
  if (!prefs.records_cols.includes("group")) {
    const ai = prefs.records_cols.indexOf("actions");
    prefs.records_cols.splice(ai >= 0 ? ai : prefs.records_cols.length, 0, "group");
  }
  if (!prefs.records_cols.includes("imported")) {
    const gi = prefs.records_cols.indexOf("group");
    prefs.records_cols.splice(gi >= 0 ? gi + 1 : prefs.records_cols.length, 0, "imported");
  }
}
async function savePrefs() {
  await api("/api/settings", { method: "POST", body: { display_prefs: JSON.stringify(prefs) } });
}

function rebuildYahooTickerMap() {
  yahooEligibleTickers = new Set(
    assets
      .filter(isYahooEligibleAsset)
      .map(a => normalizeTicker(a.ticker))
      .filter(Boolean));
}

function tickerCellHtml(ticker) {
  const t = normalizeTicker(ticker);
  if (!t) return "";
  return tickerWithYahooHtml(t, yahooEligibleTickers.has(t));
}

async function loadFilters() {
  [accounts, assets] = await Promise.all([api("/api/accounts"), api("/api/assets?all=1")]);
  txnModalsSetRefData(accounts, assets);
  rebuildYahooTickerMap();
  document.getElementById("f-account").innerHTML = `<option value="">ALL</option>` +
    accounts.map(a => `<option value="${a.id}">${esc(a.label)}</option>`).join("");
  document.getElementById("f-asset-list").innerHTML = assets.map(a =>
    `<option value="${esc(a.ticker)}">${esc(a.ticker)} · ${esc(a.asset_type)}</option>`).join("");
}

function partyCell(label, text) {
  return label ? `<span class="amber">${esc(label)}</span>`
               : `<span class="subtle">${esc(text || "—")}</span>`;
}

function recordCellHtml(t, key, i) {
  switch (key) {
    case "sel": return `<td class="center"><input type="checkbox" class="rec-sel" data-id="${t.id}"></td>`;
    case "id": return `<td class="subtle">${t.id}</td>`;
    case "txn_date": return `<td class="nowrap">${formatDisplayDateTime(t.txn_date)}</td>`;
    case "outflow_party": return `<td>${partyCell(t.outflow_account_label, t.outflow_party)}</td>`;
    case "outflow_amount": return `<td class="num red">${t.outflow_asset_id ? fmtQty(t.outflow_amount) : ""}</td>`;
    case "outflow_ticker": return `<td>${tickerCellHtml(t.outflow_ticker)}</td>`;
    case "inflow_party": return `<td>${partyCell(t.inflow_account_label, t.inflow_party)}</td>`;
    case "inflow_amount": return `<td class="num green">${t.inflow_asset_id ? fmtQty(t.inflow_amount) : ""}</td>`;
    case "inflow_ticker": return `<td>${tickerCellHtml(t.inflow_ticker)}</td>`;
    case "fee": return `<td class="num" title="${esc(t.fee_account_label || "")}">${t.fee_asset_id && t.fee_amount
      ? fmtQty(t.fee_amount) + " " + esc(t.fee_ticker) : ""}</td>`;
    case "note": return `<td class="subtle">${esc(t.note)}</td>`;
    case "group": return `<td>${t.group_name ? `<span class="amber">${esc(t.group_name)}</span>` : ""}</td>`;
    case "imported": return `<td class="center">${t.import_batch_id
      ? `<span class="tag" title="imported from file (batch #${t.import_batch_id})">IMP</span>` : ""}</td>`;
    case "updated_at": return `<td class="subtle nowrap">${formatDisplayDateTime(t.updated_at)}</td>`;
    case "actions": return `<td class="center nowrap"><button class="mini" data-edit="${i}">EDIT</button>
      <button class="mini" data-split="${i}" title="split into two pro-rata parts">SPLIT</button></td>`;
    default: return "<td></td>";
  }
}

function renderRows() {
  const cols = prefs.records_cols
    .map(key => RECORDS_COLS.find(c => c[0] === key))
    .filter(Boolean);
  document.querySelector("#tbl-records thead").innerHTML =
    "<tr>" + cols.map(([, label]) => `<th>${label}</th>`).join("") + "</tr>";
  document.querySelector("#tbl-records tbody").innerHTML = rows.map((t, i) =>
    "<tr>" + cols.map(([key]) => recordCellHtml(t, key, i)).join("") + "</tr>").join("");
  document.querySelectorAll("#tbl-records button[data-edit]").forEach(btn =>
    btn.addEventListener("click", e => {
      e.stopPropagation();
      editModal(rows[+btn.dataset.edit], loadRows);
    }));
  document.querySelectorAll("#tbl-records button[data-split]").forEach(btn =>
    btn.addEventListener("click", e => {
      e.stopPropagation();
      splitModal(rows[+btn.dataset.split], loadRows);
    }));
  // click anywhere on a row toggles its SEL checkbox (and the highlight)
  const syncSel = cb => cb.closest("tr").classList.toggle("sel-row", cb.checked);
  document.querySelectorAll("#tbl-records tbody tr").forEach(tr => {
    tr.addEventListener("click", e => {
      if (e.target.closest("button, input, select, a, label")) return;
      const cb = tr.querySelector(".rec-sel");
      if (!cb) return;
      cb.checked = !cb.checked;
      syncSel(cb);
    });
  });
  document.querySelectorAll("#tbl-records .rec-sel").forEach(cb =>
    cb.addEventListener("change", () => syncSel(cb)));
  makeSortable(document.getElementById("tbl-records"));
}

async function loadRows() {
  const q = new URLSearchParams();
  const acc = document.getElementById("f-account").value;
  const assetTicker = document.getElementById("f-asset").value.trim().toUpperCase();
  const start = document.getElementById("f-start").value;
  const end = document.getElementById("f-end").value;
  const text = document.getElementById("f-q").value;
  if (acc) q.set("account_id", acc);
  if (assetTicker) {
    const asset = assets.find(a => (a.ticker || "").toUpperCase() === assetTicker);
    if (!asset) {
      toast(`unknown asset: ${assetTicker}`, "error");
      return;
    }
    q.set("asset_id", asset.id);
  }
  if (start) q.set("start", start);
  if (end) q.set("end", end);
  if (text) q.set("q", text);
  rows = await api("/api/transactions?" + q);
  document.getElementById("rec-count").textContent = rows.length + " ROWS";
  renderRows();
}

function recordsColumnsModal() {
  const overlay = openModal("VISIBLE COLUMNS — tick to show, ▲▼ to reorder",
    colsOrderHtml(RECORDS_COLS, "records_cols", prefs.records_cols),
    `<button class="primary" id="cols-save">SAVE</button>`);
  bindColsOrder(overlay);
  document.getElementById("cols-save").onclick = async () => {
    prefs.records_cols = readColsOrder("records_cols");
    await savePrefs();
    closeModal();
    renderRows();
  };
}

/* ------------------------------------------------------------ grouping --- */
function selectedTxnIds() {
  return [...document.querySelectorAll("#tbl-records .rec-sel:checked")].map(cb => +cb.dataset.id);
}

async function groupSelectedModal() {
  const ids = selectedTxnIds();
  if (!ids.length) {
    toast("tick at least one row (SEL column)", "error");
    return;
  }
  const groups = await api("/api/txn-groups");
  openModal(`GROUP ${ids.length} TRANSACTION${ids.length > 1 ? "S" : ""}`,
    `<div class="form-row">
       <label class="fld">EXISTING GROUP
         <select id="g-sel"><option value="">— NEW GROUP —</option>${groups.map(g =>
           `<option value="${g.id}">${esc(g.name)} (${g.txn_count})</option>`).join("")}
         </select>
       </label>
       <label class="fld">NEW GROUP NAME<input id="g-name" placeholder="e.g. TSLA/F PAIR"></label>
       <label class="fld">TYPE${groupTypeSelect("g-type")}</label>
     </div>`,
    `<button class="primary" id="g-save">ASSIGN</button>`);
  document.getElementById("g-save").onclick = async () => {
    let gid = +document.getElementById("g-sel").value;
    if (!gid) {
      const name = document.getElementById("g-name").value.trim();
      if (!name) {
        toast("enter a group name", "error");
        return;
      }
      gid = (await api("/api/txn-groups", { method: "POST", body: {
        name, group_type: document.getElementById("g-type").value } })).id;
    }
    await api(`/api/txn-groups/${gid}/assign`, { method: "POST", body: { txn_ids: ids } });
    toast(`${ids.length} transactions grouped`, "ok");
    closeModal();
    loadRows();
  };
}

async function ungroupSelected() {
  const ids = selectedTxnIds();
  if (!ids.length) {
    toast("tick at least one row (SEL column)", "error");
    return;
  }
  await api("/api/txn-groups/unassign", { method: "POST", body: { txn_ids: ids } });
  toast(`${ids.length} transactions ungrouped`, "ok");
  loadRows();
}

async function deleteSelected() {
  const ids = selectedTxnIds();
  if (!ids.length) {
    toast("tick at least one row (SEL column)", "error");
    return;
  }
  if (!await confirmDialog(`Delete ${ids.length} transaction${ids.length > 1 ? "s" : ""}? This cannot be undone.`)) return;
  const res = await api("/api/transactions/delete-many", { method: "POST", body: { txn_ids: ids } });
  toast(`${res.deleted} transactions deleted`, "ok");
  loadRows();
}

/* ------------------------------------------------------------ groups view */
function groupMemberRowHtml(t) {
  return `<tr>
    <td class="subtle">${t.id}</td>
    <td class="nowrap">${formatDisplayDateTime(t.txn_date)}</td>
    <td>${partyCell(t.outflow_account_label, t.outflow_party)}</td>
    <td class="num red">${t.outflow_asset_id ? fmtQty(t.outflow_amount) + " " + esc(t.outflow_ticker || "") : ""}</td>
    <td>${partyCell(t.inflow_account_label, t.inflow_party)}</td>
    <td class="num green">${t.inflow_asset_id ? fmtQty(t.inflow_amount) + " " + esc(t.inflow_ticker || "") : ""}</td>
    <td class="num">${t.fee_asset_id && t.fee_amount ? fmtQty(t.fee_amount) + " " + esc(t.fee_ticker || "") : ""}</td>
    <td class="subtle">${esc(t.note)}</td>
    <td class="center"><button class="mini" data-gm-ungroup="${t.id}">UNGROUP</button></td>
  </tr>`;
}

function groupAssetRowHtml(a) {
  const qtyCls = a.net_qty > 0 ? "green" : (a.net_qty < 0 ? "red" : "subtle");
  return `<tr>
    <td>${tickerCellHtml(a.ticker) || esc(a.ticker)}</td>
    <td class="subtle">${esc(a.asset_type)}</td>
    <td class="num ${qtyCls}">${fmtQty(a.net_qty)}</td>
    <td class="num">${a.price_native != null ? fmt(a.price_native) : "—"}
      <span class="subtle">${esc(a.price_source || "")}</span></td>
    <td class="num ${valClass(a.value_display)}">${fmt(a.value_display)}</td>
  </tr>`;
}

function renderGroupsView() {
  const data = groupsSummaryCache;
  if (!data) return;
  const mode = document.getElementById("grp-display").value;
  const ccy = data.display_currency || "USD";
  const container = document.getElementById("groups-container");
  if (!data.groups.length) {
    container.innerHTML = `<div class="dim" style="padding:12px">No groups yet — tick rows in the LEDGER view and press GROUP SEL.</div>`;
    return;
  }
  container.innerHTML = data.groups.map(g => {
    const fxWarn = g.fx_ok ? "" : ` <span class="red" title="an FX rate is missing; values may be off">FX?</span>`;
    const head = `<div class="panel-title">
      <span class="amber">${esc(g.name)}</span>&nbsp;<span class="tag">${groupTypeLabel(g.group_type)}</span>&nbsp;<span class="dim">${g.txn_count} TXNS</span>
      <span class="spacer"></span>
      <span class="dim">CASH NET</span>&nbsp;<span class="${valClass(g.cash_display)}">${fmt(g.cash_display)}</span>&nbsp;
      <span class="dim">OPEN</span>&nbsp;<span class="${valClass(g.open_value_display)}">${fmt(g.open_value_display)}</span>&nbsp;
      <span class="dim">TOTAL P&amp;L</span>&nbsp;<span class="${valClass(g.total_display)}">${fmt(g.total_display)} ${esc(ccy)}</span>${fxWarn}&nbsp;
      <button class="mini" data-g-rename="${g.id}">RENAME</button>
      <button class="mini danger" data-g-del="${g.id}" title="dissolve group; transactions are kept">DISSOLVE</button>
    </div>`;
    const table = mode === "aggregated"
      ? `<table class="tbl">
           <thead><tr><th>ASSET</th><th>TYPE</th><th>NET QTY</th><th>PRICE (NATIVE)</th><th>VALUE (${esc(ccy)})</th></tr></thead>
           <tbody>${g.assets.map(groupAssetRowHtml).join("")}</tbody>
         </table>`
      : `<table class="tbl">
           <thead><tr><th>ID</th><th>DATE / TIME</th><th>OUT PARTY</th><th>OUT</th>
             <th>IN PARTY</th><th>IN</th><th>FEE</th><th>NOTE</th><th></th></tr></thead>
           <tbody>${g.members.map(groupMemberRowHtml).join("")}</tbody>
         </table>`;
    return `<div class="panel" style="margin-bottom:10px">${head}<div class="tbl-wrap">${table}</div></div>`;
  }).join("");
  container.querySelectorAll("button[data-gm-ungroup]").forEach(btn =>
    btn.addEventListener("click", async () => {
      await api("/api/txn-groups/unassign", { method: "POST", body: { txn_ids: [+btn.dataset.gmUngroup] } });
      toast("ungrouped", "ok");
      loadGroupsView();
    }));
  container.querySelectorAll("button[data-g-rename]").forEach(btn =>
    btn.addEventListener("click", () => {
      const g = data.groups.find(x => x.id === +btn.dataset.gRename);
      openModal(`RENAME GROUP`, `<div class="form-row">
          <label class="fld">NAME<input id="gr-name" value="${esc(g.name)}"></label>
          <label class="fld">TYPE${groupTypeSelect("gr-type", g.group_type)}</label>
          <label class="fld" style="flex:1">NOTE<input id="gr-note" value="${esc(g.note || "")}" style="width:100%"></label>
        </div>`,
        `<button class="primary" id="gr-save">SAVE</button>`);
      document.getElementById("gr-save").onclick = async () => {
        await api(`/api/txn-groups/${g.id}`, { method: "PUT", body: {
          name: document.getElementById("gr-name").value,
          group_type: document.getElementById("gr-type").value,
          note: document.getElementById("gr-note").value } });
        toast("saved", "ok");
        closeModal();
        loadGroupsView();
      };
    }));
  container.querySelectorAll("button[data-g-del]").forEach(btn =>
    btn.addEventListener("click", async () => {
      const g = data.groups.find(x => x.id === +btn.dataset.gDel);
      if (!await confirmDialog(`Dissolve group "${g.name}"? Its ${g.txn_count} transactions are kept, just ungrouped.`)) return;
      await api(`/api/txn-groups/${g.id}`, { method: "DELETE" });
      toast("group dissolved", "ok");
      loadGroupsView();
    }));
}

async function loadGroupsView() {
  groupsSummaryCache = await api("/api/txn-groups/summary");
  renderGroupsView();
}

function syncViewMode() {
  const mode = document.getElementById("view-mode").value;
  document.getElementById("ledger-wrap").style.display = mode === "ledger" ? "" : "none";
  document.getElementById("groups-wrap").style.display = mode === "groups" ? "" : "none";
  if (mode === "groups") loadGroupsView();
}

document.getElementById("f-apply").addEventListener("click", loadRows);
document.getElementById("f-q").addEventListener("keydown", e => { if (e.key === "Enter") loadRows(); });
document.getElementById("f-cols").addEventListener("click", recordsColumnsModal);
document.getElementById("f-csv").addEventListener("click", () => {
  window.location = "/api/transactions/csv";
});
document.getElementById("f-group").addEventListener("click", groupSelectedModal);
document.getElementById("f-ungroup").addEventListener("click", ungroupSelected);
document.getElementById("f-del").addEventListener("click", deleteSelected);
document.getElementById("view-mode").addEventListener("change", syncViewMode);
document.getElementById("grp-display").addEventListener("change", renderGroupsView);

(async function init() { await Promise.all([loadFilters(), loadPrefs()]); loadRows(); })();
