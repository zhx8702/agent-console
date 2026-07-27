from __future__ import annotations

from app.common.types import Channel
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookPoint
from plugins.group_activity.store import (
    GroupActivityStore,
    normalize_group_activity_identity,
)


class GroupActivityObserveHook:
    name = "group_activity.observe"
    point = HookPoint.AFTER_PREPROCESS
    priority = 95

    def __init__(self, store: GroupActivityStore) -> None:
        self._store = store

    async def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        channel = str(getattr(event.channel, "value", event.channel) or "")
        session_id = str(event.session_id or "")
        metadata = dict(event.metadata or {})
        if bool(metadata.get("is_self_sent")):
            return
        session_kind = str(metadata.get("session_kind") or "").lower()
        if channel != Channel.WECHAT.value:
            return
        if session_kind not in {"group", "chatroom"} and not session_id.endswith("@chatroom"):
            return
        if ctx.session is None:
            return
        session_name = str(metadata.get("session_name") or "").strip()
        connection_id = str(
            event.connection_id or metadata.get("connection_id") or ""
        )
        adapter_id = str(event.adapter_id or metadata.get("adapter_id") or "")
        identity: dict[str, str] | None = None
        external_candidates = dict.fromkeys(
            str(value or "").strip()
            for value in (
                event.external_conversation_id,
                metadata.get("external_conversation_id"),
                metadata.get("external_session_id"),
            )
        )
        for external_candidate in external_candidates:
            try:
                identity = normalize_group_activity_identity(
                    self._store.settings,
                    tenant_id=event.tenant_id,
                    session_id=session_id,
                    connection_id=connection_id,
                    adapter_id=adapter_id,
                    external_session_id=external_candidate,
                )
            except ValueError:
                continue
            break
        if identity is None:
            return

        adapter_id = identity["adapter_id"]
        connection_id = identity["connection_id"]
        external_session_id = identity["external_session_id"]
        identity_metadata = {
            "adapter_id": adapter_id,
            "connection_id": connection_id,
            "external_session_id": external_session_id,
            "external_conversation_id": external_session_id,
            "canonical_conversation_id": session_id,
        }
        event.adapter_id = adapter_id
        event.connection_id = connection_id
        event.external_conversation_id = external_session_id
        event.canonical_conversation_id = session_id
        event.metadata = {**metadata, **identity_metadata}

        session = ctx.session
        session.adapter_id = adapter_id
        session.connection_id = connection_id
        session.conversation_id = session_id
        session.external_conversation_id = external_session_id
        session.canonical_conversation_id = session_id
        session.metadata = {**dict(session.metadata or {}), **identity_metadata}
        await self._store.upsert_candidate(
            event.tenant_id,
            session_id,
            session_name=session_name,
            connection_id=connection_id,
            adapter_id=adapter_id,
            external_session_id=external_session_id,
        )
