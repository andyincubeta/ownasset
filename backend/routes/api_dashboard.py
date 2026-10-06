"""Home dashboard data."""
import csv
import datetime
import io
import time

from flask import Blueprint, Response, jsonify, request

from backend import benchmarks, db, portfolio

bp = Blueprint("api_dashboard", __name__, url_prefix="/api/dashboard")

HOLDINGS_EXPORT_COLS = [
    ("asset_type", "TYPE"),
    ("ticker", "TICKER"),
    ("name", "NAME"),
    ("custodian", "CUSTODIAN"),
    ("account", "ACCOUNT"),
    ("quantity", "QTY"),
    ("price_native", "PRICE"),
    ("avg_cost", "BUY PRICE"),
    ("earning", "EARNING (NATIVE)"),
    ("earning_display", "EARNING (DISP)"),
    ("value_native", "VALUE (NATIVE)"),
    ("currency", "CCY"),
    ("fx_to_usd", "FX_TO_USD"),
    ("value_display", "VALUE (DISP)"),
    ("day_change_pct", "DAY %"),
    ("day_earn_pct", "DAY EARN %"),
    ("day_change_native", "DAY EARN (NATIVE)"),
    ("day_change_display", "DAY EARN (DISP)"),
    ("industry", "INDUSTRY"),
    ("sector", "SECTOR"),
    ("country", "COUNTRY"),
]


def _display_ccy() -> str:
    ccy = request.args.get("ccy")
    if ccy:
        return ccy.upper()
    with db.db_conn() as conn:
        return db.get_setting(conn, "display_currency", "USD")


@bp.get("/index-sparks")
def index_sparks():
    """Current value + 2-day mini series for the HOME-flagged benchmark indices.
    DB-only (quotes/intraday arrive via the scheduler); spark falls back to a
    two-point prev-close -> current line when no intraday bars exist yet."""
    out = []
    with db.db_conn() as conn:
        for bm in benchmarks.get_benchmarks(conn):
            if not bm["home"]:
                continue
            key, ticker, name = bm["key"], bm["ticker"], bm["name"]
            asset_id = benchmarks.ensure_benchmark_asset(conn, bm)
            q = conn.execute("SELECT price, prev_close, ts FROM latest_quotes "
                             "WHERE asset_id = ?", (asset_id,)).fetchone()
            closes = conn.execute(
                "SELECT date, COALESCE(close, adj_close) AS c FROM prices_daily "
                "WHERE asset_id = ? AND COALESCE(close, adj_close) IS NOT NULL "
                "ORDER BY date DESC LIMIT 2", (asset_id,)).fetchall()
            current = q["price"] if q and q["price"] is not None else \
                (float(closes[0]["c"]) if closes else None)
            prev = q["prev_close"] if q and q["prev_close"] is not None else \
                (float(closes[1]["c"]) if len(closes) > 1 else None)

            spark, boundary = [], None
            bar = conn.execute(
                "SELECT bar_interval FROM prices_intraday WHERE asset_id = ? "
                "ORDER BY ts DESC LIMIT 1", (asset_id,)).fetchone()
            if bar:
                rows = conn.execute(
                    "SELECT ts, close FROM prices_intraday "
                    "WHERE asset_id = ? AND bar_interval = ? AND close IS NOT NULL "
                    "AND date(ts, 'unixepoch') IN (SELECT DISTINCT date(ts, 'unixepoch') "
                    "  FROM prices_intraday WHERE asset_id = ? AND bar_interval = ? "
                    "  ORDER BY 1 DESC LIMIT 2) "
                    "ORDER BY ts", (asset_id, bar["bar_interval"],
                                    asset_id, bar["bar_interval"])).fetchall()
                spark = [[r["ts"], float(r["close"])] for r in rows]
                if spark:
                    def _day(ts):
                        return datetime.datetime.fromtimestamp(
                            ts, tz=datetime.timezone.utc).date()
                    last_day = _day(spark[-1][0])
                    if any(_day(ts) != last_day for ts, _c in spark):
                        boundary = next(ts for ts, _c in spark if _day(ts) == last_day)
            if not spark and current is not None and prev is not None:
                t_now = int(q["ts"]) if q and q["ts"] else int(time.time())
                spark = [[t_now - 86400, prev], [t_now, current]]
                boundary = t_now - 43200
            change = None if current is None or prev is None else current - prev
            out.append({
                "key": key, "ticker": ticker, "name": name,
                "short": bm["short"],
                "current": current, "prev_close": prev, "change": change,
                "change_pct": None if change is None or not prev else change / prev * 100,
                "spark": spark, "boundary_ts": boundary,
            })
    return jsonify(out)


@bp.get("/summary")
def summary():
    ccy = _display_ccy()
    with db.db_conn() as conn:
        return jsonify(portfolio.summary(conn, ccy))


@bp.get("/position-trades")
def position_trades():
    """Trade-by-trade history behind one holding: every qty-affecting txn with
    side, unit price, fees and cycle-aware running averages. Prices and
    averages are per unit (contract multiplier removed, like BUY PRICE)."""
    try:
        asset_id = int(request.args["asset_id"])
        account_id = int(request.args["account_id"])
    except (KeyError, ValueError):
        return jsonify({"error": "asset_id and account_id are required"}), 400
    with db.db_conn() as conn:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if asset is None:
            return jsonify({"error": "asset not found"}), 404
        mult = 1.0
        if asset["asset_type"] in portfolio.DERIVATIVE_TYPES:
            d = conn.execute("SELECT multiplier FROM derivative_details WHERE asset_id = ?",
                             (asset_id,)).fetchone()
            mult = float(d["multiplier"]) if d and d["multiplier"] else 1.0
        hist = portfolio.trade_history(conn).get((account_id, asset_id))
        tickers = {r["id"]: r["ticker"] for r in conn.execute("SELECT id, ticker FROM assets")}
        group_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM txn_groups")}

        def per_unit(v):
            return None if v is None else (v / mult if mult else v)

        trades = []
        for e in (hist["events"] if hist else []):
            trades.append({
                "txn_id": e["txn_id"], "txn_date": e["txn_date"],
                "side": e["side"], "delta": e["delta"], "qty_after": e["qty_after"],
                "cycle": e["cycle"], "px": per_unit(e["px"]), "cash": e["cash"],
                "avg_buy": per_unit(e["avg_buy"]), "avg_sell": per_unit(e["avg_sell"]),
                "fee_amount": e["fee_amount"],
                "fee_ticker": tickers.get(e["fee_asset_id"]),
                "group_id": e["group_id"],
                "group_name": group_names.get(e["group_id"]),
                "note": e["note"],
            })
        s = hist["state"] if hist else None
        avg_buy = s["bc"] / s["bq"] if s and s["bq"] > 1e-9 else None
        avg_sell = s["sc"] / s["sq"] if s and s["sq"] > 1e-9 else None
        realized = None
        if avg_buy is not None and avg_sell is not None:
            realized = (avg_sell - avg_buy) * min(s["bq"], s["sq"])
        return jsonify({
            "asset_id": asset_id, "account_id": account_id,
            "ticker": asset["ticker"], "currency": asset["currency"],
            "multiplier": mult,
            "open_qty": s["qty"] if s else 0.0,
            "current_cycle": hist["cycle"] if hist else 0,
            "avg_buy": per_unit(avg_buy), "avg_sell": per_unit(avg_sell),
            "realized": realized,
            "trades": trades,
        })


@bp.get("/series")
def series():
    ccy = _display_ccy()
    mode = request.args.get("mode", "line")
    rebase = request.args.get("rebase") or None
    with db.db_conn() as conn:
        return jsonify(portfolio.portfolio_series(conn, ccy, mode=mode, rebase_date=rebase))


@bp.get("/benchmark")
def benchmark():
    key = (request.args.get("symbol") or "").strip().upper()
    with db.db_conn() as conn:
        bm = benchmarks.get_benchmark(conn, key)
        if not bm:
            keys = [b["key"] for b in benchmarks.get_benchmarks(conn)]
            return jsonify({"error": f"symbol must be one of {keys}"}), 400
        ticker = bm["ticker"]
        asset_id = benchmarks.ensure_benchmark_asset(conn, bm)
        have = conn.execute("SELECT COUNT(*) FROM prices_daily WHERE asset_id = ?",
                            (asset_id,)).fetchone()[0]
    if not have:
        from backend.scheduler import backfill_daily_asset  # lazy: pulls in apscheduler
        try:
            backfill_daily_asset(asset_id, ticker)
        except Exception as exc:
            return jsonify({"error": f"benchmark fetch failed: {exc}"}), 502
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT date, COALESCE(adj_close, close) AS c FROM prices_daily "
            "WHERE asset_id = ? AND COALESCE(adj_close, close) IS NOT NULL "
            "ORDER BY date", (asset_id,)).fetchall()
    return jsonify({"symbol": key, "ticker": ticker,
                    "dates": [r["date"] for r in rows],
                    "closes": [float(r["c"]) for r in rows]})


@bp.get("/holdings.csv")
def holdings_csv():
    ccy = _display_ccy()
    with db.db_conn() as conn:
        data = portfolio.summary(conn, ccy)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([label for _, label in HOLDINGS_EXPORT_COLS])
    for row in data["holdings"]:
        writer.writerow(["" if row.get(key) is None else row.get(key)
                         for key, _ in HOLDINGS_EXPORT_COLS])

    filename = f"holdings_{data['display_currency']}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})
