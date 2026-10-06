/* Disposable income: non-investment cash added to the portfolio.
   Entries are deposit transactions (party DISPOSABLE INCOME); the return
   index strips them as external flows. */
"use strict";

let diEntries = [], diAccounts = [], diCashAssets = [];

function accOptions(selectedId) {
  return diAccounts.map(a =>
    `<option value="${a.id}"${a.id === selectedId ? " selected" : ""}>${esc(a.label)}</option>`).join("");
}

function assetOptions(selectedId) {
  return diCashAssets.map(a =>
    `<option value="${a.id}"${a.id === selectedId ? " selected" : ""}>${esc(a.ticker)} (${esc(a.currency)})</option>`).join("");
}

async function loadRefs() {
  [diAccounts, diCashAssets] = await Promise.all([
    api("/api/accounts"),
    api("/api/assets").then(all => all.filter(a => a.asset_type === "cash")),
  ]);
  document.getElementById("di-account").innerHTML = accOptions(null);
  document.getElementById("di-asset").innerHTML = assetOptions(null);
}

async function loadEntries() {
  const res = await api("/api/disposable");
  diEntries = res.entries;
  document.getElementById("di-ccy").textContent = res.display_currency;
  document.getElementById("di-total").textContent = fmt(res.total_display);
  document.getElementById("di-count").textContent = `(${diEntries.length})`;
  document.querySelector("#tbl-disposable tbody").innerHTML = diEntries.map((e, i) => `
    <tr>
      <td>${formatDisplayDate(e.txn_date)}</td>
      <td>${esc(e.account_label)}</td>
      <td class="num">${fmt(e.amount)} <span class="dim">${esc(e.currency)}</span></td>
      <td class="num">${fmt(e.amount_display)}${e.fx_ok ? "" : " <span class='tag warn'>FX?</span>"}</td>
      <td class="subtle">${esc(e.note)}</td>
      <td class="center"><button class="mini" data-i="${i}">⋮</button></td>
    </tr>`).join("");
  document.querySelectorAll("#tbl-disposable button[data-i]").forEach(btn => {
    const e = diEntries[+btn.dataset.i];
    btn.addEventListener("click", ev => {
      ev.stopPropagation();
      rowMenu(btn, [
        ["EDIT", () => editEntry(e)],
        ["DELETE", async () => {
          if (await confirmDialog(
            `Delete income ${formatDisplayDate(e.txn_date)} = ${fmt(e.amount)} ${e.currency}? ` +
            `Snapshots will be rebuilt from that date.`)) {
            await api("/api/disposable/" + e.id, { method: "DELETE" });
            toast("deleted", "ok");
            loadEntries();
          }
        }],
      ]);
    });
  });
}

function editEntry(e) {
  openModal("EDIT DISPOSABLE INCOME",
    `<div class="form-row">
       <label class="fld">DATE<input type="date" id="de-date" value="${(e.txn_date || "").slice(0, 10)}"></label>
       <label class="fld">ACCOUNT<select id="de-account">${accOptions(e.account_id)}</select></label>
       <label class="fld">CASH ASSET<select id="de-asset">${assetOptions(e.asset_id)}</select></label>
       <label class="fld">AMOUNT<input type="number" step="any" id="de-amount" value="${e.amount}"></label>
       <label class="fld">NOTE<input id="de-note" value="${esc(e.note)}"></label>
     </div>`,
    `<button class="primary" id="de-save">SAVE</button>`);
  document.getElementById("de-save").onclick = async () => {
    await api("/api/disposable/" + e.id, { method: "PUT", body: {
      txn_date: document.getElementById("de-date").value,
      account_id: +document.getElementById("de-account").value,
      asset_id: +document.getElementById("de-asset").value,
      amount: +document.getElementById("de-amount").value,
      note: document.getElementById("de-note").value } });
    toast("saved", "ok"); closeModal(); loadEntries();
  };
}

document.getElementById("di-add").addEventListener("click", async () => {
  const amount = +document.getElementById("di-amount").value;
  if (!(amount > 0)) { toast("amount must be > 0", "error"); return; }
  await api("/api/disposable", { method: "POST", body: {
    txn_date: document.getElementById("di-date").value,
    account_id: +document.getElementById("di-account").value,
    asset_id: +document.getElementById("di-asset").value,
    amount,
    note: document.getElementById("di-note").value } });
  toast("income added, snapshots rebuilt", "ok");
  document.getElementById("di-amount").value = "";
  document.getElementById("di-note").value = "";
  loadEntries();
});

(async function init() {
  document.getElementById("di-date").value = new Date().toISOString().slice(0, 10);
  await loadRefs();
  await loadEntries();
})();
