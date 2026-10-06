"""Settings, display preferences, data management (stats + purges)."""
import datetime
import re
import threading
import time

from flask import Blueprint, abort, jsonify, request, send_file

from backend import backup, benchmarks, db
from backend.logger import get_logger, set_level

bp = Blueprint("api_settings", __name__, url_prefix="/api/settings")
log = get_logger("dashboard.api")

SCHEDULER_KEYS = {"quote_interval_seconds", "intraday_interval_minutes", "intraday_bar",
                  "daily_refresh_time", "info_refresh_time", "snapshot_time", "backup_time"}


def _utc_timestamp(value) -> int:
    dt = datetime.datetime.fromisoformat(str(value).strip().replace("T", " "))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


@bp.get("")
def get_settings():
    with db.db_conn() as conn:
        return jsonify(db.get_all_settings(conn))


@bp.post("")
def save_settings():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not data:
        return jsonify({"error": "object body required"}), 400
    if "day_close_mode" in data and data["day_close_mode"] not in ("market", "cutoff"):
        return jsonify({"error": "day_close_mode must be 'market' or 'cutoff'"}), 400
    if "day_close_cutoff_utc" in data and not re.fullmatch(
            r"([01]\d|2[0-3]):[0-5]\d", str(data["day_close_cutoff_utc"]).strip()):
        return jsonify({"error": "day_close_cutoff_utc must be HH:MM (UTC)"}), 400
    if "backup_time" in data and not re.fullmatch(
            r"([01]\d|2[0-3]):[0-5]\d", str(data["backup_time"]).strip()):
        return jsonify({"error": "backup_time must be HH:MM (UTC)"}), 400
    if "backup_keep_count" in data and not (
            str(data["backup_keep_count"]).strip().isdigit()
            and int(str(data["backup_keep_count"]).strip()) >= 1):
        return jsonify({"error": "backup_keep_count must be an integer >= 1"}), 400
    with db.db_conn() as conn:
        old_mode = db.get_setting(conn, "day_close_mode")
        old_cut = db.get_setting(conn, "day_close_cutoff_utc")
        for key, value in data.items():
            db.set_setting(conn, str(key), str(value))
        new_mode = db.get_setting(conn, "day_close_mode")
        new_cut = db.get_setting(conn, "day_close_cutoff_utc")
        if new_cut != old_cut:
            conn.execute("UPDATE prices_daily SET ext_close = NULL, ext_close_ts = NULL")
        has_ext = conn.execute("SELECT 1 FROM prices_daily WHERE ext_close IS NOT NULL "
                               "LIMIT 1").fetchone() is not None
    if "log_level" in data:
        set_level(str(data["log_level"]))
        log.info("log level changed to %s", data["log_level"])
    if SCHEDULER_KEYS & set(data):
        from backend import scheduler
        scheduler.reschedule()
    cut_changed = new_mode == "cutoff" and new_cut != old_cut
    if new_mode != old_mode or cut_changed:
        from backend import scheduler
        refetch = new_mode == "cutoff" and not has_ext
        threading.Thread(target=scheduler.apply_day_close_change, args=(refetch,),
                         daemon=True, name="day-close-apply").start()
    log.info("settings saved: %s", sorted(data.keys()))
    return jsonify({"ok": True})


@bp.get("/indices")
def get_indices():
    with db.db_conn() as conn:
        return jsonify({"configured": benchmarks.get_benchmarks(conn),
                        "presets": benchmarks.presets(),
                        "max_home": benchmarks.MAX_HOME})


@bp.put("/indices")
def put_indices():
    data = request.get_json(silent=True) or {}
    entries, err = benchmarks.validate_indices(data.get("indices"))
    if err:
        return jsonify({"error": err}), 400
    if not entries:
        return jsonify({"error": "keep at least one index"}), 400
    with db.db_conn() as conn:
        benchmarks.save_benchmarks(conn, entries)
    log.info("benchmark indices saved: %s", [b["ticker"] for b in entries])
    return jsonify({"configured": entries})


@bp.get("/indices/lookup")
def lookup_index():
    """Name/currency/last price for a Yahoo symbol, to vet a custom index."""
    ticker = (request.args.get("ticker") or "").strip().upper()
    if not benchmarks.TICKER_RE.fullmatch(ticker):
        return jsonify({"error": "invalid ticker"}), 400
    from backend import yahoo
    try:
        meta = yahoo.client.get_chart(ticker, interval="1d",
                                      period1=int(time.time()) - 10 * 86400)["meta"]
    except Exception as exc:  # noqa: BLE001 - surface any Yahoo failure to the user
        return jsonify({"error": f"lookup failed for {ticker}: {exc}"}), 404
    return jsonify({"ticker": ticker,
                    "name": meta.get("longName") or meta.get("shortName") or ticker,
                    "currency": meta.get("currency"),
                    "price": meta.get("regularMarketPrice")})


@bp.get("/datastats")
def datastats():
    with db.db_conn() as conn:
        stats = db.table_stats(conn)
        intraday_by_asset = db.rows_to_dicts(conn.execute(
            "SELECT p.asset_id, a.ticker, COUNT(*) AS rows_count, "
            "MIN(p.ts) AS min_ts, MAX(p.ts) AS max_ts "
            "FROM prices_intraday p JOIN assets a ON a.id = p.asset_id "
            "GROUP BY p.asset_id ORDER BY rows_count DESC"))
    return jsonify({"tables": stats, "db_size_bytes": db.db_file_size(),
                    "intraday_by_asset": intraday_by_asset})


@bp.post("/purge")
def purge():
    data = request.get_json(silent=True) or {}
    target = data.get("target")
    with db.db_conn() as conn:
        if target == "intraday":
            clauses, args = [], []
            if data.get("asset_id"):
                clauses.append("asset_id = ?")
                args.append(int(data["asset_id"]))
            if data.get("older_than"):  # YYYY-MM-DD
                ts = _utc_timestamp(data["older_than"])
                clauses.append("ts < ?")
                args.append(ts)
            sql = "DELETE FROM prices_intraday"
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            cur = conn.execute(sql, args)
        elif target == "scrape_log":
            cur = conn.execute("DELETE FROM scrape_log")
        else:
            return jsonify({"error": "target must be intraday | scrape_log"}), 400
        deleted = cur.rowcount
    log.warning("purge target=%s deleted=%d args=%s", target, deleted, data)
    return jsonify({"deleted": deleted})


@bp.get("/backups")
def get_backups():
    return jsonify(backup.list_backups())


@bp.post("/backups")
def run_backup():
    from backend import scheduler
    scheduler.run_job_now("backup")
    return jsonify({"ok": True})


@bp.get("/backups/<name>")
def download_backup(name: str):
    path = backup.resolve_backup(name)
    if path is None:
        abort(404)
    return send_file(path, as_attachment=True, download_name=name,
                     mimetype="application/vnd.sqlite3")
