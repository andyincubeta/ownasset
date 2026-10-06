"""Daily values matrix (+CSV), asset price matrix (+CSV), snapshot rebuild."""
import csv
import io

from flask import Blueprint, Response, jsonify, request

from backend import db, snapshots

bp = Blueprint("api_history", __name__, url_prefix="/api/history")


def _display_ccy() -> str:
    ccy = request.args.get("ccy")
    if ccy:
        return ccy.upper()
    with db.db_conn() as conn:
        return db.get_setting(conn, "display_currency", "USD")


def _matrix(include_today: bool = False):
    mode = request.args.get("mode", "value")
    if mode not in ("value", "quantity", "price"):
        mode = "value"
    limit = int(request.args.get("days", 0) or 0)
    with db.db_conn() as conn:
        return snapshots.daily_values_matrix(conn, mode=mode, display_cur=_display_ccy(),
                                             limit_days=limit, include_today=include_today)


@bp.get("/daily-values")
def daily_values():
    return jsonify(_matrix(include_today=True))


@bp.get("/daily-values.csv")
def daily_values_csv():
    data = _matrix()
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    header = ["Date", f"TOTAL ({data['display_currency']})"] \
        + [c["ticker"] for c in data["columns"]]
    writer.writerow(header)
    for row in data["rows"]:
        writer.writerow([row["date"]] + ["" if v is None else v for v in row["cells"]])
    filename = f"daily_values_{data['mode']}_{data['display_currency']}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.get("/asset-prices")
def asset_prices():
    limit = int(request.args.get("days", 0) or 0)
    with db.db_conn() as conn:
        return jsonify(snapshots.asset_prices_matrix(conn, limit_days=limit,
                                                     include_today=True))


@bp.get("/asset-prices.csv")
def asset_prices_csv():
    limit = int(request.args.get("days", 0) or 0)
    with db.db_conn() as conn:
        data = snapshots.asset_prices_matrix(conn, limit_days=limit, include_today=False)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["Date"] + [f"{c['ticker']} ({c['currency']})"
                                for c in data["columns"]])
    for row in data["rows"]:
        writer.writerow([row["date"]] + ["" if v is None else v for v in row["cells"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=asset_prices.csv"})


@bp.post("/snapshots/rebuild")
def rebuild():
    body = request.get_json(silent=True) or {}
    try:
        with db.db_conn() as conn:
            n = snapshots.rebuild_snapshots(conn, body.get("start"), body.get("end"))
    except ValueError:
        return jsonify({"error": "invalid start/end date"}), 400
    return jsonify({"rows": n})


@bp.post("/snapshots/take")
def take_now():
    with db.db_conn() as conn:
        n = snapshots.take_snapshot(conn)
    return jsonify({"rows": n})
