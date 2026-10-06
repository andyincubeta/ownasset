"""User-configurable benchmark indices (setting `benchmark_indices`, JSON list).

Each entry: {key, ticker, name, short, currency, home}. `home` = shown in the
HOME index strip; every entry is available to the index-compare charts.
Index assets are ordinary yahoo-tracked assets (notes='benchmark'), so the
scheduler refreshes them like any stock.
"""
import json
import re

from backend import db

SETTING_KEY = "benchmark_indices"
MAX_INDICES = 25
MAX_HOME = 8
TICKER_RE = re.compile(r"[\^A-Za-z0-9.=\-]{1,20}")

# (key, ticker, name, short, currency)
INDEX_PRESETS = [
    ("SP500", "^GSPC", "S&P 500", "S&P", "USD"),
    ("DOW", "^DJI", "Dow Jones Industrial Average", "DOW", "USD"),
    ("NASDAQ", "^IXIC", "NASDAQ Composite", "NDQ", "USD"),
    ("NDX", "^NDX", "NASDAQ-100", "NDX", "USD"),
    ("RUT", "^RUT", "Russell 2000", "RUT", "USD"),
    ("SOX", "^SOX", "PHLX Semiconductor", "SOX", "USD"),
    ("VIX", "^VIX", "CBOE Volatility Index", "VIX", "USD"),
    ("HSI", "^HSI", "Hang Seng Index", "HSI", "HKD"),
    ("HSCEI", "^HSCE", "Hang Seng China Enterprises", "HSCEI", "HKD"),
    ("SSE", "000001.SS", "SSE Composite (Shanghai)", "SSE", "CNY"),
    ("CSI300", "000300.SS", "CSI 300", "CSI300", "CNY"),
    ("SZSE", "399001.SZ", "SZSE Component (Shenzhen)", "SZSE", "CNY"),
    ("CHINEXT", "399006.SZ", "ChiNext", "CHINEXT", "CNY"),
    ("N225", "^N225", "Nikkei 225", "N225", "JPY"),
    ("KOSPI", "^KS11", "KOSPI", "KOSPI", "KRW"),
    ("TWII", "^TWII", "Taiwan Weighted", "TWII", "TWD"),
    ("STI", "^STI", "Straits Times Index", "STI", "SGD"),
    ("SENSEX", "^BSESN", "BSE SENSEX", "SENSEX", "INR"),
    ("FTSE", "^FTSE", "FTSE 100", "FTSE", "GBP"),
    ("DAX", "^GDAXI", "DAX", "DAX", "EUR"),
    ("CAC", "^FCHI", "CAC 40", "CAC", "EUR"),
    ("STOXX50", "^STOXX50E", "EURO STOXX 50", "SX5E", "EUR"),
    ("ASX200", "^AXJO", "S&P/ASX 200", "ASX200", "AUD"),
]
_PRESET_BY_TICKER = {p[1].upper(): p for p in INDEX_PRESETS}
DEFAULT_KEYS = ("SP500", "DOW", "NASDAQ", "NDX", "HSI", "SOX")
DEFAULT_HOME = ("SP500", "DOW", "NASDAQ")


def presets() -> list[dict]:
    return [{"key": k, "ticker": t, "name": n, "short": s, "currency": c}
            for k, t, n, s, c in INDEX_PRESETS]


def _defaults() -> list[dict]:
    by_key = {p["key"]: p for p in presets()}
    return [{**by_key[k], "home": k in DEFAULT_HOME} for k in DEFAULT_KEYS]


def get_benchmarks(conn) -> list[dict]:
    raw = db.get_setting(conn, SETTING_KEY)
    if raw:
        try:
            items, err = validate_indices(json.loads(raw))
            if not err and items:
                return items
        except (ValueError, TypeError):
            pass
    return _defaults()


def get_benchmark(conn, key: str) -> dict | None:
    return next((b for b in get_benchmarks(conn) if b["key"] == key), None)


def validate_indices(raw) -> tuple[list[dict], str | None]:
    """Normalise a client list -> (entries, error). Keys are derived server-side."""
    if not isinstance(raw, list):
        return [], "list of indices required"
    if len(raw) > MAX_INDICES:
        return [], f"at most {MAX_INDICES} indices"
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            return [], "each index must be an object"
        ticker = str(item.get("ticker") or "").strip()
        if not TICKER_RE.fullmatch(ticker):
            return [], f"invalid ticker: {ticker!r}"
        preset = _PRESET_BY_TICKER.get(ticker.upper())
        if preset:
            key, ticker, p_name, p_short, p_ccy = preset
        else:
            ticker = ticker.upper()
            key = re.sub(r"[^A-Z0-9]", "", ticker)
            p_name, p_short, p_ccy = ticker, key[:8], "USD"
        if not key:
            return [], f"invalid ticker: {ticker!r}"
        if key in seen:
            return [], f"duplicate index: {ticker}"
        seen.add(key)
        name = str(item.get("name") or "").strip()[:60] or p_name
        short = str(item.get("short") or "").strip()[:10] or p_short
        ccy = str(item.get("currency") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", ccy):
            ccy = p_ccy
        out.append({"key": key, "ticker": ticker, "name": name, "short": short,
                    "currency": ccy, "home": bool(item.get("home"))})
    if sum(1 for b in out if b["home"]) > MAX_HOME:
        return [], f"at most {MAX_HOME} indices on HOME"
    return out, None


def ensure_benchmark_asset(conn, entry: dict) -> int:
    """Index asset for `entry`, created on first use; re-activates one that was
    switched off when its index was removed."""
    row = conn.execute("SELECT id, active, notes FROM assets WHERE ticker = ?",
                       (entry["ticker"],)).fetchone()
    if row:
        if not row["active"] and row["notes"] == "benchmark":
            conn.execute("UPDATE assets SET active = 1 WHERE id = ?", (row["id"],))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked, "
        "quote_type, notes) VALUES (?, ?, 'other', ?, 1, 'INDEX', 'benchmark')",
        (entry["ticker"], entry["name"], entry.get("currency") or "USD"))
    return cur.lastrowid


def save_benchmarks(conn, entries: list[dict]):
    """Persist the list; removed index assets stop being scraped unless held."""
    old = {b["ticker"] for b in get_benchmarks(conn)}
    db.set_setting(conn, SETTING_KEY, json.dumps(entries))
    for b in entries:
        ensure_benchmark_asset(conn, b)
    for ticker in old - {b["ticker"] for b in entries}:
        conn.execute(
            "UPDATE assets SET active = 0 WHERE ticker = ? AND notes = 'benchmark' "
            "AND NOT EXISTS (SELECT 1 FROM transactions t WHERE assets.id IN "
            "(t.inflow_asset_id, t.outflow_asset_id, t.fee_asset_id))", (ticker,))
