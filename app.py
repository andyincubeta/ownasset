"""Flask app entry point.

Run:  python app.py   ->  http://127.0.0.1:5089
"""
from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

import config
from backend import db
from backend.logger import get_logger, init_logging


def create_app(start_scheduler: bool = False) -> Flask:
    config.ensure_dirs()
    init_logging()
    db.init_db()
    with db.db_conn() as conn:
        init_logging(db.get_setting(conn, "log_level", "DEBUG"))

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["JSON_SORT_KEYS"] = False

    from backend.routes import register_all
    register_all(app)

    log = get_logger()

    @app.errorhandler(Exception)
    def _unhandled(exc):
        if isinstance(exc, HTTPException):
            return exc
        # surface the real reason to the frontend toast instead of a bare HTTP 500
        log.exception("unhandled error on %s %s", request.method, request.path)
        return jsonify({"error": f"internal error: {exc}"}), 500

    if start_scheduler:
        from backend import scheduler
        scheduler.start()
    log.info("app created (scheduler=%s)", start_scheduler)
    return app


if __name__ == "__main__":
    application = create_app(start_scheduler=True)
    # reloader disabled so the background scheduler is not started twice
    application.run(host=config.HOST, port=config.PORT, debug=False, use_reloader=False)
