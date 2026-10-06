"""Options-archive client, provider routing, and source-switch endpoints
(no network: session/get_history are monkeypatched)."""
import json
import sqlite3

import pytest

import config
from backend import db, option_archive, scheduler
from backend.importers.transactions_csv import suggest_unknown_asset
from backend.routes import api_assets as api_assets_mod


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


RAW_ROWS = [
    {"date": "2026-07-17", "last": 0, "mark": 2.0, "bid": 1.9, "ask": 2.1,
     "volume": 0.0, "open_interest": 5.0, "implied_volatility": 0.0},
    {"date": "2026-07-16", "last": 1.2, "mark": 1.25, "bid": 1.1, "ask": 1.4,
     "volume": 10.0, "open_interest": 5.0, "implied_volatility": 0.9},
]

BARS = [
    {"date": "2026-07-16", "close": 1.2, "bid": 1.1, "ask": 1.4, "volume": 10.0, "iv": 0.9},
    {"date": "2026-07-17", "close": 2.0, "bid": 1.9, "ask": 2.1, "volume": 0.0, "iv": 0.0},
]


# ------------------------------------------------------------- client -----

def test_parse_occ_ticker():
    p = option_archive.parse_occ_ticker("SKHY260717P00190000")
    assert p == {"underlying": "SKHY", "expiry": "2026-07-17",
                 "contract_type": "put", "strike": 190.0}
    c = option_archive.parse_occ_ticker("aapl250103c00180000")
    assert c["contract_type"] == "call" and c["strike"] == 180.0
    assert option_archive.parse_occ_ticker("AAPL") is None
    assert option_archive.parse_occ_ticker("SKHY261399P00190000") is None  # month 13
    assert option_archive.parse_occ_ticker("") is None


def test_get_history_close_fallback_and_sort(monkeypatch):
    monkeypatch.setattr(option_archive.session, "get",
                        lambda url, params=None, timeout=None: FakeResponse(RAW_ROWS))
    bars = option_archive.get_history("SKHY", "2026-07-17", "put", 190.0)
    assert [b["date"] for b in bars] == ["2026-07-16", "2026-07-17"]
    assert [b["close"] for b in bars] == [1.2, 2.0]  # last, then mark fallback


def test_get_history_error_payload(monkeypatch):
    monkeypatch.setattr(option_archive.session, "get",
                        lambda *a, **k: FakeResponse({"error": "no such contract"}))
    with pytest.raises(option_archive.ArchiveError):
        option_archive.get_history("XXXX", "2020-01-17", "call", 1.0)
    monkeypatch.setattr(option_archive.session, "get",
                        lambda *a, **k: FakeResponse([], status=500))
    with pytest.raises(option_archive.ArchiveError):
        option_archive.get_history("XXXX", "2020-01-17", "call", 1.0)


def test_params_for_asset():
    deriv = {"underlying_ticker": "SKHY", "expiry": "2026-07-17",
             "contract_type": "put", "strike": 190}
    p = option_archive.params_for_asset({"ticker": "WHATEVER"}, deriv)
    assert p == {"underlying": "SKHY", "expiry": "2026-07-17",
                 "contract_type": "put", "strike": 190.0}
    p2 = option_archive.params_for_asset({"ticker": "SKHY260717P00190000"}, None)
    assert p2["strike"] == 190.0
    with pytest.raises(option_archive.ArchiveError):
        option_archive.params_for_asset({"ticker": "NOTOCC"}, None)


# ------------------------------------------------------------ fixtures ----

@pytest.fixture()
def option_asset(tmp_db):
    """Expired manual-priced option (the typical CSV-imported leftover)."""
    with db.db_conn() as c:
        cur = c.execute(
            "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
            "VALUES ('SKHY260717P00190000', '', 'option', 'USD', 0)")
        aid = cur.lastrowid
        c.execute(
            "INSERT INTO derivative_details(asset_id, underlying_ticker, contract_type, "
            "strike, expiry, multiplier) VALUES (?, 'SKHY', 'put', 190, '2026-07-17', 100)",
            (aid,))
    return aid


# ----------------------------------------------------------- scheduler ----

def test_backfill_archive_asset(option_asset, monkeypatch):
    monkeypatch.setattr(option_archive, "get_history", lambda *a, **k: BARS)
    n = scheduler.backfill_archive_asset(option_asset)
    assert n == 2
    with db.db_conn() as c:
        rows = c.execute("SELECT date, close, open FROM prices_daily WHERE asset_id = ? "
                         "ORDER BY date", (option_asset,)).fetchall()
        assert [(r["date"], r["close"], r["open"]) for r in rows] == \
            [("2026-07-16", 1.2, None), ("2026-07-17", 2.0, None)]
        lq = c.execute("SELECT price, prev_close, source FROM latest_quotes "
                       "WHERE asset_id = ?", (option_asset,)).fetchone()
        assert (lq["price"], lq["prev_close"], lq["source"]) == (2.0, 1.2, "archive")


def test_archive_final_skips_refetch(option_asset, monkeypatch):
    with db.db_conn() as c:
        assert scheduler._archive_final(c, option_asset) is False  # no bars yet
    monkeypatch.setattr(option_archive, "get_history", lambda *a, **k: BARS)
    scheduler.backfill_archive_asset(option_asset)
    with db.db_conn() as c:
        assert scheduler._archive_final(c, option_asset) is True  # bar on expiry date


# ------------------------------------------------------------ endpoints ---

def test_switch_source_endpoint(client, option_asset, monkeypatch):
    monkeypatch.setattr(option_archive, "get_history", lambda *a, **k: BARS)
    r = client.post(f"/api/assets/{option_asset}/switch-source",
                    json={"source": "option_archive"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["bars"] == 2
    assert data["first_date"] == "2026-07-16" and data["last_date"] == "2026-07-17"
    with db.db_conn() as c:
        row = c.execute("SELECT is_yahoo_tracked, price_provider FROM assets WHERE id = ?",
                        (option_asset,)).fetchone()
        assert row["is_yahoo_tracked"] == 1 and row["price_provider"] == "option_archive"

    r = client.post(f"/api/assets/{option_asset}/switch-source", json={"source": "manual"})
    assert r.status_code == 200
    with db.db_conn() as c:
        assert c.execute("SELECT is_yahoo_tracked FROM assets WHERE id = ?",
                         (option_asset,)).fetchone()[0] == 0

    assert client.post(f"/api/assets/{option_asset}/switch-source",
                       json={"source": "nope"}).status_code == 400
    assert client.post("/api/assets/999999/switch-source",
                       json={"source": "manual"}).status_code == 404


def test_switch_source_archive_failure_changes_nothing(client, option_asset, monkeypatch):
    def boom(*a, **k):
        raise option_archive.ArchiveError("down")
    monkeypatch.setattr(option_archive, "get_history", boom)
    r = client.post(f"/api/assets/{option_asset}/switch-source",
                    json={"source": "option_archive"})
    assert r.status_code == 502
    with db.db_conn() as c:
        assert c.execute("SELECT is_yahoo_tracked FROM assets WHERE id = ?",
                         (option_asset,)).fetchone()[0] == 0


def test_archive_preview_endpoint(client, option_asset, monkeypatch):
    monkeypatch.setattr(option_archive, "get_history", lambda *a, **k: BARS)
    r = client.get(f"/api/assets/{option_asset}/archive-preview")
    assert r.status_code == 200
    data = r.get_json()
    assert data == {"rows": 2, "first_date": "2026-07-16",
                    "last_date": "2026-07-17", "last_close": 2.0}
    assert client.get("/api/assets/999999/archive-preview").status_code == 404


def test_expired_archive_candidates(option_asset):
    with db.db_conn() as c:
        # live option (expiry 2099 via OCC ticker, no deriv row) must be excluded
        c.execute("INSERT INTO assets(ticker, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('SKHY990117C00100000', 'option', 'USD', 1)")
        # expired but already on the archive must be excluded
        cur = c.execute(
            "INSERT INTO assets(ticker, asset_type, currency, is_yahoo_tracked, "
            "price_provider) VALUES ('SKHY250620C00050000', 'option', 'USD', 1, "
            "'option_archive')")
        done_id = cur.lastrowid
        c.execute("INSERT INTO derivative_details(asset_id, underlying_ticker, "
                  "contract_type, strike, expiry) VALUES (?, 'SKHY', 'call', 50, "
                  "'2025-06-20')", (done_id,))
        cands = api_assets_mod._expired_archive_candidates(c)
    assert [x["id"] for x in cands] == [option_asset]


def test_bulk_switch_endpoint(client, option_asset, monkeypatch):
    class SyncThread:  # run the worker inline so assertions see the outcome
        def __init__(self, target=None, args=(), **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(api_assets_mod.threading, "Thread", SyncThread)
    monkeypatch.setattr(option_archive, "get_history", lambda *a, **k: BARS)
    import backend.yahoo as yahoo_mod
    monkeypatch.setattr(yahoo_mod, "polite_sleep", lambda *a, **k: None)

    r = client.post("/api/assets/switch-expired-to-archive")
    assert r.status_code == 200
    assert r.get_json() == {"candidates": 1, "started": True}
    with db.db_conn() as c:
        row = c.execute("SELECT is_yahoo_tracked, price_provider FROM assets WHERE id = ?",
                        (option_asset,)).fetchone()
        assert row["is_yahoo_tracked"] == 1 and row["price_provider"] == "option_archive"
        assert c.execute("SELECT COUNT(*) FROM prices_daily WHERE asset_id = ?",
                         (option_asset,)).fetchone()[0] == 2

    # nothing left to switch
    assert client.post("/api/assets/switch-expired-to-archive").get_json() == \
        {"candidates": 0, "started": False}


def test_create_asset_with_archive_provider(client, monkeypatch):
    monkeypatch.setattr(scheduler, "trigger_backfill", lambda *a, **k: None)
    r = client.post("/api/assets", json={
        "ticker": "SKHY260717C00200000", "asset_type": "option",
        "price_provider": "option_archive",
        "derivative": {"underlying_ticker": "SKHY", "contract_type": "call",
                       "strike": 200, "expiry": "2026-07-17"}})
    assert r.status_code == 201
    with db.db_conn() as c:
        row = c.execute("SELECT price_provider, is_yahoo_tracked, name FROM assets "
                        "WHERE ticker = 'SKHY260717C00200000'").fetchone()
        assert row["price_provider"] == "option_archive" and row["is_yahoo_tracked"] == 1
    assert client.post("/api/assets", json={
        "ticker": "X1", "price_provider": "bogus"}).status_code == 400


# ------------------------------------------------------------- importer ---

def test_suggest_unknown_asset_provider_defaults():
    expired = suggest_unknown_asset("SKHY260717P00190000")  # expired 2026-07-17
    assert expired["is_yahoo_tracked"] is True
    assert expired["price_provider"] == "option_archive"
    live = suggest_unknown_asset("SPCX990617P00150000")  # expires 2099
    assert live["is_yahoo_tracked"] is True
    assert live["price_provider"] == "yahoo"


# ------------------------------------------------------------ migration ---

def test_migration_adds_price_provider(tmp_path, monkeypatch):
    db_dir = tmp_path / ".db"
    db_dir.mkdir()
    path = str(db_dir / "old.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_DIR", str(db_dir))
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL DEFAULT '',
        asset_type TEXT NOT NULL DEFAULT 'equity',
        currency TEXT NOT NULL DEFAULT 'USD',
        is_yahoo_tracked INTEGER NOT NULL DEFAULT 1,
        industry TEXT NOT NULL DEFAULT '',
        sector TEXT NOT NULL DEFAULT '',
        country TEXT NOT NULL DEFAULT '',
        quote_type TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        notes TEXT NOT NULL DEFAULT '',
        info_json TEXT NOT NULL DEFAULT '{}',
        info_updated_at TEXT)""")
    conn.execute("INSERT INTO assets(ticker) VALUES ('OLD1')")
    conn.commit()
    conn.close()
    db.init_db(path)
    with db.db_conn(path) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(assets)")}
        assert "price_provider" in cols
        assert c.execute("SELECT price_provider FROM assets WHERE ticker = 'OLD1'"
                         ).fetchone()[0] == "yahoo"
