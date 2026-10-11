/* Transaction entry: outflow/inflow party accounts (or external), datetime,
   fee account auto-follow, template presets, inline add modals. */
"use strict";

const EXTERNAL = "ext";
let accounts = [], assets = [], currencies = [];
let recentRows = [];
let importPreviewState = null;
let importPreviewPayload = null;
let tickerOverrides = {};  // preview row number -> user-entered ticker
let yahooEligibleTickers = new Set();

function rebuildYahooTickerMap() {
  yahooEligibleTickers = new Set(
    assets
      .filter(isYahooEligibleAsset)
      .map(a => normalizeTicker(a.ticker))
      .filter(Boolean));
}

function tickerWithYahoo(ticker) {
  const t = normalizeTicker(ticker);
  if (!t) return "";
  return tickerWithYahooHtml(t, yahooEligibleTickers.has(t));
}

function qtyAndTickerHtml(amount, ticker) {
  const t = normalizeTicker(ticker);
  if (!t) return "—";
  return `${fmtQty(amount)} ${tickerWithYahoo(t)}`;
}

function resetImportPreviewUi() {
  importPreviewState = null;
  importPreviewPayload = null;
  tickerOverrides = {};
  document.getElementById("imp-commit").disabled = true;
  document.getElementById("imp-include-dups").checked = false;
  document.getElementById("imp-summary").textContent = "";
  document.querySelector("#tbl-import-preview tbody").innerHTML = "";
  document.getElementById("imp-preview-wrap").style.display = "none";
  document.getElementById("imp-unknown-wrap").style.display = "none";
  document.querySelector("#tbl-imp-unknown tbody").innerHTML = "";
  document.getElementById("imp-filter").value = "all";
}

function assetByTickerInput(id) {
  const ticker = document.getElementById(id).value.trim().toUpperCase();
  if (!ticker) return null;
  const asset = assets.find(a => (a.ticker || "").toUpperCase() === ticker);
  if (!asset) throw new Error(`unknown asset: ${ticker}`);
  return asset.id;
}

function partyOptions(sel, selected) {
  sel.innerHTML = `<option value="${EXTERNAL}">— EXTERNAL —</option>` + accounts.map(a =>
    `<option value="${a.id}" ${String(a.id) === String(selected) ? "selected" : ""}>${esc(a.label)}</option>`).join("");
}

function syncPartyVisibility() {
  document.getElementById("txn-out-party-wrap").classList.toggle(
    "hidden", document.getElementById("txn-out-acc").value !== EXTERNAL);
  document.getElementById("txn-in-party-wrap").classList.toggle(
    "hidden", document.getElementById("txn-in-acc").value !== EXTERNAL);
}

async function loadRefData() {
  [accounts, assets, currencies] = await Promise.all([
    api("/api/accounts"), api("/api/assets"), api("/api/currencies")]);
  rebuildYahooTickerMap();
  const outSel = document.getElementById("txn-out-acc");
  const inSel = document.getElementById("txn-in-acc");
  partyOptions(outSel, outSel.value || (accounts[0] && accounts[0].id));
  partyOptions(inSel, inSel.value || (accounts[0] && accounts[0].id));
  const feeSel = document.getElementById("txn-fee-acc");
  const feeCurrent = feeSel.value;
  feeSel.innerHTML = `<option value="">— AUTO (outflow party) —</option>` + accounts.map(a =>
    `<option value="${a.id}">${esc(a.label)}</option>`).join("");
  feeSel.value = feeCurrent;
  document.getElementById("asset-suggestions").innerHTML = assets.map(a =>
    `<option value="${esc(a.ticker)}">${esc(a.ticker)} · ${esc(a.asset_type)}</option>`).join("");
  const impAccount = document.getElementById("imp-account");
  if (impAccount) {
    const selected = impAccount.value;
    impAccount.innerHTML = `<option value="">— SELECT ACCOUNT —</option>` + accounts.map(a =>
      `<option value="${a.id}" ${String(a.id) === String(selected) ? "selected" : ""}>${esc(a.label)}</option>`).join("");
    if (!impAccount.value && accounts[0]) impAccount.value = String(accounts[0].id);
  }
  syncPartyVisibility();
}

async function loadRecent() {
  recentRows = await api("/api/transactions?limit=25");
  const party = (label, text) => label
    ? `<span class="amber">${esc(label)}</span>`
    : `<span class="subtle">${esc(text || "—")}</span>`;
  document.querySelector("#tbl-recent tbody").innerHTML = recentRows.map((t, i) => `
    <tr>
      <td class="nowrap">${formatDisplayDateTime(t.txn_date)}</td>
      <td>${party(t.outflow_account_label, t.outflow_party)}</td>
      <td class="num red">${qtyAndTickerHtml(t.outflow_amount, t.outflow_ticker)}</td>
      <td>${party(t.inflow_account_label, t.inflow_party)}</td>
      <td class="num green">${qtyAndTickerHtml(t.inflow_amount, t.inflow_ticker)}</td>
      <td class="subtle">${esc(t.note)}</td>
      <td class="center nowrap"><button class="mini" data-edit="${i}">EDIT</button>
        <button class="mini danger" data-del="${i}">DEL</button></td>
    </tr>`).join("");
  document.querySelectorAll("#tbl-recent button[data-edit]").forEach(btn =>
    btn.addEventListener("click", e => {
      e.stopPropagation();
      editRecentModal(recentRows[+btn.dataset.edit]);
    }));
  document.querySelectorAll("#tbl-recent button[data-del]").forEach(btn =>
    btn.addEventListener("click", async e => {
      e.stopPropagation();
      const t = recentRows[+btn.dataset.del];
      if (await confirmDialog(`Delete transaction #${t.id} (${formatDisplayDateTime(t.txn_date)})?`)) {
        await api("/api/transactions/" + t.id, { method: "DELETE" });
        toast("deleted", "ok");
        loadRecent();
      }
    }));
}

function assetListOptions() {
  return assets.map(a => `<option value="${esc(a.ticker)}">${esc(a.ticker)} · ${esc(a.asset_type)}</option>`).join("");
}

function partySelect(id, accountId) {
  return `<select id="${id}">
    <option value="${EXTERNAL}" ${accountId ? "" : "selected"}>— EXTERNAL —</option>` +
    accounts.map(a => `<option value="${a.id}" ${a.id === accountId ? "selected" : ""}>${esc(a.label)}</option>`).join("") +
    `</select>`;
}

function editRecentModal(t) {
  openModal(`MODIFY TRANSACTION #${t.id}`,
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
       <label class="fld">AMOUNT<input type="text" inputmode="decimal" data-formula maxlength="100" id="e-out-amount" value="${esc(t.outflow_amount)}"></label>
     </div>
     <div class="amber">INFLOW</div>
     <div class="form-row">
       <label class="fld">PARTY${partySelect("e-in-acc", t.inflow_account_id)}</label>
       <label class="fld">EXTERNAL NAME<input id="e-in-party" value="${esc(t.inflow_party)}"></label>
       <label class="fld">UNIT<input id="e-in-asset" list="e-in-asset-list"
         value="${esc(t.inflow_ticker || "")}" placeholder="type ticker"></label>
       <label class="fld">AMOUNT<input type="text" inputmode="decimal" data-formula maxlength="100" id="e-in-amount" value="${esc(t.inflow_amount)}"></label>
     </div>
     <div class="form-row">
       <label class="fld">FEE ACCOUNT${partySelect("e-fee-acc", t.fee_account_id)}</label>
       <label class="fld">FEE UNIT<input id="e-fee-asset" list="e-fee-asset-list"
         value="${esc(t.fee_ticker || "")}" placeholder="type ticker"></label>
       <label class="fld">FEE AMOUNT<input type="text" inputmode="decimal" data-formula maxlength="100" id="e-fee-amount" value="${esc(t.fee_amount)}"></label>
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
        outflow_amount: readAmount("e-out-amount"),
        inflow_account_id: inAcc === EXTERNAL ? null : +inAcc,
        inflow_party: inAcc === EXTERNAL ? document.getElementById("e-in-party").value : "",
        inflow_asset_id: assetByTickerInput("e-in-asset"),
        inflow_amount: readAmount("e-in-amount"),
        fee_account_id: feeAcc === EXTERNAL ? null : +feeAcc || null,
        fee_asset_id: assetByTickerInput("e-fee-asset"),
        fee_amount: readAmount("e-fee-amount"),
        note: document.getElementById("e-note").value,
      };
    } catch (e) {
      toast(e.message, "error");
      return;
    }
    await api("/api/transactions/" + t.id, { method: "PUT", body });
    toast("updated", "ok");
    closeModal();
    loadRecent();
  };
}

function renderImportPreview(payload) {
  importPreviewPayload = payload || null;
  const allRows = payload.rows || [];
  const filter = document.getElementById("imp-filter").value;
  const rows = filter === "invalid"
    ? allRows.filter(r => (r.status || "") !== "ok")
    : allRows;
  const summary = payload.summary || {};
  const wrap = document.getElementById("imp-preview-wrap");
  const body = document.querySelector("#tbl-import-preview tbody");
  const kindClass = kind => {
    if (kind === "buy" || kind === "deposit" || kind === "dividend" || kind === "interest") return "green";
    if (kind === "sell" || kind === "withdraw" || kind === "fee" || kind === "tax" || kind === "redemption") return "amber";
    return "subtle";
  };
  const compact = row => {
    const out = row.outflow_ticker ? `${fmtQty(row.outflow_amount)} ${esc(row.outflow_ticker)}` : "—";
    const inflow = row.inflow_ticker ? `${fmtQty(row.inflow_amount)} ${esc(row.inflow_ticker)}` : "—";
    return { out, inflow };
  };
  body.innerHTML = rows.map(r => {
    const flow = compact(r);
    const statusCls = r.status === "ok" ? "green"
      : (r.status === "skipped" ? "subtle" : (r.status === "duplicate" ? "amber" : "red"));
    const typeCls = kindClass(r.txn_kind);
    const note = r.error || r.note || "";
    const tickerHint = yahooLinkHtml(r.ticker || "", yahooEligibleTickers.has(normalizeTicker(r.ticker || "")));
    return `<tr>
      <td class="subtle">${r.row}</td>
      <td class="${statusCls}">${esc(r.status || "invalid")}</td>
      <td><span class="${typeCls}">${esc((r.txn_kind || "").toUpperCase())}</span></td>
      <td><span class="ticker-inline"><input class="imp-ticker" data-row="${r.row}" data-orig="${esc(r.ticker || "")}" 
        value="${esc(r.ticker || "")}" placeholder="—" style="width:11em">${tickerHint}</span></td>
      <td class="nowrap">${esc(r.txn_date || "")}</td>
      <td>${flow.out}</td>
      <td>${flow.inflow}</td>
      <td class="subtle">${esc(note)}</td>
    </tr>`;
  }).join("");

  const invalidByReason = {};
  allRows.forEach(r => {
    if (r.status === "ok" || r.status === "skipped") return;
    const reason = (r.error || "unknown error").trim();
    invalidByReason[reason] = (invalidByReason[reason] || 0) + 1;
  });
  const reasonText = Object.entries(invalidByReason)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 3)
    .map(([reason, count]) => `${count}× ${reason}`)
    .join("; ");

  const shown = filter === "invalid" ? ` · showing ${rows.length} invalid rows` : ` · showing ${rows.length} rows`;
  const inserted = summary.inserted_rows ? ` · ${summary.inserted_rows} inserted` : "";
  const invalidHint = reasonText ? ` · issues: ${reasonText}` : "";
  const dups = summary.duplicate_rows ? ` · ${summary.duplicate_rows} duplicate (skipped unless INCL. DUPLICATES ticked)` : "";
  document.getElementById("imp-summary").textContent =
    `${summary.total_rows || 0} rows · ${summary.valid_rows || 0} valid · ${summary.invalid_rows || 0} invalid · ${summary.skipped_rows || 0} skipped${dups}${shown}${inserted}${invalidHint}`;
  wrap.style.display = rows.length ? "block" : "none";
  renderUnknownTickers(payload);
}

/* ---------------------------------------- unknown ticker approval ------- */
const ASSET_TYPE_OPTIONS = ["equity", "etf", "option", "future", "bond", "crypto",
                            "cash", "real_estate", "loan", "other"];

function renderUnknownTickers(payload) {
  const wrap = document.getElementById("imp-unknown-wrap");
  const body = document.querySelector("#tbl-imp-unknown tbody");
  const list = (payload && payload.unknown_tickers) || [];
  if (!list.length) {
    wrap.style.display = "none";
    body.innerHTML = "";
    return;
  }
  body.innerHTML = list.map((u, i) => {
    const s = u.suggestion || {};
    const d = s.derivative;
    const detail = d ? `${esc(d.underlying_ticker)} ${esc(d.contract_type).toUpperCase()} ` +
      `K=${fmtQty(d.strike)} EXP ${esc(d.expiry)} ×${d.multiplier}` : "";
    return `<tr data-i="${i}">
      <td class="amber">${esc(u.ticker)}</td>
      <td class="num">${u.count}</td>
      <td><select data-f="type">${ASSET_TYPE_OPTIONS.map(t =>
        `<option ${t === (s.asset_type || "equity") ? "selected" : ""}>${t}</option>`).join("")}</select></td>
      <td><input data-f="ccy" value="${esc(s.currency || "USD")}" style="width:5em"></td>
      <td class="center"><input type="checkbox" data-f="yh" ${s.is_yahoo_tracked ? "checked" : ""}></td>
      <td class="subtle" data-f="detail">${detail}</td>
      <td class="nowrap"><button class="mini" data-validate="${i}" title="check on yahoo finance">VALIDATE</button>
        <button class="mini primary" data-create="${i}">CREATE</button></td>
    </tr>`;
  }).join("");
  wrap.style.display = "block";
  body.querySelectorAll("button[data-validate]").forEach(btn =>
    btn.addEventListener("click", async () => {
      const u = list[+btn.dataset.validate];
      const tr = btn.closest("tr");
      const det = tr.querySelector('[data-f="detail"]');
      det.innerHTML = `<span class="dim">checking…</span>`;
      try {
        const info = await api("/api/assets/validate/" + encodeURIComponent(u.ticker));
        tr.querySelector('[data-f="ccy"]').value = (info.currency || "USD").toUpperCase();
        det.innerHTML = `<span class="green">✓ ${esc(info.name || u.ticker)}` +
          `${info.quote_type ? " · " + esc(info.quote_type) : ""}</span>`;
      } catch (_e) {
        det.innerHTML = `<span class="red">✗ not found on yahoo</span>`;
      }
    }));
  body.querySelectorAll("button[data-create]").forEach(btn =>
    btn.addEventListener("click", async () => {
      await createUnknownAsset(list[+btn.dataset.create], btn.closest("tr"));
      previewImport();
    }));
  document.getElementById("imp-create-all").onclick = async () => {
    let created = 0;
    for (const tr of [...body.querySelectorAll("tr")]) {
      try {
        await createUnknownAsset(list[+tr.dataset.i], tr);
        created++;
      } catch (_e) { /* toast already shown; continue with the rest */ }
    }
    if (created) previewImport();
  };
}

async function createUnknownAsset(u, tr) {
  const type = tr.querySelector('[data-f="type"]').value;
  const ccy = tr.querySelector('[data-f="ccy"]').value.trim().toUpperCase() || "USD";
  const tracked = tr.querySelector('[data-f="yh"]').checked;
  if (type === "cash") {
    // currencies endpoint also creates the cash pseudo-asset + FX availability
    await api("/api/currencies", { method: "POST", body: { code: u.ticker, name: u.ticker } });
  } else {
    const body = { ticker: u.ticker, asset_type: type, currency: ccy, is_yahoo_tracked: tracked };
    const s = u.suggestion || {};
    if (s.price_provider) body.price_provider = s.price_provider;
    const d = s.derivative;
    if (d && (type === "option" || type === "future")) body.derivative = d;
    await api("/api/assets", { method: "POST", body });
  }
  toast(`created ${u.ticker}`, "ok");
  await loadRefData();
}

/* edited ticker cells become overrides sent with the next preview/commit */
function collectTickerOverrides() {
  document.querySelectorAll("#tbl-import-preview input.imp-ticker").forEach(inp => {
    const row = inp.dataset.row;
    const val = inp.value.trim().toUpperCase();
    const orig = (inp.dataset.orig || "").toUpperCase();
    if (val && val !== orig) tickerOverrides[row] = val;
    else if (val !== (tickerOverrides[row] || "")) delete tickerOverrides[row];
  });
}

async function previewImport() {
  const provider = document.getElementById("imp-provider").value;
  const accountId = document.getElementById("imp-account").value;
  const file = document.getElementById("imp-file").files[0];
  if (!accountId) {
    toast("select destination account", "error");
    return;
  }
  if (!file) {
    toast("choose a CSV file", "error");
    return;
  }
  collectTickerOverrides();
  const fd = new FormData();
  fd.append("provider", provider);
  fd.append("account_id", accountId);
  fd.append("ticker_overrides", JSON.stringify(tickerOverrides));
  fd.append("file", file);
  const payload = await api("/api/transactions/import/preview", { method: "POST", body: fd });
  importPreviewState = { provider, accountId, fileName: file.name };
  renderImportPreview(payload);
  const sum = payload.summary || {};
  document.getElementById("imp-commit").disabled = !(sum.valid_rows > 0 || sum.duplicate_rows > 0);
}

async function commitImport() {
  const provider = document.getElementById("imp-provider").value;
  const accountId = document.getElementById("imp-account").value;
  const file = document.getElementById("imp-file").files[0];
  const includeDuplicates = document.getElementById("imp-include-dups").checked;
  if (!file) {
    toast("choose a CSV file", "error");
    return;
  }
  if (!importPreviewState || importPreviewState.provider !== provider ||
      importPreviewState.accountId !== accountId || importPreviewState.fileName !== file.name) {
    toast("run preview first for the current file", "error");
    return;
  }
  const fd = new FormData();
  fd.append("provider", provider);
  fd.append("account_id", accountId);
  fd.append("ticker_overrides", JSON.stringify(tickerOverrides));
  fd.append("include_duplicates", includeDuplicates ? "1" : "0");
  fd.append("file", file);
  const payload = await api("/api/transactions/import/commit", { method: "POST", body: fd });
  const summary = payload.summary || {};
  const skippedDups = !includeDuplicates && summary.duplicate_rows
    ? ` · ${summary.duplicate_rows} duplicates skipped` : "";
  toast(`imported ${summary.inserted_rows || 0} rows${skippedDups}`, "ok");
  renderImportPreview(payload);
  document.getElementById("imp-commit").disabled = true;
  loadRecent();
  loadBatches();
}

/* ------------------------------------------------- import history -------- */
async function loadBatches() {
  const rows = await api("/api/transactions/import/batches");
  const body = document.querySelector("#tbl-import-batches tbody");
  document.getElementById("imp-history-wrap").style.display = rows.length ? "block" : "none";
  body.innerHTML = rows.map(b => {
    const status = b.reverted_at
      ? `<span class="subtle">REVERTED ${formatDisplayDateTime(b.reverted_at)}</span>`
      : (b.remaining_count < b.inserted_count
        ? `<span class="amber">${b.remaining_count}/${b.inserted_count} REMAIN</span>`
        : `<span class="green">ACTIVE</span>`);
    return `<tr>
      <td class="subtle">${b.id}</td>
      <td class="nowrap">${formatDisplayDateTime(b.created_at)}</td>
      <td>${esc((b.provider || "").toUpperCase())}</td>
      <td>${esc(b.account_label || "")}</td>
      <td class="subtle">${esc(b.file_name || "")}</td>
      <td class="num">${b.inserted_count}</td>
      <td>${status}</td>
      <td class="center nowrap">
        <button class="mini danger" data-undo="${b.id}" ${b.reverted_at || !b.remaining_count ? "disabled" : ""}>UNDO</button>
        <button class="mini" data-redo="${b.id}" title="re-run this import from the stored file (duplicates skipped unless INCL. DUPLICATES is ticked)">REDO</button>
      </td>
    </tr>`;
  }).join("");
  body.querySelectorAll("button[data-undo]").forEach(btn =>
    btn.addEventListener("click", async () => {
      const id = +btn.dataset.undo;
      if (!await confirmDialog(`Undo import #${id}? All transactions it inserted will be deleted.`)) return;
      const res = await api(`/api/transactions/import/batches/${id}/undo`, { method: "POST" });
      toast(`import #${id} undone · ${res.deleted_rows} transactions deleted`, "ok");
      loadRecent();
      loadBatches();
    }));
  body.querySelectorAll("button[data-redo]").forEach(btn =>
    btn.addEventListener("click", async () => {
      const id = +btn.dataset.redo;
      const includeDuplicates = document.getElementById("imp-include-dups").checked;
      const dupNote = includeDuplicates ? " INCLUDING duplicates" : " (duplicates skipped)";
      if (!await confirmDialog(`Re-run import #${id}${dupNote}?`)) return;
      const payload = await api(`/api/transactions/import/batches/${id}/redo`,
        { method: "POST", body: { include_duplicates: includeDuplicates } });
      toast(`redo of import #${id}: ${payload.summary.inserted_rows || 0} rows inserted`, "ok");
      loadRecent();
      loadBatches();
    }));
}

/* ------------------------------------------------------------ templates -- */
const TEMPLATES = {
  buy: { hint: "BUY: same account both sides — outflow = cash paid, inflow = security received.",
    apply() { mirrorInToOut(); } },
  sell: { hint: "SELL: same account both sides — outflow = security, inflow = cash.",
    apply() { mirrorInToOut(); } },
  transfer: { hint: "TRANSFER: pick two different accounts; same asset both sides.",
    apply() {} },
  deposit: { hint: "DEPOSIT: outflow party = EXTERNAL (e.g. employer), inflow = your account.",
    apply() { document.getElementById("txn-out-acc").value = EXTERNAL; syncPartyVisibility(); } },
  withdraw: { hint: "WITHDRAW: outflow = your account, inflow party = EXTERNAL.",
    apply() { document.getElementById("txn-in-acc").value = EXTERNAL; syncPartyVisibility(); } },
  loan: { hint: "LOAN: inflow = cash into your account, outflow = loan asset from the SAME account (creates the liability).",
    apply() { mirrorInToOut(); } },
};
function mirrorInToOut() {
  const out = document.getElementById("txn-out-acc");
  if (out.value === EXTERNAL && accounts.length) out.value = String(accounts[0].id);
  document.getElementById("txn-in-acc").value = out.value;
  syncPartyVisibility();
}
document.querySelectorAll("[data-tpl]").forEach(btn => btn.addEventListener("click", () => {
  const t = TEMPLATES[btn.dataset.tpl];
  document.getElementById("txn-hint").textContent = t.hint;
  t.apply();
}));

/* ----------------------------------------------------------------- save -- */
document.getElementById("txn-save").addEventListener("click", async () => {
  const outAcc = document.getElementById("txn-out-acc").value;
  const inAcc = document.getElementById("txn-in-acc").value;
  let body;
  try {
    body = {
      txn_date: displayDateTimeLocalToUtcString(document.getElementById("txn-date").value),
      outflow_account_id: outAcc === EXTERNAL ? null : +outAcc,
      outflow_party: outAcc === EXTERNAL ? document.getElementById("txn-out-party").value : "",
      outflow_asset_id: assetByTickerInput("txn-out-asset"),
      outflow_amount: readAmount("txn-out-amount"),
      inflow_account_id: inAcc === EXTERNAL ? null : +inAcc,
      inflow_party: inAcc === EXTERNAL ? document.getElementById("txn-in-party").value : "",
      inflow_asset_id: assetByTickerInput("txn-in-asset"),
      inflow_amount: readAmount("txn-in-amount"),
      fee_account_id: +document.getElementById("txn-fee-acc").value || null,
      fee_asset_id: assetByTickerInput("txn-fee-asset"),
      fee_amount: readAmount("txn-fee-amount"),
      note: document.getElementById("txn-note").value,
    };
  } catch (e) {
    toast(e.message, "error");
    return;
  }
  await api("/api/transactions", { method: "POST", body });
  toast("transaction saved", "ok");
  ["txn-out-amount", "txn-in-amount", "txn-note"].forEach(id =>
    document.getElementById(id).value = "");
  document.getElementById("txn-fee-amount").value = "0";
  loadRecent();
});

document.getElementById("txn-out-acc").addEventListener("change", syncPartyVisibility);
document.getElementById("txn-in-acc").addEventListener("change", syncPartyVisibility);
document.getElementById("imp-preview").addEventListener("click", previewImport);
document.getElementById("imp-commit").addEventListener("click", commitImport);
// edited tickers require a fresh preview before commit
document.querySelector("#tbl-import-preview tbody").addEventListener("input", e => {
  if (e.target.classList.contains("imp-ticker")) {
    document.getElementById("imp-commit").disabled = true;
  }
});
// leaving an edited ticker cell re-runs the preview: the typed symbol is
// validated and unknown ones surface in the create-asset table
document.querySelector("#tbl-import-preview tbody").addEventListener("change", e => {
  if (!e.target.classList.contains("imp-ticker")) return;
  collectTickerOverrides();
  previewImport();
});
document.getElementById("imp-file").addEventListener("change", resetImportPreviewUi);
document.getElementById("imp-provider").addEventListener("change", resetImportPreviewUi);
document.getElementById("imp-account").addEventListener("change", resetImportPreviewUi);
document.getElementById("imp-filter").addEventListener("change", () => {
  if (importPreviewPayload) renderImportPreview(importPreviewPayload);
});

/* ---------------------------------------------------------- add modals --- */
document.getElementById("add-account").addEventListener("click", () => {
  const custodians = [...new Set(accounts.map(a => a.custodian).filter(Boolean))];
  openModal("ADD ACCOUNT",
    `<div class="form-row">
      <label class="fld">CUSTODIAN TYPE<select id="m-acc-ctype">
        <option>bank</option><option>broker</option><option>exchange</option>
        <option>wallet</option><option>other</option></select></label>
      <label class="fld">CUSTODIAN / INSTITUTION
        <input id="m-acc-custodian" list="custodian-list" placeholder="HSBC / IBKR / Binance / 0x12…ab">
        <datalist id="custodian-list">${custodians.map(c => `<option value="${esc(c)}">`).join("")}</datalist>
      </label>
      <label class="fld">ACCOUNT NAME<input id="m-acc-name" placeholder="e.g. Securities / Cold wallet"></label>
     </div>
     <div class="form-row">
      <label class="fld">BASE CCY<select id="m-acc-ccy">${currencies.map(c =>
        `<option>${esc(c.code)}</option>`).join("")}</select></label>
      <label class="fld" style="flex:1">NOTE<input id="m-acc-note" style="width:100%"></label>
     </div>`,
    `<button class="primary" id="m-save">SAVE</button>`);
  document.getElementById("m-save").onclick = async () => {
    await api("/api/accounts", { method: "POST", body: {
      name: document.getElementById("m-acc-name").value,
      custodian_type: document.getElementById("m-acc-ctype").value,
      custodian: document.getElementById("m-acc-custodian").value,
      base_currency: document.getElementById("m-acc-ccy").value,
      note: document.getElementById("m-acc-note").value } });
    toast("account added", "ok"); closeModal(); loadRefData();
  };
});

document.getElementById("add-currency").addEventListener("click", () => {
  openModal("ADD CURRENCY",
    `<div class="form-row"><label class="fld">CODE<input id="m-cur-code" maxlength="5" placeholder="e.g. NOK"></label>
     <label class="fld">NAME<input id="m-cur-name"></label></div>
     <div class="dim">A cash asset for the currency is created automatically; its FX rate will be scraped.</div>`,
    `<button class="primary" id="m-save">SAVE</button>`);
  document.getElementById("m-save").onclick = async () => {
    await api("/api/currencies", { method: "POST", body: {
      code: document.getElementById("m-cur-code").value,
      name: document.getElementById("m-cur-name").value } });
    toast("currency added", "ok"); closeModal(); loadRefData();
  };
});

function addAssetModal() {
  openModal("ADD ASSET / TICKER",
    `<div class="form-row">
       <label class="fld">TICKER (YAHOO)<input id="m-as-ticker" placeholder="AAPL, 0005.HK, BTC-USD, ES=F"></label>
       <label class="fld">TYPE<select id="m-as-type">
         <option>equity</option><option>etf</option><option>bond</option><option>option</option>
         <option>future</option><option>crypto</option><option>real_estate</option>
         <option>loan</option><option>cash</option><option>other</option></select></label>
       <label class="fld hidden" id="m-as-src-wrap">SOURCE<select id="m-as-src">
         <option value="yahoo">yahoo</option>
         <option value="option_archive">option archive (EOD, incl. expired)</option></select></label>
       <label class="fld">YAHOO TRACKED?<input type="checkbox" id="m-as-yahoo" checked></label>
       <button class="mini" id="m-as-validate">VALIDATE</button>
     </div>
     <div id="m-as-preview" class="dim"></div>
     <div class="form-row hidden" id="m-as-manual-row">
       <label class="fld">NAME<input id="m-as-name"></label>
       <label class="fld">CURRENCY<input id="m-as-ccy" maxlength="5" value="USD"></label>
     </div>`,
    `<button class="primary" id="m-save">SAVE ASSET</button>`);

  const typeSel = document.getElementById("m-as-type");
  const yahooChk = document.getElementById("m-as-yahoo");
  const sync = () => {
    document.getElementById("m-as-manual-row").classList.toggle("hidden", yahooChk.checked);
    document.getElementById("m-as-src-wrap").classList.toggle("hidden", typeSel.value !== "option");
  };
  typeSel.addEventListener("change", sync);
  yahooChk.addEventListener("change", sync);

  document.getElementById("m-as-validate").onclick = async () => {
    const t = document.getElementById("m-as-ticker").value.trim();
    if (!t) return;
    const p = document.getElementById("m-as-preview");
    p.textContent = "validating...";
    try {
      const info = await api("/api/assets/validate/" + encodeURIComponent(t));
      p.innerHTML = `<span class="green">✓</span> ${esc(info.name)} · ${esc(info.currency)} · ` +
        `${esc(info.quote_type)} · ${esc(info.sector || "-")} · px ${fmt(info.price)}`;
    } catch (e) { p.innerHTML = `<span class="red">✗ ${esc(e.message)}</span>`; }
  };

  document.getElementById("m-save").onclick = async () => {
    const body = {
      ticker: document.getElementById("m-as-ticker").value.trim(),
      asset_type: typeSel.value,
      is_yahoo_tracked: yahooChk.checked,
      name: document.getElementById("m-as-name").value,
      currency: document.getElementById("m-as-ccy").value,
    };
    if (typeSel.value === "option") body.price_provider = document.getElementById("m-as-src").value;
    const created = await api("/api/assets", { method: "POST", body });
    toast(`asset ${created.ticker} added (${created.currency}); history backfill started`, "ok");
    closeModal();
    loadRefData();
  };
}
document.getElementById("add-asset").addEventListener("click", addAssetModal);

/* init: datetime-local default = now (local tz) */
(function setNow() {
  const now = new Date();
  now.setSeconds(0, 0);
  document.getElementById("txn-date").value = formatDisplayDateTimeLocal(now);
})();
loadRefData();
loadRecent();
loadBatches();
