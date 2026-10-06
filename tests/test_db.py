"""Schema, seeding and settings round-trips."""
import config
from backend import db


def test_schema_tables_exist(conn):
    tables = db.table_stats(conn)
    for expected in ["settings", "accounts", "currencies", "assets",
                     "derivative_details", "transactions", "manual_prices",
                     "prices_daily", "prices_intraday", "fx_daily", "fx_latest",
                     "latest_quotes", "daily_snapshots", "scrape_log"]:
        assert expected in tables, f"missing table {expected}"
    assert "banks" not in tables  # bank hierarchy removed in v4
    for legacy in db.LEGACY_TABLES:
        assert legacy not in tables


def test_seed_data(conn):
    codes = {r[0] for r in conn.execute("SELECT code FROM currencies")}
    assert {c for c, _ in config.SEED_CURRENCIES} <= codes
    acc = conn.execute("SELECT * FROM accounts").fetchall()
    assert len(acc) == 1
    assert acc[0]["custodian"] == "Default Bank"
    assert acc[0]["custodian_type"] == "bank"
    assert db.account_label(acc[0]) == "Default Bank / Main"
    # every seed currency has a cash asset pegged at ticker == code
    usd = conn.execute("SELECT * FROM assets WHERE ticker = 'USD'").fetchone()
    assert usd["asset_type"] == "cash"
    assert usd["is_yahoo_tracked"] == 0


def test_settings_roundtrip(tmp_db):
    with db.db_conn() as conn:
        assert db.get_setting(conn, "display_currency") == "USD"
        db.set_setting(conn, "display_currency", "HKD")
    with db.db_conn() as conn:
        assert db.get_setting(conn, "display_currency") == "HKD"
        assert db.get_int_setting(conn, "quote_interval_seconds", 0) == 60


def test_init_idempotent(tmp_db):
    db.init_db()  # second run must not raise or duplicate
    with db.db_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1


def test_migrate_drops_legacy_features(tmp_db):
    with db.db_conn() as conn:
        conn.execute("CREATE TABLE strategy_pages (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE playground_strategies (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE playground_legs (id INTEGER PRIMARY KEY, strategy_id "
                     "INTEGER REFERENCES playground_strategies(id))")
        conn.execute("ALTER TABLE accounts ADD COLUMN is_margin INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE derivative_details ADD COLUMN implied_vol REAL")
        conn.execute("ALTER TABLE derivative_details ADD COLUMN initial_margin "
                     "REAL NOT NULL DEFAULT 0")
        db.set_setting(conn, "risk_free_rate", "0.04")
    db.init_db()
    with db.db_conn() as conn:
        tables = db.table_stats(conn)
        for legacy in db.LEGACY_TABLES:
            assert legacy not in tables
        assert "is_margin" not in {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
        deriv_cols = {r[1] for r in conn.execute("PRAGMA table_info(derivative_details)")}
        assert not deriv_cols & {"implied_vol", "initial_margin", "maintenance_margin"}
        assert db.get_setting(conn, "risk_free_rate") is None
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
