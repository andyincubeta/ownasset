"""SQLite layer: schema, connection helpers, seeding, settings access.

All data lives in one file: .db/asset_dashboard.db (WAL mode).
Market data tables (prices_daily, prices_intraday, fx_*) are kept separate
from app tables so intraday bars can be purged independently.

Accounts are first-class parties: each has a custodian (bank, broker, exchange,
crypto wallet, ...) — there is no bank-as-parent hierarchy. A transaction moves
value between an OUTFLOW party and an INFLOW party; each party is either one of
the user's accounts or an external free-text label. Both parties may be the
same account (e.g. pay USD, receive stock).
"""
import datetime
import json
import os
import sqlite3
from contextlib import contextmanager

import config
from backend.logger import get_logger

log = get_logger("dashboard.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    custodian_type TEXT NOT NULL DEFAULT 'bank',   -- bank|broker|exchange|wallet|other
    custodian      TEXT NOT NULL DEFAULT '',       -- institution / wallet label
    base_currency  TEXT NOT NULL DEFAULT 'USD',
    note           TEXT NOT NULL DEFAULT '',
    UNIQUE (custodian, name)
);

CREATE TABLE IF NOT EXISTS currencies (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS assets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL DEFAULT '',
    asset_type      TEXT NOT NULL DEFAULT 'equity',
    currency        TEXT NOT NULL DEFAULT 'USD',
    is_yahoo_tracked INTEGER NOT NULL DEFAULT 1,
    price_provider  TEXT NOT NULL DEFAULT 'yahoo',  -- yahoo | option_archive
    industry        TEXT NOT NULL DEFAULT '',
    sector          TEXT NOT NULL DEFAULT '',
    country         TEXT NOT NULL DEFAULT '',
    quote_type      TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    notes           TEXT NOT NULL DEFAULT '',
    info_json       TEXT NOT NULL DEFAULT '{}',
    info_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS derivative_details (
    asset_id           INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    underlying_ticker  TEXT NOT NULL DEFAULT '',
    contract_type      TEXT NOT NULL DEFAULT 'call',
    strike             REAL,
    expiry             TEXT,
    multiplier         REAL NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS txn_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    group_type TEXT NOT NULL DEFAULT 'custom',   -- pair | option_strategy | hedge | custom
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_date           TEXT NOT NULL,               -- 'YYYY-MM-DD HH:MM:SS'
    outflow_account_id INTEGER REFERENCES accounts(id),
    outflow_party      TEXT NOT NULL DEFAULT '',    -- free text when external
    outflow_asset_id   INTEGER REFERENCES assets(id),
    outflow_amount     REAL NOT NULL DEFAULT 0,
    inflow_account_id  INTEGER REFERENCES accounts(id),
    inflow_party       TEXT NOT NULL DEFAULT '',
    inflow_asset_id    INTEGER REFERENCES assets(id),
    inflow_amount      REAL NOT NULL DEFAULT 0,
    fee_account_id     INTEGER REFERENCES accounts(id),
    fee_asset_id       INTEGER REFERENCES assets(id),
    fee_amount         REAL NOT NULL DEFAULT 0,
    note               TEXT NOT NULL DEFAULT '',
    group_id           INTEGER REFERENCES txn_groups(id),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_txn_out_acc ON transactions(outflow_account_id);
CREATE INDEX IF NOT EXISTS idx_txn_in_acc ON transactions(inflow_account_id);

-- one row per committed CSV import; file kept so the import can be re-run
CREATE TABLE IF NOT EXISTS import_batches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at         TEXT NOT NULL,
    provider           TEXT NOT NULL,
    account_id         INTEGER NOT NULL REFERENCES accounts(id),
    file_name          TEXT NOT NULL DEFAULT '',
    file_content       BLOB,
    ticker_overrides   TEXT NOT NULL DEFAULT '{}',
    include_duplicates INTEGER NOT NULL DEFAULT 0,
    inserted_count     INTEGER NOT NULL DEFAULT 0,
    reverted_at        TEXT
);

CREATE TABLE IF NOT EXISTS import_batch_txns (
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    txn_id   INTEGER NOT NULL,
    PRIMARY KEY (batch_id, txn_id)
);

-- pre-change copy of a txn's dedupe fields (kept on split/edit) so re-importing
-- the original file still recognizes the row as already present
CREATE TABLE IF NOT EXISTS txn_fingerprints (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id             INTEGER NOT NULL,
    txn_date           TEXT NOT NULL,
    outflow_account_id INTEGER,
    outflow_party      TEXT NOT NULL DEFAULT '',
    outflow_asset_id   INTEGER,
    outflow_amount     REAL NOT NULL DEFAULT 0,
    inflow_account_id  INTEGER,
    inflow_party       TEXT NOT NULL DEFAULT '',
    inflow_asset_id    INTEGER,
    inflow_amount      REAL NOT NULL DEFAULT 0,
    fee_account_id     INTEGER,
    fee_asset_id       INTEGER,
    fee_amount         REAL NOT NULL DEFAULT 0,
    note               TEXT NOT NULL DEFAULT '',
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_fp_txn ON txn_fingerprints(txn_id);
CREATE INDEX IF NOT EXISTS idx_txn_fp_date ON txn_fingerprints(txn_date);

CREATE TABLE IF NOT EXISTS manual_prices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id   INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    price_date TEXT NOT NULL,
    price      REAL NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    UNIQUE (asset_id, price_date)
);

-- market data: permanent daily record (full history from earliest available)
CREATE TABLE IF NOT EXISTS prices_daily (
    asset_id  INTEGER NOT NULL,
    date      TEXT NOT NULL,
    open      REAL, high REAL, low REAL, close REAL,
    adj_close REAL, volume REAL,
    ext_close REAL,               -- last pre/regular/post price before day_close_cutoff_utc
    ext_close_ts INTEGER,
    PRIMARY KEY (asset_id, date)
);

-- market data: non-daily-close bars, kept separate so they can be purged
CREATE TABLE IF NOT EXISTS prices_intraday (
    asset_id     INTEGER NOT NULL,
    bar_interval TEXT NOT NULL,
    ts           INTEGER NOT NULL,
    open         REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (asset_id, bar_interval, ts)
);

CREATE TABLE IF NOT EXISTS fx_daily (
    pair TEXT NOT NULL,
    date TEXT NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (pair, date)
);

CREATE TABLE IF NOT EXISTS fx_latest (
    pair TEXT PRIMARY KEY,
    rate REAL NOT NULL,
    ts   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS latest_quotes (
    asset_id   INTEGER PRIMARY KEY,
    price      REAL,
    prev_close REAL,
    ts         INTEGER,
    source     TEXT NOT NULL DEFAULT '',
    session_date TEXT             -- exchange-local date of the quoted price
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    date         TEXT NOT NULL,
    asset_id     INTEGER NOT NULL,
    account_id   INTEGER NOT NULL,
    quantity     REAL NOT NULL,
    price_native REAL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    fx_to_usd    REAL,
    value_usd    REAL,
    PRIMARY KEY (date, asset_id, account_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_date ON daily_snapshots(date);

CREATE TABLE IF NOT EXISTS scrape_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    job         TEXT NOT NULL,
    ticker      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL,               -- success | error | skipped
    duration_ms REAL NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_scrape_ts ON scrape_log(ts);
CREATE INDEX IF NOT EXISTS idx_scrape_job ON scrape_log(job);
"""

# retired features: dropped from existing databases by _migrate
LEGACY_TABLES = ("strategy_panel_history", "strategy_panels", "strategy_pages",
                 "playground_legs", "playground_strategies")
LEGACY_COLUMNS = (("accounts", "is_margin"), ("derivative_details", "implied_vol"),
                  ("derivative_details", "initial_margin"),
                  ("derivative_details", "maintenance_margin"))
LEGACY_SETTINGS = ("risk_free_rate",)



def now_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def now_ms_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def today_str() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_conn(db_path: str | None = None):
    """Context manager: yields a connection, commits on success, rolls back on error."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | None = None):
    """Create schema (idempotent), enable WAL, seed defaults."""
    config.ensure_dirs()
    path = db_path or config.DB_PATH
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _seed(conn)
        conn.commit()
        log.info("database initialized at %s", os.path.abspath(path))
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection):
    """Bring databases created by older versions up to the current schema."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(assets)")}
    if "price_provider" not in cols:
        conn.execute("ALTER TABLE assets ADD COLUMN price_provider "
                     "TEXT NOT NULL DEFAULT 'yahoo'")
        log.info("migration: added assets.price_provider")
    txn_cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
    if "group_id" not in txn_cols:
        conn.execute("ALTER TABLE transactions ADD COLUMN group_id "
                     "INTEGER REFERENCES txn_groups(id)")
        log.info("migration: added transactions.group_id")
    grp_cols = {r[1] for r in conn.execute("PRAGMA table_info(txn_groups)")}
    if "group_type" not in grp_cols:
        conn.execute("ALTER TABLE txn_groups ADD COLUMN group_type "
                     "TEXT NOT NULL DEFAULT 'custom'")
        log.info("migration: added txn_groups.group_type")
    pd_cols = {r[1] for r in conn.execute("PRAGMA table_info(prices_daily)")}
    if "ext_close" not in pd_cols:
        conn.execute("ALTER TABLE prices_daily ADD COLUMN ext_close REAL")
        conn.execute("ALTER TABLE prices_daily ADD COLUMN ext_close_ts INTEGER")
        log.info("migration: added prices_daily.ext_close/ext_close_ts")
    lq_cols = {r[1] for r in conn.execute("PRAGMA table_info(latest_quotes)")}
    if "session_date" not in lq_cols:
        conn.execute("ALTER TABLE latest_quotes ADD COLUMN session_date TEXT")
        log.info("migration: added latest_quotes.session_date")
    for table in LEGACY_TABLES:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,)).fetchone():
            conn.execute(f"DROP TABLE {table}")
            log.info("migration: dropped legacy table %s", table)
    for table, col in LEGACY_COLUMNS:
        if col in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            log.info("migration: dropped legacy column %s.%s", table, col)
    for key in LEGACY_SETTINGS:
        if conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount:
            log.info("migration: removed legacy setting %s", key)


def _seed(conn: sqlite3.Connection):
    for key, value in config.DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))
    for code, name in config.SEED_CURRENCIES:
        conn.execute("INSERT OR IGNORE INTO currencies(code, name) VALUES (?, ?)", (code, name))
        ensure_cash_asset(conn, code, name)
    if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0:
        conn.execute(
            "INSERT INTO accounts(name, custodian_type, custodian, base_currency) "
            "VALUES ('Main', 'bank', 'Default Bank', 'USD')")


def ensure_cash_asset(conn: sqlite3.Connection, code: str, name: str = ""):
    """Each currency gets a cash pseudo-asset (ticker == code, price == 1 native)."""
    conn.execute(
        """INSERT OR IGNORE INTO assets
           (ticker, name, asset_type, currency, is_yahoo_tracked, quote_type)
           VALUES (?, ?, 'cash', ?, 0, 'CASH')""",
        (code.upper(), (name or code) + " Cash", code.upper()))


def account_label(row) -> str:
    """Human label for an account row: 'Custodian / Name' or just 'Name'."""
    custodian = row["custodian"] if row["custodian"] else ""
    return f"{custodian} / {row['name']}" if custodian else row["name"]


# ---------------------------------------------------------------- settings --

def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default if default is not None else config.DEFAULT_SETTINGS.get(key)
    return row["value"]


def set_setting(conn: sqlite3.Connection, key: str, value: str):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def get_all_settings(conn: sqlite3.Connection) -> dict:
    merged = dict(config.DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        merged[row["key"]] = row["value"]
    return merged


def get_float_setting(conn, key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(conn, key))
    except (TypeError, ValueError):
        return default


def get_int_setting(conn, key: str, default: int = 0) -> int:
    try:
        return int(float(get_setting(conn, key)))
    except (TypeError, ValueError):
        return default


def daily_close_sql(conn, alias: str = "") -> str:
    """SQL expression for the prices_daily close honouring day_close_mode."""
    p = f"{alias}." if alias else ""
    if get_setting(conn, "day_close_mode") == "cutoff":
        return f"COALESCE({p}ext_close, {p}close)"
    return f"{p}close"


def cutoff_seconds(conn) -> int:
    """day_close_cutoff_utc ('HH:MM') as seconds after 00:00 UTC."""
    try:
        hh, mm = str(get_setting(conn, "day_close_cutoff_utc")).strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh < 24 and 0 <= mm < 60:
            return hh * 3600 + mm * 60
    except (TypeError, ValueError):
        pass
    return 0


# ------------------------------------------------------------------- misc --

def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def table_stats(conn: sqlite3.Connection) -> dict:
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    stats = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    return stats


def db_file_size() -> int:
    try:
        return os.path.getsize(config.DB_PATH)
    except OSError:
        return 0


def parse_json(text: str, fallback):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback
