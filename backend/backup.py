"""SQLite online backups (safe while the app is running in WAL mode)."""
import datetime
import os
import re
import sqlite3

import config
from backend import db
from backend.logger import get_logger

log = get_logger("dashboard.backup")

NAME_RE = re.compile(r"asset_dashboard_\d{8}_\d{6}\.db")


def _dir() -> str:
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    return config.BACKUP_DIR


def list_backups() -> list[dict]:
    d = _dir()
    out = []
    for name in os.listdir(d):
        if NAME_RE.fullmatch(name):
            st = os.stat(os.path.join(d, name))
            out.append({"name": name, "size_bytes": st.st_size,
                        "mtime": datetime.datetime.fromtimestamp(
                            st.st_mtime, tz=datetime.timezone.utc).isoformat(timespec="seconds")})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def resolve_backup(name: str) -> str | None:
    """Absolute path of an existing backup, or None for anything not a backup name."""
    if not NAME_RE.fullmatch(name or ""):
        return None
    d = os.path.realpath(_dir())
    path = os.path.realpath(os.path.join(d, name))
    if os.path.dirname(path) != d or not os.path.isfile(path):
        return None
    return path


def create_backup() -> str:
    d = _dir()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"asset_dashboard_{stamp}.db"
    final = os.path.join(d, name)
    tmp = final + ".tmp"
    src = db.connect()
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
        keep = max(1, db.get_int_setting(src, "backup_keep_count", 14))
    finally:
        src.close()
    if check != "ok":
        os.remove(tmp)
        raise RuntimeError(f"backup integrity check failed: {check}")
    os.replace(tmp, final)
    pruned = 0
    for old in list_backups()[keep:]:
        os.remove(os.path.join(d, old["name"]))
        pruned += 1
    size_mb = os.path.getsize(final) / 1048576
    log.info("backup written %s (%.2f MB), pruned %d", final, size_mb, pruned)
    return f"{name} ({size_mb:.2f} MB), pruned {pruned}"
