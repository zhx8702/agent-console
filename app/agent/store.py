from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.agent.scopes import DEFAULT_AGENT_SCOPE, agent_scope_lookup_order, normalize_agent_scope
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema


async def _exec(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        if result.returns_rows:
            return [dict(row._mapping) for row in result.fetchall()]
        return []


def _normalize_allowed_tools(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = [item.strip() for item in raw.replace(",", "\n").splitlines()]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []

    items: list[str] = []
    seen: set[str] = set()
    for item in parsed:
        name = str(item or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        items.append(name)
    return items


def _to_json_text(value: Any, *, default: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return default


class AgentStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="agent store")

    async def get_session_policy(
        self,
        tenant_id: str,
        session_id: str,
        *,
        scope: str = DEFAULT_AGENT_SCOPE,
        available_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_scope = normalize_agent_scope(scope)
        rows = []
        stored_scope = normalized_scope
        for candidate_scope in agent_scope_lookup_order(normalized_scope):
            rows = await _exec(
                "SELECT tenant_id, session_id, scope, enabled, allowed_tools_json, updated_at "
                "FROM plugin_agent_session_policy "
                "WHERE tenant_id = :tid AND session_id = :sid AND scope = :scope",
                {"tid": tenant_id, "sid": session_id, "scope": candidate_scope},
            )
            if rows:
                stored_scope = candidate_scope
                break
        row = rows[0] if rows else None
        stored_tools = _normalize_allowed_tools((row or {}).get("allowed_tools_json"))
        catalog = list(available_tools or [])
        # Agent tools are group features, not a separate chat participation
        # gate.  An absent row therefore keeps the feature defaults enabled,
        # while an explicit row can disable the scope or narrow its tool set.
        # An empty allowlist is the persisted sentinel for inheriting every
        # tool currently available in the selected scope.
        enabled = bool((row or {}).get("enabled", True))
        inherits_default_tools = not bool(stored_tools)
        if not enabled:
            effective_tools = []
        elif inherits_default_tools:
            effective_tools = catalog
        else:
            effective_tools = [item for item in catalog if item in stored_tools]
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "scope": normalized_scope,
            "stored_scope": stored_scope if row else "",
            "enabled": enabled,
            "policy_configured": row is not None,
            "allowed_tools": stored_tools,
            "available_tools": catalog,
            "effective_tools": effective_tools,
            "inherits_default_tools": inherits_default_tools,
            "denial_reason": (
                ""
                if enabled
                else "policy_disabled"
            ),
            "updated_at": (row or {}).get("updated_at"),
        }

    async def set_session_policy(
        self,
        tenant_id: str,
        session_id: str,
        *,
        scope: str = DEFAULT_AGENT_SCOPE,
        enabled: bool | None = None,
        allowed_tools: list[str] | None = None,
        available_tools: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_scope = normalize_agent_scope(scope)
        current = await self.get_session_policy(
            tenant_id,
            session_id,
            scope=normalized_scope,
            available_tools=available_tools,
        )
        next_enabled = current["enabled"] if enabled is None else bool(enabled)
        next_tools = current["allowed_tools"] if allowed_tools is None else _normalize_allowed_tools(allowed_tools)
        await _exec(
            "INSERT INTO plugin_agent_session_policy "
            "(tenant_id, session_id, scope, enabled, allowed_tools_json, updated_at) "
            "VALUES (:tid, :sid, :scope, :enabled, :allowed_tools_json, NOW()) "
            "ON CONFLICT (tenant_id, session_id, scope) DO UPDATE SET "
            "enabled = EXCLUDED.enabled, "
            "allowed_tools_json = EXCLUDED.allowed_tools_json, "
            "updated_at = EXCLUDED.updated_at",
            {
                "tid": tenant_id,
                "sid": session_id,
                "scope": normalized_scope,
                "enabled": next_enabled,
                "allowed_tools_json": _to_json_text(next_tools, default="[]"),
            },
        )
        return await self.get_session_policy(
            tenant_id,
            session_id,
            scope=normalized_scope,
            available_tools=available_tools,
        )

    async def create_tool_audit(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        channel: str,
        scope: str,
        tool_name: str,
        tool_args: dict[str, Any],
        tool_result: Any,
        tool_error: str,
        latency_ms: int,
        trace_id: str,
        final_reply_text: str,
    ) -> int:
        rows = await _exec(
            "INSERT INTO plugin_agent_tool_audit "
            "(tenant_id, session_id, user_id, channel, scope, tool_name, tool_args_json, "
            "tool_result_json, tool_error, latency_ms, trace_id, final_reply_text) "
            "VALUES (:tenant_id, :session_id, :user_id, :channel, :scope, :tool_name, :tool_args_json, "
            ":tool_result_json, :tool_error, :latency_ms, :trace_id, :final_reply_text) "
            "RETURNING id",
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
                "channel": channel,
                "scope": normalize_agent_scope(scope),
                "tool_name": tool_name,
                "tool_args_json": _to_json_text(tool_args, default="{}")[:8000],
                "tool_result_json": _to_json_text(tool_result, default="")[:12000],
                "tool_error": str(tool_error or "")[:2000],
                "latency_ms": max(0, int(latency_ms or 0)),
                "trace_id": str(trace_id or "")[:128],
                "final_reply_text": str(final_reply_text or "")[:4000],
            },
        )
        return int(rows[0]["id"])

    async def list_tool_audits(
        self,
        tenant_id: str,
        *,
        session_id: str = "",
        scope: str = "",
        tool_name: str = "",
        trace_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["tenant_id = :tid"]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "lim": max(1, min(int(limit or 50), 200)),
        }
        if session_id.strip():
            clauses.append("session_id = :sid")
            params["sid"] = session_id.strip()
        if scope.strip():
            lookup_scopes = list(agent_scope_lookup_order(scope))
            clauses.append("scope = ANY(:scopes)")
            params["scopes"] = lookup_scopes
        if tool_name.strip():
            clauses.append("tool_name = :tool_name")
            params["tool_name"] = tool_name.strip()
        if trace_id.strip():
            clauses.append("trace_id = :trace_id")
            params["trace_id"] = trace_id.strip()
        rows = await _exec(
            "SELECT id, tenant_id, session_id, user_id, channel, scope, tool_name, "
            "tool_args_json, tool_result_json, tool_error, latency_ms, trace_id, final_reply_text, created_at "
            "FROM plugin_agent_tool_audit "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT :lim",
            params,
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["tool_args"] = json.loads(str(item.get("tool_args_json") or "{}"))
            except Exception:
                item["tool_args"] = {}
            try:
                item["tool_result"] = json.loads(str(item.get("tool_result_json") or "null"))
            except Exception:
                item["tool_result"] = None
            items.append(item)
        return items
