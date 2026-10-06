"""EOD Options Archive client (api.onclickmedia.com) — second price source.

Yahoo deletes option contracts shortly after expiry; this keyless archive keeps
the full end-of-day life (date/last/mark/bid/ask/volume/OI/IV) of US options.
Provider guidance: no formal rate limit, but prefer few large requests — so a
contract's whole history is always fetched in ONE call, never day-by-day.
"""
import re

import requests

import config
from backend.logger import get_logger

log = get_logger("dashboard.option_archive")

# OCC option symbol: ROOT + YYMMDD + C/P + strike*1000 zero-padded to 8
OCC_OPTION_RE = re.compile(r"^([A-Z][A-Z0-9.]{0,5})(\d{6})([CP])(\d{8})$")

session = requests.Session()
session.headers.update({"User-Agent": config.YAHOO_USER_AGENT})


class ArchiveError(Exception):
    pass


def parse_occ_ticker(ticker: str) -> dict | None:
    """OCC symbol -> {underlying, expiry, contract_type, strike}; None if not OCC."""
    m = OCC_OPTION_RE.match((ticker or "").strip().upper())
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    month, day = int(ymd[2:4]), int(ymd[4:6])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return {"underlying": root,
            "expiry": f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:6]}",
            "contract_type": "call" if cp == "C" else "put",
            "strike": int(strike) / 1000.0}


def params_for_asset(asset: dict, deriv: dict | None) -> dict:
    """Contract params from derivative_details, else parsed from the OCC ticker."""
    d = deriv or {}
    if d.get("underlying_ticker") and d.get("expiry") and d.get("strike"):
        return {"underlying": str(d["underlying_ticker"]).upper(),
                "expiry": str(d["expiry"])[:10],
                "contract_type": "put" if d.get("contract_type") == "put" else "call",
                "strike": float(d["strike"])}
    parsed = parse_occ_ticker(asset.get("ticker") or "")
    if parsed is None:
        raise ArchiveError(
            f"cannot derive option contract from {asset.get('ticker')!r}: needs "
            "derivative details (underlying/expiry/strike) or an OCC-style ticker")
    return parsed


def get_history(underlying: str, expiry: str, contract_type: str,
                strike: float) -> list[dict]:
    """Whole EOD life of one contract in a single request, ascending by date.
    Returns [{date, close, bid, ask, volume, iv}, ...]; close = last, fallback mark."""
    params = {"ticker": underlying, "expiration": expiry, "type": contract_type,
              "strike": strike, "output": "json-v1"}
    try:
        resp = session.get(config.OPTION_ARCHIVE_URL, params=params,
                           timeout=config.OPTION_ARCHIVE_TIMEOUT)
    except requests.RequestException as exc:
        raise ArchiveError(f"archive request failed: {exc}") from exc
    if resp.status_code != 200:
        raise ArchiveError(f"HTTP {resp.status_code} from archive: {resp.text[:200]}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ArchiveError(f"bad JSON from archive: {exc}") from exc
    if isinstance(data, dict):
        raise ArchiveError(f"archive error: {data.get('error') or data}")
    out = []
    for row in data:
        close = row.get("last")
        if not close and row.get("mark") is not None:  # last of 0/None = no trade that day
            close = row.get("mark")
        if not row.get("date") or close is None:
            continue
        out.append({"date": str(row["date"])[:10], "close": float(close),
                    "bid": row.get("bid"), "ask": row.get("ask"),
                    "volume": row.get("volume"), "iv": row.get("implied_volatility")})
    out.sort(key=lambda r: r["date"])
    log.debug("archive %s %s %s %s -> %d bars", underlying, expiry, contract_type,
              strike, len(out))
    return out


def history_for_asset(asset: dict, deriv: dict | None) -> list[dict]:
    p = params_for_asset(asset, deriv)
    return get_history(p["underlying"], p["expiry"], p["contract_type"], p["strike"])
