/* Shared transaction edit/split modals, used by the RECORDS page and the
   holdings drill-down on HOME/HOLDINGS. Loads its own account/asset refdata
   lazily; pages that already have it can inject via txnModalsSetRefData(). */
"use strict";

const EXTERNAL = "ext";
const GROUP_TYPES = [
  ["pair", "PAIR TRADE"], ["option_strategy", "OPTION STRATEGY"],
  ["hedge", "HEDGE"], ["normal", "NORMAL"], ["custom", "CUSTOM"],
];
let tmAccounts = [];
let tmAssets = [];
let tmRefLoaded = false;

function groupTypeLabel(v) {
  const d = GROUP_TYPES.find(([val]) => val === v);
  return d ? d[1] : "CUSTOM";
}

function groupTypeSelect(id, selected = "custom") {
  return `<select id="${id}">` + GROUP_TYPES.map(([v, label]) =>
    `<option value="${v}" ${v === selected ? "selected" : ""}>${label}</option>`).join("") +
    `</select>`;
}

function txnModalsSetRefData(accounts, assets) {
  tmAccounts = accounts;
  tmAssets = assets;
  tmRefLoaded = true;
}

async function ensureTxnRefData() {
  if (tmRefLoaded) return;
  [tmAccounts, tmAssets] = await Promise.all([api("/api/accounts"), api("/api/assets?all=1")]);
  tmRefLoaded = true;
}

function assetTypeOf(assetId) {
  const a = tmAssets.find(x => x.id === assetId);
  return a ? a.asset_type : "";
}

function assetByTickerInput(id) {
  const ticker = document.getElementById(id).value.trim().toUpperCase();
  if (!ticker) return null;
  const asset = tmAssets.find(a => (a.ticker || "").toUpperCase() === ticker);
  if (!asset) throw new Error(`unknown asset: ${ticker}`);
  return asset.id;
}

function assetListOptions() {
  return tmAssets.map(a => `<option value="${esc(a.ticker)}">${esc(a.ticker)} · ${esc(a.asset_type)}</option>`).join("");
}

function partySelect(id, accountId) {
  return `<select id="${id}">
    <option value="${EXTERNAL}" ${accountId ? "" : "selected"}>— EXTERNAL —</option>` +
    tmAccounts.map(a => `<option value="${a.id}" ${a.id === accountId ? "selected" : ""}>${esc(a.label)}</option>`).join("") +
    `</select>`;
}

async function editModal(t, onSaved) {
  await ensureTxnRefData();
  openModal(`EDIT TRANSACTION #${t.id}`,
    `<div class="form-row">
       <label class="fld">DATE &amp; TIME<input type="datetime-local" id="e-date" step="60"
         value="${formatDisplayDateTimeLocal(t.txn_date)}"></label>
     </div>
     <div class="amber">OUTFLOW</div>
     <div class="form-row">
       <label class="fld">PARTY${partySelect("e-out-acc", t.outflow_account_id)}</label>
       <label class="fld">EXTERNAL NAME<input id="e-out-party" value="${esc(t.outflow_party)}"></label>
       <label class="fld">UNIT<input id="e-out-asset" list="e-out-asset-list"
         value="${esc(t.outflow_ticker || "")}" placeholder="type ticker"></label>
       <label class="fld">AMOUNT<input type="number" step="any" id="e-out-amount" value="${t.outflow_amount}"></label>
     </div>
     <div class="amber">INFLOW</div>
     <div class="form-row">
       <label class="fld">PARTY${partySelect("e-in-acc", t.inflow_account_id)}</label>
       <label class="fld">EXTERNAL NAME<input id="e-in-party" value="${esc(t.inflow_party)}"></label>
       <label class="fld">UNIT<input id="e-in-asset" list="e-in-asset-list"
         value="${esc(t.inflow_ticker || "")}" placeholder="type ticker"></label>
       <label class="fld">AMOUNT<input type="number" step="any" id="e-in-amount" value="${t.inflow_amount}"></label>
     </div>
     <div class="form-row">
       <label class="fld">FEE ACCOUNT${partySelect("e-fee-acc", t.fee_account_id)}</label>
       <label class="fld">FEE UNIT<input id="e-fee-asset" list="e-fee-asset-list"
         value="${esc(t.fee_ticker || "")}" placeholder="type ticker"></label>
       <label class="fld">FEE AMOUNT<input type="number" step="any" id="e-fee-amount" value="${t.fee_amount}"></label>
       <label class="fld" style="flex:1">NOTE<input id="e-note" value="${esc(t.note)}" style="width:100%"></label>
     </div>`,
    `<button class="primary" id="e-save">SAVE CHANGES</button>
     <datalist id="e-out-asset-list">${assetListOptions()}</datalist>
     <datalist id="e-in-asset-list">${assetListOptions()}</datalist>
     <datalist id="e-fee-asset-list">${assetListOptions()}</datalist>`);
  document.getElementById("e-save").onclick = async () => {
    const outAcc = document.getElementById("e-out-acc").value;
    const inAcc = document.getElementById("e-in-acc").value;
    const feeAcc = document.getElementById("e-fee-acc").value;
    let body;
    try {
      body = {
        txn_date: displayDateTimeLocalToUtcString(document.getElementById("e-date").value),
        outflow_account_id: outAcc === EXTERNAL ? null : +outAcc,
        outflow_party: outAcc === EXTERNAL ? document.getElementById("e-out-party").value : "",
        outflow_asset_id: assetByTickerInput("e-out-asset"),
        outflow_amount: +document.getElementById("e-out-amount").value || 0,
        inflow_account_id: inAcc === EXTERNAL ? null : +inAcc,
        inflow_party: inAcc === EXTERNAL ? document.getElementById("e-in-party").value : "",
        inflow_asset_id: assetByTickerInput("e-in-asset"),
        inflow_amount: +document.getElementById("e-in-amount").value || 0,
        fee_account_id: feeAcc === EXTERNAL ? null : +feeAcc || null,
        fee_asset_id: assetByTickerInput("e-fee-asset"),
        fee_amount: +document.getElementById("e-fee-amount").value || 0,
        note: document.getElementById("e-note").value,
      };
    } catch (e) {
      toast(e.message, "error");
      return;
    }
    await api("/api/transactions/" + t.id, { method: "PUT", body });
    toast("updated", "ok");
    closeModal();
    if (onSaved) await onSaved();
  };
}

async function splitModal(t, onSaved) {
  await ensureTxnRefData();
  const legs = [];
  if (t.outflow_asset_id && t.outflow_amount > 0)
    legs.push(["outflow", t.outflow_amount, t.outflow_ticker, assetTypeOf(t.outflow_asset_id)]);
  if (t.inflow_asset_id && t.inflow_amount > 0)
    legs.push(["inflow", t.inflow_amount, t.inflow_ticker, assetTypeOf(t.inflow_asset_id)]);
  if (!legs.length) {
    toast("nothing to split on this transaction", "error");
    return;
  }
  // security leg first so the qty input defaults to shares, not cash
  legs.sort((a, b) => (a[3] === "cash" ? 1 : 0) - (b[3] === "cash" ? 1 : 0));
  openModal(`SPLIT TRANSACTION #${t.id}`,
    `<div class="form-row">
       <label class="fld">SPLIT BY LEG
         <select id="sp-leg">${legs.map(([leg, amount, ticker]) =>
           `<option value="${leg}">${leg.toUpperCase()} — ${fmtQty(amount)} ${esc(ticker || "")}</option>`).join("")}
         </select>
       </label>
       <label class="fld">QUANTITY FOR NEW ROW<input type="number" step="any" id="sp-qty"></label>
     </div>
     <div class="dim" id="sp-preview">Enter a quantity — all other amounts (incl. fee) are prorated.</div>`,
    `<button class="primary" id="sp-save">SPLIT</button>`);
  const legAmount = () => legs.find(l => l[0] === document.getElementById("sp-leg").value)[1];
  const preview = () => {
    const q = +document.getElementById("sp-qty").value;
    const total = legAmount();
    const el = document.getElementById("sp-preview");
    if (!(q > 0 && q < total)) {
      el.innerHTML = `<span class="dim">quantity must be between 0 and ${fmtQty(total)}</span>`;
      return null;
    }
    const ratio = q / total;
    const line = (label, amount, ticker) => amount > 0
      ? `${label} ${fmtQty(amount * ratio)} + ${fmtQty(amount * (1 - ratio))} ${esc(ticker || "")}` : "";
    el.innerHTML = [`NEW ROW gets ${(ratio * 100).toFixed(2)}% · ORIGINAL keeps the rest —`,
      line("OUT:", t.outflow_amount, t.outflow_ticker),
      line("IN:", t.inflow_amount, t.inflow_ticker),
      line("FEE:", t.fee_amount, t.fee_ticker)].filter(Boolean).join(" · ");
    return q;
  };
  document.getElementById("sp-qty").addEventListener("input", preview);
  document.getElementById("sp-leg").addEventListener("change", preview);
  document.getElementById("sp-save").onclick = async () => {
    const q = preview();
    if (q === null) {
      toast("enter a valid quantity", "error");
      return;
    }
    await api(`/api/transactions/${t.id}/split`,
      { method: "POST", body: { leg: document.getElementById("sp-leg").value, quantity: q } });
    toast(`transaction #${t.id} split`, "ok");
    closeModal();
    if (onSaved) await onSaved();
  };
}
