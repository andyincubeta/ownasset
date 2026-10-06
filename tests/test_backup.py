import os
import sqlite3

import pytest

import config
from backend import backup, db, scheduler


@pytest.fixture()
def bdir(tmp_db, tmp_path, monkeypatch):
    d = tmp_path / "backups"
    monkeypatch.setattr(config, "BACKUP_DIR", str(d))
    return d


def test_backup_copy_matches(conn, bdir):
    conn.execute("INSERT INTO settings(key, value) VALUES ('x_test', '1')")
    conn.commit()
    msg = backup.create_backup()
    files = backup.list_backups()
    assert len(files) == 1 and files[0]["name"] in msg
    copy = sqlite3.connect(str(bdir / files[0]["name"]))
    try:
        assert copy.execute("SELECT value FROM settings WHERE key = 'x_test'").fetchone()[0] == "1"
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()


def test_prune_keeps_newest(conn, bdir):
    db.set_setting(conn, "backup_keep_count", "2")
    conn.commit()
    os.makedirs(bdir)
    for stamp in ("20250101_000000", "20250102_000000", "20250103_000000"):
        (bdir / f"asset_dashboard_{stamp}.db").write_bytes(b"")
    backup.create_backup()
    names = [b["name"] for b in backup.list_backups()]
    assert len(names) == 2
    assert "asset_dashboard_20250103_000000.db" in names


@pytest.mark.parametrize("name", ["../test.db", "foo.db", "/etc/passwd",
                                  "asset_dashboard_20250101_000000.db"])
def test_resolve_rejects(bdir, name):
    assert backup.resolve_backup(name) is None


def test_backup_api(client, bdir):
    backup.create_backup()
    rows = client.get("/api/settings/backups").get_json()
    assert len(rows) == 1
    res = client.get(f"/api/settings/backups/{rows[0]['name']}")
    assert res.status_code == 200 and res.data[:15] == b"SQLite format 3"
    res.close()
    assert client.get("/api/settings/backups/..%2Ftest.db").status_code == 404
    assert client.get("/api/settings/backups/nope.db").status_code == 404


def test_backup_settings_validation(client):
    assert client.post("/api/settings", json={"backup_time": "25:00"}).status_code == 400
    assert client.post("/api/settings", json={"backup_keep_count": "0"}).status_code == 400
    assert client.post("/api/settings", json={"backup_keep_count": "7",
                                              "backup_time": "01:15"}).status_code == 200


def test_backup_job_registered():
    assert "backup" in scheduler.JOB_NAMES
    assert scheduler.run_job_now("nonexistent") is False
