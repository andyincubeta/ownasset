"""Portfolio engine: holdings derivation, valuation, categories, NAV."""
from backend import db, portfolio


def test_display_currency_identity_no_fx_round_trip(sample_portfolio):
    """Holdings denominated in the display currency keep their face value."""
    from backend import snapshots

    with db.db_conn() as c:
        c.execute("UPDATE fx_latest SET rate = 0.13 WHERE pair = 'HKDUSD'")
        data = portfolio.summary(c, "HKD")
        by_ticker = {h["ticker"]: h for h in data["holdings"]}
        assert by_ticker["HKD"]["value_display"] == 100000.0
        assert by_ticker["RE:HOME"]["value_display"] == 450000.0
        assert abs(data["kpis"]["nav"] - (550000 + 103000 / 0.13)) < 0.01

        # series: frozen values use the daily rate for USD parts, HKD stays native
        snapshots.rebuild_snapshots(c, start="2024-02-02", end="2024-02-02")
        s = portfolio.portfolio_series(c, "HKD")
        assert abs(s["values"][0] - (550000 + 103000 / 0.128)) < 0.01
        assert abs(s["today"]["value"] - (550000 + 103000 / 0.13)) < 0.01


def test_holdings_from_transactions(sample_portfolio):
    with db.db_conn() as c:
        holds = portfolio.holdings(c)
    by_ticker = {h["asset"]["ticker"]: h for h in holds}
    assert by_ticker["AAPL"]["quantity"] == 100
    assert by_ticker["USD"]["quantity"] == 85000     # 100k - 15k
    assert by_ticker["HKD"]["quantity"] == 100000    # 500k - 400k
    assert by_ticker["RE:HOME"]["quantity"] == 1


def test_holdings_as_of_date(sample_portfolio):
    # date-only cutoff must include intraday times on that date
    with db.db_conn() as c:
        holds = portfolio.holdings(c, as_of="2024-01-05")
    by_ticker = {h["asset"]["ticker"]: h for h in holds}
    assert by_ticker["USD"]["quantity"] == 100000  # deposited 09:00 on 01-02
    assert "AAPL" not in by_ticker
    with db.db_conn() as c:
        holds = portfolio.holdings(c, as_of="2024-01-02")
    assert {h["asset"]["ticker"]: h["quantity"] for h in holds} == {"USD": 100000}


def test_transfer_between_accounts_moves_qty_not_nav(sample_portfolio):
    with db.db_conn() as c:
        cur = c.execute(
            "INSERT INTO accounts(name, custodian_type, custodian, base_currency) "
            "VALUES ('Securities', 'broker', 'IBKR', 'USD')")
        broker_id = cur.lastrowid
        now = db.now_str()
        c.execute(
            "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
            "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
            "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
            "note, created_at, updated_at) "
            "VALUES ('2024-03-01 12:00:00', ?, '', ?, 20000, ?, '', ?, 20000, "
            "NULL, NULL, 0, 'transfer', ?, ?)",
            (sample_portfolio["account_id"], sample_portfolio["usd"],
             broker_id, sample_portfolio["usd"], now, now))
    with db.db_conn() as c:
        holds = portfolio.holdings(c)
        usd_by_acc = {h["account_id"]: h["quantity"] for h in holds
                      if h["asset"]["ticker"] == "USD"}
        assert usd_by_acc[sample_portfolio["account_id"]] == 65000
        assert usd_by_acc[broker_id] == 20000
        s = portfolio.summary(c, "USD")
    assert abs(s["kpis"]["nav"] - 173400) < 0.01  # transfers never change NAV
    broker_rows = [h for h in s["holdings"] if h["account_id"] == broker_id]
    assert broker_rows[0]["custodian"] == "IBKR"
    assert broker_rows[0]["account_label"] == "IBKR / Securities"


def test_price_sources(sample_portfolio):
    with db.db_conn() as c:
        aapl = dict(c.execute("SELECT * FROM assets WHERE ticker='AAPL'").fetchone())
        home = dict(c.execute("SELECT * FROM assets WHERE ticker='RE:HOME'").fetchone())
        usd = dict(c.execute("SELECT * FROM assets WHERE ticker='USD'").fetchone())
        assert portfolio.price_native(c, aapl) == (180.0, "live")
        assert portfolio.price_native(c, aapl, as_of="2024-02-05") == (180.0, "daily")
        assert portfolio.price_native(c, home) == (450000.0, "manual")
        assert portfolio.price_native(c, usd) == (1.0, "peg")


def test_expired_option_prices_to_zero(sample_portfolio):
    """Options past expiry are worthless; stale closes must not forward-fill."""
    with db.db_conn() as c:
        c.execute(
            "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
            "VALUES ('GLD260914C00393000', 'GLD 14SEP26 393 C', 'option', 'USD', 1)")
        oid = c.execute(
            "SELECT id FROM assets WHERE ticker='GLD260914C00393000'").fetchone()[0]
        c.execute("INSERT INTO prices_daily(asset_id, date, open, high, low, close, "
                  "adj_close, volume) VALUES (?, '2026-09-10', 5, 6, 5, 5.5, 5.5, 100)",
                  (oid,))
        opt = dict(c.execute("SELECT * FROM assets WHERE id=?", (oid,)).fetchone())
        # before expiry the last stored close forward-fills as usual
        assert portfolio.price_native(c, opt, as_of="2026-09-12") == (5.5, "daily")
        # past expiry (2026-09-14, parsed from the OCC ticker): always 0
        assert portfolio.price_native(c, opt, as_of="2026-09-20") == (0.0, "expired")


def test_summary_nav_and_categories(sample_portfolio):
    with db.db_conn() as c:
        s = portfolio.summary(c, "USD")
    k = s["kpis"]
    # cash: 85000 USD + 100000 HKD * 0.128 = 97800
    assert abs(k["cash_total"] - 97800) < 0.01
    # equities: 100 * 180 = 18000
    assert abs(k["equities"] - 18000) < 0.01
    # real estate: 450000 * 0.128 = 57600
    assert abs(s["categories"]["real_estate"] - 57600) < 0.01
    assert abs(k["nav"] - (97800 + 18000 + 57600)) < 0.01
    assert k["exposure"] > 0
    assert not s["fx_warnings"]


def test_summary_display_currency_conversion(sample_portfolio):
    with db.db_conn() as c:
        usd_nav = portfolio.summary(c, "USD")["kpis"]["nav"]
        hkd_nav = portfolio.summary(c, "HKD")["kpis"]["nav"]
    assert abs(hkd_nav - usd_nav / 0.128) < 0.5


def test_industry_country_pies(sample_portfolio):
    with db.db_conn() as c:
        s = portfolio.summary(c, "USD")
    assert s["industry_pie"][0]["name"] == "Consumer Electronics"
    assert s["country_pie"][0]["name"] == "United States"
    total_pct = sum(x["pct"] for x in s["industry_pie"])
    assert abs(total_pct - 100) < 0.1


def test_pies_long_vs_net_with_short_position(sample_portfolio):
    """A short must stay in the long pies of other buckets and show negative net."""
    with db.db_conn() as c:
        now = db.now_str()
        c.execute(
            "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked, "
            "industry, sector, country) VALUES ('SKHY', 'SK Hynix ADR', 'equity', 'USD', 1, "
            "'Semiconductors', 'Technology', 'South Korea')")
        skhy = c.execute("SELECT id FROM assets WHERE ticker='SKHY'").fetchone()[0]
        acc = sample_portfolio["account_id"]
        # short 50 SKHY @100: stock flows out, 5000 USD comes in
        c.execute(
            "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
            "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
            "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
            "note, created_at, updated_at) VALUES ('2024-02-10 10:00:00', ?, '', ?, 50, "
            "?, '', ?, 5000, NULL, NULL, 0, '', ?, ?)",
            (acc, skhy, acc, sample_portfolio["usd"], now, now))
        c.execute("INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source) "
                  "VALUES (?, 110.0, 108.0, 1700000000, 'test')", (skhy,))
        s = portfolio.summary(c, "USD")
    long_names = [x["name"] for x in s["industry_pie"]]
    assert "Consumer Electronics" in long_names
    assert "Semiconductors" not in long_names  # short-only bucket leaves the long pie
    net = {x["name"]: x["value"] for x in s["industry_net"]}
    assert abs(net["Semiconductors"] + 50 * 110) < 0.01  # negative net kept for the bar
    assert abs(net["Consumer Electronics"] - 18000) < 0.01
    net_cty = {x["name"]: x["value"] for x in s["country_net"]}
    assert abs(net_cty["South Korea"] + 50 * 110) < 0.01
    skhy_h = next(h for h in s["holdings"] if h["ticker"] == "SKHY")
    assert abs(skhy_h["avg_cost"] - 100.0) < 1e-9  # short: avg sell price
    assert abs(skhy_h["earning"] + 500.0) < 1e-6   # sold @100, now 110: -500


def test_avg_cost_from_transactions(sample_portfolio):
    with db.db_conn() as c:
        s = portfolio.summary(c, "USD")
    aapl = next(h for h in s["holdings"] if h["ticker"] == "AAPL")
    assert abs(aapl["avg_cost"] - 150.0) < 1e-9  # 15,000 USD / 100 shares
    assert abs(aapl["earning"] - 3000.0) < 1e-6  # 18,000 native - 15,000 cost
    home = next(h for h in s["holdings"] if h["ticker"] == "RE:HOME")
    assert abs(home["avg_cost"] - 400000.0) < 1e-9
    usd_cash = next(h for h in s["holdings"] if h["ticker"] == "USD")
    assert usd_cash["avg_cost"] is None
    assert usd_cash["earning"] is None


def _yesterday_close(c, asset_id, close):
    import datetime
    d = (datetime.date.fromisoformat(db.today_str()) - datetime.timedelta(days=1)).isoformat()
    c.execute("INSERT INTO prices_daily(asset_id, date, open, high, low, close, adj_close, "
              "volume) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
              (asset_id, d, close, close, close, close, close))
    return d


def test_day_change_pct(sample_portfolio):
    with db.db_conn() as c:
        _yesterday_close(c, sample_portfolio["aapl"], 178.0)
        s = portfolio.summary(c, "USD")
    aapl = next(h for h in s["holdings"] if h["ticker"] == "AAPL")
    assert abs(aapl["day_change_pct"] - (180 - 178) / 178 * 100) < 1e-6


def test_day_change_values(sample_portfolio):
    with db.db_conn() as c:
        _yesterday_close(c, sample_portfolio["aapl"], 178.0)
        s = portfolio.summary(c, "HKD")  # display ccy differs from AAPL's USD
    aapl = next(h for h in s["holdings"] if h["ticker"] == "AAPL")
    assert abs(aapl["day_change_native"] - (180 - 178) * 100) < 1e-6
    # USD -> HKD at 1/0.128
    assert abs(aapl["day_change_display"] - 200 / 0.128) < 1e-6
    home = next(h for h in s["holdings"] if h["ticker"] == "RE:HOME")
    assert home["day_change_native"] is None  # manual asset: no prev_close
    assert home["day_change_display"] is None


def test_day_earn_pct_follows_position_direction(sample_portfolio):
    """DAY % is the raw price move; DAY EARN % signs with the position P&L."""
    with db.db_conn() as c:
        acc = sample_portfolio["account_id"]
        usd = sample_portfolio["usd"]
        c.execute(
            "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
            "VALUES ('TSLA', 'Tesla', 'equity', 'USD', 1)")
        tsla = c.execute("SELECT id FROM assets WHERE ticker='TSLA'").fetchone()[0]
        c.execute("INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source) "
                  "VALUES (?, 110.0, 100.0, 1700000000, 'test')", (tsla,))
        now = db.now_str()
        # short 10 TSLA (opened well before today)
        c.execute(
            "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
            "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
            "inflow_asset_id, inflow_amount, fee_amount, note, created_at, updated_at) "
            "VALUES ('2024-03-01 10:00:00', ?, '', ?, 10, ?, '', ?, 1200, 0, '', ?, ?)",
            (acc, tsla, acc, usd, now, now))
        s = portfolio.summary(c, "USD")
    tsla_h = next(h for h in s["holdings"] if h["ticker"] == "TSLA")
    assert abs(tsla_h["day_change_pct"] - 10.0) < 1e-6      # price up 10%
    assert abs(tsla_h["day_change_native"] - (-100.0)) < 1e-6  # short: lost 10 x 10
    assert abs(tsla_h["day_earn_pct"] - (-10.0)) < 1e-6     # sign matches the loss


def test_day_earn_measures_from_todays_trade_price(sample_portfolio):
    """Positions bought today earn vs their actual cost, not prev_close."""
    with db.db_conn() as c:
        acc = sample_portfolio["account_id"]
        usd = sample_portfolio["usd"]
        c.execute(
            "INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
            "VALUES ('NVDA', 'Nvidia', 'equity', 'USD', 1)")
        nvda = c.execute("SELECT id FROM assets WHERE ticker='NVDA'").fetchone()[0]
        c.execute("INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source) "
                  "VALUES (?, 105.0, 90.0, 1700000000, 'test')", (nvda,))
        now = db.now_str()
        # bought 10 today at 100 (prev_close 90 is irrelevant to what was earned)
        c.execute(
            "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
            "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
            "inflow_asset_id, inflow_amount, fee_amount, note, created_at, updated_at) "
            "VALUES (?, ?, '', ?, 1000, ?, '', ?, 10, 0, '', ?, ?)",
            (db.today_str() + " 10:00:00", acc, usd, acc, nvda, now, now))
        s = portfolio.summary(c, "USD")
    nvda_h = next(h for h in s["holdings"] if h["ticker"] == "NVDA")
    assert abs(nvda_h["day_change_pct"] - (105 - 90) / 90 * 100) < 1e-6  # price move
    assert abs(nvda_h["day_change_native"] - 50.0) < 1e-6  # 10 x (105 - 100 paid)
    assert abs(nvda_h["day_earn_pct"] - 5.0) < 1e-6        # 50 / 1000 cost


def _set_quote(c, asset_id, price, prev_close, session_date):
    c.execute("UPDATE latest_quotes SET price = ?, prev_close = ?, session_date = ? "
              "WHERE asset_id = ?", (price, prev_close, session_date, asset_id))


def _aapl_holding(c):
    s = portfolio.summary(c, "USD")
    return next(h for h in s["holdings"] if h["ticker"] == "AAPL")


def test_weekend_stale_quote_uses_day_close_and_zero_change(sample_portfolio):
    """Closed market: Friday's post-market tick yields to the stored close."""
    with db.db_conn() as c:
        aapl = sample_portfolio["aapl"]
        fri = _yesterday_close(c, aapl, 179.0)
        # post-market 181 vs Yahoo's prev_close (Thursday) 178
        _set_quote(c, aapl, 181.0, 178.0, fri)
        h = _aapl_holding(c)
        a = dict(c.execute("SELECT * FROM assets WHERE id = ?", (aapl,)).fetchone())
        assert portfolio.price_native(c, a) == (179.0, "daily")
    assert h["price_native"] == 179.0
    assert abs(h["day_change_pct"]) < 1e-9
    assert abs(h["day_change_native"]) < 1e-9


def test_quote_from_today_stays_live(sample_portfolio):
    """Crypto (24/7) / futures reopen / pre-market: today's session moves count."""
    with db.db_conn() as c:
        aapl = sample_portfolio["aapl"]
        _yesterday_close(c, aapl, 179.0)
        _set_quote(c, aapl, 181.0, 170.0, db.today_str())
        h = _aapl_holding(c)
    assert h["price_native"] == 181.0
    assert abs(h["day_change_pct"] - (181 - 179) / 179 * 100) < 1e-6  # vs app close, not Yahoo
    assert abs(h["day_change_native"] - 200.0) < 1e-6


def test_stale_quote_without_stored_bar_stays_live(sample_portfolio):
    """Session bar not fetched yet (job_daily pending): never fall back to an older close."""
    with db.db_conn() as c:
        aapl = sample_portfolio["aapl"]
        _set_quote(c, aapl, 181.0, 178.0, "2025-01-01")
        assert _aapl_holding(c)["price_native"] == 181.0
        _set_quote(c, aapl, 181.0, 178.0, None)  # legacy rows without session_date
        assert _aapl_holding(c)["price_native"] == 181.0


def test_day_change_base_honours_cutoff_mode(sample_portfolio):
    with db.db_conn() as c:
        aapl = sample_portfolio["aapl"]
        d = _yesterday_close(c, aapl, 179.0)
        c.execute("UPDATE prices_daily SET ext_close = 180.5 WHERE asset_id = ? AND date = ?",
                  (aapl, d))
        _set_quote(c, aapl, 182.0, 178.0, db.today_str())
        db.set_setting(c, "day_close_mode", "cutoff")
        h = _aapl_holding(c)
    assert abs(h["day_change_native"] - (182 - 180.5) * 100) < 1e-6


def _txn(c, date, out_acc, out_ast, out_amt, in_acc, in_ast, in_amt):
    now = db.now_str()
    c.execute(
        "INSERT INTO transactions(txn_date, outflow_account_id, outflow_party, "
        "outflow_asset_id, outflow_amount, inflow_account_id, inflow_party, "
        "inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount, "
        "note, created_at, updated_at) VALUES (?, ?, '', ?, ?, ?, '', ?, ?, "
        "NULL, NULL, 0, '', ?, ?)",
        (date, out_acc, out_ast, out_amt, in_acc, in_ast, in_amt, now, now))


def test_buy_price_covers_current_cycle_only(sample_portfolio):
    """Close to zero then reopen: only the new cycle sets the buy price."""
    acc = sample_portfolio["account_id"]
    aapl, usd = sample_portfolio["aapl"], sample_portfolio["usd"]
    with db.db_conn() as c:
        _txn(c, "2024-03-01 10:00:00", acc, aapl, 100, acc, usd, 17000)  # sell all @170
        _txn(c, "2024-03-05 10:00:00", acc, usd, 10000, acc, aapl, 50)   # rebuy @200
        costs = portfolio.avg_trade_prices(c)
        assert abs(costs[(acc, aapl)]["buy"] - 200.0) < 1e-9
        # before the flat point the first cycle still applies
        old = portfolio.avg_trade_prices(c, as_of="2024-02-28")
        assert abs(old[(acc, aapl)]["buy"] - 150.0) < 1e-9
        s = portfolio.summary(c, "USD")
    aapl_h = next(h for h in s["holdings"] if h["ticker"] == "AAPL")
    assert abs(aapl_h["avg_cost"] - 200.0) < 1e-9
    assert abs(aapl_h["earning"] - (50 * 180 - 50 * 200)) < 1e-6


def test_sell_price_covers_current_short_cycle_only(sample_portfolio):
    acc, usd = sample_portfolio["account_id"], sample_portfolio["usd"]
    with db.db_conn() as c:
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('SHRT', 'Short Co', 'equity', 'USD', 1)")
        shrt = c.execute("SELECT id FROM assets WHERE ticker='SHRT'").fetchone()[0]
        _txn(c, "2024-03-01 10:00:00", acc, shrt, 10, acc, usd, 1000)  # short @100
        _txn(c, "2024-03-02 10:00:00", acc, usd, 1000, acc, shrt, 10)  # cover to 0
        _txn(c, "2024-03-03 10:00:00", acc, shrt, 5, acc, usd, 600)    # short @120
        costs = portfolio.avg_trade_prices(c)
    assert abs(costs[(acc, shrt)]["sell"] - 120.0) < 1e-9
    assert "buy" not in costs[(acc, shrt)]


def test_earning_native_and_display(sample_portfolio):
    with db.db_conn() as c:
        s = portfolio.summary(c, "USD")
    aapl = next(h for h in s["holdings"] if h["ticker"] == "AAPL")
    assert abs(aapl["earning_display"] - aapl["earning"]) < 1e-9  # USD asset in USD view
    home = next(h for h in s["holdings"] if h["ticker"] == "RE:HOME")
    assert abs(home["earning"] - 50000.0) < 1e-6                # native HKD
    assert abs(home["earning_display"] - 50000 * 0.128) < 1e-6  # converted to USD
    with db.db_conn() as c:
        s2 = portfolio.summary(c, "HKD")
    home2 = next(h for h in s2["holdings"] if h["ticker"] == "RE:HOME")
    assert abs(home2["earning_display"] - 50000.0) < 1e-6       # identity in HKD view
