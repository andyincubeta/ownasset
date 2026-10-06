"""Performance analytics: monthly/yearly returns table + index comparison series.

Monthly return = Modified Dietz: (EMV - BMV - F) / (BMV + Σ w_i·F_i), where F are
external flows (value moving between an external party and the user's accounts)
weighted by the fraction of the period remaining.

Convention: cash received from / paid to the external party 'BROKER' (the CSV
importers' label for dividends, interest and withholding tax) is investment
income, part of the return — NOT an external flow. Real deposits/withdrawals
use any other party label. Override via setting perf_income_parties
(comma-separated labels).
"""
import calendar

from flask import Blueprint, jsonify, request

from backend import benchmarks, db, fx, portfolio
from backend.logger import get_logger

bp = Blueprint("api_performance", __name__, url_prefix="/api/performance")
log = get_logger("dashboard.api")


def _display_ccy() -> str:
    ccy = request.args.get("ccy")
    if ccy:
        return ccy.upper()
    with db.db_conn() as conn:
        return db.get_setting(conn, "display_currency", "USD")


@bp.get("/indices")
def indices():
    with db.db_conn() as conn:
        return jsonify([{"key": b["key"], "ticker": b["ticker"], "name": b["name"],
                         "short": b["short"]} for b in benchmarks.get_benchmarks(conn)])


# ------------------------------------------------------------- flows ------

def _income_parties(conn) -> set[str]:
    raw = db.get_setting(conn, "perf_income_parties", "BROKER") or ""
    return {p.strip().upper() for p in raw.split(",") if p.strip()}


def _external_flows(conn, display_cur: str) -> list[tuple[str, float]]:
    """[(date, signed amount in display ccy)]. Positive = money into the
    portfolio. Income (BROKER-party cash) is performance, not a flow."""
    income = _income_parties(conn)
    rows = conn.execute("""
        SELECT t.*, ai.asset_type AS in_type, ai.currency AS in_ccy,
               ao.asset_type AS out_type, ao.currency AS out_ccy
          FROM transactions t
          LEFT JOIN assets ai ON ai.id = t.inflow_asset_id
          LEFT JOIN assets ao ON ao.id = t.outflow_asset_id
         WHERE (t.inflow_account_id IS NULL) != (t.outflow_account_id IS NULL)
         ORDER BY t.txn_date""").fetchall()
    flows: list[tuple[str, float]] = []
    for r in rows:
        date = (r["txn_date"] or "")[:10]
        if r["inflow_account_id"] and r["inflow_asset_id"] and (r["inflow_amount"] or 0) > 0:
            if (r["outflow_party"] or "").strip().upper() in income:
                continue  # dividend/interest: return, not deposit
            amt = _leg_value_display(conn, r["inflow_asset_id"], r["in_type"], r["in_ccy"],
                                     r["inflow_amount"], date, display_cur)
            if amt:
                flows.append((date, amt))
        elif r["outflow_account_id"] and r["outflow_asset_id"] and (r["outflow_amount"] or 0) > 0:
            if (r["inflow_party"] or "").strip().upper() in income:
                continue  # tax/fees paid to broker: cost, not withdrawal
            amt = _leg_value_display(conn, r["outflow_asset_id"], r["out_type"], r["out_ccy"],
                                     r["outflow_amount"], date, display_cur)
            if amt:
                flows.append((date, -amt))
    return flows


def _leg_value_display(conn, asset_id: int, asset_type: str, currency: str,
                       qty: float, on_date: str, display_cur: str) -> float:
    if asset_type in ("cash", "loan"):
        native = qty
    else:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        price, _src = portfolio.price_native(conn, dict(asset), as_of=on_date) \
            if asset else (None, "missing")
        deriv = conn.execute("SELECT multiplier FROM derivative_details WHERE asset_id = ?",
                             (asset_id,)).fetchone()
        mult = float(deriv["multiplier"]) if deriv and deriv["multiplier"] else 1.0
        native = qty * (price or 0.0) * mult
    value, _ok = fx.convert(conn, native, currency or "USD", display_cur, on_date=on_date)
    return value


# ------------------------------------------------------------ monthly -----

def _month_key(date: str) -> tuple[int, int]:
    return int(date[:4]), int(date[5:7])


def monthly_returns(conn, display_cur: str) -> dict:
    """Modified Dietz per calendar month from daily snapshot totals; the
    current month uses the live provisional value and is flagged."""
    series = portfolio.portfolio_series(conn, display_cur, mode="line")
    dates, values = series["dates"], series["values"]
    today_pt = series.get("today")
    if not dates and not today_pt:
        return {"display_currency": display_cur, "years": [], "stats": {}}

    # value known at each date; append provisional today
    provisional_date = None
    if today_pt and (not dates or today_pt["date"] >= dates[-1]):
        if dates and dates[-1] == today_pt["date"]:
            dates, values = dates[:-1], values[:-1]
        dates = dates + [today_pt["date"]]
        values = values + [today_pt["value"]]
        provisional_date = today_pt["date"]

    flows = _external_flows(conn, display_cur)
    flows_by_month: dict[tuple[int, int], list[tuple[str, float]]] = {}
    for d, amt in flows:
        flows_by_month.setdefault(_month_key(d), []).append((d, amt))

    # month end value = last known value in month; month start = previous month end
    months: list[dict] = []
    prev_end = 0.0
    cur_month = None
    for i, d in enumerate(dates):
        mk = _month_key(d)
        if cur_month is None:
            cur_month = {"key": mk, "end": values[i], "end_date": d, "start": prev_end}
        elif mk != cur_month["key"]:
            months.append(cur_month)
            prev_end = cur_month["end"]
            cur_month = {"key": mk, "end": values[i], "end_date": d, "start": prev_end}
        else:
            cur_month["end"] = values[i]
            cur_month["end_date"] = d
    if cur_month is not None:
        months.append(cur_month)

    out_months = []
    first_key = months[0]["key"] if months else None
    for m in months:
        y, mo = m["key"]
        mflows = flows_by_month.get(m["key"], [])
        f_total = sum(a for _d, a in mflows)
        days_in = calendar.monthrange(y, mo)[1]
        # provisional month: period runs only to the last known day
        end_day = min(days_in, int(m["end_date"][8:10]))
        # inception month has no starting value: period starts at the first
        # flow so early deposits aren't underweighted into a huge fake return
        start_day = 1
        if m["key"] == first_key and abs(m["start"]) < 1e-9:
            in_days = [int(d[8:10]) for d, _a in mflows if d <= m["end_date"]]
            if in_days:
                start_day = min(min(in_days), end_day)
        period_days = end_day - start_day + 1
        weighted = 0.0
        for d, a in mflows:
            if d > m["end_date"]:
                continue
            day = int(d[8:10]) - start_day + 1
            weighted += a * max(0.0, (period_days - day) / period_days) if period_days > 0 else 0.0
        denom = m["start"] + weighted
        pnl = m["end"] - m["start"] - f_total
        # a non-positive base has no meaningful rate of return
        ret = (pnl / denom) if denom > 1e-9 else None
        out_months.append({
            "year": y, "month": mo, "ret": ret, "pnl": pnl,
            "end_value": m["end"],
            "provisional": provisional_date is not None and m["end_date"] == provisional_date,
        })

    years: dict[int, dict] = {}
    for m in out_months:
        yr = years.setdefault(m["year"], {"year": m["year"], "months": [None] * 12,
                                          "year_pnl": 0.0, "provisional": False})
        yr["months"][m["month"] - 1] = {k: m[k] for k in ("ret", "pnl", "end_value", "provisional")}
        yr["year_pnl"] += m["pnl"]
        yr["provisional"] = yr["provisional"] or m["provisional"]
    for yr in years.values():
        rets = [c["ret"] for c in yr["months"] if c and c["ret"] is not None]
        growth = 1.0
        for r in rets:
            growth *= (1 + r)
        yr["year_ret"] = growth - 1 if rets else None

    stats = _stats(out_months)
    return {"display_currency": display_cur,
            "years": [years[y] for y in sorted(years, reverse=True)],
            "stats": stats}


def _stats(months: list[dict]) -> dict:
    rets = [(m["year"], m["month"], m["ret"]) for m in months if m["ret"] is not None]
    if not rets:
        return {}

    def compound(sub):
        g = 1.0
        for _y, _m, r in sub:
            g *= (1 + r)
        return g - 1

    cur_year = rets[-1][0]
    ytd = compound([r for r in rets if r[0] == cur_year])
    one_year = compound(rets[-12:])
    n = len(rets)
    total = compound(rets)
    cagr = (1 + total) ** (12.0 / n) - 1 if n >= 1 and total > -1 else None
    best = max(rets, key=lambda r: r[2])
    worst = min(rets, key=lambda r: r[2])
    # max drawdown on the chained month-end index
    idx, peak, mdd = 1.0, 1.0, 0.0
    for _y, _m, r in rets:
        idx *= (1 + r)
        peak = max(peak, idx)
        mdd = min(mdd, idx / peak - 1)
    return {
        "ytd": ytd, "one_year": one_year if n >= 1 else None, "cagr": cagr,
        "months": n, "total": total,
        "best_month": {"year": best[0], "month": best[1], "ret": best[2]},
        "worst_month": {"year": worst[0], "month": worst[1], "ret": worst[2]},
        "max_drawdown": mdd,
    }


@bp.get("/monthly")
def monthly():
    ccy = _display_ccy()
    with db.db_conn() as conn:
        return jsonify(monthly_returns(conn, ccy))


# ------------------------------------------------------------- series -----

def _ensure_index_history(bm: dict) -> int:
    """Create the benchmark asset and backfill history if empty. Uses its own
    short-lived connections so the backfill writer is never lock-blocked."""
    ticker = bm["ticker"]
    with db.db_conn() as conn:
        asset_id = benchmarks.ensure_benchmark_asset(conn, bm)
    with db.db_conn() as conn:
        have = conn.execute("SELECT COUNT(*) FROM prices_daily WHERE asset_id = ?",
                            (asset_id,)).fetchone()[0]
    if not have:
        from backend.scheduler import backfill_daily_asset  # lazy: pulls in apscheduler
        try:
            backfill_daily_asset(asset_id, ticker)
        except Exception as exc:  # noqa: BLE001 - proceed with whatever data exists
            log.warning("index backfill failed for %s: %s", ticker, exc)
    return asset_id


def _index_series(conn, bm: dict, asset_id: int, start: str | None) -> dict | None:
    key, ticker, name = bm["key"], bm["ticker"], bm["name"]
    q = ("SELECT date, COALESCE(adj_close, close) AS c FROM prices_daily "
         "WHERE asset_id = ? AND COALESCE(adj_close, close) IS NOT NULL")
    args: list = [asset_id]
    if start:
        q += " AND date >= ?"
        args.append(start)
    rows = conn.execute(q + " ORDER BY date", args).fetchall()
    if not rows:
        return None
    dates = [r["date"] for r in rows]
    closes = [float(r["c"]) for r in rows]
    # live quote fresher than the last daily close -> provisional point
    today = None
    lq = conn.execute("SELECT price FROM latest_quotes WHERE asset_id = ?",
                      (asset_id,)).fetchone()
    today_s = db.today_str()
    if lq and lq["price"] is not None and dates[-1] < today_s:
        today = {"date": today_s, "value": float(lq["price"])}
    base = closes[0]
    if not base:
        return None
    values = [round(100.0 * c / base, 4) for c in closes]
    if today:
        today = {"date": today["date"], "value": round(100.0 * today["value"] / base, 4)}
    return {"key": key, "ticker": ticker, "name": name,
            "dates": dates, "values": values, "today": today}


def _twr_index(dates: list[str], values: list[float],
               flows: list[tuple[str, float]]) -> list[float]:
    """Growth-of-100 index chained from value changes, deposits/withdrawals
    stripped out (flows treated as start-of-period). Comparable to an index."""
    flows_sorted = sorted(flows)
    out: list[float] = []
    level = 100.0
    prev_val = None
    fi = 0
    for i, (d, v) in enumerate(zip(dates, values)):
        if i == 0:
            while fi < len(flows_sorted) and flows_sorted[fi][0] <= d:
                fi += 1  # flows up to the base date are inside the base value
        else:
            f = 0.0
            while fi < len(flows_sorted) and flows_sorted[fi][0] <= d:
                f += flows_sorted[fi][1]
                fi += 1
            denom = (prev_val or 0.0) + f
            r = ((v - prev_val - f) / denom) if abs(denom) > 1e-9 else 0.0
            level *= (1 + r)
        out.append(round(level, 4))
        prev_val = v
    return out


@bp.get("/series")
def series():
    """Portfolio (flow-adjusted growth of 100) + selected indices rebased to
    100 at the effective start. The start is clamped to the first day the
    portfolio had value: an index never starts before the user held assets.
    mode=candle adds portfolio.ohlc: per-day value OHLC scaled to index level
    (same approximation as the value chart; no live point)."""
    ccy = _display_ccy()
    req_start = (request.args.get("start") or "").strip() or None
    mode = "candle" if (request.args.get("mode") or "").strip().lower() == "candle" else "line"
    keys = [k.strip().upper() for k in (request.args.get("indices") or "").split(",")
            if k.strip()]
    with db.db_conn() as conn:
        configured = {b["key"]: b for b in benchmarks.get_benchmarks(conn)}
    bad = [k for k in keys if k not in configured]
    if bad:
        return jsonify({"error": f"unknown indices: {bad}; choose from {list(configured)}"}), 400
    # index assets/backfill first: the writers need their own transactions
    asset_ids = {k: _ensure_index_history(configured[k]) for k in keys}
    empty = {"display_currency": ccy, "start": None,
             "portfolio": {"dates": [], "values": [], "today": None}, "indices": []}
    with db.db_conn() as conn:
        base_series = portfolio.portfolio_series(conn, ccy, mode=mode)
        dates, values = base_series["dates"], base_series["values"]
        today_pt = base_series.get("today")
        first_nz = next((i for i, v in enumerate(values) if v), None)
        if first_nz is None:
            return jsonify(empty)
        eff_start = max(req_start, dates[first_nz]) if req_start else dates[first_nz]
        pts = [(d, values[i]) for i, d in enumerate(dates) if d >= eff_start]
        if today_pt:
            pts = [p for p in pts if p[0] < today_pt["date"]]
            pts.append((today_pt["date"], today_pt["value"]))
        if not pts or not pts[0][1]:
            return jsonify(empty)
        flows = _external_flows(conn, ccy)
        levels = _twr_index([p[0] for p in pts], [p[1] for p in pts], flows)
        if today_pt:
            pf = {"dates": [p[0] for p in pts[:-1]], "values": levels[:-1],
                  "today": {"date": pts[-1][0], "value": levels[-1]}}
        else:
            pf = {"dates": [p[0] for p in pts], "values": levels, "today": None}
        if mode == "candle":
            # day bar [o,c,l,h] scaled so its close sits on the index level
            bar_at = dict(zip(dates, base_series.get("ohlc") or []))
            pf["ohlc"] = []
            for d, lvl, (_ignored, v) in zip(pf["dates"], pf["values"],
                                             pts[:len(pf["dates"])]):
                bar = bar_at.get(d)
                if bar and v:
                    f = lvl / v
                    pf["ohlc"].append([round(x * f, 4) for x in bar])
                else:
                    pf["ohlc"].append([lvl, lvl, lvl, lvl])
        idx_out = []
        for k in keys:
            s = _index_series(conn, configured[k], asset_ids[k], eff_start)
            if s:
                idx_out.append(s)
    return jsonify({"display_currency": ccy, "start": eff_start,
                    "portfolio": pf, "indices": idx_out})
