"""Tests for POST /batches/{batch_id}/scans/restore.

The tests simulate an old-backup recovery by deleting rows from ``scans``
directly (the API never deletes scans) and, where needed, rewinding
``sqlite_sequence`` so newly accepted scans reuse freed storage sequence
numbers — exactly what happens after a real backup restore.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import service
from app.config import Settings
from app.db import Database
from app.main import create_app


@pytest.fixture()
def env(tmp_path):
    settings = Settings(db_path=str(tmp_path / "test.db"), recalc_delay_seconds=0.0)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client, Database(settings.db_path)


def register(client: TestClient, batch_id: str = "B-1", cartons=None):
    if cartons is None:
        cartons = [
            {"carton_no": "CTN-1", "serials": ["SN-1", "SN-2"]},
            {"carton_no": "CTN-2", "serials": ["SN-3", "SN-4"]},
        ]
    response = client.post("/batches", json={"batch_id": batch_id, "cartons": cartons})
    assert response.status_code == 201, response.text
    return response.json()


def scan(client: TestClient, batch_id: str, scan_id: str, point: str, **kwargs):
    payload = {"scan_id": scan_id, "receiving_point": point, **kwargs}
    return client.post(f"/batches/{batch_id}/scans", json=payload)


def restore(client: TestClient, batch_id: str = "B-1"):
    return client.post(f"/batches/{batch_id}/scans/restore")


def drop_scan(db: Database, batch_id: str, scan_id: str, rewind_sequence: bool = True):
    """Delete one scan row as an old-backup restore would.

    ``rewind_sequence`` resets the AUTOINCREMENT counter to the surviving
    maximum id, so scans accepted afterwards reuse freed sequence numbers.
    """
    with db.immediate() as conn:
        conn.execute(
            "DELETE FROM scans WHERE batch_id = ? AND scan_id = ?",
            (batch_id, scan_id),
        )
        if rewind_sequence:
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'scans'")
            conn.execute(
                """
                INSERT INTO sqlite_sequence (name, seq)
                SELECT 'scans', COALESCE(MAX(id), 0) FROM scans
                """
            )


def scan_rows(db: Database, batch_id: str = "B-1"):
    with db.read() as conn:
        return conn.execute(
            "SELECT * FROM scans WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_restore_backfills_missing_rows_preserving_original_fields(env):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "收货点A", serial_no="SN-1")
    scan(client, "B-1", "SC-2", "收货点A", carton_no="CTN-1", seal_intact=True)
    scan(client, "B-1", "SC-3", "收货点B", serial_no="SN-3")
    before = client.get("/batches/B-1/result").json()
    assert before["received_count"] == 3

    drop_scan(db, "B-1", "SC-2")
    drop_scan(db, "B-1", "SC-3")
    assert [row["scan_id"] for row in scan_rows(db)] == ["SC-1"]

    response = restore(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "restored"
    assert body["restored_count"] == 2

    restored = {item["scan_id"]: item for item in body["restored"]}
    assert set(restored) == {"SC-2", "SC-3"}
    # Original storage sequence numbers and payload fields are preserved.
    assert restored["SC-2"]["seq"] == 2
    assert restored["SC-2"]["kind"] == "carton"
    assert restored["SC-2"]["carton_no"] == "CTN-1"
    assert restored["SC-2"]["seal_intact"] is True
    assert restored["SC-3"]["seq"] == 3
    assert restored["SC-3"]["serial_no"] == "SN-3"
    assert restored["SC-3"]["receiving_point"] == "收货点B"
    assert restored["SC-3"]["accepted_at"]
    assert restored["SC-2"]["source_result_ids"]

    # The scans table holds the backfilled rows at their original positions.
    rows = scan_rows(db)
    assert [(row["id"], row["scan_id"]) for row in rows] == [
        (1, "SC-1"),
        (2, "SC-2"),
        (3, "SC-3"),
    ]
    assert rows[1]["accepted_at"] == restored["SC-2"]["accepted_at"]

    # The rebuilt active result includes the backfilled evidence.
    result = body["result"]
    assert result["received_count"] == 3
    assert {item["serial_no"] for item in result["received"]} == {
        "SN-1",
        "SN-2",
        "SN-3",
    }
    assert result["scan_high_watermark"] == 3
    assert client.get("/batches/B-1/result").json()["result_id"] == result["result_id"]


def test_restore_dedupes_scan_expanded_differently_across_versions(env):
    """One scan is backfilled once even if versions expand it differently."""
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", carton_no="CTN-1", seal_intact=True)
    # Version 2 moves SN-1 out of CTN-1, so SC-1 expands to ["SN-2"] there.
    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-1", "carton_no": "CTN-2"}]},
    )
    assert response.status_code == 200

    results = client.get("/batches/B-1/results").json()
    expansions = set()
    for summary in results:
        detail = client.get(f"/batches/B-1/results/{summary['result_id']}").json()
        row = next(
            (d for d in detail["details"] if d["scan_id"] == "SC-1"), None
        )
        if row is not None:
            expansions.add(tuple(row["expanded_serials"]))
    assert expansions == {("SN-1", "SN-2"), ("SN-2",)}

    drop_scan(db, "B-1", "SC-1")
    response = restore(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restored_count"] == 1
    restored = body["restored"][0]
    assert restored["scan_id"] == "SC-1"
    assert restored["carton_no"] == "CTN-1"
    # The detail appeared in several historical results; all are cited.
    assert len(restored["source_result_ids"]) >= 2
    # Exactly one row exists afterwards.
    assert [row["scan_id"] for row in scan_rows(db)] == ["SC-1"]


def test_restore_rebuilds_result_with_effective_packing_list(env):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", carton_no="CTN-1", seal_intact=True)
    client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-1", "carton_no": "CTN-2"}]},
    )
    drop_scan(db, "B-1", "SC-1")

    body = restore(client).json()
    assert body["status"] == "restored"
    # The result is rebuilt against the packing list effective at commit time.
    assert body["result"]["packing_list_version"] == 2
    detail = next(d for d in body["result"]["details"] if d["scan_id"] == "SC-1")
    assert detail["expanded_serials"] == ["SN-2"]


def test_restore_keeps_history_results_queryable_by_original_id(env):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    history_before = client.get("/batches/B-1/results").json()

    drop_scan(db, "B-1", "SC-1")
    body = restore(client).json()
    assert body["status"] == "restored"

    history_after = client.get("/batches/B-1/results").json()
    assert len(history_after) == len(history_before) + 1
    assert history_after[0]["result_id"] == body["result"]["result_id"]
    assert history_after[0]["active"] is True

    # Historical results are untouched and still queryable by their ids.
    for summary in history_before:
        old = client.get(f"/batches/B-1/results/{summary['result_id']}").json()
        assert old["result_id"] == summary["result_id"]
    newest_before = client.get(
        f"/batches/B-1/results/{history_before[0]['result_id']}"
    ).json()
    assert newest_before["details"][0]["scan_id"] == "SC-1"


def test_restore_no_missing_returns_noop_and_is_idempotent(env):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")

    response = restore(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_missing"
    assert body["restored_count"] == 0
    assert body["restored"] == []
    assert body["result"] is None
    assert "无需补回" in body["message"]

    # A retry after a successful repair also reports no_missing.
    drop_scan(db, "B-1", "SC-1")
    assert restore(client).json()["status"] == "restored"
    again = restore(client).json()
    assert again["status"] == "no_missing"
    assert again["restored_count"] == 0


def test_restore_unknown_batch_is_404(env):
    client, _ = env
    response = client.post("/batches/NOPE/scans/restore")
    assert response.status_code == 404


def test_scans_still_accepted_around_restore(env):
    """Scan intake keeps working before/after the repair comparison."""
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-2", "A", serial_no="SN-2")
    # Keep the sequence counter intact so the next scan gets a fresh seq.
    drop_scan(db, "B-1", "SC-2", rewind_sequence=False)

    # A new scan is accepted while the table is still missing a row.
    response = scan(client, "B-1", "SC-3", "A", serial_no="SN-3")
    assert response.status_code == 201

    body = restore(client).json()
    assert body["status"] == "restored"
    assert [item["scan_id"] for item in body["restored"]] == ["SC-2"]
    # The rebuilt result covers backfilled and newly accepted scans alike.
    assert {item["serial_no"] for item in body["result"]["received"]} == {
        "SN-1",
        "SN-2",
        "SN-3",
    }


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def test_restore_seq_conflict_stops_repair_and_names_sources(env):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-2", "A", serial_no="SN-2")

    # Backup restore loses SC-2 (seq 2); the next accepted scan reuses seq 2.
    drop_scan(db, "B-1", "SC-2")
    reused = scan(client, "B-1", "SC-NEW", "B", serial_no="SN-3")
    assert reused.status_code == 201
    assert [row["id"] for row in scan_rows(db)] == [1, 2]

    response = restore(client)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "已停止修复" in detail["message"]
    conflict = next(c for c in detail["conflicts"] if c["type"] == "seq")
    assert conflict["seq"] == 2
    assert conflict["history"]["source"] == "历史核对结果"
    assert conflict["history"]["scan_id"] == "SC-2"
    assert conflict["history"]["result_ids"]
    assert conflict["history"]["record"]["serial_no"] == "SN-2"
    assert conflict["existing"]["source"] == "现存扫描表"
    assert conflict["existing"]["scan_id"] == "SC-NEW"
    assert conflict["existing"]["record"]["serial_no"] == "SN-3"

    # Repair stopped: nothing was backfilled.
    assert [row["scan_id"] for row in scan_rows(db)] == ["SC-1", "SC-NEW"]


def test_restore_scan_id_conflict_stops_repair(env):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-2", "A", serial_no="SN-2")

    # The restored backup row for SC-2 disagrees with the historical detail.
    with db.immediate() as conn:
        conn.execute(
            "UPDATE scans SET receiving_point = '收货点Z' "
            "WHERE batch_id = 'B-1' AND scan_id = 'SC-2'"
        )

    response = restore(client)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    conflict = next(c for c in detail["conflicts"] if c["type"] == "scan_id")
    assert conflict["scan_id"] == "SC-2"
    assert conflict["history"]["record"]["receiving_point"] == "A"
    assert conflict["history"]["record"]["seq"] == 2
    assert conflict["existing"]["record"]["receiving_point"] == "收货点Z"
    # Nothing written.
    assert [row["scan_id"] for row in scan_rows(db)] == ["SC-1", "SC-2"]


def test_restore_detects_inconsistent_history_details(env):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    # A second result carries the same scan detail.
    refresh = client.post("/batches/B-1/result/refresh")
    assert refresh.status_code == 200

    # Tamper one historical copy so the two results disagree on the record.
    with db.immediate() as conn:
        result_ids = [
            row["result_id"]
            for row in conn.execute(
                """
                SELECT DISTINCT result_id FROM result_scan_details
                 WHERE scan_id = 'SC-1' ORDER BY result_id
                """
            ).fetchall()
        ]
        assert len(result_ids) == 2
        conn.execute(
            "UPDATE result_scan_details SET accepted_at = '被篡改' "
            "WHERE result_id = ? AND scan_id = 'SC-1'",
            (result_ids[0],),
        )
        conn.execute("DELETE FROM scans WHERE batch_id = 'B-1' AND scan_id = 'SC-1'")

    response = restore(client)
    assert response.status_code == 409
    detail = response.json()["detail"]
    conflict = next(c for c in detail["conflicts"] if c["type"] == "history")
    assert conflict["scan_id"] == "SC-1"
    assert {r["result_id"] for r in conflict["records"]} == set(result_ids)
    assert scan_rows(db) == []


# ---------------------------------------------------------------------------
# Concurrency / failure handling
# ---------------------------------------------------------------------------


def test_restore_returns_409_while_correction_pending(tmp_path):
    settings = Settings(db_path=str(tmp_path / "pending.db"), recalc_delay_seconds=0.6)
    app = create_app(settings)
    with TestClient(app) as client:
        register(client)
        scan(client, "B-1", "SC-1", "A", serial_no="SN-1")

        outcome: dict[str, object] = {}

        def correct():
            outcome["response"] = client.post(
                "/batches/B-1/packing-lists/corrections",
                json={"moves": [{"serial_no": "SN-3", "carton_no": "CTN-1"}]},
            )

        thread = threading.Thread(target=correct)
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if client.get("/batches/B-1").json()["recalculation_in_progress"]:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("pending version never became visible")

        response = restore(client)
        assert response.status_code == 409
        assert "稍后重试" in response.text

        thread.join(timeout=10)
        assert outcome["response"].status_code == 200

        # Once the correction settles, the restore proceeds normally.
        assert restore(client).json()["status"] == "no_missing"


def test_restore_rolls_back_on_write_failure(env, monkeypatch):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-2", "A", serial_no="SN-2")
    drop_scan(db, "B-1", "SC-2")

    original = service.refresh_result_locked

    def explode(*args, **kwargs):
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(service, "refresh_result_locked", explode)
    response = restore(client)
    assert response.status_code == 500
    assert "已回滚" in response.text
    # The inserted row was rolled back with the rest of the repair.
    assert [row["scan_id"] for row in scan_rows(db)] == ["SC-1"]
    monkeypatch.setattr(service, "refresh_result_locked", original)

    # Retrying after the rollback succeeds.
    assert restore(client).json()["status"] == "restored"
    assert [row["scan_id"] for row in scan_rows(db)] == ["SC-1", "SC-2"]


def test_restore_retry_after_success_reports_no_missing(env):
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    drop_scan(db, "B-1", "SC-1")
    assert restore(client).json()["status"] == "restored"
    assert restore(client).json()["status"] == "no_missing"


def test_restore_rechecks_conflicts_at_commit_time(env, monkeypatch):
    """A scan accepted between comparison and commit reuses a freed seq.

    The read-phase plan is clean, but the writer transaction must re-verify
    and abort instead of inserting over the newcomer.
    """
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-2", "A", serial_no="SN-2")
    drop_scan(db, "B-1", "SC-2")  # frees seq 2

    original = service._load_restore_plan
    calls = {"n": 0}

    def spy(conn, batch_id):
        calls["n"] += 1
        plan = original(conn, batch_id)
        if calls["n"] == 1:
            # Right after the read-phase comparison, a new scan is accepted
            # and reuses the freed storage sequence number.
            assert scan(client, "B-1", "SC-NEW", "B", serial_no="SN-3").status_code == 201
        return plan

    monkeypatch.setattr(service, "_load_restore_plan", spy)
    response = restore(client)
    assert response.status_code == 409
    conflict = next(
        c for c in response.json()["detail"]["conflicts"] if c["type"] == "seq"
    )
    assert conflict["history"]["scan_id"] == "SC-2"
    assert conflict["existing"]["scan_id"] == "SC-NEW"
    assert [row["scan_id"] for row in scan_rows(db)] == ["SC-1", "SC-NEW"]


def test_restore_backfills_unmatched_scans(env):
    """Unmatched scans (unknown serial) are part of the history and restore."""
    client, db = env
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-X", "A", serial_no="SN-999")  # unmatched
    before = client.get("/batches/B-1/result").json()
    assert before["unmatched_scan_count"] == 1

    drop_scan(db, "B-1", "SC-X")
    body = restore(client).json()
    assert body["status"] == "restored"
    assert [item["scan_id"] for item in body["restored"]] == ["SC-X"]
    assert body["restored"][0]["serial_no"] == "SN-999"
    assert body["result"]["unmatched_scan_count"] == 1
    detail = next(d for d in body["result"]["details"] if d["scan_id"] == "SC-X")
    assert detail["unmatched"] is True


# ---------------------------------------------------------------------------
# Pure comparison unit tests
# ---------------------------------------------------------------------------


def _detail_row(result_id, seq, scan_id, point="A", carton=None, serial=None,
                seal=None, accepted="2026-01-01T00:00:00+00:00"):
    return {
        "result_id": result_id,
        "seq": seq,
        "scan_id": scan_id,
        "receiving_point": point,
        "carton_no": carton,
        "serial_no": serial,
        "seal_intact": seal,
        "accepted_at": accepted,
    }


def test_plan_ignores_version_specific_expansion():
    rows = [
        _detail_row(1, 1, "SC-1", carton="CTN-1", seal=1),
        _detail_row(2, 1, "SC-1", carton="CTN-1", seal=1),
    ]
    plan = service.plan_scan_restore(rows, [])
    assert plan["conflicts"] == []
    assert [r["scan_id"] for r in plan["missing"]] == ["SC-1"]
    assert plan["missing"][0]["source_result_ids"] == [1, 2]


def test_plan_flags_contradictory_history():
    rows = [
        _detail_row(1, 1, "SC-1", serial="SN-1"),
        _detail_row(2, 1, "SC-1", serial="SN-2"),
    ]
    plan = service.plan_scan_restore(rows, [])
    assert plan["missing"] == []
    assert plan["conflicts"][0]["type"] == "history"
    assert len(plan["conflicts"][0]["records"]) == 2
