"""Performance analytics: Modified Dietz monthly returns and index series."""
import pytest

from backend import db


@pytest.fixture()
def snapshot_history(tmp_db):
    """Bare portfolio history: one USD-denominated asset, three month-end
    snapshots (100k -> 110k -> 99k). No open holdings, so no live today point."""
    with db.db_conn() as c:
        acc = c.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('SPY', 'ETF', 'etf', 'USD', 1)")
        spy = c.execute("SELECT id FROM assets WHERE ticker='SPY'").fetchone()[0]
        for date, value in [("2024-01-31", 100000), ("2024-02-29", 110000),
                            ("2024-03-31", 99000)]:
            c.execute(
                "INSERT INTO daily_snapshots(date, asset_id, account_id, quantity, "
                "price_native, currency, fx_to_usd, value_usd) "
                "VALUES (?, ?, ?, 1000, ?, 'USD', 1.0, ?)",
                (date, spy, acc, value / 1000.0, value))
    return {"account_id": acc, "spy": spy}


def _month(data, year, month):
    yr = next(y for y in data["years"] if y["year"] == year)
    return yr["months"][month - 1]


def test_monthly_returns_no_flows(client, snapshot_history):
    data = client.get("/api/performance/monthly?ccy=USD").get_json()
    assert _month(data, 2024, 2)["ret"] == pytest.approx(0.10, abs=1e-9)
    assert _month(data, 2024, 3)["ret"] == pytest.approx(-0.10, abs=1e-9)
    assert _month(data, 2024, 3)["pnl"] == pytest.approx(-11000)
    yr = next(y for y in data["years"] if y["year"] == 2024)
    assert yr["year_pnl"] == pytest.approx(99000)  # first month P&L is from 0
    # January has no opening value (denominator 0) -> ret None; Feb is best
    assert _month(data, 2024, 1)["ret"] is None
    assert data["stats"]["best_month"]["month"] == 2


def test_monthly_deposit_excluded_from_return(client, snapshot_history):
    acc = snapshot_history["account_id"]
    with db.db_conn() as c:
        usd = c.execute("SELECT id FROM assets WHERE ticker='USD'").fetchone()[0]
        now = db.now_str()
        c.execute(
            "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
            "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
            "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
            "note, created_at, updated_at) VALUES "
            "('2024-02-15 09:00:00', NULL, 'employer', NULL, 0, ?, '', ?, 5000, "
            "NULL, NULL, 0, 'deposit', ?, ?)", (acc, usd, now, now))
    data = client.get("/api/performance/monthly?ccy=USD").get_json()
    # Modified Dietz: (110000-100000-5000) / (100000 + 5000*(29-15)/29)
    w = (29 - 15) / 29
    expected = 5000 / (100000 + 5000 * w)
    assert _month(data, 2024, 2)["ret"] == pytest.approx(expected, rel=1e-6)
    assert _month(data, 2024, 2)["pnl"] == pytest.approx(5000)


def test_first_month_dietz_starts_at_first_flow(client, tmp_db):
    """Inception month: the Dietz period starts at the first deposit, so a
    small loss isn't amplified into a huge negative return by a tiny base."""
    with db.db_conn() as c:
        acc = c.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        usd = c.execute("SELECT id FROM assets WHERE ticker='USD'").fetchone()[0]
        now = db.now_str()
        c.execute(
            "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
            "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
            "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
            "note, created_at, updated_at) VALUES "
            "('2024-01-25 09:00:00', NULL, 'employer', NULL, 0, ?, '', ?, 100000, "
            "NULL, NULL, 0, 'deposit', ?, ?)", (acc, usd, now, now))
        c.execute(
            "INSERT INTO daily_snapshots(date, asset_id, account_id, quantity, "
            "price_native, currency, fx_to_usd, value_usd) "
            "VALUES ('2024-01-31', ?, ?, 99000, 1.0, 'USD', 1.0, 99000)", (usd, acc))
    data = client.get("/api/performance/monthly?ccy=USD").get_json()
    jan = _month(data, 2024, 1)
    assert jan["pnl"] == pytest.approx(-1000)
    # period = 25th..31st (7 days); deposit on day 1 of the period weighs 6/7
    assert jan["ret"] == pytest.approx(-1000 / (100000 * 6 / 7), rel=1e-6)


def test_monthly_broker_income_counts_as_return(client, snapshot_history):
    acc = snapshot_history["account_id"]
    with db.db_conn() as c:
        usd = c.execute("SELECT id FROM assets WHERE ticker='USD'").fetchone()[0]
        now = db.now_str()
        c.execute(
            "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
            "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
            "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
            "note, created_at, updated_at) VALUES "
            "('2024-02-20 09:00:00', NULL, 'BROKER', NULL, 0, ?, '', ?, 300, "
            "NULL, NULL, 0, 'dividend', ?, ?)", (acc, usd, now, now))
    data = client.get("/api/performance/monthly?ccy=USD").get_json()
    # dividend is performance, not a flow: plain 10% stands
    assert _month(data, 2024, 2)["ret"] == pytest.approx(0.10, abs=1e-9)


def test_indices_listing(client, tmp_db):
    keys = {ix["key"] for ix in client.get("/api/performance/indices").get_json()}
    assert {"SP500", "DOW", "NASDAQ", "NDX", "HSI", "SOX"} <= keys


def test_series_clamps_index_start(client, snapshot_history):
    """Index history predating the first snapshot is cut off at the clamp."""
    with db.db_conn() as c:
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, "
                  "is_yahoo_tracked, quote_type, notes) "
                  "VALUES ('^GSPC', 'S&P 500', 'other', 'USD', 1, 'INDEX', 'benchmark')")
        gspc = c.execute("SELECT id FROM assets WHERE ticker='^GSPC'").fetchone()[0]
        for date, close in [("2023-06-30", 4400), ("2024-01-31", 4800),
                            ("2024-02-29", 5100), ("2024-03-28", 5250)]:
            c.execute("INSERT INTO prices_daily(asset_id, date, open, high, low, close, "
                      "adj_close, volume) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                      (gspc, date, close, close, close, close, close))
    res = client.get("/api/performance/series?indices=SP500&start=2023-01-01")
    assert res.status_code == 200
    data = res.get_json()
    assert data["start"] == "2024-01-31"          # clamped to first snapshot
    assert data["portfolio"]["dates"][0] == "2024-01-31"
    assert data["portfolio"]["values"][0] == pytest.approx(100.0)
    ix = data["indices"][0]
    assert ix["dates"][0] == "2024-01-31"         # 2023 rows dropped
    assert ix["values"][0] == pytest.approx(100.0)
    assert ix["values"][-1] == pytest.approx(100.0 * 5250 / 4800, rel=1e-6)


def test_series_portfolio_is_flow_adjusted(client, snapshot_history):
    """A mid-month deposit must not appear as portfolio growth in the chart."""
    acc = snapshot_history["account_id"]
    with db.db_conn() as c:
        usd = c.execute("SELECT id FROM assets WHERE ticker='USD'").fetchone()[0]
        now = db.now_str()
        c.execute(
            "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
            "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
            "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
            "note, created_at, updated_at) VALUES "
            "('2024-02-15 09:00:00', NULL, 'employer', NULL, 0, ?, '', ?, 5000, "
            "NULL, NULL, 0, 'deposit', ?, ?)", (acc, usd, now, now))
    data = client.get("/api/performance/series").get_json()
    pf = data["portfolio"]
    assert pf["values"][0] == pytest.approx(100.0)
    # Feb: (110000-100000-5000)/(100000+5000); Mar: plain -10%
    feb = pf["values"][pf["dates"].index("2024-02-29")]
    mar = pf["values"][pf["dates"].index("2024-03-31")]
    assert feb == pytest.approx(100 * (1 + 5000 / 105000), rel=1e-6)
    assert mar == pytest.approx(feb * 0.9, rel=1e-6)


def test_series_rejects_unknown_index(client, snapshot_history):
    assert client.get("/api/performance/series?indices=NOPE").status_code == 400


def test_custom_index_configurable(client, snapshot_history):
    res = client.put("/api/settings/indices", json={"indices": [
        {"ticker": "^GSPC", "home": True},
        {"ticker": "000001.ss", "name": "Shanghai", "currency": "CNY", "home": True}]})
    assert res.status_code == 200
    cfg = res.get_json()["configured"]
    assert [b["key"] for b in cfg] == ["SP500", "SSE"]   # preset matched by ticker
    assert cfg[1]["currency"] == "CNY" and cfg[1]["name"] == "Shanghai"
    keys = [ix["key"] for ix in client.get("/api/performance/indices").get_json()]
    assert keys == ["SP500", "SSE"]
    # removed default index is no longer accepted
    assert client.get("/api/performance/series?indices=DOW").status_code == 400
    with db.db_conn() as c:
        sse = c.execute("SELECT id, currency FROM assets WHERE ticker='000001.SS'").fetchone()
        assert sse["currency"] == "CNY"
        c.execute("INSERT INTO prices_daily(asset_id, date, close, adj_close) "
                  "VALUES (?, '2024-01-31', 3000, 3000), (?, '2024-03-28', 3300, 3300)",
                  (sse["id"], sse["id"]))
    data = client.get("/api/performance/series?indices=SSE").get_json()
    assert data["indices"][0]["values"][-1] == pytest.approx(110.0)


def test_custom_index_unknown_symbol_key(client, tmp_db):
    res = client.put("/api/settings/indices", json={"indices": [{"ticker": "^xyz1"}]})
    assert res.status_code == 200
    assert res.get_json()["configured"][0]["key"] == "XYZ1"
    assert res.get_json()["configured"][0]["ticker"] == "^XYZ1"


@pytest.mark.parametrize("body", [
    {"indices": [{"ticker": "bad ticker"}]},
    {"indices": [{"ticker": "^GSPC"}, {"ticker": "^gspc"}]},
    {"indices": [{"ticker": f"T{i}", "home": True} for i in range(9)]},
    {"indices": []},
    {"indices": "SP500"},
])
def test_indices_put_validation(client, tmp_db, body):
    assert client.put("/api/settings/indices", json=body).status_code == 400


def test_removed_index_deactivated(client, tmp_db):
    client.get("/api/dashboard/index-sparks")  # creates ^GSPC/^DJI/^IXIC assets
    res = client.put("/api/settings/indices", json={"indices": [
        {"ticker": "^GSPC", "home": True}, {"ticker": "^HSI", "home": True}]})
    assert res.status_code == 200
    with db.db_conn() as c:
        act = {r["ticker"]: r["active"] for r in c.execute(
            "SELECT ticker, active FROM assets WHERE notes='benchmark'")}
    assert act["^DJI"] == 0 and act["^GSPC"] == 1 and act["^HSI"] == 1
    sparks = client.get("/api/dashboard/index-sparks").get_json()
    assert [s["key"] for s in sparks] == ["SP500", "HSI"]
    # re-adding reactivates
    client.put("/api/settings/indices", json={"indices": [{"ticker": "^DJI"}]})
    with db.db_conn() as c:
        assert c.execute("SELECT active FROM assets WHERE ticker='^DJI'").fetchone()[0] == 1


def test_series_candle_mode(client, snapshot_history):
    """mode=candle adds portfolio OHLC bars whose close sits on the index level."""
    line = client.get("/api/performance/series").get_json()
    assert "ohlc" not in line["portfolio"]
    data = client.get("/api/performance/series?mode=candle").get_json()
    pf = data["portfolio"]
    assert pf["today"] is None  # no live point in candle mode
    assert len(pf["ohlc"]) == len(pf["dates"]) == len(pf["values"])
    for bar, lvl in zip(pf["ohlc"], pf["values"]):
        assert bar[1] == pytest.approx(lvl, rel=1e-4)  # [o,c,l,h] -> close
