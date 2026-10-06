"""Accounts (first-class parties w/ custodian) and currencies CRUD."""
import sqlite3

from flask import Blueprint, jsonify, request

from backend import db
from backend.db import account_label
from backend.logger import get_logger

bp = Blueprint("api_accounts", __name__, url_prefix="/api")
log = get_logger("dashboard.api")

CUSTODIAN_TYPES = ("bank", "broker", "exchange", "wallet", "other")


def _body() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/accounts")
def list_accounts():
    with db.db_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM accounts ORDER BY custodian, name")]
    for r in rows:
        r["label"] = account_label(r)
    return jsonify(rows)


@bp.post("/accounts")
def create_account():
    data = _body()
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    ctype = data.get("custodian_type") or "bank"
    if ctype not in CUSTODIAN_TYPES:
        return jsonify({"error": f"custodian_type must be one of {CUSTODIAN_TYPES}"}), 400
    try:
        with db.db_conn() as conn:
            cur = conn.execute(
                "INSERT INTO accounts(name, custodian_type, custodian, base_currency, "
                "note) VALUES (?, ?, ?, ?, ?)",
                (name, ctype, (data.get("custodian") or "").strip(),
                 (data.get("base_currency") or "USD").upper(),
                 data.get("note") or ""))
            log.info("account created: %s/%s (%s)", data.get("custodian"), name, ctype)
            return jsonify({"id": cur.lastrowid}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "account with this custodian + name already exists"}), 409


@bp.put("/accounts/<int:account_id>")
def update_account(account_id):
    data = _body()
    with db.db_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        ctype = data.get("custodian_type", row["custodian_type"])
        if ctype not in CUSTODIAN_TYPES:
            return jsonify({"error": f"custodian_type must be one of {CUSTODIAN_TYPES}"}), 400
        try:
            conn.execute(
                "UPDATE accounts SET name = ?, custodian_type = ?, custodian = ?, "
                "base_currency = ?, note = ? WHERE id = ?",
                (data.get("name", row["name"]), ctype,
                 data.get("custodian", row["custodian"]),
                 (data.get("base_currency") or row["base_currency"]).upper(),
                 data.get("note", row["note"]), account_id))
        except sqlite3.IntegrityError:
            return jsonify({"error": "account with this custodian + name already exists"}), 409
    return jsonify({"ok": True})


@bp.delete("/accounts/<int:account_id>")
def delete_account(account_id):
    with db.db_conn() as conn:
        used = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE outflow_account_id = ? "
            "OR inflow_account_id = ? OR fee_account_id = ?",
            (account_id, account_id, account_id)).fetchone()[0]
        if used:
            return jsonify({"error": "account is referenced by transactions"}), 409
        conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return jsonify({"ok": True})


@bp.get("/currencies")
def list_currencies():
    with db.db_conn() as conn:
        return jsonify(db.rows_to_dicts(conn.execute(
            "SELECT * FROM currencies ORDER BY CASE WHEN code = 'USD' THEN 0 "
            "WHEN code = 'HKD' THEN 1 WHEN code = 'CNY' THEN 2 ELSE 3 END, code")))


@bp.post("/currencies")
def create_currency():
    data = _body()
    code = (data.get("code") or "").strip().upper()
    if not code or len(code) > 5:
        return jsonify({"error": "valid code required"}), 400
    with db.db_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO currencies(code, name) VALUES (?, ?)",
                     (code, data.get("name") or code))
        db.ensure_cash_asset(conn, code, data.get("name") or code)
    log.info("currency added: %s", code)
    return jsonify({"code": code}), 201
