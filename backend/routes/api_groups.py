"""Transaction groups: manually tag transactions into named groups (e.g. a pair
trade) and see the combined result per group.

Group P&L = net cash flow of member legs + market value of any open quantity.
Only legs touching the user's own accounts count (external parties don't
change holdings).
"""
import datetime

from flask import Blueprint, jsonify, request

from backend import db, fx, portfolio
from backend.logger import get_logger

bp = Blueprint("api_groups", __name__, url_prefix="/api/txn-groups")
log = get_logger("dashboard.api")


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _display_ccy() -> str:
    ccy = request.args.get("ccy")
    if ccy:
        return ccy.upper()
    with db.db_conn() as conn:
        return db.get_setting(conn, "display_currency", "USD")


def _clean_group_type(value, default: str = "custom") -> str | None:
    """None when invalid so callers can 400."""
    gtype = (value or default).strip().lower()
    return gtype if gtype in portfolio.GROUP_TYPES else None


@bp.get("")
def list_groups():
    with db.db_conn() as conn:
        rows = db.rows_to_dicts(conn.execute(
            """SELECT g.*, (SELECT COUNT(*) FROM transactions t WHERE t.group_id = g.id)
                      AS txn_count
                 FROM txn_groups g ORDER BY g.name COLLATE NOCASE"""))
    return jsonify(rows)


@bp.post("")
def create_group():
    name = (_body().get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    note = (_body().get("note") or "").strip()
    gtype = _clean_group_type(_body().get("group_type"))
    if gtype is None:
        return jsonify({"error": f"group_type must be one of {list(portfolio.GROUP_TYPES)}"}), 400
    with db.db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO txn_groups(name, note, group_type, created_at) VALUES (?, ?, ?, ?)",
            (name, note, gtype, db.now_str()))
        gid = cur.lastrowid
    log.info("txn group %d created (%s, %s)", gid, name, gtype)
    return jsonify({"id": gid, "name": name, "group_type": gtype}), 201


@bp.put("/<int:group_id>")
def update_group(group_id):
    data = _body()
    with db.db_conn() as conn:
        row = conn.execute("SELECT * FROM txn_groups WHERE id = ?", (group_id,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        name = (data.get("name") or row["name"]).strip() or row["name"]
        note = data.get("note") if data.get("note") is not None else row["note"]
        gtype = _clean_group_type(data.get("group_type"), default=row["group_type"])
        if gtype is None:
            return jsonify({"error": f"group_type must be one of {list(portfolio.GROUP_TYPES)}"}), 400
        conn.execute("UPDATE txn_groups SET name = ?, note = ?, group_type = ? WHERE id = ?",
                     (name, note, gtype, group_id))
    return jsonify({"ok": True})


@bp.delete("/<int:group_id>")
def delete_group(group_id):
    with db.db_conn() as conn:
        conn.execute("UPDATE transactions SET group_id = NULL WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM txn_groups WHERE id = ?", (group_id,))
    log.info("txn group %d deleted (members kept, ungrouped)", group_id)
    return jsonify({"ok": True})


def _txn_ids(data: dict) -> list[int]:
    ids = data.get("txn_ids") or []
    if not isinstance(ids, list) or not ids:
        raise ValueError("txn_ids must be a non-empty list")
    return [int(i) for i in ids]


@bp.post("/<int:group_id>/assign")
def assign_to_group(group_id):
    try:
        ids = _txn_ids(_body())
    except (ValueError, TypeError):
        return jsonify({"error": "txn_ids must be a non-empty list of ids"}), 400
    with db.db_conn() as conn:
        if conn.execute("SELECT 1 FROM txn_groups WHERE id = ?", (group_id,)).fetchone() is None:
            return jsonify({"error": "group not found"}), 404
        marks = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE transactions SET group_id = ?, updated_at = ? WHERE id IN ({marks})",
            [group_id, db.now_str(), *ids])
        updated = cur.rowcount
    return jsonify({"ok": True, "updated": updated})


@bp.post("/unassign")
def unassign_from_group():
    try:
        ids = _txn_ids(_body())
    except (ValueError, TypeError):
        return jsonify({"error": "txn_ids must be a non-empty list of ids"}), 400
    with db.db_conn() as conn:
        marks = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE transactions SET group_id = NULL, updated_at = ? WHERE id IN ({marks})",
            [db.now_str(), *ids])
        updated = cur.rowcount
    return jsonify({"ok": True, "updated": updated})


@bp.post("/from-positions")
def group_from_positions():
    """Create (or extend) a group from holdings-table positions: assigns every
    transaction of each position's CURRENT open cycle. Txns already in another
    group are left untouched and reported back."""
    data = _body()
    positions = data.get("positions") or []
    try:
        keys = [(int(p["account_id"]), int(p["asset_id"])) for p in positions]
    except (KeyError, TypeError, ValueError):
        keys = []
    if not keys:
        return jsonify({"error": "positions must be a non-empty list of "
                                 "{asset_id, account_id}"}), 400
    with db.db_conn() as conn:
        gid = data.get("group_id")
        if gid:
            row = conn.execute("SELECT * FROM txn_groups WHERE id = ?", (int(gid),)).fetchone()
            if row is None:
                return jsonify({"error": "group not found"}), 404
            gid, gname = row["id"], row["name"]
            created = False
        else:
            gname = (data.get("name") or "").strip()
            if not gname:
                return jsonify({"error": "name is required for a new group"}), 400
            gtype = _clean_group_type(data.get("group_type"))
            if gtype is None:
                return jsonify({"error": f"group_type must be one of "
                                         f"{list(portfolio.GROUP_TYPES)}"}), 400
            note = (data.get("note") or "").strip()
            gid = conn.execute(
                "INSERT INTO txn_groups(name, note, group_type, created_at) "
                "VALUES (?, ?, ?, ?)", (gname, note, gtype, db.now_str())).lastrowid
            created = True
        hist = portfolio.trade_history(conn)
        assign: set[int] = set()
        skipped: dict[int, int] = {}
        no_position: list[dict] = []
        for acc, aid in keys:
            h = hist.get((acc, aid))
            if not h or abs(h["state"]["qty"]) <= 1e-9:
                no_position.append({"account_id": acc, "asset_id": aid})
                continue
            for e in h["events"]:
                if e["cycle"] != h["cycle"]:
                    continue
                if e["group_id"] and e["group_id"] != gid:
                    skipped[e["txn_id"]] = e["group_id"]
                else:
                    assign.add(e["txn_id"])
        if assign:
            marks = ",".join("?" * len(assign))
            conn.execute(
                f"UPDATE transactions SET group_id = ?, updated_at = ? WHERE id IN ({marks})",
                [gid, db.now_str(), *assign])
        names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM txn_groups")}
    log.info("txn group %d: %d txns assigned from %d positions (%d skipped)",
             gid, len(assign), len(keys), len(skipped))
    return jsonify({
        "id": gid, "name": gname, "assigned": len(assign),
        "skipped": [{"txn_id": tid, "group_id": g, "group_name": names.get(g)}
                    for tid, g in sorted(skipped.items())],
        "no_open_position": no_position,
    }), 201 if created else 200


@bp.get("/summary")
def groups_summary():
    """Per group: member txns, per-asset net position, cash net, open value and
    total P&L in the display currency (open quantities valued at latest price)."""
    from backend.routes.api_transactions import LIST_SQL  # joined labels/tickers
    display = _display_ccy()
    with db.db_conn() as conn:
        groups = db.rows_to_dicts(conn.execute(
            "SELECT * FROM txn_groups ORDER BY name COLLATE NOCASE"))
        txns = db.rows_to_dicts(conn.execute(
            LIST_SQL + " WHERE t.group_id IS NOT NULL ORDER BY t.txn_date, t.id"))
        atype = {r["id"]: r["asset_type"]
                 for r in conn.execute("SELECT id, asset_type FROM assets")}
        out = []
        for g in groups:
            members = [t for t in txns if t["group_id"] == g["id"]]
            net: dict[int, float] = {}
            # per-asset avg entry cost (cash paid/received vs a cash counterleg)
            costs: dict[int, dict] = {}

            def cost_acc(aid):
                return costs.setdefault(aid, {"bc": 0.0, "bq": 0.0, "sc": 0.0, "sq": 0.0})

            for t in members:
                # only legs on own accounts change holdings
                if t["outflow_account_id"] and t["outflow_asset_id"]:
                    net[t["outflow_asset_id"]] = net.get(t["outflow_asset_id"], 0.0) \
                        - (t["outflow_amount"] or 0.0)
                    if (t["inflow_amount"] or 0) > 0 and atype.get(t["inflow_asset_id"]) == "cash":
                        s = cost_acc(t["outflow_asset_id"])
                        s["sc"] += t["inflow_amount"]
                        s["sq"] += t["outflow_amount"] or 0.0
                if t["inflow_account_id"] and t["inflow_asset_id"]:
                    net[t["inflow_asset_id"]] = net.get(t["inflow_asset_id"], 0.0) \
                        + (t["inflow_amount"] or 0.0)
                    if (t["outflow_amount"] or 0) > 0 and atype.get(t["outflow_asset_id"]) == "cash":
                        s = cost_acc(t["inflow_asset_id"])
                        s["bc"] += t["outflow_amount"]
                        s["bq"] += t["inflow_amount"] or 0.0
                if t["fee_account_id"] and t["fee_asset_id"] and (t["fee_amount"] or 0) > 0:
                    net[t["fee_asset_id"]] = net.get(t["fee_asset_id"], 0.0) - t["fee_amount"]
            assets_out = []
            cash_disp = 0.0
            open_disp = 0.0
            abs_disp = 0.0
            entry_cost_disp = 0.0
            fx_ok = True
            for asset_id, qty in sorted(net.items()):
                asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
                if asset is None:
                    continue
                asset = dict(asset)
                deriv = conn.execute("SELECT multiplier FROM derivative_details "
                                     "WHERE asset_id = ?", (asset_id,)).fetchone()
                mult = float(deriv["multiplier"]) if deriv and deriv["multiplier"] else 1.0
                price, src = portfolio.price_native(conn, asset)
                value_native = qty * (price or 0.0) * mult
                value_disp, ok = fx.convert(conn, value_native, asset["currency"], display)
                fx_ok = fx_ok and ok
                entry_px = None
                earn_disp = None
                if asset["asset_type"] == "cash":
                    cash_disp += value_disp
                else:
                    open_disp += value_disp
                    abs_disp += abs(value_disp)
                    s = costs.get(asset_id)
                    raw = None  # avg entry cash per unit of qty (multiplier included)
                    if s:
                        if qty >= 0 and s["bq"] > 1e-9:
                            raw = s["bc"] / s["bq"]
                        elif qty < 0 and s["sq"] > 1e-9:
                            raw = s["sc"] / s["sq"]
                    if raw is not None:
                        cost_disp, ok = fx.convert(conn, raw * qty, asset["currency"], display)
                        fx_ok = fx_ok and ok
                        entry_cost_disp += cost_disp
                        entry_px = raw / mult if mult else raw
                        earn_disp = (value_disp - cost_disp) \
                            if value_disp is not None and cost_disp is not None else None
                assets_out.append({
                    "asset_id": asset_id, "ticker": asset["ticker"],
                    "asset_type": asset["asset_type"], "currency": asset["currency"],
                    "net_qty": qty, "price_native": price, "price_source": src,
                    "multiplier": mult, "value_display": value_disp,
                    "entry_px": entry_px, "earning_display": earn_disp,
                })
            out.append({
                "id": g["id"], "name": g["name"], "note": g["note"],
                "group_type": g["group_type"],
                "created_at": g["created_at"], "txn_count": len(members),
                "members": members,
                "assets": assets_out,
                "cash_display": cash_disp,
                "open_value_display": open_disp,
                "total_display": cash_disp + open_disp,
                "abs_value_display": abs_disp,  # open legs only, cash excluded
                # cash deployed for the open legs (-entry cost): realized P&L stripped
                "net_cash_ex_pnl_display": -entry_cost_disp,
                "fx_ok": fx_ok,
            })
    return jsonify({"display_currency": display, "groups": out})


@bp.get("/<int:group_id>/timeseries")
def group_timeseries(group_id):
    """Daily P&L history for one group, first member txn -> today:
    pnl = cumulative cash flows + market value of the open position per date,
    in the display currency (historical prices/FX: last known <= date).
    legs = per non-cash asset running quantity + market value (native and
    display currency), for the positions axis."""
    display = _display_ccy()
    with db.db_conn() as conn:
        if conn.execute("SELECT 1 FROM txn_groups WHERE id = ?", (group_id,)).fetchone() is None:
            return jsonify({"error": "not found"}), 404
        txns = db.rows_to_dicts(conn.execute(
            "SELECT * FROM transactions WHERE group_id = ? ORDER BY txn_date, id",
            (group_id,)))
        empty = {"display_currency": display, "dates": [], "pnl": [],
                 "cash": [], "open_value": [], "legs": []}
        if not txns:
            return jsonify(empty)

        assets: dict[int, dict | None] = {}

        def asset_of(aid):
            if aid not in assets:
                row = conn.execute("SELECT * FROM assets WHERE id = ?", (aid,)).fetchone()
                a = dict(row) if row else None
                if a:
                    deriv = conn.execute("SELECT multiplier FROM derivative_details "
                                         "WHERE asset_id = ?", (aid,)).fetchone()
                    a["_mult"] = float(deriv["multiplier"]) if deriv and deriv["multiplier"] \
                        else 1.0
                assets[aid] = a
            return assets[aid]

        # per-date qty deltas; own-account legs only (same rules as summary)
        by_date: dict[str, list[tuple[int, float]]] = {}
        leg_ids: list[int] = []
        for t in txns:
            d = str(t["txn_date"])[:10]
            flows = []
            if t["outflow_account_id"] and t["outflow_asset_id"]:
                flows.append((t["outflow_asset_id"], -(t["outflow_amount"] or 0.0)))
            if t["inflow_account_id"] and t["inflow_asset_id"]:
                flows.append((t["inflow_asset_id"], t["inflow_amount"] or 0.0))
            if t["fee_account_id"] and t["fee_asset_id"] and (t["fee_amount"] or 0) > 0:
                flows.append((t["fee_asset_id"], -t["fee_amount"]))
            for aid, dq in flows:
                by_date.setdefault(d, []).append((aid, dq))
                a = asset_of(aid)
                if a and a["asset_type"] != "cash" and aid not in leg_ids:
                    leg_ids.append(aid)

        try:
            d = datetime.date.fromisoformat(str(txns[0]["txn_date"])[:10])
        except ValueError:
            return jsonify(empty)
        d_end = datetime.date.fromisoformat(db.today_str())
        today_s = d_end.isoformat()
        net: dict[int, float] = {}
        last_price: dict[int, float] = {}
        dates: list[str] = []
        pnl: list[float] = []
        cash_s: list[float] = []
        open_s: list[float] = []
        leg_qty: dict[int, list[float]] = {aid: [] for aid in leg_ids}
        leg_val_nat: dict[int, list[float]] = {aid: [] for aid in leg_ids}
        leg_val_disp: dict[int, list[float]] = {aid: [] for aid in leg_ids}
        fx_ok = True
        while d <= d_end:
            ds = d.isoformat()
            for aid, dq in by_date.get(ds, []):
                net[aid] = net.get(aid, 0.0) + dq
            cash_v = 0.0
            open_v = 0.0
            day_nat: dict[int, float] = {}
            day_disp: dict[int, float] = {}
            for aid, qty in net.items():
                a = asset_of(aid)
                if a is None:
                    continue
                if a["asset_type"] == "cash":
                    v, ok = fx.convert(conn, qty, a["currency"], display, on_date=ds)
                    cash_v += v
                else:
                    # today: live-first pricing; missing price: carry last known
                    price, _src = portfolio.price_native(
                        conn, a, as_of=None if ds == today_s else ds)
                    if price is None:
                        price = last_price.get(aid)
                    else:
                        last_price[aid] = price
                    v_nat = qty * (price or 0.0) * a["_mult"]
                    v, ok = fx.convert(conn, v_nat, a["currency"], display, on_date=ds)
                    open_v += v
                    day_nat[aid] = v_nat
                    day_disp[aid] = v
                fx_ok = fx_ok and ok
            dates.append(ds)
            cash_s.append(round(cash_v, 2))
            open_s.append(round(open_v, 2))
            pnl.append(round(cash_v + open_v, 2))
            for aid in leg_ids:
                leg_qty[aid].append(net.get(aid, 0.0))
                leg_val_nat[aid].append(round(day_nat.get(aid, 0.0), 2))
                leg_val_disp[aid].append(round(day_disp.get(aid, 0.0), 2))
            d += datetime.timedelta(days=1)

        legs = [{"asset_id": aid, "ticker": (assets[aid] or {}).get("ticker"),
                 "asset_type": (assets[aid] or {}).get("asset_type"),
                 "currency": (assets[aid] or {}).get("currency"),
                 "qty": leg_qty[aid], "value_native": leg_val_nat[aid],
                 "value_display": leg_val_disp[aid]} for aid in leg_ids]
    return jsonify({"display_currency": display, "dates": dates, "pnl": pnl,
                    "cash": cash_s, "open_value": open_s, "legs": legs,
                    "fx_ok": fx_ok})
