"""Unit-ish tests for provider CSV parsing (Chinese broker templates)."""

from backend.importers.transactions_csv import (
    detect_provider_from_header,
    parse_transactions_csv,
    suggest_unknown_asset,
)

ASSETS = {"USD": 1, "HKD": 2, "GLDM": 3, "SKHY": 4,
          "SKHY260821P00160000": 5, "000660.KS": 6, "KRW": 7,
          "T 1 1/2 08/15/26": 8}

FT_CSV = (
    "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
    '28/7/2026,賣出,-375,WORLD GOLD TRUST SPDR GOLD MINISHARES,GLDM,現金,79.92,"29,969.38"\n'
    '28/7/2026,買進,375,WORLD GOLD TRUST SPDR GOLD MINISHARES,GLDM,現金,79.845,"-29,941.88"\n'
    '24/7/2026,賣出,-5,PUT SKHY 08/21/26 160 CLOSING CONTRACT,SKHY260821P00160000,現金,23.4,"11,699.65"\n'
    ",,,,,,,\n"
    ",,,,,,,\n"
)

IB_LEGACY_CSV = (
    "Statement,Header,域名稱,域值\n"
    "Statement,Data,Title,轉賬歷史\n"
    "轉賬歷史,Header,日期,賬戶,說明,交易類型,代碼,交易量,價格,Price Currency,總額,佣金,淨金額\n"
    "轉賬歷史,Data,31/7/2026,U***18203,SK HYNIX INC-ADR,賣,SKHY,-5,158,USD,6195.259,-0.135386014,6195.123614\n"
)

# IB Flex Query multi-section CSV (line numbers matter: rows keyed by lineno)
IB_FLEX_CSV = (
    # L1-2: per-currency cash summary (ignored)
    "ClientAccountID,CurrencyPrimary,LevelOfDetail,FromDate,ToDate,StartingCash,EndingCash\n"
    "U1,BASE_SUMMARY,BaseCurrency,25/8/2025,24/8/2026,40.75,614840.5466\n"
    # L3: trades
    "ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,DateTime,TradeDate,"
    "Buy/Sell,LevelOfDetail,Quantity,TradePrice,TradeMoney,Taxes,IBCommission,"
    "IBCommissionCurrency\n"
    "U1,KRW,STK,000660.KS,SK HYNIX INC,30/07/202600:51:01 EDT,30/7/2026,BUY,EXECUTION,"
    "20,1310000,26200000,0,-15720,KRW\n"                                          # L4
    "U1,KRW,STK,000660.KS,SK HYNIX INC,03/08/202606:27:11 EDT,3/8/2026,SELL,EXECUTION,"
    "-11,1530000,-16830000,-33660,-10098,KRW\n"                                   # L5
    "U1,USD,STK,SKHY,SK HYNIX INC-ADR,28/07/202606:28:32 EDT,28/7/2026,SELL,EXECUTION,"
    "-40,136.4,-5456,0,-1.1203136,USD\n"                                          # L6
    "U1,HKD,CASH,USD.HKD,USD.HKD,06/07/202610:40:33 EDT,6/7/2026,BUY,EXECUTION,"
    "12754,7.843,100029.622,0,-15.6862,HKD\n"                                     # L7
    "U1,KRW,CASH,USD.KRW,USD.KRW,20/08/202621:08:09 EDT,21/8/2026,SELL,EXECUTION,"
    "-42980,1387.46,-59633030.8,0,-15.682,HKD\n"                                  # L8
    "U1,USD,BOND,T 1 1/2 08/15/26,T 1 1/2 08/15/26,04/08/202607:49:17 EDT,4/8/2026,"
    "BUY,EXECUTION,70000,99.92579,69948.05,0,-5,USD\n"                            # L9
    "U1,USD,OPT,SKHY 260821P00160000,SKHY 21AUG26 160 P,24/07/202610:00:00 EDT,"
    "24/7/2026,SELL,EXECUTION,-5,23.4,-11700,0,-0.35,USD\n"                       # L10
    "U1,KRW,STK,000660.KS,SK HYNIX INC,03/08/202606:27:11 EDT,3/8/2026,SELL,SUMMARY,"
    "-11,1530000,-16830000,-33660,-10098,KRW\n"                                   # L11
    # L12: transaction taxes (duplicate detail of the Taxes column)
    "ClientAccountID,CurrencyPrimary,Symbol,TaxDescription,Quantity,TaxAmount,Date,"
    "LevelOfDetail\n"
    "U1,KRW,000660.KS,KR Securities Transaction Tax,-11,-33660,"
    "03/08/202606:27:11 EDT,SUMMARY\n"                                            # L13
    # L14: corporate actions
    "ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Report Date,Date/Time,"
    "ActionDescription,Amount,Proceeds,Value,Quantity,Code,Type\n"
    "U1,USD,BOND,T 1 1/2 08/15/26,17/8/2026,14/08/202620:25:00 EDT,"
    "(US9128282A70) FULL CALL / EARLY REDEMPTION FOR USD 1.00 PER BOND,"
    "-70000,70000,0,-70000,,BM\n"                                                 # L15
    # L16: cash transactions
    "ClientAccountID,CurrencyPrimary,Symbol,Description,Date/Time,SettleDate,Amount,"
    "Type,DividendType\n"
    "U1,USD,,USD CREDIT INT FOR JUL-2026,5/8/2026,5/8/2026,41.68,"
    "Broker Interest Received,\n"                                                 # L17
    "U1,KRW,,KRW DEBIT INT FOR JUL-2026,5/8/2026,5/8/2026,-6.56,"
    "Broker Interest Paid,\n"                                                     # L18
    "U1,KRW,,KRW CUSTODY FEE ON STK FOR 2026-08-03 FOR AUG 2026,4/8/2026,4/8/2026,"
    "-16.31,Other Fees,\n"                                                        # L19
    "U1,USD,T 1 1/2 08/15/26,BOND COUPON PAYMENT,17/8/2026,17/8/2026,525,"
    "Bond Interest Received,\n"                                                   # L20
    "U1,USD,T 1 1/2 08/15/26,PURCHASE ACCRUED INT,04/08/202607:49:17 EDT,4/8/2026,"
    "-495.99,Bond Interest Paid,\n"                                               # L21
    "U1,HKD,,CASH RECEIPTS / ELECTRONIC FUND TRANSFERS,22/03/202614:40:40 EDT,"
    "23/3/2026,10000,Deposits/Withdrawals,\n"                                     # L22
    "U1,USD,SKHY,SKHY DIVIDEND,15/8/2026,15/8/2026,12.5,Dividends,Ordinary\n"     # L23
    "U1,USD,SKHY,SKHY WHT,15/8/2026,15/8/2026,-1.88,Withholding Tax,\n"           # L24
    # L25: daily interest accruals (not cash movements)
    "ClientAccountID,CurrencyPrimary,InterestType,ReportDate,ValueDate,Rate,"
    "TotalInterest,Code\n"
    "U1,HKD,Credit Interest,1/6/2026,15/5/2026,0.236,0.21,Po\n"                   # L26
)


def test_firstrade_trades_and_option_row():
    rows = parse_transactions_csv("firstrade", FT_CSV.encode("utf-8"), 7, ASSETS)
    assert len(rows) == 3  # trailing all-empty lines dropped silently
    sell, buy, opt = rows
    assert sell.status == "ok" and sell.txn_kind == "sell"
    assert sell.payload["txn_date"] == "2026-07-28 00:00:00"
    assert sell.payload["outflow_asset_id"] == 3 and sell.payload["outflow_amount"] == 375
    assert sell.payload["inflow_asset_id"] == 1 and sell.payload["inflow_amount"] == 29969.38
    assert buy.txn_kind == "buy"
    assert buy.payload["outflow_asset_id"] == 1 and buy.payload["outflow_amount"] == 29941.88
    assert buy.payload["inflow_asset_id"] == 3 and buy.payload["inflow_amount"] == 375
    assert opt.txn_kind == "sell"
    assert opt.payload["outflow_asset_id"] == 5 and opt.payload["outflow_amount"] == 5
    assert opt.payload["inflow_amount"] == 11699.65


def test_firstrade_respects_base_ccy_override():
    rows = parse_transactions_csv("firstrade", FT_CSV.encode("utf-8"), 7, ASSETS,
                                  base_ccy="HKD")
    assert rows[0].payload["inflow_asset_id"] == 2


FT_CSV_RAW = (  # raw broker download: every field quoted, year-first dates
    '"日期","交易類別","數量","說明","代號","賬戶類別","價格","金額"\n'
    '"2026/7/16","買進","150","WORLD GOLD TRUST SPDR GOLD MINISHARES","GLDM","現金","82.01","-12,301.50"\n'
    '"2026/7/7","賣出","-10","PUT SKHY 08/21/26 160 CLOSING CONTRACT","SKHY260821P00160000","現金","30.00","29,999.18"\n'
)


def test_firstrade_raw_year_first_dates():
    rows = parse_transactions_csv("firstrade", FT_CSV_RAW.encode("utf-8"), 7, ASSETS)
    assert [r.status for r in rows] == ["ok", "ok"]
    assert rows[0].payload["txn_date"] == "2026-07-16 00:00:00"
    assert rows[1].payload["txn_date"] == "2026-07-07 00:00:00"


def test_firstrade_us_month_first_fallback():
    csv_text = ("日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
                "7/16/2026,買進,1,X,GLDM,現金,1,-1\n")
    rows = parse_transactions_csv("firstrade", csv_text.encode("utf-8"), 7, ASSETS)
    assert rows[0].status == "ok"
    assert rows[0].payload["txn_date"] == "2026-07-16 00:00:00"


def test_firstrade_interest_income_and_row_currency():
    csv_text = (
        "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
        "2026/7/2,利息收入,,FIRSTRADE CASH SWEEP INTEREST,,現金,,5.25\n"
        "2026/7/3,利息收入,,HKD DEPOSIT INTEREST,HKD,現金,,12.5\n"
    )
    rows = parse_transactions_csv("firstrade", csv_text.encode("utf-8"), 7, ASSETS,
                                  cash_tickers={"USD", "HKD", "KRW"})
    assert [r.status for r in rows] == ["ok", "ok"]
    base, hkd = rows
    assert base.txn_kind == "interest"
    assert base.payload["inflow_asset_id"] == 1 and base.payload["inflow_amount"] == 5.25
    assert base.payload["outflow_party"] == "BROKER"
    assert hkd.payload["inflow_asset_id"] == 2  # 代號 names the payout currency


def test_firstrade_missing_ticker_and_override():
    csv_text = (
        "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
        '2026/7/6,買進,150,GOLD MINISHARES,,現金,82.01,"-12,301.50"\n'
    )
    rows = parse_transactions_csv("firstrade", csv_text.encode("utf-8"), 7, ASSETS)
    assert rows[0].status == "invalid"
    assert "missing ticker" in (rows[0].error or "")
    assert rows[0].ticker == ""
    rows = parse_transactions_csv("firstrade", csv_text.encode("utf-8"), 7, ASSETS,
                                  ticker_overrides={2: "GLDM"})
    assert rows[0].status == "ok"
    assert rows[0].ticker == "GLDM"
    assert rows[0].payload["inflow_asset_id"] == 3 and rows[0].payload["inflow_amount"] == 150


def test_suggest_unknown_asset():
    opt = suggest_unknown_asset("SPCX270617P00150000")
    assert opt["asset_type"] == "option"
    d = opt["derivative"]
    assert d["underlying_ticker"] == "SPCX" and d["contract_type"] == "put"
    assert d["strike"] == 150.0 and d["expiry"] == "2027-06-17" and d["multiplier"] == 100
    assert suggest_unknown_asset("KRW")["asset_type"] == "cash"
    assert suggest_unknown_asset("000660.KS")["asset_type"] == "equity"


def test_firstrade_unknown_ticker_and_type_invalid():
    csv_text = (
        "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
        "28/7/2026,賣出,-1,X,NOPE,現金,1,1\n"
        "28/7/2026,神秘類別,1,X,GLDM,現金,1,1\n"
    )
    rows = parse_transactions_csv("firstrade", csv_text.encode("utf-8"), 7, ASSETS)
    assert rows[0].status == "invalid" and "unknown ticker" in rows[0].error.lower()
    assert rows[1].status == "invalid" and "unsupported" in rows[1].error.lower()


def _flex_rows_by_line(rows):
    return {r.row: r for r in rows}


def test_ib_flex_stock_trades_fee_in_own_currency():
    rows = _flex_rows_by_line(
        parse_transactions_csv("ib", IB_FLEX_CSV.encode("utf-8"), 9, ASSETS))
    buy = rows[4]
    assert buy.status == "ok" and buy.txn_kind == "buy"
    assert buy.payload["txn_date"] == "2026-07-30 00:51:01"  # glued flex datetime
    assert buy.payload["outflow_asset_id"] == 7 and buy.payload["outflow_amount"] == 26200000
    assert buy.payload["inflow_asset_id"] == 6 and buy.payload["inflow_amount"] == 20
    assert buy.payload["fee_asset_id"] == 7 and buy.payload["fee_amount"] == 15720
    sell = rows[5]  # KR securities tax folds into the cash leg
    assert sell.txn_kind == "sell"
    assert sell.payload["outflow_asset_id"] == 6 and sell.payload["outflow_amount"] == 11
    assert sell.payload["inflow_asset_id"] == 7
    assert sell.payload["inflow_amount"] == 16830000 - 33660
    assert sell.payload["fee_asset_id"] == 7 and sell.payload["fee_amount"] == 10098
    usd_sell = rows[6]  # NASDAQ commission stays in USD, not statement base
    assert usd_sell.payload["inflow_asset_id"] == 1 and usd_sell.payload["inflow_amount"] == 5456
    assert usd_sell.payload["fee_asset_id"] == 1
    assert abs(usd_sell.payload["fee_amount"] - 1.1203136) < 1e-9


def test_ib_flex_fx_pair_trades():
    rows = _flex_rows_by_line(
        parse_transactions_csv("ib", IB_FLEX_CSV.encode("utf-8"), 9, ASSETS))
    buy = rows[7]  # bought USD with HKD
    assert buy.txn_kind == "fx"
    assert buy.payload["inflow_asset_id"] == 1 and buy.payload["inflow_amount"] == 12754
    assert buy.payload["outflow_asset_id"] == 2
    assert abs(buy.payload["outflow_amount"] - 100029.622) < 1e-6
    assert buy.payload["fee_asset_id"] == 2
    assert abs(buy.payload["fee_amount"] - 15.6862) < 1e-9
    sell = rows[8]  # sold USD into KRW; commission still charged in HKD
    assert sell.payload["outflow_asset_id"] == 1 and sell.payload["outflow_amount"] == 42980
    assert sell.payload["inflow_asset_id"] == 7
    assert abs(sell.payload["inflow_amount"] - 59633030.8) < 1e-6
    assert sell.payload["fee_asset_id"] == 2


def test_ib_flex_bond_trade_redemption_and_interest():
    rows = _flex_rows_by_line(
        parse_transactions_csv("ib", IB_FLEX_CSV.encode("utf-8"), 9, ASSETS))
    buy = rows[9]  # qty = face value; cash = TradeMoney (per-100 quote already applied)
    assert buy.payload["outflow_asset_id"] == 1
    assert abs(buy.payload["outflow_amount"] - 69948.05) < 1e-6
    assert buy.payload["inflow_asset_id"] == 8 and buy.payload["inflow_amount"] == 70000
    assert buy.payload["fee_amount"] == 5
    assert buy.price_info == {"ticker": "T 1 1/2 08/15/26", "date": "2026-08-04",
                              "price": 0.9992579}
    assert buy.asset_hint == {"asset_type": "bond", "currency": "USD",
                              "is_yahoo_tracked": False}
    redemption = rows[15]
    assert redemption.txn_kind == "redemption" and redemption.status == "ok"
    assert redemption.payload["outflow_asset_id"] == 8
    assert redemption.payload["outflow_amount"] == 70000
    assert redemption.payload["inflow_asset_id"] == 1
    assert redemption.payload["inflow_amount"] == 70000
    assert redemption.price_info["price"] == 1.0
    assert redemption.price_info["date"] == "2026-08-14"
    coupon = rows[20]  # bond coupon / purchase accrued int are plain cash flows
    assert coupon.txn_kind == "interest"
    assert coupon.payload["inflow_asset_id"] == 1 and coupon.payload["inflow_amount"] == 525
    accrued = rows[21]
    assert accrued.payload["outflow_asset_id"] == 1
    assert abs(accrued.payload["outflow_amount"] - 495.99) < 1e-9
    assert accrued.payload["inflow_party"] == "BROKER"


def test_ib_flex_option_symbol_unpadded():
    rows = _flex_rows_by_line(
        parse_transactions_csv("ib", IB_FLEX_CSV.encode("utf-8"), 9, ASSETS))
    opt = rows[10]
    assert opt.status == "ok" and opt.ticker == "SKHY260821P00160000"
    assert opt.payload["outflow_asset_id"] == 5 and opt.payload["outflow_amount"] == 5
    assert opt.payload["inflow_asset_id"] == 1 and opt.payload["inflow_amount"] == 11700


def test_ib_flex_cash_transactions():
    rows = _flex_rows_by_line(
        parse_transactions_csv("ib", IB_FLEX_CSV.encode("utf-8"), 9, ASSETS))
    credit = rows[17]  # periodic posting imports as-is (no daily deduction)
    assert credit.txn_kind == "interest"
    assert credit.payload["inflow_asset_id"] == 1 and credit.payload["inflow_amount"] == 41.68
    assert credit.payload["outflow_party"] == "BROKER"
    debit = rows[18]
    assert debit.payload["outflow_asset_id"] == 7 and debit.payload["outflow_amount"] == 6.56
    fee = rows[19]
    assert fee.txn_kind == "fee"
    assert fee.payload["outflow_asset_id"] == 7 and fee.payload["outflow_amount"] == 16.31
    dividend = rows[23]
    assert dividend.txn_kind == "dividend" and dividend.payload["inflow_amount"] == 12.5
    wht = rows[24]
    assert wht.txn_kind == "tax"
    assert wht.payload["outflow_asset_id"] == 1 and wht.payload["outflow_amount"] == 1.88


def test_ib_flex_booktrade_assignment_zero_cash():
    """Option assignment BookTrade rows carry TradeMoney=0: the position leg
    still imports (asset vs BROKER), no cash counterleg."""
    csv = (
        "ClientAccountID,CurrencyPrimary,AssetClass,Symbol,Description,DateTime,"
        "TradeDate,TransactionType,Buy/Sell,LevelOfDetail,Quantity,TradePrice,"
        "TradeMoney,Taxes,IBCommission,IBCommissionCurrency\n"
        # assigned short put closed at zero: no cash moves on the option leg
        "U1,USD,OPT,SKHY 260821P00160000,SKHY 21AUG26 160 P,21/08/202616:20:00 EDT,"
        "21/8/2026,BookTrade,BUY,EXECUTION,5,0,0,0,0,USD\n"
        # matching stock delivery has cash (strike x qty): normal two-leg trade
        "U1,USD,STK,SKHY,SK HYNIX INC-ADR,21/08/202616:20:00 EDT,21/8/2026,"
        "BookTrade,BUY,EXECUTION,500,160,80000,0,0,USD\n"
        # zero cash on a NON-BookTrade row is still an error
        "U1,USD,STK,SKHY,SK HYNIX INC-ADR,21/08/202616:20:00 EDT,21/8/2026,"
        "ExchTrade,BUY,EXECUTION,10,0,0,0,0,USD\n"
    )
    opt, stk, bad = parse_transactions_csv("ib", csv.encode("utf-8"), 9, ASSETS)
    assert opt.status == "ok" and opt.txn_kind == "buy"
    assert opt.payload["inflow_asset_id"] == 5 and opt.payload["inflow_amount"] == 5
    assert opt.payload["outflow_asset_id"] is None
    assert opt.payload["outflow_party"] == "BROKER"
    assert stk.status == "ok"
    assert stk.payload["outflow_asset_id"] == 1 and stk.payload["outflow_amount"] == 80000
    assert stk.payload["inflow_asset_id"] == 4 and stk.payload["inflow_amount"] == 500
    assert bad.status == "invalid" and "missing trade amount" in (bad.error or "")


def test_ib_flex_skipped_rows():
    rows = _flex_rows_by_line(
        parse_transactions_csv("ib", IB_FLEX_CSV.encode("utf-8"), 9, ASSETS))
    assert len(rows) == 19  # summary section emits nothing
    assert rows[11].status == "skipped"  # non-execution duplicate detail
    assert "non-execution" in rows[11].error
    assert rows[13].status == "skipped"  # tax detail already in the trade cash leg
    assert rows[13].txn_kind == "tax"
    assert rows[22].status == "skipped" and rows[22].txn_kind == "transfer"
    assert rows[26].status == "skipped"  # daily accrual, cash posts periodically
    assert "accrual" in rows[26].error


def test_ib_flex_unknown_bond_carries_hint():
    assets = {"USD": 1, "HKD": 2, "KRW": 7, "000660.KS": 6, "SKHY": 4,
              "SKHY260821P00160000": 5}
    rows = _flex_rows_by_line(
        parse_transactions_csv("ib", IB_FLEX_CSV.encode("utf-8"), 9, assets))
    bond_buy = rows[9]
    assert bond_buy.status == "invalid"
    assert "unknown ticker: T 1 1/2 08/15/26" in bond_buy.error
    assert bond_buy.asset_hint == {"asset_type": "bond", "currency": "USD",
                                   "is_yahoo_tracked": False}


def test_ib_legacy_format_rejected():
    try:
        parse_transactions_csv("ib", IB_LEGACY_CSV.encode("utf-8"), 9, ASSETS)
    except ValueError as exc:
        assert "Flex Query" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_provider_guard():
    try:
        parse_transactions_csv("x", "日期,交易類別\n".encode("utf-8"), 1, {"USD": 1})
    except ValueError as exc:
        assert "provider" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_detect_provider_from_header():
    assert detect_provider_from_header(FT_CSV.encode("utf-8")) == "firstrade"
    assert detect_provider_from_header(IB_FLEX_CSV.encode("utf-8")) == "ib"
    assert detect_provider_from_header(IB_LEGACY_CSV.encode("utf-8")) is None
    assert detect_provider_from_header(b"a,b,c\n1,2,3\n") is None
