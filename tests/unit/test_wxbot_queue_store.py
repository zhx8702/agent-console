from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from wxbot_client import queue_store
from wxbot_client.queue_migrations import LATEST_REVISION, QueueSchemaError, migrate


def test_inbound_metadata_round_trip(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    assert migrate(str(db_path)) == LATEST_REVISION
    queue_store.init(str(db_path))

    inserted = queue_store.push_inbound(
        msg_svr_id="msg-1",
        session_id="room@chatroom",
        session_name="测试群",
        sender_wxid="wxid_sender",
        sender_name="小石",
        msg_text="@机器人\u2005@张三 你怎么看",
        recv_ts=1_700_000_000,
        metadata={
            "mentioned_me": True,
            "mention_mode": "text_name_match",
            "at_wxids": ["wxid_bot"],
            "bot_mentioned": True,
            "bot_addressed": True,
            "bot_mention_position": "leading",
            "bot_mention_names": ["机器人"],
            "bot_normalized_content": "@张三 你怎么看",
            "bot_wxid": "wxid_bot",
            "capture_allowed": True,
            "capture_reason": "bot_mention",
            "quote": {
                "refer_msg_svr_id": "quoted-1",
                "sender_name": "张三",
                "text": "上一条内容",
            },
        },
    )

    assert inserted is not None
    rows = queue_store.pull_inbound()
    assert len(rows) == 1
    item = rows[0]
    assert item["mentioned_me"] is True
    assert item["mention_mode"] == "text_name_match"
    assert item["at_wxids"] == ["wxid_bot"]
    assert item["bot_mentioned"] is True
    assert item["bot_addressed"] is True
    assert item["bot_mention_position"] == "leading"
    assert item["bot_mention_names"] == ["机器人"]
    assert item["bot_normalized_content"] == "@张三 你怎么看"
    assert item["capture_reason"] == "bot_mention"
    assert item["quote"]["refer_msg_svr_id"] == "quoted-1"


def test_versioned_migration_upgrades_legacy_table_and_hydrates_defaults(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE inbound (
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
                recv_ts INTEGER NOT NULL,
                created_ts INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
            """
        )
        conn.execute(
            """
            INSERT INTO inbound (
                msg_svr_id, session_id, session_name, sender_wxid,
                sender_name, msg_text, recv_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy-1", "room@chatroom", "旧群", "wxid_a", "A", "普通消息", 1),
        )

    assert migrate(str(db_path)) == LATEST_REVISION
    queue_store.init(str(db_path))

    rows = queue_store.pull_inbound()
    assert len(rows) == 1
    assert rows[0]["mentioned_me"] is False
    assert rows[0]["at_wxids"] == []
    assert rows[0]["quote"] is None
    assert rows[0]["capture_allowed"] is True


def test_malformed_inbound_metadata_falls_back_to_safe_defaults(tmp_path) -> None:
    db_path = tmp_path / "malformed.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO inbound (
                msg_svr_id, session_id, session_name, msg_text,
                metadata_json, recv_ts
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("bad-1", "room@chatroom", "测试群", "普通消息", "{bad-json", 1),
        )

    item = queue_store.pull_inbound()[0]
    assert item["mentioned_me"] is False
    assert item["at_wxids"] == []
    assert item["quote"] is None


def test_runtime_store_rejects_unmigrated_schema(tmp_path) -> None:
    db_path = tmp_path / "unmigrated.db"

    with pytest.raises(QueueSchemaError, match="queue_migrations"):
        queue_store.init(str(db_path))


def test_migration_is_idempotent_and_records_one_current_revision(tmp_path) -> None:
    db_path = tmp_path / "queue.db"

    assert migrate(str(db_path)) == LATEST_REVISION
    assert migrate(str(db_path)) == LATEST_REVISION

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT singleton, revision FROM queue_schema_revision"
        ).fetchall()
    assert rows == [(1, LATEST_REVISION)]


def test_outbound_command_id_exactly_replays_and_rejects_payload_reuse(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))

    first = queue_store.enqueue_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="你好",
        command_id="command-1",
    )
    replay = queue_store.enqueue_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="你好",
        command_id="command-1",
    )

    assert replay.row_id == first.row_id
    assert first.replayed is False
    assert replay.replayed is True
    with pytest.raises(
        queue_store.OutboundIdempotencyConflict,
        match="outbound_idempotency_conflict",
    ):
        queue_store.enqueue_outbound(
            session_id="room@chatroom",
            session_name="测试群",
            reply_text="不同内容",
            command_id="command-1",
        )
    rows = queue_store.list_outbound()
    assert len(rows) == 1
    assert rows[0]["command_id"] == "command-1"


def test_outbound_command_id_is_atomic_under_concurrent_retries(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))

    def enqueue() -> int:
        queue_store.init(str(db_path))
        return queue_store.push_outbound(
            session_id="room@chatroom",
            session_name="测试群",
            reply_text="并发重试",
            command_id="command-concurrent",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda _index: enqueue(), range(16)))

    assert len(set(ids)) == 1
    assert len(queue_store.list_outbound()) == 1


def test_outbound_migration_adds_partial_unique_command_index(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))

    with sqlite3.connect(db_path) as connection:
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(outbound)").fetchall()
        }
        indexes = {
            str(row[1]) for row in connection.execute("PRAGMA index_list(outbound)").fetchall()
        }

    assert {
        "command_id",
        "request_fingerprint",
        "claim_token",
        "claimed_ts",
        "reconciliation_key_hash",
        "reconciliation_fingerprint",
        "reconciliation_response_json",
    }.issubset(columns)
    assert "ux_outbound_command_id" in indexes
    with sqlite3.connect(db_path) as connection:
        ledger_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(local_mutation_ledger)").fetchall()
        }
    assert {
        "operation",
        "idempotency_key_hash",
        "request_fingerprint",
        "status",
        "response_json",
    }.issubset(ledger_columns)


def test_outbound_batch_rolls_back_all_new_rows_on_late_conflict(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))
    queue_store.enqueue_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="original",
        command_id="existing-command",
    )

    with pytest.raises(queue_store.OutboundIdempotencyConflict):
        queue_store.enqueue_outbound_batch(
            [
                {
                    "session_id": "room@chatroom",
                    "session_name": "测试群",
                    "reply_text": "must roll back",
                    "command_id": "new-command",
                },
                {
                    "session_id": "room@chatroom",
                    "session_name": "测试群",
                    "reply_text": "different",
                    "command_id": "existing-command",
                },
            ]
        )

    rows = queue_store.list_outbound()
    assert [row["command_id"] for row in rows] == ["existing-command"]


def test_clear_outbound_replays_exact_response_and_rejects_key_reuse(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))
    row_id = queue_store.push_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="待清理",
        command_id="clear-target",
    )

    first = queue_store.clear_outbound_idempotent(
        "pending",
        "room@chatroom",
        idempotency_key="clear-command-1",
    )
    replay = queue_store.clear_outbound_idempotent(
        "pending",
        "room@chatroom",
        idempotency_key="clear-command-1",
    )

    assert first.response == replay.response
    assert first.response["ids"] == [row_id]
    assert first.replayed is False
    assert replay.replayed is True
    with pytest.raises(queue_store.LocalMutationIdempotencyConflict):
        queue_store.clear_outbound_idempotent(
            "failed",
            "room@chatroom",
            idempotency_key="clear-command-1",
        )


def test_local_mutation_prepare_complete_and_replay(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))
    payload = {"group_require_at_me": False}

    assert (
        queue_store.begin_local_mutation(
            "trigger-config.update",
            "trigger-command-1",
            payload,
        )
        is None
    )
    completed = queue_store.complete_local_mutation(
        "trigger-config.update",
        "trigger-command-1",
        payload,
        {"saved": True},
    )
    replay = queue_store.begin_local_mutation(
        "trigger-config.update",
        "trigger-command-1",
        payload,
    )

    assert completed.response == {"saved": True}
    assert completed.replayed is False
    assert replay is not None
    assert replay.response == completed.response
    assert replay.replayed is True
    with pytest.raises(queue_store.LocalMutationIdempotencyConflict):
        queue_store.begin_local_mutation(
            "trigger-config.update",
            "trigger-command-1",
            {"group_require_at_me": True},
        )


def test_outbound_claim_is_fenced_and_completed_once(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))
    row_id = queue_store.push_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="只发送一次",
        command_id="command-fenced",
    )

    claimed = queue_store.claim_outbound_pending()

    assert [item["id"] for item in claimed] == [row_id]
    assert queue_store.claim_outbound_pending() == []
    assert queue_store.mark_outbound_sent(row_id, claim_token="wrong") is False
    assert (
        queue_store.mark_outbound_sent(
            row_id,
            claim_token=claimed[0]["claim_token"],
        )
        is True
    )
    assert queue_store.list_outbound()[0]["status"] == "sent"


def test_get_outbound_resolves_exact_row_scope(tmp_path) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))
    row_id = queue_store.push_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="待核对",
        command_id="command-exact-row",
    )

    row = queue_store.get_outbound(row_id)

    assert row is not None
    assert row["id"] == row_id
    assert row["session_id"] == "room@chatroom"
    assert queue_store.get_outbound(row_id + 1) is None


def test_interrupted_delivery_is_quarantined_and_reconciled_idempotently(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "queue.db"
    migrate(str(db_path))
    queue_store.init(str(db_path))
    monkeypatch.setattr(queue_store.time, "time", lambda: 1_000)
    row_id = queue_store.push_outbound(
        session_id="room@chatroom",
        session_name="测试群",
        reply_text="结果待确认",
        command_id="command-uncertain",
    )
    assert queue_store.claim_outbound_pending()

    monkeypatch.setattr(queue_store.time, "time", lambda: 1_121)
    assert queue_store.recover_stale_outbound(stale_seconds=120) == 1
    assert queue_store.claim_outbound_pending() == []
    assert queue_store.list_outbound()[0]["status"] == "uncertain"

    first = queue_store.reconcile_outbound(
        row_id,
        "retry",
        idempotency_key="reconcile-command-uncertain",
    )
    replay = queue_store.reconcile_outbound(
        row_id,
        "retry",
        idempotency_key="reconcile-command-uncertain",
    )

    assert first["status"] == "pending"
    assert first["idempotent_replayed"] is False
    assert replay == {**first, "idempotent_replayed": True}
    with pytest.raises(queue_store.OutboundIdempotencyConflict):
        queue_store.reconcile_outbound(
            row_id,
            "confirm_sent",
            idempotency_key="reconcile-command-uncertain",
        )
    assert [item["id"] for item in queue_store.fetch_outbound_pending()] == [row_id]
