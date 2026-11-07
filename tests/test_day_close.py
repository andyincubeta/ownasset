"""Configurable day close: official market close vs UTC day-end cutoff."""
import datetime

from backend import db, portfolio, scheduler


def _ts(s: str) -> int:
    return int(datetime.datetime.fromisoformat(s).replace(
        tzinfo=datetime.timezone.utc).timestamp())


def _bar(s: str, c: float) -> dict:
    return {"ts": _ts(s), "close": c}


def test_ext_close_rows_midnight_cutoff():
    bars = [_bar("2024-02-02 14:30", 100), _bar("2024-02-02 19:55", 101),  # regular
            _bar("2024-02-02 23:55", 102),                                 # post-market
            _bar("2024-02-03 00:00", 999)]                                 # next day
    rows = scheduler._ext_close_rows(bars, 0)
    assert rows["2024-02-02"] == (102, _ts("2024-02-02 23:55"))
    assert rows["2024-02-03"][0] == 999


def test_ext_close_rows_evening_cutoff():
    bars = [_bar("2024-02-02 19:55", 101), _bar("2024-02-02 20:00", 103),
            _bar("2024-02-02 23:55", 102)]
    rows = scheduler._ext_close_rows(bars, 20 * 3600)
    assert rows["2024-02-02"][0] == 101   # bar starting at the cutoff belongs to next day
    assert rows["2024-02-03"][0] == 102


def test_cutoff_seconds_parsing(conn):
    assert db.cutoff_seconds(conn) == 0
    db.set_setting(conn, "day_close_cutoff_utc", "20:30")
    assert db.cutoff_seconds(conn) == 20 * 3600 + 30 * 60
    db.set_setting(conn, "day_close_cutoff_utc", "bogus")
    assert db.cutoff_seconds(conn) == 0


def test_price_native_respects_mode(sample_portfolio):
    aapl = sample_portfolio["aapl"]
    with db.db_conn() as c:
        asset = dict(c.execute("SELECT * FROM assets WHERE id = ?", (aapl,)).fetchone())
        c.execute("UPDATE prices_daily SET ext_close = 182 WHERE asset_id = ?", (aapl,))
        assert portfolio.price_native(c, asset, "2024-02-02") == (180.0, "daily")
        db.set_setting(c, "day_close_mode", "cutoff")
        assert portfolio.price_native(c, asset, "2024-02-02") == (182.0, "daily")
        c.execute("UPDATE prices_daily SET ext_close = NULL WHERE asset_id = ?", (aapl,))
        assert portfolio.price_native(c, asset, "2024-02-02") == (180.0, "daily")


def test_fetch_ext_close_updates_existing_rows_only(sample_portfolio, monkeypatch):
    aapl = sample_portfolio["aapl"]
    bars = [_bar("2024-02-02 23:00", 181.5), _bar("2024-02-03 12:00", 190)]
    monkeypatch.setattr(scheduler.client, "get_chart", lambda *a, **k: {"meta": {}, "bars": bars})
    scheduler.fetch_ext_close(aapl, "AAPL")
    with db.db_conn() as c:
        rows = c.execute("SELECT date, ext_close FROM prices_daily WHERE asset_id = ?",
                         (aapl,)).fetchall()
    assert [(r["date"], r["ext_close"]) for r in rows] == [("2024-02-02", 181.5)]


def _quotes_return(monkeypatch, quotes: dict):
    monkeypatch.setattr(scheduler.client, "get_quotes", lambda tickers: {
        t: {"price": p, "prev_close": 178.0, "ts": ts, "session_date": None}
        for t, (p, ts) in quotes.items() if t in tickers})
    monkeypatch.setattr(scheduler.client, "get_quote_via_chart",
                        lambda t: {"price": None, "prev_close": None, "ts": None})


def _ext(aapl):
    with db.db_conn() as c:
        r = c.execute("SELECT ext_close, ext_close_ts FROM prices_daily "
                      "WHERE asset_id = ? AND date = '2024-02-02'", (aapl,)).fetchone()
    return r["ext_close"], r["ext_close_ts"]


def test_job_quotes_keeps_ext_close_current_in_cutoff_mode(sample_portfolio, monkeypatch):
    aapl = sample_portfolio["aapl"]
    with db.db_conn() as c:
        db.set_setting(c, "day_close_mode", "cutoff")
    _quotes_return(monkeypatch, {"AAPL": (183.0, _ts("2024-02-02 23:30"))})
    scheduler.job_quotes()
    assert _ext(aapl) == (183.0, _ts("2024-02-02 23:30"))
    # past the 00:00 cutoff: belongs to 2024-02-03 (no trading-day row) -> untouched
    _quotes_return(monkeypatch, {"AAPL": (185.0, _ts("2024-02-03 00:30"))})
    scheduler.job_quotes()
    assert _ext(aapl) == (183.0, _ts("2024-02-02 23:30"))
    # an older tick never overwrites a newer stored one
    _quotes_return(monkeypatch, {"AAPL": (170.0, _ts("2024-02-02 21:00"))})
    scheduler.job_quotes()
    assert _ext(aapl) == (183.0, _ts("2024-02-02 23:30"))


def test_job_quotes_leaves_ext_close_alone_in_market_mode(sample_portfolio, monkeypatch):
    aapl = sample_portfolio["aapl"]
    _quotes_return(monkeypatch, {"AAPL": (183.0, _ts("2024-02-02 23:30"))})
    scheduler.job_quotes()
    assert _ext(aapl) == (None, None)


def test_cutoff_day_evening_cutoff():
    assert scheduler._cutoff_day(_ts("2024-02-02 19:59"), 20 * 3600) == "2024-02-02"
    assert scheduler._cutoff_day(_ts("2024-02-02 20:00"), 20 * 3600) == "2024-02-03"


def test_dayend_job_only_in_cutoff_mode():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone=datetime.timezone.utc)
    scheduler._apply_dayend_job(sched, {"day_close_mode": "market",
                                        "day_close_cutoff_utc": "00:00"})
    assert sched.get_job("daily_dayend") is None
    scheduler._apply_dayend_job(sched, {"day_close_mode": "cutoff",
                                        "day_close_cutoff_utc": "23:50"})
    trig = str(sched.get_job("daily_dayend").trigger)
    assert "hour='0'" in trig and "minute='5'" in trig  # 23:50 + 15 min wraps
    scheduler._apply_dayend_job(sched, {"day_close_mode": "market",
                                        "day_close_cutoff_utc": "23:50"})
    assert sched.get_job("daily_dayend") is None


def test_settings_validation(client):
    assert client.post("/api/settings", json={"day_close_mode": "x"}).status_code == 400
    assert client.post("/api/settings", json={"day_close_cutoff_utc": "25:00"}).status_code == 400
    with db.db_conn() as c:
        assert db.get_setting(c, "day_close_mode") == "market"
        assert db.get_setting(c, "day_close_cutoff_utc") == "00:00"
