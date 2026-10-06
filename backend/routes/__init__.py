"""Blueprint registration."""
from flask import Flask


def register_all(app: Flask):
    from backend.routes import (api_accounts, api_assets, api_dashboard, api_disposable,
                                api_groups, api_history, api_performance, api_scrape,
                                api_settings, api_transactions, pages)
    app.register_blueprint(pages.bp)
    app.register_blueprint(api_accounts.bp)
    app.register_blueprint(api_assets.bp)
    app.register_blueprint(api_transactions.bp)
    app.register_blueprint(api_disposable.bp)
    app.register_blueprint(api_groups.bp)
    app.register_blueprint(api_dashboard.bp)
    app.register_blueprint(api_history.bp)
    app.register_blueprint(api_performance.bp)
    app.register_blueprint(api_scrape.bp)
    app.register_blueprint(api_settings.bp)
