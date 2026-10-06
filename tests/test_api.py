"""API routes via Flask test client (Yahoo mocked where needed)."""
import io
import json

from backend import db


def test_pages_render(client):
    for path in ["/", "/holdings", "/transactions", "/records", "/manual-prices",
                 "/daily-values", "/asset-prices", "/performance", "/strategy-groups",
                 "/disposable-income", "/scrape-monitor", "/settings"]:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert b"ASSET" in resp.data
    for path in ["/historical", "/strategies", "/playground"]:
        assert client.get(path).status_code == 404, path


def test_accounts_crud(client):
    r = client.post("/api/accounts", json={"name": "Securities", "custodian": "IBKR",
                                           "custodian_type": "broker",
                                           "base_currency": "usd"})
    assert r.status_code == 201
    acc_id = r.get_json()["id"]
    # duplicate custodian+name rejected
    assert client.post("/api/accounts", json={"name": "Securities",
                                              "custodian": "IBKR"}).status_code == 409
    assert client.post("/api/accounts", json={}).status_code == 400
    assert client.post("/api/accounts", json={"name": "X",
                                              "custodian_type": "bad"}).status_code == 400

    accounts = client.get("/api/accounts").get_json()
    mine = next(a for a in accounts if a["id"] == acc_id)
    assert mine["label"] == "IBKR / Securities"
    assert mine["base_currency"] == "USD" and "is_margin" not in mine

    assert client.put(f"/api/accounts/{acc_id}",
                      json={"custodian_type": "exchange"}).status_code == 200
    # unused account deletes fine
    assert client.delete(f"/api/accounts/{acc_id}").status_code == 200


def test_delete_account_referenced_by_txn(client, sample_portfolio):
    r = client.delete(f"/api/accounts/{sample_portfolio['account_id']}")
    assert r.status_code == 409


def test_currency_creates_cash_asset(client):
    r = client.post("/api/currencies", json={"code": "nok", "name": "Norwegian Krone"})
    assert r.status_code == 201
    with db.db_conn() as conn:
        row = conn.execute("SELECT * FROM assets WHERE ticker='NOK'").fetchone()
        assert row is not None and row["asset_type"] == "cash"


def test_assets_list_orders_cash_equity_then_options(client, sample_portfolio, monkeypatch):
    from backend.routes import api_assets

    monkeypatch.setattr(api_assets.client, "get_stock_info", lambda ticker: None)
    monkeypatch.setattr("backend.scheduler.trigger_backfill", lambda asset_id, ticker: None)
    r = client.post("/api/assets", json={"ticker": "SPCX260731P00160000",
                                         "asset_type": "option",
                                         "currency": "USD",
                                         "is_yahoo_tracked": True})
    assert r.status_code == 201
    rows = client.get("/api/assets?all=1").get_json()
    assert rows[0]["asset_type"] == "cash"
    assert rows[-1]["asset_type"] == "option"


def test_option_daily_backfill_uses_exact_contract_symbol(client, sample_portfolio, monkeypatch):
    from backend.routes import api_assets
    from backend import scheduler

    monkeypatch.setattr(api_assets.client, "get_stock_info", lambda ticker: None)
    monkeypatch.setattr("backend.scheduler.trigger_backfill", lambda asset_id, ticker: None)
    r = client.post("/api/assets", json={"ticker": "SPCX260731P00160000",
                                         "asset_type": "option",
                                         "currency": "USD",
                                         "is_yahoo_tracked": True})
    assert r.status_code == 201
    aid = r.get_json()["id"]

    def fake_chart(ticker, interval="1d", period1=0, period2=9999999999):
        assert ticker == "SPCX260731P00160000"
        return {"meta": {}, "bars": [{"ts": 1704067200, "open": 1.0, "high": 1.2,
                                       "low": 0.8, "close": 1.1, "adj_close": 1.1,
                                       "volume": 42}]}

    monkeypatch.setattr(scheduler.client, "get_chart", fake_chart)
    assert scheduler.backfill_daily_asset(aid, "SPCX260731P00160000") == 1
    with db.db_conn() as conn:
        row = conn.execute("SELECT close FROM prices_daily WHERE asset_id = ?",
                           (aid,)).fetchone()
    assert row[0] == 1.1


def test_manual_asset_and_prices_flow(client):
    r = client.post("/api/assets", json={"ticker": "RE:X", "asset_type": "real_estate",
                                         "currency": "HKD", "is_yahoo_tracked": False,
                                         "name": "Unit X"})
    assert r.status_code == 201
    aid = r.get_json()["id"]
    assert client.post(f"/api/assets/{aid}/manual-prices",
                       json={"price_date": "2024-03-01", "price": 5000000}).status_code == 201
    prices = client.get(f"/api/assets/{aid}/manual-prices").get_json()
    assert prices[0]["price"] == 5000000
    pid = prices[0]["id"]
    client.put(f"/api/manual-prices/{pid}", json={"price": 5100000})
    assert client.get(f"/api/assets/{aid}/manual-prices").get_json()[0]["price"] == 5100000
    client.delete(f"/api/manual-prices/{pid}")
    assert client.get(f"/api/assets/{aid}/manual-prices").get_json() == []


def test_transactions_crud_and_validation(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    # no flow at all
    assert client.post("/api/transactions",
                       json={"txn_date": "2024-05-01"}).status_code == 400
    # flow present but neither party is an account
    assert client.post("/api/transactions", json={
        "txn_date": "2024-05-01", "inflow_asset_id": sample_portfolio["usd"],
        "inflow_amount": 5, "inflow_party": "someone",
        "outflow_party": "someone else"}).status_code == 400

    # external -> account deposit, date-only normalized to midnight
    r = client.post("/api/transactions", json={
        "txn_date": "2024-05-01", "inflow_account_id": acc,
        "inflow_asset_id": sample_portfolio["usd"], "inflow_amount": 500,
        "outflow_party": "employer", "note": "interest"})
    assert r.status_code == 201
    tid = r.get_json()["id"]

    rows = client.get("/api/transactions?q=interest").get_json()
    assert len(rows) == 1 and rows[0]["id"] == tid
    assert rows[0]["txn_date"] == "2024-05-01 00:00:00"
    assert rows[0]["inflow_account_label"] == "Default Bank / Main"
    assert rows[0]["outflow_account_label"] is None

    # datetime-local format normalized ('T', minutes only)
    r2 = client.post("/api/transactions", json={
        "txn_date": "2024-05-02T13:45", "inflow_account_id": acc,
        "inflow_asset_id": sample_portfolio["usd"], "inflow_amount": 1,
        "outflow_party": "x", "note": "timecheck"})
    assert r2.status_code == 201
    row2 = client.get("/api/transactions?q=timecheck").get_json()[0]
    assert row2["txn_date"] == "2024-05-02 13:45:00"
    # end filter (date-only) includes same-day intraday times
    hits = client.get("/api/transactions?q=timecheck&end=2024-05-02").get_json()
    assert len(hits) == 1

    upd = client.put(f"/api/transactions/{tid}", json={"inflow_amount": 600})
    assert upd.status_code == 200
    assert client.get("/api/transactions?q=interest").get_json()[0]["inflow_amount"] == 600

    assert client.delete(f"/api/transactions/{tid}").get_json()["ok"]
    assert client.get("/api/transactions?q=interest").get_json() == []


def test_transactions_bulk_delete(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    ids = []
    for i in range(3):
        r = client.post("/api/transactions", json={
            "txn_date": f"2024-07-0{i + 1}", "inflow_account_id": acc,
            "inflow_asset_id": sample_portfolio["usd"], "inflow_amount": 10 + i,
            "outflow_party": "x", "note": "bulkdel"})
        assert r.status_code == 201
        ids.append(r.get_json()["id"])

    for bad in ({}, {"txn_ids": []}, {"txn_ids": "nope"}, {"txn_ids": ["abc"]}):
        assert client.post("/api/transactions/delete-many", json=bad).status_code == 400

    r = client.post("/api/transactions/delete-many", json={"txn_ids": ids[:2]})
    assert r.get_json() == {"ok": True, "deleted": 2}
    left = client.get("/api/transactions?q=bulkdel").get_json()
    assert [t["id"] for t in left] == [ids[2]]


def test_same_account_buy_and_fee_default(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    r = client.post("/api/transactions", json={
        "txn_date": "2024-06-03 10:00", "outflow_account_id": acc,
        "outflow_asset_id": sample_portfolio["usd"], "outflow_amount": 1800,
        "inflow_account_id": acc, "inflow_asset_id": sample_portfolio["aapl"],
        "inflow_amount": 10, "fee_asset_id": sample_portfolio["usd"],
        "fee_amount": 2, "note": "samefee"})
    assert r.status_code == 201
    row = client.get("/api/transactions?q=samefee").get_json()[0]
    assert row["fee_account_id"] == acc  # defaulted to the outflow party account
    data = client.get("/api/dashboard/summary?ccy=USD").get_json()
    aapl_qty = sum(h["quantity"] for h in data["holdings"] if h["ticker"] == "AAPL")
    assert aapl_qty == 110


def test_dashboard_summary_endpoint(client, sample_portfolio):
    data = client.get("/api/dashboard/summary?ccy=USD").get_json()
    assert abs(data["kpis"]["nav"] - 173400) < 0.01
    assert data["display_currency"] == "USD"
    assert len(data["holdings"]) == 4


def test_daily_values_and_csv(client, sample_portfolio):
    client.post("/api/history/snapshots/take")
    data = client.get("/api/history/daily-values?mode=value").get_json()
    assert data["rows"] and data["rows"][0]["cells"][0] > 0
    csv_resp = client.get("/api/history/daily-values.csv?mode=value")
    assert csv_resp.status_code == 200
    body = csv_resp.data.decode()
    assert body.splitlines()[0].startswith("Date,TOTAL")
    assert "AAPL" in body


def test_daily_values_endpoint_provisional_today(client, sample_portfolio):
    client.post("/api/history/snapshots/rebuild",
                json={"start": "2024-02-01", "end": "2024-02-02"})
    data = client.get("/api/history/daily-values?mode=value").get_json()
    top = data["rows"][0]
    assert top["provisional"] is True and top["date"] == db.today_str()
    assert abs(top["cells"][0] - 173400) < 0.01
    assert data["rows"][1]["date"] == "2024-02-02"
    # CSV export remains frozen-only (no provisional today row)
    body = client.get("/api/history/daily-values.csv?mode=value").data.decode()
    assert db.today_str() not in body
    assert "2024-02-02" in body


def test_dashboard_series_live_today_point(client, sample_portfolio):
    client.post("/api/history/snapshots/rebuild",
                json={"start": "2024-02-01", "end": "2024-02-02"})
    s = client.get("/api/dashboard/series?ccy=USD").get_json()
    assert s["dates"][-1] == "2024-02-02"
    assert s["today"]["date"] == db.today_str()
    assert abs(s["today"]["value"] - 173400) < 0.01
    # candle mode carries no provisional point
    s2 = client.get("/api/dashboard/series?ccy=USD&mode=candle").get_json()
    assert s2["today"] is None


def test_manual_price_change_resyncs_snapshots(client, sample_portfolio):
    client.post("/api/history/snapshots/rebuild",
                json={"start": "2024-02-01", "end": "2024-02-02"})
    home = sample_portfolio["home"]
    with db.db_conn() as c:
        before = c.execute("SELECT value_usd FROM daily_snapshots "
                           "WHERE date='2024-02-02' AND asset_id=?", (home,)).fetchone()[0]
    assert abs(before - 57600) < 0.01  # 450000 HKD x 0.128
    r = client.post(f"/api/assets/{home}/manual-prices",
                    json={"price_date": "2024-02-01", "price": 500000})
    assert r.status_code == 201
    with db.db_conn() as c:
        after = c.execute("SELECT value_usd FROM daily_snapshots "
                          "WHERE date='2024-02-02' AND asset_id=?", (home,)).fetchone()[0]
    assert abs(after - 64000) < 0.01  # 500000 HKD x 0.128


def test_holdings_csv(client, sample_portfolio):
    csv_resp = client.get("/api/dashboard/holdings.csv?ccy=USD")
    assert csv_resp.status_code == 200
    body = csv_resp.data.decode()
    assert body.splitlines()[0].startswith("TYPE,TICKER,NAME")
    assert "AAPL" in body


def test_transactions_csv(client, sample_portfolio):
    csv_resp = client.get("/api/transactions/csv")
    assert csv_resp.status_code == 200
    body = csv_resp.data.decode()
    assert body.splitlines()[0].startswith("ID,DATE / TIME,OUTFLOW PARTY")
    assert "AAPL" in body


def test_transactions_import_preview_and_commit_firstrade(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    csv_text = (
        "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
        '1/5/2024,買進,2,Buy AAPL,AAPL,現金,180,"-360.00"\n'
        "2/5/2024,股息,0,Dividend credit,,現金,,5\n"
        ",,,,,,,\n"
    )
    preview = client.post(
        "/api/transactions/import/preview",
        data={
            "provider": "firstrade",
            "account_id": str(acc),
            "file": (io.BytesIO(csv_text.encode("utf-8")), "firstrade.csv"),
        },
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    data = preview.get_json()
    assert data["summary"]["total_rows"] == 2
    assert data["summary"]["valid_rows"] == 2

    before = len(client.get("/api/transactions?limit=500").get_json())
    commit = client.post(
        "/api/transactions/import/commit",
        data={
            "provider": "firstrade",
            "account_id": str(acc),
            "file": (io.BytesIO(csv_text.encode("utf-8")), "firstrade.csv"),
        },
        content_type="multipart/form-data",
    )
    assert commit.status_code == 200
    res = commit.get_json()
    assert res["summary"]["inserted_rows"] == 2
    after = len(client.get("/api/transactions?limit=500").get_json())
    assert after == before + 2


def test_transactions_import_duplicates_skipped_by_default(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    csv_text = (
        "ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,DateTime,"
        "Buy/Sell,LevelOfDetail,Quantity,TradePrice,TradeMoney,Taxes,IBCommission,"
        "IBCommissionCurrency\n"
        "U1,USD,STK,AAPL,IB buy,3/5/2024,BUY,EXECUTION,1,180,180,0,-7.8,USD\n"
    )

    def _commit(**extra):
        return client.post(
            "/api/transactions/import/commit",
            data={"provider": "ib", "account_id": str(acc),
                  "file": (io.BytesIO(csv_text.encode("utf-8")), "ib.csv"), **extra},
            content_type="multipart/form-data")

    first = _commit()
    assert first.status_code == 200
    assert first.get_json()["summary"]["inserted_rows"] == 1

    # same file again: the identical row is flagged and skipped by default
    second = _commit()
    assert second.status_code == 200
    body = second.get_json()
    assert body["summary"]["inserted_rows"] == 0
    assert body["summary"]["duplicate_rows"] == 1
    assert body["rows"][0]["status"] == "duplicate"
    assert len(client.get("/api/transactions?q=IB buy&limit=500").get_json()) == 1

    # explicit opt-in re-imports the duplicate
    third = _commit(include_duplicates="1")
    assert third.get_json()["summary"]["inserted_rows"] == 1
    assert len(client.get("/api/transactions?q=IB buy&limit=500").get_json()) == 2


def test_ib_flex_bond_import_seeds_manual_price(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    with db.db_conn() as c:
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, "
                  "is_yahoo_tracked) VALUES ('T 1 1/2 08/15/26', 'UST', 'bond', 'USD', 0)")
        bond = c.execute("SELECT id FROM assets WHERE ticker='T 1 1/2 08/15/26'"
                         ).fetchone()[0]
    csv_text = (
        "ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,DateTime,"
        "Buy/Sell,LevelOfDetail,Quantity,TradePrice,TradeMoney,Taxes,IBCommission,"
        "IBCommissionCurrency\n"
        "U1,USD,BOND,T 1 1/2 08/15/26,T 1 1/2 08/15/26,04/08/202607:49:17 EDT,"
        "BUY,EXECUTION,70000,99.92579,69948.05,0,-5,USD\n"
    )
    commit = client.post(
        "/api/transactions/import/commit",
        data={"provider": "ib", "account_id": str(acc),
              "file": (io.BytesIO(csv_text.encode("utf-8")), "flex.csv")},
        content_type="multipart/form-data")
    assert commit.status_code == 200
    assert commit.get_json()["summary"]["inserted_rows"] == 1
    prices = client.get(f"/api/assets/{bond}/manual-prices").get_json()
    assert len(prices) == 1
    assert prices[0]["price_date"] == "2026-08-04"
    assert abs(prices[0]["price"] - 0.9992579) < 1e-9


def test_import_duplicate_matching_is_count_aware(client, sample_portfolio):
    """N identical DB rows mark only the first N identical file rows."""
    acc = sample_portfolio["account_id"]
    header = "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
    row = '1/5/2024,買進,2,Same fill,AAPL,現金,180,"-360.00"\n'

    commit = client.post(
        "/api/transactions/import/commit",
        data={"provider": "firstrade", "account_id": str(acc),
              "file": (io.BytesIO((header + row * 2).encode("utf-8")), "ft.csv")},
        content_type="multipart/form-data")
    assert commit.get_json()["summary"]["inserted_rows"] == 2

    preview = client.post(
        "/api/transactions/import/preview",
        data={"provider": "firstrade", "account_id": str(acc),
              "file": (io.BytesIO((header + row * 3).encode("utf-8")), "ft.csv")},
        content_type="multipart/form-data")
    s = preview.get_json()["summary"]
    assert s["duplicate_rows"] == 2 and s["valid_rows"] == 1


def test_transactions_import_validation_errors(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    bad_provider = client.post(
        "/api/transactions/import/preview",
        data={
            "provider": "bad",
            "account_id": str(acc),
            "file": (io.BytesIO(b"Date,Action\n"), "x.csv"),
        },
        content_type="multipart/form-data",
    )
    assert bad_provider.status_code == 400

    missing_file = client.post(
        "/api/transactions/import/preview",
        data={"provider": "firstrade", "account_id": str(acc)},
        content_type="multipart/form-data",
    )
    assert missing_file.status_code == 400

    unknown_ticker_csv = (
        "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
        "1/5/2024,買進,2,Unknown ticker,NOPE,現金,180,-360\n"
    )
    preview = client.post(
        "/api/transactions/import/preview",
        data={
            "provider": "firstrade",
            "account_id": str(acc),
            "file": (io.BytesIO(unknown_ticker_csv.encode("utf-8")), "x.csv"),
        },
        content_type="multipart/form-data",
    )
    assert preview.status_code == 200
    payload = preview.get_json()
    assert payload["summary"]["valid_rows"] == 0
    assert payload["summary"]["invalid_rows"] == 1
    assert "unknown ticker" in (payload["rows"][0]["error"] or "").lower()
    assert payload["rows"][0]["ticker"] == "NOPE"
    unknown = payload["unknown_tickers"]
    assert len(unknown) == 1 and unknown[0]["ticker"] == "NOPE"
    assert unknown[0]["count"] == 1
    assert unknown[0]["suggestion"]["asset_type"] == "equity"


def test_zero_amount_flow_side_cleared(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    r = client.post("/api/transactions", json={
        "txn_date": "2024-03-01 10:00",
        "outflow_account_id": acc, "outflow_party": "someone",
        "outflow_asset_id": sample_portfolio["usd"], "outflow_amount": 0,
        "inflow_account_id": acc,
        "inflow_asset_id": sample_portfolio["usd"], "inflow_amount": 25,
        "note": "zeroflow"})
    assert r.status_code == 201
    t = client.get("/api/transactions?q=zeroflow").get_json()[0]
    assert t["outflow_account_id"] is None
    assert t["outflow_asset_id"] is None
    assert t["outflow_party"] == ""
    assert t["outflow_amount"] == 0


def test_asset_prices_matrix_endpoint(client, sample_portfolio):
    client.post("/api/history/snapshots/rebuild",
                json={"start": "2024-02-01", "end": "2024-02-02"})
    data = client.get("/api/history/asset-prices").get_json()
    tickers = [c["ticker"] for c in data["columns"]]
    assert "AAPL" in tickers and "RE:HOME" in tickers
    assert "USD" not in tickers and "HKD" not in tickers  # cash excluded
    aapl, home = tickers.index("AAPL"), tickers.index("RE:HOME")
    frozen = next(r for r in data["rows"] if r["date"] == "2024-02-02")
    assert frozen["cells"][aapl] == 180.0     # prices_daily close
    assert frozen["cells"][home] == 450000.0  # manual price carried forward
    live = data["rows"][0]
    assert live["provisional"] is True
    assert live["cells"][aapl] == 180.0       # latest_quotes
    csv_resp = client.get("/api/history/asset-prices.csv")
    assert csv_resp.status_code == 200
    head = csv_resp.data.decode().splitlines()[0]
    assert head.startswith("Date,") and "AAPL (USD)" in head


def test_index_sparks_endpoint(client, sample_portfolio):
    data = client.get("/api/dashboard/index-sparks").get_json()
    assert [d["key"] for d in data] == ["SP500", "DOW", "NASDAQ"]
    for d in data:  # assets created lazily; nothing scraped yet
        assert d["current"] is None and d["spark"] == []
    with db.db_conn() as conn:
        aid = conn.execute("SELECT id FROM assets WHERE ticker='^GSPC'").fetchone()[0]
        conn.execute("INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source) "
                     "VALUES (?, 5000, 4900, 1700000000, 'test')", (aid,))
    sp = next(d for d in client.get("/api/dashboard/index-sparks").get_json()
              if d["key"] == "SP500")
    assert sp["current"] == 5000 and sp["prev_close"] == 4900 and sp["change"] == 100
    assert len(sp["spark"]) == 2  # two-point fallback: prev close -> current
    assert sp["boundary_ts"] == 1700000000 - 43200


def test_snapshots_rebuild_endpoint_bounds_validation(client, sample_portfolio):
    ok = client.post("/api/history/snapshots/rebuild",
                     json={"start": "2024-01-01 12:34:56", "end": "2024-02-02T18:00:00"})
    assert ok.status_code == 200
    assert ok.get_json()["rows"] > 0

    bad = client.post("/api/history/snapshots/rebuild", json={"start": "not-a-date"})
    assert bad.status_code == 400
    assert bad.get_json() == {"error": "invalid start/end date"}

    rev = client.post("/api/history/snapshots/rebuild",
                      json={"start": "2024-02-03", "end": "2024-02-02"})
    assert rev.status_code == 400
    assert rev.get_json() == {"error": "invalid start/end date"}


def test_settings_roundtrip_and_prefs(client):
    r = client.post("/api/settings", json={"display_currency": "HKD",
                                           "display_utc_offset_hours": "5",
                                           "display_prefs": json.dumps({"kpis": ["nav"]})})
    assert r.get_json()["ok"]
    s = client.get("/api/settings").get_json()
    assert s["display_currency"] == "HKD"
    assert s["display_utc_offset_hours"] == "5"
    assert json.loads(s["display_prefs"]) == {"kpis": ["nav"]}


def test_purge_intraday(client, sample_portfolio):
    with db.db_conn() as conn:
        conn.executemany(
            "INSERT INTO prices_intraday(asset_id, bar_interval, ts, open, high, low, close, "
            "volume) VALUES (?, '60m', ?, 1, 1, 1, 1, 0)",
            [(sample_portfolio["aapl"], 1700000000 + i * 3600) for i in range(5)])
    res = client.post("/api/settings/purge",
                      json={"target": "intraday", "asset_id": sample_portfolio["aapl"]})
    assert res.get_json()["deleted"] == 5
    with db.db_conn() as conn:
        left = conn.execute("SELECT COUNT(*) FROM prices_intraday").fetchone()[0]
        daily = conn.execute("SELECT COUNT(*) FROM prices_daily").fetchone()[0]
    assert left == 0
    assert daily == 1  # daily history untouched


def test_scrape_endpoints(client):
    with db.db_conn() as conn:
        conn.execute("INSERT INTO scrape_log(ts, job, ticker, status, duration_ms, message) "
                     "VALUES (?, 'quotes', '', 'success', 120.5, '10/10 quotes')",
                     (db.now_ms_str(),))
    rows = client.get("/api/scrape/log?job=quotes").get_json()
    assert rows and rows[0]["status"] == "success"
    assert client.get("/api/scrape/status").status_code == 200
    assert client.get("/api/scrape/hourly").status_code == 200
    assert client.post("/api/scrape/run/nonexistent").status_code == 404


def test_position_trades_endpoint(client, sample_portfolio):
    import pytest
    acc, usd, aapl = (sample_portfolio["account_id"], sample_portfolio["usd"],
                      sample_portfolio["aapl"])
    url = f"/api/dashboard/position-trades?asset_id={aapl}&account_id={acc}"

    data = client.get(url).get_json()
    assert data["ticker"] == "AAPL" and data["current_cycle"] == 0
    assert data["open_qty"] == pytest.approx(100)
    assert data["avg_buy"] == pytest.approx(150) and data["avg_sell"] is None
    assert len(data["trades"]) == 1
    t = data["trades"][0]
    assert t["side"] == "buy" and t["px"] == pytest.approx(150)
    assert t["delta"] == pytest.approx(100) and t["qty_after"] == pytest.approx(100)

    # partial sell realizes against the cycle's avg buy
    client.post("/api/transactions", json={
        "txn_date": "2024-03-01 10:00:00",
        "outflow_account_id": acc, "outflow_asset_id": aapl, "outflow_amount": 50,
        "inflow_account_id": acc, "inflow_asset_id": usd, "inflow_amount": 9500})
    data = client.get(url).get_json()
    assert data["open_qty"] == pytest.approx(50)
    assert data["avg_sell"] == pytest.approx(190)
    assert data["realized"] == pytest.approx((190 - 150) * 50)

    # closing the rest ends the cycle; a re-buy starts a fresh one
    client.post("/api/transactions", json={
        "txn_date": "2024-03-02 10:00:00",
        "outflow_account_id": acc, "outflow_asset_id": aapl, "outflow_amount": 50,
        "inflow_account_id": acc, "inflow_asset_id": usd, "inflow_amount": 9500})
    client.post("/api/transactions", json={
        "txn_date": "2024-03-03 10:00:00",
        "outflow_account_id": acc, "outflow_asset_id": usd, "outflow_amount": 3000,
        "inflow_account_id": acc, "inflow_asset_id": aapl, "inflow_amount": 30})
    data = client.get(url).get_json()
    assert data["current_cycle"] == 1
    assert data["open_qty"] == pytest.approx(30)
    assert data["avg_buy"] == pytest.approx(100) and data["avg_sell"] is None
    assert [t["cycle"] for t in data["trades"]] == [0, 0, 0, 1]

    assert client.get("/api/dashboard/position-trades").status_code == 400
    assert client.get(f"/api/dashboard/position-trades?asset_id=999999&account_id={acc}"
                      ).status_code == 404
