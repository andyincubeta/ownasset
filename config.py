"""Central configuration for the asset dashboard."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, ".db")
DB_PATH = os.path.join(DB_DIR, "asset_dashboard.db")
LOG_DIR = os.path.join(BASE_DIR, "logs")
# deliberately not a web setting: the app has no login
BACKUP_DIR = os.environ.get("OWNASSET_BACKUP_DIR") or os.path.join(DB_DIR, "backups")

HOST = "127.0.0.1"
PORT = 5089

# Yahoo Finance endpoints (direct REST, never the deprecated yfinance lib)
YAHOO_QUOTE_SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
YAHOO_BATCH_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_COOKIE_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
YAHOO_TIMEOUT = 15

# EOD Options Archive (keyless): full option history incl. expired contracts
OPTION_ARCHIVE_URL = "https://api.onclickmedia.com/chart/"
OPTION_ARCHIVE_TIMEOUT = 20

# per-asset market-data engine when is_yahoo_tracked = 1
PRICE_PROVIDERS = ("yahoo", "option_archive")


DEFAULT_SETTINGS = {
    "display_currency": "USD",
    "display_utc_offset_hours": "0",
    "log_level": "DEBUG",
    "quote_interval_seconds": "60",
    "intraday_interval_minutes": "60",
    "intraday_bar": "60m",           # yahoo bar size for intraday collection
    "daily_refresh_time": "22:30",   # HH:MM local
    "snapshot_time": "23:59",        # HH:MM local
    "info_refresh_time": "21:00",    # HH:MM local
    "backup_time": "00:30",          # HH:MM UTC, after the snapshot
    "backup_keep_count": "14",
    "display_prefs": "{}",           # JSON: per-view column/tile visibility
    "quote_summary_modules": "financialData,quoteType,defaultKeyStatistics,assetProfile,summaryDetail,price",
    "scrape_log_keep_rows": "20000",
    "day_close_mode": "market",      # market = official close | cutoff = incl. pre/post
    "day_close_cutoff_utc": "00:00", # HH:MM UTC day end for cutoff mode
}

SEED_CURRENCIES = [
    ("USD", "US Dollar"),
    ("HKD", "Hong Kong Dollar"),
    ("CNY", "Chinese Yuan / RMB"),
]

ASSET_TYPES = ["cash", "equity", "etf", "bond", "option", "future",
               "crypto", "real_estate", "loan", "other"]


def ensure_dirs():
    os.makedirs(DB_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
