"""Local SQLite message queue for the SDK.

Two logical queues:
- inbound: messages scanned from WeChat DB, ready for external consumers
- outbound: send requests from external consumers, waiting for UI dispatch
"""

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

_db_path: str = ""
_local = threading.local()


class OutboundIdempotencyConflict(RuntimeError):
    pass


class LocalMutationIdempotencyConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OutboundEnqueueResult:
    row_id: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class LocalMutationResult:
    response: dict
    replayed: bool


def init(db_path: str):
    global _db_path
    existing = getattr(_local, "conn", None)
    if existing is not None and _db_path and _db_path != db_path:
        existing.close()
        _local.conn = None
    _db_path = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as c:
        try:
            from .queue_migrations import verify_current
        except ImportError:  # pragma: no cover - direct Windows SDK launch
            from queue_migrations import verify_current

        verify_current(c)


def _conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(_db_path, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def _json_object(value):
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, dict) else {}


def _json_list(value):
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def _local_mutation_identity(operation: str, idempotency_key: str, payload: dict):
    normalized_operation = str(operation or "").strip()
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_operation or len(normalized_operation) > 128:
        raise ValueError("valid mutation operation required")
    if len(normalized_key) < 8 or len(normalized_key) > 128:
        raise ValueError("valid idempotency key required")
    key_hash = hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return normalized_operation, key_hash, fingerprint


def _load_local_mutation(connection, operation, key_hash, fingerprint):
    connection.execute(
        "INSERT OR IGNORE INTO local_mutation_ledger "
        "(operation, idempotency_key_hash, request_fingerprint, status) "
        "VALUES (?, ?, ?, 'prepared')",
        (operation, key_hash, fingerprint),
    )
    row = connection.execute(
        "SELECT request_fingerprint, status, response_json "
        "FROM local_mutation_ledger "
        "WHERE operation=? AND idempotency_key_hash=?",
        (operation, key_hash),
    ).fetchone()
    if row is None:
        raise RuntimeError("local_mutation_record_missing")
    if str(row["request_fingerprint"] or "") != fingerprint:
        raise LocalMutationIdempotencyConflict("local_mutation_idempotency_conflict")
    return row


def _completed_local_mutation(row) -> LocalMutationResult | None:
    if str(row["status"] or "") != "completed":
        return None
    response = json.loads(str(row["response_json"] or "{}"))
    if not isinstance(response, dict):
        raise RuntimeError("local_mutation_response_corrupt")
    return LocalMutationResult(response=response, replayed=True)


def begin_local_mutation(
    operation: str,
    idempotency_key: str,
    payload: dict,
) -> LocalMutationResult | None:
    """Prepare a recoverable local side effect or replay its exact response."""

    identity = _local_mutation_identity(operation, idempotency_key, payload)
    with _conn() as connection:
        row = _load_local_mutation(connection, *identity)
        return _completed_local_mutation(row)


def complete_local_mutation(
    operation: str,
    idempotency_key: str,
    payload: dict,
    response: dict,
) -> LocalMutationResult:
    identity = _local_mutation_identity(operation, idempotency_key, payload)
    response_json = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    connection = _conn()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_local_mutation(connection, *identity)
        completed = _completed_local_mutation(row)
        if completed is not None:
            connection.commit()
            return completed
        result = connection.execute(
            "UPDATE local_mutation_ledger SET status='completed', response_json=?, "
            "updated_ts=strftime('%s','now') "
            "WHERE operation=? AND idempotency_key_hash=? "
            "AND request_fingerprint=? AND status='prepared'",
            (response_json, identity[0], identity[1], identity[2]),
        )
        if not result.rowcount:
            raise RuntimeError("local_mutation_concurrent_update")
        connection.commit()
        return LocalMutationResult(response=dict(response), replayed=False)
    except Exception:
        connection.rollback()
        raise


def _inbound_metadata(
    metadata=None,
    *,
    mentioned_me=False,
    mention_mode="",
    is_self_sent=False,
    at_wxids=None,
    quote=None,
):
    payload = _json_object(metadata)
    payload.setdefault("mentioned_me", bool(mentioned_me))
    payload.setdefault("mention_mode", str(mention_mode or ""))
    payload.setdefault("is_self_sent", bool(is_self_sent))
    payload.setdefault("at_wxids", _json_list(at_wxids))
    if quote is not None:
        payload.setdefault("quote", quote)
    return payload


def _hydrate_inbound_row(row):
    item = dict(row)
    metadata = _json_object(item.pop("metadata_json", ""))
    item.update(metadata)
    item["mentioned_me"] = bool(item.get("mentioned_me"))
    item["mention_mode"] = str(item.get("mention_mode") or "")
    item["is_self_sent"] = bool(item.get("is_self_sent"))
    item["at_wxids"] = _json_list(item.get("at_wxids"))
    item["quote"] = item.get("quote") if isinstance(item.get("quote"), dict) else None
    item["bot_mentioned"] = bool(item.get("bot_mentioned") or item["mentioned_me"])
    bot_addressed = item.get("bot_addressed")
    item["bot_addressed"] = bool(item["mentioned_me"] if bot_addressed is None else bot_addressed)
    item["bot_mention_position"] = str(item.get("bot_mention_position") or "")
    item["bot_mention_names"] = _json_list(item.get("bot_mention_names"))
    item["bot_normalized_content"] = str(item.get("bot_normalized_content") or "")
    item["capture_allowed"] = bool(item.get("capture_allowed", True))
    item["capture_reason"] = str(item.get("capture_reason") or "")
    return item


# ── Ingest cursors ──


def get_ingest_cursor(table_name: str):
    with _conn() as c:
        row = c.execute(
            "SELECT cursor_val FROM ingest_cursors WHERE table_name=?",
            (table_name,),
        ).fetchone()
    return row["cursor_val"] if row else None


def set_ingest_cursor(table_name: str, val: int):
    with _conn() as c:
        c.execute(
            "INSERT INTO ingest_cursors (table_name, cursor_val) VALUES (?, ?) "
            "ON CONFLICT(table_name) DO UPDATE SET cursor_val=?",
            (table_name, val, val),
        )


# ── Inbound (messages from WeChat) ──


def push_inbound(
    *,
    msg_svr_id,
    session_id,
    session_name,
    sender_wxid,
    sender_name,
    msg_text,
    recv_ts,
    msg_type="text",
    image_path=None,
    media_status="",
    image_failure_reason="",
    media_variant="",
    metadata=None,
    mentioned_me=False,
    mention_mode="",
    is_self_sent=False,
    at_wxids=None,
    quote=None,
):
    metadata_payload = _inbound_metadata(
        metadata,
        mentioned_me=mentioned_me,
        mention_mode=mention_mode,
        is_self_sent=is_self_sent,
        at_wxids=at_wxids,
        quote=quote,
    )
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO inbound "
                "(msg_svr_id, session_id, session_name, sender_wxid, sender_name, "
                " msg_text, msg_type, image_path, media_status, image_failure_reason, "
                " media_variant, metadata_json, recv_ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(msg_svr_id),
                    session_id,
                    session_name,
                    sender_wxid,
                    sender_name,
                    msg_text,
                    msg_type,
                    image_path,
                    media_status,
                    image_failure_reason,
                    media_variant,
                    json.dumps(metadata_payload, ensure_ascii=False),
                    recv_ts,
                ),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def pull_inbound(cursor: int = 0, limit: int = 100):
    with _conn() as c:
        rows = c.execute(
            "SELECT id, msg_svr_id, session_id, session_name, sender_wxid, "
            "sender_name, msg_text, msg_type, image_path, media_status, "
            "image_failure_reason, media_variant, metadata_json, recv_ts, created_ts "
            "FROM inbound WHERE id > ? ORDER BY id LIMIT ?",
            (cursor, limit),
        ).fetchall()
    return [_hydrate_inbound_row(r) for r in rows]


def list_inbound_unready_images(limit: int = 100):
    with _conn() as c:
        rows = c.execute(
            "SELECT id, msg_svr_id, session_id, session_name, sender_wxid, "
            "sender_name, msg_text, msg_type, image_path, media_status, "
            "image_failure_reason, media_variant, metadata_json, recv_ts, created_ts "
            "FROM inbound "
            "WHERE msg_type='image' AND COALESCE(media_status, '') IN ('', 'pending', 'thumbnail') "
            "ORDER BY created_ts, id LIMIT ?",
            (max(1, min(int(limit or 100), 500)),),
        ).fetchall()
    return [_hydrate_inbound_row(r) for r in rows]


def update_inbound_image_path(
    msg_svr_id: str,
    image_path: str,
    *,
    media_status: str = "ready",
    image_failure_reason: str = "",
    media_variant: str = "original",
):
    with _conn() as c:
        cur = c.execute(
            "UPDATE inbound SET image_path=?, media_status=?, image_failure_reason=?, media_variant=? "
            "WHERE msg_svr_id=? AND msg_type='image'",
            (image_path, media_status, image_failure_reason, media_variant, str(msg_svr_id)),
        )
        return cur.rowcount


def save_pending_media(
    *,
    msg_svr_id,
    table_name,
    local_id,
    session_id,
    session_name,
    sender_wxid,
    sender_name,
    xml_body,
    recv_ts,
    image_path="",
    media_status="pending",
    image_failure_reason="",
    media_variant="",
    metadata=None,
):
    metadata_payload = _inbound_metadata(metadata)
    with _conn() as c:
        c.execute(
            "INSERT INTO pending_media "
            "(msg_svr_id, table_name, local_id, session_id, session_name, sender_wxid, "
            " sender_name, xml_body, image_path, media_status, image_failure_reason, "
            " media_variant, metadata_json, recv_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(msg_svr_id) DO UPDATE SET "
            "table_name=excluded.table_name, local_id=excluded.local_id, "
            "session_id=excluded.session_id, session_name=excluded.session_name, "
            "sender_wxid=excluded.sender_wxid, sender_name=excluded.sender_name, "
            "xml_body=excluded.xml_body, image_path=excluded.image_path, "
            "media_status=excluded.media_status, image_failure_reason=excluded.image_failure_reason, "
            "media_variant=excluded.media_variant, metadata_json=excluded.metadata_json, "
            "recv_ts=excluded.recv_ts, "
            "retry_count=pending_media.retry_count + 1, "
            "updated_ts=strftime('%s','now')",
            (
                str(msg_svr_id),
                table_name,
                int(local_id),
                session_id,
                session_name,
                sender_wxid,
                sender_name,
                xml_body,
                image_path,
                media_status,
                image_failure_reason,
                media_variant,
                json.dumps(metadata_payload, ensure_ascii=False),
                int(recv_ts),
            ),
        )


def pull_pending_media(limit: int = 100):
    with _conn() as c:
        rows = c.execute(
            "SELECT msg_svr_id, table_name, local_id, session_id, session_name, "
            "sender_wxid, sender_name, xml_body, image_path, media_status, "
            "image_failure_reason, media_variant, metadata_json, recv_ts, retry_count, "
            "created_ts, updated_ts "
            "FROM pending_media ORDER BY created_ts, msg_svr_id LIMIT ?",
            (max(1, min(int(limit or 100), 500)),),
        ).fetchall()
    return [_hydrate_inbound_row(r) for r in rows]


def delete_pending_media(msg_svr_id: str):
    with _conn() as c:
        c.execute("DELETE FROM pending_media WHERE msg_svr_id=?", (str(msg_svr_id),))


def inbound_image_stats():
    with _conn() as c:
        rows = c.execute(
            "SELECT COALESCE(NULLIF(media_status, ''), 'unknown') AS status, COUNT(*) AS n "
            "FROM inbound WHERE msg_type='image' GROUP BY COALESCE(NULLIF(media_status, ''), 'unknown')"
        ).fetchall()
        pending_rows = c.execute(
            "SELECT COALESCE(NULLIF(media_status, ''), 'pending') AS status, COUNT(*) AS n "
            "FROM pending_media GROUP BY COALESCE(NULLIF(media_status, ''), 'pending')"
        ).fetchall()
        max_row = c.execute("SELECT COALESCE(MAX(id), 0) AS max_inbound_id FROM inbound").fetchone()
    stats = {r["status"]: r["n"] for r in rows}
    stats["pending_records"] = sum(int(r["n"]) for r in pending_rows)
    for r in pending_rows:
        stats[f"pending_{r['status']}"] = int(r["n"])
    stats["max_inbound_id"] = int(max_row["max_inbound_id"] if max_row else 0)
    return stats


# ── Outbound (send requests from consumers) ──


def push_outbound(
    *,
    session_id,
    session_name,
    sender_name="",
    reply_text=None,
    image_path=None,
    msg_type="text",
    mention_sender=False,
    command_id="",
):
    return enqueue_outbound(
        session_id=session_id,
        session_name=session_name,
        sender_name=sender_name,
        reply_text=reply_text,
        image_path=image_path,
        msg_type=msg_type,
        mention_sender=mention_sender,
        command_id=command_id,
    ).row_id


def _normalize_outbound_request(
    *,
    session_id,
    session_name,
    sender_name="",
    reply_text=None,
    image_path=None,
    msg_type="text",
    mention_sender=False,
    command_id="",
) -> tuple[dict, str, str]:
    normalized = {
        "session_id": str(session_id or "").strip(),
        "session_name": str(session_name or ""),
        "sender_name": str(sender_name or ""),
        "mention_sender": bool(mention_sender),
        "reply_text": None if reply_text is None else str(reply_text),
        "image_path": None if image_path is None else str(image_path),
        "msg_type": str(msg_type or "text").strip().lower() or "text",
    }
    normalized_command_id = str(command_id or "").strip()
    if len(normalized_command_id) > 128:
        raise ValueError("command_id_too_long")
    request_fingerprint = hashlib.sha256(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return normalized, normalized_command_id, request_fingerprint


def _enqueue_outbound_with_connection(connection, request: dict) -> OutboundEnqueueResult:
    normalized, normalized_command_id, request_fingerprint = _normalize_outbound_request(**request)
    cur = connection.execute(
        "INSERT OR IGNORE INTO outbound "
        "(session_id, session_name, sender_name, mention_sender, reply_text, "
        "image_path, msg_type, command_id, request_fingerprint) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            normalized["session_id"],
            normalized["session_name"],
            normalized["sender_name"],
            1 if normalized["mention_sender"] else 0,
            normalized["reply_text"],
            normalized["image_path"],
            normalized["msg_type"],
            normalized_command_id,
            request_fingerprint if normalized_command_id else "",
        ),
    )
    if cur.rowcount:
        return OutboundEnqueueResult(row_id=int(cur.lastrowid), replayed=False)
    if not normalized_command_id:
        raise RuntimeError("outbound_enqueue_failed")
    existing = connection.execute(
        "SELECT id, request_fingerprint FROM outbound WHERE command_id=?",
        (normalized_command_id,),
    ).fetchone()
    if existing is None:
        raise RuntimeError("outbound_idempotency_record_missing")
    if str(existing["request_fingerprint"] or "") != request_fingerprint:
        raise OutboundIdempotencyConflict("outbound_idempotency_conflict")
    return OutboundEnqueueResult(row_id=int(existing["id"]), replayed=True)


def enqueue_outbound(
    *,
    session_id,
    session_name,
    sender_name="",
    reply_text=None,
    image_path=None,
    msg_type="text",
    mention_sender=False,
    command_id="",
) -> OutboundEnqueueResult:
    request = {
        "session_id": session_id,
        "session_name": session_name,
        "sender_name": sender_name,
        "reply_text": reply_text,
        "image_path": image_path,
        "msg_type": msg_type,
        "mention_sender": mention_sender,
        "command_id": command_id,
    }
    with _conn() as connection:
        return _enqueue_outbound_with_connection(connection, request)


def enqueue_outbound_batch(requests: list[dict]) -> list[OutboundEnqueueResult]:
    """Validate and enqueue a batch in one SQLite transaction."""

    normalized_requests: list[dict] = []
    for item in requests:
        normalized, command_id, _fingerprint = _normalize_outbound_request(**item)
        normalized_requests.append({**normalized, "command_id": command_id})

    connection = _conn()
    try:
        connection.execute("BEGIN IMMEDIATE")
        results = [
            _enqueue_outbound_with_connection(connection, item) for item in normalized_requests
        ]
        connection.commit()
        return results
    except Exception:
        connection.rollback()
        raise


def fetch_outbound_pending(limit: int = 50):
    with _conn() as c:
        rows = c.execute(
            "SELECT id, session_id, session_name, sender_name, mention_sender, reply_text, "
            "image_path, msg_type, command_id, attempt_count "
            "FROM outbound WHERE status='pending' "
            "ORDER BY session_id, created_ts LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def claim_outbound_pending(limit: int = 50):
    """Move pending rows to running before touching the WeChat UI."""

    claim_token = secrets.token_hex(16)
    connection = _conn()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT id FROM outbound WHERE status='pending' "
            "ORDER BY session_id, created_ts LIMIT ?",
            (max(1, min(int(limit or 50), 500)),),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            connection.commit()
            return []
        placeholders = ",".join("?" for _ in ids)
        connection.execute(
            "UPDATE outbound SET status='running', claim_token=?, claimed_ts=? "
            f"WHERE status='pending' AND id IN ({placeholders})",
            (claim_token, int(time.time()), *ids),
        )
        claimed = connection.execute(
            "SELECT id, session_id, session_name, sender_name, mention_sender, reply_text, "
            "image_path, msg_type, command_id, attempt_count, claim_token "
            "FROM outbound WHERE claim_token=? AND status='running' ORDER BY session_id, id",
            (claim_token,),
        ).fetchall()
        connection.commit()
        return [dict(row) for row in claimed]
    except Exception:
        connection.rollback()
        raise


def recover_stale_outbound(stale_seconds: int = 120) -> int:
    """Quarantine abandoned running rows instead of blindly sending twice."""

    cutoff = int(time.time()) - max(1, int(stale_seconds or 120))
    with _conn() as connection:
        result = connection.execute(
            "UPDATE outbound SET status='uncertain', "
            "error='delivery outcome unknown after interrupted send', claim_token='' "
            "WHERE status='running' AND COALESCE(claimed_ts, 0) <= ?",
            (cutoff,),
        )
        return max(0, int(result.rowcount or 0))


def mark_outbound_sent(row_id: int, claim_token: str = ""):
    with _conn() as c:
        params = [row_id]
        claim_clause = ""
        if str(claim_token or "").strip():
            claim_clause = " AND claim_token=?"
            params.append(str(claim_token).strip())
        result = c.execute(
            "UPDATE outbound SET status='sent', sent_ts=strftime('%s','now'), "
            "claim_token='' WHERE id=? AND status='running'" + claim_clause,
            tuple(params),
        )
        return bool(result.rowcount)


def mark_outbound_uncertain(row_id: int, error: str, claim_token: str = "") -> bool:
    with _conn() as connection:
        params = [str(error or "delivery outcome unknown")[:500], row_id]
        claim_clause = ""
        if str(claim_token or "").strip():
            claim_clause = " AND claim_token=?"
            params.append(str(claim_token).strip())
        result = connection.execute(
            "UPDATE outbound SET status='uncertain', error=?, attempt_count=attempt_count+1, "
            "claim_token='' WHERE id=? AND status='running'" + claim_clause,
            tuple(params),
        )
        return bool(result.rowcount)


def reconcile_outbound(row_id: int, action: str, *, idempotency_key: str) -> dict:
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"confirm_sent", "retry"}:
        raise ValueError("reconcile action must be confirm_sent or retry")
    normalized_key = str(idempotency_key or "").strip()
    if not normalized_key or len(normalized_key) > 128:
        raise ValueError("valid idempotency key required")
    key_hash = hashlib.sha256(normalized_key.encode()).hexdigest()
    request_fingerprint = hashlib.sha256(f"{int(row_id)}\0{normalized_action}".encode()).hexdigest()
    with _conn() as connection:
        row = connection.execute(
            "SELECT id, status, command_id, reconciliation_key_hash, "
            "reconciliation_fingerprint, reconciliation_response_json "
            "FROM outbound WHERE id=?",
            (int(row_id),),
        ).fetchone()
        if row is None:
            raise KeyError(row_id)
        existing_key_hash = str(row["reconciliation_key_hash"] or "")
        if existing_key_hash:
            if (
                existing_key_hash != key_hash
                or str(row["reconciliation_fingerprint"] or "") != request_fingerprint
            ):
                raise OutboundIdempotencyConflict("outbound_reconciliation_idempotency_conflict")
            response = json.loads(str(row["reconciliation_response_json"] or "{}"))
            if not isinstance(response, dict):
                raise RuntimeError("outbound_reconciliation_record_corrupt")
            return {**response, "idempotent_replayed": True}
        if str(row["status"]) != "uncertain":
            raise ValueError("only uncertain outbound rows can be reconciled")
        next_status = "sent" if normalized_action == "confirm_sent" else "pending"
        sent_expression = "strftime('%s','now')" if next_status == "sent" else "NULL"
        response = {
            "id": int(row["id"]),
            "command_id": str(row["command_id"] or ""),
            "status": next_status,
            "action": normalized_action,
            "idempotent_replayed": False,
        }
        result = connection.execute(
            f"UPDATE outbound SET status=?, error='', claim_token='', claimed_ts=NULL, "
            f"sent_ts={sent_expression}, reconciliation_key_hash=?, "
            "reconciliation_fingerprint=?, reconciliation_response_json=? "
            "WHERE id=? AND status='uncertain' AND reconciliation_key_hash=''",
            (
                next_status,
                key_hash,
                request_fingerprint,
                json.dumps(response, ensure_ascii=False, sort_keys=True),
                int(row_id),
            ),
        )
        if not result.rowcount:
            raise RuntimeError("outbound_reconciliation_concurrent_update")
        return response


def mark_outbound_failed(row_id: int, error: str, max_attempts: int = 3):
    with _conn() as c:
        row = c.execute("SELECT attempt_count FROM outbound WHERE id=?", (row_id,)).fetchone()
        if not row:
            return "not_found"
        attempts = row["attempt_count"] + 1
        if attempts >= max_attempts:
            c.execute(
                "UPDATE outbound SET status='failed', error=?, attempt_count=? WHERE id=?",
                (error[:500], attempts, row_id),
            )
            return "failed"
        c.execute(
            "UPDATE outbound SET error=?, attempt_count=? WHERE id=?",
            (error[:500], attempts, row_id),
        )
        return "retry"


def outbound_stats():
    with _conn() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM outbound GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def list_outbound(status: str = "", limit: int = 100):
    params = []
    where = ""
    if status.strip():
        where = "WHERE status=?"
        params.append(status.strip())
    params.append(max(1, min(int(limit or 100), 500)))
    with _conn() as c:
        rows = c.execute(
            "SELECT id, session_id, session_name, sender_name, mention_sender, reply_text, "
            "image_path, msg_type, command_id, status, error, attempt_count, "
            "created_ts, claimed_ts, sent_ts "
            f"FROM outbound {where} ORDER BY created_ts DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def get_outbound(row_id: int):
    """Return one exact outbound row for scope-safe reconciliation."""

    with _conn() as connection:
        row = connection.execute(
            "SELECT id, session_id, session_name, sender_name, mention_sender, "
            "reply_text, image_path, msg_type, command_id, status, error, "
            "attempt_count, created_ts, claimed_ts, sent_ts "
            "FROM outbound WHERE id=?",
            (int(row_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def _clear_outbound_in_connection(connection, status: str, session_id: str) -> dict:
    clauses = []
    params = []
    target_status = (status or "pending").strip()
    target_session = (session_id or "").strip()
    if target_status and target_status != "all":
        clauses.append("status=?")
        params.append(target_status)
    else:
        # A running row may already be interacting with the WeChat UI. Clearing
        # it concurrently would erase the only evidence needed for recovery.
        clauses.append("status<>'running'")
    if target_session:
        clauses.append("session_id=?")
        params.append(target_session)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(
        f"SELECT id FROM outbound {where} ORDER BY id",
        tuple(params),
    ).fetchall()
    ids = [int(r["id"]) for r in rows]
    if ids:
        connection.execute(
            f"UPDATE outbound SET status='cleared', error=? {where}",
            ("manually cleared", *params),
        )
    return {
        "status": target_status or "pending",
        "session_id": target_session,
        "cleared": len(ids),
        "ids": ids,
    }


def clear_outbound(status: str = "pending", session_id: str = ""):
    with _conn() as connection:
        return _clear_outbound_in_connection(connection, status, session_id)


def clear_outbound_idempotent(
    status: str,
    session_id: str,
    *,
    idempotency_key: str,
) -> LocalMutationResult:
    payload = {
        "status": str(status or "pending").strip() or "pending",
        "session_id": str(session_id or "").strip(),
    }
    identity = _local_mutation_identity(
        "outbound.clear",
        idempotency_key,
        payload,
    )
    connection = _conn()
    try:
        # The queue mutation and replay record commit together, eliminating the
        # usual crash window between an external write and its idempotency row.
        connection.execute("BEGIN IMMEDIATE")
        row = _load_local_mutation(connection, *identity)
        completed = _completed_local_mutation(row)
        if completed is not None:
            connection.commit()
            return completed
        response = _clear_outbound_in_connection(
            connection,
            payload["status"],
            payload["session_id"],
        )
        response_json = json.dumps(
            response,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        result = connection.execute(
            "UPDATE local_mutation_ledger SET status='completed', response_json=?, "
            "updated_ts=strftime('%s','now') "
            "WHERE operation=? AND idempotency_key_hash=? "
            "AND request_fingerprint=? AND status='prepared'",
            (response_json, identity[0], identity[1], identity[2]),
        )
        if not result.rowcount:
            raise RuntimeError("local_mutation_concurrent_update")
        connection.commit()
        return LocalMutationResult(response=response, replayed=False)
    except Exception:
        connection.rollback()
        raise
