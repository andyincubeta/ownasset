/* Asset end-price matrix: rows = dates, cols = every asset ever held. */
"use strict";

async function load() {
  const days = document.getElementById("ap-days").value;
  const data = await api(`/api/history/asset-prices?days=${days}`);
  const dp = +document.getElementById("ap-dp").value;
  const thead = document.querySelector("#tbl-ap thead");
  const tbody = document.querySelector("#tbl-ap tbody");
  const expiry = data.columns.map(c => c.expiry || null);
  const refDate = data.rows.length ? data.rows[0].date : "";  // newest row = today
  thead.innerHTML = `<tr><th>DATE</th>${data.columns.map((c, i) => {
    const cls = [(c.ticker || "").length > 8 ? "th-opt" : "",
                 expiry[i] && refDate > expiry[i] ? "expired" : ""].filter(Boolean).join(" ");
    return `<th${cls ? ` class="${cls}"` : ""} title="${esc(c.name || c.ticker)} · ${esc(c.currency)}">` +
      `${esc(c.ticker)}<div class="dim">${esc(c.currency)}</div></th>`;
  }).join("")}</tr>`;
  tbody.innerHTML = data.rows.map(r => {
    const tag = r.provisional ? ' <span class="live-tag">LIVE</span>' : "";
    return `<tr${r.provisional ? ' class="provisional"' : ""}>
      <td class="nowrap">${formatDisplayDate(r.date)}${tag}</td>
      ${r.cells.map((v, i) => {
        const dead = expiry[i] && r.date > expiry[i];  // strictly after expiry
        return `<td class="num${dead ? " expired" : ""}">${v == null ? "—" : fmt(v, dp)}</td>`;
      }).join("")}
    </tr>`;
  }).join("");
}

["ap-days", "ap-dp"].forEach(id =>
  document.getElementById(id).addEventListener("change", load));
document.getElementById("ap-csv").addEventListener("click", () => {
  window.location = `/api/history/asset-prices.csv?days=${document.getElementById("ap-days").value}`;
});
poll(load, 60);
