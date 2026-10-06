"""Background jobs (APScheduler): quotes, intraday bars, daily history,
info dumps, snapshots. Every run is timed and recorded in scrape_log +
logs/scrape.log. Rate-limit responses trigger adaptive backoff.
"""
import datetime
import threading
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from backend import db, fx
from backend.logger import get_logger
from backend.option_archive import history_for_asset as archive_history
from backend.yahoo import RateLimitedError, client, polite_sleep

log = get_logger("dashboard.scrape")

_scheduler: BackgroundScheduler | None = None
_backoff_until = 0.0
_backoff_factor = 1
JOB_NAMES = ["quotes", "intraday", "daily", "info", "snapshot", "backup"]


def _utc_date_from_ts(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).date().isoformat()


# Yahoo serves pre/post intraday bars for ~730d at 60m and ~60d at 5m
EXT_BACKFILL_DAYS = 729
EXT_RECENT_DAYS = 35


def _ext_close_rows(bars: list[dict], cutoff_sec: int) -> dict[str, tuple[float, int]]:
    """{date: (close, bar_ts)} = last bar starting before each date's UTC cutoff.
    Day D ends at the first HH:MM UTC after D 00:00 (00:00 -> midnight ending D)."""
    shift = (86400 - cutoff_sec) % 86400
    out: dict[str, tuple[float, int]] = {}
    for b in sorted(bars, key=lambda x: x["ts"]):
        if b["close"] is not None:
            out[_utc_date_from_ts(b["ts"] + shift)] = (b["close"], b["ts"])
    return out


def fetch_ext_close(asset_id: int, ticker: str, full: bool = False) -> int:
    """Fill prices_daily.ext_close from pre/regular/post intraday bars.
    Only existing daily rows (trading days) are updated."""
    now = int(time.time())
    days, bar = (EXT_BACKFILL_DAYS, "60m") if full else (EXT_RECENT_DAYS, "5m")
    data = client.get_chart(ticker, interval=bar, period1=now - days * 86400, period2=now,
                            include_prepost=True)
    with db.db_conn() as conn:
        rows = _ext_close_rows(data["bars"], db.cutoff_seconds(conn))
        conn.executemany(
            "UPDATE prices_daily SET ext_close = ?, ext_close_ts = ? "
            "WHERE asset_id = ? AND date = ?",
            [(c, ts, asset_id, d) for d, (c, ts) in rows.items()])
    return len(rows)


def job_ext_close(full: bool = True):
    """Recompute ext closes for every yahoo asset (after a day-close setting change)."""
    with db.db_conn() as conn:
        assets = conn.execute(
            "SELECT id, ticker FROM assets WHERE active = 1 AND is_yahoo_tracked = 1 "
            "AND price_provider = 'yahoo'").fetchall()
        if full:
            conn.execute("UPDATE prices_daily SET ext_close = NULL, ext_close_ts = NULL")
    ok = err = 0
    for a in assets:
        try:
            fetch_ext_close(a["id"], a["ticker"], full=full)
            ok += 1
        except Exception as exc:
            err += 1
            log_run("daily", "error", 0, ticker=a["ticker"], message=f"ext close: {exc!r}")
        polite_sleep()
    return f"ext close {ok} ok / {err} err"


def apply_day_close_change(refetch: bool):
    """Background task after day_close_* settings change: refetch then rebuild snapshots."""
    from backend import snapshots
    if refetch:
        _timed("daily", job_ext_close, True)
    with db.db_conn() as conn:
        n = snapshots.rebuild_snapshots(conn)
    log.info("day close setting applied: %d snapshot rows rebuilt", n)


def log_run(job: str, status: str, duration_ms: float, ticker: str = "", message: str = ""):
    try:
        with db.db_conn() as conn:
            conn.execute(
                "INSERT INTO scrape_log(ts, job, ticker, status, duration_ms, message) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (db.now_ms_str(), job, ticker, status, round(duration_ms, 1), message[:2000]))
            keep = db.get_int_setting(conn, "scrape_log_keep_rows", 20000)
            conn.execute(
                "DELETE FROM scrape_log WHERE id <= "
                "(SELECT MAX(id) FROM scrape_log) - ?", (keep,))
    except Exception:  # pragma: no cover - logging must never kill a job
        log.exception("failed to write scrape_log")
    lvl = log.info if status == "success" else log.error
    lvl("job=%s ticker=%s status=%s %.0fms %s", job, ticker, status, duration_ms, message)


def _timed(job_name: str, fn, *args, **kwargs):
    global _backoff_until, _backoff_factor
    if job_name == "quotes" and time.time() < _backoff_until:
        log_run(job_name, "skipped", 0, message="rate-limit backoff active")
        return
    started = time.perf_counter()
    try:
        detail = fn(*args, **kwargs) or ""
        _backoff_factor = 1
        log_run(job_name, "success", (time.perf_counter() - started) * 1000, message=str(detail))
    except RateLimitedError as exc:
        _backoff_factor = min(_backoff_factor * 2, 16)
        with db.db_conn() as conn:
            interval = db.get_int_setting(conn, "quote_interval_seconds", 60)
        _backoff_until = time.time() + interval * _backoff_factor
        log_run(job_name, "error", (time.perf_counter() - started) * 1000,
                message=f"rate limited; backoff x{_backoff_factor}: {exc}")
    except Exception as exc:
        log_run(job_name, "error", (time.perf_counter() - started) * 1000, message=repr(exc))


# ------------------------------------------------------------------ jobs --

def job_quotes():
    """ONE batched request for all active yahoo tickers + FX pairs."""
    with db.db_conn() as conn:
        assets = conn.execute(
            "SELECT id, ticker FROM assets WHERE active = 1 AND is_yahoo_tracked = 1 "
            "AND price_provider = 'yahoo'").fetchall()
        fx_tickers = fx.needed_fx_tickers(conn)
    ticker_map = {a["ticker"]: a["id"] for a in assets}
    universe = list(ticker_map) + fx_tickers
    if not universe:
        return "universe empty"
    quotes = client.get_quotes(universe)
    now = int(time.time())
    missing = [t for t in universe if t not in quotes]
    with db.db_conn() as conn:
        for tkr, q in quotes.items():
            if tkr in ticker_map and q["price"] is not None:
                conn.execute(
                    "INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source, "
                    "session_date) VALUES (?, ?, ?, ?, 'batch', ?) "
                    "ON CONFLICT(asset_id) DO UPDATE SET price=excluded.price, "
                    "prev_close=excluded.prev_close, ts=excluded.ts, source=excluded.source, "
                    "session_date=excluded.session_date",
                    (ticker_map[tkr], q["price"], q["prev_close"], q["ts"] or now,
                     q.get("session_date")))
            elif tkr.endswith("=X") and q["price"] is not None:
                fx.store_latest(conn, tkr[:-2], q["price"], q["ts"] or now)
    # fallback: per-ticker chart meta for anything the batch endpoint missed
    fetched_fallback = 0
    still_missing = list(missing[10:])  # beyond the fallback attempt cap
    for tkr in missing[:10]:
        try:
            q = client.get_quote_via_chart(tkr)
            if q["price"] is None:
                still_missing.append(tkr)
                continue
            with db.db_conn() as conn:
                if tkr in ticker_map:
                    conn.execute(
                        "INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source, "
                        "session_date) VALUES (?, ?, ?, ?, 'chart', ?) "
                        "ON CONFLICT(asset_id) DO UPDATE SET price=excluded.price, "
                        "prev_close=excluded.prev_close, ts=excluded.ts, source=excluded.source, "
                        "session_date=excluded.session_date",
                        (ticker_map[tkr], q["price"], q["prev_close"], q["ts"],
                         q.get("session_date")))
                else:
                    fx.store_latest(conn, tkr[:-2], q["price"], q["ts"])
            fetched_fallback += 1
            polite_sleep(0.2)
        except Exception as exc:
            log.debug("fallback quote failed for %s: %r", tkr, exc)
            still_missing.append(tkr)
    msg = f"{len(quotes)}/{len(universe)} quotes (+{fetched_fallback} fallback)"
    if still_missing:
        shown = ", ".join(still_missing[:20])
        extra = len(still_missing) - 20
        msg += f"; missing: {shown}" + (f" (+{extra} more)" if extra > 0 else "")
    return msg


def job_intraday():
    """Recent intraday bars for each yahoo asset at the configured bar size."""
    with db.db_conn() as conn:
        bar = db.get_setting(conn, "intraday_bar", "60m")
        assets = conn.execute(
            "SELECT id, ticker FROM assets WHERE active = 1 AND is_yahoo_tracked = 1 "
            "AND price_provider = 'yahoo'").fetchall()
    now = int(time.time())
    period1 = now - 7 * 86400
    ok = err = 0
    for a in assets:
        try:
            data = client.get_chart(a["ticker"], interval=bar, period1=period1, period2=now)
            with db.db_conn() as conn:
                conn.executemany(
                    "INSERT INTO prices_intraday(asset_id, bar_interval, ts, open, high, low, "
                    "close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(asset_id, bar_interval, ts) DO UPDATE SET open=excluded.open, "
                    "high=excluded.high, low=excluded.low, close=excluded.close, "
                    "volume=excluded.volume",
                    [(a["id"], bar, b["ts"], b["open"], b["high"], b["low"], b["close"],
                      b["volume"]) for b in data["bars"]])
            ok += 1
        except Exception as exc:
            err += 1
            log_run("intraday", "error", 0, ticker=a["ticker"], message=repr(exc))
        polite_sleep()
    return f"{ok} ok / {err} err @ {bar}"


def backfill_daily_asset(asset_id: int, ticker: str) -> int:
    """Full daily history from the earliest date Yahoo has (period1=0)."""
    data = client.get_chart(ticker, interval="1d", period1=0, period2=9999999999)
    rows = []
    seen = set()
    for b in data["bars"]:
        d = _utc_date_from_ts(b["ts"])
        if d in seen:
            continue
        seen.add(d)
        rows.append((asset_id, d, b["open"], b["high"], b["low"], b["close"],
                     b["adj_close"], b["volume"]))
    with db.db_conn() as conn:
        conn.executemany(
            "INSERT INTO prices_daily(asset_id, date, open, high, low, close, adj_close, "
            "volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(asset_id, date) DO UPDATE SET open=excluded.open, high=excluded.high, "
            "low=excluded.low, close=excluded.close, adj_close=excluded.adj_close, "
            "volume=excluded.volume", rows)
    return len(rows)


def backfill_archive_asset(asset_id: int) -> int:
    """Whole contract life from the options archive (ONE request) -> prices_daily.
    Also refreshes latest_quotes so holdings show the newest EOD close."""
    with db.db_conn() as conn:
        asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        deriv = conn.execute("SELECT * FROM derivative_details WHERE asset_id = ?",
                             (asset_id,)).fetchone()
    if asset is None:
        raise ValueError(f"asset {asset_id} not found")
    bars = archive_history(dict(asset), dict(deriv) if deriv else None)
    if not bars:
        return 0
    with db.db_conn() as conn:
        conn.executemany(
            "INSERT INTO prices_daily(asset_id, date, open, high, low, close, adj_close, "
            "volume) VALUES (?, ?, NULL, NULL, NULL, ?, ?, ?) "
            "ON CONFLICT(asset_id, date) DO UPDATE SET close=excluded.close, "
            "adj_close=excluded.adj_close, volume=excluded.volume",
            [(asset_id, b["date"], b["close"], b["close"], b["volume"]) for b in bars])
        last = bars[-1]
        prev = bars[-2] if len(bars) > 1 else None
        ts = int(datetime.datetime.fromisoformat(last["date"]).replace(
            tzinfo=datetime.timezone.utc).timestamp())
        conn.execute(
            "INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source, session_date) "
            "VALUES (?, ?, ?, ?, 'archive', ?) "
            "ON CONFLICT(asset_id) DO UPDATE SET price=excluded.price, "
            "prev_close=excluded.prev_close, ts=excluded.ts, source=excluded.source, "
            "session_date=excluded.session_date",
            (asset_id, last["close"], prev["close"] if prev else None, ts, last["date"][:10]))
    return len(bars)


def _archive_final(conn, asset_id: int) -> bool:
    """True when the contract expired and its stored history will not change."""
    row = conn.execute("SELECT expiry FROM derivative_details WHERE asset_id = ?",
                       (asset_id,)).fetchone()
    expiry = str(row["expiry"])[:10] if row and row["expiry"] else ""
    today = db.today_str()
    if not expiry or expiry >= today:
        return False
    last = conn.execute("SELECT MAX(date) FROM prices_daily WHERE asset_id = ?",
                        (asset_id,)).fetchone()[0]
    if last is None:
        return False
    week_ago = (datetime.date.fromisoformat(today) - datetime.timedelta(days=7)).isoformat()
    return last >= expiry or expiry < week_ago


def job_daily():
    """Daily OHLC upkeep for assets and FX pairs. Full backfill when table empty."""
    with db.db_conn() as conn:
        assets = conn.execute(
            "SELECT id, ticker FROM assets WHERE active = 1 AND is_yahoo_tracked = 1 "
            "AND price_provider = 'yahoo'").fetchall()
        archive_assets = conn.execute(
            "SELECT id, ticker FROM assets WHERE active = 1 AND is_yahoo_tracked = 1 "
            "AND price_provider = 'option_archive'").fetchall()
        fx_tickers = fx.needed_fx_tickers(conn)
        cutoff_mode = db.get_setting(conn, "day_close_mode") == "cutoff"
    now = int(time.time())
    ok = err = 0
    for a in assets:
        try:
            with db.db_conn() as conn:
                have = conn.execute("SELECT COUNT(*) FROM prices_daily WHERE asset_id = ?",
                                    (a["id"],)).fetchone()[0]
            if have == 0:
                n = backfill_daily_asset(a["id"], a["ticker"])
                log_run("daily", "success", 0, ticker=a["ticker"], message=f"backfill {n} bars")
            else:
                data = client.get_chart(a["ticker"], interval="1d",
                                        period1=now - 35 * 86400, period2=now)
                rows = []
                for b in data["bars"]:
                    d = _utc_date_from_ts(b["ts"])
                    rows.append((a["id"], d, b["open"], b["high"], b["low"], b["close"],
                                 b["adj_close"], b["volume"]))
                with db.db_conn() as conn:
                    conn.executemany(
                        "INSERT INTO prices_daily(asset_id, date, open, high, low, close, "
                        "adj_close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(asset_id, date) DO UPDATE SET open=excluded.open, "
                        "high=excluded.high, low=excluded.low, close=excluded.close, "
                        "adj_close=excluded.adj_close, volume=excluded.volume", rows)
            if cutoff_mode:
                with db.db_conn() as conn:
                    has_ext = conn.execute(
                        "SELECT 1 FROM prices_daily WHERE asset_id = ? AND ext_close IS NOT NULL "
                        "LIMIT 1", (a["id"],)).fetchone()
                polite_sleep()
                fetch_ext_close(a["id"], a["ticker"], full=has_ext is None)
            ok += 1
        except Exception as exc:
            err += 1
            log_run("daily", "error", 0, ticker=a["ticker"], message=repr(exc))
        polite_sleep()
    arch_ok = arch_err = 0
    for a in archive_assets:
        try:
            with db.db_conn() as conn:
                if _archive_final(conn, a["id"]):
                    continue
            n = backfill_archive_asset(a["id"])
            log_run("daily", "success", 0, ticker=a["ticker"], message=f"archive {n} bars")
            arch_ok += 1
        except Exception as exc:
            arch_err += 1
            log_run("daily", "error", 0, ticker=a["ticker"], message=repr(exc))
        polite_sleep()
    fx_ok = 0
    for tkr in fx_tickers:
        pair = tkr[:-2]
        try:
            with db.db_conn() as conn:
                have = conn.execute("SELECT COUNT(*) FROM fx_daily WHERE pair = ?",
                                    (pair,)).fetchone()[0]
            period1 = 0 if have == 0 else now - 35 * 86400
            data = client.get_chart(tkr, interval="1d", period1=period1, period2=now)
            rows = []
            for b in data["bars"]:
                d = _utc_date_from_ts(b["ts"])
                rows.append((d, b["close"]))
            with db.db_conn() as conn:
                fx.store_daily(conn, pair, rows)
            fx_ok += 1
        except Exception as exc:
            log_run("daily", "error", 0, ticker=tkr, message=repr(exc))
        polite_sleep()
    _resync_recent_snapshots()
    return (f"{ok} assets ok / {err} err; {fx_ok} fx pairs; "
            f"{arch_ok} archive ok / {arch_err} err")


def _resync_recent_snapshots(days: int = 7):
    """Re-price the last frozen snapshot days (taken at live/ext-hours prices) with
    the configured daily close."""
    from backend import snapshots
    today = datetime.datetime.now(datetime.timezone.utc).date()
    start = (today - datetime.timedelta(days=days)).isoformat()
    end = (today - datetime.timedelta(days=1)).isoformat()
    with db.db_conn() as conn:
        first_txn = conn.execute("SELECT MIN(txn_date) FROM transactions").fetchone()[0]
        if first_txn is None or str(first_txn)[:10] > end:
            return
        snapshots.rebuild_snapshots(conn, max(start, str(first_txn)[:10]), end)


def job_info():
    """Daily full quoteSummary dump per asset -> assets.info_json (ALL fields kept)."""
    with db.db_conn() as conn:
        modules = db.get_setting(conn, "quote_summary_modules")
        assets = conn.execute(
            "SELECT id, ticker, name, industry FROM assets "
            "WHERE active = 1 AND is_yahoo_tracked = 1 AND price_provider = 'yahoo'").fetchall()
    ok = err = 0
    for a in assets:
        try:
            info = client.get_stock_info(a["ticker"], modules)
            if info is None:
                err += 1
                continue
            import json as _json
            with db.db_conn() as conn:
                conn.execute(
                    "UPDATE assets SET info_json = ?, info_updated_at = ?, "
                    "name = CASE WHEN name = '' THEN ? ELSE name END, "
                    "industry = CASE WHEN industry = '' THEN ? ELSE industry END, "
                    "sector = CASE WHEN sector = '' THEN ? ELSE sector END, "
                    "country = CASE WHEN country = '' THEN ? ELSE country END "
                    "WHERE id = ?",
                    (_json.dumps(info), db.now_str(),
                     info.get("longName") or info.get("shortName") or "",
                     info.get("industry") or "", info.get("sector") or "",
                     info.get("country") or "", a["id"]))
            ok += 1
        except Exception as exc:
            err += 1
            log_run("info", "error", 0, ticker=a["ticker"], message=repr(exc))
        polite_sleep()
    return f"{ok} ok / {err} err"


def job_snapshot():
    from backend import snapshots
    with db.db_conn() as conn:
        n = snapshots.take_snapshot(conn)
    return f"{n} positions"


def catchup_snapshots():
    """Backfill snapshot days missed while the app was off (runs at startup).
    Refreshes daily prices/FX first so the rebuild has closes for gap days."""
    from backend import snapshots
    with db.db_conn() as conn:
        last = conn.execute("SELECT MAX(date) FROM daily_snapshots").fetchone()[0]
    yesterday = (datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)).isoformat()
    if last is None or last >= yesterday:
        return "up to date"
    _timed("daily", job_daily)
    gap_start = (datetime.date.fromisoformat(last[:10]) + datetime.timedelta(days=1)).isoformat()
    with db.db_conn() as conn:
        n = snapshots.rebuild_snapshots(conn, gap_start, yesterday)
    return f"backfilled {gap_start}..{yesterday} ({n} rows)"


def job_backup():
    from backend import backup
    return backup.create_backup()


_JOB_FUNCS = {
    "quotes": job_quotes, "intraday": job_intraday, "daily": job_daily,
    "info": job_info, "snapshot": job_snapshot, "backup": job_backup,
}


# ------------------------------------------------------------- lifecycle --

def _cron_from_hhmm(value: str, fallback: str) -> CronTrigger:
    try:
        hh, mm = value.strip().split(":")
        return CronTrigger(hour=int(hh), minute=int(mm))
    except Exception:
        hh, mm = fallback.split(":")
        return CronTrigger(hour=int(hh), minute=int(mm))


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    sched = BackgroundScheduler(daemon=True, timezone=datetime.timezone.utc)
    with db.db_conn() as conn:
        s = db.get_all_settings(conn)
    sched.add_job(lambda: _timed("quotes", job_quotes), IntervalTrigger(
        seconds=max(10, int(float(s["quote_interval_seconds"])))), id="quotes",
        max_instances=1, coalesce=True)
    sched.add_job(lambda: _timed("intraday", job_intraday), IntervalTrigger(
        minutes=max(1, int(float(s["intraday_interval_minutes"])))), id="intraday",
        max_instances=1, coalesce=True)
    sched.add_job(lambda: _timed("daily", job_daily),
                  _cron_from_hhmm(s["daily_refresh_time"], "22:30"), id="daily",
                  max_instances=1, coalesce=True)
    sched.add_job(lambda: _timed("info", job_info),
                  _cron_from_hhmm(s["info_refresh_time"], "21:00"), id="info",
                  max_instances=1, coalesce=True)
    sched.add_job(lambda: _timed("snapshot", job_snapshot),
                  _cron_from_hhmm(s["snapshot_time"], "23:59"), id="snapshot",
                  max_instances=1, coalesce=True, misfire_grace_time=3600)
    sched.add_job(lambda: _timed("backup", job_backup),
                  _cron_from_hhmm(s["backup_time"], "00:30"), id="backup",
                  max_instances=1, coalesce=True, misfire_grace_time=3600)
    sched.start()
    _scheduler = sched
    threading.Thread(target=_timed, args=("snapshot", catchup_snapshots), daemon=True,
                     name="snapshot-catchup").start()
    log.info("scheduler started: %s", [j.id for j in sched.get_jobs()])
    return sched


def reschedule():
    """Re-read settings and apply new intervals/times."""
    if _scheduler is None:
        return
    with db.db_conn() as conn:
        s = db.get_all_settings(conn)
    _scheduler.reschedule_job("quotes", trigger=IntervalTrigger(
        seconds=max(10, int(float(s["quote_interval_seconds"])))))
    _scheduler.reschedule_job("intraday", trigger=IntervalTrigger(
        minutes=max(1, int(float(s["intraday_interval_minutes"])))))
    _scheduler.reschedule_job("daily", trigger=_cron_from_hhmm(s["daily_refresh_time"], "22:30"))
    _scheduler.reschedule_job("info", trigger=_cron_from_hhmm(s["info_refresh_time"], "21:00"))
    _scheduler.reschedule_job("snapshot", trigger=_cron_from_hhmm(s["snapshot_time"], "23:59"))
    _scheduler.reschedule_job("backup", trigger=_cron_from_hhmm(s["backup_time"], "00:30"))
    log.info("scheduler rescheduled from settings")


def run_job_now(name: str) -> bool:
    fn = _JOB_FUNCS.get(name)
    if fn is None:
        return False
    threading.Thread(target=_timed, args=(name, fn), daemon=True,
                     name=f"manual-{name}").start()
    return True


def trigger_backfill(asset_id: int, ticker: str):
    """One-shot full history backfill for a newly added/switched asset,
    routed by the asset's price provider."""
    with db.db_conn() as conn:
        row = conn.execute("SELECT price_provider FROM assets WHERE id = ?",
                           (asset_id,)).fetchone()
    if row and row["price_provider"] == "option_archive":
        def _run():
            _timed("daily", lambda: f"archive backfill {ticker}: "
                   f"{backfill_archive_asset(asset_id)} bars")
    else:
        def _run():
            _timed("daily", lambda: f"backfill {ticker}: "
                   f"{backfill_daily_asset(asset_id, ticker)} bars")
    threading.Thread(target=_run, daemon=True, name=f"backfill-{ticker}").start()


def job_status() -> list[dict]:
    out = []
    jobs = {j.id: j for j in (_scheduler.get_jobs() if _scheduler else [])}
    with db.db_conn() as conn:
        for name in JOB_NAMES:
            last = conn.execute(
                "SELECT ts, status, duration_ms, message FROM scrape_log "
                "WHERE job = ? AND ticker = '' ORDER BY id DESC LIMIT 1", (name,)).fetchone()
            day_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)) \
                .strftime("%Y-%m-%d %H:%M:%S")
            agg = conn.execute(
                "SELECT SUM(status = 'success') AS ok, COUNT(*) AS total FROM scrape_log "
                "WHERE job = ? AND ts >= ?", (name, day_ago)).fetchone()
            job = jobs.get(name)
            out.append({
                "job": name,
                "next_run": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
                if job and job.next_run_time else None,
                "last_run": dict(last) if last else None,
                "success_24h": agg["ok"] or 0, "total_24h": agg["total"] or 0,
                "backoff_active": name == "quotes" and time.time() < _backoff_until,
            })
    return out
