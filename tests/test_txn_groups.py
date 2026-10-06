"""Transaction split (pro-rata) and manual grouping with P&L summary."""
import pytest

from backend import db


def _txn_by_note(client, note):
    rows = client.get("/api/transactions?q=" + note).get_json()
    assert rows, f"no transaction with note {note}"
    return rows[0]


@pytest.fixture()
def aapl_buy(client, sample_portfolio):
    """The fixture's buy: -15000 USD / +100 AAPL inside the same account."""
    rows = client.get("/api/transactions").get_json()
    return next(t for t in rows if t["inflow_ticker"] == "AAPL")


def test_split_pro_rata_amounts(client, sample_portfolio, aapl_buy):
    res = client.post(f"/api/transactions/{aapl_buy['id']}/split",
                      json={"leg": "inflow", "quantity": 40})
    assert res.status_code == 200
    body = res.get_json()
    rows = client.get("/api/transactions").get_json()
    orig = next(t for t in rows if t["id"] == body["original_id"])
    new = next(t for t in rows if t["id"] == body["new_id"])
    assert new["inflow_amount"] == pytest.approx(40)
    assert new["outflow_amount"] == pytest.approx(6000)      # 40% of 15000
    assert orig["inflow_amount"] == pytest.approx(60)
    assert orig["outflow_amount"] == pytest.approx(9000)
    assert new["txn_date"] == orig["txn_date"]
    # totals preserved exactly
    assert new["inflow_amount"] + orig["inflow_amount"] == pytest.approx(100)
    assert new["outflow_amount"] + orig["outflow_amount"] == pytest.approx(15000)


def test_split_prorates_fee(client, sample_portfolio, aapl_buy):
    usd = sample_portfolio["usd"]
    client.put(f"/api/transactions/{aapl_buy['id']}",
               json={"fee_asset_id": usd, "fee_amount": 10})
    res = client.post(f"/api/transactions/{aapl_buy['id']}/split",
                      json={"leg": "inflow", "quantity": 25})
    body = res.get_json()
    rows = client.get("/api/transactions").get_json()
    orig = next(t for t in rows if t["id"] == body["original_id"])
    new = next(t for t in rows if t["id"] == body["new_id"])
    assert new["fee_amount"] == pytest.approx(2.5)
    assert orig["fee_amount"] == pytest.approx(7.5)


def test_split_validation(client, sample_portfolio, aapl_buy):
    tid = aapl_buy["id"]
    assert client.post(f"/api/transactions/{tid}/split",
                       json={"leg": "inflow", "quantity": 100}).status_code == 400
    assert client.post(f"/api/transactions/{tid}/split",
                       json={"leg": "inflow", "quantity": 0}).status_code == 400
    assert client.post(f"/api/transactions/{tid}/split",
                       json={"leg": "sideways", "quantity": 10}).status_code == 400
    assert client.post("/api/transactions/999999/split",
                       json={"leg": "inflow", "quantity": 1}).status_code == 404


def test_group_crud_assign_unassign(client, sample_portfolio, aapl_buy):
    created = client.post("/api/txn-groups", json={"name": "PAIR A"})
    assert created.status_code == 201
    gid = created.get_json()["id"]

    assert client.post(f"/api/txn-groups/{gid}/assign",
                       json={"txn_ids": [aapl_buy["id"]]}).status_code == 200
    rows = client.get("/api/transactions").get_json()
    tagged = next(t for t in rows if t["id"] == aapl_buy["id"])
    assert tagged["group_id"] == gid and tagged["group_name"] == "PAIR A"

    groups = client.get("/api/txn-groups").get_json()
    assert groups[0]["txn_count"] == 1

    assert client.put(f"/api/txn-groups/{gid}", json={"name": "PAIR B"}).status_code == 200
    assert client.get("/api/txn-groups").get_json()[0]["name"] == "PAIR B"

    assert client.post("/api/txn-groups/unassign",
                       json={"txn_ids": [aapl_buy["id"]]}).status_code == 200
    rows = client.get("/api/transactions").get_json()
    assert next(t for t in rows if t["id"] == aapl_buy["id"])["group_id"] is None

    assert client.post(f"/api/txn-groups/{gid}/assign",
                       json={"txn_ids": []}).status_code == 400
    assert client.post("/api/txn-groups/99999/assign",
                       json={"txn_ids": [1]}).status_code == 404


def test_group_delete_keeps_transactions(client, sample_portfolio, aapl_buy):
    gid = client.post("/api/txn-groups", json={"name": "TEMP"}).get_json()["id"]
    client.post(f"/api/txn-groups/{gid}/assign", json={"txn_ids": [aapl_buy["id"]]})
    assert client.delete(f"/api/txn-groups/{gid}").status_code == 200
    rows = client.get("/api/transactions").get_json()
    survivor = next(t for t in rows if t["id"] == aapl_buy["id"])
    assert survivor["group_id"] is None


def test_group_summary_realized_plus_open(client, sample_portfolio, aapl_buy):
    """Buy 100 AAPL @150, sell 50 @190: cash net -5500, open 50 x 180 = 9000."""
    acc = sample_portfolio["account_id"]
    usd, aapl = sample_portfolio["usd"], sample_portfolio["aapl"]
    sell = client.post("/api/transactions", json={
        "txn_date": "2024-03-01 10:00:00",
        "outflow_account_id": acc, "outflow_asset_id": aapl, "outflow_amount": 50,
        "inflow_account_id": acc, "inflow_asset_id": usd, "inflow_amount": 9500,
    })
    assert sell.status_code == 201
    gid = client.post("/api/txn-groups", json={"name": "AAPL TRADE"}).get_json()["id"]
    client.post(f"/api/txn-groups/{gid}/assign",
                json={"txn_ids": [aapl_buy["id"], sell.get_json()["id"]]})

    data = client.get("/api/txn-groups/summary?ccy=USD").get_json()
    g = next(x for x in data["groups"] if x["id"] == gid)
    assert g["txn_count"] == 2 and len(g["members"]) == 2
    by_ticker = {a["ticker"]: a for a in g["assets"]}
    assert by_ticker["AAPL"]["net_qty"] == pytest.approx(50)
    assert by_ticker["USD"]["net_qty"] == pytest.approx(-5500)
    assert g["cash_display"] == pytest.approx(-5500)
    assert g["open_value_display"] == pytest.approx(50 * 180.0)  # latest quote
    assert g["total_display"] == pytest.approx(3500)
    # absolute exposure: open legs only, cash excluded
    assert g["abs_value_display"] == pytest.approx(9000)
    # cash deployed for the open 50 shares at avg entry 150, realized P&L stripped
    assert g["net_cash_ex_pnl_display"] == pytest.approx(-7500)


def test_group_timeseries(client, sample_portfolio, aapl_buy):
    """Daily P&L series: cash flows + open position at each day's known price."""
    acc = sample_portfolio["account_id"]
    usd, aapl = sample_portfolio["usd"], sample_portfolio["aapl"]
    sell = client.post("/api/transactions", json={
        "txn_date": "2024-03-01 10:00:00",
        "outflow_account_id": acc, "outflow_asset_id": aapl, "outflow_amount": 50,
        "inflow_account_id": acc, "inflow_asset_id": usd, "inflow_amount": 9500,
    }).get_json()
    gid = client.post("/api/txn-groups", json={"name": "TS"}).get_json()["id"]
    client.post(f"/api/txn-groups/{gid}/assign",
                json={"txn_ids": [aapl_buy["id"], sell["id"]]})

    ts = client.get(f"/api/txn-groups/{gid}/timeseries?ccy=USD").get_json()
    assert ts["dates"][0] == "2024-01-10"           # first member txn
    assert ts["dates"][-1] == db.today_str()
    i = ts["dates"].index("2024-02-02")             # first day with an AAPL close
    assert ts["pnl"][i] == pytest.approx(3000)      # -15000 cash + 100 x 180
    assert ts["cash"][-1] == pytest.approx(-5500)   # -15000 + 9500
    assert ts["open_value"][-1] == pytest.approx(9000)  # 50 x 180 (daily close)
    assert ts["pnl"][-1] == pytest.approx(3500)
    leg = next(x for x in ts["legs"] if x["ticker"] == "AAPL")
    assert leg["qty"][0] == pytest.approx(100)
    assert leg["qty"][-1] == pytest.approx(50)
    assert leg["currency"] == "USD"
    assert leg["value_native"][i] == pytest.approx(18000)   # 100 x 180
    assert leg["value_native"][-1] == pytest.approx(9000)   # 50 x 180
    assert leg["value_display"][-1] == pytest.approx(9000)  # display ccy = USD

    assert client.get("/api/txn-groups/999/timeseries").status_code == 404
    empty_gid = client.post("/api/txn-groups", json={"name": "EMPTY"}).get_json()["id"]
    empty = client.get(f"/api/txn-groups/{empty_gid}/timeseries").get_json()
    assert empty["dates"] == [] and empty["pnl"] == [] and empty["legs"] == []


def test_group_timeseries_live_price_for_today(client, sample_portfolio):
    """A leg with a live quote but no daily bars is valued at the quote today
    instead of dropping to zero."""
    acc = sample_portfolio["account_id"]
    usd = sample_portfolio["usd"]
    with db.db_conn() as c:
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
                  "VALUES ('000660.KS', 'SK Hynix', 'equity', 'USD', 1)")
        hynix = c.execute("SELECT id FROM assets WHERE ticker='000660.KS'").fetchone()[0]
        c.execute("INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source) "
                  "VALUES (?, 200.0, 195.0, 1700000000, 'test')", (hynix,))
    buy = client.post("/api/transactions", json={
        "txn_date": "2024-03-05 10:00:00",
        "outflow_account_id": acc, "outflow_asset_id": usd, "outflow_amount": 1000,
        "inflow_account_id": acc, "inflow_asset_id": hynix, "inflow_amount": 5,
    }).get_json()
    gid = client.post("/api/txn-groups", json={"name": "LIVE"}).get_json()["id"]
    client.post(f"/api/txn-groups/{gid}/assign", json={"txn_ids": [buy["id"]]})

    ts = client.get(f"/api/txn-groups/{gid}/timeseries?ccy=USD").get_json()
    leg = next(x for x in ts["legs"] if x["ticker"] == "000660.KS")
    assert leg["value_native"][0] == pytest.approx(0)         # no history at all
    assert leg["value_native"][-1] == pytest.approx(5 * 200)  # live quote today
    assert ts["pnl"][-1] == pytest.approx(-1000 + 1000)


# ---------------------------------------------------------- group types ----

def test_group_type_crud(client, sample_portfolio):
    res = client.post("/api/txn-groups", json={"name": "P1", "group_type": "pair"})
    assert res.status_code == 201 and res.get_json()["group_type"] == "pair"
    gid = res.get_json()["id"]
    assert client.get("/api/txn-groups").get_json()[0]["group_type"] == "pair"

    assert client.put(f"/api/txn-groups/{gid}", json={"group_type": "hedge"}).status_code == 200
    assert client.get("/api/txn-groups").get_json()[0]["group_type"] == "hedge"
    # type untouched when not sent
    assert client.put(f"/api/txn-groups/{gid}", json={"name": "P2"}).status_code == 200
    assert client.get("/api/txn-groups").get_json()[0]["group_type"] == "hedge"

    assert client.post("/api/txn-groups",
                       json={"name": "BAD", "group_type": "bogus"}).status_code == 400
    assert client.put(f"/api/txn-groups/{gid}", json={"group_type": "bogus"}).status_code == 400

    data = client.get("/api/txn-groups/summary?ccy=USD").get_json()
    assert next(x for x in data["groups"] if x["id"] == gid)["group_type"] == "hedge"


# -------------------------------------------------------- from-positions ----

def _add_asset(ticker, price):
    """Insert a quoted equity directly (avoids the Yahoo validation in the API)."""
    with db.db_conn() as c:
        c.execute("INSERT INTO assets(ticker, name, asset_type, currency, is_yahoo_tracked) "
                  "VALUES (?, ?, 'equity', 'USD', 1)", (ticker, ticker))
        aid = c.execute("SELECT id FROM assets WHERE ticker = ?", (ticker,)).fetchone()[0]
        c.execute("INSERT INTO latest_quotes(asset_id, price, prev_close, ts, source) "
                  "VALUES (?, ?, ?, 1700000000, 'test')", (aid, price, price))
    return aid


def test_from_positions_assigns_current_cycle_only(client, sample_portfolio):
    acc, usd = sample_portfolio["account_id"], sample_portfolio["usd"]
    cyc = _add_asset("CYC", 120.0)
    mk = lambda body: client.post("/api/transactions", json=body).get_json()["id"]
    mk({"txn_date": "2024-01-05 10:00:00", "outflow_account_id": acc, "outflow_asset_id": usd,
        "outflow_amount": 1000, "inflow_account_id": acc, "inflow_asset_id": cyc, "inflow_amount": 10})
    mk({"txn_date": "2024-01-06 10:00:00", "outflow_account_id": acc, "outflow_asset_id": cyc,
        "outflow_amount": 10, "inflow_account_id": acc, "inflow_asset_id": usd, "inflow_amount": 1100})
    reopen = mk({"txn_date": "2024-01-07 10:00:00", "outflow_account_id": acc, "outflow_asset_id": usd,
                 "outflow_amount": 2400, "inflow_account_id": acc, "inflow_asset_id": cyc, "inflow_amount": 20})

    res = client.post("/api/txn-groups/from-positions", json={
        "name": "CYC STRAT", "group_type": "custom",
        "positions": [{"asset_id": cyc, "account_id": acc}]})
    assert res.status_code == 201
    body = res.get_json()
    assert body["assigned"] == 1 and body["skipped"] == [] and body["no_open_position"] == []
    members = client.get(f"/api/transactions?group_id={body['id']}").get_json()
    assert [t["id"] for t in members] == [reopen]  # closed round trip stays out


def test_from_positions_skips_and_reports_foreign_group(client, sample_portfolio, aapl_buy):
    acc, aapl = sample_portfolio["account_id"], sample_portfolio["aapl"]
    other = client.post("/api/txn-groups", json={"name": "OTHER"}).get_json()["id"]
    client.post(f"/api/txn-groups/{other}/assign", json={"txn_ids": [aapl_buy["id"]]})

    res = client.post("/api/txn-groups/from-positions", json={
        "name": "NEW", "positions": [{"asset_id": aapl, "account_id": acc}]})
    assert res.status_code == 201
    body = res.get_json()
    assert body["assigned"] == 0
    assert body["skipped"] == [{"txn_id": aapl_buy["id"], "group_id": other, "group_name": "OTHER"}]
    # original membership untouched
    rows = client.get("/api/transactions").get_json()
    assert next(t for t in rows if t["id"] == aapl_buy["id"])["group_id"] == other


def test_from_positions_add_to_existing_and_validation(client, sample_portfolio):
    acc, aapl = sample_portfolio["account_id"], sample_portfolio["aapl"]
    gid = client.post("/api/txn-groups", json={"name": "EXIST"}).get_json()["id"]
    res = client.post("/api/txn-groups/from-positions", json={
        "group_id": gid, "positions": [{"asset_id": aapl, "account_id": acc}]})
    assert res.status_code == 200 and res.get_json()["assigned"] == 1

    ghost = _add_asset("GHOST", 1.0)  # no transactions -> no open position
    res = client.post("/api/txn-groups/from-positions", json={
        "group_id": gid, "positions": [{"asset_id": ghost, "account_id": acc}]})
    assert res.get_json()["no_open_position"] == [{"account_id": acc, "asset_id": ghost}]

    assert client.post("/api/txn-groups/from-positions", json={"positions": []}).status_code == 400
    assert client.post("/api/txn-groups/from-positions", json={
        "positions": [{"asset_id": aapl, "account_id": acc}]}).status_code == 400  # name missing
    assert client.post("/api/txn-groups/from-positions", json={
        "group_id": 99999, "positions": [{"asset_id": aapl, "account_id": acc}]}).status_code == 404


# ------------------------------------------------- summary strategy groups --

def test_dashboard_summary_pair_group_metrics(client, sample_portfolio):
    """Long 100 AAPL @150 (now 180) vs short 50 SHT @190 (now 200)."""
    acc, usd, aapl = sample_portfolio["account_id"], sample_portfolio["usd"], sample_portfolio["aapl"]
    sht = _add_asset("SHT", 200.0)
    client.post("/api/transactions", json={
        "txn_date": "2024-01-20 10:00:00",
        "outflow_account_id": acc, "outflow_asset_id": sht, "outflow_amount": 50,
        "inflow_account_id": acc, "inflow_asset_id": usd, "inflow_amount": 9500})
    res = client.post("/api/txn-groups/from-positions", json={
        "name": "A/S PAIR", "group_type": "pair",
        "positions": [{"asset_id": aapl, "account_id": acc},
                      {"asset_id": sht, "account_id": acc}]})
    assert res.status_code == 201 and res.get_json()["assigned"] == 2

    data = client.get("/api/dashboard/summary?ccy=USD").get_json()
    g = next(x for x in data["txn_groups"] if x["name"] == "A/S PAIR")
    assert g["group_type"] == "pair" and g["txn_count"] == 2
    legs = {leg["ticker"]: leg for leg in g["legs"]}
    assert legs["AAPL"]["qty"] == pytest.approx(100)
    assert legs["AAPL"]["entry_px"] == pytest.approx(150)
    assert legs["SHT"]["qty"] == pytest.approx(-50)
    assert legs["SHT"]["entry_px"] == pytest.approx(190)
    assert legs["SHT"]["earning_display"] == pytest.approx(-500)  # short 190 -> 200

    assert g["entry_cost_display"] == pytest.approx(150 * 100 - 190 * 50)
    assert g["open_value_display"] == pytest.approx(180 * 100 - 200 * 50)
    assert g["cash_display"] == pytest.approx(-15000 + 9500)
    assert g["unrealized_display"] == pytest.approx(3000 - 500)
    assert g["total_display"] == pytest.approx(g["cash_display"] + g["open_value_display"])
    assert g["pair"]["a_ticker"] == "AAPL" and g["pair"]["b_ticker"] == "SHT"
    assert g["pair"]["entry_ratio"] == pytest.approx(150 / 190)
    assert g["pair"]["current_ratio"] == pytest.approx(180 / 200)

    # holdings rows are tagged with their group memberships
    hold = next(h for h in data["holdings"] if h["ticker"] == "AAPL")
    assert hold["groups"][0]["name"] == "A/S PAIR"
    assert hold["groups"][0]["qty"] == pytest.approx(100)
    cashrow = next(h for h in data["holdings"] if h["ticker"] == "USD")
    assert cashrow["groups"] == []  # cash legs stay untagged


def test_group_id_survives_transaction_update(client, sample_portfolio, aapl_buy):
    gid = client.post("/api/txn-groups", json={"name": "KEEP"}).get_json()["id"]
    client.post(f"/api/txn-groups/{gid}/assign", json={"txn_ids": [aapl_buy["id"]]})
    client.put(f"/api/transactions/{aapl_buy['id']}", json={"note": "edited"})
    rows = client.get("/api/transactions?q=edited").get_json()
    assert rows and rows[0]["group_id"] == gid


def test_split_keeps_group(client, sample_portfolio, aapl_buy):
    gid = client.post("/api/txn-groups", json={"name": "SPLITGRP"}).get_json()["id"]
    client.post(f"/api/txn-groups/{gid}/assign", json={"txn_ids": [aapl_buy["id"]]})
    body = client.post(f"/api/transactions/{aapl_buy['id']}/split",
                       json={"leg": "inflow", "quantity": 30}).get_json()
    rows = client.get("/api/transactions").get_json()
    assert next(t for t in rows if t["id"] == body["new_id"])["group_id"] == gid
    assert next(t for t in rows if t["id"] == body["original_id"])["group_id"] == gid
