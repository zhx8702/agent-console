"""
Database persistence for the wxbot plugin.

Tables:
- plugin_wxbot_reply_queue: outbound replies waiting for SDK dispatch
- plugin_wxbot_interaction_cursor: latest per-group inbound for stale-reply fencing
- plugin_wxbot_user_bans: per-group user bans for wxbot ingress
- plugin_wxbot_member_events: typed member events mirrored from SDK
- plugin_wxbot_media_ready_events: message.media.ready updates mirrored from SDK
- plugin_wxbot_group_observations: durable, idempotent group-message observations
- plugin_wxbot_group_summary_state: optimistic-lock state for rolling group summaries
- plugin_wxbot_group_summary_jobs: debounced, leased rolling-summary work
- plugin_wxbot_tenant_policy: tenant-wide default auto-reply policy
- plugin_wxbot_session_policy: per-session auto-reply policy for full SDK ingress
- plugin_wxbot_report_subscriptions: local daily/weekly/monthly report subscriptions
- plugin_wxbot_report_jobs: cached daily/weekly/monthly report generation jobs
- plugin_wxbot_self_review_subscriptions: per-group self-review schedules
- plugin_wxbot_self_review_jobs: cached daily self-review jobs
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.channel.identity import (
    LEGACY_WXBOT_CONNECTION_ID,
    canonical_conversation_id,
    canonical_participant_id,
)
from app.common.logging import get_logger
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema
from app.social.reply_style import NaturalReplyStyleGuard
from app.social.revalidation import evaluate_group_reply_revalidation
from app.social.speech_ledger import (
    GroupSpeechBudgetExceeded,
    GroupSpeechLedger,
    GroupSpeechLedgerProtocol,
    SpeechReservation,
    derive_speech_idempotency_key,
    reserve_or_raise,
)
from app.social.telemetry import observe_duplicate_guard, observe_final_delivery
from plugins.wxbot.admin_mutations import (
    WxbotAdminIdempotencyConflictError,
    WxbotAdminMutationBusyError,
    WxbotAdminMutationClaim,
    WxbotAdminMutationResult,
    WxbotAdminVersionConflictError,
    claim_admin_mutation,
    complete_admin_mutation,
    fail_admin_mutation,
    observe_admin_resource,
)
from plugins.wxbot.report_store import WxbotReportStoreMixin

__all__ = [
    "WxbotAdminIdempotencyConflictError",
    "WxbotAdminMutationBusyError",
    "WxbotAdminVersionConflictError",
    "WxbotStore",
    "normalize_wxbot_event_connection_id",
]

logger = get_logger(__name__)

DEFAULT_GROUP_PARTICIPATION_POLICY: dict[str, Any] = {
    "threshold": 60,
    "quiet_start_hour": 23,
    "quiet_end_hour": 8,
    "timezone": "Asia/Shanghai",
    "max_soft_replies_10m": 2,
    "max_soft_replies_hour": 6,
    "max_bot_ratio_last_40": 0.15,
    "max_consecutive_bot_messages": 2,
}

_GLOBAL_POLICY_COLUMNS = (
    "tenant_id, private_reply_mode, group_reply_mode, "
    "group_reply_mention_sender, trigger_keywords_text, version, updated_at"
)
_SESSION_POLICY_COLUMNS = (
    "tenant_id, session_id, reply_mode, mention_sender_mode, "
    "trigger_keywords_text, reply_cooldown_seconds, coalesce_window_ms, "
    "adaptive_cooldown_enabled, participation_policy_json, version, updated_at"
)


class WxbotPolicyVersionConflictError(RuntimeError):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(f"expected version {expected}, current version {current}")
        self.expected = expected
        self.current = current


class ReplyPolicyIdempotencyConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WxbotPolicyMutation:
    before: dict[str, Any]
    after: dict[str, Any]


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    cleaned = str(value).strip().lower()
    if cleaned in {"1", "true", "yes", "y", "on"}:
        return True
    if cleaned in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def normalize_group_participation_policy(
    value: Any,
    *,
    current: Any = None,
) -> dict[str, Any]:
    merged = {
        **DEFAULT_GROUP_PARTICIPATION_POLICY,
        **{
            key: item
            for key, item in _json_object(current).items()
            if key in DEFAULT_GROUP_PARTICIPATION_POLICY
        },
        **{
            key: item
            for key, item in _json_object(value).items()
            if key in DEFAULT_GROUP_PARTICIPATION_POLICY
        },
    }
    return merged


def _queue_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(parsed, datetime):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _speech_output_kind(delivery: dict[str, Any]) -> str:
    explicit = str(delivery.get("speech_output_kind") or "").strip().lower()
    if explicit in {"ordinary", "proactive", "repeater", "report"}:
        return explicit
    source = str(delivery.get("source") or "").strip().lower()
    if source == "group_activity":
        return "proactive"
    if str(delivery.get("reply_policy_reason") or "").strip() == "repeater_triggered":
        return "repeater"
    return "ordinary"


def _speech_class(delivery: dict[str, Any], output_kind: str) -> str:
    explicit = str(delivery.get("speech_class") or "").strip().lower()
    if explicit in {"obligation", "soft", "scheduled"}:
        return explicit
    participation = str(delivery.get("participation_status") or "").strip()
    if participation == "must_reply" or bool(delivery.get("privacy_control")):
        return "obligation"
    if bool(delivery.get("force_send")) and output_kind == "ordinary":
        return "obligation"
    if output_kind in {"proactive", "repeater", "report"}:
        return "scheduled"
    return "soft"


def _delivery_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _is_absolute_outbound_file_path(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _normalize_outbound_file_size(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("file_size must be a non-negative integer")
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("file_size must be a non-negative integer") from exc
    if size < 0:
        raise ValueError("file_size must be a non-negative integer")
    return size


def _normalize_outbound_file_digest(value: object, *, algorithm: str) -> str:
    digest = str(value or "").strip().lower()
    length = 32 if algorithm == "md5" else 64
    if digest and (
        len(digest) != length or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"file_{algorithm} must be a {length}-character lowercase hex digest")
    return digest


def _validate_configured_outbound_file(
    settings: Any,
    *,
    file_path: str,
    file_size: int | None,
    file_md5: str,
    file_sha256: str,
) -> None:
    """Fail closed for production settings while keeping legacy test doubles usable."""

    if not hasattr(settings, "wxbot_outbound_file_dir"):
        return
    raw_root = str(getattr(settings, "wxbot_outbound_file_dir", "") or "").strip()
    if not raw_root:
        raise ValueError("wxbot_outbound_file_dir must be configured for file messages")
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        raise ValueError("wxbot_outbound_file_dir must be absolute")
    root = root.resolve(strict=False)
    raw_candidate = Path(file_path).expanduser()
    if raw_candidate.is_symlink():
        raise ValueError("file_path must not be a symbolic link")
    candidate = raw_candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("file_path must be inside wxbot_outbound_file_dir") from exc
    if not candidate.exists() or not candidate.is_file():
        raise ValueError("file_path must reference an existing regular file")
    actual_size = candidate.stat().st_size
    configured_max = int(
        getattr(settings, "wxbot_outbound_file_max_bytes", 100 * 1024 * 1024)
        or 100 * 1024 * 1024
    )
    if actual_size > configured_max:
        raise ValueError("file_path exceeds wxbot_outbound_file_max_bytes")
    if file_size is not None and actual_size != file_size:
        raise ValueError("file_size does not match the staged file")
    if file_md5 or file_sha256:
        digest_md5 = hashlib.md5()
        digest_sha256 = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest_md5.update(chunk)
                digest_sha256.update(chunk)
        if file_md5 and digest_md5.hexdigest() != file_md5:
            raise ValueError("file_md5 does not match the staged file")
        if file_sha256 and digest_sha256.hexdigest() != file_sha256:
            raise ValueError("file_sha256 does not match the staged file")


def _outbound_file_idempotency_material(
    *,
    file_path: str,
    file_name: str,
    file_size: int | None,
    file_md5: str,
    file_sha256: str,
) -> str:
    return json.dumps(
        {
            "file_path": file_path,
            "file_name": file_name,
            "file_size": file_size,
            "file_md5": file_md5,
            "file_sha256": file_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _reply_idempotency_material(reply: dict[str, Any]) -> str:
    if str(reply.get("msg_type") or "text").strip().lower() == "file":
        return _outbound_file_idempotency_material(
            file_path=str(reply.get("file_path") or ""),
            file_name=str(reply.get("file_name") or ""),
            file_size=_normalize_outbound_file_size(reply.get("file_size")),
            file_md5=str(reply.get("file_md5") or ""),
            file_sha256=str(reply.get("file_sha256") or ""),
        )
    return str(reply.get("reply_text") or reply.get("image_url") or reply.get("image_path") or "")


def _normalize_reply_queue_connection_id(connection_id: str) -> str:
    clean_connection_id = str(connection_id or "").strip()[:64]
    if not clean_connection_id or clean_connection_id == LEGACY_WXBOT_CONNECTION_ID:
        return LEGACY_WXBOT_CONNECTION_ID
    return clean_connection_id


_WXBOT_CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def normalize_wxbot_event_connection_id(connection_id: str) -> str:
    """Return the canonical durable scope for wxbot event persistence."""

    clean_connection_id = str(connection_id or "").strip()
    if not clean_connection_id or clean_connection_id == LEGACY_WXBOT_CONNECTION_ID:
        return LEGACY_WXBOT_CONNECTION_ID
    if not _WXBOT_CONNECTION_ID_RE.fullmatch(clean_connection_id):
        raise ValueError("connection_id must be a stable identifier")
    return clean_connection_id


def _reply_queue_connection_scope(
    connection_id: str,
    *,
    column: str,
) -> tuple[str, dict[str, str]]:
    """Build the durable queue scope shared by every claim transition."""

    clean_connection_id = _normalize_reply_queue_connection_id(connection_id)
    if clean_connection_id == LEGACY_WXBOT_CONNECTION_ID:
        return (
            f"COALESCE({column}, '') IN ('', :legacy_connection_id)",
            {"legacy_connection_id": LEGACY_WXBOT_CONNECTION_ID},
        )
    return (
        f"{column} = :connection_id",
        {"connection_id": clean_connection_id},
    )


def _reservation_from_delivery(value: Any) -> SpeechReservation | None:
    delivery = _delivery_dict(value)
    ledger = delivery.get("speech_ledger")
    if not isinstance(ledger, dict):
        return None
    reservation_id = str(ledger.get("reservation_id") or "").strip()
    idempotency_key = str(ledger.get("idempotency_key") or "").strip()
    if not reservation_id or not idempotency_key:
        return None
    return SpeechReservation(
        allowed=True,
        idempotency_key=idempotency_key,
        output_kind=str(ledger.get("output_kind") or "ordinary"),
        speech_class=str(ledger.get("speech_class") or "soft"),
        reservation_id=reservation_id,
        replayed=bool(ledger.get("replayed")),
    )


_OBLIGATION_FILLER_RE = re.compile(
    r"^\s*(?:收到|好的|好|可以|可以的|没问题|当然|确实)[，,。.!！\s]+"
)


def _rewrite_obligation_duplicate(text_value: str) -> tuple[str, bool]:
    """Remove only a non-semantic lead-in; never trim answer content."""

    original = str(text_value or "")
    rewritten = _OBLIGATION_FILLER_RE.sub("", original, count=1).strip()
    if rewritten and rewritten != original.strip():
        return rewritten, True
    return original, False


def _source_message_text(source_message: dict[str, Any] | None) -> str:
    payload = source_message if isinstance(source_message, dict) else {}
    message = payload.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(payload.get("content") or payload.get("text") or "")


async def _exec(sql: str, params: dict | None = None) -> list[dict]:
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        if result.returns_rows:
            return [dict(row._mapping) for row in result.fetchall()]
        return []


def _normalize_global_policy(
    row: dict[str, Any] | None,
    tenant_id: str,
) -> dict[str, Any]:
    source = row or {}
    policy = {
        "tenant_id": str(source.get("tenant_id") or tenant_id),
        "private_reply_mode": str(source.get("private_reply_mode") or "all"),
        "group_reply_mode": str(source.get("group_reply_mode") or "off"),
        "group_reply_mention_sender": bool(source.get("group_reply_mention_sender")),
        "trigger_keywords_text": str(source.get("trigger_keywords_text") or ""),
        "version": max(0, int(source.get("version") or 0)),
        "updated_at": source.get("updated_at"),
    }
    policy["trigger_keywords"] = [
        line.strip() for line in policy["trigger_keywords_text"].splitlines() if line.strip()
    ]
    return policy


def _session_policy_document(
    row: dict[str, Any] | None,
    tenant_id: str,
    session_id: str,
    global_policy: dict[str, Any],
    *,
    settings: Any = None,
) -> dict[str, Any]:
    source = row or {}
    policy: dict[str, Any] = {
        "tenant_id": str(source.get("tenant_id") or tenant_id),
        "session_id": str(source.get("session_id") or session_id),
        "reply_mode": str(source.get("reply_mode") or "inherit"),
        "mention_sender_mode": str(source.get("mention_sender_mode") or "inherit"),
        "trigger_keywords_text": str(source.get("trigger_keywords_text") or ""),
        "reply_cooldown_seconds": source.get("reply_cooldown_seconds"),
        "coalesce_window_ms": source.get("coalesce_window_ms"),
        "adaptive_cooldown_enabled": source.get("adaptive_cooldown_enabled"),
        "participation_policy": normalize_group_participation_policy(
            source.get("participation_policy_json")
        ),
        "version": max(0, int(source.get("version") or 0)),
        "updated_at": source.get("updated_at"),
    }
    is_group = str(session_id or "").endswith("@chatroom")
    policy["default_mode"] = (
        global_policy["group_reply_mode"] if is_group else global_policy["private_reply_mode"]
    )
    policy["effective_mode"] = (
        policy["reply_mode"] if policy["reply_mode"] != "inherit" else policy["default_mode"]
    )
    policy["default_mention_sender"] = (
        bool(global_policy.get("group_reply_mention_sender")) if is_group else False
    )
    policy["effective_mention_sender"] = (
        bool(policy["default_mention_sender"])
        if policy["mention_sender_mode"] == "inherit"
        else policy["mention_sender_mode"] == "on"
    )
    policy["effective_reply_cooldown_seconds"] = (
        float(policy["reply_cooldown_seconds"])
        if policy["reply_cooldown_seconds"] is not None
        else float(getattr(settings, "wxbot_group_reply_cooldown_seconds", 1.0) or 0.0)
    )
    policy["effective_coalesce_window_ms"] = (
        int(policy["coalesce_window_ms"])
        if policy["coalesce_window_ms"] is not None
        else int(getattr(settings, "wxbot_group_reply_coalesce_window_ms", 250) or 0)
    )
    policy["effective_adaptive_cooldown_enabled"] = (
        bool(policy["adaptive_cooldown_enabled"])
        if policy["adaptive_cooldown_enabled"] is not None
        else bool(getattr(settings, "wxbot_group_reply_adaptive_cooldown_enabled", True))
    )
    effective_keywords_text = str(policy.get("trigger_keywords_text") or "").strip() or str(
        global_policy.get("trigger_keywords_text") or ""
    )
    policy["global_policy"] = global_policy
    policy["inherits_global_keywords"] = not bool(
        str(policy.get("trigger_keywords_text") or "").strip()
    )
    policy["effective_trigger_keywords_text"] = effective_keywords_text
    policy["trigger_keywords"] = [
        line.strip() for line in effective_keywords_text.splitlines() if line.strip()
    ]
    return policy


async def _global_policy_row(
    db: AsyncConnection | AsyncSession,
    tenant_id: str,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            f"SELECT {_GLOBAL_POLICY_COLUMNS} FROM plugin_wxbot_tenant_policy "
            "WHERE tenant_id = :tid"
            f"{suffix}"
        ),
        {"tid": tenant_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _session_policy_row(
    db: AsyncConnection | AsyncSession,
    tenant_id: str,
    session_id: str,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            f"SELECT {_SESSION_POLICY_COLUMNS} FROM plugin_wxbot_session_policy "
            "WHERE tenant_id = :tid AND session_id = :sid"
            f"{suffix}"
        ),
        {"tid": tenant_id, "sid": session_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _repeater_config_document(
    db: AsyncConnection | AsyncSession,
    tenant_id: str,
    session_id: str,
) -> dict[str, Any]:
    result = await db.execute(
        text(
            "SELECT tenant_id, session_id, enabled, cooldown_seconds, version, "
            "updated_at FROM plugin_repeater_config "
            "WHERE tenant_id = :tid AND session_id = :sid"
        ),
        {"tid": tenant_id, "sid": session_id},
    )
    row = result.mappings().first()
    if row is None:
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "enabled": False,
            "cooldown_seconds": 300,
            "version": 0,
            "updated_at": None,
        }
    item = dict(row)
    item["enabled"] = bool(item.get("enabled"))
    item["cooldown_seconds"] = max(
        1,
        int(item.get("cooldown_seconds") or 300),
    )
    item["version"] = max(1, int(item.get("version") or 1))
    return item


def reply_policy_composite_etag(aggregate: dict[str, Any]) -> str:
    versions = aggregate.get("versions")
    source = versions if isinstance(versions, dict) else {}
    return (
        '"reply-policy-'
        f"g{max(0, int(source.get('global') or 0))}-"
        f"s{max(0, int(source.get('session') or 0))}-"
        f"r{max(0, int(source.get('repeater') or 0))}-"
        f'a{max(0, int(source.get("aggregate") or 0))}"'
    )


def compose_reply_policy_aggregate(
    *,
    tenant_id: str,
    session_id: str,
    global_policy: dict[str, Any],
    session_policy: dict[str, Any],
    repeater_config: dict[str, Any],
    aggregate_state: dict[str, Any],
    effect_status: str,
) -> dict[str, Any]:
    versions = {
        "global": max(0, int(global_policy.get("version") or 0)),
        "session": max(0, int(session_policy.get("version") or 0)),
        "repeater": max(0, int(repeater_config.get("version") or 0)),
        "aggregate": max(0, int(aggregate_state.get("version") or 0)),
    }
    result = {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "global_policy": global_policy,
        "session_policy": session_policy,
        "repeater_config": repeater_config,
        "sdk_gate": {
            "group_require_at_me": bool(aggregate_state.get("sdk_group_require_at_me", True)),
            "status": str(effect_status or "not_requested"),
            "idempotency_key": str(aggregate_state.get("effect_idempotency_key") or ""),
        },
        "versions": versions,
    }
    result["etag"] = reply_policy_composite_etag(result)
    return result


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class WxbotStore(WxbotReportStoreMixin):
    def __init__(
        self,
        settings: Any,
        *,
        speech_ledger: GroupSpeechLedgerProtocol | None = None,
        style_guard: NaturalReplyStyleGuard | None = None,
    ) -> None:
        self.settings = settings
        # Real Settings always carries db_dsn. Lightweight unit-test settings
        # omit it and may inject the deterministic in-memory ledger explicitly.
        self._speech_ledger = speech_ledger or (
            GroupSpeechLedger() if str(getattr(settings, "db_dsn", "") or "").strip() else None
        )
        self._style_guard = style_guard or NaturalReplyStyleGuard()

    @property
    def speech_ledger(self) -> GroupSpeechLedgerProtocol | None:
        return self._speech_ledger

    async def _finalize_speech_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        succeeded: bool,
        reason: str,
    ) -> None:
        if self._speech_ledger is None:
            return
        for row in rows:
            delivery = _delivery_dict(row.get("delivery_json") or row.get("delivery"))
            reservation = _reservation_from_delivery(delivery)
            if reservation is None:
                continue
            if succeeded:
                await self._speech_ledger.commit(
                    reservation,
                    provider_message_id=str(row.get("sdk_outbound_id") or row.get("id") or ""),
                )
            else:
                await self._speech_ledger.release(
                    reservation,
                    reason=reason,
                )
            created_at = _queue_datetime(row.get("created_at"))
            delay = (
                max(0.0, (datetime.now(UTC) - created_at).total_seconds())
                if created_at is not None
                else None
            )
            observe_final_delivery(
                result="succeeded" if succeeded else "cancelled",
                stage=str(delivery.get("humanization_stage") or "legacy"),
                cohort=str(delivery.get("humanization_cohort") or "legacy"),
                speech_class=reservation.speech_class,
                actual_delay_seconds=delay,
            )

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="wxbot store")
        logger.info("wxbot.schema_verified")

    async def observe_admin_resource(
        self,
        tenant_id: str,
        resource_key: str,
        *,
        resource_kind: str,
        state_payload: Any,
    ) -> int:
        return await observe_admin_resource(
            self,
            tenant_id,
            resource_key,
            resource_kind=resource_kind,
            state_payload=state_payload,
        )

    async def claim_admin_mutation(
        self,
        tenant_id: str,
        *,
        operation: str,
        resource_key: str,
        idempotency_key: str,
        request_payload: Any,
        trace_id: str = "",
        expected_version: int | None = None,
        desired_state: Any = None,
        recovery_response: Any = None,
    ) -> WxbotAdminMutationClaim:
        return await claim_admin_mutation(
            self,
            tenant_id,
            operation=operation,
            resource_key=resource_key,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            trace_id=trace_id,
            expected_version=expected_version,
            desired_state=desired_state,
            recovery_response=recovery_response,
        )

    async def complete_admin_mutation(
        self,
        mutation_id: str,
        *,
        response: Any,
        response_status_code: int = 200,
        state_payload: Any = None,
    ) -> WxbotAdminMutationResult:
        return await complete_admin_mutation(
            self,
            mutation_id,
            response=response,
            response_status_code=response_status_code,
            state_payload=state_payload,
        )

    async def fail_admin_mutation(
        self,
        mutation_id: str,
        *,
        status_code: int,
        response: Any,
        error_code: str,
        indeterminate: bool = False,
    ) -> None:
        await fail_admin_mutation(
            self,
            mutation_id,
            status_code=status_code,
            response=response,
            error_code=error_code,
            indeterminate=indeterminate,
        )

    async def quote_targets_bot(
        self,
        tenant_id: str,
        session_id: str,
        quote: dict[str, Any],
    ) -> bool:
        """Resolve a quoted message against durable bot-authored observations."""
        candidates: list[dict[str, Any]] = [quote]
        for key in ("message", "quoted_message", "quoted", "raw"):
            nested = quote.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
        references: list[str] = []
        for candidate in candidates:
            for key in (
                "refer_msg_svr_id",
                "refer_message_id",
                "refer_id",
                "msg_svr_id",
                "message_id",
                "id",
            ):
                value = str(candidate.get(key) or "").strip()
                if value and value not in references:
                    references.append(value)
        if not references:
            return False
        references = references[:8]
        placeholders = ", ".join(f":reference_{index}" for index in range(len(references)))
        params: dict[str, Any] = {
            "tid": tenant_id,
            "sid": session_id,
            **{f"reference_{index}": value for index, value in enumerate(references)},
        }
        rows = await _exec(
            "SELECT id FROM plugin_wxbot_group_observations "
            "WHERE tenant_id = :tid AND session_id = :sid AND is_self_sent = TRUE "
            f"AND (message_id IN ({placeholders}) OR "
            f"COALESCE(metadata_json::jsonb ->> 'msg_svr_id', '') IN ({placeholders})) "
            "ORDER BY id DESC LIMIT 1",
            params,
        )
        return bool(rows)

    async def record_interactive_inbound(
        self,
        *,
        tenant_id: str,
        session_id: str,
        message_id: str,
    ) -> None:
        """Record the newest message that is expected to produce a reply."""
        if not session_id or not message_id:
            return
        burst_window = max(
            0.1,
            float(
                getattr(
                    self.settings,
                    "wxbot_group_reply_burst_window_seconds",
                    10.0,
                )
                or 10.0
            ),
        )
        await _exec(
            "INSERT INTO plugin_wxbot_interaction_cursor "
            "(tenant_id, session_id, latest_message_id, latest_received_at, updated_at) "
            "VALUES (:tenant_id, :session_id, :message_id, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, session_id) DO UPDATE SET "
            "burst_count = CASE WHEN plugin_wxbot_interaction_cursor.latest_received_at "
            ">= NOW() - (:burst_window * INTERVAL '1 second') "
            "THEN plugin_wxbot_interaction_cursor.burst_count + 1 ELSE 1 END, "
            "burst_started_at = CASE WHEN plugin_wxbot_interaction_cursor.latest_received_at "
            ">= NOW() - (:burst_window * INTERVAL '1 second') "
            "THEN plugin_wxbot_interaction_cursor.burst_started_at ELSE NOW() END, "
            "latest_message_id = EXCLUDED.latest_message_id, "
            "latest_received_at = EXCLUDED.latest_received_at, updated_at = NOW()",
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "message_id": message_id,
                "burst_window": burst_window,
            },
        )

    async def claim_interactive_reply(
        self,
        *,
        tenant_id: str,
        session_id: str,
        message_id: str,
        cooldown_seconds: float = 0.0,
        adaptive_cooldown: bool = False,
        adaptive_max_seconds: float = 8.0,
    ) -> bool:
        """Atomically reject stale replies and optional high-volume group spam."""
        if not session_id or not message_id:
            return True
        await _exec(
            "INSERT INTO plugin_wxbot_interaction_cursor "
            "(tenant_id, session_id, latest_message_id, latest_received_at, updated_at) "
            "VALUES (:tenant_id, :session_id, :message_id, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, session_id) DO NOTHING",
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "message_id": message_id,
            },
        )
        cooldown_expression = ":cooldown"
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "message_id": message_id,
            "cooldown": max(0.0, float(cooldown_seconds or 0.0)),
        }
        if adaptive_cooldown:
            cooldown_expression = (
                "LEAST(:adaptive_max, :cooldown + GREATEST(0, burst_count - 1) * 0.5)"
            )
            params["adaptive_max"] = max(0.0, float(adaptive_max_seconds or 0.0))
        rows = await _exec(
            "UPDATE plugin_wxbot_interaction_cursor SET "
            "last_replied_message_id = :message_id, last_reply_at = NOW(), updated_at = NOW() "
            "WHERE tenant_id = :tenant_id AND session_id = :session_id "
            "AND latest_message_id = :message_id "
            "AND last_replied_message_id <> :message_id "
            "AND (:cooldown <= 0 OR last_reply_at IS NULL "
            f"OR last_reply_at <= NOW() - (({cooldown_expression}) * INTERVAL '1 second')) "
            "RETURNING latest_message_id",
            params,
        )
        return bool(rows)

    # ── Reply queue ──

    async def enqueue_reply(
        self,
        tenant_id: str,
        session_id: str,
        session_name: str,
        sender_name: str,
        reply_text: str,
        trace_id: str = "",
        *,
        msg_type: str = "text",
        image_path: str = "",
        image_url: str = "",
        file_path: str = "",
        file_name: str = "",
        file_size: int | None = None,
        file_md5: str = "",
        file_sha256: str = "",
        mention_sender: bool = False,
        sender_wxid: str = "",
        reply_to_msg_svr_id: str = "",
        session_kind: str = "",
        source_message: dict[str, Any] | None = None,
        delivery: dict[str, Any] | None = None,
        command_id: str = "",
    ) -> int:
        msg_type = str(msg_type or "text").strip().lower() or "text"
        file_path = str(file_path or "").strip()
        file_name = str(file_name or "").strip()
        file_md5 = _normalize_outbound_file_digest(file_md5, algorithm="md5")
        file_sha256 = _normalize_outbound_file_digest(file_sha256, algorithm="sha256")
        file_size = _normalize_outbound_file_size(file_size)
        if msg_type == "file":
            if image_url:
                raise ValueError("file_url is not supported; use an SDK-local file_path")
            if not file_path:
                raise ValueError("file_path is required for file messages")
            if not _is_absolute_outbound_file_path(file_path):
                raise ValueError("file_path must be absolute on the SDK machine")
            if file_name and (
                "\x00" in file_name
                or "/" in file_name
                or "\\" in file_name
                or file_name in {".", ".."}
            ):
                raise ValueError("file_name must be a basename")
            _validate_configured_outbound_file(
                self.settings,
                file_path=file_path,
                file_size=file_size,
                file_md5=file_md5,
                file_sha256=file_sha256,
            )
        file_idempotency_material = (
            _outbound_file_idempotency_material(
                file_path=file_path,
                file_name=file_name,
                file_size=file_size,
                file_md5=file_md5,
                file_sha256=file_sha256,
            )
            if msg_type == "file"
            else ""
        )
        initial_delivery = dict(delivery or {})
        source_payload = source_message if isinstance(source_message, dict) else {}
        clean_connection_id = _normalize_reply_queue_connection_id(
            str(initial_delivery.get("connection_id") or "").strip()
            or str(source_payload.get("connection_id") or "").strip()
        )
        initial_delivery["connection_id"] = clean_connection_id
        participation_status = str(initial_delivery.get("participation_status") or "").strip()[:24]
        source_message_id = str(initial_delivery.get("source_message_id") or "").strip()[:128]
        not_before = _queue_datetime(initial_delivery.get("not_before"))
        expires_at = _queue_datetime(initial_delivery.get("expires_at"))
        clean_command_id = (
            str(command_id or "")
            or str(initial_delivery.get("command_id") or "")
            or str(initial_delivery.get("idempotency_key") or "")
        ).strip()[:256]
        reservation: SpeechReservation | None = None
        is_group = (
            str(session_id or "").endswith("@chatroom")
            or str(session_kind or initial_delivery.get("session_kind") or "").lower() == "group"
        )
        if is_group and self._speech_ledger is not None:
            output_kind = _speech_output_kind(initial_delivery)
            speech_class = _speech_class(initial_delivery, output_kind)
            initial_delivery["speech_class"] = speech_class
            speech_budget_enabled = bool(
                output_kind != "repeater" and initial_delivery.get("speech_budget_enabled", True)
            )
            initial_delivery["speech_budget_enabled"] = speech_budget_enabled
            duplicate_guard_enabled = bool(initial_delivery.get("duplicate_guard_enabled", True))
            deferred_candidate = participation_status == "defer" or bool(
                initial_delivery.get("deferred_candidate")
            )
            clean_command_id = derive_speech_idempotency_key(
                tenant_id=tenant_id,
                session_id=session_id,
                command_id=clean_command_id,
                trace_id=trace_id,
                source_message_id=source_message_id,
                output_kind=output_kind,
                text=file_idempotency_material or reply_text or image_url or image_path,
            )
            style_result = None
            if (
                msg_type == "text"
                and output_kind in {"ordinary", "proactive"}
                and bool(initial_delivery.get("style_eligible"))
            ):
                history = await self._speech_ledger.recent_style_history(
                    tenant_id,
                    session_id,
                )
                style_result = self._style_guard.apply(
                    reply_text,
                    deterministic_key=clean_command_id,
                    eligible=True,
                    source_text=_source_message_text(source_message),
                    explicitly_detailed=(
                        bool(initial_delivery["explicitly_detailed"])
                        if "explicitly_detailed" in initial_delivery
                        else None
                    ),
                    history=history,
                    voice_profile=(
                        initial_delivery.get("voice_profile")
                        if isinstance(initial_delivery.get("voice_profile"), dict)
                        else None
                    ),
                )
                reply_text = style_result.text
                initial_delivery["style_guard"] = {
                    "mode": style_result.mode,
                    "transformed": style_result.transformed,
                    "reason_codes": list(style_result.reason_codes),
                    "emoji": style_result.emoji,
                    "catchphrase": style_result.catchphrase,
                }
            if msg_type == "text" and output_kind == "ordinary" and duplicate_guard_enabled:
                near_duplicate = await self._speech_ledger.has_near_duplicate(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    text=reply_text,
                    idempotency_key=clean_command_id,
                )
                if near_duplicate and speech_class in {"soft", "scheduled"}:
                    observe_duplicate_guard(
                        speech_class=speech_class,
                        action="cancelled",
                    )
                    raise GroupSpeechBudgetExceeded(
                        "near_duplicate_24h",
                        output_kind=output_kind,
                        idempotency_key=clean_command_id,
                    )
                if near_duplicate and speech_class == "obligation":
                    reply_text, rewritten = _rewrite_obligation_duplicate(reply_text)
                    action = "rewritten" if rewritten else "preserved"
                    observe_duplicate_guard(
                        speech_class=speech_class,
                        action=action,
                    )
                    initial_delivery["near_duplicate_guard"] = {
                        "matched": True,
                        "action": action,
                        "complete_answer_preserved": True,
                    }
                elif not near_duplicate:
                    observe_duplicate_guard(
                        speech_class=speech_class,
                        action="allowed",
                    )
            if speech_budget_enabled and not deferred_candidate:
                reservation = await reserve_or_raise(
                    self._speech_ledger,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    idempotency_key=clean_command_id,
                    output_kind=output_kind,
                    speech_class=speech_class,
                    text=file_idempotency_material or reply_text or image_url or image_path,
                    emoji=style_result.emoji if style_result is not None else "",
                    catchphrase=(style_result.catchphrase if style_result is not None else ""),
                    metadata={
                        "msg_type": msg_type,
                        "trace_id": trace_id,
                        "source_message_id": source_message_id,
                    },
                )
                initial_delivery["speech_ledger"] = {
                    "reservation_id": reservation.reservation_id,
                    "output_kind": output_kind,
                    "speech_class": speech_class,
                    "idempotency_key": clean_command_id,
                    "replayed": reservation.replayed,
                }
        if clean_command_id:
            initial_delivery["command_id"] = clean_command_id
            initial_delivery["idempotency_key"] = clean_command_id
        try:
            rows = await _exec(
                "INSERT INTO plugin_wxbot_reply_queue "
                "(tenant_id, connection_id, session_id, session_name, sender_name, sender_wxid, "
                "mention_sender, reply_to_msg_svr_id, session_kind, reply_text, "
                "msg_type, image_path, image_url, file_path, file_name, file_size, "
                "file_md5, file_sha256, source_message_json, delivery_json, "
                "trace_id, command_id, participation_status, source_message_id, "
                "not_before, expires_at) VALUES "
                "(:tid, :connection_id, :sid, :sname, :sender, :sender_wxid, :mention_sender, "
                ":reply_to_msg_svr_id, :session_kind, :reply, :msg_type, :image_path, "
                ":image_url, :file_path, :file_name, :file_size, :file_md5, :file_sha256, "
                ":source_message_json, :delivery_json, :trace, :command_id, "
                ":participation_status, :source_message_id, :not_before, :expires_at) "
                "ON CONFLICT (tenant_id, connection_id, command_id) "
                "WHERE command_id <> '' DO NOTHING "
                "RETURNING id",
                {
                    "tid": tenant_id,
                    "connection_id": clean_connection_id,
                    "sid": session_id,
                    "sname": session_name,
                    "sender": sender_name,
                    "sender_wxid": sender_wxid,
                    "mention_sender": bool(mention_sender),
                    "reply_to_msg_svr_id": reply_to_msg_svr_id,
                    "session_kind": session_kind,
                    "reply": reply_text,
                    "msg_type": msg_type,
                    "image_path": image_path,
                    "image_url": image_url,
                    "file_path": file_path,
                    "file_name": file_name,
                    "file_size": file_size,
                    "file_md5": file_md5,
                    "file_sha256": file_sha256,
                    "source_message_json": json.dumps(
                        source_message or {},
                        ensure_ascii=False,
                    ),
                    "delivery_json": json.dumps(
                        initial_delivery,
                        ensure_ascii=False,
                    ),
                    "trace": trace_id,
                    "command_id": clean_command_id,
                    "participation_status": participation_status,
                    "source_message_id": source_message_id,
                    "not_before": not_before,
                    "expires_at": expires_at,
                },
            )
        except Exception:
            if reservation is not None and self._speech_ledger is not None:
                await self._speech_ledger.release(
                    reservation,
                    reason="reply_queue_insert_failed",
                )
            raise
        if not rows:
            existing = await _exec(
                "SELECT id FROM plugin_wxbot_reply_queue "
                "WHERE tenant_id = :tid AND connection_id = :connection_id "
                "AND command_id = :command_id LIMIT 1",
                {
                    "tid": tenant_id,
                    "connection_id": clean_connection_id,
                    "command_id": clean_command_id,
                },
            )
            if existing:
                return int(existing[0]["id"])
            if reservation is not None and self._speech_ledger is not None:
                await self._speech_ledger.release(
                    reservation,
                    reason="reply_queue_conflict_unresolved",
                )
            raise RuntimeError("wxbot reply enqueue conflict could not be resolved")
        reply_id = int(rows[0]["id"])
        effective_command_id = clean_command_id or f"wxbot-reply:{reply_id}"
        normalized_delivery = {
            **initial_delivery,
            "command_id": effective_command_id,
            "idempotency_key": effective_command_id,
            "reply_queue_id": reply_id,
            "trace_id": trace_id,
        }
        await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            "SET command_id = :command_id, delivery_json = :delivery_json "
            "WHERE id = :id AND tenant_id = :tid",
            {
                "id": reply_id,
                "tid": tenant_id,
                "command_id": effective_command_id,
                "delivery_json": json.dumps(normalized_delivery, ensure_ascii=False),
            },
        )
        return reply_id

    async def prepare_claimed_reply_speech(
        self,
        reply: dict[str, Any],
        *,
        tenant_id: str,
        connection_id: str = "",
        claim_token: str,
    ) -> bool:
        """Revalidate the shared speech reservation immediately before send.

        Deferred candidates intentionally enter the durable queue without a
        long-lived reservation.  This method acquires their slot when they are
        actually due.  It also refreshes an expired retry reservation using the
        same idempotency key, so no SDK send can bypass the shared ledger.
        """

        if self._speech_ledger is None:
            return True
        session_id = str(reply.get("session_id") or "").strip()
        session_kind = str(reply.get("session_kind") or "").strip().lower()
        if not session_id.endswith("@chatroom") and session_kind != "group":
            return True
        delivery = _delivery_dict(reply.get("delivery"))
        output_kind = _speech_output_kind(delivery)
        if output_kind == "repeater" or not bool(delivery.get("speech_budget_enabled", True)):
            return True
        clean_token = str(claim_token or "").strip()[:64]
        if not clean_token:
            return False

        speech_class = _speech_class(delivery, output_kind)
        command_id = derive_speech_idempotency_key(
            tenant_id=tenant_id,
            session_id=session_id,
            command_id=str(
                reply.get("command_id")
                or delivery.get("command_id")
                or delivery.get("idempotency_key")
                or ""
            ),
            trace_id=str(reply.get("trace_id") or delivery.get("trace_id") or ""),
            source_message_id=str(reply.get("source_message_id") or ""),
            output_kind=output_kind,
            text=_reply_idempotency_material(reply),
        )
        reply_text = str(reply.get("reply_text") or "")
        if (
            str(reply.get("msg_type") or "text") == "text"
            and output_kind == "ordinary"
            and bool(delivery.get("duplicate_guard_enabled", True))
        ):
            near_duplicate = await self._speech_ledger.has_near_duplicate(
                tenant_id=tenant_id,
                session_id=session_id,
                text=reply_text,
                idempotency_key=command_id,
            )
            if near_duplicate and speech_class in {"soft", "scheduled"}:
                observe_duplicate_guard(
                    speech_class=speech_class,
                    action="cancelled",
                )
                raise GroupSpeechBudgetExceeded(
                    "near_duplicate_24h",
                    output_kind=output_kind,
                    idempotency_key=command_id,
                )
            if near_duplicate and speech_class == "obligation":
                reply_text, rewritten = _rewrite_obligation_duplicate(reply_text)
                observe_duplicate_guard(
                    speech_class=speech_class,
                    action="rewritten" if rewritten else "preserved",
                )

        style_metadata = _delivery_dict(delivery.get("style_guard"))
        reservation = await reserve_or_raise(
            self._speech_ledger,
            tenant_id=tenant_id,
            session_id=session_id,
            idempotency_key=command_id,
            output_kind=output_kind,
            speech_class=speech_class,
            text=(
                _reply_idempotency_material(reply)
                if str(reply.get("msg_type") or "text").strip().lower() == "file"
                else reply_text or _reply_idempotency_material(reply)
            ),
            emoji=str(style_metadata.get("emoji") or ""),
            catchphrase=str(style_metadata.get("catchphrase") or ""),
            metadata={
                "msg_type": str(reply.get("msg_type") or "text"),
                "trace_id": str(reply.get("trace_id") or ""),
                "source_message_id": str(reply.get("source_message_id") or ""),
            },
        )
        delivery["speech_class"] = speech_class
        delivery["speech_ledger"] = {
            "reservation_id": reservation.reservation_id,
            "output_kind": output_kind,
            "speech_class": speech_class,
            "idempotency_key": command_id,
            "replayed": reservation.replayed,
        }
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue SET reply_text = :reply_text, "
            "delivery_json = :delivery_json WHERE tenant_id = :tenant_id "
            f"AND {connection_scope} "
            "AND id = :id AND status = 'sending' AND claim_token = :claim_token "
            "AND claim_until > NOW() RETURNING id",
            {
                "reply_text": reply_text,
                "delivery_json": json.dumps(delivery, ensure_ascii=False),
                "tenant_id": str(tenant_id or ""),
                "id": int(reply["id"]),
                "claim_token": clean_token,
                **scope_params,
            },
        )
        if not rows:
            await self._speech_ledger.release(
                reservation,
                reason="reply_claim_lost_before_send",
            )
            return False
        reply["reply_text"] = reply_text
        reply["delivery"] = delivery
        reply["command_id"] = command_id
        return True

    async def claim_pending_reply(
        self,
        tenant_id: str,
        *,
        connection_id: str = "",
        claim_owner: str,
        lease_seconds: float = 45.0,
        max_attempts: int = 3,
    ) -> dict[str, Any] | None:
        """Lease one due reply without reserving work the sender cannot start.

        ``attempt_count`` is incremented at claim time so a process crash still
        consumes an attempt.  Every state transition after this method must be
        fenced by the returned ``claim_token``.
        """
        clean_owner = str(claim_owner or "").strip()[:128]
        if not clean_owner:
            raise ValueError("claim_owner is required")
        lease = max(5.0, float(lease_seconds or 45.0))
        attempts = max(1, int(max_attempts or 3))
        expired_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="q.connection_id",
        )
        exhausted_scope, _ = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        picked_scope, _ = _reply_queue_connection_scope(
            connection_id,
            column="pending.connection_id",
        )

        expired_rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue AS q "
            "SET status = 'cancelled', claim_owner = '', claim_token = '', "
            "claim_until = NULL, error = CASE "
            "  WHEN q.expires_at IS NOT NULL AND q.expires_at <= NOW() "
            "    THEN 'reply_expired' "
            "  ELSE 'reply_cancelled_before_claim' END "
            "WHERE q.tenant_id = :tid "
            f"AND {expired_scope} AND ("
            "  q.status = 'pending' OR (q.status = 'sending' "
            "    AND (q.claim_until IS NULL OR q.claim_until <= NOW()))"
            ") AND ("
            "  (q.expires_at IS NOT NULL AND q.expires_at <= NOW())"
            ") RETURNING q.id, q.connection_id, q.delivery_json, "
            "q.created_at, q.sdk_outbound_id",
            {"tid": tenant_id, **scope_params},
        )
        await self._finalize_speech_rows(
            expired_rows,
            succeeded=False,
            reason="reply_expired",
        )
        exhausted_rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            "SET status = 'failed', error = 'max_attempts_exhausted', "
            "claim_owner = '', claim_token = '', claim_until = NULL "
            "WHERE tenant_id = :tid "
            f"AND {exhausted_scope} "
            "AND COALESCE(attempt_count, 0) >= :max_attempts AND ("
            "status = 'pending' OR (status = 'sending' "
            "AND (claim_until IS NULL OR claim_until <= NOW()))) "
            "RETURNING id, connection_id, delivery_json, created_at, sdk_outbound_id",
            {"tid": tenant_id, "max_attempts": attempts, **scope_params},
        )
        await self._finalize_speech_rows(
            exhausted_rows,
            succeeded=False,
            reason="max_attempts_exhausted",
        )
        claim_token = uuid4().hex
        rows = await _exec(
            "WITH picked AS ("
            "  SELECT pending.id FROM plugin_wxbot_reply_queue AS pending "
            "  WHERE pending.tenant_id = :tid "
            f"  AND {picked_scope} AND ("
            "    pending.status = 'pending' OR (pending.status = 'sending' "
            "      AND (pending.claim_until IS NULL OR pending.claim_until <= NOW()))"
            "  ) "
            "  AND COALESCE(pending.attempt_count, 0) < :max_attempts "
            "  AND (pending.not_before IS NULL OR pending.not_before <= NOW()) "
            "  AND (pending.expires_at IS NULL OR pending.expires_at > NOW()) "
            "  ORDER BY pending.created_at, pending.id LIMIT 1 "
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE plugin_wxbot_reply_queue AS q "
            "SET status = 'sending', claim_owner = :claim_owner, "
            "claim_token = :claim_token, "
            "claim_until = NOW() + (:lease_seconds * INTERVAL '1 second'), "
            "attempt_count = COALESCE(q.attempt_count, 0) + 1 "
            "FROM picked WHERE q.tenant_id = :tid AND q.id = picked.id "
            "RETURNING q.id, q.tenant_id, q.connection_id, q.session_id, q.session_name, "
            "q.sender_name, q.sender_wxid, "
            "q.mention_sender, q.reply_to_msg_svr_id, q.session_kind, q.reply_text, q.msg_type, "
            "q.image_path, q.image_url, q.file_path, q.file_name, q.file_size, "
            "q.file_md5, q.file_sha256, q.source_message_json, q.delivery_json, q.command_id, "
            "q.trace_id, q.participation_status, q.source_message_id, q.not_before, "
            "q.expires_at, q.attempt_count, q.claim_owner, q.claim_token, "
            "q.claim_until, q.created_at",
            {
                "tid": tenant_id,
                "claim_owner": clean_owner,
                "claim_token": claim_token,
                "lease_seconds": lease,
                "max_attempts": attempts,
                **scope_params,
            },
        )
        if not rows:
            return None
        return self._deserialize_reply_queue_row(rows[0])

    async def cancel_claimed_reply(
        self,
        reply_id: int,
        *,
        tenant_id: str,
        connection_id: str = "",
        claim_token: str,
        reason: str,
    ) -> bool:
        """Cancel a leased reply while fencing out an expired sender."""

        clean_token = str(claim_token or "").strip()[:64]
        if not clean_token:
            return False
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            "SET status = 'cancelled', error = :reason, "
            "claim_owner = '', claim_token = '', claim_until = NULL "
            "WHERE tenant_id = :tenant_id AND id = :id AND status = 'sending' "
            f"AND {connection_scope} "
            "AND claim_token = :claim_token AND claim_until > NOW() "
            "RETURNING id, delivery_json, created_at, sdk_outbound_id",
            {
                "id": int(reply_id),
                "tenant_id": str(tenant_id or ""),
                "claim_token": clean_token,
                "reason": str(reason or "send_time_revalidation_cancelled")[:500],
                **scope_params,
            },
        )
        await self._finalize_speech_rows(
            rows,
            succeeded=False,
            reason=str(reason or "send_time_revalidation_cancelled")[:64],
        )
        return bool(rows)

    async def reschedule_claimed_reply(
        self,
        reply_id: int,
        *,
        tenant_id: str,
        connection_id: str = "",
        claim_token: str,
        not_before: datetime,
        expires_at: datetime | None,
        reason: str,
    ) -> bool:
        """Durably return a temporary DEFER decision to the pending queue."""

        clean_token = str(claim_token or "").strip()[:64]
        if not clean_token:
            return False
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue SET status = 'pending', "
            "not_before = :not_before, expires_at = :expires_at, error = :reason, "
            "attempt_count = GREATEST(COALESCE(attempt_count, 1) - 1, 0), "
            "claim_owner = '', claim_token = '', claim_until = NULL "
            "WHERE tenant_id = :tenant_id AND id = :id AND status = 'sending' "
            f"AND {connection_scope} "
            "AND claim_token = :claim_token AND claim_until > NOW() RETURNING id",
            {
                "id": int(reply_id),
                "tenant_id": str(tenant_id or ""),
                "claim_token": clean_token,
                "not_before": _queue_datetime(not_before),
                "expires_at": _queue_datetime(expires_at),
                "reason": str(reason or "send_time_revalidation_deferred")[:500],
                **scope_params,
            },
        )
        return bool(rows)

    async def get_group_reply_revalidation(
        self,
        *,
        tenant_id: str,
        session_id: str,
        source_message_id: str,
        participation_status: str,
    ) -> dict[str, Any]:
        """Load bounded observations and classify volatile send conditions."""

        clean_tenant = str(tenant_id or "").strip()
        clean_session = str(session_id or "").strip()
        clean_message = str(source_message_id or "").strip()
        if not clean_tenant or not clean_session.endswith("@chatroom") or not clean_message:
            return {
                "context_available": False,
                "reason_codes": ["revalidation_scope_invalid"],
            }
        source_rows = await _exec(
            "SELECT id, tenant_id, session_id, message_id, sender_wxid, sender_name, "
            "content, mentioned_me, bot_addressed, is_self_sent, occurred_ts, metadata_json "
            "FROM plugin_wxbot_group_observations "
            "WHERE tenant_id = :tid AND session_id = :sid AND message_id = :mid "
            "ORDER BY id DESC LIMIT 1",
            {"tid": clean_tenant, "sid": clean_session, "mid": clean_message},
        )
        if not source_rows:
            return {
                "context_available": False,
                "reason_codes": ["source_observation_missing"],
            }
        source = self._hydrate_group_observation(source_rows[0])
        newer_rows = await _exec(
            "SELECT id, tenant_id, session_id, message_id, sender_wxid, sender_name, "
            "content, mentioned_me, bot_addressed, is_self_sent, occurred_ts, metadata_json "
            "FROM plugin_wxbot_group_observations "
            "WHERE tenant_id = :tid AND session_id = :sid AND id > :source_id "
            "ORDER BY id DESC LIMIT 80",
            {
                "tid": clean_tenant,
                "sid": clean_session,
                "source_id": int(source.get("id") or 0),
            },
        )
        result = evaluate_group_reply_revalidation(
            source=source,
            newer_observations=(self._hydrate_group_observation(row) for row in newer_rows),
            participation_status=participation_status,
        )
        return {
            "context_available": result.context_available,
            "valid_member_answer_exists": result.valid_member_answer_exists,
            "topic_changed": result.topic_changed,
            "superseded_by_newer_message": result.superseded_by_newer_message,
            "source_is_self_sent": result.source_is_self_sent,
            "newer_human_messages": result.newer_human_messages,
            "reason_codes": list(result.reason_codes),
        }

    async def update_reply_command(
        self,
        reply_id: int,
        *,
        tenant_id: str,
        connection_id: str = "",
        claim_token: str,
        command_id: str,
        delivery: dict[str, Any] | None = None,
    ) -> bool:
        clean_command_id = str(command_id or "").strip()[:256]
        clean_token = str(claim_token or "").strip()[:64]
        if not clean_command_id or not clean_token:
            return False
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            "SET command_id = :command_id, delivery_json = COALESCE(:delivery_json, delivery_json) "
            "WHERE tenant_id = :tenant_id AND id = :id AND status = 'sending' "
            f"AND {connection_scope} "
            "AND claim_token = :claim_token AND claim_until > NOW() RETURNING id",
            {
                "id": reply_id,
                "tenant_id": tenant_id,
                "claim_token": clean_token,
                "command_id": clean_command_id,
                "delivery_json": (
                    json.dumps(delivery, ensure_ascii=False) if delivery is not None else None
                ),
                **scope_params,
            },
        )
        return bool(rows)

    async def mark_reply_queued(
        self,
        reply_id: int,
        *,
        tenant_id: str,
        connection_id: str = "",
        claim_token: str,
        sdk_outbound_id: int | None = None,
    ) -> bool:
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            "SET status = 'queued', error = '', "
            "sdk_outbound_id = COALESCE(:sdk_outbound_id, sdk_outbound_id), "
            "queued_at = COALESCE(queued_at, NOW()), claim_owner = '', "
            "claim_token = '', claim_until = NULL "
            "WHERE tenant_id = :tenant_id AND id = :id AND status = 'sending' "
            f"AND {connection_scope} "
            "AND claim_token = :claim_token AND claim_until > NOW() RETURNING id",
            {
                "id": reply_id,
                "tenant_id": tenant_id,
                "claim_token": str(claim_token or "")[:64],
                "sdk_outbound_id": sdk_outbound_id,
                **scope_params,
            },
        )
        return bool(rows)

    async def mark_reply_sent(
        self,
        reply_id: int,
        *,
        tenant_id: str,
        connection_id: str = "",
        claim_token: str,
    ) -> bool:
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            "SET status = 'sent', error = '', sent_at = NOW(), claim_owner = '', "
            "claim_token = '', claim_until = NULL "
            "WHERE tenant_id = :tenant_id AND id = :id AND status = 'sending' "
            f"AND {connection_scope} "
            "AND claim_token = :claim_token AND claim_until > NOW() "
            "RETURNING id, delivery_json, created_at, sdk_outbound_id",
            {
                "id": reply_id,
                "tenant_id": tenant_id,
                "claim_token": str(claim_token or "")[:64],
                **scope_params,
            },
        )
        await self._finalize_speech_rows(
            rows,
            succeeded=True,
            reason="legacy_delivery_succeeded",
        )
        return bool(rows)

    async def mark_reply_delivery_succeeded(
        self,
        command_id: str,
        *,
        tenant_id: str,
        connection_id: str = "",
        sdk_outbound_id: int | None = None,
    ) -> list[dict]:
        clean_command_id = str(command_id or "").strip()[:256]
        if not clean_command_id:
            return []
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            "SET status = 'sent', error = '', "
            "sdk_outbound_id = COALESCE(:sdk_outbound_id, sdk_outbound_id), "
            "sent_at = NOW(), claim_owner = '', claim_token = '', claim_until = NULL "
            "WHERE tenant_id = :tenant_id AND command_id = :command_id "
            f"AND {connection_scope} "
            "AND status IN ('sending', 'queued', 'sent') "
            "RETURNING id, tenant_id, connection_id, session_id, status, delivery_json, "
            "created_at, sdk_outbound_id",
            {
                "tenant_id": tenant_id,
                "command_id": clean_command_id,
                "sdk_outbound_id": sdk_outbound_id,
                **scope_params,
            },
        )
        await self._finalize_speech_rows(
            rows,
            succeeded=True,
            reason="delivery_succeeded",
        )
        return rows

    async def mark_reply_delivery_failed(
        self,
        command_id: str,
        *,
        tenant_id: str,
        connection_id: str = "",
        error: str = "",
        terminal: bool = False,
        sdk_outbound_id: int | None = None,
    ) -> list[dict]:
        clean_command_id = str(command_id or "").strip()[:256]
        if not clean_command_id:
            return []
        status_expr = "'failed'" if terminal else "status"
        clear_claim = ", claim_owner = '', claim_token = '', claim_until = NULL" if terminal else ""
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            f"SET status = {status_expr}, error = :error, "
            "sdk_outbound_id = COALESCE(:sdk_outbound_id, sdk_outbound_id) "
            f"{clear_claim} "
            "WHERE tenant_id = :tenant_id AND command_id = :command_id "
            f"AND {connection_scope} "
            "AND status IN ('sending', 'queued') "
            "RETURNING id, tenant_id, connection_id, session_id, status, delivery_json, "
            "created_at, sdk_outbound_id",
            {
                "tenant_id": tenant_id,
                "command_id": clean_command_id,
                "error": str(error or "")[:500],
                "sdk_outbound_id": sdk_outbound_id,
                **scope_params,
            },
        )
        if terminal:
            await self._finalize_speech_rows(
                rows,
                succeeded=False,
                reason=str(error or "delivery_failed")[:64],
            )
        return rows

    async def mark_reply_failed(
        self,
        reply_id: int,
        error: str,
        *,
        tenant_id: str,
        connection_id: str = "",
        claim_token: str,
        max_attempts: int = 3,
    ) -> str:
        clean_token = str(claim_token or "").strip()[:64]
        if not clean_token:
            return "stale_claim"
        connection_scope, scope_params = _reply_queue_connection_scope(
            connection_id,
            column="connection_id",
        )
        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue SET "
            "status = CASE WHEN COALESCE(attempt_count, 0) >= :max_attempts "
            "THEN 'failed' ELSE 'pending' END, error = :err, "
            "claim_owner = '', claim_token = '', claim_until = NULL "
            "WHERE tenant_id = :tenant_id AND id = :id AND status = 'sending' "
            f"AND {connection_scope} "
            "AND claim_token = :claim_token AND claim_until > NOW() "
            "RETURNING status, id, delivery_json, created_at, sdk_outbound_id",
            {
                "id": reply_id,
                "tenant_id": tenant_id,
                "claim_token": clean_token,
                "err": str(error or "")[:500],
                "max_attempts": max(1, int(max_attempts or 3)),
                **scope_params,
            },
        )
        if not rows:
            return "stale_claim"
        if rows[0]["status"] == "failed":
            await self._finalize_speech_rows(
                rows,
                succeeded=False,
                reason="max_attempts_exhausted",
            )
        return "failed" if rows[0]["status"] == "failed" else "retry"

    async def reply_queue_stats(self, tenant_id: str) -> dict:
        rows = await _exec(
            "SELECT status, COUNT(*) AS n FROM plugin_wxbot_reply_queue "
            "WHERE tenant_id = :tid GROUP BY status",
            {"tid": tenant_id},
        )
        return {r["status"]: r["n"] for r in rows}

    async def list_active_outbound_file_paths(self, *, limit: int = 10000) -> list[str]:
        """Return every in-flight file path that staging cleanup must preserve."""

        rows = await _exec(
            "SELECT DISTINCT file_path FROM plugin_wxbot_reply_queue "
            "WHERE msg_type = 'file' AND file_path <> '' "
            "AND status IN ('pending', 'sending', 'queued') "
            "ORDER BY file_path LIMIT :lim",
            {"lim": max(1, min(int(limit or 10000), 50000))},
        )
        return [
            path
            for row in rows
            if (path := str(row.get("file_path") or "").strip())
        ]

    async def list_reply_queue(
        self,
        tenant_id: str,
        *,
        status: str = "",
        session_id: str = "",
        trace_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tid"]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "lim": max(1, min(int(limit or 100), 500)),
        }
        if status.strip():
            clauses.append("status = :status")
            params["status"] = status.strip()
        if session_id.strip():
            clauses.append("session_id = :sid")
            params["sid"] = session_id.strip()
        if trace_id.strip():
            clauses.append("trace_id = :trace_id")
            params["trace_id"] = trace_id.strip()

        rows = await _exec(
            "SELECT id, tenant_id, connection_id, session_id, session_name, sender_name, "
            "sender_wxid, mention_sender, "
            "reply_to_msg_svr_id, session_kind, reply_text, msg_type, image_path, image_url, "
            "file_path, file_name, file_size, file_md5, file_sha256, "
            "source_message_json, delivery_json, command_id, sdk_outbound_id, trace_id, status, "
            "participation_status, source_message_id, not_before, expires_at, "
            "attempt_count, claim_owner, claim_token, claim_until, error, "
            "created_at, queued_at, sent_at "
            "FROM plugin_wxbot_reply_queue "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT :lim",
            params,
        )
        return [self._deserialize_reply_queue_row(row) for row in rows]

    async def clear_reply_queue(
        self,
        tenant_id: str,
        *,
        status: str = "pending",
        session_id: str = "",
    ) -> dict[str, Any]:
        clauses = ["tenant_id = :tid"]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "status": status.strip() or "pending",
            "sid": session_id.strip(),
        }
        if params["status"] != "all":
            clauses.append("status = :status")
        if params["sid"]:
            clauses.append("session_id = :sid")

        rows = await _exec(
            "UPDATE plugin_wxbot_reply_queue "
            "SET status = 'cleared', error = :err, claim_owner = '', "
            "claim_token = '', claim_until = NULL "
            f"WHERE {' AND '.join(clauses)} "
            "RETURNING id, tenant_id, session_id, status",
            {
                **params,
                "err": "manually cleared",
            },
        )
        return {
            "tenant_id": tenant_id,
            "status": params["status"],
            "session_id": params["sid"],
            "cleared": len(rows),
            "ids": [int(row["id"]) for row in rows],
        }

    @staticmethod
    def _deserialize_reply_queue_row(row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        for key in ("source_message_json", "delivery_json"):
            raw = item.pop(key, "") or ""
            try:
                item[key[:-5]] = json.loads(raw) if raw else {}
            except Exception:
                item[key[:-5]] = {}
        return item

    # ── User bans ──

    async def create_user_ban(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_wxid: str,
        user_name: str = "",
        reason: str = "",
        created_by: str = "",
        expires_at: Any = None,
    ) -> dict[str, Any]:
        rows = await _exec(
            "INSERT INTO plugin_wxbot_user_bans "
            "(tenant_id, session_id, user_wxid, user_name, reason, created_by, expires_at) "
            "VALUES (:tid, :sid, :wxid, :name, :reason, :created_by, :expires_at) "
            "ON CONFLICT (tenant_id, session_id, user_wxid) WHERE revoked_at IS NULL "
            "DO UPDATE SET user_name = EXCLUDED.user_name, reason = EXCLUDED.reason, "
            "created_by = EXCLUDED.created_by, expires_at = EXCLUDED.expires_at, updated_at = NOW() "
            "RETURNING id, tenant_id, session_id, user_wxid, user_name, reason, created_by, "
            "expires_at, revoked_at, created_at, updated_at",
            {
                "tid": tenant_id,
                "sid": session_id,
                "wxid": user_wxid,
                "name": user_name,
                "reason": reason,
                "created_by": created_by,
                "expires_at": expires_at,
            },
        )
        return rows[0] if rows else {}

    async def revoke_user_ban(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_wxid: str,
    ) -> bool:
        rows = await _exec(
            "UPDATE plugin_wxbot_user_bans "
            "SET revoked_at = NOW(), updated_at = NOW() "
            "WHERE tenant_id = :tid AND session_id = :sid AND user_wxid = :wxid "
            "AND revoked_at IS NULL "
            "RETURNING id",
            {"tid": tenant_id, "sid": session_id, "wxid": user_wxid},
        )
        return bool(rows)

    async def list_active_user_bans(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = await _exec(
            "SELECT id, tenant_id, session_id, user_wxid, user_name, reason, created_by, "
            "expires_at, revoked_at, created_at, updated_at "
            "FROM plugin_wxbot_user_bans "
            "WHERE tenant_id = :tid AND session_id = :sid "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW()) "
            "ORDER BY created_at DESC, id DESC LIMIT :lim",
            {
                "tid": tenant_id,
                "sid": session_id,
                "lim": max(1, min(int(limit or 100), 500)),
            },
        )
        return rows

    async def get_active_user_ban(
        self,
        tenant_id: str,
        session_id: str,
        user_wxid: str,
    ) -> dict[str, Any] | None:
        rows = await _exec(
            "SELECT id, tenant_id, session_id, user_wxid, user_name, reason, created_by, "
            "expires_at, revoked_at, created_at, updated_at "
            "FROM plugin_wxbot_user_bans "
            "WHERE tenant_id = :tid AND session_id = :sid AND user_wxid = :wxid "
            "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > NOW()) "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            {"tid": tenant_id, "sid": session_id, "wxid": user_wxid},
        )
        return rows[0] if rows else None

    # ── Member events ──

    async def save_member_event(
        self,
        *,
        tenant_id: str,
        sdk_event_id: int,
        event_type: str,
        session_id: str,
        session_name: str,
        entity_wxid: str,
        entity_name: str,
        payload: dict[str, Any] | None,
        created_ts: int,
        connection_id: str = "",
    ) -> bool:
        clean_connection_id = normalize_wxbot_event_connection_id(connection_id)
        rows = await _exec(
            "INSERT INTO plugin_wxbot_member_events "
            "(tenant_id, connection_id, sdk_event_id, event_type, session_id, session_name, "
            " entity_wxid, entity_name, payload_json, created_ts) "
            "VALUES (:tid, :connection_id, :eid, :etype, :sid, :sname, :wxid, :ename, "
            ":payload, :cts) "
            "ON CONFLICT (tenant_id, connection_id, sdk_event_id) DO NOTHING "
            "RETURNING id",
            {
                "tid": tenant_id,
                "connection_id": clean_connection_id,
                "eid": sdk_event_id,
                "etype": event_type,
                "sid": session_id,
                "sname": session_name or "",
                "wxid": entity_wxid or "",
                "ename": entity_name or "",
                "payload": json.dumps(payload or {}, ensure_ascii=False),
                "cts": created_ts,
            },
        )
        saved = bool(rows)
        if saved and entity_wxid and session_id.endswith("@chatroom"):
            normalized_type = str(event_type or "").strip().lower()
            canonical_session_id = canonical_conversation_id(
                clean_connection_id,
                session_id,
            )
            canonical_user_wxid = canonical_participant_id(
                clean_connection_id,
                entity_wxid,
            )
            if normalized_type.endswith((".left", ".removed")):
                await self.set_group_member_active(
                    tenant_id=tenant_id,
                    session_id=canonical_session_id,
                    user_wxid=canonical_user_wxid,
                    user_name=entity_name,
                    active=False,
                    event_id=sdk_event_id,
                )
            elif normalized_type.endswith((".joined", ".added")):
                await self.set_group_member_active(
                    tenant_id=tenant_id,
                    session_id=canonical_session_id,
                    user_wxid=canonical_user_wxid,
                    user_name=entity_name,
                    active=True,
                    event_id=sdk_event_id,
                )
        return saved

    async def set_group_member_active(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_wxid: str,
        user_name: str = "",
        active: bool = True,
        event_id: int | None = None,
    ) -> None:
        if not session_id.endswith("@chatroom") or not user_wxid:
            return
        await _exec(
            "INSERT INTO plugin_wxbot_group_membership "
            "(tenant_id, session_id, user_wxid, user_name, is_active, joined_at, left_at, "
            " last_event_id, updated_at) VALUES "
            "(:tid, :sid, :wxid, :name, :active, CASE WHEN :active THEN NOW() ELSE NULL END, "
            " CASE WHEN :active THEN NULL ELSE NOW() END, :event_id, NOW()) "
            "ON CONFLICT (tenant_id, session_id, user_wxid) DO UPDATE SET "
            "user_name = CASE WHEN EXCLUDED.user_name <> '' THEN EXCLUDED.user_name "
            "ELSE plugin_wxbot_group_membership.user_name END, "
            "is_active = EXCLUDED.is_active, "
            "joined_at = CASE WHEN EXCLUDED.is_active THEN COALESCE("
            "plugin_wxbot_group_membership.joined_at, NOW()) "
            "ELSE plugin_wxbot_group_membership.joined_at END, "
            "left_at = CASE WHEN EXCLUDED.is_active THEN NULL ELSE NOW() END, "
            "last_event_id = COALESCE(EXCLUDED.last_event_id, "
            "plugin_wxbot_group_membership.last_event_id), updated_at = NOW()",
            {
                "tid": tenant_id,
                "sid": session_id,
                "wxid": user_wxid,
                "name": user_name or "",
                "active": bool(active),
                "event_id": event_id,
            },
        )

    async def record_group_member_seen(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_wxid: str,
        user_name: str = "",
    ) -> None:
        await self.set_group_member_active(
            tenant_id=tenant_id,
            session_id=session_id,
            user_wxid=user_wxid,
            user_name=user_name,
            active=True,
        )

    async def is_group_member(
        self,
        tenant_id: str,
        session_id: str,
        user_wxid: str,
    ) -> bool:
        if not session_id.endswith("@chatroom") or not user_wxid:
            return False
        rows = await _exec(
            "SELECT 1 FROM plugin_wxbot_group_membership "
            "WHERE tenant_id = :tid AND session_id = :sid AND user_wxid = :wxid "
            "AND is_active = TRUE LIMIT 1",
            {"tid": tenant_id, "sid": session_id, "wxid": user_wxid},
        )
        return bool(rows)

    async def list_member_events(
        self,
        tenant_id: str,
        limit: int = 50,
        *,
        connection_id: str = "",
    ) -> list[dict]:
        clean_connection_id = normalize_wxbot_event_connection_id(connection_id)
        rows = await _exec(
            "SELECT connection_id, sdk_event_id, event_type, session_id, session_name, entity_wxid, "
            "entity_name, payload_json, created_ts, received_at "
            "FROM plugin_wxbot_member_events "
            "WHERE tenant_id = :tid AND connection_id = :connection_id "
            "ORDER BY created_ts DESC, sdk_event_id DESC "
            "LIMIT :lim",
            {
                "tid": tenant_id,
                "connection_id": clean_connection_id,
                "lim": limit,
            },
        )
        out: list[dict] = []
        for row in rows:
            payload_raw = row.get("payload_json") or "{}"
            try:
                payload = json.loads(payload_raw)
            except Exception:
                payload = {}
            item = dict(row)
            item["payload"] = payload
            item.pop("payload_json", None)
            out.append(item)
        return out

    async def member_event_stats(
        self,
        tenant_id: str,
        *,
        connection_id: str = "",
    ) -> dict[str, int]:
        clean_connection_id = normalize_wxbot_event_connection_id(connection_id)
        rows = await _exec(
            "SELECT event_type, COUNT(*) AS n "
            "FROM plugin_wxbot_member_events "
            "WHERE tenant_id = :tid AND connection_id = :connection_id "
            "GROUP BY event_type",
            {"tid": tenant_id, "connection_id": clean_connection_id},
        )
        return {r["event_type"]: r["n"] for r in rows}

    # ── Message media ready events ──

    async def save_media_ready_event(
        self,
        *,
        tenant_id: str,
        sdk_event_id: int,
        event_type: str,
        stream_event_id: str,
        message_id: str,
        session_id: str,
        session_name: str,
        sender_wxid: str,
        sender_name: str,
        msg_type: str,
        media_type: str,
        media_path: str,
        media_url: str,
        payload: dict[str, Any] | None,
        created_ts: int,
        connection_id: str = "",
    ) -> bool:
        clean_connection_id = normalize_wxbot_event_connection_id(connection_id)
        rows = await _exec(
            "INSERT INTO plugin_wxbot_media_ready_events "
            "(tenant_id, connection_id, sdk_event_id, event_type, stream_event_id, message_id, "
            " session_id, session_name, "
            " sender_wxid, sender_name, msg_type, media_type, media_path, media_url, payload_json, created_ts) "
            "VALUES (:tid, :connection_id, :eid, :etype, :stream_eid, :mid, :sid, :session_name, "
            " :sender_wxid, :sender_name, :msg_type, :media_type, :media_path, :media_url, :payload, :cts) "
            "ON CONFLICT (tenant_id, connection_id, sdk_event_id) DO NOTHING "
            "RETURNING id",
            {
                "tid": tenant_id,
                "connection_id": clean_connection_id,
                "eid": sdk_event_id,
                "etype": event_type,
                "stream_eid": stream_event_id,
                "mid": message_id,
                "sid": session_id,
                "session_name": session_name or "",
                "sender_wxid": sender_wxid or "",
                "sender_name": sender_name or "",
                "msg_type": msg_type or "",
                "media_type": media_type or "",
                "media_path": media_path or "",
                "media_url": media_url or "",
                "payload": json.dumps(payload or {}, ensure_ascii=False),
                "cts": created_ts,
            },
        )
        return bool(rows)

    async def list_media_ready_events(
        self,
        tenant_id: str,
        limit: int = 50,
        *,
        connection_id: str = "",
    ) -> list[dict[str, Any]]:
        clean_connection_id = normalize_wxbot_event_connection_id(connection_id)
        rows = await _exec(
            "SELECT connection_id, sdk_event_id, event_type, stream_event_id, message_id, "
            "session_id, session_name, "
            "sender_wxid, sender_name, msg_type, media_type, media_path, media_url, payload_json, "
            "created_ts, received_at "
            "FROM plugin_wxbot_media_ready_events "
            "WHERE tenant_id = :tid AND connection_id = :connection_id "
            "ORDER BY created_ts DESC, sdk_event_id DESC "
            "LIMIT :lim",
            {
                "tid": tenant_id,
                "connection_id": clean_connection_id,
                "lim": limit,
            },
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            payload_raw = row.get("payload_json") or "{}"
            try:
                payload = json.loads(payload_raw)
            except Exception:
                payload = {}
            item = dict(row)
            item["payload"] = payload
            item.pop("payload_json", None)
            out.append(item)
        return out

    async def get_media_ready_event(
        self,
        tenant_id: str,
        *,
        message_id: str,
        connection_id: str = "",
    ) -> dict[str, Any] | None:
        """Return the newest deferred-media update for one inbound message."""

        clean_message_id = str(message_id or "").strip()
        if not clean_message_id:
            return None
        clean_connection_id = normalize_wxbot_event_connection_id(connection_id)
        rows = await _exec(
            "SELECT connection_id, sdk_event_id, event_type, stream_event_id, message_id, "
            " session_id, session_name, sender_wxid, sender_name, msg_type, media_type, "
            " media_path, media_url, payload_json, created_ts, received_at "
            "FROM plugin_wxbot_media_ready_events "
            "WHERE tenant_id = :tid AND connection_id = :connection_id AND message_id = :mid "
            "ORDER BY created_ts DESC, sdk_event_id DESC LIMIT 1",
            {
                "tid": tenant_id,
                "connection_id": clean_connection_id,
                "mid": clean_message_id,
            },
        )
        if not rows:
            return None
        item = dict(rows[0])
        payload_raw = item.get("payload_json") or "{}"
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        item["payload"] = payload
        item.pop("payload_json", None)
        return item

    async def media_ready_stats(
        self,
        tenant_id: str,
        *,
        connection_id: str = "",
    ) -> dict[str, int]:
        clean_connection_id = normalize_wxbot_event_connection_id(connection_id)
        rows = await _exec(
            "SELECT event_type, COUNT(*) AS n "
            "FROM plugin_wxbot_media_ready_events "
            "WHERE tenant_id = :tid AND connection_id = :connection_id "
            "GROUP BY event_type",
            {"tid": tenant_id, "connection_id": clean_connection_id},
        )
        return {r["event_type"]: r["n"] for r in rows}

    # ── Durable group observations / rolling summary state ──

    @staticmethod
    def _hydrate_group_observation(row: dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        raw = str(item.get("metadata_json") or "")
        try:
            item["metadata"] = json.loads(raw) if raw else {}
        except Exception:
            item["metadata"] = {}
        item.pop("metadata_json", None)
        return item

    async def record_group_observation(
        self,
        *,
        tenant_id: str,
        session_id: str,
        message_id: str,
        session_name: str = "",
        sender_wxid: str = "",
        sender_name: str = "",
        msg_type: str = "text",
        content: str = "",
        mentioned_me: bool = False,
        bot_addressed: bool = False,
        is_self_sent: bool = False,
        occurred_ts: int = 0,
        metadata: dict[str, Any] | None = None,
        summary_debounce_seconds: float = 5.0,
    ) -> bool:
        """Persist one immutable group message and debounce its summary job."""
        tenant_id = str(tenant_id or "").strip()
        session_id = str(session_id or "").strip()
        message_id = str(message_id or "").strip()
        if not tenant_id or not session_id.endswith("@chatroom") or not message_id:
            return False
        rows = await _exec(
            "WITH inserted AS ("
            " INSERT INTO plugin_wxbot_group_observations "
            " (tenant_id, session_id, message_id, session_name, sender_wxid, sender_name, "
            "  msg_type, content, mentioned_me, bot_addressed, is_self_sent, occurred_ts, metadata_json) "
            " VALUES (:tid, :sid, :mid, :session_name, :sender_wxid, :sender_name, "
            "  :msg_type, :content, :mentioned_me, :bot_addressed, :is_self_sent, :occurred_ts, :metadata) "
            " ON CONFLICT (tenant_id, session_id, message_id) DO NOTHING "
            " RETURNING id, tenant_id, session_id, session_name"
            "), scheduled AS ("
            " INSERT INTO plugin_wxbot_group_summary_jobs "
            " (tenant_id, session_id, session_name, status, requested_through_observation_id, "
            "  next_attempt_at, created_at, updated_at) "
            " SELECT tenant_id, session_id, session_name, 'pending', id, "
            "  NOW() + (:debounce * INTERVAL '1 second'), NOW(), NOW() FROM inserted "
            " ON CONFLICT (tenant_id, session_id) DO UPDATE SET "
            " session_name = EXCLUDED.session_name, "
            " requested_through_observation_id = GREATEST("
            "   plugin_wxbot_group_summary_jobs.requested_through_observation_id, "
            "   EXCLUDED.requested_through_observation_id"
            " ), "
            " status = CASE WHEN plugin_wxbot_group_summary_jobs.status = 'running' "
            "   AND plugin_wxbot_group_summary_jobs.claim_expires_at > NOW() "
            "   THEN 'running' ELSE 'pending' END, "
            " claimed_by = CASE WHEN plugin_wxbot_group_summary_jobs.status = 'running' "
            "   AND plugin_wxbot_group_summary_jobs.claim_expires_at > NOW() "
            "   THEN plugin_wxbot_group_summary_jobs.claimed_by ELSE '' END, "
            " claim_token = CASE WHEN plugin_wxbot_group_summary_jobs.status = 'running' "
            "   AND plugin_wxbot_group_summary_jobs.claim_expires_at > NOW() "
            "   THEN plugin_wxbot_group_summary_jobs.claim_token ELSE '' END, "
            " claim_expires_at = CASE WHEN plugin_wxbot_group_summary_jobs.status = 'running' "
            "   AND plugin_wxbot_group_summary_jobs.claim_expires_at > NOW() "
            "   THEN plugin_wxbot_group_summary_jobs.claim_expires_at ELSE NULL END, "
            " next_attempt_at = CASE WHEN plugin_wxbot_group_summary_jobs.status = 'running' "
            "   AND plugin_wxbot_group_summary_jobs.claim_expires_at > NOW() "
            "   THEN plugin_wxbot_group_summary_jobs.next_attempt_at "
            "   WHEN plugin_wxbot_group_summary_jobs.status IN ('pending', 'failed') "
            "   THEN LEAST(plugin_wxbot_group_summary_jobs.next_attempt_at, EXCLUDED.next_attempt_at) "
            "   ELSE EXCLUDED.next_attempt_at END, "
            " error = CASE WHEN plugin_wxbot_group_summary_jobs.status = 'running' "
            "   AND plugin_wxbot_group_summary_jobs.claim_expires_at > NOW() "
            "   THEN plugin_wxbot_group_summary_jobs.error ELSE '' END, "
            " completed_at = NULL, updated_at = NOW() "
            " RETURNING tenant_id, session_id"
            ") "
            "SELECT inserted.id FROM inserted "
            "JOIN scheduled USING (tenant_id, session_id)",
            {
                "tid": tenant_id,
                "sid": session_id,
                "mid": message_id,
                "session_name": str(session_name or ""),
                "sender_wxid": str(sender_wxid or ""),
                "sender_name": str(sender_name or ""),
                "msg_type": str(msg_type or "text"),
                "content": str(content or ""),
                "mentioned_me": bool(mentioned_me),
                "bot_addressed": bool(bot_addressed),
                "is_self_sent": bool(is_self_sent),
                "occurred_ts": max(0, int(occurred_ts or 0)),
                "metadata": json.dumps(metadata or {}, ensure_ascii=False, default=str),
                "debounce": max(0.0, float(summary_debounce_seconds or 0.0)),
            },
        )
        if not is_self_sent and sender_wxid:
            try:
                await self.record_group_member_seen(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_wxid=str(sender_wxid),
                    user_name=str(sender_name or ""),
                )
            except Exception as exc:
                logger.warning(
                    "wxbot.store.group_membership_update_failed",
                    session_id=session_id,
                    user_wxid=str(sender_wxid),
                    error_class=exc.__class__.__name__,
                )
        if self._speech_ledger is not None:
            observed_at = (
                datetime.fromtimestamp(int(occurred_ts), tz=UTC)
                if int(occurred_ts or 0) > 0
                else datetime.now(UTC)
            )
            # Independently idempotent: retries can repair a transient ledger
            # failure even when the immutable observation already exists.
            await self._speech_ledger.observe_message(
                tenant_id=tenant_id,
                session_id=session_id,
                message_id=message_id,
                is_bot=bool(is_self_sent),
                text=content,
                occurred_at=observed_at,
            )
        return bool(rows)

    async def save_group_observation(self, **kwargs: Any) -> bool:
        """Backward-compatible alias for record_group_observation."""
        return await self.record_group_observation(**kwargs)

    async def list_group_observations(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 100,
        exclude_message_id: str = "",
        after_id: int = 0,
    ) -> list[dict[str, Any]]:
        """Return one chronological observation batch after a durable cursor."""
        excluded = str(exclude_message_id or "").strip()
        exclude_clause = "AND message_id <> :exclude_mid " if excluded else ""
        params: dict[str, Any] = {
            "tid": str(tenant_id or "").strip(),
            "sid": str(session_id or "").strip(),
            "after_id": max(0, int(after_id or 0)),
            "lim": max(1, min(int(limit or 100), 500)),
        }
        if excluded:
            params["exclude_mid"] = excluded
        rows = await _exec(
            "SELECT id, tenant_id, session_id, message_id, session_name, sender_wxid, sender_name, "
            "msg_type, content, mentioned_me, bot_addressed, is_self_sent, occurred_ts, "
            "metadata_json, received_at "
            "FROM plugin_wxbot_group_observations "
            "WHERE tenant_id = :tid AND session_id = :sid AND id > :after_id "
            f"{exclude_clause}"
            "ORDER BY id ASC LIMIT :lim",
            params,
        )
        return [self._hydrate_group_observation(row) for row in rows]

    async def list_recent_group_observations(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return the newest durable group observations in newest-first order."""
        limit = max(1, min(int(limit or 50), 500))
        before_clause = "AND id < :before_id " if before_id is not None else ""
        params: dict[str, Any] = {
            "tid": str(tenant_id or "").strip(),
            "sid": str(session_id or "").strip(),
            "lim": limit,
        }
        if before_id is not None:
            params["before_id"] = max(1, int(before_id))
        rows = await _exec(
            "SELECT id, tenant_id, session_id, message_id, session_name, sender_wxid, sender_name, "
            "msg_type, content, mentioned_me, bot_addressed, is_self_sent, occurred_ts, "
            "metadata_json, received_at "
            "FROM plugin_wxbot_group_observations "
            "WHERE tenant_id = :tid AND session_id = :sid "
            f"{before_clause}"
            "ORDER BY id DESC LIMIT :lim",
            params,
        )
        return [self._hydrate_group_observation(row) for row in rows]

    async def list_group_observations_for_period(
        self,
        tenant_id: str,
        session_id: str,
        *,
        start_occurred_ts: int,
        end_occurred_ts: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return observations in one ``occurred_ts`` [start, end) window."""
        start_ts = max(0, int(start_occurred_ts or 0))
        end_ts = max(0, int(end_occurred_ts or 0))
        if end_ts <= start_ts:
            return []
        rows = await _exec(
            "SELECT id, tenant_id, session_id, message_id, session_name, sender_wxid, sender_name, "
            "msg_type, content, mentioned_me, bot_addressed, is_self_sent, occurred_ts, "
            "metadata_json, received_at "
            "FROM plugin_wxbot_group_observations "
            "WHERE tenant_id = :tid AND session_id = :sid "
            "AND occurred_ts >= :start_ts AND occurred_ts < :end_ts "
            "ORDER BY occurred_ts ASC, id ASC LIMIT :lim",
            {
                "tid": str(tenant_id or "").strip(),
                "sid": str(session_id or "").strip(),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "lim": max(1, min(int(limit or 1), 10001)),
            },
        )
        return [self._hydrate_group_observation(row) for row in rows]

    async def get_participation_snapshot(
        self,
        tenant_id: str,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return the durable, bounded observations used by social policy."""
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        now_ts = int(current.timestamp())
        observations = await self.list_recent_group_observations(
            tenant_id,
            session_id,
            limit=40,
        )
        total_messages = len(observations)
        bot_messages = sum(1 for item in observations if bool(item.get("is_self_sent")))
        consecutive_bot_messages = 0
        for item in observations:
            if not bool(item.get("is_self_sent")):
                break
            consecutive_bot_messages += 1
        recent_human = [
            item
            for item in observations
            if not bool(item.get("is_self_sent"))
            and int(item.get("occurred_ts") or 0) >= now_ts - 15
        ]
        recent_senders = {
            str(item.get("sender_wxid") or item.get("sender_name") or "").strip()
            for item in recent_human
            if str(item.get("sender_wxid") or item.get("sender_name") or "").strip()
        }
        budget_rows = await _exec(
            "SELECT "
            "COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '10 minutes') "
            "  AS soft_replies_last_10m, "
            "COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 hour') "
            "  AS soft_replies_last_hour "
            "FROM plugin_wxbot_reply_queue "
            "WHERE tenant_id = :tid AND session_id = :sid "
            "AND participation_status = 'may_reply' "
            "AND status IN ('pending', 'sending', 'queued', 'sent')",
            {"tid": tenant_id, "sid": session_id},
        )
        budget = budget_rows[0] if budget_rows else {}
        return {
            "bot_messages_last_40": bot_messages,
            "total_messages_last_40": total_messages,
            "soft_replies_last_10m": int(budget.get("soft_replies_last_10m") or 0),
            "soft_replies_last_hour": int(budget.get("soft_replies_last_hour") or 0),
            "consecutive_bot_messages": consecutive_bot_messages,
            "bot_replied_within_60s": any(
                bool(item.get("is_self_sent")) and int(item.get("occurred_ts") or 0) >= now_ts - 60
                for item in observations
            ),
            "rapid_multi_party_chat": (len(recent_human) >= 4 and len(recent_senders) >= 3),
        }

    async def list_group_observations_after(
        self,
        tenant_id: str,
        session_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return the next chronological batch used by a rolling summarizer."""
        return await self.list_group_observations(
            tenant_id,
            session_id,
            after_id=after_id,
            limit=limit,
        )

    @staticmethod
    def _hydrate_group_summary_state(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        item = dict(row)
        raw = str(item.get("summary_json") or "")
        try:
            item["summary_payload"] = json.loads(raw) if raw else {}
        except Exception:
            item["summary_payload"] = {}
        item.pop("summary_json", None)
        return item

    async def get_group_summary_state(
        self,
        tenant_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        rows = await _exec(
            "SELECT tenant_id, session_id, session_name, summary_text, summary_json, "
            "last_observation_id, last_message_id, message_count, version, updated_at "
            "FROM plugin_wxbot_group_summary_state "
            "WHERE tenant_id = :tid AND session_id = :sid",
            {
                "tid": str(tenant_id or "").strip(),
                "sid": str(session_id or "").strip(),
            },
        )
        return self._hydrate_group_summary_state(rows[0] if rows else None)

    async def get_group_summary(
        self,
        tenant_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        return await self.get_group_summary_state(tenant_id, session_id)

    async def compare_and_set_group_summary_state(
        self,
        *,
        tenant_id: str,
        session_id: str,
        expected_version: int,
        summary_text: str,
        summary_payload: dict[str, Any] | None,
        last_observation_id: int,
        last_message_id: str,
        message_count: int,
        session_name: str = "",
    ) -> dict[str, Any] | None:
        """Atomically create/update rolling summary state using optimistic locking."""
        tenant_id = str(tenant_id or "").strip()
        session_id = str(session_id or "").strip()
        expected_version = int(expected_version)
        if (
            not tenant_id
            or not session_id.endswith("@chatroom")
            or expected_version < 0
            or int(last_observation_id) < 0
        ):
            return None
        params = {
            "tid": tenant_id,
            "sid": session_id,
            "session_name": str(session_name or ""),
            "summary_text": str(summary_text or ""),
            "summary_json": json.dumps(summary_payload or {}, ensure_ascii=False),
            "last_observation_id": int(last_observation_id),
            "last_message_id": str(last_message_id or ""),
            "message_count": max(0, int(message_count or 0)),
            "expected_version": expected_version,
        }
        returning = (
            " RETURNING tenant_id, session_id, session_name, summary_text, summary_json, "
            "last_observation_id, last_message_id, message_count, version, updated_at"
        )
        if expected_version == 0:
            rows = await _exec(
                "INSERT INTO plugin_wxbot_group_summary_state "
                "(tenant_id, session_id, session_name, summary_text, summary_json, "
                " last_observation_id, last_message_id, message_count, version, updated_at) "
                "VALUES (:tid, :sid, :session_name, :summary_text, :summary_json, "
                " :last_observation_id, :last_message_id, :message_count, 1, NOW()) "
                "ON CONFLICT (tenant_id, session_id) DO NOTHING" + returning,
                params,
            )
        else:
            rows = await _exec(
                "UPDATE plugin_wxbot_group_summary_state SET "
                "session_name = :session_name, summary_text = :summary_text, "
                "summary_json = :summary_json, last_observation_id = :last_observation_id, "
                "last_message_id = :last_message_id, message_count = :message_count, "
                "version = version + 1, updated_at = NOW() "
                "WHERE tenant_id = :tid AND session_id = :sid "
                "AND version = :expected_version "
                "AND last_observation_id <= :last_observation_id" + returning,
                params,
            )
        return self._hydrate_group_summary_state(rows[0] if rows else None)

    async def claim_group_summary_job(
        self,
        worker_id: str,
        lock_ttl_seconds: float = 120.0,
    ) -> dict[str, Any] | None:
        """Claim one due group-summary job, recovering expired worker leases."""
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            return None
        claim_token = uuid4().hex
        rows = await _exec(
            "UPDATE plugin_wxbot_group_summary_jobs AS job SET "
            "status = 'running', claimed_by = :worker_id, claim_token = :claim_token, "
            "claim_expires_at = NOW() + (:lock_ttl * INTERVAL '1 second'), "
            "claimed_through_observation_id = job.requested_through_observation_id, "
            "attempt_count = job.attempt_count + 1, error = '', "
            "started_at = NOW(), completed_at = NULL, updated_at = NOW() "
            "FROM ("
            " SELECT tenant_id, session_id FROM plugin_wxbot_group_summary_jobs "
            " WHERE (status IN ('pending', 'failed') AND next_attempt_at <= NOW()) "
            "    OR (status = 'running' AND (claim_expires_at IS NULL OR claim_expires_at <= NOW())) "
            " ORDER BY next_attempt_at, updated_at, tenant_id, session_id LIMIT 1 "
            " FOR UPDATE SKIP LOCKED"
            ") AS picked "
            "WHERE job.tenant_id = picked.tenant_id AND job.session_id = picked.session_id "
            "RETURNING job.tenant_id, job.session_id, job.session_name, job.status, "
            "job.requested_through_observation_id, job.claimed_through_observation_id, "
            "job.claimed_by, job.claim_token, job.claim_expires_at, job.attempt_count, "
            "job.next_attempt_at, job.created_at, job.updated_at, job.started_at",
            {
                "worker_id": worker_id,
                "claim_token": claim_token,
                "lock_ttl": max(1.0, float(lock_ttl_seconds or 120.0)),
            },
        )
        return dict(rows[0]) if rows else None

    async def complete_group_summary_job(
        self,
        *,
        tenant_id: str,
        session_id: str,
        covered_observation_id: int,
        summary_text: str,
        worker_id: str,
        claim_token: str = "",
    ) -> bool:
        """Commit a summary and release its lease in one database statement."""
        tenant_id = str(tenant_id or "").strip()
        session_id = str(session_id or "").strip()
        worker_id = str(worker_id or "").strip()
        covered_observation_id = int(covered_observation_id or 0)
        if (
            not tenant_id
            or not session_id.endswith("@chatroom")
            or not worker_id
            or covered_observation_id <= 0
        ):
            return False
        rows = await _exec(
            "WITH owned AS ("
            " SELECT job.tenant_id, job.session_id, job.session_name, "
            " job.requested_through_observation_id, "
            " COALESCE(("
            "   SELECT state.last_observation_id FROM plugin_wxbot_group_summary_state AS state "
            "   WHERE state.tenant_id = job.tenant_id AND state.session_id = job.session_id"
            " ), 0) AS previous_observation_id, "
            " COALESCE(("
            "   SELECT state.message_count FROM plugin_wxbot_group_summary_state AS state "
            "   WHERE state.tenant_id = job.tenant_id AND state.session_id = job.session_id"
            " ), 0) AS previous_message_count "
            " FROM plugin_wxbot_group_summary_jobs AS job "
            " WHERE job.tenant_id = :tid AND job.session_id = :sid "
            " AND job.status = 'running' AND job.claimed_by = :worker_id "
            " AND (:claim_token = '' OR job.claim_token = :claim_token) "
            " AND job.claim_expires_at > NOW() "
            " AND job.claimed_through_observation_id >= :covered_id "
            " AND :covered_id >= COALESCE(("
            "   SELECT state.last_observation_id FROM plugin_wxbot_group_summary_state AS state "
            "   WHERE state.tenant_id = job.tenant_id AND state.session_id = job.session_id"
            " ), 0) "
            " FOR UPDATE"
            "), saved AS ("
            " INSERT INTO plugin_wxbot_group_summary_state "
            " (tenant_id, session_id, session_name, summary_text, summary_json, "
            "  last_observation_id, last_message_id, message_count, version, updated_at) "
            " SELECT owned.tenant_id, owned.session_id, owned.session_name, :summary_text, '{}', "
            " :covered_id, COALESCE(("
            "   SELECT observation.message_id FROM plugin_wxbot_group_observations AS observation "
            "   WHERE observation.tenant_id = owned.tenant_id "
            "   AND observation.session_id = owned.session_id AND observation.id = :covered_id"
            " ), ''), owned.previous_message_count + ("
            "   SELECT COUNT(*) FROM plugin_wxbot_group_observations AS observation "
            "   WHERE observation.tenant_id = owned.tenant_id "
            "   AND observation.session_id = owned.session_id "
            "   AND observation.id > owned.previous_observation_id "
            "   AND observation.id <= :covered_id"
            " ), 1, NOW() FROM owned "
            " ON CONFLICT (tenant_id, session_id) DO UPDATE SET "
            " session_name = EXCLUDED.session_name, summary_text = EXCLUDED.summary_text, "
            " last_observation_id = EXCLUDED.last_observation_id, "
            " last_message_id = EXCLUDED.last_message_id, message_count = EXCLUDED.message_count, "
            " version = plugin_wxbot_group_summary_state.version + 1, updated_at = NOW() "
            " WHERE plugin_wxbot_group_summary_state.last_observation_id <= EXCLUDED.last_observation_id "
            " RETURNING tenant_id, session_id"
            "), finished AS ("
            " UPDATE plugin_wxbot_group_summary_jobs AS job SET "
            " status = CASE WHEN job.requested_through_observation_id > :covered_id "
            "   THEN 'pending' ELSE 'completed' END, "
            " next_attempt_at = CASE WHEN job.requested_through_observation_id > :covered_id "
            "   THEN NOW() ELSE job.next_attempt_at END, "
            " claimed_by = '', claim_token = '', claim_expires_at = NULL, error = '', "
            " completed_at = CASE WHEN job.requested_through_observation_id > :covered_id "
            "   THEN NULL ELSE NOW() END, updated_at = NOW() "
            " FROM owned JOIN saved USING (tenant_id, session_id) "
            " WHERE job.tenant_id = owned.tenant_id AND job.session_id = owned.session_id "
            " RETURNING job.tenant_id"
            ") SELECT COUNT(*) AS n FROM finished",
            {
                "tid": tenant_id,
                "sid": session_id,
                "covered_id": covered_observation_id,
                "summary_text": str(summary_text or ""),
                "worker_id": worker_id,
                "claim_token": str(claim_token or "").strip(),
            },
        )
        return bool(rows and int(rows[0].get("n") or 0) > 0)

    async def defer_group_summary_job(
        self,
        *,
        tenant_id: str,
        session_id: str,
        worker_id: str,
        claim_token: str = "",
        defer_seconds: float = 30.0,
    ) -> bool:
        """Release scope-denied summary work without spending an attempt."""

        tenant_id = str(tenant_id or "").strip()
        session_id = str(session_id or "").strip()
        worker_id = str(worker_id or "").strip()
        claim_token = str(claim_token or "").strip()
        if not tenant_id or not session_id.endswith("@chatroom") or not worker_id:
            return False
        rows = await _exec(
            "UPDATE plugin_wxbot_group_summary_jobs SET "
            "status = 'pending', claimed_by = '', claim_token = '', "
            "claim_expires_at = NULL, "
            "attempt_count = GREATEST(attempt_count - 1, 0), error = '', "
            "next_attempt_at = NOW() + (:defer_seconds * INTERVAL '1 second'), "
            "updated_at = NOW() "
            "WHERE tenant_id = :tid AND session_id = :sid "
            "AND status = 'running' AND claimed_by = :worker_id "
            "AND (:claim_token = '' OR claim_token = :claim_token) "
            "AND claim_expires_at > NOW() RETURNING tenant_id",
            {
                "tid": tenant_id,
                "sid": session_id,
                "worker_id": worker_id,
                "claim_token": claim_token,
                "defer_seconds": max(1.0, float(defer_seconds or 30.0)),
            },
        )
        return bool(rows)

    async def fail_group_summary_job(
        self,
        *,
        tenant_id: str,
        session_id: str,
        error: str,
        worker_id: str,
        retry_backoff_seconds: float = 30.0,
        claim_token: str = "",
    ) -> bool:
        """Release an owned lease and make the job retryable after backoff."""
        tenant_id = str(tenant_id or "").strip()
        session_id = str(session_id or "").strip()
        worker_id = str(worker_id or "").strip()
        if not tenant_id or not session_id.endswith("@chatroom") or not worker_id:
            return False
        rows = await _exec(
            "UPDATE plugin_wxbot_group_summary_jobs SET "
            "status = 'failed', claimed_by = '', claim_token = '', claim_expires_at = NULL, "
            "error = :error, next_attempt_at = NOW() + (:backoff * INTERVAL '1 second'), "
            "updated_at = NOW() "
            "WHERE tenant_id = :tid AND session_id = :sid "
            "AND status = 'running' AND claimed_by = :worker_id "
            "AND (:claim_token = '' OR claim_token = :claim_token) "
            "AND claim_expires_at > NOW() "
            "RETURNING tenant_id",
            {
                "tid": tenant_id,
                "sid": session_id,
                "error": str(error or "")[:4000],
                "worker_id": worker_id,
                "claim_token": str(claim_token or "").strip(),
                "backoff": max(0.0, float(retry_backoff_seconds or 0.0)),
            },
        )
        return bool(rows)

    async def prune_group_observations(
        self,
        retention_days: int,
        keep_recent: int = 200,
    ) -> int:
        """Delete only old observations already covered by a durable summary."""
        rows = await _exec(
            "WITH ranked AS ("
            " SELECT observation.id, observation.tenant_id, observation.session_id, "
            " observation.received_at, "
            " ROW_NUMBER() OVER ("
            "   PARTITION BY observation.tenant_id, observation.session_id "
            "   ORDER BY observation.id DESC"
            " ) AS recent_rank "
            " FROM plugin_wxbot_group_observations AS observation"
            "), deleted AS ("
            " DELETE FROM plugin_wxbot_group_observations AS observation "
            " USING ranked, plugin_wxbot_group_summary_state AS state "
            " WHERE observation.id = ranked.id "
            " AND state.tenant_id = observation.tenant_id "
            " AND state.session_id = observation.session_id "
            " AND observation.id <= state.last_observation_id "
            " AND observation.received_at < NOW() - (:retention_days * INTERVAL '1 day') "
            " AND ranked.recent_rank > :keep_recent "
            " RETURNING observation.id"
            ") SELECT COUNT(*) AS n FROM deleted",
            {
                "retention_days": max(0, int(retention_days or 0)),
                "keep_recent": max(0, int(keep_recent or 0)),
            },
        )
        return int(rows[0]["n"] or 0) if rows else 0

    # ── Session reply policy ──

    async def get_global_policy(self, tenant_id: str) -> dict[str, Any]:
        rows = await _exec(
            f"SELECT {_GLOBAL_POLICY_COLUMNS} FROM plugin_wxbot_tenant_policy "
            "WHERE tenant_id = :tid",
            {"tid": tenant_id},
        )
        return _normalize_global_policy(rows[0] if rows else None, tenant_id)

    async def set_global_policy(
        self,
        tenant_id: str,
        *,
        expected_version: int,
        private_reply_mode: str | None = None,
        group_reply_mode: str | None = None,
        group_reply_mention_sender: bool | None = None,
        trigger_keywords_text: str | None = None,
    ) -> WxbotPolicyMutation:
        engine = get_engine()
        async with engine.begin() as conn:
            return await self.set_global_policy_in_transaction(
                conn,
                tenant_id=tenant_id,
                expected_version=expected_version,
                private_reply_mode=private_reply_mode,
                group_reply_mode=group_reply_mode,
                group_reply_mention_sender=group_reply_mention_sender,
                trigger_keywords_text=trigger_keywords_text,
            )

    async def set_global_policy_in_transaction(
        self,
        db: AsyncConnection | AsyncSession,
        *,
        tenant_id: str,
        expected_version: int,
        private_reply_mode: str | None = None,
        group_reply_mode: str | None = None,
        group_reply_mention_sender: bool | None = None,
        trigger_keywords_text: str | None = None,
    ) -> WxbotPolicyMutation:
        row = await _global_policy_row(db, tenant_id, for_update=True)
        before = _normalize_global_policy(row, tenant_id)
        current_version = int(before["version"])
        if current_version != int(expected_version):
            raise WxbotPolicyVersionConflictError(
                expected=int(expected_version),
                current=current_version,
            )
        current = dict(before)
        if private_reply_mode is not None:
            current["private_reply_mode"] = private_reply_mode
        if group_reply_mode is not None:
            current["group_reply_mode"] = group_reply_mode
        if group_reply_mention_sender is not None:
            current["group_reply_mention_sender"] = bool(group_reply_mention_sender)
        if trigger_keywords_text is not None:
            current["trigger_keywords_text"] = trigger_keywords_text
        params = {
            "tid": tenant_id,
            "private_mode": current["private_reply_mode"],
            "group_mode": current["group_reply_mode"],
            "group_mention": bool(current["group_reply_mention_sender"]),
            "keywords": current["trigger_keywords_text"],
            "expected_version": int(expected_version),
        }
        if row is None:
            result = await db.execute(
                text(
                    "INSERT INTO plugin_wxbot_tenant_policy "
                    "(tenant_id, private_reply_mode, group_reply_mode, "
                    "group_reply_mention_sender, trigger_keywords_text, version, updated_at) "
                    "VALUES (:tid, :private_mode, :group_mode, :group_mention, "
                    ":keywords, 1, NOW()) ON CONFLICT (tenant_id) DO NOTHING "
                    f"RETURNING {_GLOBAL_POLICY_COLUMNS}"
                ),
                params,
            )
        else:
            result = await db.execute(
                text(
                    "UPDATE plugin_wxbot_tenant_policy SET "
                    "private_reply_mode = :private_mode, group_reply_mode = :group_mode, "
                    "group_reply_mention_sender = :group_mention, "
                    "trigger_keywords_text = :keywords, version = version + 1, "
                    "updated_at = NOW() WHERE tenant_id = :tid "
                    "AND version = :expected_version "
                    f"RETURNING {_GLOBAL_POLICY_COLUMNS}"
                ),
                params,
            )
        written = result.mappings().first()
        if written is None:
            latest = await _global_policy_row(db, tenant_id, for_update=True)
            raise WxbotPolicyVersionConflictError(
                expected=int(expected_version),
                current=int((latest or {}).get("version") or 0),
            )
        after = _normalize_global_policy(dict(written), tenant_id)
        return WxbotPolicyMutation(before=before, after=after)

    async def get_session_policy(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        global_policy = await self.get_global_policy(tenant_id)
        rows = await _exec(
            f"SELECT {_SESSION_POLICY_COLUMNS} FROM plugin_wxbot_session_policy "
            "WHERE tenant_id = :tid AND session_id = :sid",
            {"tid": tenant_id, "sid": session_id},
        )
        return _session_policy_document(
            rows[0] if rows else None,
            tenant_id,
            session_id,
            global_policy,
            settings=self.settings,
        )

    async def set_session_policy(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        reply_mode: str | None = None,
        mention_sender_mode: str | None = None,
        trigger_keywords_text: str | None = None,
        reply_cooldown_seconds: float | None = None,
        coalesce_window_ms: int | None = None,
        adaptive_cooldown_enabled: bool | None = None,
        participation_policy: dict[str, Any] | None = None,
    ) -> WxbotPolicyMutation:
        engine = get_engine()
        async with engine.begin() as conn:
            return await self.set_session_policy_in_transaction(
                conn,
                tenant_id=tenant_id,
                session_id=session_id,
                expected_version=expected_version,
                reply_mode=reply_mode,
                mention_sender_mode=mention_sender_mode,
                trigger_keywords_text=trigger_keywords_text,
                reply_cooldown_seconds=reply_cooldown_seconds,
                coalesce_window_ms=coalesce_window_ms,
                adaptive_cooldown_enabled=adaptive_cooldown_enabled,
                participation_policy=participation_policy,
            )

    async def set_session_policy_in_transaction(
        self,
        db: AsyncConnection | AsyncSession,
        *,
        tenant_id: str,
        session_id: str,
        expected_version: int,
        reply_mode: str | None = None,
        mention_sender_mode: str | None = None,
        trigger_keywords_text: str | None = None,
        reply_cooldown_seconds: float | None = None,
        coalesce_window_ms: int | None = None,
        adaptive_cooldown_enabled: bool | None = None,
        participation_policy: dict[str, Any] | None = None,
    ) -> WxbotPolicyMutation:
        row = await _session_policy_row(
            db,
            tenant_id,
            session_id,
            for_update=True,
        )
        global_row = await _global_policy_row(db, tenant_id, for_update=False)
        global_policy = _normalize_global_policy(global_row, tenant_id)
        before = _session_policy_document(
            row,
            tenant_id,
            session_id,
            global_policy,
            settings=self.settings,
        )
        current_version = int(before["version"])
        if current_version != int(expected_version):
            raise WxbotPolicyVersionConflictError(
                expected=int(expected_version),
                current=current_version,
            )
        current = dict(before)
        if reply_mode is not None:
            current["reply_mode"] = reply_mode
        if mention_sender_mode is not None:
            current["mention_sender_mode"] = mention_sender_mode
        if trigger_keywords_text is not None:
            current["trigger_keywords_text"] = trigger_keywords_text
        if reply_cooldown_seconds is not None:
            current["reply_cooldown_seconds"] = max(
                0.0,
                min(float(reply_cooldown_seconds), 60.0),
            )
        if coalesce_window_ms is not None:
            current["coalesce_window_ms"] = max(0, min(int(coalesce_window_ms), 5000))
        if adaptive_cooldown_enabled is not None:
            current["adaptive_cooldown_enabled"] = bool(adaptive_cooldown_enabled)
        if participation_policy is not None:
            current["participation_policy"] = normalize_group_participation_policy(
                participation_policy,
                current=current.get("participation_policy"),
            )
        params = {
            "tid": tenant_id,
            "sid": session_id,
            "mode": current["reply_mode"],
            "mention_mode": current["mention_sender_mode"],
            "keywords": current["trigger_keywords_text"],
            "cooldown": current.get("reply_cooldown_seconds"),
            "coalesce": current.get("coalesce_window_ms"),
            "adaptive": current.get("adaptive_cooldown_enabled"),
            "participation_policy_json": json.dumps(
                normalize_group_participation_policy(current.get("participation_policy")),
                ensure_ascii=False,
            ),
            "expected_version": int(expected_version),
        }
        if row is None:
            result = await db.execute(
                text(
                    "INSERT INTO plugin_wxbot_session_policy "
                    "(tenant_id, session_id, reply_mode, mention_sender_mode, "
                    "trigger_keywords_text, reply_cooldown_seconds, coalesce_window_ms, "
                    "adaptive_cooldown_enabled, participation_policy_json, version, updated_at) "
                    "VALUES (:tid, :sid, :mode, :mention_mode, :keywords, "
                    ":cooldown, :coalesce, :adaptive, "
                    "CAST(:participation_policy_json AS JSONB), 1, NOW()) "
                    "ON CONFLICT (tenant_id, session_id) DO NOTHING "
                    f"RETURNING {_SESSION_POLICY_COLUMNS}"
                ),
                params,
            )
        else:
            result = await db.execute(
                text(
                    "UPDATE plugin_wxbot_session_policy SET reply_mode = :mode, "
                    "mention_sender_mode = :mention_mode, "
                    "trigger_keywords_text = :keywords, "
                    "reply_cooldown_seconds = :cooldown, "
                    "coalesce_window_ms = :coalesce, "
                    "adaptive_cooldown_enabled = :adaptive, "
                    "participation_policy_json = CAST(:participation_policy_json AS JSONB), "
                    "version = version + 1, updated_at = NOW() "
                    "WHERE tenant_id = :tid AND session_id = :sid "
                    "AND version = :expected_version "
                    f"RETURNING {_SESSION_POLICY_COLUMNS}"
                ),
                params,
            )
        written = result.mappings().first()
        if written is None:
            latest = await _session_policy_row(
                db,
                tenant_id,
                session_id,
                for_update=True,
            )
            raise WxbotPolicyVersionConflictError(
                expected=int(expected_version),
                current=int((latest or {}).get("version") or 0),
            )
        refreshed_global = await _global_policy_row(db, tenant_id, for_update=False)
        after = _session_policy_document(
            dict(written),
            tenant_id,
            session_id,
            _normalize_global_policy(refreshed_global, tenant_id),
            settings=self.settings,
        )
        return WxbotPolicyMutation(before=before, after=after)

    async def get_global_policy_in_transaction(
        self,
        db: AsyncConnection | AsyncSession,
        tenant_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        row = await _global_policy_row(db, tenant_id, for_update=for_update)
        return _normalize_global_policy(row, tenant_id)

    async def get_session_policy_in_transaction(
        self,
        db: AsyncConnection | AsyncSession,
        tenant_id: str,
        session_id: str,
        *,
        global_policy: dict[str, Any] | None = None,
        for_update: bool = False,
    ) -> dict[str, Any]:
        resolved_global = global_policy or await self.get_global_policy_in_transaction(
            db,
            tenant_id,
        )
        row = await _session_policy_row(
            db,
            tenant_id,
            session_id,
            for_update=for_update,
        )
        return _session_policy_document(
            row,
            tenant_id,
            session_id,
            resolved_global,
            settings=self.settings,
        )

    async def get_reply_policy_aggregate(
        self,
        tenant_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                global_policy = await self.get_global_policy_in_transaction(
                    conn,
                    tenant_id,
                )
                session_policy = await self.get_session_policy_in_transaction(
                    conn,
                    tenant_id,
                    session_id,
                    global_policy=global_policy,
                )
                repeater_config = await _repeater_config_document(
                    conn,
                    tenant_id,
                    session_id,
                )
                state = await self.get_reply_policy_aggregate_state(
                    conn,
                    tenant_id,
                    session_id,
                )
                effect_status = await self.get_reply_policy_effect_status(
                    conn,
                    tenant_id,
                    str(state.get("effect_idempotency_key") or ""),
                )
        return compose_reply_policy_aggregate(
            tenant_id=tenant_id,
            session_id=session_id,
            global_policy=global_policy,
            session_policy=session_policy,
            repeater_config=repeater_config,
            aggregate_state=state,
            effect_status=effect_status,
        )

    async def begin_reply_policy_idempotency(
        self,
        db: AsyncConnection | AsyncSession,
        *,
        tenant_id: str,
        session_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> dict[str, Any]:
        await db.execute(
            text(
                "INSERT INTO plugin_wxbot_reply_policy_idempotency "
                "(tenant_id, idempotency_key, session_id, request_hash, "
                "response_json, response_etag, completed, created_at, updated_at) "
                "VALUES (:tid, :key, :sid, :request_hash, '{}'::jsonb, '', FALSE, "
                "NOW(), NOW()) ON CONFLICT (tenant_id, idempotency_key) DO NOTHING"
            ),
            {
                "tid": tenant_id,
                "sid": session_id,
                "key": idempotency_key,
                "request_hash": request_hash,
            },
        )
        result = await db.execute(
            text(
                "SELECT tenant_id, idempotency_key, session_id, request_hash, "
                "response_json, response_etag, completed "
                "FROM plugin_wxbot_reply_policy_idempotency "
                "WHERE tenant_id = :tid AND idempotency_key = :key FOR UPDATE"
            ),
            {"tid": tenant_id, "key": idempotency_key},
        )
        row = result.mappings().first()
        if row is None:
            raise RuntimeError("reply_policy_idempotency_guard_unavailable")
        item = dict(row)
        if (
            str(item.get("session_id") or "") != session_id
            or str(item.get("request_hash") or "") != request_hash
        ):
            raise ReplyPolicyIdempotencyConflictError(
                "idempotency key is already bound to a different request"
            )
        item["response_json"] = _json_object(item.get("response_json"))
        item["completed"] = bool(item.get("completed"))
        return item

    async def complete_reply_policy_idempotency(
        self,
        db: AsyncConnection | AsyncSession,
        *,
        tenant_id: str,
        idempotency_key: str,
        response_payload: dict[str, Any],
        response_etag: str,
    ) -> None:
        result = await db.execute(
            text(
                "UPDATE plugin_wxbot_reply_policy_idempotency SET "
                "response_json = CAST(:response_json AS JSONB), "
                "response_etag = :response_etag, completed = TRUE, updated_at = NOW() "
                "WHERE tenant_id = :tid AND idempotency_key = :key "
                "AND completed = FALSE"
            ),
            {
                "tid": tenant_id,
                "key": idempotency_key,
                "response_json": json.dumps(
                    response_payload,
                    ensure_ascii=False,
                    default=_json_default,
                ),
                "response_etag": response_etag,
            },
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise RuntimeError("reply_policy_idempotency_completion_lost")

    async def lock_reply_policy_aggregate_state(
        self,
        db: AsyncConnection | AsyncSession,
        tenant_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        await db.execute(
            text(
                "INSERT INTO plugin_wxbot_reply_policy_aggregate_state "
                "(tenant_id, session_id, sdk_group_require_at_me, "
                "effect_idempotency_key, version, updated_at) "
                "VALUES (:tid, :sid, TRUE, '', 0, NOW()) "
                "ON CONFLICT (tenant_id, session_id) DO NOTHING"
            ),
            {"tid": tenant_id, "sid": session_id},
        )
        return await self.get_reply_policy_aggregate_state(
            db,
            tenant_id,
            session_id,
            for_update=True,
        )

    async def get_reply_policy_aggregate_state(
        self,
        db: AsyncConnection | AsyncSession,
        tenant_id: str,
        session_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        suffix = " FOR UPDATE" if for_update else ""
        result = await db.execute(
            text(
                "SELECT tenant_id, session_id, sdk_group_require_at_me, "
                "effect_idempotency_key, version, updated_at "
                "FROM plugin_wxbot_reply_policy_aggregate_state "
                "WHERE tenant_id = :tid AND session_id = :sid"
                f"{suffix}"
            ),
            {"tid": tenant_id, "sid": session_id},
        )
        row = result.mappings().first()
        if row is None:
            return {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "sdk_group_require_at_me": True,
                "effect_idempotency_key": "",
                "version": 0,
                "updated_at": None,
            }
        item = dict(row)
        item["sdk_group_require_at_me"] = bool(item.get("sdk_group_require_at_me"))
        item["version"] = max(0, int(item.get("version") or 0))
        return item

    async def update_reply_policy_aggregate_state(
        self,
        db: AsyncConnection | AsyncSession,
        *,
        tenant_id: str,
        session_id: str,
        expected_version: int,
        sdk_group_require_at_me: bool,
        effect_idempotency_key: str,
    ) -> dict[str, Any]:
        result = await db.execute(
            text(
                "UPDATE plugin_wxbot_reply_policy_aggregate_state SET "
                "sdk_group_require_at_me = :sdk_gate, "
                "effect_idempotency_key = :effect_key, version = version + 1, "
                "updated_at = NOW() WHERE tenant_id = :tid AND session_id = :sid "
                "AND version = :expected_version RETURNING tenant_id, session_id, "
                "sdk_group_require_at_me, effect_idempotency_key, version, updated_at"
            ),
            {
                "tid": tenant_id,
                "sid": session_id,
                "sdk_gate": bool(sdk_group_require_at_me),
                "effect_key": effect_idempotency_key,
                "expected_version": int(expected_version),
            },
        )
        row = result.mappings().first()
        if row is None:
            raise WxbotPolicyVersionConflictError(
                expected=int(expected_version),
                current=int(
                    (
                        await self.get_reply_policy_aggregate_state(
                            db,
                            tenant_id,
                            session_id,
                            for_update=True,
                        )
                    ).get("version")
                    or 0
                ),
            )
        item = dict(row)
        item["sdk_group_require_at_me"] = bool(item.get("sdk_group_require_at_me"))
        item["version"] = int(item.get("version") or 0)
        return item

    async def get_reply_policy_effect_status(
        self,
        db: AsyncConnection | AsyncSession,
        tenant_id: str,
        effect_idempotency_key: str,
    ) -> str:
        if not effect_idempotency_key:
            return "not_requested"
        result = await db.execute(
            text(
                "SELECT status FROM message_effect_intent "
                "WHERE tenant_id = :tid AND idempotency_key = :effect_key"
            ),
            {"tid": tenant_id, "effect_key": effect_idempotency_key},
        )
        status_value = result.scalar_one_or_none()
        return str(status_value or "missing")
