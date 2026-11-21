"""Verification domain logic and persistence orchestration.

Key invariants
--------------
* Every packing-list version is immutable once written.  A correction creates a
  new ``pending`` version and only makes it active when its verification result
  has been fully written.
* The active result is a persisted snapshot.  Readers always see a complete
  snapshot for one packing-list version; received, conflict, unevidenced and
  scan-detail rows are written and activated in one transaction.
* While a pending version exists, scan intake keeps committing, queries keep
  reading the previous active result, and the final activation picks up every
  scan accepted up to that moment.
* A failed correction leaves the previous active version and result untouched.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from .db import Database

logger = logging.getLogger(__name__)


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ServiceError(Exception):
    status_code = 400

    def __init__(self, detail: Any):
        super().__init__(str(detail))
        self.detail = detail


class NotFoundError(ServiceError):
    status_code = 404


class ConflictError(ServiceError):
    status_code = 409


class InvalidCorrectionError(ServiceError):
    status_code = 422


class InternalError(ServiceError):
    status_code = 500


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _batch_row(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"批次 {batch_id} 不存在")
    return row


def _active_version_row(
    conn: sqlite3.Connection, batch_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM packing_list_versions
         WHERE batch_id = ? AND status = 'active'
        """,
        (batch_id,),
    ).fetchone()


def _pending_version_row(
    conn: sqlite3.Connection, batch_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM packing_list_versions
         WHERE batch_id = ? AND status = 'pending'
        """,
        (batch_id,),
    ).fetchone()


def _version_row(
    conn: sqlite3.Connection, batch_id: str, version: int
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT * FROM packing_list_versions
         WHERE batch_id = ? AND version = ?
        """,
        (batch_id, version),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"批次 {batch_id} 的装箱单版本 {version} 不存在")
    return row


# ---------------------------------------------------------------------------
# Packing-list representation
# ---------------------------------------------------------------------------


class PackingList:
    """Ordered packing list used by the verification calculation."""

    __slots__ = ("by_carton", "cartons", "items", "ordered_items")

    def __init__(
        self,
        cartons: list[tuple[str, int]],
        items: dict[str, tuple[str, int]],
        by_carton: dict[str, list[str]],
        ordered_items: list[str],
    ) -> None:
        self.cartons = cartons
        self.items = items
        self.by_carton = by_carton
        self.ordered_items = ordered_items


def load_packing_list(conn: sqlite3.Connection, version_id: int) -> PackingList:
    carton_rows = conn.execute(
        """
        SELECT carton_no, position FROM packing_list_cartons
         WHERE version_id = ? ORDER BY position
        """,
        (version_id,),
    ).fetchall()
    item_rows = conn.execute(
        """
        SELECT i.serial_no, i.carton_no, i.position
          FROM packing_list_items i
          JOIN packing_list_cartons c
            ON c.version_id = i.version_id AND c.carton_no = i.carton_no
         WHERE i.version_id = ?
         ORDER BY c.position, i.position
        """,
        (version_id,),
    ).fetchall()
    cartons = [(row["carton_no"], row["position"]) for row in carton_rows]
    items: dict[str, tuple[str, int]] = {}
    by_carton: dict[str, list[str]] = {carton: [] for carton, _ in cartons}
    ordered_items: list[str] = []
    for row in item_rows:
        items[row["serial_no"]] = (row["carton_no"], row["position"])
        by_carton.setdefault(row["carton_no"], []).append(row["serial_no"])
        ordered_items.append(row["serial_no"])
    return PackingList(
        cartons=cartons,
        items=items,
        by_carton=by_carton,
        ordered_items=ordered_items,
    )


def _write_packing_list(
    conn: sqlite3.Connection,
    version_id: int,
    cartons: Sequence[str],
    by_carton: dict[str, list[str]],
    item_order: Sequence[str],
) -> None:
    """Persist an ordered packing list.

    ``item_order`` defines the global expected-item order.  Each item's
    ``position`` is its index within its carton, which is also the expansion
    order used for carton scans.
    """
    item_carton: dict[str, str] = {}
    item_position: dict[str, int] = {}
    for carton in cartons:
        for idx, serial in enumerate(by_carton.get(carton, [])):
            item_carton[serial] = carton
            item_position[serial] = idx

    missing = set(item_order) - set(item_carton)
    if missing:
        raise InvalidCorrectionError(
            {"message": "装箱单排序包含未知序列号", "serial_numbers": sorted(missing)}
        )

    conn.executemany(
        "INSERT INTO packing_list_cartons (version_id, carton_no, position) VALUES (?, ?, ?)",
        [(version_id, carton, idx) for idx, carton in enumerate(cartons)],
    )
    conn.executemany(
        """
        INSERT INTO packing_list_items (version_id, serial_no, carton_no, position)
        VALUES (?, ?, ?, ?)
        """,
        [
            (version_id, serial, item_carton[serial], item_position[serial])
            for serial in item_order
        ],
    )


# ---------------------------------------------------------------------------
# Verification calculation
# ---------------------------------------------------------------------------


def compute_verification(
    packing: PackingList, scans: Sequence[sqlite3.Row]
) -> dict[str, Any]:
    """Apply the acceptance rules to a packing list and a scan snapshot.

    Rules:
    * A carton scan expands to every serial declared in that carton for this
      packing-list version.  Unknown/empty cartons expand to nothing.
    * Evidence is deduplicated per (serial, receiving point): repeated scans of
      the same item at one point count as one receipt and keep all scan ids.
    * One point -> received.  More than one point -> conflict (with each
      point's scan ids).  No point -> unevidenced.
    """
    evidence: dict[str, dict[str, list[str]]] = {serial: {} for serial in packing.items}
    details: list[dict[str, Any]] = []

    for scan in scans:
        expanded: list[str] = []
        if scan["carton_no"] is not None:
            expanded = list(packing.by_carton.get(scan["carton_no"], []))
        else:
            serial = scan["serial_no"]
            if serial in packing.items:
                expanded = [serial]

        # ``unmatched`` means the scan could not be mapped to the packing list
        # at all: an unknown carton, or a serial absent from the list.  A known
        # carton that is currently empty still mapped successfully.
        if scan["carton_no"] is not None:
            mapped = scan["carton_no"] in packing.by_carton
        else:
            mapped = scan["serial_no"] in packing.items

        for serial in expanded:
            point_map = evidence[serial]
            scan_ids = point_map.setdefault(scan["receiving_point"], [])
            if scan["scan_id"] not in scan_ids:
                scan_ids.append(scan["scan_id"])

        details.append(
            {
                "seq": scan["id"],
                "scan_id": scan["scan_id"],
                "receiving_point": scan["receiving_point"],
                "kind": "carton" if scan["carton_no"] is not None else "serial",
                "carton_no": scan["carton_no"],
                "serial_no": scan["serial_no"],
                "seal_intact": (
                    None if scan["seal_intact"] is None else bool(scan["seal_intact"])
                ),
                "accepted_at": scan["accepted_at"],
                "expanded_serials": expanded,
                "unmatched": not mapped,
            }
        )

    ordered_items = [
        (serial, packing.items[serial]) for serial in packing.ordered_items
    ]

    received: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unevidenced: list[dict[str, Any]] = []

    for serial, (carton_no, _) in ordered_items:
        point_map = evidence[serial]
        if not point_map:
            unevidenced.append({"serial_no": serial, "carton_no": carton_no})
        elif len(point_map) == 1:
            point, scan_ids = next(iter(point_map.items()))
            received.append(
                {
                    "serial_no": serial,
                    "carton_no": carton_no,
                    "receiving_point": point,
                    "scan_ids": list(scan_ids),
                }
            )
        else:
            conflicts.append(
                {
                    "serial_no": serial,
                    "carton_no": carton_no,
                    "reports": [
                        {"receiving_point": point, "scan_ids": list(scan_ids)}
                        for point, scan_ids in point_map.items()
                    ],
                }
            )

    return {
        "expected_count": len(packing.items),
        "received": received,
        "conflicts": conflicts,
        "unevidenced": unevidenced,
        "details": details,
        "unmatched_scan_count": sum(1 for detail in details if detail["unmatched"]),
    }


def _persist_result(
    conn: sqlite3.Connection,
    batch_id: str,
    packing_list_version: int,
    scan_high_watermark: int,
    computed: dict[str, Any],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO verification_results (
            batch_id, packing_list_version, scan_high_watermark,
            expected_count, received_count, conflict_count, unevidenced_count,
            unmatched_scan_count, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            packing_list_version,
            scan_high_watermark,
            computed["expected_count"],
            len(computed["received"]),
            len(computed["conflicts"]),
            len(computed["unevidenced"]),
            computed["unmatched_scan_count"],
            now_utc(),
        ),
    )
    result_id = int(cursor.lastrowid)

    conn.executemany(
        """
        INSERT INTO result_received (
            result_id, serial_no, carton_no, receiving_point, scan_ids_json, position
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                result_id,
                item["serial_no"],
                item["carton_no"],
                item["receiving_point"],
                json.dumps(item["scan_ids"], ensure_ascii=False),
                position,
            )
            for position, item in enumerate(computed["received"])
        ],
    )
    conn.executemany(
        """
        INSERT INTO result_conflicts (
            result_id, serial_no, carton_no, reports_json, position
        ) VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                result_id,
                item["serial_no"],
                item["carton_no"],
                json.dumps(item["reports"], ensure_ascii=False),
                position,
            )
            for position, item in enumerate(computed["conflicts"])
        ],
    )
    conn.executemany(
        """
        INSERT INTO result_unevidenced (result_id, serial_no, carton_no, position)
        VALUES (?, ?, ?, ?)
        """,
        [
            (result_id, item["serial_no"], item["carton_no"], position)
            for position, item in enumerate(computed["unevidenced"])
        ],
    )
    conn.executemany(
        """
        INSERT INTO result_scan_details (
            result_id, seq, scan_id, receiving_point, kind, carton_no,
            serial_no, seal_intact, accepted_at, expanded_json, unmatched
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                result_id,
                detail["seq"],
                detail["scan_id"],
                detail["receiving_point"],
                detail["kind"],
                detail["carton_no"],
                detail["serial_no"],
                None if detail["seal_intact"] is None else int(detail["seal_intact"]),
                detail["accepted_at"],
                json.dumps(detail["expanded_serials"], ensure_ascii=False),
                int(detail["unmatched"]),
            )
            for detail in computed["details"]
        ],
    )
    return result_id


def _scans_snapshot(conn: sqlite3.Connection, batch_id: str) -> tuple[list[sqlite3.Row], int]:
    rows = conn.execute(
        "SELECT * FROM scans WHERE batch_id = ? ORDER BY id",
        (batch_id,),
    ).fetchall()
    watermark = rows[-1]["id"] if rows else 0
    return list(rows), int(watermark)


def refresh_result_locked(
    conn: sqlite3.Connection, batch_id: str, version_row: sqlite3.Row
) -> int:
    """Recompute and persist a result for ``version_row``. Writer txn required."""
    packing = load_packing_list(conn, version_row["id"])
    scans, watermark = _scans_snapshot(conn, batch_id)
    computed = compute_verification(packing, scans)
    result_id = _persist_result(
        conn, batch_id, version_row["version"], watermark, computed
    )
    conn.execute(
        "UPDATE batches SET active_result_id = ?, updated_at = ? WHERE batch_id = ?",
        (result_id, now_utc(), batch_id),
    )
    return result_id


def recover_on_startup(db: Database) -> None:
    """Close out recalculation attempts interrupted by a crash.

    A ``pending`` version can only exist while a correction request is running.
    After a restart that request will never activate, so it is marked failed;
    the previously active version keeps serving.  Any scan accepted during the
    dead window is folded into the active result so the stored snapshot stays
    converged.
    """
    with db.immediate() as conn:
        pending = conn.execute(
            """
            SELECT batch_id, id FROM packing_list_versions
             WHERE status = 'pending'
            """
        ).fetchall()
        for row in pending:
            conn.execute(
                """
                UPDATE packing_list_versions
                   SET status = 'failed', failure_reason = COALESCE(
                           failure_reason,
                           'recalculation interrupted by service restart'
                       )
                 WHERE id = ? AND status = 'pending'
                """,
                (row["id"],),
            )
            active = _active_version_row(conn, row["batch_id"])
            if active is not None:
                try:
                    refresh_result_locked(conn, row["batch_id"], active)
                except Exception:
                    # Serving the previous snapshot is still correct; the next
                    # accepted scan or explicit refresh will converge it.
                    logger.exception(
                        "startup reconcile failed for batch %s; serving previous result",
                        row["batch_id"],
                    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def create_batch(db: Database, payload: dict[str, Any]) -> int:
    """Register a batch and its first packing-list version. Returns result id."""
    batch_id = payload["batch_id"]
    cartons = payload["cartons"]
    timestamp = now_utc()

    carton_numbers = [carton["carton_no"] for carton in cartons]
    by_carton = {carton["carton_no"]: list(carton["serials"]) for carton in cartons}
    item_order = [serial for carton in carton_numbers for serial in by_carton[carton]]

    with db.immediate() as conn:
        try:
            conn.execute(
                """
                INSERT INTO batches (
                    batch_id, latest_packing_list_version, active_result_id,
                    created_at, updated_at
                ) VALUES (?, 1, NULL, ?, ?)
                """,
                (batch_id, timestamp, timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"批次 {batch_id} 已存在") from exc

        cursor = conn.execute(
            """
            INSERT INTO packing_list_versions (
                batch_id, version, status, created_at, activated_at
            ) VALUES (?, 1, 'active', ?, ?)
            """,
            (batch_id, timestamp, timestamp),
        )
        version_id = int(cursor.lastrowid)

        _write_packing_list(conn, version_id, carton_numbers, by_carton, item_order)
        version_row = conn.execute(
            "SELECT * FROM packing_list_versions WHERE id = ?", (version_id,)
        ).fetchone()
        result_id = refresh_result_locked(conn, batch_id, version_row)
    return result_id


def accept_scan(db: Database, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist one accepted scan.

    When no recalculation is pending, the active result is refreshed in the
    same transaction, so a subsequent query sees the scan.  While a packing
    list correction is being recalculated, the scan is still accepted but the
    active result is intentionally left untouched; activation of the new
    version will include it.
    """
    with db.immediate() as conn:
        _batch_row(conn, batch_id)
        pending = _pending_version_row(conn, batch_id)
        timestamp = now_utc()
        try:
            cursor = conn.execute(
                """
                INSERT INTO scans (
                    batch_id, scan_id, receiving_point, carton_no,
                    serial_no, seal_intact, accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    payload["scan_id"],
                    payload["receiving_point"],
                    payload.get("carton_no"),
                    payload.get("serial_no"),
                    None if payload.get("seal_intact") is None else int(payload["seal_intact"]),
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"扫描编号 {payload['scan_id']} 在本批次已存在") from exc
        scan_row = conn.execute(
            "SELECT * FROM scans WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()

        refreshed = False
        if pending is None:
            active = _active_version_row(conn, batch_id)
            if active is None:  # defensive: registration always creates one
                raise ConflictError(f"批次 {batch_id} 没有生效的装箱单")
            refresh_result_locked(conn, batch_id, active)
            refreshed = True

        batch = _batch_row(conn, batch_id)
    return {"scan": scan_row, "result_refreshed": refreshed, "batch": batch}


def refresh_active_result(db: Database, batch_id: str) -> int:
    """Explicitly recompute the active result with all accepted scans."""
    with db.immediate() as conn:
        _batch_row(conn, batch_id)
        if _pending_version_row(conn, batch_id) is not None:
            raise ConflictError(
                "装箱单更正正在重算，查询继续使用原装箱单的核对结果；请稍后重试"
            )
        active = _active_version_row(conn, batch_id)
        if active is None:
            raise ConflictError(f"批次 {batch_id} 没有生效的装箱单")
        return refresh_result_locked(conn, batch_id, active)


# ---------------------------------------------------------------------------
# Scan-table repair from historical verification results
# ---------------------------------------------------------------------------
#
# When the scans table is recovered from an old backup, some accepted rows are
# missing while every historical result still carries their scan details.
# ``restore_scans`` backfills the missing rows from those details, preserving
# the original scan id, storage sequence number (``scans.id``) and payload.
#
# The comparison runs on a read snapshot so scan intake keeps committing; the
# commit phase re-verifies under the write lock, inserts the missing rows and
# rebuilds the active result with the packing list effective at that moment —
# all in one transaction.


def _history_record(row: sqlite3.Row) -> dict[str, Any]:
    """Original scan fields carried by one historical result detail row."""
    return {
        "seq": int(row["seq"]),
        "scan_id": row["scan_id"],
        "receiving_point": row["receiving_point"],
        "carton_no": row["carton_no"],
        "serial_no": row["serial_no"],
        "seal_intact": (
            None if row["seal_intact"] is None else bool(row["seal_intact"])
        ),
        "accepted_at": row["accepted_at"],
    }


def _existing_record(row: sqlite3.Row) -> dict[str, Any]:
    """Original scan fields of a surviving ``scans`` row (id == seq)."""
    return {
        "seq": int(row["id"]),
        "scan_id": row["scan_id"],
        "receiving_point": row["receiving_point"],
        "carton_no": row["carton_no"],
        "serial_no": row["serial_no"],
        "seal_intact": (
            None if row["seal_intact"] is None else bool(row["seal_intact"])
        ),
        "accepted_at": row["accepted_at"],
    }


def _same_original(record: dict[str, Any], row: sqlite3.Row) -> bool:
    return (
        record["seq"] == int(row["id"])
        and record["scan_id"] == row["scan_id"]
        and record["receiving_point"] == row["receiving_point"]
        and record["carton_no"] == row["carton_no"]
        and record["serial_no"] == row["serial_no"]
        and record["seal_intact"]
        == (None if row["seal_intact"] is None else bool(row["seal_intact"]))
        and record["accepted_at"] == row["accepted_at"]
    )


def plan_scan_restore(
    history_rows: Sequence[sqlite3.Row], existing_rows: Sequence[sqlite3.Row]
) -> dict[str, Any]:
    """Compare historical scan details with surviving rows.

    Returns ``{"conflicts": [...], "missing": [...]}``.  ``missing`` rows are
    ready to insert (original seq/scan_id/fields plus ``source_result_ids``).
    Any conflict aborts the repair: the same batch scan id or the same global
    storage sequence number must never point at two different records.
    """
    conflicts: list[dict[str, Any]] = []

    # The same scan appears once per result that included it, expanded
    # differently under each packing-list version.  Only the original record
    # fields are compared; expansion/unmatched columns are version-specific
    # and intentionally ignored.
    by_scan_id: dict[str, list[sqlite3.Row]] = {}
    for row in history_rows:
        by_scan_id.setdefault(row["scan_id"], []).append(row)

    canonical: dict[str, dict[str, Any]] = {}
    for scan_id, rows in by_scan_id.items():
        entries = [(int(row["result_id"]), _history_record(row)) for row in rows]
        first_result_id, first = entries[0]
        mismatched = [
            {"source": "历史核对结果", "result_id": result_id, "record": record}
            for result_id, record in entries[1:]
            if record != first
        ]
        if mismatched:
            conflicts.append(
                {
                    "type": "history",
                    "scan_id": scan_id,
                    "records": [
                        {
                            "source": "历史核对结果",
                            "result_id": first_result_id,
                            "record": first,
                        },
                        *mismatched,
                    ],
                }
            )
            continue
        canonical[scan_id] = {
            **first,
            "source_result_ids": sorted({result_id for result_id, _ in entries}),
        }

    if conflicts:
        return {"conflicts": conflicts, "missing": []}

    existing_by_scan_id = {row["scan_id"]: row for row in existing_rows}
    existing_by_seq = {int(row["id"]): row for row in existing_rows}

    missing: list[dict[str, Any]] = []
    for scan_id, record in sorted(canonical.items(), key=lambda kv: kv[1]["seq"]):
        public_record = {
            key: record[key] for key in record if key != "source_result_ids"
        }
        existing = existing_by_scan_id.get(scan_id)
        if existing is not None:
            if not _same_original(record, existing):
                conflicts.append(
                    {
                        "type": "scan_id",
                        "scan_id": scan_id,
                        "history": {
                            "source": "历史核对结果",
                            "result_ids": record["source_result_ids"],
                            "record": public_record,
                        },
                        "existing": {
                            "source": "现存扫描表",
                            "record": _existing_record(existing),
                        },
                    }
                )
            continue
        occupant = existing_by_seq.get(record["seq"])
        if occupant is not None:
            conflicts.append(
                {
                    "type": "seq",
                    "seq": record["seq"],
                    "history": {
                        "source": "历史核对结果",
                        "scan_id": scan_id,
                        "result_ids": record["source_result_ids"],
                        "record": public_record,
                    },
                    "existing": {
                        "source": "现存扫描表",
                        "scan_id": occupant["scan_id"],
                        "record": _existing_record(occupant),
                    },
                }
            )
            continue
        missing.append(record)

    if not conflicts:
        # Two different missing scan ids claiming one storage sequence number
        # means the historical details contradict each other.
        seq_owner: dict[int, str] = {}
        for record in missing:
            other = seq_owner.get(record["seq"])
            if other is not None:
                conflicts.append(
                    {
                        "type": "history",
                        "seq": record["seq"],
                        "scan_ids": [other, record["scan_id"]],
                        "message": (
                            f"入库序号 {record['seq']} 在历史明细中同时对应"
                            f"扫描号 {other} 与 {record['scan_id']}"
                        ),
                    }
                )
            else:
                seq_owner[record["seq"]] = record["scan_id"]

    return {"conflicts": conflicts, "missing": [] if conflicts else missing}


def _load_restore_plan(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    history_rows = conn.execute(
        """
        SELECT d.result_id, d.seq, d.scan_id, d.receiving_point,
               d.carton_no, d.serial_no, d.seal_intact, d.accepted_at
          FROM result_scan_details d
          JOIN verification_results r ON r.id = d.result_id
         WHERE r.batch_id = ?
         ORDER BY d.seq, d.result_id
        """,
        (batch_id,),
    ).fetchall()
    existing_rows = conn.execute(
        "SELECT * FROM scans WHERE batch_id = ? ORDER BY id", (batch_id,)
    ).fetchall()
    return plan_scan_restore(history_rows, existing_rows)


def _conflict_payload(conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "message": "历史扫描明细与现存扫描记录存在冲突，已停止修复",
        "conflicts": conflicts,
    }


def restore_scans(db: Database, batch_id: str) -> dict[str, Any]:
    """Backfill scan rows lost to an old backup restore.

    Phase 1 (read snapshot): collect historical scan details and compare them
    with surviving rows.  Scan intake keeps committing during this phase.
    Phase 2 (writer transaction): re-verify under the write lock (a scan
    accepted meanwhile may have reused a missing sequence number), insert the
    missing rows preserving their original storage sequence numbers, and
    rebuild the active result with the packing list effective at commit time.
    Any write failure rolls the whole repair back; once every historical scan
    is present the repair reports ``no_missing``.
    """
    with db.read() as conn:
        _batch_row(conn, batch_id)
        if _pending_version_row(conn, batch_id) is not None:
            raise ConflictError("装箱单更正正在重算，请稍后重试")
        preview = _load_restore_plan(conn, batch_id)
        if preview["conflicts"]:
            raise ConflictError(_conflict_payload(preview["conflicts"]))
        if not preview["missing"]:
            return {"status": "no_missing", "restored": [], "result_id": None}

    try:
        with db.immediate() as conn:
            _batch_row(conn, batch_id)
            if _pending_version_row(conn, batch_id) is not None:
                raise ConflictError("装箱单更正正在重算，请稍后重试")
            plan = _load_restore_plan(conn, batch_id)
            if plan["conflicts"]:
                raise ConflictError(_conflict_payload(plan["conflicts"]))
            if not plan["missing"]:
                return {"status": "no_missing", "restored": [], "result_id": None}
            for record in plan["missing"]:
                conn.execute(
                    """
                    INSERT INTO scans (
                        id, batch_id, scan_id, receiving_point,
                        carton_no, serial_no, seal_intact, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["seq"],
                        batch_id,
                        record["scan_id"],
                        record["receiving_point"],
                        record["carton_no"],
                        record["serial_no"],
                        (
                            None
                            if record["seal_intact"] is None
                            else int(record["seal_intact"])
                        ),
                        record["accepted_at"],
                    ),
                )
            active = _active_version_row(conn, batch_id)
            if active is None:  # defensive: registration always creates one
                raise ConflictError(f"批次 {batch_id} 没有生效的装箱单")
            result_id = refresh_result_locked(conn, batch_id, active)
    except ServiceError:
        raise
    except Exception as exc:
        raise InternalError("修复写入失败，本次修复已回滚，请重试") from exc

    return {"status": "restored", "restored": plan["missing"], "result_id": result_id}


def correct_packing_list(
    db: Database,
    batch_id: str,
    payload: dict[str, Any],
    recalculation_delay_seconds: float = 0.0,
) -> tuple[int, int, int]:
    """Apply a serial-to-carton correction and activate a new version.

    Returns ``(previous_version, new_version, result_id)``.
    """
    timestamp = now_utc()
    with db.immediate() as conn:
        batch = _batch_row(conn, batch_id)
        if _pending_version_row(conn, batch_id) is not None:
            raise ConflictError("上一次装箱单更正仍在重算，请稍后重试")
        active = _active_version_row(conn, batch_id)
        if active is None:
            raise ConflictError(f"批次 {batch_id} 没有生效的装箱单")

        packing = load_packing_list(conn, active["id"])
        moves = payload["moves"]

        unknown = [move["serial_no"] for move in moves if move["serial_no"] not in packing.items]
        if unknown:
            raise InvalidCorrectionError(
                {
                    "message": "以下序列号不在当前生效的装箱单中",
                    "serial_numbers": unknown,
                }
            )

        cartons = [carton for carton, _ in packing.cartons]
        by_carton = {carton: list(serials) for carton, serials in packing.by_carton.items()}
        item_carton = {serial: carton for serial, (carton, _) in packing.items.items()}
        for move in moves:
            serial = move["serial_no"]
            target = move["carton_no"]
            if target not in by_carton:
                cartons.append(target)
                by_carton[target] = []
            if item_carton[serial] == target:
                continue
            by_carton[item_carton[serial]].remove(serial)
            by_carton[target].append(serial)
            item_carton[serial] = target

        if all(item_carton[serial] == carton for serial, (carton, _) in packing.items.items()):
            raise InvalidCorrectionError("更正内容与当前生效的装箱单一致")

        # Preserve the original expected-item order; moved serials keep their
        # place in the expected list while their carton and within-carton
        # position change.
        item_order = list(packing.ordered_items)

        new_version = int(batch["latest_packing_list_version"]) + 1
        conn.execute(
            "UPDATE batches SET latest_packing_list_version = ?, updated_at = ? WHERE batch_id = ?",
            (new_version, timestamp, batch_id),
        )
        cursor = conn.execute(
            """
            INSERT INTO packing_list_versions (
                batch_id, version, status, reason, created_at
            ) VALUES (?, ?, 'pending', ?, ?)
            """,
            (batch_id, new_version, payload.get("reason"), timestamp),
        )
        pending_id = int(cursor.lastrowid)
        _write_packing_list(conn, pending_id, cartons, by_carton, item_order)
        previous_version = int(active["version"])

    # Recalculation work happens outside any write transaction.  Scans continue
    # to be accepted; reads continue to use the previous active result.
    if recalculation_delay_seconds > 0:
        time.sleep(recalculation_delay_seconds)

    try:
        with db.immediate() as conn:
            pending = conn.execute(
                "SELECT * FROM packing_list_versions WHERE id = ?", (pending_id,)
            ).fetchone()
            if pending is None or pending["status"] != "pending":
                raise ServiceError("装箱单更正状态异常，已放弃本次变更")
            old_active = _active_version_row(conn, batch_id)
            packing = load_packing_list(conn, pending_id)
            scans, watermark = _scans_snapshot(conn, batch_id)
            computed = compute_verification(packing, scans)
            result_id = _persist_result(
                conn, batch_id, new_version, watermark, computed
            )
            if old_active is not None:
                conn.execute(
                    "UPDATE packing_list_versions SET status = 'superseded' WHERE id = ?",
                    (old_active["id"],),
                )
            conn.execute(
                """
                UPDATE packing_list_versions
                   SET status = 'active', activated_at = ?
                 WHERE id = ?
                """,
                (now_utc(), pending_id),
            )
            conn.execute(
                """
                UPDATE batches SET active_result_id = ?, updated_at = ?
                 WHERE batch_id = ?
                """,
                (result_id, now_utc(), batch_id),
            )
    except Exception as exc:
        # The old version and result were never modified.  Record the failed
        # attempt for audit, then surface the failure.
        try:
            with db.immediate() as conn:
                conn.execute(
                    """
                    UPDATE packing_list_versions
                       SET status = 'failed', failure_reason = ?
                     WHERE id = ? AND status = 'pending'
                    """,
                    (f"{type(exc).__name__}: {exc}", pending_id),
                )
        except sqlite3.Error:
            logger.exception(
                "could not record failed packing-list version %s", pending_id
            )

        # Keep the active result converged with scans that were accepted during
        # the failed attempt, still using the original packing list.  This is a
        # separate transaction so a failure here cannot commit partial rows.
        recovered = False
        try:
            with db.immediate() as conn:
                active = _active_version_row(conn, batch_id)
                if active is not None:
                    refresh_result_locked(conn, batch_id, active)
                    recovered = True
        except Exception:
            # The stale-but-complete snapshot is still valid to serve.
            logger.exception("post-failure reconcile failed for batch %s", batch_id)
        if isinstance(exc, ServiceError):
            raise
        detail = "装箱单变更失败，继续使用原装箱单的核对结果"
        if not recovered:
            detail += "（期间受理的扫描将在下次重算时纳入）"
        raise InternalError(detail) from exc

    return previous_version, new_version, result_id


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def get_batch(db: Database, batch_id: str) -> dict[str, Any]:
    with db.read() as conn:
        batch = _batch_row(conn, batch_id)
        active = _active_version_row(conn, batch_id)
        pending = _pending_version_row(conn, batch_id)
        return {
            "batch_id": batch["batch_id"],
            "active_packing_list_version": int(active["version"]) if active else 0,
            "latest_packing_list_version": int(batch["latest_packing_list_version"]),
            "active_result_id": batch["active_result_id"],
            "recalculation_in_progress": pending is not None,
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
        }


def get_packing_list(db: Database, batch_id: str, version: int | None) -> dict[str, Any]:
    with db.read() as conn:
        _batch_row(conn, batch_id)
        row = (
            _active_version_row(conn, batch_id)
            if version is None
            else _version_row(conn, batch_id, version)
        )
        if row is None:
            raise NotFoundError(f"批次 {batch_id} 没有生效的装箱单")
        packing = load_packing_list(conn, row["id"])
        return {
            "batch_id": batch_id,
            "version": row["version"],
            "status": row["status"],
            "reason": row["reason"],
            "failure_reason": row["failure_reason"],
            "created_at": row["created_at"],
            "activated_at": row["activated_at"],
            "cartons": [
                {"carton_no": carton, "serials": packing.by_carton.get(carton, [])}
                for carton, _ in packing.cartons
            ],
        }


def list_scans(db: Database, batch_id: str) -> list[dict[str, Any]]:
    with db.read() as conn:
        _batch_row(conn, batch_id)
        rows = conn.execute(
            "SELECT * FROM scans WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall()
        return [
            {
                "batch_id": row["batch_id"],
                "scan_id": row["scan_id"],
                "receiving_point": row["receiving_point"],
                "kind": "carton" if row["carton_no"] is not None else "serial",
                "carton_no": row["carton_no"],
                "serial_no": row["serial_no"],
                "seal_intact": None if row["seal_intact"] is None else bool(row["seal_intact"]),
                "accepted_at": row["accepted_at"],
            }
            for row in rows
        ]


def _result_id_for_batch(
    conn: sqlite3.Connection, batch: sqlite3.Row, result_id: int | None
) -> int:
    if result_id is None:
        if batch["active_result_id"] is None:
            raise NotFoundError(f"批次 {batch['batch_id']} 暂无核对结果")
        return int(batch["active_result_id"])
    row = conn.execute(
        "SELECT id FROM verification_results WHERE id = ? AND batch_id = ?",
        (result_id, batch["batch_id"]),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"批次 {batch['batch_id']} 的核对结果 {result_id} 不存在")
    return int(row["id"])


def get_result(
    db: Database, batch_id: str, result_id: int | None = None
) -> dict[str, Any]:
    with db.read() as conn:
        batch = _batch_row(conn, batch_id)
        resolved_id = _result_id_for_batch(conn, batch, result_id)
        row = conn.execute(
            "SELECT * FROM verification_results WHERE id = ?", (resolved_id,)
        ).fetchone()

        received = conn.execute(
            """
            SELECT serial_no, carton_no, receiving_point, scan_ids_json
              FROM result_received WHERE result_id = ? ORDER BY position
            """,
            (resolved_id,),
        ).fetchall()
        conflicts = conn.execute(
            """
            SELECT serial_no, carton_no, reports_json
              FROM result_conflicts WHERE result_id = ? ORDER BY position
            """,
            (resolved_id,),
        ).fetchall()
        unevidenced = conn.execute(
            """
            SELECT serial_no, carton_no FROM result_unevidenced
             WHERE result_id = ? ORDER BY position
            """,
            (resolved_id,),
        ).fetchall()
        details = conn.execute(
            """
            SELECT * FROM result_scan_details
             WHERE result_id = ? ORDER BY seq
            """,
            (resolved_id,),
        ).fetchall()

        return {
            "batch_id": batch_id,
            "result_id": resolved_id,
            "packing_list_version": row["packing_list_version"],
            "scan_high_watermark": row["scan_high_watermark"],
            "expected_count": row["expected_count"],
            "received_count": row["received_count"],
            "conflict_count": row["conflict_count"],
            "unevidenced_count": row["unevidenced_count"],
            "unmatched_scan_count": row["unmatched_scan_count"],
            "created_at": row["created_at"],
            "received": [
                {
                    "serial_no": item["serial_no"],
                    "carton_no": item["carton_no"],
                    "receiving_point": item["receiving_point"],
                    "scan_ids": json.loads(item["scan_ids_json"]),
                }
                for item in received
            ],
            "conflicts": [
                {
                    "serial_no": item["serial_no"],
                    "carton_no": item["carton_no"],
                    "reports": json.loads(item["reports_json"]),
                }
                for item in conflicts
            ],
            "unevidenced": [
                {"serial_no": item["serial_no"], "carton_no": item["carton_no"]}
                for item in unevidenced
            ],
            "details": [
                {
                    "seq": item["seq"],
                    "scan_id": item["scan_id"],
                    "receiving_point": item["receiving_point"],
                    "kind": item["kind"],
                    "carton_no": item["carton_no"],
                    "serial_no": item["serial_no"],
                    "seal_intact": (
                        None if item["seal_intact"] is None else bool(item["seal_intact"])
                    ),
                    "accepted_at": item["accepted_at"],
                    "expanded_serials": json.loads(item["expanded_json"]),
                    "unmatched": bool(item["unmatched"]),
                }
                for item in details
            ],
        }


def list_results(db: Database, batch_id: str) -> list[dict[str, Any]]:
    with db.read() as conn:
        batch = _batch_row(conn, batch_id)
        rows = conn.execute(
            """
            SELECT * FROM verification_results
             WHERE batch_id = ? ORDER BY id DESC
            """,
            (batch_id,),
        ).fetchall()
        return [
            {
                "result_id": row["id"],
                "packing_list_version": row["packing_list_version"],
                "scan_high_watermark": row["scan_high_watermark"],
                "expected_count": row["expected_count"],
                "received_count": row["received_count"],
                "conflict_count": row["conflict_count"],
                "unevidenced_count": row["unevidenced_count"],
                "unmatched_scan_count": row["unmatched_scan_count"],
                "created_at": row["created_at"],
                "active": row["id"] == batch["active_result_id"],
            }
            for row in rows
        ]


def list_versions(db: Database, batch_id: str) -> list[dict[str, Any]]:
    with db.read() as conn:
        _batch_row(conn, batch_id)
        rows = conn.execute(
            """
            SELECT * FROM packing_list_versions
             WHERE batch_id = ? ORDER BY version DESC
            """,
            (batch_id,),
        ).fetchall()
        return [
            {
                "version": row["version"],
                "status": row["status"],
                "reason": row["reason"],
                "failure_reason": row["failure_reason"],
                "created_at": row["created_at"],
                "activated_at": row["activated_at"],
            }
            for row in rows
        ]
