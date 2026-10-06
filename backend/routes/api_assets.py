"""Assets CRUD, Yahoo validation, full-info detail, manual prices,
price-source switching (yahoo / options archive / manual)."""
import json
import sqlite3
import threading

from flask import Blueprint, jsonify, request

import config
from backend import db, option_archive, snapshots
from backend.logger import get_logger
from backend.yahoo import YahooError, client

bp = Blueprint("api_assets", __name__, url_prefix="/api")
log = get_logger("dashboard.api")


def _body() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/assets")
def list_assets():
    include_inactive = request.args.get("all") == "1"
    q = "SELECT * FROM assets" + ("" if include_inactive else " WHERE active = 1")
    with db.db_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            q + " ORDER BY CASE WHEN asset_type = 'cash' THEN 0 "
            "WHEN asset_type IN ('equity', 'etf', 'bond', 'crypto', 'real_estate', 'loan', 'other') THEN 1 "
            "WHEN asset_type IN ('option', 'future') THEN 2 ELSE 3 END, ticker")]
        for r in rows:
            r.pop("info_json", None)  # keep list payload lean; detail endpoint has it
            deriv = conn.execute("SELECT * FROM derivative_details WHERE asset_id = ?",
                                 (r["id"],)).fetchone()
            r["derivative"] = dict(deriv) if deriv else None
    return jsonify(rows)


@bp.get("/assets/validate/<path:ticker>")
def validate_ticker(ticker):
    """Live Yahoo lookup used by the add-ticker modal (preview before saving)."""
    try:
        info = client.get_stock_info(ticker.upper())
    except YahooError as exc:
        return jsonify({"error": str(exc)}), 502
    if info is None:
        return jsonify({"error": f"no Yahoo result for {ticker}"}), 404
    return jsonify({
        "ticker": ticker.upper(),
        "name": info.get("longName") or info.get("shortName") or "",
        "currency": info.get("currency") or "USD",
        "quote_type": info.get("quoteType") or "",
        "industry": info.get("industry") or "",
        "sector": info.get("sector") or "",
        "country": info.get("country") or "",
        "price": info.get("regularMarketPrice") or info.get("currentPrice"),
    })


@bp.post("/assets")
def create_asset():
    data = _body()
    ticker = (data.get("ticker") or "").strip().upper()
    if not ticker:
        return jsonify({"error": "ticker required"}), 400
    asset_type = data.get("asset_type") or "equity"
    if asset_type not in config.ASSET_TYPES:
        return jsonify({"error": f"asset_type must be one of {config.ASSET_TYPES}"}), 400
    is_yahoo = 1 if data.get("is_yahoo_tracked", True) else 0
    provider = (data.get("price_provider") or "yahoo").strip()
    if provider not in config.PRICE_PROVIDERS:
        return jsonify({"error": f"price_provider must be one of {config.PRICE_PROVIDERS}"}), 400

    name = data.get("name") or ""
    currency = (data.get("currency") or "USD").upper()
    industry = sector = country = quote_type = ""
    info_json = "{}"
    if is_yahoo and provider == "yahoo":
        try:
            info = client.get_stock_info(ticker)
        except YahooError as exc:
            if asset_type not in ("option", "future"):
                return jsonify({"error": f"yahoo validation failed: {exc}"}), 502
            info = None
        if info is None and asset_type not in ("option", "future"):
            return jsonify({"error": f"ticker {ticker} not found on Yahoo"}), 404
        if info is not None:
            name = name or info.get("longName") or info.get("shortName") or ticker
            currency = (info.get("currency") or currency).upper()
            industry = info.get("industry") or ""
            sector = info.get("sector") or ""
            country = info.get("country") or ""
            quote_type = info.get("quoteType") or ""
            info_json = json.dumps(info)
        elif asset_type in ("option", "future"):
            name = name or ticker
    elif is_yahoo:  # archive provider: no yahoo validation possible
        name = name or ticker

    try:
        with db.db_conn() as conn:
            conn.execute("INSERT OR IGNORE INTO currencies(code, name) VALUES (?, ?)",
                         (currency, currency))
            db.ensure_cash_asset(conn, currency)
            cur = conn.execute(
                "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked, "
                "price_provider, industry, sector, country, quote_type, notes, info_json, "
                "info_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ticker, name, asset_type, currency, is_yahoo, provider, industry, sector,
                 country, quote_type, data.get("notes") or "", info_json,
                 db.now_str() if is_yahoo else None))
            asset_id = cur.lastrowid
            deriv = data.get("derivative")
            if deriv and asset_type in ("option", "future"):
                conn.execute(
                    "INSERT INTO derivative_details(asset_id, underlying_ticker, contract_type, "
                    "strike, expiry, multiplier) VALUES (?, ?, ?, ?, ?, ?)",
                    (asset_id, (deriv.get("underlying_ticker") or "").upper(),
                     deriv.get("contract_type") or ("future" if asset_type == "future" else "call"),
                     deriv.get("strike"), deriv.get("expiry"),
                     deriv.get("multiplier") or 100))
    except sqlite3.IntegrityError:
        return jsonify({"error": f"asset {ticker} already exists"}), 409

    if is_yahoo:
        from backend import scheduler
        scheduler.trigger_backfill(asset_id, ticker)
    log.info("asset created: %s (%s, yahoo=%s, provider=%s)",
             ticker, asset_type, bool(is_yahoo), provider)
    return jsonify({"id": asset_id, "ticker": ticker, "currency": currency, "name": name}), 201


@bp.get("/assets/<int:asset_id>")
def asset_detail(asset_id):
    with db.db_conn() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        data = dict(row)
        data["info"] = db.parse_json(data.pop("info_json", "{}"), {})
        deriv = conn.execute("SELECT * FROM derivative_details WHERE asset_id = ?",
                             (asset_id,)).fetchone()
        data["derivative"] = dict(deriv) if deriv else None
        lq = conn.execute("SELECT * FROM latest_quotes WHERE asset_id = ?",
                          (asset_id,)).fetchone()
        data["latest_quote"] = dict(lq) if lq else None
    return jsonify(data)


@bp.put("/assets/<int:asset_id>")
def update_asset(asset_id):
    data = _body()
    provider = data.get("price_provider")
    if provider is not None and provider not in config.PRICE_PROVIDERS:
        return jsonify({"error": f"price_provider must be one of {config.PRICE_PROVIDERS}"}), 400
    with db.db_conn() as conn:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        conn.execute(
            "UPDATE assets SET name = ?, asset_type = ?, notes = ?, active = ?, "
            "industry = ?, sector = ?, country = ?, price_provider = ? WHERE id = ?",
            (data.get("name", row["name"]), data.get("asset_type", row["asset_type"]),
             data.get("notes", row["notes"]),
             1 if data.get("active", row["active"]) else 0,
             data.get("industry", row["industry"]), data.get("sector", row["sector"]),
             data.get("country", row["country"]),
             provider if provider is not None else row["price_provider"], asset_id))
        deriv = data.get("derivative")
        if deriv:
            conn.execute(
                "INSERT INTO derivative_details(asset_id, underlying_ticker, contract_type, "
                "strike, expiry, multiplier) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET underlying_ticker=excluded.underlying_ticker, "
                "contract_type=excluded.contract_type, strike=excluded.strike, "
                "expiry=excluded.expiry, multiplier=excluded.multiplier",
                (asset_id, (deriv.get("underlying_ticker") or "").upper(),
                 deriv.get("contract_type") or "call", deriv.get("strike"),
                 deriv.get("expiry"), deriv.get("multiplier") or 100))
    return jsonify({"ok": True})


# ------------------------------------------------------- price source -----

def _asset_with_deriv(conn, asset_id):
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    deriv = conn.execute("SELECT * FROM derivative_details WHERE asset_id = ?",
                         (asset_id,)).fetchone()
    return (dict(asset) if asset else None), (dict(deriv) if deriv else None)


@bp.get("/assets/<int:asset_id>/archive-preview")
def archive_preview(asset_id):
    """Dry-run fetch from the options archive (nothing is written)."""
    with db.db_conn() as conn:
        asset, deriv = _asset_with_deriv(conn, asset_id)
    if asset is None:
        return jsonify({"error": "not found"}), 404
    try:
        bars = option_archive.history_for_asset(asset, deriv)
    except option_archive.ArchiveError as exc:
        return jsonify({"error": str(exc)}), 502
    if not bars:
        return jsonify({"rows": 0})
    return jsonify({"rows": len(bars), "first_date": bars[0]["date"],
                    "last_date": bars[-1]["date"], "last_close": bars[-1]["close"]})


@bp.post("/assets/<int:asset_id>/switch-source")
def switch_source(asset_id):
    """Set the effective price source: manual | yahoo | option_archive.
    Archive switches backfill synchronously and resync snapshots before returning."""
    from backend import scheduler
    source = (_body().get("source") or "").strip()
    if source not in ("manual", "yahoo", "option_archive"):
        return jsonify({"error": "source must be manual, yahoo or option_archive"}), 400
    with db.db_conn() as conn:
        asset, _deriv = _asset_with_deriv(conn, asset_id)
    if asset is None:
        return jsonify({"error": "not found"}), 404

    if source == "manual":
        with db.db_conn() as conn:
            conn.execute("UPDATE assets SET is_yahoo_tracked = 0 WHERE id = ?", (asset_id,))
        log.info("asset %s switched to manual pricing", asset["ticker"])
        return jsonify({"ok": True, "source": "manual"})

    if source == "yahoo":
        with db.db_conn() as conn:
            conn.execute("UPDATE assets SET is_yahoo_tracked = 1, price_provider = 'yahoo' "
                         "WHERE id = ?", (asset_id,))
        scheduler.trigger_backfill(asset_id, asset["ticker"])
        log.info("asset %s switched to yahoo pricing", asset["ticker"])
        return jsonify({"ok": True, "source": "yahoo", "backfill": "started"})

    # option_archive: fetch BEFORE flipping flags so a failed switch changes nothing
    try:
        bars = scheduler.backfill_archive_asset(asset_id)
    except option_archive.ArchiveError as exc:
        return jsonify({"error": str(exc)}), 502
    with db.db_conn() as conn:
        conn.execute("UPDATE assets SET is_yahoo_tracked = 1, "
                     "price_provider = 'option_archive' WHERE id = ?", (asset_id,))
        rng = conn.execute("SELECT MIN(date), MAX(date) FROM prices_daily WHERE asset_id = ?",
                           (asset_id,)).fetchone()
        if rng and rng[0]:
            _resync_snapshots(conn, rng[0])
    log.info("asset %s switched to option archive (%d bars)", asset["ticker"], bars)
    return jsonify({"ok": True, "source": "option_archive", "bars": bars,
                    "first_date": rng[0] if rng else None,
                    "last_date": rng[1] if rng else None})


def _expired_archive_candidates(conn) -> list[dict]:
    """Option assets past expiry whose price source is not the archive yet."""
    today = db.today_str()
    out = []
    for r in conn.execute(
            "SELECT a.id, a.ticker, a.is_yahoo_tracked, a.price_provider, d.expiry "
            "FROM assets a LEFT JOIN derivative_details d ON d.asset_id = a.id "
            "WHERE a.asset_type = 'option'"):
        if r["is_yahoo_tracked"] and r["price_provider"] == "option_archive":
            continue
        expiry = str(r["expiry"])[:10] if r["expiry"] else ""
        if not expiry:
            parsed = option_archive.parse_occ_ticker(r["ticker"])
            expiry = parsed["expiry"] if parsed else ""
        if expiry and expiry < today:
            out.append({"id": r["id"], "ticker": r["ticker"]})
    return out


def _bulk_switch_worker(candidates: list[dict]):
    """Switch each expired option to the archive; flags first so failures are
    retried by the daily job. One snapshot resync at the end covers all assets."""
    from backend import scheduler
    from backend.yahoo import polite_sleep
    ok = err = 0
    first_dates = []
    for c in candidates:
        try:
            with db.db_conn() as conn:
                conn.execute("UPDATE assets SET is_yahoo_tracked = 1, "
                             "price_provider = 'option_archive' WHERE id = ?", (c["id"],))
            bars = scheduler.backfill_archive_asset(c["id"])
            with db.db_conn() as conn:
                first = conn.execute("SELECT MIN(date) FROM prices_daily WHERE asset_id = ?",
                                     (c["id"],)).fetchone()[0]
            if first:
                first_dates.append(first)
            ok += 1
            scheduler.log_run("daily", "success", 0, ticker=c["ticker"],
                              message=f"switched to archive: {bars} bars")
        except Exception as exc:
            err += 1
            scheduler.log_run("daily", "error", 0, ticker=c["ticker"], message=repr(exc))
        polite_sleep()
    if first_dates:
        with db.db_conn() as conn:
            _resync_snapshots(conn, min(first_dates))
    scheduler.log_run("daily", "success" if not err else "error", 0,
                      message=f"bulk expired->archive: {ok} switched, {err} failed")


@bp.post("/assets/switch-expired-to-archive")
def switch_expired_to_archive():
    """One-click: move every expired option to the archive source (background)."""
    with db.db_conn() as conn:
        candidates = _expired_archive_candidates(conn)
    if not candidates:
        return jsonify({"candidates": 0, "started": False})
    threading.Thread(target=_bulk_switch_worker, args=(candidates,), daemon=True,
                     name="bulk-archive-switch").start()
    log.info("bulk archive switch started for %d expired options", len(candidates))
    return jsonify({"candidates": len(candidates), "started": True})


# ------------------------------------------------------- manual prices ----

def _resync_snapshots(conn, *dates):
    """A manual price on date D affects valuations from D onward; rebuild
    the snapshot range min(dates)..today so history follows the change."""
    today = db.today_str()
    valid = [str(d)[:10] for d in dates if d]
    start = min(valid) if valid else None
    if not start or start > today:
        return
    try:
        snapshots.rebuild_snapshots(conn, start, today)
    except ValueError as exc:
        log.warning("snapshot resync skipped (%s..%s): %s", start, today, exc)


@bp.get("/assets/<int:asset_id>/manual-prices")
def list_manual_prices(asset_id):
    with db.db_conn() as conn:
        rows = conn.execute("SELECT * FROM manual_prices WHERE asset_id = ? "
                            "ORDER BY price_date DESC", (asset_id,))
        return jsonify(db.rows_to_dicts(rows))


@bp.post("/assets/<int:asset_id>/manual-prices")
def add_manual_price(asset_id):
    data = _body()
    price_date, price = data.get("price_date"), data.get("price")
    if not price_date or price is None:
        return jsonify({"error": "price_date and price required"}), 400
    with db.db_conn() as conn:
        conn.execute(
            "INSERT INTO manual_prices(asset_id, price_date, price, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(asset_id, price_date) DO UPDATE SET price = excluded.price, "
            "note = excluded.note",
            (asset_id, price_date, float(price), data.get("note") or ""))
        _resync_snapshots(conn, price_date)
    return jsonify({"ok": True}), 201


@bp.put("/manual-prices/<int:price_id>")
def update_manual_price(price_id):
    data = _body()
    with db.db_conn() as conn:
        row = conn.execute("SELECT * FROM manual_prices WHERE id = ?", (price_id,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        conn.execute("UPDATE manual_prices SET price_date = ?, price = ?, note = ? WHERE id = ?",
                     (data.get("price_date", row["price_date"]),
                      float(data.get("price", row["price"])),
                      data.get("note", row["note"]), price_id))
        _resync_snapshots(conn, row["price_date"], data.get("price_date"))
    return jsonify({"ok": True})


@bp.delete("/manual-prices/<int:price_id>")
def delete_manual_price(price_id):
    with db.db_conn() as conn:
        row = conn.execute("SELECT price_date FROM manual_prices WHERE id = ?",
                           (price_id,)).fetchone()
        conn.execute("DELETE FROM manual_prices WHERE id = ?", (price_id,))
        if row is not None:
            _resync_snapshots(conn, row["price_date"])
    return jsonify({"ok": True})
