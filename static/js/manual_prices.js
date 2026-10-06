/* Manual price management for assets Yahoo cannot track (real estate, private, ...). */
"use strict";

let massets = [], selected = null, spark = null;

async function loadAssets() {
  const all = await api("/api/assets");
  massets = all.filter(a => !a.is_yahoo_tracked && !["cash", "loan"].includes(a.asset_type));
  document.querySelector("#tbl-massets tbody").innerHTML = massets.map((a, i) => `
    <tr class="clickable" data-i="${i}">
      <td class="amber">${esc(a.ticker)}</td><td>${esc(a.asset_type)}</td>
      <td>${esc(a.currency)}</td><td class="num" id="lastpx-${a.id}">…</td>
      <td>${a.asset_type === "option"
        ? `<button class="mini" data-arch="${i}" title="switch to option-archive EOD prices">ARCHIVE</button>`
        : ""}</td>
    </tr>`).join("");
  document.querySelectorAll("#tbl-massets tr[data-i]").forEach(tr =>
    tr.addEventListener("click", () => select(massets[+tr.dataset.i])));
  document.querySelectorAll("#tbl-massets button[data-arch]").forEach(btn =>
    btn.addEventListener("click", e => {
      e.stopPropagation();
      switchToArchive(massets[+btn.dataset.arch], btn);
    }));
  for (const a of massets) {
    api(`/api/assets/${a.id}/manual-prices`).then(ps => {
      const el = document.getElementById(`lastpx-${a.id}`);
      if (el) el.textContent = ps.length ? fmt(ps[0].price) : "—";
    });
  }
}

async function switchToArchive(a, btn) {
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const p = await api(`/api/assets/${a.id}/archive-preview`);
    if (!p.rows) { toast(`archive has no data for ${a.ticker}`, "error"); return; }
    const msg = `Archive has ${p.rows} days (${p.first_date} → ${p.last_date}, ` +
      `last close ${fmt(p.last_close, 4)}). Switch ${a.ticker} to archive prices? ` +
      `Daily history and snapshots will be rebuilt.`;
    if (!await confirmDialog(msg)) return;
    btn.textContent = "LOADING";
    const res = await api(`/api/assets/${a.id}/switch-source`, {
      method: "POST", body: { source: "option_archive" } });
    toast(`${a.ticker}: ${res.bars} daily bars loaded, snapshots resynced`, "ok");
    document.getElementById("mp-editor").classList.add("hidden");
    document.getElementById("mp-title").textContent = "SELECT AN ASSET";
    loadAssets();
  } catch (_err) { /* api() already toasted the error */ }
  finally {
    btn.disabled = false;
    btn.textContent = "ARCHIVE";
  }
}

async function select(asset) {
  selected = asset;
  document.getElementById("mp-title").textContent =
    `${asset.ticker} — ${asset.name || asset.asset_type} (${asset.currency})`;
  document.getElementById("mp-editor").classList.remove("hidden");
  document.getElementById("mp-date").value = formatDisplayDate(new Date());
  if (!spark) spark = mkChart("mp-spark");
  await loadPrices();
}

async function loadPrices() {
  const prices = await api(`/api/assets/${selected.id}/manual-prices`);
  document.querySelector("#tbl-mprices tbody").innerHTML = prices.map((p, i) => `
    <tr>
      <td>${formatDisplayDate(p.price_date)}</td><td class="num">${fmt(p.price, 4)}</td>
      <td class="subtle">${esc(p.note)}</td>
      <td class="center"><button class="mini" data-i="${i}">⋮</button></td>
    </tr>`).join("");
  document.querySelectorAll("#tbl-mprices button[data-i]").forEach(btn => {
    const p = prices[+btn.dataset.i];
    btn.addEventListener("click", e => {
      rowMenu(btn, [
        ["EDIT", () => editPrice(p)],
        ["DELETE", async () => {
          if (await confirmDialog(`Delete price ${formatDisplayDate(p.price_date)} = ${p.price}?`)) {
            await api("/api/manual-prices/" + p.id, { method: "DELETE" });
            toast("deleted", "ok");
            loadPrices();
          }
        }],
      ]);
      e.stopPropagation();
    });
  });
  const asc = [...prices].reverse();
  spark.setOption({
    grid: { left: 60, right: 12, top: 8, bottom: 20 },
    xAxis: { type: "category", data: asc.map(p => formatDisplayDate(p.price_date)) },
    yAxis: { type: "value", scale: true },
    tooltip: { trigger: "axis" },
    series: [{ type: "line", data: asc.map(p => p.price), showSymbol: true,
      symbolSize: 5, lineStyle: { color: "#ffb000" } }],
  }, true);
}

function editPrice(p) {
  openModal("EDIT PRICE",
    `<div class="form-row">
       <label class="fld">DATE<input type="date" id="ep-date" value="${formatDisplayDate(p.price_date)}"></label>
       <label class="fld">PRICE<input type="number" step="any" id="ep-price" value="${p.price}"></label>
       <label class="fld">NOTE<input id="ep-note" value="${esc(p.note)}"></label>
     </div>`,
    `<button class="primary" id="ep-save">SAVE</button>`);
  document.getElementById("ep-save").onclick = async () => {
    await api("/api/manual-prices/" + p.id, { method: "PUT", body: {
      price_date: document.getElementById("ep-date").value,
      price: +document.getElementById("ep-price").value,
      note: document.getElementById("ep-note").value } });
    toast("saved", "ok"); closeModal(); loadPrices();
  };
}

document.getElementById("mp-add").addEventListener("click", async () => {
  if (!selected) return;
  await api(`/api/assets/${selected.id}/manual-prices`, { method: "POST", body: {
    price_date: document.getElementById("mp-date").value,
    price: +document.getElementById("mp-price").value,
    note: document.getElementById("mp-note").value } });
  toast("price saved", "ok");
  document.getElementById("mp-price").value = "";
  document.getElementById("mp-note").value = "";
  loadPrices();
  loadAssets();
});

document.getElementById("btn-new-manual").addEventListener("click", () => {
  openModal("NEW MANUAL ASSET",
    `<div class="form-row">
       <label class="fld">IDENTIFIER<input id="nm-ticker" placeholder="e.g. RE:HOME1"></label>
       <label class="fld">NAME<input id="nm-name" placeholder="e.g. Apartment 12F"></label>
     </div>
     <div class="form-row">
       <label class="fld">TYPE<select id="nm-type">
         <option>real_estate</option><option>bond</option><option>other</option>
         <option>crypto</option><option>equity</option></select></label>
       <label class="fld">CURRENCY<input id="nm-ccy" maxlength="5" value="USD"></label>
     </div>
     <div class="dim">Value = latest manual price × quantity held (via transactions).</div>`,
    `<button class="primary" id="nm-save">CREATE</button>`);
  document.getElementById("nm-save").onclick = async () => {
    await api("/api/assets", { method: "POST", body: {
      ticker: document.getElementById("nm-ticker").value,
      name: document.getElementById("nm-name").value,
      asset_type: document.getElementById("nm-type").value,
      currency: document.getElementById("nm-ccy").value,
      is_yahoo_tracked: false } });
    toast("asset created", "ok"); closeModal(); loadAssets();
  };
});

loadAssets();
