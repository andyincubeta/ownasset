"""Shared pytest fixtures: isolated temp DB, Flask test client, sample data,
and a fully mocked Yahoo client (tests never hit the network)."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from backend import db  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Fresh initialized database in a temp folder."""
    db_dir = tmp_path / ".db"
    db_dir.mkdir()
    path = str(db_dir / "test.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_DIR", str(db_dir))
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    db.init_db()
    return path


@pytest.fixture()
def conn(tmp_db):
    c = db.connect(tmp_db)
    yield c
    c.close()


@pytest.fixture()
def app(tmp_db):
    from app import create_app
    application = create_app(start_scheduler=False)
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def sample_portfolio(tmp_db):
    """Default account, AAPL + HKD cash + manual real estate, party-style
    transactions (with times), prices, fx. NAV in USD = 173,400."""
    with db.db_conn() as c:
        acc_id = c.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
        c.execute(
            "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked, "
            "industry, sector, country) VALUES ('AAPL', 'Apple Inc', 'equity', 'USD', 1, "
            "'Consumer Electronics', 'Technology', 'United States')")
        aapl = c.execute("SELECT id FROM assets WHERE ticker='AAPL'").fetchone()[0]
        usd = c.execute("SELECT id FROM assets WHERE ticker='USD'").fetchone()[0]
        hkd = c.execute("SELECT id FROM assets WHERE ticker='HKD'").fetchone()[0]
        c.execute(
            "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
            "VALUES ('RE:HOME', 'Flat 12F', 'real_estate', 'HKD', 0)")
        home = c.execute("SELECT id FROM assets WHERE ticker='RE:HOME'").fetchone()[0]

        now = db.now_str()
        txns = [
            # (date, out_acc, out_party, out_asset, out_amt, in_acc, in_party, in_asset, in_amt)
            # deposit 100k USD from employer (external outflow party)
            ("2024-01-02 09:00:00", None, "employer", None, 0, acc_id, "", usd, 100000),
            # buy 100 AAPL @150 inside the SAME account: -15000 USD +100 AAPL
            ("2024-01-10 10:30:00", acc_id, "", usd, 15000, acc_id, "", aapl, 100),
            # deposit 500k HKD
            ("2024-01-15 09:00:00", None, "external", None, 0, acc_id, "", hkd, 500000),
            # buy home for 400k HKD (same account both sides)
            ("2024-02-01 14:00:00", acc_id, "", hkd, 400000, acc_id, "", home, 1),
        ]
        for t in txns:
            c.execute(
                "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
                "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
                "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
                "note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, '', ?, ?)",
                t + (now, now))

        c.execute("INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source) "
                  "VALUES (?, 180.0, 178.0, 1700000000, 'test')", (aapl,))
        c.execute("INSERT INTO prices_daily(asset_id, date, open, high, low, close, "
                  "adj_close, volume) VALUES (?, '2024-02-02', 179, 181, 178, 180, 180, 1e6)",
                  (aapl,))
        c.execute("INSERT INTO manual_prices(asset_id, price_date, price, note) "
                  "VALUES (?, '2024-02-01', 450000, 'valuation')", (home,))
        c.execute("INSERT INTO fx_latest(pair, rate, ts) VALUES ('HKDUSD', 0.128, ?)",
                  (int(time.time()),))
        c.execute("INSERT INTO fx_daily(pair, date, rate) VALUES ('HKDUSD', '2024-02-02', 0.128)")
        return {"account_id": acc_id, "aapl": aapl, "usd": usd, "hkd": hkd, "home": home}
