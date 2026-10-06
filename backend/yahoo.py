"""Direct Yahoo Finance REST client (the deprecated yfinance lib is NOT used).

Endpoints (per yahoo_api_template.py conventions):
  * quoteSummary v10  - metadata / fundamentals (cookie + crumb required)
  * chart v8          - OHLCV bars, any interval, full history (no crumb)
  * quote v7          - batched real-time quotes, ONE request for many tickers
  * options v7        - option chains with IV (cookie + crumb)

Credential handling: a shared requests.Session obtains the auth cookie from
fc.yahoo.com and a crumb from v1/test/getcrumb; both are cached and refreshed
automatically on 401/403. HTTP 429/999 raise RateLimitedError so the
scheduler can back off.
"""
import datetime
import json
import random
import threading
import time
from urllib.parse import quote as urlquote

import requests

import config
from backend.logger import get_logger

log = get_logger("dashboard.yahoo")


class YahooError(Exception):
    pass


class RateLimitedError(YahooError):
    pass


def _raw(value):
    """Yahoo wraps numbers as {'raw': x, 'fmt': '...'} when formatted=true."""
    if isinstance(value, dict):
        return value.get("raw")
    return value


def _session_date(ts, gmt_offset_s) -> str | None:
    """Exchange-local calendar date of a unix timestamp."""
    if ts is None:
        return None
    return datetime.datetime.fromtimestamp(
        int(ts) + int(gmt_offset_s or 0), datetime.timezone.utc).date().isoformat()


class YahooClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.YAHOO_USER_AGENT})
        self.crumb: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ auth ----
    def _refresh_credentials(self):
        with self._lock:
            log.debug("refreshing yahoo cookie + crumb")
            self.session.cookies.clear()
            self.session.get(config.YAHOO_COOKIE_URL, timeout=config.YAHOO_TIMEOUT,
                             allow_redirects=True)
            if not self.session.cookies:
                raise YahooError("Failed to obtain Yahoo auth cookie.")
            resp = self.session.get(config.YAHOO_CRUMB_URL, timeout=config.YAHOO_TIMEOUT)
            crumb = resp.text.strip()
            if not crumb or "<html" in crumb.lower():
                raise YahooError("Failed to retrieve Yahoo crumb.")
            self.crumb = crumb
            log.debug("yahoo crumb refreshed")

    def _get_json(self, url: str, params: dict | None = None,
                  need_crumb: bool = False, _retry: bool = True) -> dict:
        params = dict(params or {})
        if need_crumb:
            if not self.crumb:
                self._refresh_credentials()
            params["crumb"] = self.crumb
        started = time.perf_counter()
        resp = self.session.get(url, params=params, timeout=config.YAHOO_TIMEOUT,
                                allow_redirects=True)
        elapsed = (time.perf_counter() - started) * 1000
        log.debug("GET %s -> %s in %.0fms", resp.url.split("?")[0], resp.status_code, elapsed)
        if resp.status_code in (401, 403) and _retry:
            self._refresh_credentials()
            return self._get_json(url, params={k: v for k, v in params.items() if k != "crumb"},
                                  need_crumb=need_crumb, _retry=False)
        if resp.status_code in (429, 999):
            raise RateLimitedError(f"HTTP {resp.status_code} rate limited: {url}")
        if resp.status_code != 200:
            raise YahooError(f"HTTP {resp.status_code} for {url}: {resp.text[:200]}")
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise YahooError(f"Bad JSON from {url}: {exc}") from exc

    # ------------------------------------------------------ quoteSummary --
    def get_stock_info(self, ticker: str, modules: str | None = None) -> dict | None:
        """Flattened quoteSummary dump (all 'raw' values unwrapped). None if no result."""
        modules = modules or config.DEFAULT_SETTINGS["quote_summary_modules"]
        url = config.YAHOO_QUOTE_SUMMARY_URL.format(ticker=urlquote(ticker))
        data = self._get_json(url, {"modules": modules, "ssl": "true"}, need_crumb=True)
        results = (data.get("quoteSummary") or {}).get("result") or []
        if not results:
            log.debug("no quoteSummary result for %s", ticker)
            return None
        info = results[0]
        flat: dict = {}
        for _module, payload in info.items():
            if not isinstance(payload, dict):
                continue
            for key, val in payload.items():
                if isinstance(val, dict) and "raw" in val:
                    flat[key] = val["raw"]
                elif isinstance(val, (dict, list)) and len(val) == 0:
                    flat.setdefault(key, None)
                elif isinstance(val, (dict, list)):
                    flat[key] = "DETAILS"
                else:
                    flat[key] = val
        return flat

    # ------------------------------------------------------------ chart ---
    def get_chart(self, ticker: str, interval: str = "1d",
                  period1: int = 0, period2: int = 9999999999,
                  include_prepost: bool = False) -> dict:
        """Bars for a ticker. Returns {'meta': {...}, 'bars': [{ts,o,h,l,c,adj_close,v},...]}."""
        url = config.YAHOO_CHART_URL.format(ticker=urlquote(ticker))
        params = {
            "events": "capitalGain|div|split", "formatted": "true",
            "includeAdjustedClose": "true", "interval": interval,
            "period1": int(period1), "period2": int(period2),
            "symbol": ticker, "lang": "en-US", "region": "US",
        }
        if include_prepost:
            params["includePrePost"] = "true"
        data = self._get_json(url, params)
        chart = data.get("chart") or {}
        if chart.get("error"):
            raise YahooError(f"chart error for {ticker}: {chart['error']}")
        result = (chart.get("result") or [None])[0]
        if result is None:
            raise YahooError(f"no chart result for {ticker}")
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adj_list = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        bars = []
        for i, ts in enumerate(timestamps):
            close = closes[i] if i < len(closes) else None
            if close is None:
                continue
            bars.append({
                "ts": int(ts),
                "open": opens[i] if i < len(opens) else None,
                "high": highs[i] if i < len(highs) else None,
                "low": lows[i] if i < len(lows) else None,
                "close": close,
                "adj_close": (adj_list[i] if adj_list and i < len(adj_list) else close),
                "volume": volumes[i] if i < len(volumes) else None,
            })
        return {"meta": meta, "bars": bars}

    # ----------------------------------------------------- batch quotes ---
    def get_quotes(self, tickers: list[str]) -> dict[str, dict]:
        """ONE batched request for the whole universe -> {ticker: quote dict}."""
        if not tickers:
            return {}
        data = self._get_json(config.YAHOO_BATCH_QUOTE_URL,
                              {"symbols": ",".join(tickers)}, need_crumb=True)
        out: dict[str, dict] = {}
        for item in (data.get("quoteResponse") or {}).get("result") or []:
            sym = item.get("symbol")
            if not sym:
                continue
            state = item.get("marketState") or ""
            price = _raw(item.get("regularMarketPrice"))
            ts = _raw(item.get("regularMarketTime")) or int(time.time())
            session = "regular"
            # newest of regular/pre/post wins; marketState is unreliable (CLOSED, PREPRE)
            for name, p_key, t_key in (("pre", "preMarketPrice", "preMarketTime"),
                                       ("post", "postMarketPrice", "postMarketTime")):
                p, t = _raw(item.get(p_key)), _raw(item.get(t_key))
                if p is not None and t is not None and (price is None or t > ts):
                    price, ts, session = p, t, name
            out[sym] = {
                "price": price,
                "prev_close": _raw(item.get("regularMarketPreviousClose")),
                "currency": item.get("currency"),
                "market_state": state,
                "session": session,
                "ts": ts,
                "session_date": _session_date(
                    ts, (_raw(item.get("gmtOffSetMilliseconds")) or 0) / 1000),
            }
        return out

    def get_quote_via_chart(self, ticker: str) -> dict:
        """Per-ticker fallback via 5m chart bars incl. pre/post (no crumb needed)."""
        now = int(time.time())
        data = self.get_chart(ticker, interval="5m", period1=now - 2 * 86400, period2=now,
                              include_prepost=True)
        meta = data["meta"]
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        bars = data["bars"]
        # last bar carries the current session price (pre/regular/post)
        price = bars[-1]["close"] if bars else meta.get("regularMarketPrice")
        ts = bars[-1]["ts"] if bars else (meta.get("regularMarketTime") or now)
        return {
            "price": price,
            "prev_close": prev,
            "currency": meta.get("currency"),
            "market_state": "",
            "ts": ts,
            "session_date": _session_date(ts, meta.get("gmtoffset")),
        }


def polite_sleep(base: float = 0.4):
    """Small jittered delay between sequential per-ticker calls."""
    time.sleep(base + random.uniform(0, 0.3))


# Shared singleton used across app + scheduler threads.
client = YahooClient()
