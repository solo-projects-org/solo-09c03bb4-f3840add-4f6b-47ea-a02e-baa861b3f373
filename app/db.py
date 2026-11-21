"""SQLite storage helpers.

The service keeps its SQL in one small module so the verification rules remain
readable and the persistence layer can be audited independently.

Concurrency model
-----------------
* WAL mode lets readers proceed while one writer commits.
* Writers use ``BEGIN IMMEDIATE`` and are therefore serialised by SQLite.
* Long-running recalculation work never holds a write transaction open: the
  pending packing-list version is committed first, the computation runs
  outside any transaction, and the result is activated in one short final
  transaction.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id                    TEXT PRIMARY KEY,
    latest_packing_list_version INTEGER NOT NULL DEFAULT 0,
    active_result_id            INTEGER,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packing_list_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL REFERENCES batches(batch_id),
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL
                 CHECK (status IN ('pending', 'active', 'superseded', 'failed')),
    reason       TEXT,
    failure_reason TEXT,
    created_at   TEXT NOT NULL,
    activated_at TEXT,
    UNIQUE (batch_id, version)
);
-- At most one active and one pending version per batch.  Failed and
-- superseded versions remain for audit.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pl_one_active
    ON packing_list_versions(batch_id) WHERE status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS ux_pl_one_pending
    ON packing_list_versions(batch_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS packing_list_cartons (
    version_id  INTEGER NOT NULL REFERENCES packing_list_versions(id),
    carton_no   TEXT NOT NULL,
    position    INTEGER NOT NULL,
    PRIMARY KEY (version_id, carton_no)
);

CREATE TABLE IF NOT EXISTS packing_list_items (
    version_id  INTEGER NOT NULL REFERENCES packing_list_versions(id),
    serial_no   TEXT NOT NULL,
    carton_no   TEXT NOT NULL,
    position    INTEGER NOT NULL,
    PRIMARY KEY (version_id, serial_no),
    FOREIGN KEY (version_id, carton_no)
        REFERENCES packing_list_cartons(version_id, carton_no)
);
CREATE INDEX IF NOT EXISTS idx_packing_items_carton
    ON packing_list_items(version_id, carton_no);

CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
    scan_id         TEXT NOT NULL,
    receiving_point TEXT NOT NULL,
    carton_no       TEXT,
    serial_no       TEXT,
    seal_intact     INTEGER,
    accepted_at     TEXT NOT NULL,
    UNIQUE (batch_id, scan_id),
    CHECK (
        (carton_no IS NOT NULL AND serial_no IS NULL)
        OR (carton_no IS NULL AND serial_no IS NOT NULL)
    ),
    -- A whole-carton scan is only admissible when its seal is intact.
    -- ``IS 1`` (not ``= 1``) rejects NULL, which a bare CHECK would let pass.
    CHECK (carton_no IS NULL OR seal_intact IS 1),
    -- A single-item scan must not carry a seal value.
    CHECK (serial_no IS NULL OR seal_intact IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_scans_batch_seq ON scans(batch_id, id);

CREATE TABLE IF NOT EXISTS verification_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             TEXT NOT NULL REFERENCES batches(batch_id),
    packing_list_version INTEGER NOT NULL,
    scan_high_watermark  INTEGER NOT NULL,
    expected_count       INTEGER NOT NULL,
    received_count       INTEGER NOT NULL,
    conflict_count       INTEGER NOT NULL,
    unevidenced_count    INTEGER NOT NULL,
    unmatched_scan_count INTEGER NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_batch
    ON verification_results(batch_id, id);

CREATE TABLE IF NOT EXISTS result_received (
    result_id       INTEGER NOT NULL REFERENCES verification_results(id),
    serial_no       TEXT NOT NULL,
    carton_no       TEXT NOT NULL,
    receiving_point TEXT NOT NULL,
    scan_ids_json   TEXT NOT NULL,
    position        INTEGER NOT NULL,
    PRIMARY KEY (result_id, serial_no)
);

CREATE TABLE IF NOT EXISTS result_conflicts (
    result_id   INTEGER NOT NULL REFERENCES verification_results(id),
    serial_no   TEXT NOT NULL,
    carton_no   TEXT NOT NULL,
    reports_json TEXT NOT NULL,
    position    INTEGER NOT NULL,
    PRIMARY KEY (result_id, serial_no)
);

CREATE TABLE IF NOT EXISTS result_unevidenced (
    result_id  INTEGER NOT NULL REFERENCES verification_results(id),
    serial_no  TEXT NOT NULL,
    carton_no  TEXT NOT NULL,
    position   INTEGER NOT NULL,
    PRIMARY KEY (result_id, serial_no)
);

CREATE TABLE IF NOT EXISTS result_scan_details (
    result_id      INTEGER NOT NULL REFERENCES verification_results(id),
    seq            INTEGER NOT NULL,
    scan_id        TEXT NOT NULL,
    receiving_point TEXT NOT NULL,
    kind           TEXT NOT NULL,
    carton_no      TEXT,
    serial_no      TEXT,
    seal_intact    INTEGER,
    accepted_at    TEXT NOT NULL,
    expanded_json  TEXT NOT NULL,
    unmatched      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (result_id, seq)
);
"""


class Database:
    """Tiny connection factory with explicit transaction helpers."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,  # explicit BEGIN/COMMIT below
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def init(self) -> None:
        """Create the schema.

        Interrupted recalculation recovery lives in ``service.recover_on_startup``
        because it also needs the verification calculation.
        """
        conn = self.connect()
        try:
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A read transaction with a stable snapshot."""
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        """A writer transaction; SQLite serialises these across processes."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


def dbfile_from_env(default: str = "data/verification.db") -> str:
    return os.getenv("DB_PATH", default)
