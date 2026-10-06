"""Import batch tracking: history, undo (delete inserted rows), redo (re-run)."""
import io


CSV_TEXT = (
    "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
    '1/5/2024,買進,2,Batch buy,AAPL,現金,180,"-360.00"\n'
    "2/5/2024,股息,0,Batch dividend,,現金,,5\n"
)


def _commit(client, acc, **extra):
    return client.post(
        "/api/transactions/import/commit",
        data={"provider": "firstrade", "account_id": str(acc),
              "file": (io.BytesIO(CSV_TEXT.encode("utf-8")), "batch.csv"), **extra},
        content_type="multipart/form-data")


def test_commit_records_batch(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    res = _commit(client, acc)
    assert res.status_code == 200
    body = res.get_json()
    assert body["summary"]["inserted_rows"] == 2
    assert body["batch_id"] is not None

    batches = client.get("/api/transactions/import/batches").get_json()
    assert len(batches) == 1
    b = batches[0]
    assert b["id"] == body["batch_id"]
    assert b["provider"] == "firstrade"
    assert b["file_name"] == "batch.csv"
    assert b["inserted_count"] == 2
    assert b["remaining_count"] == 2
    assert b["reverted_at"] is None


def test_undo_deletes_only_batch_rows(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    before = len(client.get("/api/transactions?limit=500").get_json())
    batch_id = _commit(client, acc).get_json()["batch_id"]
    assert len(client.get("/api/transactions?limit=500").get_json()) == before + 2

    undo = client.post(f"/api/transactions/import/batches/{batch_id}/undo")
    assert undo.status_code == 200
    assert undo.get_json()["deleted_rows"] == 2
    assert len(client.get("/api/transactions?limit=500").get_json()) == before

    b = client.get("/api/transactions/import/batches").get_json()[0]
    assert b["reverted_at"] is not None and b["remaining_count"] == 0

    # a second undo is refused
    assert client.post(f"/api/transactions/import/batches/{batch_id}/undo").status_code == 400
    assert client.post("/api/transactions/import/batches/99999/undo").status_code == 404


def test_redo_after_undo_reimports(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    batch_id = _commit(client, acc).get_json()["batch_id"]
    client.post(f"/api/transactions/import/batches/{batch_id}/undo")

    redo = client.post(f"/api/transactions/import/batches/{batch_id}/redo", json={})
    assert redo.status_code == 200
    body = redo.get_json()
    assert body["summary"]["inserted_rows"] == 2       # rows were deleted, no dups
    assert body["batch_id"] != batch_id                # a fresh batch is recorded
    assert len(client.get("/api/transactions/import/batches").get_json()) == 2


def test_redo_without_undo_skips_duplicates(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    batch_id = _commit(client, acc).get_json()["batch_id"]
    before = len(client.get("/api/transactions?limit=500").get_json())

    redo = client.post(f"/api/transactions/import/batches/{batch_id}/redo", json={})
    assert redo.status_code == 200
    assert redo.get_json()["summary"]["inserted_rows"] == 0
    assert len(client.get("/api/transactions?limit=500").get_json()) == before

    # opting in re-imports the duplicates, mirroring the commit checkbox
    forced = client.post(f"/api/transactions/import/batches/{batch_id}/redo",
                         json={"include_duplicates": True})
    assert forced.get_json()["summary"]["inserted_rows"] == 2

    assert client.post("/api/transactions/import/batches/99999/redo",
                       json={}).status_code == 404


def _imported_buy(client):
    txns = client.get("/api/transactions?limit=500").get_json()
    return next(t for t in txns if t["inflow_amount"] == 2)


def test_reimport_after_split_skips_duplicates(client, sample_portfolio):
    """Splitting an imported txn must not make a re-import see it as new."""
    acc = sample_portfolio["account_id"]
    batch_id = _commit(client, acc).get_json()["batch_id"]
    buy = _imported_buy(client)
    res = client.post(f"/api/transactions/{buy['id']}/split",
                      json={"leg": "inflow", "quantity": 1})
    assert res.status_code == 200

    redo = client.post(f"/api/transactions/import/batches/{batch_id}/redo", json={})
    body = redo.get_json()
    assert body["summary"]["inserted_rows"] == 0
    assert body["summary"]["duplicate_rows"] == 2


def test_reimport_after_edit_skips_duplicates(client, sample_portfolio):
    acc = sample_portfolio["account_id"]
    batch_id = _commit(client, acc).get_json()["batch_id"]
    buy = _imported_buy(client)
    assert client.put(f"/api/transactions/{buy['id']}",
                      json={"outflow_amount": 999}).status_code == 200

    redo = client.post(f"/api/transactions/import/batches/{batch_id}/redo", json={})
    assert redo.get_json()["summary"]["inserted_rows"] == 0


def test_undo_after_split_lets_redo_reimport(client, sample_portfolio):
    """Undo drops the batch rows AND their fingerprints, so redo works again."""
    acc = sample_portfolio["account_id"]
    batch_id = _commit(client, acc).get_json()["batch_id"]
    buy = _imported_buy(client)
    client.post(f"/api/transactions/{buy['id']}/split", json={"leg": "inflow", "quantity": 1})
    client.post(f"/api/transactions/import/batches/{batch_id}/undo")

    redo = client.post(f"/api/transactions/import/batches/{batch_id}/redo", json={})
    assert redo.get_json()["summary"]["inserted_rows"] == 2


def test_edit_back_to_original_counts_once(client, sample_portfolio):
    """A txn edited A->B->A matches both live and fingerprint: count once."""
    acc = sample_portfolio["account_id"]
    _commit(client, acc)
    buy = _imported_buy(client)
    client.put(f"/api/transactions/{buy['id']}", json={"outflow_amount": 999})
    client.put(f"/api/transactions/{buy['id']}", json={"outflow_amount": 360})

    two_rows = (
        "日期,交易類別,數量,說明,代號,賬戶類別,價格,金額\n"
        '1/5/2024,買進,2,Batch buy,AAPL,現金,180,"-360.00"\n'
        '1/5/2024,買進,2,Batch buy,AAPL,現金,180,"-360.00"\n'
    )
    res = client.post(
        "/api/transactions/import/preview",
        data={"provider": "firstrade", "account_id": str(acc),
              "file": (io.BytesIO(two_rows.encode("utf-8")), "two.csv")},
        content_type="multipart/form-data")
    s = res.get_json()["summary"]
    assert s["duplicate_rows"] == 1
    assert s["valid_rows"] == 1


def test_delete_txn_clears_fingerprints(client, sample_portfolio):
    """Deleting a split txn removes its fingerprints: re-import sees it as new."""
    acc = sample_portfolio["account_id"]
    batch_id = _commit(client, acc).get_json()["batch_id"]
    buy = _imported_buy(client)
    new_id = client.post(f"/api/transactions/{buy['id']}/split",
                         json={"leg": "inflow", "quantity": 1}).get_json()["new_id"]
    client.delete(f"/api/transactions/{buy['id']}")
    client.delete(f"/api/transactions/{new_id}")

    redo = client.post(f"/api/transactions/import/batches/{batch_id}/redo", json={})
    # buy is new again; the untouched dividend row is still a duplicate
    assert redo.get_json()["summary"]["inserted_rows"] == 1
