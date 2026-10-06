"""Scrape monitor: job status, run log, hourly aggregates, manual triggers."""
import datetime

from flask import Blueprint, jsonify, request

from backend import db, scheduler

bp = Blueprint("api_scrape", __name__, url_prefix="/api/scrape")


@bp.get("/status")
def status():
    return jsonify(scheduler.job_status())


@bp.get("/log")
def log_rows():
    clauses, args = [], []
    if request.args.get("job"):
        clauses.append("job = ?")
        args.append(request.args["job"])
    if request.args.get("status"):
        clauses.append("status = ?")
        args.append(request.args["status"])
    sql = "SELECT * FROM scrape_log"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(request.args.get("limit", 200)))
    with db.db_conn() as conn:
        return jsonify(db.rows_to_dicts(conn.execute(sql, args)))


@bp.get("/hourly")
def hourly():
    """Success/error counts per hour for the last 24h."""
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)) \
        .strftime("%Y-%m-%d %H:00:00")
    with db.db_conn() as conn:
        rows = conn.execute(
            "SELECT substr(ts, 1, 13) AS hour, "
            "SUM(status = 'success') AS ok, SUM(status = 'error') AS err "
            "FROM scrape_log WHERE ts >= ? GROUP BY hour ORDER BY hour", (since,)).fetchall()
    return jsonify([{"hour": r["hour"] + ":00", "ok": r["ok"] or 0, "err": r["err"] or 0}
                    for r in rows])


@bp.post("/run/<job>")
def run_now(job):
    if not scheduler.run_job_now(job):
        return jsonify({"error": f"unknown job '{job}'"}), 404
    return jsonify({"ok": True, "job": job})
