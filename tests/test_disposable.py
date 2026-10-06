"""Disposable income entries: CRUD via /api/disposable and exclusion of the
DISPOSABLE INCOME party from the investment return math."""
import pytest

from backend import db


def _refs(client):
    acc = client.get("/api/accounts").get_json()[0]["id"]
    assets = client.get("/api/assets").get_json()
    usd = next(a["id"] for a in assets if a["ticker"] == "USD")
    return acc, usd, assets


def test_crud_and_snapshot_resync(client, tmp_db):
    acc, usd, _ = _refs(client)
    today = db.today_str()

    res = client.post("/api/disposable", json={
        "txn_date": today, "account_id": acc, "asset_id": usd,
        "amount": 1234.5, "note": "salary savings"})
    assert res.status_code == 201
    entry_id = res.get_json()["id"]

    data = client.get("/api/disposable").get_json()
    assert data["display_currency"] == "USD"
    assert data["total_display"] == pytest.approx(1234.5)
    e = data["entries"][0]
    assert (e["id"], e["amount"], e["note"]) == (entry_id, 1234.5, "salary savings")

    with db.db_conn() as c:
        txn = c.execute("SELECT * FROM transactions WHERE id = ?", (entry_id,)).fetchone()
        assert txn["outflow_party"] == "DISPOSABLE INCOME"
        assert txn["outflow_account_id"] is None
        assert txn["inflow_account_id"] == acc
        snap = c.execute("SELECT value_usd FROM daily_snapshots WHERE date = ? "
                         "AND asset_id = ?", (today, usd)).fetchone()
        assert snap is not None and snap["value_usd"] == pytest.approx(1234.5)

    assert client.put(f"/api/disposable/{entry_id}",
                      json={"amount": 2000}).status_code == 200
    data = client.get("/api/disposable").get_json()
    assert data["entries"][0]["amount"] == pytest.approx(2000)
    with db.db_conn() as c:
        snap = c.execute("SELECT value_usd FROM daily_snapshots WHERE date = ? "
                         "AND asset_id = ?", (today, usd)).fetchone()
        assert snap["value_usd"] == pytest.approx(2000)

    assert client.delete(f"/api/disposable/{entry_id}").status_code == 200
    assert client.get("/api/disposable").get_json()["entries"] == []
    with db.db_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM daily_snapshots WHERE date = ?",
                         (today,)).fetchone()[0] == 0


def test_validation(client, tmp_db):
    acc, usd, assets = _refs(client)
    bad = [
        {"account_id": acc, "asset_id": usd, "amount": 0},
        {"account_id": acc, "asset_id": usd, "amount": -5},
        {"account_id": 999999, "asset_id": usd, "amount": 10},
        {"account_id": acc, "asset_id": 999999, "amount": 10},
    ]
    for body in bad:
        assert client.post("/api/disposable", json=body).status_code == 400
    # non-cash assets are rejected
    with db.db_conn() as c:
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('AAPL', 'Apple', 'equity', 'USD', 1)")
        aapl = c.execute("SELECT id FROM assets WHERE ticker='AAPL'").fetchone()[0]
    res = client.post("/api/disposable", json={
        "account_id": acc, "asset_id": aapl, "amount": 10})
    assert res.status_code == 400
    assert "cash" in res.get_json()["error"]
    # ordinary transactions are not editable through this API
    with db.db_conn() as c:
        now = db.now_str()
        c.execute("INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
                  "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
                  "inflow_asset_id, inflow_amount, fee_amount, note, created_at, updated_at) "
                  "VALUES ('2024-01-02 09:00:00', NULL, 'employer', NULL, 0, ?, '', ?, 100, "
                  "0, '', ?, ?)", (acc, usd, now, now))
        other = c.execute("SELECT MAX(id) FROM transactions").fetchone()[0]
    assert client.delete(f"/api/disposable/{other}").status_code == 404
    assert client.put(f"/api/disposable/{other}", json={"amount": 1}).status_code == 404


def test_disposable_income_excluded_from_return(client, tmp_db):
    """Disposable income raises portfolio value but not the return index."""
    with db.db_conn() as c:
        acc = c.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        usd = c.execute("SELECT id FROM assets WHERE ticker='USD'").fetchone()[0]
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('SPY', 'ETF', 'etf', 'USD', 1)")
        spy = c.execute("SELECT id FROM assets WHERE ticker='SPY'").fetchone()[0]
        for date, value in [("2024-01-31", 100000), ("2024-02-29", 110000)]:
            c.execute("INSERT INTO daily_snapshots(date, asset_id, account_id, quantity, "
                      "price_native, currency, fx_to_usd, value_usd) "
                      "VALUES (?, ?, ?, 1000, ?, 'USD', 1.0, ?)",
                      (date, spy, acc, value / 1000.0, value))
        now = db.now_str()
        c.execute("INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
                  "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
                  "inflow_asset_id, inflow_amount, fee_amount, note, created_at, updated_at) "
                  "VALUES ('2024-02-15 09:00:00', NULL, 'DISPOSABLE INCOME', ?, 5000, ?, '', "
                  "?, 5000, 0, 'salary', ?, ?)", (usd, acc, usd, now, now))
    data = client.get("/api/performance/monthly?ccy=USD").get_json()
    months = next(y for y in data["years"] if y["year"] == 2024)["months"]
    w = (29 - 15) / 29
    expected = 5000 / (100000 + 5000 * w)  # Modified Dietz with the flow stripped
    assert months[1]["ret"] == pytest.approx(expected, rel=1e-6)
    assert months[1]["pnl"] == pytest.approx(5000)  # 110000 - 100000 - 5000
