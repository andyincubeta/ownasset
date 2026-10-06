"""Disposable income: cash deposited into the portfolio that is not an
investment return (salary savings etc.).

Entries are ordinary deposit transactions with the external party label
DISPOSABLE INCOME. They raise portfolio value, and because they are external
flows (one side external, one side own account) the TWR / Modified Dietz math
in api_performance strips them from the return index automatically.
"""
from flask import Blueprint, jsonify, request

from backend import db, fx, snapshots
from backend.logger import get_logger

bp = Blueprint("api_disposable", __name__, url_prefix="/api/disposable")
log = get_logger("dashboard.api")

PARTY = "DISPOSABLE INCOME"

LIST_SQL = """
SELECT t.id, t.txn_date, t.note, t.inflow_account_id AS account_id,
       t.inflow_asset_id AS asset_id, t.inflow_amount AS amount,
       a.ticker, a.currency,
       CASE WHEN acc.custodian = '' THEN acc.name
            ELSE acc.custodian || ' / ' || acc.name END AS account_label
  FROM transactions t
  JOIN assets a ON a.id = t.inflow_asset_id
  JOIN accounts acc ON acc.id = t.inflow_account_id
 WHERE t.outflow_account_id IS NULL AND UPPER(t.outflow_party) = ?
 ORDER BY t.txn_date DESC, t.id DESC
"""


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _norm_date(value) -> str:
    s = str(value or "").strip().replace("T", " ")
    if not s:
        return db.now_str()
    if len(s) == 10:
        return s + " 00:00:00"
    if len(s) == 16:
        return s + ":00"
    return s


def _resync(conn, *dates):
    today = db.today_str()
    valid = [str(d)[:10] for d in dates if d]
    start = min(valid) if valid else None
    if not start or start > today:
        return
    try:
        if conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone() is None:
            snapshots.rebuild_snapshots(conn)  # empty ledger: full wipe of stale rows
        else:
            snapshots.rebuild_snapshots(conn, start, today)
    except ValueError as exc:
        log.warning("disposable resync skipped (%s..%s): %s", start, today, exc)


def _validate(conn, data: dict) -> tuple[dict | None, str | None]:
    try:
        account_id = int(data.get("account_id") or 0)
        asset_id = int(data.get("asset_id") or 0)
        amount = float(data.get("amount") or 0)
    except (TypeError, ValueError):
        return None, "account_id, asset_id and amount must be numbers"
    if amount <= 0:
        return None, "amount must be > 0"
    if not conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
        return None, "account not found"
    asset = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if asset is None:
        return None, "asset not found"
    if asset["asset_type"] != "cash":
        return None, "asset must be a cash asset"
    return {"account_id": account_id, "asset_id": asset_id, "amount": amount,
            "txn_date": _norm_date(data.get("txn_date")),
            "note": (data.get("note") or "").strip()}, None


def _fetch(conn, entry_id: int):
    return conn.execute(
        "SELECT * FROM transactions WHERE id = ? AND outflow_account_id IS NULL "
        "AND UPPER(outflow_party) = ?", (entry_id, PARTY)).fetchone()


@bp.get("")
def list_entries():
    with db.db_conn() as conn:
        display = db.get_setting(conn, "display_currency", "USD")
        rows = db.rows_to_dicts(conn.execute(LIST_SQL, (PARTY,)))
        total = 0.0
        for r in rows:
            val, ok = fx.convert(conn, r["amount"], r["currency"], display,
                                 on_date=r["txn_date"][:10])
            r["amount_display"] = val
            r["fx_ok"] = ok
            total += val or 0.0
    return jsonify({"display_currency": display, "total_display": total, "entries": rows})


@bp.post("")
def create_entry():
    with db.db_conn() as conn:
        t, err = _validate(conn, _body())
        if err:
            return jsonify({"error": err}), 400
        now = db.now_str()
        cur = conn.execute(
            """INSERT INTO transactions(txn_date, outflow_account_id, outflow_party,
               outflow_asset_id, outflow_amount, inflow_account_id, inflow_party,
               inflow_asset_id, inflow_amount, fee_amount, note, created_at, updated_at)
               VALUES (?, NULL, ?, ?, ?, ?, '', ?, ?, 0, ?, ?, ?)""",
            (t["txn_date"], PARTY, t["asset_id"], t["amount"], t["account_id"],
             t["asset_id"], t["amount"], t["note"], now, now))
        entry_id = cur.lastrowid
        _resync(conn, t["txn_date"])
    log.info("disposable income %d created (%s)", entry_id, t["txn_date"])
    return jsonify({"id": entry_id}), 201


@bp.put("/<int:entry_id>")
def update_entry(entry_id):
    with db.db_conn() as conn:
        row = _fetch(conn, entry_id)
        if row is None:
            return jsonify({"error": "not found"}), 404
        merged = {"account_id": row["inflow_account_id"], "asset_id": row["inflow_asset_id"],
                  "amount": row["inflow_amount"], "txn_date": row["txn_date"],
                  "note": row["note"]}
        merged.update({k: v for k, v in _body().items() if k in merged})
        t, err = _validate(conn, merged)
        if err:
            return jsonify({"error": err}), 400
        conn.execute(
            """UPDATE transactions SET txn_date=?, outflow_asset_id=?, outflow_amount=?,
               inflow_account_id=?, inflow_asset_id=?, inflow_amount=?, note=?,
               updated_at=? WHERE id = ?""",
            (t["txn_date"], t["asset_id"], t["amount"], t["account_id"], t["asset_id"],
             t["amount"], t["note"], db.now_str(), entry_id))
        _resync(conn, row["txn_date"], t["txn_date"])
    log.info("disposable income %d updated", entry_id)
    return jsonify({"ok": True})


@bp.delete("/<int:entry_id>")
def delete_entry(entry_id):
    with db.db_conn() as conn:
        row = _fetch(conn, entry_id)
        if row is None:
            return jsonify({"error": "not found"}), 404
        conn.execute("DELETE FROM txn_fingerprints WHERE txn_id = ?", (entry_id,))
        conn.execute("DELETE FROM transactions WHERE id = ?", (entry_id,))
        _resync(conn, row["txn_date"])
    log.info("disposable income %d deleted", entry_id)
    return jsonify({"ok": True})
