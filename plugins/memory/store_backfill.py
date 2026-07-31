"""History-adapter reads and deterministic memory backfill workflows."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.channel.identity import require_legacy_wxbot_history_scope
from app.common.wxbot_auth import wxbot_sdk_headers
from plugins.memory import store as _store_runtime
from plugins.memory.store import (
    GROUP_HISTORY_USER_ID_SCOPE,
    SESSION_STATE_VERSION,
    _backfill_event_key,
    _build_short_term_summary,
    _coerce_datetime,
    _decode_message_hex,
    _detect_sensitivity,
    _extract_long_term_candidates,
    _format_history_timestamp,
    _group_history_user_scope,
    _history_datetime,
    _history_sync_adapter_for_channel,
    _is_group_session_id,
    _looks_like_wechat_username,
    _memory_event_expiry,
    _message_table_name,
    _normalize_line,
    _parse_group_body,
    _parse_history_target_date,
    _redact_memory_storage_text,
    _sanitize_db_text,
    _to_json,
    _update_session_state,
)

_ACTIVE_LEGACY_WXBOT_HISTORY_SCOPE: ContextVar[tuple[str, str] | None] = ContextVar(
    "memory_active_legacy_wxbot_history_scope",
    default=None,
)


async def _exec(sql: str, params: dict | None = None) -> list[dict]:
    return await _store_runtime._exec(sql, params)


async def safe_trusted_service_request(*args: Any, **kwargs: Any) -> httpx.Response:
    return await _store_runtime.safe_trusted_service_request(*args, **kwargs)


class MemoryBackfillStoreMixin:
    async def _group_backfill_tenant_control_floor(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> tuple[bool, datetime | None]:
        """Return whether a tenant opt-out/erase barrier exists and its re-opt-in."""

        rows = await _exec(
            "SELECT action, before_state_json, after_state_json, created_at "
            "FROM audit_events WHERE tenant_id = :tid AND user_id = :uid "
            "AND target_type = 'social_tenant_member_control' "
            "ORDER BY created_at ASC, id ASC",
            {
                "tid": str(tenant_id or "").strip(),
                "uid": str(user_id or "").strip(),
            },
        )
        barrier_seen = False
        latest_barrier: datetime | None = None
        latest_reopt: datetime | None = None

        def decoded(value: Any) -> dict[str, Any]:
            if isinstance(value, str):
                value = _store_runtime._safe_json_loads(value, {})
            return value if isinstance(value, dict) else {}

        def blocked(value: dict[str, Any]) -> bool:
            control = value.get("control")
            memory_opt_out = (
                bool(control.get("memory_opt_out"))
                if isinstance(control, dict)
                else False
            )
            deletion_state = str(value.get("deletion_state") or "none")
            return memory_opt_out or deletion_state in {
                "requested",
                "failed",
            }

        for row in rows:
            created_at = _coerce_datetime(row.get("created_at"))
            if created_at is None:
                continue
            action = str(row.get("action") or "")
            before = decoded(row.get("before_state_json"))
            after = decoded(row.get("after_state_json"))
            if action == "tenant_member_memory_deletion.completed":
                barrier_seen = True
                latest_barrier = created_at
                latest_reopt = None
                continue
            if action != "tenant_member_control.update":
                continue
            before_blocked = blocked(before)
            after_blocked = blocked(after)
            if after_blocked:
                barrier_seen = True
                latest_barrier = created_at
                latest_reopt = None
            elif before_blocked:
                # A completed erasure remains a barrier, but an explicit
                # update from opt-out/deleted to enabled starts a fresh
                # non-retroactive consent period.
                barrier_seen = True
                latest_reopt = created_at
        if not barrier_seen:
            return False, None
        if (
            latest_reopt is None
            or (
                latest_barrier is not None
                and latest_reopt <= latest_barrier
            )
        ):
            return True, None
        return True, latest_reopt

    async def _group_backfill_consent_since(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        sensitive_required: bool,
    ) -> datetime | None:
        """Return the start of the current uninterrupted history-import consent.

        There is no retroactive-consent field in ``MemberPrivacyValues``.
        Therefore enabling memory today authorizes only history created from
        that policy transition onward.  Invalid or missing history is a
        fail-closed boundary.
        """

        rows = await _exec(
            "SELECT snapshot_json, created_at FROM social_member_policy_history "
            "WHERE tenant_id = :tid AND session_id = :sid AND user_id = :uid "
            "ORDER BY version DESC, created_at DESC",
            {
                "tid": str(tenant_id or "").strip(),
                "sid": str(session_id or "").strip(),
                "uid": str(user_id or "").strip(),
            },
        )
        consent_since: datetime | None = None
        for row in rows:
            snapshot = row.get("snapshot_json")
            if isinstance(snapshot, str):
                snapshot = _store_runtime._safe_json_loads(snapshot, {})
            if not isinstance(snapshot, dict):
                break
            raw_policy = snapshot.get("policy")
            if not isinstance(raw_policy, dict):
                break
            audience = str(raw_policy.get("audience_scope") or "private")
            allowed_sessions = {
                str(value)
                for value in (raw_policy.get("allowed_session_ids") or [])
            }
            audience_allowed = audience == "session" or (
                audience == "explicit" and session_id in allowed_sessions
            )
            enabled = bool(raw_policy.get("memory_enabled")) and audience_allowed
            if sensitive_required:
                enabled = enabled and bool(
                    raw_policy.get("sensitive_memory_enabled")
                )
            created_at = _coerce_datetime(row.get("created_at"))
            if not enabled or created_at is None:
                break
            consent_since = created_at
        return consent_since

    async def _group_backfill_member_policy(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        user_text: Any = "",
        assistant_text: Any = "",
        event_created_at: Any = None,
    ) -> Any | None:
        """Return an opt-in policy for this exact group/member, or deny.

        History import is a durable write, so a missing/invalid control-plane
        row is never treated as consent.  Capture authorization intentionally
        does not depend on ``allow_group_recall``: that flag governs reads,
        while ``memory_enabled`` plus the audience governs writes.
        """

        loader = getattr(self, "get_group_member_privacy_policy", None)
        if not callable(loader):
            return None
        try:
            policy = await loader(
                tenant_id=str(tenant_id or "").strip(),
                session_id=str(session_id or "").strip(),
                user_id=str(user_id or "").strip(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if not bool(getattr(policy, "memory_enabled", False)):
            return None
        audience = str(getattr(policy, "audience_scope", "private") or "private")
        if audience == "session":
            audience_allowed = True
        elif audience == "explicit":
            audience_allowed = str(session_id or "").strip() in {
                str(value)
                for value in (getattr(policy, "allowed_session_ids", None) or [])
            }
        else:
            audience_allowed = False
        if not audience_allowed:
            return None
        sensitivity_user_text = str(user_text or "")
        member_prefix = f"{str(user_id or '').strip()}:"
        if member_prefix != ":" and sensitivity_user_text.startswith(member_prefix):
            sensitivity_user_text = sensitivity_user_text[len(member_prefix) :].lstrip()
        sensitivity = _detect_sensitivity(
            f"{sensitivity_user_text}\n{(assistant_text or '')!s}"
        )
        if sensitivity != "normal" and not bool(
            getattr(policy, "sensitive_memory_enabled", False)
        ):
            return None
        if event_created_at is not None:
            consent_since = await self._group_backfill_consent_since(
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
                sensitive_required=sensitivity != "normal",
            )
            created_at = _coerce_datetime(event_created_at)
            if (
                consent_since is None
                or created_at is None
                or created_at < consent_since
            ):
                return None
            control_barrier, control_floor = (
                await self._group_backfill_tenant_control_floor(
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
            )
            if control_barrier and (
                control_floor is None or created_at < control_floor
            ):
                return None
        return policy

    def _require_legacy_wxbot_history_connection(
        self,
        *,
        tenant_id: str,
        connection_id: str | None = None,
    ) -> tuple[str, str]:
        active_scope = _ACTIVE_LEGACY_WXBOT_HISTORY_SCOPE.get()
        tenant = str(tenant_id or "").strip()
        if not tenant and active_scope is not None:
            tenant = active_scope[0]
        connection = (
            active_scope[1]
            if connection_id is None and active_scope is not None
            else str(connection_id or "").strip()
        )
        try:
            validated_connection = require_legacy_wxbot_history_scope(
                self.settings,
                tenant_id=tenant,
                connection_id=connection,
            )
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        return tenant, validated_connection

    async def _runtime_scope_allowed(
        self,
        gate_name: str,
        *,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        gate = getattr(self, gate_name, None)
        if not callable(gate):
            return not bool(getattr(self, "runtime_scope_gates_required", False))
        try:
            return await gate(str(tenant_id or ""), str(session_id or "")) is True
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _require_history_runtime_scope(
        self,
        *,
        tenant_id: str,
        session_id: str,
    ) -> None:
        combined_gate = getattr(self, "combined_history_scope_execution_allowed", None)
        if callable(combined_gate):
            try:
                if (
                    await combined_gate(
                        str(tenant_id or ""),
                        str(session_id or ""),
                    )
                    is True
                ):
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            raise RuntimeError("memory/wxbot plugin runtime disabled for history scope")
        if not await self._runtime_scope_allowed(
            "scope_execution_allowed",
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            raise RuntimeError("memory plugin runtime disabled for history scope")
        if not await self._runtime_scope_allowed(
            "history_scope_execution_allowed",
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            raise RuntimeError("wxbot plugin runtime disabled for history scope")

    async def _insert_backfill_event(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        message: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        session_id = str(message.get("session_id") or "")
        await self._require_history_runtime_scope(
            tenant_id=tenant_id,
            session_id=session_id,
        )
        source_member_id = str(
            message.get("source_member_id")
            or message.get("sender_wxid")
            or message.get("sender_id")
            or (user_id if user_id != GROUP_HISTORY_USER_ID_SCOPE else "")
        ).strip()[:128]
        timestamp = message.get("ts") or message.get("created_at") or ""
        created_at = _history_datetime(timestamp)
        if (
            channel == "wechat"
            and user_id == GROUP_HISTORY_USER_ID_SCOPE
            and not source_member_id
        ):
            return None, False
        if (
            source_member_id
            and channel == "wechat"
            and _store_runtime._ACTIVE_MUTATION_CONNECTION.get() is None
        ):
            async with self._mutation_transaction():
                return await self._insert_backfill_event(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    message=message,
                )
        if source_member_id and channel == "wechat":
            await self._lock_member_memory_mutation(
                tenant_id=tenant_id,
                user_id=source_member_id,
            )
            if await self._member_memory_write_blocked(
                tenant_id=tenant_id,
                user_id=source_member_id,
            ):
                return None, False
            if user_id == GROUP_HISTORY_USER_ID_SCOPE:
                policy = await self._group_backfill_member_policy(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=source_member_id,
                    user_text=message.get("user_text"),
                    assistant_text=message.get("assistant_text"),
                    event_created_at=created_at,
                )
                if policy is None:
                    return None, False
            else:
                policy = None
        else:
            policy = None

        user_text = _redact_memory_storage_text(message.get("user_text"))
        assistant_text = _redact_memory_storage_text(message.get("assistant_text"))
        if not _normalize_line(user_text):
            return None, False
        source_message_id = str(
            message.get("source_message_id")
            or message.get("message_id")
            or message.get("rowid")
            or ""
        ).strip()
        if source_message_id:
            source_message_id = f"{session_id}:{source_message_id}"[:256]
        event_key = _backfill_event_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            timestamp=timestamp,
            user_text=user_text,
            source_member_id=source_member_id,
            source_message_id=source_message_id,
        )
        if not source_message_id:
            source_message_id = event_key[:256]
        policy_expiry = (
            created_at + timedelta(days=int(policy.retention_days))
            if policy is not None
            else None
        )
        explicit_expiry = _coerce_datetime(message.get("expires_at"))
        if policy_expiry is not None and explicit_expiry is not None:
            requested_expiry = min(policy_expiry, explicit_expiry)
        else:
            requested_expiry = policy_expiry or explicit_expiry
        event_expiry = _memory_event_expiry(
            self.settings,
            created_at=created_at,
            expires_at=requested_expiry,
        )
        rows = await _exec(
            "INSERT INTO plugin_memory_event "
            "(tenant_id, channel, source_key, user_id, session_id, user_text, assistant_text, "
            "trace_id, event_key, source_member_id, source_message_id, expires_at, created_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :sid, :user_text, :assistant_text, "
            ":trace, :event_key, :source_member_id, :source_message_id, :expires_at, :created_at) "
            "ON CONFLICT DO NOTHING "
            "RETURNING id, tenant_id, channel, source_key, user_id, session_id, "
            "user_text, assistant_text, trace_id, event_key, source_member_id, "
            "source_message_id, expires_at, created_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "sid": session_id,
                "user_text": user_text[:2000],
                "assistant_text": assistant_text[:2000],
                "trace": event_key[:128],
                "event_key": event_key,
                "source_member_id": source_member_id,
                "source_message_id": source_message_id,
                "expires_at": event_expiry,
                "created_at": created_at,
            },
        )
        if rows:
            return rows[0], True
        existing = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, "
            "user_text, assistant_text, trace_id, event_key, source_member_id, "
            "source_message_id, expires_at, created_at "
            "FROM plugin_memory_event WHERE event_key = :event_key LIMIT 1",
            {"event_key": event_key},
        )
        return (existing[0] if existing else None), False

    async def _sdk_query_read(
        self,
        *,
        tenant_id: str = "",
        session_id: str = "",
        connection_id: str | None = None,
        database: str,
        sql: str,
        params: list[Any] | dict[str, Any] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        tenant_id, _ = self._require_legacy_wxbot_history_connection(
            tenant_id=tenant_id,
            connection_id=connection_id,
        )
        base_url = str(
            getattr(self.settings, "wxbot_sdk_url", "http://127.0.0.1:5080") or ""
        ).rstrip("/")
        if not base_url:
            raise RuntimeError("wxbot_sdk_url is not configured")
        await self._require_history_runtime_scope(
            tenant_id=tenant_id,
            session_id=session_id,
        )
        try:
            async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
                response = await safe_trusted_service_request(
                    client,
                    "POST",
                    base_url,
                    "/ext/query/read",
                    json={
                        "database": database,
                        "sql": sql,
                        "params": params,
                        "limit": max(1, min(int(limit or 200), 500)),
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        **wxbot_sdk_headers(self.settings),
                    },
                    timeout_seconds=20.0,
                    max_response_bytes=10 * 1024 * 1024,
                    allowed_response_content_types=(
                        "application/json",
                        "application/problem+json",
                        "text/plain",
                    ),
                )
        except httpx.HTTPError as exc:
            raise RuntimeError("wxbot sdk unavailable") from exc
        # The remote response may contain private chat history.  A disable
        # that completed while the SDK request was in flight must prevent the
        # data from entering memory persistence or later LLM processing.
        await self._require_history_runtime_scope(
            tenant_id=tenant_id,
            session_id=session_id,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"wxbot sdk query error: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("wxbot sdk query returned invalid payload") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError("wxbot sdk query returned invalid payload")
        return payload

    async def _sdk_query_rows(
        self,
        *,
        tenant_id: str = "",
        session_id: str = "",
        database: str,
        sql: str,
        params: list[Any] | dict[str, Any] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        payload = await self._sdk_query_read(
            tenant_id=tenant_id,
            session_id=session_id,
            database=database,
            sql=sql,
            params=params,
            limit=limit,
        )
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    async def _load_private_sender_map(
        self,
        sender_ids: Iterable[int],
        *,
        tenant_id: str,
        session_id: str,
    ) -> dict[int, str]:
        unique_ids = sorted({int(item) for item in sender_ids if int(item) > 0})
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = await self._sdk_query_rows(
            tenant_id=tenant_id,
            session_id=session_id,
            database="message",
            sql=f"SELECT rowid, user_name FROM Name2Id WHERE rowid IN ({placeholders})",
            params=unique_ids,
            limit=min(len(unique_ids), 500),
        )
        return {
            int(row["rowid"]): str(row.get("user_name") or "")
            for row in rows
            if row.get("rowid") is not None
        }

    async def _load_wechat_group_contact_display_map(
        self,
        *,
        tenant_id: str = "",
        session_id: str,
        usernames: Iterable[str],
    ) -> dict[str, dict[str, str]]:
        room_id = _normalize_line(_sanitize_db_text(session_id))
        if not _is_group_session_id(room_id):
            return {}
        unique_usernames = sorted(
            {
                _normalize_line(_sanitize_db_text(username))
                for username in usernames
                if _looks_like_wechat_username(username)
            }
        )
        if not unique_usernames:
            return {}
        unique_usernames = unique_usernames[:500]
        placeholders = ", ".join("?" for _ in unique_usernames)
        try:
            rows = await self._sdk_query_rows(
                tenant_id=tenant_id,
                session_id=session_id,
                database="contact",
                sql=(
                    "SELECT mn.username AS username, c.remark, c.nick_name, c.alias "
                    "FROM chatroom_member cm "
                    "JOIN name2id rn ON rn.rowid = cm.room_id "
                    "JOIN name2id mn ON mn.rowid = cm.member_id "
                    "LEFT JOIN contact c ON c.username = mn.username "
                    f"WHERE rn.username = ? AND mn.username IN ({placeholders})"
                ),
                params=[room_id, *unique_usernames],
                limit=min(len(unique_usernames), 500),
            )
        except Exception:
            return {}
        metadata_by_username: dict[str, dict[str, str]] = {}
        for row in rows:
            username = _normalize_line(_sanitize_db_text(row.get("username")))
            if not username:
                continue
            metadata_by_username[username] = {
                "remark": _normalize_line(
                    _redact_memory_storage_text(row.get("remark"))
                )[:80],
                "nick_name": _normalize_line(
                    _redact_memory_storage_text(row.get("nick_name"))
                )[:80],
                "alias": _normalize_line(
                    _redact_memory_storage_text(row.get("alias"))
                )[:80],
            }
        return metadata_by_username

    async def _collect_session_history(
        self,
        *,
        tenant_id: str = "",
        session_id: str,
        user_id: str | None,
        cutoff_ts: int,
        max_messages: int,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return []
        user_id_scope, group_scope_auto = _group_history_user_scope(session_id, user_id)
        table = _message_table_name(session_id)
        table_exists = await self._sdk_query_rows(
            tenant_id=tenant_id,
            session_id=session_id,
            database="message",
            sql="SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = ?",
            params=[table],
            limit=1,
        )
        if not table_exists:
            return []

        history_limit = max(1, min(int(max_messages or 200), 10000))
        page_size = max(50, min(history_limit, 500))
        raw_row_budget = history_limit * 5
        is_group = _is_group_session_id(session_id)
        messages: list[dict[str, Any]] = []

        async def append_filtered_rows(rows: list[dict[str, Any]]) -> None:
            sender_map: dict[int, str] = {}
            if not is_group:
                sender_map = await self._load_private_sender_map(
                    (
                        int(row.get("real_sender_id") or 0)
                        for row in rows
                        if row.get("real_sender_id") is not None
                    ),
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
            for row in rows:
                body = _decode_message_hex(
                    str(row.get("message_content_hex") or ""),
                    row.get("compression_type"),
                )
                if not body:
                    continue
                if is_group:
                    sender_wxid, content = _parse_group_body(body)
                    sender_wxid = str(sender_wxid or "").strip()
                    if not group_scope_auto and sender_wxid != user_id_scope:
                        continue
                else:
                    sender_wxid = sender_map.get(int(row.get("real_sender_id") or 0), "")
                    if sender_wxid != user_id_scope:
                        continue
                    content = body
                normalized_content = str(content or "").strip()
                if not normalized_content:
                    continue
                user_text = normalized_content
                if group_scope_auto and sender_wxid:
                    user_text = f"{sender_wxid}: {normalized_content}"
                message = {
                    "session_id": session_id,
                    "user_text": user_text[:1000],
                    "assistant_text": "",
                    "created_at": _format_history_timestamp(row.get("create_time")),
                    "ts": int(row.get("create_time") or 0),
                    "source_message_id": str(row.get("rowid") or ""),
                }
                if is_group:
                    message.update(
                        {
                            "sender_id": sender_wxid,
                            "sender_wxid": sender_wxid,
                            "source_member_id": sender_wxid,
                        }
                    )
                elif sender_wxid:
                    message["source_member_id"] = sender_wxid
                messages.append(message)
            if len(messages) > history_limit:
                del messages[:-history_limit]

        try:
            cursor_create_time: int | None = None
            cursor_rowid: int | None = None
            raw_rows_seen = 0
            while raw_rows_seen < raw_row_budget:
                page_limit = min(page_size, raw_row_budget - raw_rows_seen)
                where = "local_type = 1 AND create_time >= ?"
                params: list[Any] = [cutoff_ts]
                if end_ts is not None:
                    where += " AND create_time < ?"
                    params.append(end_ts)
                if cursor_create_time is not None and cursor_rowid is not None:
                    where += " AND (create_time > ? OR (create_time = ? AND rowid > ?))"
                    params.extend([cursor_create_time, cursor_create_time, cursor_rowid])
                rows = await self._sdk_query_rows(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    database="message",
                    sql=(
                        f"SELECT rowid AS rowid, create_time, real_sender_id, local_type, "
                        f"hex(message_content) AS message_content_hex, "
                        f"WCDB_CT_message_content AS compression_type "
                        f"FROM [{table}] "
                        f"WHERE {where} "
                        f"ORDER BY create_time ASC, rowid ASC"
                    ),
                    params=params,
                    limit=page_limit,
                )
                if not rows:
                    break
                await append_filtered_rows(rows)
                raw_rows_seen += len(rows)
                last_row = rows[-1]
                try:
                    next_create_time = int(last_row.get("create_time") or 0)
                    next_rowid = int(last_row["rowid"])
                except Exception as exc:
                    raise RuntimeError("message rowid cursor unavailable") from exc
                if next_create_time == cursor_create_time and next_rowid == cursor_rowid:
                    break
                cursor_create_time = next_create_time
                cursor_rowid = next_rowid
                if len(rows) < page_limit:
                    break
        except RuntimeError:
            messages.clear()
            offset = 0
            raw_rows_seen = 0
            while raw_rows_seen < raw_row_budget:
                page_limit = min(page_size, raw_row_budget - raw_rows_seen)
                where = "local_type = 1 AND create_time >= ?"
                params: list[Any] = [cutoff_ts]
                if end_ts is not None:
                    where += " AND create_time < ?"
                    params.append(end_ts)
                params.extend([page_limit, offset])
                rows = await self._sdk_query_rows(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    database="message",
                    sql=(
                        f"SELECT rowid AS rowid, create_time, real_sender_id, local_type, "
                        f"hex(message_content) AS message_content_hex, "
                        f"WCDB_CT_message_content AS compression_type "
                        f"FROM [{table}] "
                        f"WHERE {where} "
                        f"ORDER BY create_time ASC "
                        f"LIMIT ? OFFSET ?"
                    ),
                    params=params,
                    limit=page_limit,
                )
                if not rows:
                    break
                await append_filtered_rows(rows)
                raw_rows_seen += len(rows)
                offset += len(rows)
                if len(rows) < page_limit:
                    break
        if len(messages) > history_limit:
            messages = messages[-history_limit:]
        return messages

    async def get_group_graph_history_dates(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id: str | None,
        recent_days: int = 14,
    ) -> dict[str, Any]:
        if str(channel or "").strip().lower() != "wechat":
            raise RuntimeError("memory history dates only supports wechat channel")
        session_id = str(session_id or "").strip()
        if not session_id:
            raise RuntimeError("session_id required")
        user_id, user_id_auto = _group_history_user_scope(session_id, user_id)
        if not user_id:
            raise RuntimeError("user_id required")

        recent_days = max(1, min(int(recent_days or 14), 90))
        today = datetime.now().date()
        rows: list[dict[str, Any]] = []
        for offset in range(recent_days):
            day = today - timedelta(days=offset)
            start_at = datetime.combine(day, datetime.min.time())
            end_at = start_at + timedelta(days=1)
            messages = await self._collect_session_history(
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
                cutoff_ts=int(start_at.timestamp()),
                end_ts=int(end_at.timestamp()),
                max_messages=10000,
            )
            imported = await _exec(
                "SELECT COUNT(*) AS count FROM plugin_memory_event "
                "WHERE tenant_id = :tid AND channel = :channel "
                "AND source_key IN (:source_key, '*') "
                "AND user_id = :uid AND session_id = :sid "
                "AND created_at >= :start_at AND created_at < :end_at",
                {
                    "tid": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "uid": user_id,
                    "sid": session_id,
                    "start_at": start_at,
                    "end_at": end_at,
                },
            )
            raw_count = len(messages)
            imported_count = int((imported[0] if imported else {}).get("count") or 0)
            job_counts = await self.get_llm_extraction_job_status_counts_for_day(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                start_at=start_at,
                end_at=end_at,
            )
            if raw_count <= 0:
                status = "not_extracted"
            elif imported_count <= 0:
                status = "not_extracted"
            elif imported_count >= raw_count:
                status = "extracted"
            else:
                status = "partial"
            rows.append(
                {
                    "date": day.isoformat(),
                    "raw_message_count": raw_count,
                    "imported_count": imported_count,
                    "job_counts": job_counts,
                    "status": status,
                }
            )
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "session_id": session_id,
            "user_id": user_id,
            "user_id_scope": user_id,
            "user_id_auto": user_id_auto,
            "recent_days": recent_days,
            "items": rows,
        }

    async def _live_group_backfill_messages(
        self,
        *,
        tenant_id: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep only still-present evidence from members who permit memory."""

        by_event_id: dict[int, dict[str, Any]] = {}
        for message in messages:
            event = message.get("source_event")
            if not isinstance(event, dict):
                continue
            try:
                event_id = int(event.get("id"))
            except (TypeError, ValueError):
                continue
            if event_id > 0:
                by_event_id[event_id] = message
        if not by_event_id:
            return []
        rows = await _exec(
            "SELECT id, source_member_id, session_id, user_text, assistant_text, "
            "created_at, expires_at FROM plugin_memory_event "
            "WHERE id = ANY(:event_ids) ORDER BY id ASC",
            {"event_ids": sorted(by_event_id)},
        )
        allowed_ids: set[int] = set()
        for member_id in sorted(
            {
                str(row.get("source_member_id") or "").strip()
                for row in rows
                if str(row.get("source_member_id") or "").strip()
            }
        ):
            await self._lock_member_memory_mutation(
                tenant_id=tenant_id,
                user_id=member_id,
            )
        blocked_members: set[str] = set()
        for member_id in {
            str(row.get("source_member_id") or "").strip() for row in rows
        }:
            if member_id and await self._member_memory_write_blocked(
                tenant_id=tenant_id,
                user_id=member_id,
            ):
                blocked_members.add(member_id)
        now = datetime.now(UTC).replace(tzinfo=None)
        for row in rows:
            event_id = int(row.get("id") or 0)
            member_id = str(row.get("source_member_id") or "").strip()
            event_session_id = str(row.get("session_id") or "").strip()
            if event_id <= 0 or not member_id or member_id in blocked_members:
                continue
            policy = await self._group_backfill_member_policy(
                tenant_id=tenant_id,
                session_id=event_session_id,
                user_id=member_id,
                user_text=row.get("user_text"),
                assistant_text=row.get("assistant_text"),
                event_created_at=row.get("created_at"),
            )
            if policy is None:
                continue
            created_at = _coerce_datetime(row.get("created_at"))
            expires_at = _coerce_datetime(row.get("expires_at"))
            policy_expiry = (
                created_at + timedelta(days=int(policy.retention_days))
                if created_at is not None
                else None
            )
            effective_expiry = min(
                [
                    value
                    for value in (expires_at, policy_expiry)
                    if value is not None
                ],
                default=None,
            )
            if effective_expiry is None or effective_expiry > now:
                allowed_ids.add(event_id)
        return [
            message
            for event_id, message in by_event_id.items()
            if event_id in allowed_ids
        ]

    async def _apply_backfill_session_messages(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id: str,
        messages: list[dict[str, Any]],
        imported_count: int | None = None,
    ) -> dict[str, Any]:
        if (
            user_id == GROUP_HISTORY_USER_ID_SCOPE
            and _store_runtime._ACTIVE_MUTATION_CONNECTION.get() is None
        ):
            async with self._mutation_transaction():
                return await self._apply_backfill_session_messages(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    session_id=session_id,
                    user_id=user_id,
                    messages=messages,
                    imported_count=imported_count,
                )
        await self._require_history_runtime_scope(
            tenant_id=tenant_id,
            session_id=session_id,
        )
        if user_id == GROUP_HISTORY_USER_ID_SCOPE:
            messages = await self._live_group_backfill_messages(
                tenant_id=tenant_id,
                messages=messages,
            )
            imported_count = min(
                len(messages),
                len(messages) if imported_count is None else max(0, int(imported_count or 0)),
            )
        current = await self.get_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )
        current_short_items = list(current.get("short_term_items") or [])
        if current_short_items:
            short_items = current_short_items
            short_term_memory = current.get("short_term_memory") or _build_short_term_summary(
                short_items
            )
        else:
            short_items = [
                {
                    "session_id": session_id,
                    "user_text": item["user_text"],
                    "assistant_text": item.get("assistant_text") or "",
                    "created_at": item.get("created_at") or "",
                }
                for item in messages[-6:]
            ]
            short_term_memory = _build_short_term_summary(short_items)
        session_state = dict(current)
        # Only newly inserted/idempotency-filtered events reach this method.
        # Feed the whole accepted batch chronologically so evicted turns enter
        # the rolling compaction instead of being discarded before the state
        # machine can summarize them.
        for item in messages:
            session_state = {
                **session_state,
                **_update_session_state(
                    session_state,
                    session_id=session_id,
                    user_text=str(item.get("user_text") or ""),
                    assistant_text=str(item.get("assistant_text") or ""),
                    created_at=str(
                        item.get("created_at") or datetime.now(UTC).replace(tzinfo=None).isoformat()
                    ),
                ),
            }

        imported_count = (
            len(messages) if imported_count is None else max(0, int(imported_count or 0))
        )
        if imported_count <= 0 and current.get("session_id"):
            return current
        await self._require_history_runtime_scope(
            tenant_id=tenant_id,
            session_id=session_id,
        )
        await _exec(
            "INSERT INTO plugin_memory_session_profile "
            "(tenant_id, channel, source_key, session_id, user_id, short_term_memory, manual_notes, "
            "short_term_items_json, session_summary, open_items_json, decisions_json, recent_turns_json, "
            "last_compacted_at, summary_version, message_count, imported_message_count, last_seen_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :sid, :uid, :short_term, :manual, :short_items, "
            ":session_summary, :open_items, :decisions, :recent_turns, :last_compacted_at, "
            ":summary_version, :message_count, :imported_message_count, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, channel, source_key, session_id, user_id) DO UPDATE SET "
            "short_term_memory = EXCLUDED.short_term_memory, "
            "manual_notes = EXCLUDED.manual_notes, "
            "short_term_items_json = EXCLUDED.short_term_items_json, "
            "session_summary = EXCLUDED.session_summary, "
            "open_items_json = EXCLUDED.open_items_json, "
            "decisions_json = EXCLUDED.decisions_json, "
            "recent_turns_json = EXCLUDED.recent_turns_json, "
            "last_compacted_at = EXCLUDED.last_compacted_at, "
            "summary_version = EXCLUDED.summary_version, "
            "message_count = EXCLUDED.message_count, "
            "imported_message_count = EXCLUDED.imported_message_count, "
            "last_seen_at = EXCLUDED.last_seen_at, "
            "updated_at = EXCLUDED.updated_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "sid": session_id,
                "uid": user_id,
                "short_term": short_term_memory,
                "manual": current.get("manual_notes") or "",
                "short_items": _to_json(short_items),
                "session_summary": session_state.get("session_summary") or "",
                "open_items": _to_json(session_state.get("open_items") or []),
                "decisions": _to_json(session_state.get("decisions") or []),
                "recent_turns": _to_json(session_state.get("recent_turns") or []),
                "last_compacted_at": session_state.get("last_compacted_at"),
                "summary_version": int(
                    session_state.get("summary_version") or SESSION_STATE_VERSION
                ),
                "message_count": int(current.get("message_count") or 0) + imported_count,
                "imported_message_count": int(current.get("imported_message_count") or 0)
                + imported_count,
            },
        )
        return await self.get_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )

    async def _apply_backfill_identity_messages(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        last_session_id: str,
        messages: list[dict[str, Any]],
        imported_count: int | None = None,
    ) -> dict[str, Any]:
        if (
            user_id == GROUP_HISTORY_USER_ID_SCOPE
            and _store_runtime._ACTIVE_MUTATION_CONNECTION.get() is None
        ):
            async with self._mutation_transaction():
                return await self._apply_backfill_identity_messages(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    last_session_id=last_session_id,
                    messages=messages,
                    imported_count=imported_count,
                )
        await self._require_history_runtime_scope(
            tenant_id=tenant_id,
            session_id=last_session_id,
        )
        if user_id == GROUP_HISTORY_USER_ID_SCOPE:
            messages = await self._live_group_backfill_messages(
                tenant_id=tenant_id,
                messages=messages,
            )
            imported_count = min(
                len(messages),
                len(messages) if imported_count is None else max(0, int(imported_count or 0)),
            )
        current = await self.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )
        if not await self.list_memory_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id="",
            scope_type="identity",
            limit=1,
        ):
            await self._import_legacy_identity_items(current)
        imported_count = (
            len(messages) if imported_count is None else max(0, int(imported_count or 0))
        )
        if imported_count <= 0 and current.get("user_id"):
            return current
        await self._require_history_runtime_scope(
            tenant_id=tenant_id,
            session_id=last_session_id,
        )
        await _exec(
            "INSERT INTO plugin_memory_identity_profile "
            "(tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
            "long_term_items_json, message_count, imported_message_count, last_session_id, "
            "last_seen_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :long_term, :manual, :long_items, "
            ":message_count, :imported_message_count, :last_session_id, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, channel, source_key, user_id) DO UPDATE SET "
            "long_term_memory = EXCLUDED.long_term_memory, "
            "manual_notes = EXCLUDED.manual_notes, "
            "long_term_items_json = EXCLUDED.long_term_items_json, "
            "message_count = EXCLUDED.message_count, "
            "imported_message_count = EXCLUDED.imported_message_count, "
            "last_session_id = EXCLUDED.last_session_id, "
            "last_seen_at = EXCLUDED.last_seen_at, "
            "updated_at = EXCLUDED.updated_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "long_term": current.get("long_term_memory") or "",
                "manual": current.get("manual_notes") or "",
                "long_items": _to_json(current.get("long_term_items") or []),
                "message_count": int(current.get("message_count") or 0) + imported_count,
                "imported_message_count": int(current.get("imported_message_count") or 0)
                + imported_count,
                "last_session_id": last_session_id,
            },
        )
        await self._refresh_legacy_cache_for_item_scope(
            {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "user_id": user_id,
                "session_id": "",
                "scope_type": "identity",
            }
        )
        return await self.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )

    async def backfill_from_sdk(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str | None,
        session_ids: list[str],
        connection_id: str = "",
        days_limit: int = 180,
        max_messages_per_session: int = 200,
        enqueue_llm_jobs: bool = False,
        target_date: str | None = None,
    ) -> dict[str, Any]:
        tenant = str(tenant_id or "").strip()
        connection = str(connection_id or "").strip()
        if bool(getattr(self, "runtime_scope_gates_required", False)):
            tenant, connection = self._require_legacy_wxbot_history_connection(
                tenant_id=tenant,
                connection_id=connection,
            )
        token = _ACTIVE_LEGACY_WXBOT_HISTORY_SCOPE.set((tenant, connection))
        try:
            return await self._backfill_from_sdk_scoped(
                tenant_id=tenant,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_ids=session_ids,
                days_limit=days_limit,
                max_messages_per_session=max_messages_per_session,
                enqueue_llm_jobs=enqueue_llm_jobs,
                target_date=target_date,
            )
        finally:
            _ACTIVE_LEGACY_WXBOT_HISTORY_SCOPE.reset(token)

    async def _backfill_from_sdk_scoped(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str | None,
        session_ids: list[str],
        days_limit: int = 180,
        max_messages_per_session: int = 200,
        enqueue_llm_jobs: bool = False,
        target_date: str | None = None,
    ) -> dict[str, Any]:
        if _store_runtime._ACTIVE_MUTATION_CONNECTION.get() is None:
            # Event, deterministic projections, extraction jobs and both
            # profile caches are one retryable unit.  Otherwise an event-only
            # commit would make the next run treat the message as a duplicate
            # and permanently skip its missing derived state.
            async with self._mutation_transaction():
                return await self._backfill_from_sdk_scoped(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_ids=session_ids,
                    days_limit=days_limit,
                    max_messages_per_session=max_messages_per_session,
                    enqueue_llm_jobs=enqueue_llm_jobs,
                    target_date=target_date,
                )
        history_adapter = _history_sync_adapter_for_channel(channel, self)
        cleaned_sessions = [
            str(item or "").strip() for item in session_ids if str(item or "").strip()
        ]
        unique_sessions = list(dict.fromkeys(cleaned_sessions))
        if not unique_sessions:
            raise RuntimeError("session_ids required")
        requested_user_id = str(user_id or "").strip()
        auto_scope_sessions = [
            session_id
            for session_id in unique_sessions
            if _group_history_user_scope(session_id, requested_user_id)[1]
        ]
        if not requested_user_id and len(auto_scope_sessions) != len(unique_sessions):
            raise RuntimeError("user_id required")
        if requested_user_id == GROUP_HISTORY_USER_ID_SCOPE and len(auto_scope_sessions) != len(
            unique_sessions
        ):
            raise RuntimeError("user_id required")
        user_id_scope = GROUP_HISTORY_USER_ID_SCOPE if auto_scope_sessions else requested_user_id
        user_id_auto = bool(auto_scope_sessions)
        days_limit = max(0, int(days_limit or 0))
        target_day = _parse_history_target_date(target_date)
        if target_day is not None:
            max_messages_per_session = 10000
            cutoff_ts = int(target_day.timestamp())
            end_ts: int | None = int((target_day + timedelta(days=1)).timestamp())
        else:
            max_messages_per_session = max(1, min(int(max_messages_per_session or 200), 500))
            cutoff_ts = 0
            end_ts = None
            if days_limit > 0:
                cutoff_ts = int((datetime.now() - timedelta(days=days_limit)).timestamp())

        per_session_stats: list[dict[str, Any]] = []
        imported_messages: list[dict[str, Any]] = []
        stats = {
            "processed_count": 0,
            "imported_count": 0,
            "skipped_count": 0,
            "duplicate_count": 0,
            "events_inserted": 0,
            "events_duplicate": 0,
            "items_created": 0,
            "items_updated": 0,
            "items_pending": 0,
            "jobs_enqueued": 0,
        }
        should_enqueue_jobs = bool(enqueue_llm_jobs) and self._can_enqueue_llm_extraction_jobs()
        for session_id in unique_sessions:
            await self._require_history_runtime_scope(
                tenant_id=tenant_id,
                session_id=session_id,
            )
            is_group_session = _is_group_session_id(session_id)
            is_group_history_scope = (
                is_group_session and user_id_scope == GROUP_HISTORY_USER_ID_SCOPE
            )
            audience_kwargs = {
                "origin_session_kind": "group" if is_group_session else "private",
                "audience_scope": "session" if is_group_history_scope else "private",
                "allowed_session_ids": [session_id] if is_group_history_scope else [],
                "sensitivity_category": "normal",
                "expires_at": None,
                "source_kind": "backfill",
            }
            session_messages = await history_adapter.collect_session_history(
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id_scope,
                cutoff_ts=cutoff_ts,
                end_ts=end_ts,
                max_messages=max_messages_per_session,
            )
            session_stats = {
                "session_id": session_id,
                "processed_count": len(session_messages),
                "imported_count": 0,
                "skipped_count": 0,
                "duplicate_count": 0,
                "events_inserted": 0,
                "events_duplicate": 0,
                "items_created": 0,
                "items_updated": 0,
                "items_pending": 0,
                "jobs_enqueued": 0,
                "first_timestamp": session_messages[0]["created_at"] if session_messages else "",
                "last_timestamp": session_messages[-1]["created_at"] if session_messages else "",
            }
            inserted_session_messages: list[dict[str, Any]] = []
            for message in session_messages:
                await self._require_history_runtime_scope(
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
                stats["processed_count"] += 1
                event, inserted = await self._insert_backfill_event(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id_scope,
                    message=message,
                )
                if event is None:
                    stats["skipped_count"] += 1
                    session_stats["skipped_count"] += 1
                    continue
                if not inserted:
                    stats["duplicate_count"] += 1
                    stats["events_duplicate"] += 1
                    session_stats["duplicate_count"] += 1
                    session_stats["events_duplicate"] += 1
                    continue

                stats["imported_count"] += 1
                stats["events_inserted"] += 1
                session_stats["imported_count"] += 1
                session_stats["events_inserted"] += 1
                # The database adapter returns the complete event row, while
                # alternate adapters and test doubles may only return the
                # generated identifiers. Preserve the already-sanitized source
                # message as the contract fallback instead of silently
                # skipping deterministic extraction.
                event_user_text = _redact_memory_storage_text(
                    event.get("user_text") or message.get("user_text") or ""
                )
                event_assistant_text = _redact_memory_storage_text(
                    event.get("assistant_text") or message.get("assistant_text") or ""
                )
                enriched_message = {
                    **message,
                    "user_text": event_user_text,
                    "assistant_text": event_assistant_text,
                    "source_event": event,
                }
                inserted_session_messages.append(enriched_message)
                imported_messages.append(enriched_message)
                message_audience_kwargs = {
                    **audience_kwargs,
                    "sensitivity_category": _detect_sensitivity(event_user_text),
                    "expires_at": event.get("expires_at"),
                }

                for candidate in _extract_long_term_candidates(event_user_text):
                    await self._require_history_runtime_scope(
                        tenant_id=tenant_id,
                        session_id=session_id,
                    )
                    item = await self._apply_structured_memory_action(
                        tenant_id=tenant_id,
                        channel=channel,
                        source_key=source_key,
                        user_id=user_id_scope,
                        action=candidate,
                        source_event_id=int(event["id"]) if event.get("id") is not None else None,
                        source_trace_id=str(event.get("trace_id") or "backfill"),
                        original_text=event_user_text,
                        source_type_override="backfill",
                        **message_audience_kwargs,
                    )
                    if item is not None:
                        if int(item.get("occurrence_count") or 1) <= 1:
                            stats["items_created"] += 1
                            session_stats["items_created"] += 1
                        else:
                            stats["items_updated"] += 1
                            session_stats["items_updated"] += 1
                        if str(item.get("status") or "") == "pending":
                            stats["items_pending"] += 1
                            session_stats["items_pending"] += 1

                if should_enqueue_jobs:
                    await self._require_history_runtime_scope(
                        tenant_id=tenant_id,
                        session_id=session_id,
                    )
                    job = await self.enqueue_llm_extraction_job(
                        tenant_id=tenant_id,
                        channel=channel,
                        source_key=source_key,
                        user_id=user_id_scope,
                        session_id=session_id,
                        trace_id=str(event.get("trace_id") or ""),
                        source_event_id=int(event["id"]) if event.get("id") is not None else None,
                        **message_audience_kwargs,
                    )
                    if job is not None:
                        stats["jobs_enqueued"] += 1
                        session_stats["jobs_enqueued"] += 1

            if inserted_session_messages:
                await self._require_history_runtime_scope(
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
                await self._apply_backfill_session_messages(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    session_id=session_id,
                    user_id=user_id_scope,
                    messages=inserted_session_messages,
                    imported_count=len(inserted_session_messages),
                )
            per_session_stats.append(session_stats)

        for session_id in unique_sessions:
            await self._require_history_runtime_scope(
                tenant_id=tenant_id,
                session_id=session_id,
            )
        identity_profile = await self._apply_backfill_identity_messages(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id_scope,
            last_session_id=imported_messages[-1]["session_id"] if imported_messages else "",
            messages=imported_messages,
            imported_count=len(imported_messages),
        )
        session_profiles = await self.list_session_profiles(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id_scope,
            limit=min(len(unique_sessions), 200) or 50,
        )
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "user_id": user_id_scope,
            "user_id_scope": user_id_scope,
            "user_id_auto": user_id_auto,
            "days_limit": days_limit,
            "target_date": target_day.date().isoformat() if target_day is not None else None,
            "max_messages_per_session": max_messages_per_session,
            "session_count": len(unique_sessions),
            "processed_count": stats["processed_count"],
            "imported_count": stats["imported_count"],
            "skipped_count": stats["skipped_count"],
            "duplicate_count": stats["duplicate_count"],
            "events_inserted": stats["events_inserted"],
            "events_duplicate": stats["events_duplicate"],
            "items_created": stats["items_created"],
            "items_updated": stats["items_updated"],
            "items_pending": stats["items_pending"],
            "jobs_enqueued": stats["jobs_enqueued"],
            "llm_jobs_enabled": should_enqueue_jobs,
            "sessions": per_session_stats,
            "identity_profile": identity_profile,
            "session_profiles": session_profiles,
        }
