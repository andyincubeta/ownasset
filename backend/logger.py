"""Application-wide logging: timestamped (ms precision), rotating files + console.

Loggers
-------
dashboard            -> logs/app.log   (everything, DEBUG by default)
dashboard.scrape     -> logs/scrape.log (scrape jobs only; also propagates to app.log)
"""
import logging
import os
from logging.handlers import RotatingFileHandler

import config

FMT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | %(module)s:%(lineno)d | %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def init_logging(level: str = "DEBUG") -> logging.Logger:
    """Idempotent logging bootstrap. Returns the root 'dashboard' logger."""
    global _initialized
    root = logging.getLogger("dashboard")
    if _initialized:
        set_level(level)
        return root

    config.ensure_dirs()
    formatter = logging.Formatter(FMT, datefmt=DATEFMT)
    root.setLevel(_coerce(level))

    fh = RotatingFileHandler(os.path.join(config.LOG_DIR, "app.log"),
                             maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(formatter)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    root.addHandler(ch)

    scrape = logging.getLogger("dashboard.scrape")
    sh = RotatingFileHandler(os.path.join(config.LOG_DIR, "scrape.log"),
                             maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    sh.setFormatter(formatter)
    scrape.addHandler(sh)

    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    _initialized = True
    root.debug("logging initialized (level=%s)", level)
    return root


def get_logger(name: str = "dashboard") -> logging.Logger:
    return logging.getLogger(name)


def set_level(level: str):
    logging.getLogger("dashboard").setLevel(_coerce(level))


def _coerce(level):
    if isinstance(level, str):
        return getattr(logging, level.upper(), logging.DEBUG)
    return level
