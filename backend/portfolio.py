"""Portfolio engine: holdings from transactions, valuations, category totals.

Holdings are DERIVED from the transaction ledger (single source of truth).
Each transaction moves value between an outflow party and an inflow party;
only party sides that are user accounts affect holdings:
    quantity(account, asset) = sum(inflows to it) - sum(outflows from it)
                               - sum(fees charged to it)
"""
import sqlite3

from backend import fx
from backend.db import account_label, daily_close_sql, today_str
from backend.logger import get_logger
from backend.option_archive import parse_occ_ticker

log = get_logger("dashboard.portfolio")

EQUITY_TYPES = ("equity", "etf")
DERIVATIVE_TYPES = ("option", "future")
GROUP_TYPES = ("pair", "option_strategy", "hedge", "normal", "custom")

HOLDINGS_SQL = """
WITH flows AS (
    SELECT inflow_account_id AS account_id, inflow_asset_id AS asset_id,
           inflow_amount AS qty
      FROM transactions
     WHERE inflow_asset_id IS NOT NULL AND inflow_account_id IS NOT NULL
       AND txn_date <= :asof
    UNION ALL
    SELECT outflow_account_id, outflow_asset_id, -outflow_amount
      FROM transactions
     WHERE outflow_asset_id IS NOT NULL AND outflow_account_id IS NOT NULL
       AND txn_date <= :asof
    UNION ALL
    SELECT fee_account_id, fee_asset_id, -fee_amount
      FROM transactions
     WHERE fee_asset_id IS NOT NULL AND fee_account_id IS NOT NULL
       AND txn_date <= :asof
)
SELECT f.account_id, f.asset_id, SUM(f.qty) AS quantity
  FROM flows f
 GROUP BY f.account_id, f.asset_id
HAVING ABS(SUM(f.qty)) > 1e-9
"""


def normalize_asof(as_of: str | None) -> str:
    """txn_date holds datetimes; a date-only cutoff must include the whole day."""
    asof = as_of or "9999-12-31"
    if len(asof) == 10:
        asof += " 23:59:59"
    return asof


# every qty-affecting leg per transaction, in ledger order, with the asset
# types of both trade sides so cash-settled buy/sell legs can be recognized
_TRADE_EVENTS_SQL = """
SELECT t.id AS txn_id, t.txn_date, t.note, t.group_id,
       t.inflow_account_id AS in_acc, t.inflow_asset_id AS in_ast,
       t.inflow_amount AS in_amt,
       t.outflow_account_id AS out_acc, t.outflow_asset_id AS out_ast,
       t.outflow_amount AS out_amt,
       t.fee_account_id AS fee_acc, t.fee_asset_id AS fee_ast, t.fee_amount AS fee_amt,
       ci.asset_type AS in_type, co.asset_type AS out_type
  FROM transactions t
  LEFT JOIN assets ci ON ci.id = t.inflow_asset_id
  LEFT JOIN assets co ON co.id = t.outflow_asset_id
 WHERE t.txn_date <= :asof
 ORDER BY t.txn_date, t.id
"""

_EPS = 1e-9


def _fresh_cycle() -> dict:
    return {"qty": 0.0, "bc": 0.0, "bq": 0.0, "sc": 0.0, "sq": 0.0}


def trade_history(conn: sqlite3.Connection,
                  as_of: str | None = None) -> dict[tuple[int, int], dict]:
    """Chronological per-(account_id, asset_id) trade events with cycle-aware
    running averages. A cycle ends when the running quantity returns to zero;
    crossing zero seeds the new cycle with the crossing leg's pro-rata cost.

    Returns key -> {'events': [event, ...], 'state': accumulators of the open
    cycle, 'cycle': index of the open cycle}. Prices are in the cash leg's
    native units per 1 unit of asset (multipliers NOT removed)."""
    hist: dict[tuple[int, int], dict] = {}

    for r in conn.execute(_TRADE_EVENTS_SQL, {"asof": normalize_asof(as_of)}):
        deltas: dict[tuple[int, int], float] = {}
        legs: dict[tuple[int, int], dict] = {}
        if r["in_acc"] is not None and r["in_ast"] is not None:
            key = (r["in_acc"], r["in_ast"])
            deltas[key] = deltas.get(key, 0.0) + (r["in_amt"] or 0.0)
            if (r["in_amt"] or 0) > 0 and (r["out_amt"] or 0) > 0 \
                    and r["out_type"] == "cash":
                legs.setdefault(key, {})["buy"] = (r["out_amt"], r["in_amt"])
        if r["out_acc"] is not None and r["out_ast"] is not None:
            key = (r["out_acc"], r["out_ast"])
            deltas[key] = deltas.get(key, 0.0) - (r["out_amt"] or 0.0)
            if (r["out_amt"] or 0) > 0 and (r["in_amt"] or 0) > 0 \
                    and r["in_type"] == "cash":
                legs.setdefault(key, {})["sell"] = (r["in_amt"], r["out_amt"])
        if r["fee_acc"] is not None and r["fee_ast"] is not None:
            key = (r["fee_acc"], r["fee_ast"])
            deltas[key] = deltas.get(key, 0.0) - (r["fee_amt"] or 0.0)

        for key, delta in deltas.items():
            h = hist.setdefault(key, {"events": [], "state": _fresh_cycle(), "cycle": 0})
            s = h["state"]
            leg = legs.get(key, {})
            if "buy" in leg:
                s["bc"] += leg["buy"][0]
                s["bq"] += leg["buy"][1]
            if "sell" in leg:
                s["sc"] += leg["sell"][0]
                s["sq"] += leg["sell"][1]
            old, new = s["qty"], s["qty"] + delta
            closed = abs(new) <= _EPS
            if closed:                            # position closed: cycle over
                s["qty"] = 0.0  # keep accumulators so the closing event shows cycle avgs
            elif old and (old > 0) != (new > 0):  # crossed zero: seed new cycle
                s2 = _fresh_cycle()
                s2["qty"] = new
                side = "sell" if new < 0 else "buy"
                if side in leg and leg[side][1]:
                    cash, qty_leg = leg[side]
                    frac = min(abs(new) / qty_leg, 1.0)
                    s2["sc" if side == "sell" else "bc"] = cash * frac
                    s2["sq" if side == "sell" else "bq"] = abs(new)
                h["cycle"] += 1
                h["state"] = s2
            else:
                s["qty"] = new
            snap = h["state"]
            side = "buy" if "buy" in leg else ("sell" if "sell" in leg else None)
            cash, leg_qty = leg[side] if side else (None, None)
            h["events"].append({
                "txn_id": r["txn_id"], "txn_date": r["txn_date"],
                "delta": delta, "qty_after": 0.0 if closed else new,
                "cycle": h["cycle"], "side": side,
                "cash": cash, "leg_qty": leg_qty,
                "px": (cash / leg_qty) if side and leg_qty else None,
                "avg_buy": (snap["bc"] / snap["bq"]) if snap["bq"] > _EPS else None,
                "avg_sell": (snap["sc"] / snap["sq"]) if snap["sq"] > _EPS else None,
                "fee_amount": r["fee_amt"] or 0.0, "fee_asset_id": r["fee_ast"],
                "group_id": r["group_id"], "note": r["note"] or "",
            })
            if closed:
                h["state"] = _fresh_cycle()
                h["cycle"] += 1
    return hist


def avg_trade_prices(conn: sqlite3.Connection,
                     as_of: str | None = None) -> dict[tuple[int, int], dict]:
    """(account_id, asset_id) -> {'buy': avg buy px, 'sell': avg sell px} in the
    cash leg's native units per 1 unit of asset (contract multipliers not yet
    removed; the caller divides by the multiplier for derivatives).

    Averages cover only the CURRENT holding cycle: whenever the running
    position returns to zero, the accumulators reset, so earlier round trips
    stop influencing the buy/sell price shown today."""
    out: dict[tuple[int, int], dict] = {}
    for key, h in trade_history(conn, as_of).items():
        s = h["state"]
        d = {}
        if s["bq"] > _EPS:
            d["buy"] = s["bc"] / s["bq"]
        if s["sq"] > _EPS:
            d["sell"] = s["sc"] / s["sq"]
        if d:
            out[key] = d
    return out


def holdings(conn: sqlite3.Connection, as_of: str | None = None) -> list[dict]:
    """Raw quantities per (account, asset) as of a date/datetime (inclusive)."""
    rows = conn.execute(HOLDINGS_SQL, {"asof": normalize_asof(as_of)}).fetchall()
    out = []
    for r in rows:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (r["asset_id"],)).fetchone()
        account = conn.execute("SELECT * FROM accounts WHERE id = ?",
                               (r["account_id"],)).fetchone()
        if asset is None or account is None:
            continue
        deriv = conn.execute("SELECT * FROM derivative_details WHERE asset_id = ?",
                             (r["asset_id"],)).fetchone()
        out.append({
            "account_id": r["account_id"], "asset_id": r["asset_id"],
            "quantity": r["quantity"],
            "asset": dict(asset), "account": dict(account),
            "account_label": account_label(account),
            "derivative": dict(deriv) if deriv else None,
        })
    return out


def _derivative_expiry(conn: sqlite3.Connection, asset: dict) -> str | None:
    """'YYYY-MM-DD' expiry from derivative_details, else the OCC ticker."""
    row = conn.execute("SELECT expiry FROM derivative_details WHERE asset_id = ?",
                       (asset["id"],)).fetchone()
    exp = str(row["expiry"])[:10] if row and row["expiry"] else ""
    if not exp:
        parsed = parse_occ_ticker(asset.get("ticker") or "")
        exp = parsed["expiry"] if parsed else ""
    return exp or None


def _quote_stale(conn: sqlite3.Connection, asset_id: int, session_date: str | None) -> bool:
    """Quote belongs to an earlier, already-closed session (weekend/holiday/overnight)
    whose daily bar is stored -> the configured day close supersedes it."""
    if not session_date or session_date[:10] >= today_str():
        return False
    return conn.execute("SELECT 1 FROM prices_daily WHERE asset_id = ? AND date >= ? LIMIT 1",
                        (asset_id, session_date[:10])).fetchone() is not None


def prev_day_close(conn: sqlite3.Connection, asset_id: int) -> float | None:
    """Configured day close of the latest stored bar before today."""
    row = conn.execute(
        f"SELECT {daily_close_sql(conn)} FROM prices_daily WHERE asset_id = ? AND date < ? "
        "ORDER BY date DESC LIMIT 1", (asset_id, today_str())).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def price_native(conn: sqlite3.Connection, asset: dict,
                 as_of: str | None = None) -> tuple[float | None, str]:
    """(price in asset's own currency, source). Cash/loan pegs to 1.0."""
    atype = asset["asset_type"]
    if atype in ("cash", "loan"):
        return 1.0, "peg"
    aid = asset["id"]
    if atype in DERIVATIVE_TYPES:
        # past expiry the contract is gone: never forward-fill a stale close
        exp = _derivative_expiry(conn, asset)
        if exp and exp < (as_of or today_str())[:10]:
            return 0.0, "expired"
    if not asset["is_yahoo_tracked"]:
        q = "SELECT price FROM manual_prices WHERE asset_id = ?"
        args: list = [aid]
        if as_of:
            q += " AND price_date <= ?"
            args.append(as_of)
        q += " ORDER BY price_date DESC LIMIT 1"
        row = conn.execute(q, args).fetchone()
        return (float(row[0]), "manual") if row else (None, "missing")
    if as_of is None:
        row = conn.execute("SELECT price, session_date FROM latest_quotes WHERE asset_id = ?",
                           (aid,)).fetchone()
        if row and row[0] is not None and not _quote_stale(conn, aid, row[1]):
            return float(row[0]), "live"
    q = f"SELECT {daily_close_sql(conn)} FROM prices_daily WHERE asset_id = ?"
    args = [aid]
    if as_of:
        q += " AND date <= ?"
        args.append(as_of)
    q += " ORDER BY date DESC LIMIT 1"
    row = conn.execute(q, args).fetchone()
    return (float(row[0]), "daily") if row else (None, "missing")


def enrich_holdings(conn: sqlite3.Connection, holds: list[dict],
                    display_cur: str, as_of: str | None = None) -> list[dict]:
    """Attach price, multiplier, fx and values (native/USD/display) to raw holdings.
    Assets denominated in the display currency convert identically: the native
    value passes through untouched (no USD round-trip drift)."""
    usd2disp, disp_ok = fx.usd_to_display(conn, display_cur, as_of)
    disp_ccy = (display_cur or "USD").upper()
    costs = avg_trade_prices(conn, as_of)
    # today's non-cash asset flows per (account, asset): traded qty + net cash
    # spent, so day earn on positions traded today measures from the trade price
    day_flows: dict[tuple[int, int], dict] = {}
    if as_of is None:
        cash_ids = {r[0] for r in conn.execute(
            "SELECT id FROM assets WHERE asset_type IN ('cash', 'loan')")}
        for t in conn.execute("SELECT * FROM transactions WHERE txn_date >= ?",
                              (today_str(),)):
            if t["inflow_account_id"] and t["inflow_asset_id"] \
                    and t["inflow_asset_id"] not in cash_ids:
                f = day_flows.setdefault((t["inflow_account_id"], t["inflow_asset_id"]),
                                         {"qty": 0.0, "cash": 0.0})
                f["qty"] += t["inflow_amount"] or 0.0
                if t["outflow_account_id"] and t["outflow_asset_id"] in cash_ids:
                    f["cash"] += t["outflow_amount"] or 0.0
            if t["outflow_account_id"] and t["outflow_asset_id"] \
                    and t["outflow_asset_id"] not in cash_ids:
                f = day_flows.setdefault((t["outflow_account_id"], t["outflow_asset_id"]),
                                         {"qty": 0.0, "cash": 0.0})
                f["qty"] -= t["outflow_amount"] or 0.0
                if t["inflow_account_id"] and t["inflow_asset_id"] in cash_ids:
                    f["cash"] -= t["inflow_amount"] or 0.0
    for h in holds:
        asset = h["asset"]
        price, source = price_native(conn, asset, as_of)
        mult = (h["derivative"] or {}).get("multiplier") or 1.0 \
            if asset["asset_type"] in DERIVATIVE_TYPES else 1.0
        rate, fx_ok = fx.rate_to_usd(conn, asset["currency"], as_of)
        value_native = (h["quantity"] * price * mult) if price is not None else None
        value_usd = value_native * rate if value_native is not None else None
        if (asset["currency"] or "").upper() == disp_ccy:
            value_display = value_native
        else:
            value_display = value_usd * usd2disp if value_usd is not None else None
        trade = costs.get((h["account_id"], h["asset_id"]), {})
        avg_px = trade.get("buy") if h["quantity"] >= 0 else trade.get("sell")
        if asset["asset_type"] in ("cash", "loan") or avg_px is None:
            avg_cost = None
        else:
            avg_cost = avg_px / mult if mult else avg_px
        # unrealized: current native value minus what the position cost
        if avg_cost is None or value_native is None:
            earning = None
            earning_display = None
        else:
            earning = value_native - avg_cost * h["quantity"] * mult
            if (asset["currency"] or "").upper() == disp_ccy:
                earning_display = earning
            else:
                earning_display = earning * rate * usd2disp
        h.update({
            "price_native": price, "price_source": source, "multiplier": mult,
            "fx_to_usd": rate, "fx_ok": fx_ok and disp_ok,
            "value_native": value_native, "value_usd": value_usd,
            "value_display": value_display, "avg_cost": avg_cost, "earning": earning,
            "earning_display": earning_display,
        })
        if as_of is None and asset["is_yahoo_tracked"]:
            lq = conn.execute("SELECT prev_close FROM latest_quotes WHERE asset_id = ?",
                              (asset["id"],)).fetchone()
            base = prev_day_close(conn, asset["id"])
            if base is None and lq:
                base = lq["prev_close"]
            if price and base:
                # price move of the asset itself, position-agnostic
                h["day_change_pct"] = (price - base) / base * 100
                # position P&L today: end value - overnight value - net cash in.
                # Covers longs, shorts and positions opened/changed today (which
                # measure from their actual trade price, not prev_close).
                flow = day_flows.get((h["account_id"], h["asset_id"]))
                qty_prev = h["quantity"] - (flow["qty"] if flow else 0.0)
                net_cash = flow["cash"] if flow else 0.0
                prev_val = base * qty_prev * mult
                day_chg = price * h["quantity"] * mult - prev_val - net_cash
                denom = abs(prev_val) if abs(qty_prev) > 1e-12 else abs(net_cash)
                h["day_earn_pct"] = (day_chg / denom * 100) if denom > 1e-9 else None
                h["day_change_native"] = day_chg
                if (asset["currency"] or "").upper() == disp_ccy:
                    h["day_change_display"] = day_chg
                else:
                    h["day_change_display"] = day_chg * rate * usd2disp
            else:
                h["day_change_pct"] = None
                h["day_earn_pct"] = None
                h["day_change_native"] = None
                h["day_change_display"] = None
        else:
            h["day_change_pct"] = None
            h["day_earn_pct"] = None
            h["day_change_native"] = None
            h["day_change_display"] = None
    return holds


def group_positions(conn: sqlite3.Connection,
                    display_cur: str) -> tuple[list[dict], dict[tuple[int, int], list[dict]]]:
    """Strategy-group analytics for the holdings table.

    Returns (groups, memberships):
      groups: per txn_group with per-(account, asset) legs, entry px derived
              from the group's own member transactions, current value and P&L,
              plus entry/current price ratio when exactly two open non-cash legs;
      memberships: (account_id, asset_id) -> [{'id','name','group_type','qty'}]
                   for tagging holdings rows (cash legs excluded).
    Groups without member transactions are omitted."""
    grows = conn.execute("SELECT * FROM txn_groups ORDER BY name COLLATE NOCASE").fetchall()
    if not grows:
        return [], {}
    txns = conn.execute("SELECT * FROM transactions WHERE group_id IS NOT NULL "
                        "ORDER BY txn_date, id").fetchall()
    by_gid: dict[int, list] = {}
    for t in txns:
        by_gid.setdefault(t["group_id"], []).append(t)
    usd2disp, disp_ok = fx.usd_to_display(conn, display_cur)
    disp_ccy = (display_cur or "USD").upper()

    asset_cache: dict[int, dict | None] = {}

    def asset_of(aid):
        if aid not in asset_cache:
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (aid,)).fetchone()
            asset_cache[aid] = dict(row) if row else None
        return asset_cache[aid]

    mult_cache: dict[int, float] = {}

    def mult_of(aid, atype):
        if atype not in DERIVATIVE_TYPES:
            return 1.0
        if aid not in mult_cache:
            row = conn.execute("SELECT multiplier FROM derivative_details "
                               "WHERE asset_id = ?", (aid,)).fetchone()
            mult_cache[aid] = float(row["multiplier"]) if row and row["multiplier"] else 1.0
        return mult_cache[aid]

    groups_out: list[dict] = []
    memberships: dict[tuple[int, int], list[dict]] = {}
    for g in grows:
        members = by_gid.get(g["id"], [])
        if not members:
            continue
        legs: dict[tuple[int, int], dict] = {}

        def leg_acc(acc, aid):
            return legs.setdefault((acc, aid),
                                   {"qty": 0.0, "bc": 0.0, "bq": 0.0, "sc": 0.0, "sq": 0.0})

        for t in members:
            in_a = asset_of(t["inflow_asset_id"]) if t["inflow_asset_id"] else None
            out_a = asset_of(t["outflow_asset_id"]) if t["outflow_asset_id"] else None
            if t["inflow_account_id"] and t["inflow_asset_id"]:
                s = leg_acc(t["inflow_account_id"], t["inflow_asset_id"])
                s["qty"] += t["inflow_amount"] or 0.0
                if (t["outflow_amount"] or 0) > 0 and out_a and out_a["asset_type"] == "cash":
                    s["bc"] += t["outflow_amount"]
                    s["bq"] += t["inflow_amount"] or 0.0
            if t["outflow_account_id"] and t["outflow_asset_id"]:
                s = leg_acc(t["outflow_account_id"], t["outflow_asset_id"])
                s["qty"] -= t["outflow_amount"] or 0.0
                if (t["inflow_amount"] or 0) > 0 and in_a and in_a["asset_type"] == "cash":
                    s["sc"] += t["inflow_amount"]
                    s["sq"] += t["outflow_amount"] or 0.0
            if t["fee_account_id"] and t["fee_asset_id"] and (t["fee_amount"] or 0) > 0:
                leg_acc(t["fee_account_id"], t["fee_asset_id"])["qty"] -= t["fee_amount"]

        leg_rows: list[dict] = []
        cash_disp = 0.0
        entry_cost_disp = 0.0
        open_disp = 0.0
        unreal_disp = 0.0
        fx_all_ok = disp_ok
        for (acc, aid), s in sorted(legs.items()):
            asset = asset_of(aid)
            if asset is None:
                continue
            atype = asset["asset_type"]
            qty = s["qty"]
            mult = mult_of(aid, atype)
            price, src = price_native(conn, asset)
            rate, ok = fx.rate_to_usd(conn, asset["currency"])
            fx_all_ok = fx_all_ok and ok
            same_ccy = (asset["currency"] or "").upper() == disp_ccy

            def to_disp(native):
                if native is None:
                    return None
                return native if same_ccy else native * rate * usd2disp

            value_native = qty * price * mult if price is not None else None
            value_disp = to_disp(value_native)
            if atype == "cash":
                cash_disp += value_disp or 0.0
                continue  # cash flows roll into the group aggregates, not legs
            if abs(qty) <= _EPS:
                continue  # fully closed leg: realized result lives in cash
            # entry px per unit, multiplier removed (same convention as avg_cost)
            raw = (s["bc"] / s["bq"]) if qty >= 0 and s["bq"] > _EPS else \
                  ((s["sc"] / s["sq"]) if qty < 0 and s["sq"] > _EPS else None)
            entry_px = (raw / mult if mult else raw) if raw is not None else None
            cost_native = entry_px * qty * mult if entry_px is not None else None
            earn_native = value_native - cost_native \
                if value_native is not None and cost_native is not None else None
            cost_disp = to_disp(cost_native)
            earn_disp = to_disp(earn_native)
            entry_cost_disp += cost_disp or 0.0
            open_disp += value_disp or 0.0
            unreal_disp += earn_disp or 0.0
            leg_rows.append({
                "asset_id": aid, "account_id": acc, "ticker": asset["ticker"],
                "asset_type": atype, "currency": asset["currency"],
                "qty": qty, "multiplier": mult,
                "entry_px": entry_px, "price_native": price, "price_source": src,
                "value_native": value_native, "value_display": value_disp,
                "entry_cost_display": cost_disp,
                "earning": earn_native, "earning_display": earn_disp,
            })
            memberships.setdefault((acc, aid), []).append({
                "id": g["id"], "name": g["name"],
                "group_type": g["group_type"], "qty": qty})

        pair = None
        if g["group_type"] == "pair" and len(leg_rows) == 2:
            a, b = sorted(leg_rows, key=lambda leg: (leg["qty"] < 0,
                                                     -abs(leg["value_display"] or 0.0)))
            pair = {
                "a_ticker": a["ticker"], "b_ticker": b["ticker"],
                "entry_ratio": (a["entry_px"] / b["entry_px"])
                    if a["entry_px"] and b["entry_px"] else None,
                "current_ratio": (a["price_native"] / b["price_native"])
                    if a["price_native"] and b["price_native"] else None,
                "weight_a": (abs(a["value_display"]) /
                             (abs(a["value_display"]) + abs(b["value_display"])))
                    if a["value_display"] is not None and b["value_display"] is not None
                    and (abs(a["value_display"]) + abs(b["value_display"])) > 0 else None,
            }
        groups_out.append({
            "id": g["id"], "name": g["name"], "group_type": g["group_type"],
            "note": g["note"], "txn_count": len(members),
            "txn_ids": [t["id"] for t in members],
            "legs": leg_rows,
            "cash_display": cash_disp,
            "entry_cost_display": entry_cost_disp,
            "open_value_display": open_disp,
            "unrealized_display": unreal_disp,
            "total_display": cash_disp + open_disp,
            "pair": pair,
            "fx_ok": fx_all_ok,
        })
    return groups_out, memberships


def summary(conn: sqlite3.Connection, display_cur: str) -> dict:
    """Everything the home dashboard needs, in the display currency."""
    holds = enrich_holdings(conn, holdings(conn), display_cur)
    usd2disp, _ = fx.usd_to_display(conn, display_cur)

    categories = {t: 0.0 for t in ("cash", "equity", "etf", "bond", "option", "future",
                                   "crypto", "real_estate", "loan", "other")}
    nav = 0.0
    fx_warnings: set[str] = set()
    industry_long: dict[str, float] = {}
    industry_net: dict[str, float] = {}
    country_long: dict[str, float] = {}
    country_net: dict[str, float] = {}
    exposure = 0.0

    for h in holds:
        v = h["value_display"]
        if v is None:
            fx_warnings.add(h["asset"]["ticker"] + ":no-price")
            continue
        if not h["fx_ok"]:
            fx_warnings.add(h["asset"]["currency"])
        atype = h["asset"]["asset_type"]
        categories[atype if atype in categories else "other"] += v
        nav += v
        if atype in EQUITY_TYPES:
            ind = h["asset"]["industry"] or "Unknown"
            cty = h["asset"]["country"] or "Unknown"
            industry_net[ind] = industry_net.get(ind, 0.0) + v
            country_net[cty] = country_net.get(cty, 0.0) + v
            if v > 0:  # short positions stay out of the long-exposure pies
                industry_long[ind] = industry_long.get(ind, 0.0) + v
                country_long[cty] = country_long.get(cty, 0.0) + v
        if atype in DERIVATIVE_TYPES:
            exposure += _derivative_exposure(conn, h) * usd2disp
        elif atype not in ("cash", "loan"):
            exposure += abs(v)

    cash_total = categories["cash"]
    groups, group_map = group_positions(conn, display_cur)
    flat_holdings = []
    for h in holds:
        row = _flatten_holding(h)
        row["groups"] = group_map.get((h["account_id"], h["asset_id"]), [])
        flat_holdings.append(row)
    kpis = {
        "nav": nav,
        "cash_total": cash_total,
        "equities": categories["equity"] + categories["etf"],
        "bonds": categories["bond"],
        "derivatives": categories["option"] + categories["future"],
        "crypto": categories["crypto"],
        "real_estate": categories["real_estate"],
        "loans": categories["loan"],
        "other": categories["other"],
        "exposure": exposure,
    }
    return {
        "display_currency": display_cur,
        "kpis": kpis,
        "categories": categories,
        "industry_pie": _pie(industry_long),
        "country_pie": _pie(country_long),
        "industry_net": _net_items(industry_net),
        "country_net": _net_items(country_net),
        "holdings": flat_holdings,
        "txn_groups": groups,
        "fx_warnings": sorted(fx_warnings),
    }


def _derivative_exposure(conn: sqlite3.Connection, h: dict) -> float:
    """Notional in USD: |qty| * multiplier * underlying spot when available, else |value|."""
    d = h["derivative"] or {}
    und = (d.get("underlying_ticker") or "").upper()
    if und:
        row = conn.execute(
            "SELECT lq.price, a.currency FROM latest_quotes lq "
            "JOIN assets a ON a.id = lq.asset_id WHERE a.ticker = ?", (und,)).fetchone()
        if row and row["price"]:
            rate, _ = fx.rate_to_usd(conn, row["currency"])
            return abs(h["quantity"]) * (d.get("multiplier") or 1) * row["price"] * rate
    return abs(h["value_usd"] or 0.0)


def _pie(bucket: dict[str, float]) -> list[dict]:
    total = sum(v for v in bucket.values() if v > 0)
    items = [{"name": k, "value": round(v, 2),
              "pct": round(v / total * 100, 2) if total else 0}
             for k, v in sorted(bucket.items(), key=lambda kv: -kv[1]) if v > 0]
    return items


def _net_items(bucket: dict[str, float]) -> list[dict]:
    """Net values sorted desc, negatives kept (rendered by the NET BAR mode)."""
    return [{"name": k, "value": round(v, 2)}
            for k, v in sorted(bucket.items(), key=lambda kv: -kv[1])
            if abs(v) > 1e-9]


def _flatten_holding(h: dict) -> dict:
    a, acc = h["asset"], h["account"]
    return {
        "asset_id": h["asset_id"], "account_id": h["account_id"],
        "ticker": a["ticker"], "name": a["name"], "asset_type": a["asset_type"],
        "currency": a["currency"], "industry": a["industry"], "sector": a["sector"],
        "country": a["country"], "custodian": acc["custodian"],
        "custodian_type": acc["custodian_type"], "account": acc["name"],
        "account_label": h["account_label"],
        "quantity": h["quantity"], "price_native": h["price_native"],
        "price_source": h["price_source"], "multiplier": h["multiplier"],
        "avg_cost": h["avg_cost"], "earning": h["earning"],
        "earning_display": h["earning_display"],
        "fx_to_usd": h["fx_to_usd"], "fx_ok": h["fx_ok"],
        "value_native": h["value_native"], "value_usd": h["value_usd"],
        "value_display": h["value_display"], "day_change_pct": h["day_change_pct"],
        "day_earn_pct": h["day_earn_pct"],
        "day_change_native": h["day_change_native"],
        "day_change_display": h["day_change_display"],
        "derivative": h["derivative"],
    }


# ------------------------------------------------------------- series -----

def portfolio_series(conn: sqlite3.Connection, display_cur: str,
                     mode: str = "line", rebase_date: str | None = None) -> dict:
    """Historical portfolio value from daily_snapshots. line -> totals;
    candle -> per-day OHLC approximated as sum(qty * asset OHLC) (documented approx).
    line mode also attaches `today`: a provisional point from live holdings x
    latest prices/FX (not persisted; supersedes any frozen snapshot for today).
    Snapshot rows denominated in the display currency convert identically
    (native value, no USD round-trip drift)."""
    today_point = None
    if mode != "candle":
        holds = enrich_holdings(conn, holdings(conn), display_cur)
        if holds:
            live = sum(h["value_display"] or 0.0 for h in holds)
            today_point = {"date": today_str(), "value": round(live, 2)}

    rows = conn.execute(f"""
        SELECT s.date, s.asset_id, s.quantity, s.price_native, s.currency, s.fx_to_usd,
               s.value_usd, p.open, p.high, p.low, {daily_close_sql(conn, 'p')} AS close,
               COALESCE(dd.multiplier, 1) AS mult, a.asset_type
          FROM daily_snapshots s
          JOIN assets a ON a.id = s.asset_id
          LEFT JOIN prices_daily p ON p.asset_id = s.asset_id AND p.date = s.date
          LEFT JOIN derivative_details dd ON dd.asset_id = s.asset_id
         ORDER BY s.date
    """).fetchall()
    if not rows:
        return {"mode": mode, "dates": [], "values": [], "ohlc": [], "today": today_point}

    disp_ccy = (display_cur or "USD").upper()
    by_date: dict[str, dict] = {}
    for r in rows:
        d = by_date.setdefault(r["date"], {
            "o": 0.0, "h": 0.0, "l": 0.0, "c": 0.0, "v": 0.0,       # USD parts
            "on": 0.0, "hn": 0.0, "ln": 0.0, "cn": 0.0, "vn": 0.0})  # display-ccy parts
        fx_rate = r["fx_to_usd"] or 1.0
        mult = r["mult"] if r["asset_type"] in DERIVATIVE_TYPES else 1.0
        if r["close"] is not None and r["asset_type"] not in ("cash", "loan"):
            c = r["close"]
            o, h_, l_ = (r["open"] or c, max(r["high"] or c, c), min(r["low"] or c, c))
        else:
            o = h_ = l_ = c = (r["price_native"] or 0.0)
        if (r["currency"] or "").upper() == disp_ccy:
            base = r["quantity"] * mult  # identity: stays in the display currency
            d["on"] += base * o
            d["hn"] += base * h_
            d["ln"] += base * l_
            d["cn"] += base * c
            if r["value_usd"] is not None:
                d["vn"] += r["value_usd"] / fx_rate if fx_rate else r["value_usd"]
        else:
            base = r["quantity"] * mult * fx_rate
            d["o"] += base * o
            d["h"] += base * h_
            d["l"] += base * l_
            d["c"] += base * c
            d["v"] += r["value_usd"] or 0.0

    dates = sorted(by_date)
    if today_point is not None and dates and dates[-1] == today_point["date"]:
        dates = dates[:-1]  # frozen today row exists; the live point supersedes it
    values, ohlc = [], []
    for dt in dates:
        m, _ = fx.usd_to_display(conn, display_cur, dt)
        d = by_date[dt]
        values.append(round(d["vn"] + d["v"] * m, 2))
        ohlc.append([round(d["on"] + d["o"] * m, 2), round(d["cn"] + d["c"] * m, 2),
                     round(d["ln"] + d["l"] * m, 2), round(d["hn"] + d["h"] * m, 2)])
        # echarts order o,c,l,h

    if rebase_date:
        base_idx = next((i for i, dt in enumerate(dates) if dt >= rebase_date), None)
        if base_idx is not None and values[base_idx]:
            factor = 100.0 / values[base_idx]
            values = [round(v * factor, 4) for v in values]
            ohlc = [[round(x * factor, 4) for x in bar] for bar in ohlc]
            if today_point is not None:
                today_point = {"date": today_point["date"],
                               "value": round(today_point["value"] * factor, 4)}

    return {"mode": mode, "dates": dates, "values": values, "ohlc": ohlc,
            "today": today_point}
