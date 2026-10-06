"""Yahoo client parsers against recorded fixture payloads (no network)."""
import json

import pytest

from backend import yahoo


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.url = "https://query1.finance.yahoo.com/test?x=1"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


QUOTE_SUMMARY = {"quoteSummary": {"result": [{
    "price": {"regularMarketPrice": {"raw": 180.5, "fmt": "180.50"},
              "longName": "Apple Inc.", "currency": "USD"},
    "assetProfile": {"industry": "Consumer Electronics", "sector": "Technology",
                     "country": "United States", "companyOfficers": []},
    "quoteType": {"quoteType": "EQUITY", "shortName": "Apple"},
}], "error": None}}

CHART = {"chart": {"error": None, "result": [{
    "meta": {"currency": "USD", "regularMarketPrice": 181.0, "previousClose": 179.0},
    "timestamp": [1704153600, 1704240000, 1704326400],
    "indicators": {
        "quote": [{"open": [180, 181, None], "high": [182, 183, None],
                    "low": [179, 180, None], "close": [181, 182, None],
                    "volume": [1000, 1100, None]}],
        "adjclose": [{"adjclose": [180.5, 181.5, None]}],
    },
}]}}

BATCH = {"quoteResponse": {"error": None, "result": [
    {"symbol": "AAPL", "regularMarketPrice": 180.5, "regularMarketPreviousClose": 178.0,
     "currency": "USD", "marketState": "REGULAR", "regularMarketTime": 1704326400},
    {"symbol": "HKDUSD=X", "regularMarketPrice": 0.128,
     "regularMarketPreviousClose": 0.1279, "currency": "USD",
     "regularMarketTime": 1704326400},
]}}

BATCH_PREPOST = {"quoteResponse": {"error": None, "result": [
    {"symbol": "AAPL", "regularMarketPrice": 180.5, "regularMarketPreviousClose": 178.0,
     "currency": "USD", "marketState": "POST", "regularMarketTime": 1704326400,
     "postMarketPrice": 182.3, "postMarketTime": 1704330000},
    {"symbol": "MSFT", "regularMarketPrice": 402.0, "regularMarketPreviousClose": 400.0,
     "currency": "USD", "marketState": "PRE", "regularMarketTime": 1704326400,
     "preMarketPrice": 405.5, "preMarketTime": 1704360000},
    {"symbol": "0005.HK", "regularMarketPrice": 62.0, "regularMarketPreviousClose": 61.5,
     "currency": "HKD", "marketState": "CLOSED", "regularMarketTime": 1704326400},
]}}


@pytest.fixture()
def fake_client(monkeypatch):
    client = yahoo.YahooClient()
    client.crumb = "test-crumb"
    responses = {}

    def fake_get_json(url, params=None, need_crumb=False, _retry=True):
        for key, payload in responses.items():
            if key in url:
                return payload
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    client._responses = responses
    return client


def test_get_stock_info_flattens_raw(fake_client):
    fake_client._responses["quoteSummary"] = QUOTE_SUMMARY
    info = fake_client.get_stock_info("AAPL")
    assert info["regularMarketPrice"] == 180.5
    assert info["longName"] == "Apple Inc."
    assert info["industry"] == "Consumer Electronics"
    assert info["companyOfficers"] is None  # empty list -> None


def test_get_stock_info_none_when_empty(fake_client):
    fake_client._responses["quoteSummary"] = {"quoteSummary": {"result": [], "error": None}}
    assert fake_client.get_stock_info("NOPE") is None


def test_get_chart_skips_null_closes(fake_client):
    fake_client._responses["/v8/finance/chart/"] = CHART
    data = fake_client.get_chart("AAPL")
    assert len(data["bars"]) == 2  # third row dropped (close None)
    assert data["bars"][0] == {"ts": 1704153600, "open": 180, "high": 182, "low": 179,
                               "close": 181, "adj_close": 180.5, "volume": 1000}
    assert data["meta"]["currency"] == "USD"


def test_get_quotes_batch(fake_client):
    fake_client._responses["/v7/finance/quote"] = BATCH
    quotes = fake_client.get_quotes(["AAPL", "HKDUSD=X"])
    assert quotes["AAPL"]["price"] == 180.5
    assert quotes["AAPL"]["prev_close"] == 178.0
    assert quotes["HKDUSD=X"]["price"] == 0.128


def test_get_quotes_empty_universe(fake_client):
    assert fake_client.get_quotes([]) == {}


def test_get_quotes_pre_post_sessions(fake_client):
    fake_client._responses["/v7/finance/quote"] = BATCH_PREPOST
    quotes = fake_client.get_quotes(["AAPL", "MSFT", "0005.HK"])
    assert quotes["AAPL"]["price"] == 182.3          # POST -> post-market price
    assert quotes["AAPL"]["ts"] == 1704330000
    assert quotes["AAPL"]["prev_close"] == 178.0     # day change vs regular close
    assert quotes["MSFT"]["price"] == 405.5          # PRE -> pre-market price
    assert quotes["0005.HK"]["price"] == 62.0        # no extended session -> regular


def test_get_quotes_newest_session_ignores_market_state(fake_client):
    fake_client._responses["/v7/finance/quote"] = {"quoteResponse": {"error": None, "result": [
        # weekend: CLOSED but Friday post-market is newer than the 16:00 close
        {"symbol": "AAPL", "regularMarketPrice": 180.5, "marketState": "CLOSED",
         "regularMarketTime": 1704326400, "postMarketPrice": 182.3,
         "postMarketTime": 1704340800},
        # PREPRE: only last evening's post-market exists
        {"symbol": "MSFT", "regularMarketPrice": 402.0, "marketState": "PREPRE",
         "regularMarketTime": 1704326400, "postMarketPrice": 401.0,
         "postMarketTime": 1704340800},
        # REGULAR session: yesterday's post-market is stale
        {"symbol": "NVDA", "regularMarketPrice": 500.0, "marketState": "REGULAR",
         "regularMarketTime": 1704400000, "postMarketPrice": 490.0,
         "postMarketTime": 1704340800},
    ]}}
    q = fake_client.get_quotes(["AAPL", "MSFT", "NVDA"])
    assert (q["AAPL"]["price"], q["AAPL"]["session"], q["AAPL"]["ts"]) == (182.3, "post", 1704340800)
    assert (q["MSFT"]["price"], q["MSFT"]["session"]) == (401.0, "post")
    assert (q["NVDA"]["price"], q["NVDA"]["session"]) == (500.0, "regular")


def test_get_quotes_session_date_is_exchange_local(fake_client):
    # Fri 2026-10-02 20:00 EDT post-market = Sat 00:00 UTC, still Friday's session
    fake_client._responses["/v7/finance/quote"] = {"quoteResponse": {"error": None, "result": [
        {"symbol": "SPY", "regularMarketPrice": 769.78, "regularMarketTime": 1790971200,
         "postMarketPrice": 769.86, "postMarketTime": 1790985600,
         "gmtOffSetMilliseconds": -14400000},
        {"symbol": "BTC-USD", "regularMarketPrice": 60000.0, "regularMarketTime": 1790985600,
         "gmtOffSetMilliseconds": 0},
    ]}}
    q = fake_client.get_quotes(["SPY", "BTC-USD"])
    assert q["SPY"]["session_date"] == "2026-10-02"
    assert q["BTC-USD"]["session_date"] == "2026-10-03"


def test_get_quote_via_chart_uses_last_prepost_bar(fake_client, monkeypatch):
    captured = {}
    orig = fake_client._get_json

    def spy(url, params=None, **kw):
        captured.update(params or {})
        return orig(url, params, **kw)

    monkeypatch.setattr(fake_client, "_get_json", spy)
    fake_client._responses["/v8/finance/chart/"] = CHART
    q = fake_client.get_quote_via_chart("AAPL")
    assert captured["includePrePost"] == "true"
    assert captured["interval"] == "5m"
    assert q["price"] == 182            # last bar close, not meta regularMarketPrice
    assert q["ts"] == 1704240000
    assert q["prev_close"] == 179.0


def test_rate_limit_raises(monkeypatch):
    client = yahoo.YahooClient()
    client.crumb = "x"
    monkeypatch.setattr(client.session, "get",
                        lambda *a, **k: FakeResponse({}, status=429))
    with pytest.raises(yahoo.RateLimitedError):
        client._get_json("https://query1.finance.yahoo.com/v7/finance/quote")


def test_raw_unwrap():
    assert yahoo._raw({"raw": 5, "fmt": "5"}) == 5
    assert yahoo._raw(7) == 7
    assert yahoo._raw(None) is None
