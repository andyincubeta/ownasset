"""Transaction ledger CRUD with filters.

A transaction moves value between an OUTFLOW party and an INFLOW party.
Each party is either one of the user's accounts (affects holdings) or an
external free-text label (no holdings effect). Both sides may reference the
same account (e.g. pay USD, receive stock inside one broker account).
txn_date carries a time component; missing/short inputs are normalized.
"""
import csv
import io
import json
import re

from flask import Blueprint, Response, jsonify, request

from backend import db
from backend.importers.transactions_csv import (
    PROVIDER_DEFAULT_CCY,
    parse_transactions_csv,
    suggest_unknown_asset,
)
from backend.logger import get_logger

bp = Blueprint("api_transactions", __name__, url_prefix="/api/transactions")
log = get_logger("dashboard.api")

ACC_LABEL = ("CASE WHEN {a}.id IS NULL THEN NULL "
             "WHEN {a}.custodian = '' THEN {a}.name "
             "ELSE {a}.custodian || ' / ' || {a}.name END")

LIST_SQL = f"""
SELECT t.*,
       ao.ticker AS outflow_ticker, ai.ticker AS inflow_ticker, af.ticker AS fee_ticker,
       {ACC_LABEL.format(a='oa')} AS outflow_account_label,
       {ACC_LABEL.format(a='ia')} AS inflow_account_label,
       {ACC_LABEL.format(a='fa')} AS fee_account_label,
       g.name AS group_name,
       ib.batch_id AS import_batch_id
  FROM transactions t
  LEFT JOIN assets ao ON ao.id = t.outflow_asset_id
  LEFT JOIN assets ai ON ai.id = t.inflow_asset_id
  LEFT JOIN assets af ON af.id = t.fee_asset_id
  LEFT JOIN accounts oa ON oa.id = t.outflow_account_id
  LEFT JOIN accounts ia ON ia.id = t.inflow_account_id
  LEFT JOIN accounts fa ON fa.id = t.fee_account_id
  LEFT JOIN txn_groups g ON g.id = t.group_id
  LEFT JOIN import_batch_txns ib ON ib.txn_id = t.id
"""


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _norm_date(value) -> str:
    """'' -> now; 'YYYY-MM-DDTHH:MM' -> 'YYYY-MM-DD HH:MM:00'; date-only -> midnight."""
    s = str(value or "").strip().replace("T", " ")
    if not s:
        return db.now_str()
    if len(s) == 10:
        return s + " 00:00:00"
    if len(s) == 16:
        return s + ":00"
    return s


def _clean(data: dict) -> dict:
    """Coerce ids/amounts; empty -> None/0."""
    out = {}
    for key in ("outflow_account_id", "inflow_account_id", "fee_account_id",
                "outflow_asset_id", "inflow_asset_id", "fee_asset_id"):
        v = data.get(key)
        out[key] = int(v) if v not in (None, "", 0) else None
    for key in ("outflow_amount", "inflow_amount", "fee_amount"):
        try:
            out[key] = float(data.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0.0
    out["outflow_party"] = (data.get("outflow_party") or "").strip()
    out["inflow_party"] = (data.get("inflow_party") or "").strip()
    # a side without a complete flow (asset + amount > 0) stores no party/unit
    for side in ("outflow", "inflow"):
        if not out[f"{side}_asset_id"] or out[f"{side}_amount"] <= 0:
            out[f"{side}_account_id"] = None
            out[f"{side}_asset_id"] = None
            out[f"{side}_amount"] = 0.0
            out[f"{side}_party"] = ""
    out["note"] = data.get("note") or ""
    out["txn_date"] = _norm_date(data.get("txn_date"))
    return out


def _validate(t: dict) -> str | None:
    has_out = t["outflow_asset_id"] and t["outflow_amount"] > 0
    has_in = t["inflow_asset_id"] and t["inflow_amount"] > 0
    if not has_out and not has_in:
        return "at least one flow (asset + amount > 0) required"
    if not t["outflow_account_id"] and not t["inflow_account_id"]:
        return "at least one party must be one of your accounts"
    if has_out and t["outflow_account_id"] is None and not t["outflow_party"]:
        return "outflow party: choose an account or enter an external name"
    if has_in and t["inflow_account_id"] is None and not t["inflow_party"]:
        return "inflow party: choose an account or enter an external name"
    if t["fee_amount"] > 0:
        if not t["fee_asset_id"]:
            return "fee asset required when fee amount > 0"
        if not t["fee_account_id"]:
            t["fee_account_id"] = t["outflow_account_id"] or t["inflow_account_id"]
    return None


def _list_query(apply_filters: bool = True, apply_limit: bool = True) -> tuple[str, list]:
    clauses, args = [], []
    if apply_filters and request.args.get("account_id"):
        aid = int(request.args["account_id"])
        clauses.append("(t.outflow_account_id = ? OR t.inflow_account_id = ? "
                       "OR t.fee_account_id = ?)")
        args += [aid, aid, aid]
    if apply_filters and request.args.get("asset_id"):
        clauses.append("(t.outflow_asset_id = ? OR t.inflow_asset_id = ?)")
        args += [int(request.args["asset_id"])] * 2
    if apply_filters and request.args.get("group_id"):
        clauses.append("t.group_id = ?")
        args.append(int(request.args["group_id"]))
    if apply_filters and request.args.get("start"):
        clauses.append("t.txn_date >= ?")
        args.append(request.args["start"])
    if apply_filters and request.args.get("end"):
        end = request.args["end"]
        clauses.append("t.txn_date <= ?")
        args.append(end + " 23:59:59" if len(end) == 10 else end)
    if apply_filters and request.args.get("q"):
        clauses.append("(t.note LIKE ? OR t.outflow_party LIKE ? OR t.inflow_party LIKE ?)")
        args += [f"%{request.args['q']}%"] * 3
    sql = LIST_SQL + (" WHERE " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY t.txn_date DESC, t.id DESC"
    if apply_limit:
        sql += " LIMIT ?"
        args.append(int(request.args.get("limit", 500)))
    return sql, args


def _asset_maps(conn) -> tuple[dict[str, int], set[str]]:
    rows = db.rows_to_dicts(conn.execute("SELECT id, ticker, asset_type FROM assets"))
    by_ticker = {(r["ticker"] or "").upper(): r["id"] for r in rows}
    cash = {(r["ticker"] or "").upper() for r in rows if r["asset_type"] == "cash"}
    return by_ticker, cash


def _csv_upload_bytes(max_bytes: int = 2_000_000) -> bytes:
    f = request.files.get("file")
    if f is None:
        raise ValueError("file is required")
    raw = f.read(max_bytes + 1)
    if not raw:
        raise ValueError("uploaded file is empty")
    if len(raw) > max_bytes:
        raise ValueError("file too large (max 2MB)")
    return raw


def _import_params() -> tuple[str, int, bytes, dict[int, str]]:
    provider = (request.form.get("provider") or "").strip().lower()
    if provider not in ("firstrade", "ib"):
        raise ValueError("provider must be firstrade or ib")
    account_raw = request.form.get("account_id")
    if account_raw in (None, ""):
        raise ValueError("account_id is required")
    try:
        account_id = int(account_raw)
    except ValueError as exc:
        raise ValueError("account_id must be an integer") from exc
    raw = _csv_upload_bytes()
    try:
        overrides_json = json.loads(request.form.get("ticker_overrides") or "{}")
        overrides = {int(k): str(v).strip().upper()
                     for k, v in overrides_json.items() if str(v).strip()}
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("ticker_overrides must be a JSON object of row -> ticker") from exc
    if len(overrides) > 10000:
        raise ValueError("too many ticker overrides")
    return provider, account_id, raw, overrides


def _preview_rows(provider: str, account_id: int, raw: bytes,
                  overrides: dict[int, str] | None = None):
    with db.db_conn() as conn:
        account_exists = conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not account_exists:
            raise ValueError("account not found")
        base_ccy = db.get_setting(conn, f"import_ccy_{provider}", PROVIDER_DEFAULT_CCY[provider])
        by_ticker, cash_tickers = _asset_maps(conn)
        parsed = parse_transactions_csv(provider, raw, account_id, by_ticker, base_ccy,
                                        cash_tickers=cash_tickers,
                                        ticker_overrides=overrides)
    max_rows = 10000
    if len(parsed) > max_rows:
        raise ValueError("too many rows (max 10000)")
    normalized = []
    for row in parsed:
        payload = row.payload or None
        if payload is not None:
            payload = _clean(payload)
            err = _validate(payload)
            if err:
                row.status = "invalid"
                row.error = err
                payload = None
        normalized.append((row, payload))
    _mark_duplicates(normalized)
    return normalized


# every persisted transaction column: two rows matching on all of these are
# considered the same real-world transaction (re-imported file)
DUP_MATCH_COLS = ("txn_date", "outflow_account_id", "outflow_party", "outflow_asset_id",
                  "outflow_amount", "inflow_account_id", "inflow_party", "inflow_asset_id",
                  "inflow_amount", "fee_account_id", "fee_asset_id", "fee_amount", "note")


def _record_fingerprint(conn, txn_row) -> None:
    """Snapshot a txn's dedupe fields before a split/edit mutates them, so a
    re-import of the original file still sees the pre-change row as present."""
    t = dict(txn_row)
    where = " AND ".join(f"{c} IS ?" for c in DUP_MATCH_COLS)
    vals = tuple(t[c] for c in DUP_MATCH_COLS)
    if conn.execute(f"SELECT 1 FROM txn_fingerprints WHERE txn_id = ? AND {where}",
                    (t["id"], *vals)).fetchone():
        return
    cols = ", ".join(DUP_MATCH_COLS)
    marks = ", ".join("?" * len(DUP_MATCH_COLS))
    conn.execute(f"INSERT INTO txn_fingerprints(txn_id, {cols}, created_at) "
                 f"VALUES (?, {marks}, ?)", (t["id"], *vals, db.now_str()))


def _mark_duplicates(rows_with_payload: list[tuple]) -> None:
    """Flag rows identical to existing DB transactions as status='duplicate'.
    Count-aware: N identical rows in the DB mark only the first N identical
    file rows, so a legitimately repeated fill still imports. Fingerprints of
    split/edited txns count too (UNION dedupes a txn matching both ways)."""
    groups: dict[tuple, list] = {}
    for row, payload in rows_with_payload:
        if row.status == "ok" and payload is not None:
            key = tuple(payload[c] for c in DUP_MATCH_COLS)
            groups.setdefault(key, []).append(row)
    if not groups:
        return
    where = " AND ".join(f"{c} IS ?" for c in DUP_MATCH_COLS)  # IS: NULL-safe
    with db.db_conn() as conn:
        for key, rows in groups.items():
            n = conn.execute(
                f"SELECT COUNT(*) FROM ("
                f"SELECT id FROM transactions WHERE {where} "
                f"UNION SELECT txn_id FROM txn_fingerprints WHERE {where})",
                key + key).fetchone()[0]
            for row in rows[:n]:
                row.status = "duplicate"
                row.error = "identical transaction already in the ledger"


def _preview_payload(rows_with_payload: list[tuple]):
    with db.db_conn() as conn:
        ticker_rows = db.rows_to_dicts(conn.execute("SELECT id, ticker FROM assets"))
    ticker_by_id = {r["id"]: r["ticker"] for r in ticker_rows}
    unknown_re = re.compile(r"unknown (?:ticker|asset): (.+)$")
    unknown_counts: dict[str, int] = {}
    unknown_hints: dict[str, dict] = {}
    rows = []
    valid = 0
    invalid = 0
    skipped = 0
    duplicate = 0
    for row, payload in rows_with_payload:
        if row.status == "ok" and payload is not None:
            valid += 1
        elif row.status == "skipped":
            skipped += 1
        elif row.status == "duplicate":
            duplicate += 1
        else:
            invalid += 1
            m = unknown_re.search(row.error or "")
            if m:
                t = m.group(1).strip().upper()
                unknown_counts[t] = unknown_counts.get(t, 0) + 1
                # hint applies only when the missing asset is the row's own symbol
                if row.asset_hint and t == (row.ticker or "").upper():
                    unknown_hints.setdefault(t, row.asset_hint)
        rows.append({
            "row": row.row,
            "status": row.status,
            "txn_kind": row.txn_kind,
            "ticker": row.ticker or "",
            "txn_date": payload["txn_date"] if payload else None,
            "outflow_ticker": ticker_by_id.get(payload["outflow_asset_id"]) if payload else None,
            "outflow_amount": payload["outflow_amount"] if payload else None,
            "inflow_ticker": ticker_by_id.get(payload["inflow_asset_id"]) if payload else None,
            "inflow_amount": payload["inflow_amount"] if payload else None,
            "note": payload["note"] if payload else "",
            "error": row.error,
        })
    return {
        "rows": rows,
        "unknown_tickers": [
            {"ticker": t, "count": unknown_counts[t],
             "suggestion": unknown_hints.get(t) or suggest_unknown_asset(t)}
            for t in sorted(unknown_counts)],
        "summary": {
            "total_rows": len(rows),
            "valid_rows": valid,
            "invalid_rows": invalid,
            "skipped_rows": skipped,
            "duplicate_rows": duplicate,
        },
    }


@bp.get("")
def list_transactions():
    sql, args = _list_query(apply_filters=True, apply_limit=True)
    with db.db_conn() as conn:
        return jsonify(db.rows_to_dicts(conn.execute(sql, args)))


@bp.get("/<int:txn_id>")
def get_transaction(txn_id):
    with db.db_conn() as conn:
        row = conn.execute(LIST_SQL + " WHERE t.id = ?", (txn_id,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@bp.get("/csv")
def transactions_csv():
    sql, args = _list_query(apply_filters=False, apply_limit=False)
    with db.db_conn() as conn:
        rows = db.rows_to_dicts(conn.execute(sql, args))

    def _party(account_label, party):
        return account_label or party or ""

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["ID", "DATE / TIME", "OUTFLOW PARTY", "OUTFLOW AMOUNT", "OUTFLOW UNIT",
                     "INFLOW PARTY", "INFLOW AMOUNT", "INFLOW UNIT", "FEE", "NOTE",
                     "UPDATED"])
    for t in rows:
        fee = ""
        if t["fee_asset_id"] and t["fee_amount"]:
            fee = f"{t['fee_amount']} {t['fee_ticker'] or ''}".strip()
        writer.writerow([
            t["id"],
            t["txn_date"],
            _party(t["outflow_account_label"], t["outflow_party"]),
            "" if not t["outflow_asset_id"] else t["outflow_amount"],
            t["outflow_ticker"] or "",
            _party(t["inflow_account_label"], t["inflow_party"]),
            "" if not t["inflow_asset_id"] else t["inflow_amount"],
            t["inflow_ticker"] or "",
            fee,
            t["note"] or "",
            t["updated_at"],
        ])

    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transactions_all.csv"})


@bp.post("/import/preview")
def preview_import_transactions():
    try:
        provider, account_id, raw, overrides = _import_params()
        rows_with_payload = _preview_rows(provider, account_id, raw, overrides)
        return jsonify(_preview_payload(rows_with_payload))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def _seed_manual_prices(conn, seeds: dict[tuple[str, str], float]) -> None:
    """Bond flex rows carry a per-1-face price; keep manual_prices in sync."""
    ids = {(r["ticker"] or "").upper(): r["id"]
           for r in conn.execute("SELECT id, ticker FROM assets")}
    for (ticker, price_date), price in seeds.items():
        asset_id = ids.get(ticker)
        if asset_id:
            conn.execute(
                "INSERT INTO manual_prices(asset_id, price_date, price, note) "
                "VALUES (?, ?, ?, 'ib import') "
                "ON CONFLICT(asset_id, price_date) DO UPDATE SET price = excluded.price",
                (asset_id, price_date, price))


def _commit_rows(rows_with_payload, provider: str, account_id: int, raw: bytes,
                 overrides: dict[int, str], include_dups: bool,
                 file_name: str) -> tuple[int | None, int]:
    """Insert importable rows and record them as an import batch (for undo/redo)."""
    now = db.now_str()
    inserted_ids: list[int] = []
    price_seeds: dict[tuple[str, str], float] = {}
    with db.db_conn() as conn:
        for row, payload in rows_with_payload:
            if payload is None or row.status not in ("ok", "duplicate"):
                continue
            if row.status == "duplicate" and not include_dups:
                continue
            cur = conn.execute(
                """INSERT INTO transactions(txn_date, outflow_account_id, outflow_party,
                   outflow_asset_id, outflow_amount, inflow_account_id, inflow_party,
                   inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount,
                   note, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (payload["txn_date"], payload["outflow_account_id"], payload["outflow_party"],
                 payload["outflow_asset_id"], payload["outflow_amount"], payload["inflow_account_id"],
                 payload["inflow_party"], payload["inflow_asset_id"], payload["inflow_amount"],
                 payload["fee_account_id"], payload["fee_asset_id"], payload["fee_amount"],
                 payload["note"], now, now))
            inserted_ids.append(cur.lastrowid)
            if row.price_info:
                info = row.price_info
                price_seeds[(info["ticker"], info["date"])] = info["price"]
        if price_seeds:
            _seed_manual_prices(conn, price_seeds)
        batch_id = None
        if inserted_ids:
            cur = conn.execute(
                """INSERT INTO import_batches(created_at, provider, account_id, file_name,
                   file_content, ticker_overrides, include_duplicates, inserted_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, provider, account_id, file_name, raw,
                 json.dumps({str(k): v for k, v in (overrides or {}).items()}),
                 1 if include_dups else 0, len(inserted_ids)))
            batch_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO import_batch_txns(batch_id, txn_id) VALUES (?, ?)",
                [(batch_id, txn_id) for txn_id in inserted_ids])
    return batch_id, len(inserted_ids)


@bp.post("/import/commit")
def commit_import_transactions():
    try:
        provider, account_id, raw, overrides = _import_params()
        rows_with_payload = _preview_rows(provider, account_id, raw, overrides)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    include_dups = (request.form.get("include_duplicates") or "").strip().lower() \
        in ("1", "true", "yes")
    file_obj = request.files.get("file")
    file_name = (file_obj.filename or "") if file_obj else ""
    batch_id, inserted = _commit_rows(rows_with_payload, provider, account_id, raw,
                                      overrides, include_dups, file_name)
    log.info("imported %d transactions from provider=%s account=%d (batch=%s)",
             inserted, provider, account_id, batch_id)
    preview = _preview_payload(rows_with_payload)
    preview["summary"]["inserted_rows"] = inserted
    preview["batch_id"] = batch_id
    return jsonify(preview)


@bp.get("/import/batches")
def list_import_batches():
    label = ACC_LABEL.format(a="a")
    with db.db_conn() as conn:
        rows = db.rows_to_dicts(conn.execute(
            f"""SELECT b.id, b.created_at, b.provider, b.account_id, b.file_name,
                       b.include_duplicates, b.inserted_count, b.reverted_at,
                       {label} AS account_label,
                       (SELECT COUNT(*) FROM import_batch_txns ib
                         JOIN transactions t ON t.id = ib.txn_id
                        WHERE ib.batch_id = b.id) AS remaining_count
                  FROM import_batches b
                  LEFT JOIN accounts a ON a.id = b.account_id
                 ORDER BY b.id DESC LIMIT 20"""))
    return jsonify(rows)


@bp.post("/import/batches/<int:batch_id>/undo")
def undo_import_batch(batch_id):
    with db.db_conn() as conn:
        b = conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
        if b is None:
            return jsonify({"error": "batch not found"}), 404
        if b["reverted_at"]:
            return jsonify({"error": "batch already reverted"}), 400
        conn.execute(
            "DELETE FROM txn_fingerprints WHERE txn_id IN "
            "(SELECT txn_id FROM import_batch_txns WHERE batch_id = ?)", (batch_id,))
        cur = conn.execute(
            "DELETE FROM transactions WHERE id IN "
            "(SELECT txn_id FROM import_batch_txns WHERE batch_id = ?)", (batch_id,))
        deleted = cur.rowcount
        conn.execute("UPDATE import_batches SET reverted_at = ? WHERE id = ?",
                     (db.now_str(), batch_id))
    log.info("import batch %d reverted: %d transactions deleted", batch_id, deleted)
    return jsonify({"ok": True, "deleted_rows": deleted})


@bp.post("/import/batches/<int:batch_id>/redo")
def redo_import_batch(batch_id):
    body = request.get_json(silent=True) or {}
    include_dups = bool(body.get("include_duplicates", False))
    with db.db_conn() as conn:
        b = conn.execute("SELECT * FROM import_batches WHERE id = ?", (batch_id,)).fetchone()
    if b is None:
        return jsonify({"error": "batch not found"}), 404
    if not b["file_content"]:
        return jsonify({"error": "no stored file for this batch"}), 400
    try:
        overrides = {int(k): str(v) for k, v in
                     json.loads(b["ticker_overrides"] or "{}").items()}
        rows_with_payload = _preview_rows(b["provider"], b["account_id"],
                                          b["file_content"], overrides)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    new_batch_id, inserted = _commit_rows(rows_with_payload, b["provider"], b["account_id"],
                                          b["file_content"], overrides, include_dups,
                                          b["file_name"])
    log.info("import batch %d re-run as batch %s: %d inserted", batch_id, new_batch_id, inserted)
    preview = _preview_payload(rows_with_payload)
    preview["summary"]["inserted_rows"] = inserted
    preview["batch_id"] = new_batch_id
    return jsonify(preview)


@bp.post("")
def create_transaction():
    t = _clean(_body())
    err = _validate(t)
    if err:
        return jsonify({"error": err}), 400
    now = db.now_str()
    with db.db_conn() as conn:
        cur = conn.execute(
            """INSERT INTO transactions(txn_date, outflow_account_id, outflow_party,
               outflow_asset_id, outflow_amount, inflow_account_id, inflow_party,
               inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount,
               note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (t["txn_date"], t["outflow_account_id"], t["outflow_party"],
             t["outflow_asset_id"], t["outflow_amount"], t["inflow_account_id"],
             t["inflow_party"], t["inflow_asset_id"], t["inflow_amount"],
             t["fee_account_id"], t["fee_asset_id"], t["fee_amount"],
             t["note"], now, now))
        txn_id = cur.lastrowid
    log.info("transaction %d created (%s)", txn_id, t["txn_date"])
    return jsonify({"id": txn_id}), 201


@bp.put("/<int:txn_id>")
def update_transaction(txn_id):
    data = _body()
    with db.db_conn() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        merged = {**dict(row), **{k: v for k, v in data.items() if k in dict(row)}}
        t = _clean(merged)
        err = _validate(t)
        if err:
            return jsonify({"error": err}), 400
        if any(t[c] != row[c] for c in DUP_MATCH_COLS):
            _record_fingerprint(conn, row)
        conn.execute(
            """UPDATE transactions SET txn_date=?, outflow_account_id=?, outflow_party=?,
               outflow_asset_id=?, outflow_amount=?, inflow_account_id=?, inflow_party=?,
               inflow_asset_id=?, inflow_amount=?, fee_account_id=?, fee_asset_id=?,
               fee_amount=?, note=?, updated_at=? WHERE id = ?""",
            (t["txn_date"], t["outflow_account_id"], t["outflow_party"],
             t["outflow_asset_id"], t["outflow_amount"], t["inflow_account_id"],
             t["inflow_party"], t["inflow_asset_id"], t["inflow_amount"],
             t["fee_account_id"], t["fee_asset_id"], t["fee_amount"],
             t["note"], db.now_str(), txn_id))
    log.info("transaction %d updated", txn_id)
    return jsonify({"ok": True})


@bp.delete("/<int:txn_id>")
def delete_transaction(txn_id):
    with db.db_conn() as conn:
        conn.execute("DELETE FROM txn_fingerprints WHERE txn_id = ?", (txn_id,))
        conn.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    log.info("transaction %d deleted", txn_id)
    return jsonify({"ok": True})


@bp.post("/delete-many")
def delete_transactions():
    ids = _body().get("txn_ids") or []
    try:
        if not isinstance(ids, list) or not ids:
            raise ValueError
        ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        return jsonify({"error": "txn_ids must be a non-empty list of ids"}), 400
    with db.db_conn() as conn:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM txn_fingerprints WHERE txn_id IN ({marks})", ids)
        cur = conn.execute(f"DELETE FROM transactions WHERE id IN ({marks})", ids)
        deleted = cur.rowcount
    log.info("bulk delete: %d transactions removed", deleted)
    return jsonify({"ok": True, "deleted": deleted})


@bp.post("/<int:txn_id>/split")
def split_transaction(txn_id):
    """Split one transaction into two, pro-rata on all amounts (incl. fee).
    Body: {leg: 'inflow'|'outflow', quantity: part to split off}."""
    data = _body()
    leg = (data.get("leg") or "").strip().lower()
    if leg not in ("inflow", "outflow"):
        return jsonify({"error": "leg must be inflow or outflow"}), 400
    try:
        part_qty = float(data.get("quantity") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "quantity must be a number"}), 400
    with db.db_conn() as conn:
        row = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        if row is None:
            return jsonify({"error": "not found"}), 404
        t = dict(row)
        leg_amount = t[f"{leg}_amount"] or 0.0
        if not t[f"{leg}_asset_id"] or leg_amount <= 0:
            return jsonify({"error": f"transaction has no {leg} to split"}), 400
        if not (0 < part_qty < leg_amount):
            return jsonify({"error": f"quantity must be between 0 and {leg_amount}"}), 400
        _record_fingerprint(conn, row)
        ratio = part_qty / leg_amount
        # remainder = original - part, so the two rows always sum to the original
        part = {k: (t[k] or 0.0) * ratio
                for k in ("outflow_amount", "inflow_amount", "fee_amount")}
        part[f"{leg}_amount"] = part_qty
        now = db.now_str()
        conn.execute(
            "UPDATE transactions SET outflow_amount = ?, inflow_amount = ?, "
            "fee_amount = ?, updated_at = ? WHERE id = ?",
            ((t["outflow_amount"] or 0.0) - part["outflow_amount"],
             (t["inflow_amount"] or 0.0) - part["inflow_amount"],
             (t["fee_amount"] or 0.0) - part["fee_amount"], now, txn_id))
        cur = conn.execute(
            """INSERT INTO transactions(txn_date, outflow_account_id, outflow_party,
               outflow_asset_id, outflow_amount, inflow_account_id, inflow_party,
               inflow_asset_id, inflow_amount, fee_account_id, fee_asset_id, fee_amount,
               note, group_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (t["txn_date"], t["outflow_account_id"], t["outflow_party"],
             t["outflow_asset_id"], part["outflow_amount"], t["inflow_account_id"],
             t["inflow_party"], t["inflow_asset_id"], part["inflow_amount"],
             t["fee_account_id"], t["fee_asset_id"], part["fee_amount"],
             t["note"], t["group_id"], now, now))
        new_id = cur.lastrowid
    log.info("transaction %d split: %s %s -> new transaction %d", txn_id, part_qty, leg, new_id)
    return jsonify({"ok": True, "original_id": txn_id, "new_id": new_id})
