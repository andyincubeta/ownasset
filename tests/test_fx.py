"""FX conversion chains."""
import datetime
import time

from backend import db, fx


def test_usd_identity(conn):
    rate, found = fx.rate_to_usd(conn, "USD")
    assert rate == 1.0 and found


def test_rate_lookup_order(tmp_db):
    with db.db_conn() as c:
        c.execute("INSERT INTO fx_daily(pair, date, rate) VALUES ('HKDUSD', '2024-01-01', 0.127)")
        c.execute("INSERT INTO fx_latest(pair, rate, ts) VALUES ('HKDUSD', 0.128, ?)",
                  (int(time.time()),))
    with db.db_conn() as c:
        rate, found = fx.rate_to_usd(c, "HKD")
        assert rate == 0.128 and found  # latest wins without a date
        rate, found = fx.rate_to_usd(c, "HKD", on_date="2024-06-01")
        assert rate == 0.127 and found  # daily wins with a date
        rate, found = fx.rate_to_usd(c, "NOK")
        assert rate == 1.0 and not found  # unknown -> flagged fallback


def test_stale_latest_tick_yields_to_daily_close(tmp_db):
    """Weekend: Friday's last tick is superseded by Friday's stored daily close."""
    fri_ts = int(datetime.datetime(2026, 9, 30, 22, tzinfo=datetime.timezone.utc).timestamp())
    with db.db_conn() as c:
        c.execute("INSERT INTO fx_daily(pair, date, rate) VALUES ('HKDUSD', '2026-09-30', 0.127)")
        c.execute("INSERT INTO fx_latest(pair, rate, ts) VALUES ('HKDUSD', 0.129, ?)", (fri_ts,))
        assert fx.rate_to_usd(c, "HKD") == (0.127, True)
        # tick newer than any stored bar (e.g. Sunday-evening reopen) stays live
        c.execute("DELETE FROM fx_daily")
        c.execute("INSERT INTO fx_daily(pair, date, rate) VALUES ('HKDUSD', '2026-09-29', 0.127)")
        assert fx.rate_to_usd(c, "HKD") == (0.129, True)


def test_cross_conversion(tmp_db):
    with db.db_conn() as c:
        c.execute("INSERT INTO fx_latest(pair, rate, ts) VALUES ('HKDUSD', 0.128, 1)")
        c.execute("INSERT INTO fx_latest(pair, rate, ts) VALUES ('EURUSD', 1.10, 1)")
        amount, ok = fx.convert(c, 1000, "HKD", "EUR")
        assert ok
        assert abs(amount - 1000 * 0.128 / 1.10) < 1e-9


def test_usd_to_display(tmp_db):
    with db.db_conn() as c:
        c.execute("INSERT INTO fx_latest(pair, rate, ts) VALUES ('HKDUSD', 0.125, 1)")
        mult, ok = fx.usd_to_display(c, "HKD")
        assert ok and abs(mult - 8.0) < 1e-9


def test_needed_fx_tickers(tmp_db):
    with db.db_conn() as c:
        c.execute("INSERT INTO assets(ticker, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('0005.HK', 'equity', 'HKD', 1)")
        tickers = fx.needed_fx_tickers(c)
        assert "HKDUSD=X" in tickers
        assert all(not t.startswith("USDUSD") for t in tickers)
