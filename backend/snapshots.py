"""Daily snapshots: end-of-day portfolio state per (date, asset, account).

take_snapshot(): today's state using live quotes.
rebuild_snapshots(): retroactive reconstruction from transactions x prices_daily
x fx_daily (used after backfills or ledger edits).
"""
import datetime
import sqlite3

from backend import db, fx, portfolio
from backend.db import today_str
from backend.logger import get_logger
from backend.option_archive import parse_occ_ticker

log = get_logger("dashboard.snapshots")


def _norm_rebuild_date(value: str | None) -> str | None:
    """Normalize date/datetime-ish inputs to YYYY-MM-DD for rebuild bounds."""
    s = str(value or "").strip().replace("T", " ")
    if not s:
        return None
    try:
        return datetime.date.fromisoformat(s[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("invalid start/end date") from exc


def _upsert(conn, date, asset_id, account_id, qty, price, currency, rate, value_usd):
    conn.execute("""
        INSERT INTO daily_snapshots
            (date, asset_id, account_id, quantity, price_native, currency, fx_to_usd, value_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date, asset_id, account_id) DO UPDATE SET
            quantity = excluded.quantity, price_native = excluded.price_native,
            currency = excluded.currency, fx_to_usd = excluded.fx_to_usd,
            value_usd = excluded.value_usd
    """, (date, asset_id, account_id, qty, price, currency, rate, value_usd))


def take_snapshot(conn: sqlite3.Connection, date: str | None = None) -> int:
    """Snapshot current holdings at live prices. Returns row count."""
    date = date or today_str()
    holds = portfolio.enrich_holdings(conn, portfolio.holdings(conn), "USD")
    count = 0
    for h in holds:
        _upsert(conn, date, h["asset_id"], h["account_id"], h["quantity"],
                h["price_native"], h["asset"]["currency"], h["fx_to_usd"], h["value_usd"])
        count += 1
    conn.execute("DELETE FROM daily_snapshots WHERE date = ? AND asset_id NOT IN "
                 "(SELECT asset_id FROM (%s))" % portfolio.HOLDINGS_SQL.replace(":asof", "'9999-12-31'"),
                 (date,))
    log.info("snapshot %s: %d positions", date, count)
    return count


def rebuild_snapshots(conn: sqlite3.Connection, start: str | None = None,
                      end: str | None = None) -> int:
    """Rebuild snapshots for every date in [start, end] from historical data.
    A full rebuild (no explicit bounds) wipes the whole table first so rows
    outside the ledger's lifetime (e.g. from since-deleted prices) don't linger."""
    full_rebuild = start in (None, "") and end in (None, "")
    first_txn = conn.execute("SELECT MIN(txn_date) FROM transactions").fetchone()[0]
    if first_txn is None:
        if full_rebuild:
            conn.execute("DELETE FROM daily_snapshots")
        return 0
    start = _norm_rebuild_date(start if start not in (None, "") else first_txn)
    end = _norm_rebuild_date(end if end not in (None, "") else today_str())
    if start is None or end is None:
        raise ValueError("invalid start/end date")

    d = datetime.date.fromisoformat(start)
    d_end = datetime.date.fromisoformat(end)
    if d > d_end:
        raise ValueError("invalid start/end date")

    if full_rebuild:
        conn.execute("DELETE FROM daily_snapshots")
    else:
        conn.execute("DELETE FROM daily_snapshots WHERE date >= ? AND date <= ?", (start, end))

    total = 0
    while d <= d_end:
        ds = d.isoformat()
        holds = portfolio.holdings(conn, as_of=ds)
        for h in holds:
            asset = h["asset"]
            price, _src = portfolio.price_native(conn, asset, as_of=ds)
            rate, _ok = fx.rate_to_usd(conn, asset["currency"], on_date=ds)
            mult = (h["derivative"] or {}).get("multiplier") or 1.0 \
                if asset["asset_type"] in portfolio.DERIVATIVE_TYPES else 1.0
            value = h["quantity"] * price * mult * rate if price is not None else None
            _upsert(conn, ds, h["asset_id"], h["account_id"], h["quantity"],
                    price, asset["currency"], rate, value)
            total += 1
        d += datetime.timedelta(days=1)
    log.info("rebuilt snapshots %s..%s (%d rows)", start, end, total)
    return total


# ------------------------------------------------------- daily values -----

# column grouping for the daily-values page: currencies first, options last
DV_GROUP_NAMES = ("currency", "equity", "other", "option")


def _dv_group(asset_type: str) -> int:
    if asset_type == "cash":
        return 0
    if asset_type in ("equity", "etf"):
        return 1
    if asset_type in ("option", "future"):
        return 3
    return 2


def _live_today_cells(conn: sqlite3.Connection, mode: str, assets: dict[int, dict]) \
        -> tuple[dict[int, float | None], dict[int, float], dict[int, float]] | None:
    """Provisional data for today from live holdings x latest prices/FX
    (same math as take_snapshot, but nothing is persisted). Extends `assets`
    with any held asset not yet present in the snapshot table.
    Returns (mode cells, value_usd per asset, value_native per asset),
    or None when there are no holdings."""
    holds = portfolio.enrich_holdings(conn, portfolio.holdings(conn), "USD")
    if not holds:
        return None
    cells: dict[int, float | None] = {}
    vals_usd: dict[int, float] = {}
    vals_nat: dict[int, float] = {}
    for h in holds:
        aid = h["asset_id"]
        if aid not in assets:
            row = conn.execute(
                "SELECT id, ticker, name, currency, asset_type FROM assets WHERE id = ?",
                (aid,)).fetchone()
            if row is None:
                continue
            assets[aid] = dict(row)
        if mode == "quantity":
            cells[aid] = (cells.get(aid) or 0) + h["quantity"]
        elif mode == "price":
            cells[aid] = h["price_native"] if h["price_native"] is not None else cells.get(aid)
        if h["value_usd"] is not None:
            vals_usd[aid] = vals_usd.get(aid, 0.0) + h["value_usd"]
            vals_nat[aid] = vals_nat.get(aid, 0.0) + (h["value_native"] or 0.0)
    return cells, vals_usd, vals_nat


def daily_values_matrix(conn: sqlite3.Connection, mode: str = "value",
                        display_cur: str = "USD", limit_days: int = 0,
                        include_today: bool = False) -> dict:
    """Matrix for the daily-values page: rows = dates desc, cols = assets,
    TOTAL as first data column. mode: value | quantity | price.
    include_today: prepend a provisional row for today from live holdings x
    latest prices/FX; it supersedes any frozen row for today in the response
    (the snapshot table itself is untouched).
    Assets denominated in the display currency convert identically (native
    value passes through, no USD round-trip), so e.g. HKD cash always shows
    its face value when the display currency is HKD."""
    disp_ccy = (display_cur or "USD").upper()
    assets = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, ticker, name, currency, asset_type FROM assets WHERE id IN "
        "(SELECT DISTINCT asset_id FROM daily_snapshots) ORDER BY ticker")}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_snapshots ORDER BY date DESC"
        + (f" LIMIT {int(limit_days)}" if limit_days else ""))]
    cells: dict[tuple[str, int], float | None] = {}
    vals_usd: dict[tuple[str, int], float] = {}
    vals_nat: dict[tuple[str, int], float] = {}
    for r in conn.execute("SELECT * FROM daily_snapshots"):
        key = (r["date"], r["asset_id"])
        if mode == "quantity":
            val = r["quantity"]
        elif mode == "price":
            val = r["price_native"]
        else:
            val = r["value_usd"]
        prev = cells.get(key)
        cells[key] = (prev or 0) + (val or 0) if mode != "price" else (val or prev)
        if r["value_usd"] is not None:
            vals_usd[key] = vals_usd.get(key, 0.0) + r["value_usd"]
            fxr = r["fx_to_usd"]
            vals_nat[key] = vals_nat.get(key, 0.0) \
                + (r["value_usd"] / fxr if fxr else r["value_usd"])

    today = today_str()
    live = None
    if include_today:
        live = _live_today_cells(conn, mode, assets)
        if live is not None:
            dates = [d for d in dates if d != today]

    col_ids = sorted(assets, key=lambda aid: (
        _dv_group(assets[aid].get("asset_type") or ""), assets[aid]["ticker"] or ""))

    def _display_vals(v_usd: dict, v_nat: dict, key_of, m: float) -> dict[int, float | None]:
        """Per-asset display values: identity for display-currency assets."""
        out: dict[int, float | None] = {}
        for aid in col_ids:
            key = key_of(aid)
            if key not in v_usd:
                out[aid] = None
            elif (assets[aid].get("currency") or "").upper() == disp_ccy:
                out[aid] = v_nat[key]
            else:
                out[aid] = v_usd[key] * m
        return out

    def _row_cells(disp: dict[int, float | None], mode_cells, key_of) -> list:
        row_vals: list = [round(sum(v for v in disp.values() if v is not None), 2)]
        for aid in col_ids:
            if mode == "value":
                v = disp[aid]
                row_vals.append(None if v is None else round(v, 2))
            else:
                v = mode_cells.get(key_of(aid))
                row_vals.append(None if v is None else round(v, 6))
        return row_vals

    rows = []
    if live is not None:
        live_cells, live_usd, live_nat = live
        m, _ = fx.usd_to_display(conn, display_cur)
        disp = _display_vals(live_usd, live_nat, lambda aid: aid, m)
        rows.append({"date": today, "cells": _row_cells(disp, live_cells, lambda aid: aid),
                     "provisional": True})
    for dt in dates:
        m, _ = fx.usd_to_display(conn, display_cur, dt)
        disp = _display_vals(vals_usd, vals_nat, lambda aid: (dt, aid), m)
        rows.append({"date": dt, "cells": _row_cells(disp, cells, lambda aid: (dt, aid))})
    return {
        "mode": mode, "display_currency": display_cur,
        "columns": [{"asset_id": aid, "ticker": assets[aid]["ticker"],
                     "name": assets[aid]["name"],
                     "asset_type": assets[aid].get("asset_type") or "",
                     "group": DV_GROUP_NAMES[_dv_group(assets[aid].get("asset_type") or "")]}
                    for aid in col_ids],
        "rows": rows,
    }


def asset_prices_matrix(conn: sqlite3.Connection, limit_days: int = 0,
                        include_today: bool = True) -> dict:
    """Matrix for the asset-prices page: rows = snapshot dates desc, cols =
    every asset ever held (appears in snapshots or transactions; cash/loan
    excluded), cells = end-of-day price in the asset's native currency.
    Tracked assets read prices_daily; manual assets carry the latest manual
    price on/before each date. include_today prepends a provisional row from
    latest_quotes (superseding any frozen row for today)."""
    assets = {r["id"]: dict(r) for r in conn.execute("""
        SELECT a.id, a.ticker, a.name, a.currency, a.asset_type, d.expiry
          FROM assets a
          LEFT JOIN derivative_details d ON d.asset_id = a.id
         WHERE a.asset_type NOT IN ('cash', 'loan') AND (
               a.id IN (SELECT DISTINCT asset_id FROM daily_snapshots)
            OR a.id IN (SELECT DISTINCT outflow_asset_id FROM transactions
                       WHERE outflow_asset_id IS NOT NULL)
            OR a.id IN (SELECT DISTINCT inflow_asset_id FROM transactions
                       WHERE inflow_asset_id IS NOT NULL))
         ORDER BY a.ticker""")}
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_snapshots ORDER BY date DESC"
        + (f" LIMIT {int(limit_days)}" if limit_days else ""))]
    col_ids = list(assets)  # insertion order = ticker order

    price: dict[tuple[str, int], float] = {}
    manual: dict[int, list[tuple[str, float]]] = {}
    if col_ids:
        ph = ",".join("?" * len(col_ids))
        for r in conn.execute(
                f"SELECT asset_id, date, COALESCE({db.daily_close_sql(conn)}, adj_close) AS c "
                f"FROM prices_daily WHERE asset_id IN ({ph})", col_ids):
            if r["c"] is not None:
                price[(r["date"], r["asset_id"])] = float(r["c"])
        for r in conn.execute(
                f"SELECT asset_id, price_date, price FROM manual_prices "
                f"WHERE asset_id IN ({ph}) ORDER BY price_date", col_ids):
            manual.setdefault(r["asset_id"], []).append(
                (r["price_date"][:10], float(r["price"])))

    def _manual_asof(aid: int, date: str) -> float | None:
        best = None
        for d, p in manual.get(aid, ()):
            if d > date:
                break
            best = p
        return best

    def _cell(aid: int, date: str) -> float | None:
        v = price.get((date, aid))
        v = v if v is not None else _manual_asof(aid, date)
        return None if v is None else round(v, 6)

    rows = []
    today = today_str()
    if include_today:
        dates = [d for d in dates if d != today]  # live row supersedes frozen
        quotes = {r["asset_id"]: float(r["price"]) for r in conn.execute(
            "SELECT asset_id, price FROM latest_quotes WHERE price IS NOT NULL")}
        cells = [quotes.get(aid) if quotes.get(aid) is not None else _cell(aid, today)
                 for aid in col_ids]
        if any(v is not None for v in cells):
            rows.append({"date": today, "cells": cells, "provisional": True})
    for dt in dates:
        rows.append({"date": dt, "cells": [_cell(aid, dt) for aid in col_ids]})

    def _expiry(a: dict) -> str | None:
        """Options/futures: details expiry, else parsed from the OCC ticker."""
        exp = str(a.get("expiry") or "")[:10]
        if exp:
            return exp
        if (a.get("asset_type") or "") == "option":
            parsed = parse_occ_ticker(a.get("ticker") or "")
            if parsed:
                return parsed["expiry"]
        return None

    return {
        "columns": [{"asset_id": aid, "ticker": assets[aid]["ticker"],
                     "name": assets[aid]["name"],
                     "currency": assets[aid]["currency"],
                     "asset_type": assets[aid].get("asset_type") or "",
                     "expiry": _expiry(assets[aid])}
                    for aid in col_ids],
        "rows": rows,
    }
