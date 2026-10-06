/* Scrape monitor: job cards, hourly success/error chart, run log. */
"use strict";

let hourlyChart = null;

const JOB_HELP = {
  quotes: [
    "QUOTES - live prices",
    "Runs every N seconds (default 60, min 10; SETUP > SCRAPER INTERVALS).",
    "ONE batched Yahoo request for every active Yahoo-tracked asset plus the FX pairs you need.",
    "Writes the latest price + previous close (latest_quotes / fx_latest).",
    "Tickers the batch misses are retried one by one (up to 10 per run).",
    "If Yahoo rate-limits, runs are skipped with a growing backoff (x2 up to x16) and BACKOFF is shown.",
    "Used by: HOME and HOLD live values, day change %, FX conversion.",
  ],
  intraday: [
    "INTRADAY - bars within the day",
    "Runs every N minutes (default 60).",
    "Fetches the last 7 days of OHLC bars per asset at the INTRADAY BAR size (default 60m).",
    "Stored in prices_intraday; purge old bars in SETUP > DATA MANAGEMENT.",
    "Used by: intraday price charts.",
  ],
  daily: [
    "DAILY - end-of-day price history",
    "Runs once a day at DAILY REFRESH (default 22:30 UTC).",
    "First run for a new asset backfills its FULL history; later runs refresh the last 35 days.",
    "Also refreshes options-archive contracts and daily FX rates.",
    "Stored in prices_daily / fx_daily.",
    "Used by: PRICES charts, performance, snapshot rebuilds.",
  ],
  info: [
    "INFO - company / instrument profile",
    "Runs once a day at INFO REFRESH (default 21:00 UTC).",
    "Downloads the Yahoo quoteSummary (key stats, profile, financial data...) per asset.",
    "Saved to the asset record; fills in name, industry, sector and country when blank.",
  ],
  snapshot: [
    "SNAPSHOT - daily portfolio photo",
    "Runs once a day at SNAPSHOT (default 23:59 UTC).",
    "Records every holding: quantity, price, FX rate and USD value for that date.",
    "Stored in daily_snapshots; days missed while the app was off are rebuilt at startup.",
    "Used by: DAILY values history and portfolio value charts.",
  ],
  backup: [
    "BACKUP - database copy",
    "Runs once a day at BACKUP TIME (default 00:30 UTC; SETUP > BACKUPS).",
    "Copies the live SQLite database with the online backup API and verifies it (integrity_check).",
    "Saved to .db/backups; only the newest KEEP LAST N files are kept.",
    "Copy that folder off this machine for a real off-site backup.",
  ],
};

async function loadStatus() {
  const jobs = await api("/api/scrape/status");
  document.getElementById("job-cards").innerHTML = jobs.map(j => {
    const last = j.last_run;
    const ok = last && last.status === "success";
    const rate = j.total_24h ? Math.round(j.success_24h / j.total_24h * 100) : null;
    const help = JOB_HELP[j.job];
    return `<div class="kpi">
      <div class="label">${esc(j.job)} ${help ? `<span class="help-q" title="${esc(help.join("\n"))}">?</span>` : ""} ${j.backoff_active ? '<span class="tag warn">BACKOFF</span>' : ""}</div>
      <div class="value ${last ? (ok ? "green" : "red") : ""}">${last ? last.status.toUpperCase() : "IDLE"}</div>
      <div class="sub">last: ${last ? esc(last.ts) : "—"}</div>
      <div class="sub">next: ${esc(j.next_run || "—")}</div>
      <div class="sub">24h: ${j.success_24h}/${j.total_24h}${rate !== null ? " (" + rate + "%)" : ""}</div>
      <button class="mini" data-run="${esc(j.job)}" style="margin-top:3px">RUN NOW</button>
    </div>`;
  }).join("");
  document.querySelectorAll("[data-run]").forEach(btn => btn.addEventListener("click", async () => {
    await api("/api/scrape/run/" + btn.dataset.run, { method: "POST" });
    toast(`job ${btn.dataset.run} triggered`, "ok");
    setTimeout(loadAll, 1500);
  }));
}

async function loadHourly() {
  const rows = await api("/api/scrape/hourly");
  hourlyChart.setOption({
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 50, right: 16, top: 26, bottom: 24 },
    xAxis: { type: "category", data: rows.map(r => r.hour.slice(11)) },
    yAxis: { type: "value" },
    series: [
      { name: "success", type: "bar", stack: "s", data: rows.map(r => r.ok),
        itemStyle: { color: "#00c853" } },
      { name: "error", type: "bar", stack: "s", data: rows.map(r => r.err),
        itemStyle: { color: "#ff1744" } },
    ],
  }, true);
}

async function loadRuns() {
  const q = new URLSearchParams({ limit: 300 });
  if (document.getElementById("sc-job").value) q.set("job", document.getElementById("sc-job").value);
  if (document.getElementById("sc-status").value) q.set("status", document.getElementById("sc-status").value);
  const rows = await api("/api/scrape/log?" + q);
  const tbody = document.querySelector("#tbl-runs tbody");
  tbody.innerHTML = rows.map((r, i) => `
    <tr${r.message ? ` class="clickable" data-msg="${i}"` : ""}>
      <td class="nowrap">${esc(r.ts)}</td><td>${esc(r.job)}</td><td>${esc(r.ticker)}</td>
      <td><span class="tag ${r.status === "success" ? "ok" : (r.status === "error" ? "err" : "warn")}">${esc(r.status)}</span></td>
      <td class="num">${fmt(r.duration_ms, 0)}</td>
      <td class="subtle" style="max-width:640px;overflow:hidden;text-overflow:ellipsis"
          title="${esc(r.message)}">${esc(r.message)}</td>
    </tr>
    <tr class="sc-detail" data-detail="${i}" style="display:none">
      <td colspan="6"><pre style="white-space:pre-wrap;word-break:break-word;margin:4px 0">${esc(r.message)}</pre></td>
    </tr>`).join("");
  tbody.querySelectorAll("[data-msg]").forEach(tr => tr.addEventListener("click", () => {
    const det = tbody.querySelector(`[data-detail="${tr.dataset.msg}"]`);
    det.style.display = det.style.display === "none" ? "" : "none";
  }));
}

function loadAll() { loadStatus(); loadHourly(); loadRuns(); }
document.getElementById("sc-apply").addEventListener("click", loadRuns);
document.getElementById("sc-switch-expired").addEventListener("click", async () => {
  const res = await api("/api/assets/switch-expired-to-archive", { method: "POST" });
  toast(res.started
    ? `switching ${res.candidates} expired option(s) to archive — progress in the run log below`
    : "no expired options need switching", res.started ? "ok" : "");
  if (res.started) setTimeout(loadAll, 2000);
});
hourlyChart = mkChart("sc-hourly");
poll(loadAll, 30);
