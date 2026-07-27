from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.channel import (
    LEGACY_WXBOT_CONNECTION_ID,
    WECHAT_SDK_ADAPTER_ID,
    ChannelOutbound,
    ChannelRegistry,
    ChannelSendOptions,
    ChannelTarget,
    canonical_conversation_id,
)
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.types import Channel
from plugins.tibo_reset.client import TiboResetClient
from plugins.tibo_reset.store import TiboResetStore

logger = get_logger(__name__)
_PLUGIN_OWNER = "tibo_reset"
_DELIVERY_TTL_SECONDS = 5 * 60


def delivery_command_id(tenant_id: str, session_id: str, tweet_id: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\0{session_id}\0{tweet_id}".encode()).hexdigest()[:32]
    return f"tibo-reset:{tweet_id}:{digest}"


class TiboResetScopeGate(Protocol):
    async def __call__(
        self,
        owner: str,
        *,
        tenant_id: str,
        session_id: str,
    ) -> bool: ...


class TiboResetService:
    def __init__(
        self,
        *,
        store: TiboResetStore,
        client: TiboResetClient,
        outbound: ChannelOutbound,
        scope_execution_allowed: TiboResetScopeGate | None = None,
        channel_registry: ChannelRegistry | None = None,
        connection_id: str = "",
    ) -> None:
        self._store = store
        self._client = client
        self._outbound = outbound
        self._scope_execution_allowed = scope_execution_allowed
        self._channel_registry = channel_registry
        self._connection_id = str(connection_id or "").strip() or LEGACY_WXBOT_CONNECTION_ID

    async def _scope_allowed(self, tenant_id: str, session_id: str) -> bool:
        gate = self._scope_execution_allowed
        if gate is None:
            logger.error(
                "tibo_reset.scope_gate_missing",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            allowed = await gate(
                _PLUGIN_OWNER,
                tenant_id=tenant_id,
                session_id=session_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "tibo_reset.scope_gate_failed",
                tenant_id=tenant_id,
                session_id=session_id,
                error_class=exc.__class__.__name__,
            )
            return False
        if allowed is not True:
            logger.info(
                "tibo_reset.scope_execution_denied",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        return True

    def _outbound_for_target(self, target: ChannelTarget) -> ChannelOutbound:
        if self._channel_registry is not None:
            registered = self._channel_registry.outbound_for_target(target)
            if registered is not None:
                return registered
        return self._outbound

    async def poll_once(
        self, *, scope_limit: int = 500, per_scope_limit: int = 100
    ) -> dict[str, Any]:
        expired = {
            "delivery_count": 0,
            "dlq_count": 0,
            "sent_count": 0,
            "reply_count": 0,
        }
        try:
            expired = await self._store.expire_stale_queued(
                max_age_seconds=_DELIVERY_TTL_SECONDS,
            )
        except Exception:
            logger.exception("tibo_reset.delivery_expiry_failed")
        try:
            entries = await self._client.fetch_resets()
            ingest = await self._store.ingest_feed(entries)
        except Exception as exc:
            try:
                await self._store.mark_poll_failed(str(exc))
            except Exception:
                logger.exception("tibo_reset.poll_failure_persistence_failed")
            logger.warning("tibo_reset.poll_failed", error=str(exc))
            return {
                "status": "failed",
                "error": str(exc),
                "fetched": 0,
                "groups": 0,
                "queued": 0,
                "failed": 0,
                "dlq": int(expired.get("dlq_count") or 0),
                "settled_sent": int(expired.get("sent_count") or 0),
            }

        scopes = await self._store.list_enabled_scopes(limit=scope_limit)
        queued = 0
        failed = 0
        claimed = 0
        deferred = 0
        scope_denied = 0
        errors: list[dict[str, str]] = []

        for scope in scopes:
            tenant_id = str(scope.get("tenant_id") or "")
            session_id = str(scope.get("session_id") or "")
            enabled_at = str(scope.get("enabled_at") or "")
            if not tenant_id or not session_id or not enabled_at:
                continue
            if not await self._scope_allowed(tenant_id, session_id):
                scope_denied += 1
                continue
            candidates = await self._store.list_deliverable(
                tenant_id=tenant_id,
                session_id=session_id,
                enabled_at=enabled_at,
                limit=per_scope_limit,
            )
            for entry in candidates:
                # Recheck for every record so a tenant/session disable in the
                # middle of a large batch prevents any subsequent claim.
                if not await self._scope_allowed(tenant_id, session_id):
                    scope_denied += 1
                    break
                tweet_id = str(entry.get("tweet_id") or "")
                command_id = delivery_command_id(tenant_id, session_id, tweet_id)
                delivery = await self._store.claim_delivery(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    session_name=str(scope.get("session_name") or ""),
                    tweet_id=tweet_id,
                    command_id=command_id,
                )
                if delivery is None:
                    continue
                claimed += 1
                delivery_id = int(delivery["id"])
                trace_id = new_trace_id()
                try:
                    tweet_text = str(entry.get("text") or "")
                    source_url = str(entry.get("source_url") or "")
                    message_text = (
                        f"{tweet_text}\n\n原文: {source_url}" if source_url else tweet_text
                    )
                    # The claim may outlive a control-plane change. Revalidate
                    # immediately before outbound and defer the durable claim
                    # instead of sending under stale authority.
                    if not await self._scope_allowed(tenant_id, session_id):
                        await self._store.mark_delivery_failed(
                            delivery_id,
                            error="scope_execution_denied",
                        )
                        deferred += 1
                        continue
                    external_session_id = session_id
                    canonical_session_id = canonical_conversation_id(
                        self._connection_id,
                        external_session_id,
                    )
                    target = ChannelTarget(
                        tenant_id=tenant_id,
                        channel=Channel.WECHAT.value,
                        adapter_id=WECHAT_SDK_ADAPTER_ID,
                        connection_id=(
                            ""
                            if self._connection_id == LEGACY_WXBOT_CONNECTION_ID
                            else self._connection_id
                        ),
                        session_id=canonical_session_id,
                        external_conversation_id=external_session_id,
                        canonical_conversation_id=canonical_session_id,
                        session_name=str(scope.get("session_name") or ""),
                        session_kind="group",
                    )
                    result = await self._outbound_for_target(target).send_text(
                        target,
                        message_text,
                        ChannelSendOptions(
                            trace_id=trace_id,
                            mention_sender=False,
                            idempotency_key=command_id,
                            source_message={
                                "type": "tibo_reset_tweet",
                                "tweet_id": tweet_id,
                                "text": tweet_text,
                                "source_url": source_url,
                            },
                            delivery_metadata={
                                "source": "tibo_reset",
                                "speech_output_kind": "report",
                                "speech_class": "scheduled",
                                # This is an explicitly enabled, idempotent
                                # operational notification. It must not be
                                # dropped by the conversational speech budget.
                                "speech_budget_enabled": False,
                                "deferred_candidate": True,
                                "tweet_id": tweet_id,
                                "source_url": source_url,
                                "reset_type": str(entry.get("reset_type") or ""),
                                "beneficiaries": str(entry.get("beneficiaries") or ""),
                                "expires_at": (
                                    datetime.now(UTC) + timedelta(seconds=_DELIVERY_TTL_SECONDS)
                                ).isoformat(),
                            },
                        ),
                    )
                    if bool(result.metadata.get("suppressed")):
                        raise RuntimeError(
                            "tibo_reset_delivery_suppressed:"
                            f"{result.metadata.get('reason') or 'unknown'}"
                        )
                    reply_queue_id = int(result.metadata.get("reply_queue_id") or 0) or None
                    if reply_queue_id is None:
                        raise RuntimeError("tibo_reset_reply_queue_id_missing")
                    await self._store.mark_delivery_queued(
                        delivery_id,
                        reply_queue_id=reply_queue_id,
                    )
                    queued += 1
                except Exception as exc:
                    await self._store.mark_delivery_failed(delivery_id, error=str(exc))
                    failed += 1
                    errors.append(
                        {
                            "tenant_id": tenant_id,
                            "session_id": session_id,
                            "tweet_id": tweet_id,
                            "error": str(exc),
                        }
                    )
                    logger.warning(
                        "tibo_reset.delivery_failed",
                        tenant_id=tenant_id,
                        session_id=session_id,
                        tweet_id=tweet_id,
                        error=str(exc),
                    )

        return {
            "status": "completed" if failed == 0 else "partial",
            "fetched": len(entries),
            "ingest": ingest,
            "groups": len(scopes),
            "claimed": claimed,
            "queued": queued,
            "failed": failed,
            "dlq": int(expired.get("dlq_count") or 0),
            "settled_sent": int(expired.get("sent_count") or 0),
            "expired_replies": int(expired.get("reply_count") or 0),
            "deferred": deferred,
            "scope_denied": scope_denied,
            "errors": errors,
        }

    async def status(self) -> dict[str, Any]:
        return {
            **await self._store.runtime_status(),
            "stats": await self._store.reset_stats(),
            "api_url": self._client.api_url,
        }

    async def stats(self, *, timezone_name: str | None = None) -> dict[str, Any]:
        return await self._store.reset_stats(timezone_name=timezone_name)
