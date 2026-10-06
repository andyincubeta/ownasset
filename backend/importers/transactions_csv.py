"""Provider CSV parsers for transaction import preview/commit.

Parsed in memory (UTF-8, BOM tolerated); uploads are never persisted to disk.
  * firstrade: flat Chinese-template CSV - 日期,交易類別,數量,說明,代號,賬戶類別,價格,金額
  * ib: Flex Query CSV - multiple sections, each opened by a ClientAccountID
    header row. Trades (stocks, FX pairs, bonds, options), corporate-action
    bond redemptions and cash transactions (dividends, periodic interest,
    fees, withholding tax) are imported. Per-trade exchange-tax detail and
    daily interest accruals surface as skipped rows (informational only).
Deposits/withdrawals (transfers in/out) are reported as skipped rows.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.option_archive import OCC_OPTION_RE

# fallback cash currency per provider (overridable via settings import_ccy_*)
PROVIDER_DEFAULT_CCY = {"firstrade": "USD", "ib": "HKD"}

# 交易類別 (firstrade) -> normalized kind
FT_KINDS = {"買進": "buy", "賣出": "sell", "股息": "dividend", "現金股息": "dividend",
            "利息": "interest", "利息收入": "interest"}
FT_SKIP_KINDS = {"轉入", "轉出", "存入", "提出", "存款", "提款"}

# IB Flex cash-transaction Type -> normalized kind
IB_CASH_KINDS = {
    "Dividends": "dividend",
    "Payment In Lieu Of Dividends": "dividend",
    "Withholding Tax": "tax",
    "Broker Interest Received": "interest",
    "Broker Interest Paid": "interest",
    "Bond Interest Received": "interest",
    "Bond Interest Paid": "interest",
    "Other Fees": "fee",
    "Broker Fees": "fee",
    "Commission Adjustments": "adjustment",
}
IB_CASH_SKIP_TYPES = {"Deposits/Withdrawals"}

# flex DateTime glues date and time together: '30/07/202600:51:01 EDT'
IB_FLEX_DT_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4})(\d{2}:\d{2}:\d{2})(?:\s+\S+)?$")


@dataclass
class ImportRow:
    row: int
    status: str  # ok | invalid | skipped
    txn_kind: str
    payload: dict[str, Any] | None
    error: str | None = None
    ticker: str = ""  # raw symbol column (post-override) so the UI can edit it
    asset_hint: dict[str, Any] | None = None  # provider-known unknown-ticker suggestion
    price_info: dict[str, Any] | None = None  # bond price seed {ticker, date, price}


def parse_transactions_csv(provider: str, raw_bytes: bytes, account_id: int,
                           asset_by_ticker: dict[str, int],
                           base_ccy: str | None = None,
                           cash_tickers: set[str] | None = None,
                           ticker_overrides: dict[int, str] | None = None) -> list[ImportRow]:
    if provider not in PROVIDER_DEFAULT_CCY:
        raise ValueError("provider must be firstrade or ib")
    ccy = _norm_ticker(base_ccy) or PROVIDER_DEFAULT_CCY[provider]
    cash = cash_tickers or set()
    overrides = {int(k): _norm_ticker(v) for k, v in (ticker_overrides or {}).items()}
    if provider == "firstrade":
        return _parse_firstrade(raw_bytes, account_id, asset_by_ticker, ccy, cash, overrides)
    return _parse_ib(raw_bytes, account_id, asset_by_ticker, ccy, overrides)


def _decode_text(raw_bytes: bytes) -> str:
    return raw_bytes.decode("utf-8-sig", errors="replace")


def _parse_num(s: str) -> float:
    cleaned = (s or "").strip().replace(",", "")
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)


def _norm_ticker(s: str) -> str:
    return (s or "").strip().upper()


def _lookup_asset(asset_by_ticker: dict[str, int], ticker: str) -> int:
    aid = asset_by_ticker.get(_norm_ticker(ticker))
    if not aid:
        raise ValueError(f"unknown ticker: {ticker}")
    return aid


def _parse_date_dmy(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        raise ValueError("missing date")
    m = IB_FLEX_DT_RE.match(s)
    if m:
        s = f"{m.group(1)} {m.group(2)}"
    # raw broker download is year-first (2026/7/16); an Excel re-save flips it
    # to locale order: day-first preferred, month-first as fallback (7/16/2026)
    if re.match(r"^\d{4}[/-]", s):
        fmts = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d", "%Y/%m/%d %H:%M:%S")
    else:
        fmts = ("%d/%m/%Y", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y", "%m/%d/%Y %H:%M:%S")
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"invalid date: {raw}")


def _base_payload(txn_date: str, account_id: int, cash_id: int, note: str) -> dict[str, Any]:
    return {
        "txn_date": txn_date,
        "outflow_account_id": None, "outflow_party": "",
        "outflow_asset_id": None, "outflow_amount": 0.0,
        "inflow_account_id": None, "inflow_party": "",
        "inflow_asset_id": None, "inflow_amount": 0.0,
        "fee_account_id": account_id, "fee_asset_id": cash_id, "fee_amount": 0.0,
        "note": note,
    }


def _cash_flow(payload: dict[str, Any], account_id: int, cash_id: int, amount: float) -> None:
    """Signed broker cash movement: positive = income, negative = expense."""
    if amount >= 0:
        payload.update({"outflow_party": "BROKER", "inflow_account_id": account_id,
                        "inflow_asset_id": cash_id, "inflow_amount": abs(amount)})
    else:
        payload.update({"inflow_party": "BROKER", "outflow_account_id": account_id,
                        "outflow_asset_id": cash_id, "outflow_amount": abs(amount)})


def _trade_legs(payload: dict[str, Any], kind: str, account_id: int, asset_id: int,
                qty: float, cash_id: int, cash_amt: float,
                allow_zero_cash: bool = False) -> None:
    if qty <= 0:
        raise ValueError("missing quantity for trade row")
    if cash_amt <= 0:
        if not (allow_zero_cash and cash_amt == 0):
            raise ValueError("missing trade amount")
        # zero-cash book trade (e.g. option assignment): asset moves, no cash leg
        if kind == "buy":
            payload.update({"outflow_party": "BROKER",
                            "inflow_account_id": account_id,
                            "inflow_asset_id": asset_id, "inflow_amount": qty})
        else:
            payload.update({"inflow_party": "BROKER",
                            "outflow_account_id": account_id,
                            "outflow_asset_id": asset_id, "outflow_amount": qty})
        return
    if kind == "buy":
        payload.update({
            "outflow_account_id": account_id, "outflow_asset_id": cash_id,
            "outflow_amount": cash_amt,
            "inflow_account_id": account_id, "inflow_asset_id": asset_id,
            "inflow_amount": qty,
        })
    else:
        payload.update({
            "outflow_account_id": account_id, "outflow_asset_id": asset_id,
            "outflow_amount": qty,
            "inflow_account_id": account_id, "inflow_asset_id": cash_id,
            "inflow_amount": cash_amt,
        })


def _fx_legs(payload: dict[str, Any], pair: str, qty: float, trade_money: float,
             account_id: int, asset_by_ticker: dict[str, int]) -> None:
    """AssetClass CASH: Symbol = BASE.QUOTE, Quantity signed in BASE,
    |TradeMoney| in QUOTE (= CurrencyPrimary)."""
    parts = pair.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"invalid FX pair symbol: {pair or '(empty)'}")
    if qty == 0 or trade_money <= 0:
        raise ValueError("missing FX quantity or trade money")
    base_id = _lookup_asset(asset_by_ticker, parts[0])
    quote_id = _lookup_asset(asset_by_ticker, parts[1])
    legs = {"inflow_asset_id": base_id, "inflow_amount": abs(qty),
            "outflow_asset_id": quote_id, "outflow_amount": round(trade_money, 6)}
    if qty < 0:  # sold the base currency of the pair
        legs = {"inflow_asset_id": quote_id, "inflow_amount": round(trade_money, 6),
                "outflow_asset_id": base_id, "outflow_amount": abs(qty)}
    payload.update({**legs, "outflow_account_id": account_id,
                    "inflow_account_id": account_id})


def _parse_firstrade(raw_bytes: bytes, account_id: int,
                     asset_by_ticker: dict[str, int], base_ccy: str,
                     cash_tickers: set[str], overrides: dict[int, str]) -> list[ImportRow]:
    reader = csv.DictReader(io.StringIO(_decode_text(raw_bytes)))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    out: list[ImportRow] = []
    for idx, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if not any(row.values()):
            continue  # template pads trailing all-empty lines
        raw_kind = row.get("交易類別", "")
        ticker = overrides.get(idx) or _norm_ticker(row.get("代號", ""))
        try:
            if raw_kind in FT_SKIP_KINDS:
                out.append(ImportRow(row=idx, status="skipped", txn_kind="transfer",
                                     payload=None, error="transfer in/out not imported",
                                     ticker=ticker))
                continue
            kind = FT_KINDS.get(raw_kind)
            if kind is None:
                raise ValueError(f"unsupported transaction type: {raw_kind or '(empty)'}")
            # interest/dividend rows may name their payout currency in 代號
            row_ccy = base_ccy
            if kind in ("dividend", "interest") and ticker and \
                    (ticker in cash_tickers or ticker in COMMON_CCY_CODES):
                row_ccy = ticker
            cash_id = _lookup_asset(asset_by_ticker, row_ccy)
            payload = _base_payload(_parse_date_dmy(row.get("日期", "")), account_id,
                                    cash_id, row.get("說明", ""))
            amount = _parse_num(row.get("金額", ""))
            if kind in ("buy", "sell"):
                if not ticker:
                    raise ValueError("missing ticker for trade row")
                _trade_legs(payload, kind, account_id,
                            _lookup_asset(asset_by_ticker, ticker),
                            abs(_parse_num(row.get("數量", ""))), cash_id, abs(amount))
            else:  # dividend / interest
                _cash_flow(payload, account_id, cash_id, amount)
            out.append(ImportRow(row=idx, status="ok", txn_kind=kind, payload=payload,
                                 ticker=ticker))
        except ValueError as exc:
            out.append(ImportRow(row=idx, status="invalid",
                                 txn_kind=FT_KINDS.get(raw_kind, "unknown"),
                                 payload=None, error=str(exc), ticker=ticker))
    return out


def _classify_flex_section(cols: set[str]) -> str:
    """Identify a flex section by its signature columns."""
    if "StartingCash" in cols:
        return "summary"
    if "Buy/Sell" in cols and "TradePrice" in cols:
        return "trades"
    if "TaxDescription" in cols:
        return "taxes"
    if "ActionDescription" in cols:
        return "actions"
    if "Type" in cols and "Amount" in cols and "DividendType" in cols:
        return "cash"
    if "InterestType" in cols and "ValueDate" in cols:
        return "accruals"
    return "unknown"


def _flex_kind_guess(section: str, row: dict[str, str]) -> str:
    if section == "trades":
        return {"BUY": "buy", "SELL": "sell"}.get(
            _norm_ticker(row.get("Buy/Sell", "")), "trade")
    if section == "actions":
        return "redemption"
    return IB_CASH_KINDS.get(row.get("Type", ""), "unknown")


def _flex_asset_hint(asset_class: str, row: dict[str, str]) -> dict[str, Any] | None:
    """Provider-known asset info for the unknown-ticker approval UI.
    Only bonds need one; option symbols resolve via the OCC suggestion."""
    if asset_class != "BOND":
        return None
    ccy = _norm_ticker(row.get("CurrencyPrimary", "")) or "USD"
    return {"asset_type": "bond", "currency": ccy, "is_yahoo_tracked": False}


def _flex_trade_row(lineno: int, row: dict[str, str], ticker: str,
                    hint: dict[str, Any] | None, account_id: int,
                    asset_by_ticker: dict[str, int], base_ccy: str) -> ImportRow:
    buy_sell = _norm_ticker(row.get("Buy/Sell", ""))
    kind = {"BUY": "buy", "SELL": "sell"}.get(buy_sell)
    if kind is None:
        raise ValueError(f"unsupported Buy/Sell value: {buy_sell or '(empty)'}")
    detail = _norm_ticker(row.get("LevelOfDetail", ""))
    if detail and detail != "EXECUTION":
        return ImportRow(row=lineno, status="skipped", txn_kind=kind, payload=None,
                         ticker=ticker, error=f"non-execution detail level: {detail}")
    ccy = _norm_ticker(row.get("CurrencyPrimary", "")) or base_ccy
    cash_id = _lookup_asset(asset_by_ticker, ccy)
    txn_date = _parse_date_dmy(row.get("DateTime", "") or row.get("TradeDate", ""))
    payload = _base_payload(txn_date, account_id, cash_id, row.get("Description", ""))
    # commissions settle in their own currency, not the statement base
    fee_ccy = _norm_ticker(row.get("IBCommissionCurrency", "")) or ccy
    payload["fee_asset_id"] = _lookup_asset(asset_by_ticker, fee_ccy)
    payload["fee_amount"] = abs(_parse_num(row.get("IBCommission", "")))
    qty = _parse_num(row.get("Quantity", ""))
    trade_money = abs(_parse_num(row.get("TradeMoney", "")))
    asset_class = _norm_ticker(row.get("AssetClass", ""))
    price_info = None
    if asset_class == "CASH":
        kind = "fx"
        _fx_legs(payload, ticker, qty, trade_money, account_id, asset_by_ticker)
    else:
        if not ticker:
            raise ValueError("missing ticker for trade row")
        # exchange taxes (e.g. KR securities tax) settle inside the cash leg
        taxes = abs(_parse_num(row.get("Taxes", "")))
        cash_amt = trade_money + taxes if kind == "buy" else trade_money - taxes
        # BookTrade rows with zero cash are option assignments / exercises:
        # the position moves without a cash counterleg
        tx_type = _norm_ticker(row.get("TransactionType", ""))
        book_zero = tx_type == "BOOKTRADE" and round(cash_amt, 6) == 0
        _trade_legs(payload, kind, account_id, _lookup_asset(asset_by_ticker, ticker),
                    abs(qty), cash_id, round(cash_amt, 6), allow_zero_cash=book_zero)
        if asset_class == "BOND":
            price = _parse_num(row.get("TradePrice", ""))
            if price > 0:  # bonds quote per 100 face; manual prices per 1 face unit
                price_info = {"ticker": ticker, "date": txn_date[:10],
                              "price": round(price / 100.0, 8)}
    return ImportRow(row=lineno, status="ok", txn_kind=kind, payload=payload,
                     ticker=ticker, asset_hint=hint, price_info=price_info)


def _flex_action_row(lineno: int, row: dict[str, str], ticker: str,
                     hint: dict[str, Any] | None, account_id: int,
                     asset_by_ticker: dict[str, int], base_ccy: str) -> ImportRow:
    code = _norm_ticker(row.get("Type", ""))
    qty = _parse_num(row.get("Quantity", ""))
    proceeds = _parse_num(row.get("Proceeds", ""))
    # BM = bond maturity / full call: face removed, cash in at redemption value
    if code != "BM" or qty >= 0 or proceeds <= 0:
        raise ValueError(f"unsupported corporate action type: {code or '(empty)'}")
    if not ticker:
        raise ValueError("missing ticker for corporate action")
    ccy = _norm_ticker(row.get("CurrencyPrimary", "")) or base_ccy
    cash_id = _lookup_asset(asset_by_ticker, ccy)
    txn_date = _parse_date_dmy(row.get("Date/Time", "") or row.get("Report Date", ""))
    payload = _base_payload(txn_date, account_id, cash_id,
                            row.get("ActionDescription", ""))
    _trade_legs(payload, "sell", account_id, _lookup_asset(asset_by_ticker, ticker),
                abs(qty), cash_id, round(proceeds, 6))
    price_info = {"ticker": ticker, "date": txn_date[:10],
                  "price": round(proceeds / abs(qty), 8)}
    return ImportRow(row=lineno, status="ok", txn_kind="redemption", payload=payload,
                     ticker=ticker, asset_hint=hint, price_info=price_info)


def _flex_cash_row(lineno: int, row: dict[str, str], ticker: str, account_id: int,
                   asset_by_ticker: dict[str, int], base_ccy: str) -> ImportRow:
    raw_type = row.get("Type", "")
    if raw_type in IB_CASH_SKIP_TYPES:
        return ImportRow(row=lineno, status="skipped", txn_kind="transfer",
                         payload=None, ticker=ticker,
                         error="transfer in/out not imported")
    kind = IB_CASH_KINDS.get(raw_type)
    if kind is None:
        raise ValueError(f"unsupported cash transaction type: {raw_type or '(empty)'}")
    amount = _parse_num(row.get("Amount", ""))
    if amount == 0:
        raise ValueError("missing amount")
    ccy = _norm_ticker(row.get("CurrencyPrimary", "")) or base_ccy
    cash_id = _lookup_asset(asset_by_ticker, ccy)
    txn_date = _parse_date_dmy(row.get("Date/Time", "") or row.get("SettleDate", ""))
    payload = _base_payload(txn_date, account_id, cash_id, row.get("Description", ""))
    _cash_flow(payload, account_id, cash_id, amount)
    return ImportRow(row=lineno, status="ok", txn_kind=kind, payload=payload,
                     ticker=ticker)


def _parse_ib(raw_bytes: bytes, account_id: int,
              asset_by_ticker: dict[str, int], base_ccy: str,
              overrides: dict[int, str]) -> list[ImportRow]:
    """IB Flex Query CSV: sections opened by ClientAccountID header rows."""
    text = _decode_text(raw_bytes)
    if "ClientAccountID" not in text.split("\n", 1)[0]:
        raise ValueError(
            "not an IB Flex Query CSV (expected a ClientAccountID header row); "
            "the legacy activity-statement export is no longer supported")
    out: list[ImportRow] = []
    header: list[str] | None = None
    section = "unknown"
    for lineno, rec in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not rec:
            continue
        if (rec[0] or "").strip() == "ClientAccountID":
            header = [c.strip() for c in rec]
            section = _classify_flex_section(set(header))
            continue
        if header is None:
            continue
        row = {h: (v or "").strip() for h, v in zip(header, rec) if h}
        if not any(row.values()) or section == "summary":
            continue
        asset_class = _norm_ticker(row.get("AssetClass", ""))
        symbol = _norm_ticker(row.get("Symbol", ""))
        if asset_class in ("OPT", "FOP"):
            symbol = symbol.replace(" ", "")  # IB pads OCC option symbols
        ticker = overrides.get(lineno) or symbol
        hint = _flex_asset_hint(asset_class, row)
        if section == "taxes":
            out.append(ImportRow(row=lineno, status="skipped", txn_kind="tax",
                                 payload=None, ticker=ticker,
                                 error="already included in the trade's cash leg"))
            continue
        if section == "accruals":
            out.append(ImportRow(row=lineno, status="skipped", txn_kind="interest",
                                 payload=None, ticker=ticker,
                                 error="daily accrual only; cash posts with the "
                                       "periodic CREDIT/DEBIT INT entry"))
            continue
        if section == "unknown":
            out.append(ImportRow(row=lineno, status="skipped", txn_kind="unknown",
                                 payload=None, ticker=ticker,
                                 error="unrecognized flex section"))
            continue
        try:
            if section == "trades":
                out.append(_flex_trade_row(lineno, row, ticker, hint, account_id,
                                           asset_by_ticker, base_ccy))
            elif section == "actions":
                out.append(_flex_action_row(lineno, row, ticker, hint, account_id,
                                            asset_by_ticker, base_ccy))
            else:
                out.append(_flex_cash_row(lineno, row, ticker, account_id,
                                          asset_by_ticker, base_ccy))
        except ValueError as exc:
            out.append(ImportRow(row=lineno, status="invalid",
                                 txn_kind=_flex_kind_guess(section, row),
                                 payload=None, error=str(exc), ticker=ticker,
                                 asset_hint=hint))
    return out


def detect_provider_from_header(raw_bytes: bytes) -> str | None:
    head = "\n".join(_decode_text(raw_bytes).splitlines()[:5])
    if head.startswith("ClientAccountID,"):
        return "ib"
    if "交易類別" in head and "代號" in head:
        return "firstrade"
    return None


# common ISO-4217 codes used only to suggest "cash" for unknown 3-letter tickers
COMMON_CCY_CODES = {"USD", "EUR", "GBP", "JPY", "CNY", "HKD", "KRW", "TWD", "SGD",
                    "AUD", "NZD", "CAD", "CHF", "SEK", "NOK", "DKK", "INR", "THB",
                    "MYR", "IDR", "PHP", "VND", "ZAR", "BRL", "MXN", "PLN", "TRY"}


def suggest_unknown_asset(ticker: str) -> dict[str, Any]:
    """Best-guess creation payload for a ticker missing from the asset list."""
    t = _norm_ticker(ticker)
    if t in COMMON_CCY_CODES:
        return {"asset_type": "cash", "currency": t, "is_yahoo_tracked": False}
    m = OCC_OPTION_RE.match(t)
    if m:
        root, ymd, cp, strike = m.groups()
        month, day = int(ymd[2:4]), int(ymd[4:6])
        if 1 <= month <= 12 and 1 <= day <= 31:
            expiry = f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}"
            # expired contracts are gone from yahoo -> default to the options archive
            expired = expiry < datetime.now(timezone.utc).date().isoformat()
            return {"asset_type": "option", "currency": "USD", "is_yahoo_tracked": True,
                    "price_provider": "option_archive" if expired else "yahoo",
                    "derivative": {"underlying_ticker": root,
                                   "expiry": expiry,
                                   "contract_type": "call" if cp == "C" else "put",
                                   "strike": int(strike) / 1000.0, "multiplier": 100}}
    return {"asset_type": "equity", "currency": "USD", "is_yahoo_tracked": True}
