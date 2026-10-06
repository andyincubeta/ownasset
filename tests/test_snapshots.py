"""Snapshots: take, rebuild, daily-values matrix."""
import pytest

from backend import db, snapshots


def test_take_snapshot(sample_portfolio):
    with db.db_conn() as c:
        n = snapshots.take_snapshot(c, date="2024-02-02")
        assert n == 4
        total = c.execute(
            "SELECT SUM(value_usd) FROM daily_snapshots WHERE date='2024-02-02'"
        ).fetchone()[0]
        # 85000 + 18000 (AAPL@180) + 12800 (HKD) + 57600 (home) = 173400
        assert abs(total - 173400) < 0.01


def test_rebuild_snapshots(sample_portfolio):
    with db.db_conn() as c:
        n = snapshots.rebuild_snapshots(c, start="2024-01-01", end="2024-02-02")
        assert n > 0
        # before first txn nothing exists
        first = c.execute("SELECT MIN(date) FROM daily_snapshots").fetchone()[0]
        assert first == "2024-01-02"
        # day after AAPL buy the position exists at 100 shares
        qty = c.execute(
            "SELECT quantity FROM daily_snapshots WHERE date='2024-01-10' AND asset_id=?",
            (sample_portfolio["aapl"],)).fetchone()[0]
        assert qty == 100


def test_rebuild_snapshots_accepts_datetime_bounds(sample_portfolio):
    with db.db_conn() as c:
        n = snapshots.rebuild_snapshots(c, start="2024-01-01 12:34:56",
                                        end="2024-02-02T18:00:00")
        assert n > 0
        first = c.execute("SELECT MIN(date) FROM daily_snapshots").fetchone()[0]
        assert first == "2024-01-02"


def test_full_rebuild_purges_rows_outside_ledger(sample_portfolio):
    """Stale snapshots predating the first txn (e.g. from a deleted manual
    price) must disappear on a full rebuild."""
    with db.db_conn() as c:
        c.execute("INSERT INTO daily_snapshots(date, asset_id, account_id, quantity, "
                  "price_native, currency, fx_to_usd, value_usd) "
                  "VALUES ('2023-06-03', ?, 1, 1, 999, 'USD', 1, 999)",
                  (sample_portfolio["aapl"],))
        n = snapshots.rebuild_snapshots(c)  # no bounds = full rebuild
        assert n > 0
        first = c.execute("SELECT MIN(date) FROM daily_snapshots").fetchone()[0]
        assert first == "2024-01-02"
        # explicit-range rebuild keeps rows outside the range (partial resync)
        c.execute("INSERT INTO daily_snapshots(date, asset_id, account_id, quantity, "
                  "price_native, currency, fx_to_usd, value_usd) "
                  "VALUES ('2023-06-03', ?, 1, 1, 999, 'USD', 1, 999)",
                  (sample_portfolio["aapl"],))
        snapshots.rebuild_snapshots(c, start="2024-01-02", end="2024-01-05")
        assert c.execute("SELECT COUNT(*) FROM daily_snapshots WHERE date='2023-06-03'") \
            .fetchone()[0] == 1


def test_full_rebuild_wipes_when_no_transactions(tmp_db):
    with db.db_conn() as c:
        c.execute("INSERT INTO daily_snapshots(date, asset_id, account_id, quantity, "
                  "price_native, currency, fx_to_usd, value_usd) "
                  "VALUES ('2023-06-03', 1, 1, 1, 999, 'USD', 1, 999)")
        assert snapshots.rebuild_snapshots(c) == 0
        assert c.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0] == 0


def test_rebuild_snapshots_invalid_bounds(sample_portfolio):
    with db.db_conn() as c:
        with pytest.raises(ValueError, match="invalid start/end date"):
            snapshots.rebuild_snapshots(c, start="bad-date", end="2024-02-02")
        with pytest.raises(ValueError, match="invalid start/end date"):
            snapshots.rebuild_snapshots(c, start="2024-02-03", end="2024-02-02")


def test_daily_values_matrix_modes(sample_portfolio):
    with db.db_conn() as c:
        snapshots.take_snapshot(c, date="2024-02-02")
        m = snapshots.daily_values_matrix(c, mode="value", display_cur="USD")
        assert m["rows"][0]["date"] == "2024-02-02"
        assert abs(m["rows"][0]["cells"][0] - 173400) < 0.01  # TOTAL first
        assert len(m["columns"]) == 4
        q = snapshots.daily_values_matrix(c, mode="quantity", display_cur="USD")
        aapl_idx = next(i for i, col in enumerate(q["columns"]) if col["ticker"] == "AAPL")
        assert q["rows"][0]["cells"][aapl_idx + 1] == 100  # +1: TOTAL occupies col 0


def test_daily_values_matrix_include_today(sample_portfolio):
    with db.db_conn() as c:
        snapshots.take_snapshot(c, date="2024-02-02")
        m = snapshots.daily_values_matrix(c, mode="value", display_cur="USD",
                                          include_today=True)
        top = m["rows"][0]
        assert top["date"] == db.today_str() and top["provisional"] is True
        assert abs(top["cells"][0] - 173400) < 0.01
        # frozen history intact below the live row, without the provisional flag
        assert m["rows"][1]["date"] == "2024-02-02"
        assert "provisional" not in m["rows"][1]
        # default (CSV / legacy callers): frozen rows only
        plain = snapshots.daily_values_matrix(c, mode="value", display_cur="USD")
        assert plain["rows"][0]["date"] == "2024-02-02"


def test_daily_values_matrix_today_follows_price_change(sample_portfolio):
    with db.db_conn() as c:
        snapshots.take_snapshot(c)  # frozen row for today at AAPL@180
        c.execute("UPDATE latest_quotes SET price = 200 WHERE asset_id = ?",
                  (sample_portfolio["aapl"],))
        m = snapshots.daily_values_matrix(c, include_today=True)
        todays = [r for r in m["rows"] if r["date"] == db.today_str()]
        assert len(todays) == 1 and todays[0].get("provisional") is True
        # 173400 + 100 x (200 - 180) = 175400, despite the stale frozen row
        assert abs(todays[0]["cells"][0] - 175400) < 0.01


def test_display_currency_cash_stays_at_face_value(sample_portfolio):
    """180 HKD must show as 180 when the display currency is HKD — no USD
    round-trip drift, even when live and daily FX rates diverge."""
    with db.db_conn() as c:
        # live rate (0.13) deliberately differs from the daily close (0.128)
        c.execute("UPDATE fx_latest SET rate = 0.13 WHERE pair = 'HKDUSD'")
        snapshots.take_snapshot(c, date="2024-02-02")  # stores fx_to_usd = 0.13
        m = snapshots.daily_values_matrix(c, mode="value", display_cur="HKD",
                                          include_today=True)
        idx = {col["ticker"]: i + 1 for i, col in enumerate(m["columns"])}
        live, frozen = m["rows"][0], m["rows"][1]
        assert live["provisional"] is True and frozen["date"] == "2024-02-02"
        for row in (live, frozen):
            assert row["cells"][idx["HKD"]] == 100000.0      # face value, always
            assert row["cells"][idx["RE:HOME"]] == 450000.0  # HKD-denominated too
        # USD parts still convert at each row's own rate (daily vs live)
        assert abs(frozen["cells"][0] - (550000 + 103000 / 0.128)) < 0.01
        assert abs(live["cells"][0] - (550000 + 103000 / 0.13)) < 0.01


def test_catchup_snapshots_backfills_gap(sample_portfolio, monkeypatch):
    import datetime

    from backend import scheduler

    with db.db_conn() as c:
        snapshots.rebuild_snapshots(c, start="2024-01-02", end="2024-01-03")
    monkeypatch.setattr(scheduler, "job_daily", lambda: "skipped in test")
    msg = scheduler.catchup_snapshots()
    assert msg.startswith("backfilled 2024-01-04")
    # catchup targets UTC-yesterday (matches production logic, not local date)
    yesterday = (datetime.datetime.now(datetime.timezone.utc).date()
                 - datetime.timedelta(days=1)).isoformat()
    with db.db_conn() as c:
        assert c.execute("SELECT MAX(date) FROM daily_snapshots").fetchone()[0] == yesterday
    assert scheduler.catchup_snapshots() == "up to date"


def test_asset_prices_matrix_option_expiry(sample_portfolio):
    """Option columns carry expiry: from derivative details or the OCC ticker."""
    acc, usd = sample_portfolio["account_id"], sample_portfolio["usd"]
    with db.db_conn() as c:
        now = db.now_str()
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('AAPL240119C00150000', 'AAPL 150 Call', 'option', 'USD', 1)")
        occ = c.execute("SELECT id FROM assets WHERE ticker='AAPL240119C00150000'").fetchone()[0]
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('MYOPT', 'Custom Option', 'option', 'USD', 0)")
        myopt = c.execute("SELECT id FROM assets WHERE ticker='MYOPT'").fetchone()[0]
        c.execute("INSERT INTO derivative_details(asset_id, multiplier, expiry) "
                  "VALUES (?, 100, '2024-06-21')", (myopt,))
        for aid in (occ, myopt):
            c.execute(
                "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
                "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
                "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
                "note, created_at, updated_at) VALUES ('2024-01-05 10:00:00', ?, '', ?, 500, "
                "?, '', ?, 1, NULL, NULL, 0, '', ?, ?)", (acc, usd, acc, aid, now, now))
        data = snapshots.asset_prices_matrix(c, include_today=False)
    cols = {col["ticker"]: col for col in data["columns"]}
    assert cols["AAPL240119C00150000"]["expiry"] == "2024-01-19"  # parsed from OCC
    assert cols["MYOPT"]["expiry"] == "2024-06-21"                # details win
    assert cols["AAPL"]["expiry"] is None
