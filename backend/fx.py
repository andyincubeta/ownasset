"""FX conversion layer.

Convention: every rate is stored as pair '{CUR}USD' = value of 1 CUR in USD.
Yahoo ticker for that pair is '{CUR}USD=X'. USD itself is always 1.0.
Conversions route through USD: native -> USD -> display currency.
"""
import datetime
import sqlite3

from backend.logger import get_logger

log = get_logger("dashboard.fx")


def fx_pair(currency: str) -> str:
    return f"{currency.upper()}USD"


def fx_yahoo_ticker(currency: str) -> str:
    return f"{currency.upper()}USD=X"


def pair_to_currency(pair: str) -> str:
    return pair[:-3] if pair.endswith("USD") else pair


def active_currencies(conn: sqlite3.Connection) -> set[str]:
    """All currencies referenced by active assets, accounts, and display setting."""
    # benchmark indices are only ever rebased, never converted
    curs = {r[0] for r in conn.execute(
        "SELECT DISTINCT currency FROM assets WHERE active = 1 "
        "AND COALESCE(notes, '') != 'benchmark'")}
    curs |= {r[0] for r in conn.execute("SELECT DISTINCT base_currency FROM accounts")}
    row = conn.execute("SELECT value FROM settings WHERE key='display_currency'").fetchone()
    if row:
        curs.add(row[0])
    return {c.upper() for c in curs if c}


def needed_fx_tickers(conn: sqlite3.Connection) -> list[str]:
    return [fx_yahoo_ticker(c) for c in sorted(active_currencies(conn)) if c != "USD"]


def store_latest(conn: sqlite3.Connection, pair: str, rate: float, ts: int):
    conn.execute(
        "INSERT INTO fx_latest(pair, rate, ts) VALUES (?, ?, ?) "
        "ON CONFLICT(pair) DO UPDATE SET rate = excluded.rate, ts = excluded.ts",
        (pair, rate, ts))


def store_daily(conn: sqlite3.Connection, pair: str, rows: list[tuple[str, float]]):
    conn.executemany(
        "INSERT INTO fx_daily(pair, date, rate) VALUES (?, ?, ?) "
        "ON CONFLICT(pair, date) DO UPDATE SET rate = excluded.rate",
        [(pair, d, r) for d, r in rows if r is not None])


def _latest_stale(conn: sqlite3.Connection, pair: str, ts) -> bool:
    """Live tick from a closed earlier UTC day (FX weekend) whose daily bar is stored."""
    if not ts:
        return False
    tick_day = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).date().isoformat()
    if tick_day >= datetime.datetime.now(datetime.timezone.utc).date().isoformat():
        return False
    return conn.execute("SELECT 1 FROM fx_daily WHERE pair = ? AND date >= ? LIMIT 1",
                        (pair, tick_day)).fetchone() is not None


def rate_to_usd(conn: sqlite3.Connection, currency: str,
                on_date: str | None = None) -> tuple[float, bool]:
    """(rate, found). If no rate is known, returns (1.0, False) and the caller
    should surface an FX warning rather than silently trust the value."""
    currency = (currency or "USD").upper()
    if currency == "USD":
        return 1.0, True
    pair = fx_pair(currency)
    if on_date:
        row = conn.execute(
            "SELECT rate FROM fx_daily WHERE pair = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 1", (pair, on_date)).fetchone()
        if row:
            return float(row[0]), True
    row = conn.execute("SELECT rate, ts FROM fx_latest WHERE pair = ?", (pair,)).fetchone()
    if row and not on_date and _latest_stale(conn, pair, row[1]):
        row = None
    if row:
        return float(row[0]), True
    row = conn.execute(
        "SELECT rate FROM fx_daily WHERE pair = ? ORDER BY date DESC LIMIT 1",
        (pair,)).fetchone()
    if row:
        return float(row[0]), True
    log.warning("no FX rate known for %s (date=%s); using 1.0 fallback", pair, on_date)
    return 1.0, False


def usd_to_display(conn: sqlite3.Connection, display_currency: str,
                   on_date: str | None = None) -> tuple[float, bool]:
    """Multiplier converting a USD amount into the display currency."""
    display_currency = (display_currency or "USD").upper()
    if display_currency == "USD":
        return 1.0, True
    rate, found = rate_to_usd(conn, display_currency, on_date)
    if rate == 0:
        return 1.0, False
    return 1.0 / rate, found


def convert(conn: sqlite3.Connection, amount: float, from_cur: str, to_cur: str,
            on_date: str | None = None) -> tuple[float, bool]:
    r1, f1 = rate_to_usd(conn, from_cur, on_date)
    r2, f2 = usd_to_display(conn, to_cur, on_date)
    return amount * r1 * r2, (f1 and f2)
