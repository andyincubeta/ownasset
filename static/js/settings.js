/* Settings: general, scheduler intervals, home layout, data management + purges. */
"use strict";

const HOME_SECTION_DEFS = [
  ["kpis", "CONTAINERS (KPI TILES)"],
  ["indices", "INDICES (VALUE & CHANGE)"],
  ["series", "PORTFOLIO VALUE CHART"],
  ["holdings", "HOLDINGS TABLE"],
  ["charts", "ALLOCATION & EQUITY CHARTS"],
];
let displayPrefs = {};

function renderHomeSections() {
  const keys = HOME_SECTION_DEFS.map(d => d[0]);
  const order = (displayPrefs.home_sections || keys).filter(k => keys.includes(k));
  keys.forEach(k => { if (!order.includes(k)) order.push(k); });
  const label = k => HOME_SECTION_DEFS.find(d => d[0] === k)[1];
  document.getElementById("s-home-sections").innerHTML = order.map(k => `
    <div class="col-order-row" data-key="${k}">
      <span>${label(k)}</span><span class="spacer"></span>
      <button class="mini" data-mv="-1" title="move up">▲</button>
      <button class="mini" data-mv="1" title="move down">▼</button>
    </div>`).join("");
}

async function load() {
  const s = await api("/api/settings");
  try { displayPrefs = JSON.parse(s.display_prefs || "{}"); } catch (_e) { displayPrefs = {}; }
  renderHomeSections();
  loadIndices();
  document.getElementById("s-ccy").value = s.display_currency;
  document.getElementById("s-utc-offset").value = s.display_utc_offset_hours;
  document.getElementById("s-log").value = s.log_level;
  document.getElementById("s-imp-ccy-ib").value = s.import_ccy_ib || "HKD";
  document.getElementById("s-imp-ccy-ft").value = s.import_ccy_firstrade || "USD";
  document.getElementById("s-quote-sec").value = s.quote_interval_seconds;
  document.getElementById("s-intra-min").value = s.intraday_interval_minutes;
  document.getElementById("s-intra-bar").value = s.intraday_bar;
  document.getElementById("s-daily-time").value = s.daily_refresh_time;
  document.getElementById("s-info-time").value = s.info_refresh_time;
  document.getElementById("s-snap-time").value = s.snapshot_time;
  document.getElementById("s-close-mode").value = s.day_close_mode || "market";
  document.getElementById("s-close-cutoff").value = s.day_close_cutoff_utc || "00:00";
  document.getElementById("s-backup-time").value = s.backup_time || "00:30";
  document.getElementById("s-backup-keep").value = s.backup_keep_count || "14";
  loadStats();
  loadBackups();
  const assets = await api("/api/assets?all=1");
  document.getElementById("s-purge-asset-list").innerHTML = assets.filter(a => a.is_yahoo_tracked).map(a =>
    `<option value="${esc(a.ticker)}">${esc(a.ticker)} · ${esc(a.asset_type)}</option>`).join("");
}

async function loadStats() {
  const st = await api("/api/settings/datastats");
  const mb = (st.db_size_bytes / 1048576).toFixed(2);
  const t = st.tables;
  document.getElementById("s-stats").innerHTML =
    `DB SIZE: <span class="amber">${mb} MB</span> · ` +
    ["prices_daily", "prices_intraday", "fx_daily", "transactions", "daily_snapshots",
     "scrape_log"]
      .map(k => `${k}: <span class="amber">${(t[k] ?? 0).toLocaleString()}</span>`).join(" · ");
  document.querySelector("#tbl-intraday-stats tbody").innerHTML =
    st.intraday_by_asset.map(r => `<tr><td>${esc(r.ticker)}</td>
      <td class="num">${r.rows_count.toLocaleString()}</td>
      <td>${formatDisplayDate(r.min_ts)}</td>
      <td>${formatDisplayDate(r.max_ts)}</td></tr>`).join("");
}

async function loadBackups() {
  const rows = await api("/api/settings/backups");
  document.querySelector("#tbl-backups tbody").innerHTML = rows.length ? rows.map(b => `<tr>
      <td>${esc(b.name)}</td>
      <td class="num">${(b.size_bytes / 1048576).toFixed(2)}</td>
      <td>${esc(b.mtime)}</td>
      <td><a class="mini" href="/api/settings/backups/${encodeURIComponent(b.name)}">DOWNLOAD</a></td>
    </tr>`).join("") : `<tr><td colspan="4" class="dim">no backups yet</td></tr>`;
}

document.getElementById("s-save-backup").addEventListener("click", async () => {
  await api("/api/settings", { method: "POST", body: {
    backup_time: document.getElementById("s-backup-time").value.trim(),
    backup_keep_count: document.getElementById("s-backup-keep").value.trim() } });
  toast("backup settings saved & applied", "ok");
});

document.getElementById("s-backup-now").addEventListener("click", async () => {
  await api("/api/settings/backups", { method: "POST" });
  toast("backup started", "ok");
  setTimeout(loadBackups, 2000);
});

document.getElementById("s-home-sections").addEventListener("click", e => {
  const btn = e.target.closest("button[data-mv]");
  if (!btn) return;
  const row = btn.closest(".col-order-row");
  if (+btn.dataset.mv < 0) {
    if (row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
  } else if (row.nextElementSibling) {
    row.parentNode.insertBefore(row.nextElementSibling, row);
  }
});

document.getElementById("s-save-home").addEventListener("click", async () => {
  displayPrefs.home_sections = [...document.querySelectorAll("#s-home-sections .col-order-row")]
    .map(r => r.dataset.key);
  await api("/api/settings", { method: "POST",
    body: { display_prefs: JSON.stringify(displayPrefs) } });
  toast("home layout saved", "ok");
});

/* ------------------------------------------------------------ indices --- */
let idxList = [], idxPresets = [], idxMaxHome = 8, idxCustomCcy = null;

function renderIndices() {
  document.querySelector("#tbl-indices tbody").innerHTML = idxList.map((ix, i) => `
    <tr data-i="${i}"><td>${esc(ix.ticker)}</td><td>${esc(ix.name)}</td>
      <td>${esc(ix.short)}</td><td>${esc(ix.currency || "")}</td>
      <td><input type="checkbox" data-home ${ix.home ? "checked" : ""}></td>
      <td style="white-space:nowrap">
        <button class="mini" data-mv="-1" title="move up">▲</button>
        <button class="mini" data-mv="1" title="move down">▼</button>
        <button class="mini" data-rm title="remove">✕</button></td></tr>`).join("");
  const used = new Set(idxList.map(ix => ix.ticker.toUpperCase()));
  document.getElementById("s-idx-preset").innerHTML = idxPresets
    .filter(p => !used.has(p.ticker.toUpperCase()))
    .map(p => `<option value="${esc(p.key)}">${esc(p.name)} (${esc(p.ticker)})</option>`).join("");
}

async function loadIndices() {
  const d = await api("/api/settings/indices");
  idxList = d.configured;
  idxPresets = d.presets;
  idxMaxHome = d.max_home;
  renderIndices();
}

document.getElementById("tbl-indices").addEventListener("click", e => {
  const tr = e.target.closest("tr[data-i]");
  if (!tr) return;
  const i = +tr.dataset.i;
  if (e.target.matches("[data-home]")) {
    if (e.target.checked && idxList.filter(ix => ix.home).length >= idxMaxHome) {
      e.target.checked = false;
      toast(`at most ${idxMaxHome} indices on HOME`, "error");
      return;
    }
    idxList[i].home = e.target.checked;
    return;
  }
  const mv = e.target.closest("button[data-mv]");
  if (mv) {
    const j = i + +mv.dataset.mv;
    if (j < 0 || j >= idxList.length) return;
    [idxList[i], idxList[j]] = [idxList[j], idxList[i]];
  } else if (e.target.closest("button[data-rm]")) {
    idxList.splice(i, 1);
  } else return;
  renderIndices();
});

document.getElementById("s-idx-add-preset").addEventListener("click", () => {
  const p = idxPresets.find(x => x.key === document.getElementById("s-idx-preset").value);
  if (!p) return;
  idxList.push({ ...p, home: false });
  renderIndices();
});

document.getElementById("s-idx-lookup").addEventListener("click", async () => {
  const ticker = document.getElementById("s-idx-ticker").value.trim();
  if (!ticker) return;
  const r = await api("/api/settings/indices/lookup?ticker=" + encodeURIComponent(ticker));
  document.getElementById("s-idx-name").value = r.name || "";
  idxCustomCcy = r.currency || null;
  toast(`${r.ticker}: ${r.name} · ${r.currency || "?"} ${r.price != null ? fmt(r.price) : ""}`, "ok");
});

document.getElementById("s-idx-ticker").addEventListener("input", () => { idxCustomCcy = null; });

document.getElementById("s-idx-add-custom").addEventListener("click", () => {
  const ticker = document.getElementById("s-idx-ticker").value.trim().toUpperCase();
  if (!/^[\^A-Z0-9.=\-]{1,20}$/.test(ticker)) {
    toast("invalid Yahoo symbol", "error");
    return;
  }
  if (idxList.some(ix => ix.ticker.toUpperCase() === ticker)) {
    toast(`${ticker} already listed`, "error");
    return;
  }
  const short = document.getElementById("s-idx-short").value.trim();
  idxList.push({ ticker, name: document.getElementById("s-idx-name").value.trim() || ticker,
                 short: short || ticker.replace(/[^A-Z0-9]/g, "").slice(0, 8),
                 currency: idxCustomCcy || "", home: false });
  ["s-idx-ticker", "s-idx-name", "s-idx-short"].forEach(id => {
    document.getElementById(id).value = "";
  });
  idxCustomCcy = null;
  renderIndices();
});

document.getElementById("s-save-indices").addEventListener("click", async () => {
  const d = await api("/api/settings/indices", { method: "PUT", body: { indices: idxList } });
  idxList = d.configured;
  renderIndices();
  toast("indices saved", "ok");
});

document.getElementById("s-save-general").addEventListener("click", async () => {
  await api("/api/settings", { method: "POST", body: {
    display_currency: document.getElementById("s-ccy").value,
    display_utc_offset_hours: document.getElementById("s-utc-offset").value,
    log_level: document.getElementById("s-log").value,
    import_ccy_ib: document.getElementById("s-imp-ccy-ib").value,
    import_ccy_firstrade: document.getElementById("s-imp-ccy-ft").value } });
  document.body.dataset.displayUtcOffset = document.getElementById("s-utc-offset").value || "0";
  await loadStats();
  toast("general settings saved", "ok");
});

document.getElementById("s-save-sched").addEventListener("click", async () => {
  await api("/api/settings", { method: "POST", body: {
    quote_interval_seconds: document.getElementById("s-quote-sec").value,
    intraday_interval_minutes: document.getElementById("s-intra-min").value,
    intraday_bar: document.getElementById("s-intra-bar").value,
    daily_refresh_time: document.getElementById("s-daily-time").value,
    info_refresh_time: document.getElementById("s-info-time").value,
    snapshot_time: document.getElementById("s-snap-time").value } });
  toast("scheduler settings saved & applied", "ok");
});

document.getElementById("s-save-close").addEventListener("click", async () => {
  await api("/api/settings", { method: "POST", body: {
    day_close_mode: document.getElementById("s-close-mode").value,
    day_close_cutoff_utc: document.getElementById("s-close-cutoff").value.trim() } });
  toast("day close saved; refetch/rebuild running in background", "ok");
});

document.getElementById("s-purge-intraday").addEventListener("click", async () => {
  const assetSel = document.getElementById("s-purge-asset");
  const scope = assetSel.value ? assetSel.value : "ALL assets";
  const older = document.getElementById("s-purge-date").value;
  if (!await confirmDialog(`Purge intraday bars for ${scope}` +
      (older ? ` older than ${older}` : "") + "? Daily history is unaffected.")) return;
  const body = { target: "intraday" };
  if (assetSel.value) {
    const assets = await api("/api/assets?all=1");
    const asset = assets.find(a => (a.ticker || "").toUpperCase() === assetSel.value.trim().toUpperCase());
    if (!asset) {
      toast(`unknown asset: ${assetSel.value.trim().toUpperCase()}`, "error");
      return;
    }
    body.asset_id = asset.id;
  }
  if (older) body.older_than = older;
  const res = await api("/api/settings/purge", { method: "POST", body });
  toast(`purged ${res.deleted} intraday rows`, "ok");
  loadStats();
});

document.getElementById("s-purge-scrape").addEventListener("click", async () => {
  if (!await confirmDialog("Purge ALL scrape log rows?")) return;
  const res = await api("/api/settings/purge", { method: "POST", body: { target: "scrape_log" } });
  toast(`purged ${res.deleted} rows`, "ok");
  loadStats();
});

load();
