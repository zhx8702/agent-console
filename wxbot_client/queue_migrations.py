"""Versioned SQLite migrations for the standalone wxbot queue.

Only this module owns queue schema DDL. ``queue_store`` is a runtime data
access module and merely verifies that the dedicated migration step reached
``LATEST_REVISION``.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Callable
from pathlib import Path

LATEST_REVISION = 5


class QueueSchemaError(RuntimeError):
    pass


def migrate(db_path: str) -> int:
    """Apply every pending queue migration and return the final revision."""

    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        # Serialize migration ownership and keep the whole revision sequence
        # atomic. Runtime readers never observe a half-applied revision.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_schema_revision (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                revision INTEGER NOT NULL,
                applied_ts INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO queue_schema_revision (singleton, revision) VALUES (1, 0)"
        )
        current = _current_revision(connection)
        for revision, migration in _MIGRATIONS:
            if revision <= current:
                continue
            migration(connection)
            connection.execute(
                "UPDATE queue_schema_revision "
                "SET revision=?, applied_ts=strftime('%s','now') WHERE singleton=1",
                (revision,),
            )
            current = revision
        verify_current(connection)
        connection.commit()
        return current
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def verify_current(connection: sqlite3.Connection) -> None:
    """Fail closed when runtime opens a queue DB not migrated explicitly."""

    try:
        revision = _current_revision(connection)
    except sqlite3.OperationalError as exc:
        raise QueueSchemaError(
            "wxbot queue schema is missing; run `python -m "
            "wxbot_client.queue_migrations <queue.db>` before startup"
        ) from exc
    if revision != LATEST_REVISION:
        raise QueueSchemaError(
            f"wxbot queue schema revision {revision} is not supported; "
            f"expected {LATEST_REVISION}. Run `python -m "
            "wxbot_client.queue_migrations <queue.db>` before startup"
        )


def _current_revision(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT revision FROM queue_schema_revision WHERE singleton=1"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _create_base_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS inbound (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_svr_id TEXT UNIQUE NOT NULL,
            session_id TEXT NOT NULL,
            session_name TEXT NOT NULL,
            sender_wxid TEXT,
            sender_name TEXT,
            msg_text TEXT,
            msg_type TEXT NOT NULL DEFAULT 'text',
            image_path TEXT,
            media_status TEXT NOT NULL DEFAULT '',
            image_failure_reason TEXT NOT NULL DEFAULT '',
            media_variant TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            recv_ts INTEGER NOT NULL,
            created_ts INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS outbound (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            session_name TEXT NOT NULL,
            sender_name TEXT DEFAULT '',
            mention_sender INTEGER NOT NULL DEFAULT 0,
            reply_text TEXT,
            image_path TEXT,
            msg_type TEXT NOT NULL DEFAULT 'text',
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            sent_ts INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ingest_cursors (
            table_name TEXT PRIMARY KEY,
            cursor_val INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pending_media (
            msg_svr_id TEXT PRIMARY KEY,
            table_name TEXT NOT NULL,
            local_id INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            session_name TEXT NOT NULL,
            sender_wxid TEXT,
            sender_name TEXT,
            xml_body TEXT,
            image_path TEXT,
            media_status TEXT NOT NULL DEFAULT 'pending',
            image_failure_reason TEXT NOT NULL DEFAULT '',
            media_variant TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            recv_ts INTEGER NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            updated_ts INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _upgrade_legacy_columns(connection: sqlite3.Connection) -> None:
    columns = (
        ("outbound", "mention_sender", "INTEGER NOT NULL DEFAULT 0"),
        ("inbound", "media_status", "TEXT NOT NULL DEFAULT ''"),
        ("inbound", "image_failure_reason", "TEXT NOT NULL DEFAULT ''"),
        ("inbound", "media_variant", "TEXT NOT NULL DEFAULT ''"),
        ("inbound", "metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("pending_media", "image_path", "TEXT"),
        ("pending_media", "media_status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("pending_media", "image_failure_reason", "TEXT NOT NULL DEFAULT ''"),
        ("pending_media", "media_variant", "TEXT NOT NULL DEFAULT ''"),
        ("pending_media", "metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
    )
    for table_name, column_name, definition in columns:
        present = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in present:
            connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _add_outbound_idempotency(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outbound)").fetchall()}
    if "command_id" not in columns:
        connection.execute("ALTER TABLE outbound ADD COLUMN command_id TEXT NOT NULL DEFAULT ''")
    if "request_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE outbound ADD COLUMN request_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_outbound_command_id "
        "ON outbound(command_id) WHERE command_id <> ''"
    )


def _add_outbound_delivery_claims(connection: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(outbound)").fetchall()}
    if "claim_token" not in columns:
        connection.execute("ALTER TABLE outbound ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")
    if "claimed_ts" not in columns:
        connection.execute("ALTER TABLE outbound ADD COLUMN claimed_ts INTEGER")
    if "reconciliation_key_hash" not in columns:
        connection.execute(
            "ALTER TABLE outbound ADD COLUMN reconciliation_key_hash TEXT NOT NULL DEFAULT ''"
        )
    if "reconciliation_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE outbound ADD COLUMN reconciliation_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    if "reconciliation_response_json" not in columns:
        connection.execute(
            "ALTER TABLE outbound ADD COLUMN reconciliation_response_json TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS ix_outbound_delivery_claim ON outbound(status, claimed_ts)"
    )


def _add_local_mutation_ledger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS local_mutation_ledger (
            operation TEXT NOT NULL,
            idempotency_key_hash TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('prepared', 'completed')),
            response_json TEXT NOT NULL DEFAULT '',
            created_ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            updated_ts INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            PRIMARY KEY (operation, idempotency_key_hash)
        )
        """
    )


_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _create_base_schema),
    (2, _upgrade_legacy_columns),
    (3, _add_outbound_idempotency),
    (4, _add_outbound_delivery_claims),
    (5, _add_local_mutation_ledger),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate the wxbot SQLite queue")
    parser.add_argument("db_path", help="Path to queue.db")
    args = parser.parse_args()
    revision = migrate(args.db_path)
    print(f"wxbot queue schema is current at revision {revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
