"""HTML page routes."""
from flask import Blueprint, render_template

from backend import db

bp = Blueprint("pages", __name__)

# TEXT size presets: name -> multiplier applied to --font-scale on <html>
FONT_SCALES = {"xsmall": 0.9, "small": 1.0, "normal": 1.15, "large": 1.3, "xlarge": 1.5,
               "xxlarge": 1.7, "xxxlarge": 2.0, "max": 2.5}

PAGES = [
    ("home", "/", "index.html"),
    ("holdings", "/holdings", "holdings.html"),
    ("transactions", "/transactions", "transactions.html"),
    ("records", "/records", "records.html"),
    ("manual_prices", "/manual-prices", "manual_prices.html"),
    ("daily_values", "/daily-values", "daily_values.html"),
    ("asset_prices", "/asset-prices", "asset_prices.html"),
    ("performance", "/performance", "performance.html"),
    ("strategy_groups", "/strategy-groups", "strategy_groups.html"),
    ("disposable", "/disposable-income", "disposable_income.html"),
    ("scrape_monitor", "/scrape-monitor", "scrape_monitor.html"),
    ("settings", "/settings", "settings.html"),
]


def _render(template: str, page: str):
    with db.db_conn() as conn:
        display_currency = db.get_setting(conn, "display_currency", "USD")
        display_utc_offset_hours = db.get_setting(conn, "display_utc_offset_hours", "0")
        font_size = db.get_setting(conn, "font_size", "small")
        ui_theme = db.get_setting(conn, "ui_theme", "dark")
        currencies = [r["code"] for r in conn.execute("SELECT code FROM currencies ORDER BY code")]
    if font_size not in FONT_SCALES:
        font_size = "small"
    if ui_theme not in ("dark", "light"):
        ui_theme = "dark"
    return render_template(template, active_page=page,
                           display_currency=display_currency,
                           display_utc_offset_hours=display_utc_offset_hours,
                           currencies=currencies,
                           font_size=font_size, font_scale=FONT_SCALES[font_size],
                           ui_theme=ui_theme)


def _make_view(template, page):
    def view():
        return _render(template, page)
    view.__name__ = f"page_{page}"
    return view


for page, path, template in PAGES:
    bp.add_url_rule(path, f"page_{page}", _make_view(template, page))
