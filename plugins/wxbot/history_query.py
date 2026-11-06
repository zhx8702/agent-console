"""Bounded, store-backed history queries for the current conversation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.common.intent import is_local_history_lookup
from app.common.intent_runtime import decision_from_session


async def search_recorded_history(service: Any, session: Any, arguments: dict[str, Any]) -> dict:
    external = service._external_session_id(session)
    canonical = service._canonical_delivery_session_id(session, external_session_id=external)
    tenant = str(getattr(session, "tenant_id", "") or "").strip()
    if not tenant or not external.endswith("@chatroom"):
        raise ValueError("group_history_scope_unavailable")
    # Scope and requester identity come from the normalized session, never tool arguments.
    _turn, metadata = service._latest_user_context(session)
    decision = decision_from_session(session)
    history_intent = is_local_history_lookup(decision)
    self_only = arguments.get("sender_scope") == "self"
    if history_intent and decision.slots.get("sender_scope") == "self":
        self_only = True
    sender = str(arguments.get("sender_wxid") or "").strip()
    sender_name = str(arguments.get("sender_name") or "").strip()
    if self_only:
        sender = str(
            metadata.get("sender_wxid")
            or metadata.get("external_participant_id")
            or getattr(session, "external_participant_id", "")
            or ""
        ).strip()
        sender_name = ""
        if not sender or sender.startswith("cx1:"):
            raise ValueError("history_requester_identity_unavailable")

    timezone = "Asia/Shanghai"
    if service._social_policy_store is not None:
        document = await service._social_policy_store.get_group_policy(tenant, external)
        timezone = document.policy.timezone
    now = datetime.now(UTC)
    period = str(arguments.get("period") or "recent")
    if history_intent and decision.slots.get("period"):
        period = str(decision.slots["period"])
    if period not in {"today", "yesterday", "recent"}:
        raise ValueError("history_period_invalid")
    end = now
    if period in {"today", "yesterday"}:
        midnight = now.astimezone(ZoneInfo(timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight if period == "today" else midnight - timedelta(days=1)
        if period == "yesterday":
            end = midnight
    else:
        hours = max(1, min(int(arguments.get("hours") or 24), 336))
        start = now - timedelta(hours=hours)
    limit = max(1, min(int(arguments.get("limit") or 20), 20))
    result = await service._wxbot_store.search_recorded_group_messages(
        tenant_id=tenant,
        session_id=canonical,
        start_ts=int(start.timestamp()),
        end_ts=int(end.timestamp()),
        sender_wxid=sender,
        sender_name=sender_name,
        query=str(arguments.get("query") or "").strip()[:400],
        exclude_message_id=str(metadata.get("canonical_message_id") or getattr(_turn, "message_id", "") or ""),
        limit=limit,
    )
    return {
        "source": "recorded_group_observations",
        "scope": "current_group",
        "sender_scope": "self" if self_only else "filtered_group",
        "timezone": timezone,
        "from": start.astimezone(UTC).isoformat(),
        "until": end.astimezone(UTC).isoformat(),
        **result,
        "coverage": "captured_records_only",
        "note": "只覆盖本系统已采集的记录。未找到不代表没有发言；截断时不能声称列出了全部。",
    }
