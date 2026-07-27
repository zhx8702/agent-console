from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.agent.scopes import (
    DEFAULT_AGENT_SCOPE,
    GROUP_PERSONAL_MAP_SCOPE,
    GROUP_PLUGIN_STATUS_SCOPE,
    agent_scope_disabled_reply,
    agent_scope_empty_reply,
    agent_scope_lookup_order,
    agent_scope_system_hint,
    normalize_agent_scope,
)
from app.agent.store import AgentStore
from app.agent.tools.group import (
    GroupAgentToolService,
    build_group_agent_tools,
    build_group_plugin_status_agent_tools,
    group_plugin_status_tool_catalog,
    group_tool_catalog,
)
from app.billing import BillingCoordinator, BillingReservation, BillingResource, BillingSubject
from app.common.config import Settings, get_settings
from app.common.context import get_trace_id
from app.common.conversation import render_turn as render_conversation_turn
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.prompting import augment_prompt_with_persona_and_memory, chat_system_prompt
from app.common.types import (
    CapabilityResult,
    Channel,
    ChatMessage,
    ChatRequest,
    ChatUsage,
    PreprocessedMessage,
    Role,
    RouteType,
    Session,
    ToolCall,
    ToolSchema,
    Turn,
    channel_id_value,
)
from app.llm.service import LLMService

log = get_logger(__name__)

_GROUP_MENTION_PREFIX_RE = re.compile(r"^\s*(?:@\S+[\s\u2005\u00a0]+)+")
DEFAULT_TOOL_OWNER_GATE_TIMEOUT_SECONDS = 1.0
MAX_TOOL_OWNER_GATE_TIMEOUT_SECONDS = 30.0

ToolOwnerGate = Callable[[str, Session], Awaitable[bool]]


def _bounded_tool_owner_gate_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_TOOL_OWNER_GATE_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = DEFAULT_TOOL_OWNER_GATE_TIMEOUT_SECONDS
    return min(timeout, MAX_TOOL_OWNER_GATE_TIMEOUT_SECONDS)


@dataclass
class _AgentTool:
    schema: ToolSchema
    handler: Any
    definition: AgentToolDefinition | None = None
    owner: str = "core"


class AgentCapabilityEngine:
    name = "agent"

    def __init__(
        self,
        llm_service: LLMService,
        *,
        settings: Settings | None = None,
        wxbot_tools: GroupAgentToolService | None = None,
        group_tools: GroupAgentToolService | None = None,
        agent_store: AgentStore | None = None,
        agent_tool_registry: AgentToolRegistry | None = None,
        billing: BillingCoordinator | None = None,
        history_turns: int = 8,
        max_tokens: int = 700,
        temperature: float = 0.2,
        tier: str = "tier-2",
        max_tool_rounds: int = 3,
        max_tool_calls_per_round: int = 3,
        tool_owner_gate: ToolOwnerGate | None = None,
        tool_owner_gate_timeout_seconds: float = DEFAULT_TOOL_OWNER_GATE_TIMEOUT_SECONDS,
    ) -> None:
        self._llm = llm_service
        self._settings = settings or get_settings()
        self._group_tools = group_tools or wxbot_tools
        self._agent_store = agent_store
        self._agent_tool_registry = agent_tool_registry
        self._billing = billing
        self._history_turns = history_turns
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._tier = tier
        self._max_tool_rounds = max_tool_rounds
        self._max_tool_calls_per_round = max_tool_calls_per_round
        self._tool_owner_gate = tool_owner_gate
        self._tool_owner_gate_timeout_seconds = _bounded_tool_owner_gate_timeout(
            tool_owner_gate_timeout_seconds
        )

    @property
    def tool_owner_gate(self) -> ToolOwnerGate | None:
        return self._tool_owner_gate

    @property
    def tool_owner_gate_timeout_seconds(self) -> float:
        return self._tool_owner_gate_timeout_seconds

    def set_tool_owner_gate(
        self,
        gate: ToolOwnerGate | None,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        """Replace the durable owner policy used by future tool calls."""

        self._tool_owner_gate = gate
        if timeout_seconds is not None:
            self._tool_owner_gate_timeout_seconds = _bounded_tool_owner_gate_timeout(
                timeout_seconds
            )

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        scope = normalize_agent_scope((hints or {}).get("agent_tool_scope"))
        model_tier, temperature = self._generation_config(hints)
        tools, policy = await self._available_tools(session, hints, pre)
        if not tools:
            if self._is_group_session(session):
                if policy.get("enabled") is False:
                    return CapabilityResult(
                        route=RouteType.AGENT,
                        reply_text=agent_scope_disabled_reply(scope),
                        tool_calls=[],
                        usage=ChatUsage(),
                        metadata={
                            "agent_tool_scope": scope,
                            "agent_disabled": True,
                            "effective_tools": [],
                            "tool_preselection_verdict": policy.get(
                                "tool_preselection_verdict", "LOW"
                            ),
                            "tool_preselection_selected": policy.get(
                                "tool_preselection_selected", []
                            ),
                        },
                    )
            return await self._fallback_chat(
                pre,
                session,
                model_tier=model_tier,
                temperature=temperature,
            )

        system_prompt = self._compose_system_prompt(session, scope)
        messages = self._build_messages(pre, session)
        aggregate_usage = ChatUsage()
        executed_tool_calls: list[ToolCall] = []
        final_response = None

        for round_index in range(self._max_tool_rounds):
            request = ChatRequest(
                tenant_id=session.tenant_id,
                trace_id=get_trace_id() or new_trace_id(),
                model_tier=model_tier,
                messages=messages,
                system=system_prompt,
                max_tokens=self._max_tokens,
                temperature=temperature,
                tools=[tool.schema for tool in tools.values()],
                cache_system=True,
                metadata={"agent_round": round_index + 1, "agent_scope": scope},
            )
            response = await self._llm.chat(request)
            final_response = response
            aggregate_usage.input_tokens += int(response.usage.input_tokens or 0)
            aggregate_usage.output_tokens += int(response.usage.output_tokens or 0)
            aggregate_usage.cache_read_tokens += int(response.usage.cache_read_tokens or 0)
            aggregate_usage.cache_write_tokens += int(response.usage.cache_write_tokens or 0)
            aggregate_usage.cost_usd += float(response.usage.cost_usd or 0.0)

            if not response.tool_calls:
                break

            tool_calls = list(response.tool_calls[: self._max_tool_calls_per_round])
            messages.append(
                ChatMessage(
                    role=Role.ASSISTANT,
                    content=response.content or "",
                    tool_calls=tool_calls,
                )
            )

            for tool_call in tool_calls:
                executed = await self._execute_tool_call(session, tool_call, tools)
                executed_tool_calls.append(executed)
                messages.append(
                    ChatMessage(
                        role=Role.TOOL,
                        tool_call_id=executed.id,
                        content=json.dumps(
                            {
                                "ok": executed.error is None,
                                "name": executed.name,
                                "result": self._tool_result_for_llm(executed.result),
                                "error": executed.error,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )

        if final_response is not None and final_response.tool_calls:
            auto_tool_call = await self._maybe_execute_auto_personal_map(
                pre,
                session,
                scope=scope,
                tools=tools,
                executed_tool_calls=executed_tool_calls,
            )
            if auto_tool_call is not None:
                executed_tool_calls.append(auto_tool_call)
                messages.append(
                    ChatMessage(
                        role=Role.TOOL,
                        tool_call_id=auto_tool_call.id,
                        content=json.dumps(
                            {
                                "ok": auto_tool_call.error is None,
                                "name": auto_tool_call.name,
                                "result": self._tool_result_for_llm(auto_tool_call.result),
                                "error": auto_tool_call.error,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )

        if final_response is not None and not final_response.tool_calls and not executed_tool_calls:
            fallback_tool_call = self._maybe_build_amap_search_after_address_prompt(
                pre,
                scope=scope,
                tools=tools,
                response_text=final_response.content,
            )
            if fallback_tool_call is not None:
                messages.append(
                    ChatMessage(
                        role=Role.ASSISTANT,
                        content="",
                        tool_calls=[fallback_tool_call],
                    )
                )
                executed = await self._execute_tool_call(session, fallback_tool_call, tools)
                executed_tool_calls.append(executed)
                messages.append(
                    ChatMessage(
                        role=Role.TOOL,
                        tool_call_id=executed.id,
                        content=json.dumps(
                            {
                                "ok": executed.error is None,
                                "name": executed.name,
                                "result": self._tool_result_for_llm(executed.result),
                                "error": executed.error,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                request = ChatRequest(
                    tenant_id=session.tenant_id,
                    trace_id=get_trace_id() or new_trace_id(),
                    model_tier=model_tier,
                    messages=messages,
                    system=system_prompt,
                    max_tokens=self._max_tokens,
                    temperature=temperature,
                    tools=[],
                    cache_system=True,
                    metadata={
                        "agent_round": self._max_tool_rounds + 1,
                        "agent_scope": scope,
                        "agent_final_synthesis": True,
                        "agent_fallback_search": True,
                    },
                )
                response = await self._llm.chat(request)
                final_response = response
                aggregate_usage.input_tokens += int(response.usage.input_tokens or 0)
                aggregate_usage.output_tokens += int(response.usage.output_tokens or 0)
                aggregate_usage.cache_read_tokens += int(response.usage.cache_read_tokens or 0)
                aggregate_usage.cache_write_tokens += int(response.usage.cache_write_tokens or 0)
                aggregate_usage.cost_usd += float(response.usage.cost_usd or 0.0)

        if (
            final_response is not None
            and final_response.tool_calls
            and not self._tool_calls_suppress_final_reply(executed_tool_calls)
        ):
            request = ChatRequest(
                tenant_id=session.tenant_id,
                trace_id=get_trace_id() or new_trace_id(),
                model_tier=model_tier,
                messages=messages,
                system=system_prompt,
                max_tokens=self._max_tokens,
                temperature=temperature,
                tools=[],
                cache_system=True,
                metadata={
                    "agent_round": self._max_tool_rounds + 1,
                    "agent_scope": scope,
                    "agent_final_synthesis": True,
                },
            )
            response = await self._llm.chat(request)
            final_response = response
            aggregate_usage.input_tokens += int(response.usage.input_tokens or 0)
            aggregate_usage.output_tokens += int(response.usage.output_tokens or 0)
            aggregate_usage.cache_read_tokens += int(response.usage.cache_read_tokens or 0)
            aggregate_usage.cache_write_tokens += int(response.usage.cache_write_tokens or 0)
            aggregate_usage.cost_usd += float(response.usage.cost_usd or 0.0)

        if final_response is None:
            return await self._fallback_chat(
                pre,
                session,
                model_tier=model_tier,
                temperature=temperature,
            )

        final_text = str(final_response.content or "").strip()
        suppress_final_reply = self._tool_calls_suppress_final_reply(executed_tool_calls)
        channel_reply_effects = self._collect_channel_reply_effects(
            executed_tool_calls,
            tools,
        )
        if suppress_final_reply:
            final_text = ""
        elif not final_text and executed_tool_calls:
            final_text = agent_scope_empty_reply(scope)
        else:
            final_text = self._sanitize_final_text(final_text)

        billing_operation = await self._settle_aggregated_agent_charge(
            session,
            scope=scope,
            tool_calls=executed_tool_calls,
        )

        await self._audit_tool_calls(
            session,
            executed_tool_calls,
            final_text,
            scope=scope,
        )

        return CapabilityResult(
            route=RouteType.AGENT,
            reply_text=final_text,
            tool_calls=executed_tool_calls,
            usage=aggregate_usage,
            metadata={
                "model": final_response.model,
                "latency_ms": final_response.latency_ms,
                "agent_tool_scope": scope,
                "tool_count": len(executed_tool_calls),
                "suppress_final_reply": suppress_final_reply,
                "suppress_outbound": suppress_final_reply,
                "skip_assistant_turn": suppress_final_reply,
                "channel_reply_effects": channel_reply_effects,
                "persona_profile": session.variables.get("persona_profile"),
                "effective_tools": policy.get("effective_tools") or [],
                "tool_preselection_verdict": policy.get("tool_preselection_verdict", "LOW"),
                "tool_preselection_selected": policy.get("tool_preselection_selected", []),
                "tool_preselection_scores": policy.get("tool_preselection_scores", {}),
                "agent_billing_operation": billing_operation,
            },
        )

    async def _fallback_chat(
        self,
        pre: PreprocessedMessage,
        session: Session,
        *,
        model_tier: str,
        temperature: float,
    ) -> CapabilityResult:
        request = ChatRequest(
            tenant_id=session.tenant_id,
            trace_id=get_trace_id() or new_trace_id(),
            model_tier=model_tier,
            messages=self._build_messages(pre, session),
            system=self._compose_system_prompt(session, None),
            max_tokens=self._max_tokens,
            temperature=temperature,
            cache_system=True,
        )
        response = await self._llm.chat(request)
        return CapabilityResult(
            route=RouteType.AGENT,
            reply_text=response.content,
            citations=list(response.citations),
            tool_calls=[],
            usage=response.usage,
            metadata={"model": response.model, "latency_ms": response.latency_ms},
        )

    async def _execute_tool_call(
        self,
        session: Session,
        tool_call: ToolCall,
        tools: dict[str, _AgentTool],
    ) -> ToolCall:
        started = time.monotonic()
        tool = tools.get(tool_call.name)
        if tool is None:
            return ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=dict(tool_call.arguments or {}),
                error=f"unsupported_tool:{tool_call.name}",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        if not await self._tool_owner_allowed(
            tool.owner,
            session,
            phase="execute",
        ):
            return ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=dict(tool_call.arguments or {}),
                error="tool_owner_execution_denied",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        try:
            result = await tool.handler(session, dict(tool_call.arguments or {}))
            # Tool handlers may contain long network or model calls.  Re-read
            # the durable owner policy after the handler settles so a scope
            # disabled in flight cannot publish its late result or deferred
            # effects into the rest of the agent pipeline.
            if not await self._tool_owner_allowed(
                tool.owner,
                session,
                phase="complete",
            ):
                return ToolCall(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments=dict(tool_call.arguments or {}),
                    error="tool_owner_execution_denied",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            return ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=dict(tool_call.arguments or {}),
                result=result,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:
            log.warning(
                "agent.tool_failed",
                session_id=session.session_id,
                tool_name=tool_call.name,
                error=str(exc),
            )
            return ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=dict(tool_call.arguments or {}),
                error=str(exc),
                latency_ms=int((time.monotonic() - started) * 1000),
            )

    async def _settle_aggregated_agent_charge(
        self,
        session: Session,
        *,
        scope: str,
        tool_calls: list[ToolCall],
    ) -> str:
        if self._billing is None or self._billing.provider("credits") is None:
            return ""
        operation = self._billing_operation_for_tool_calls(scope, tool_calls)
        if not operation:
            return ""
        tool_names = [item.name for item in tool_calls]
        reservation: BillingReservation | None = None
        try:
            reservation = await self._billing.reserve(
                BillingSubject(
                    tenant_id=session.tenant_id,
                    session_id=(
                        session.external_conversation_id or session.session_id
                    ),
                    user_id=str(session.user_id or ""),
                    display_name=self._sender_name(session),
                ),
                BillingResource(
                    kind="agent_tool",
                    operation=operation,
                    reference=str(get_trace_id() or ""),
                    metadata={
                        "agent_scope": scope,
                        "tool_names": tool_names,
                        "tool_count": len(tool_names),
                    },
                ),
            )
            if reservation.amount <= 0:
                return operation
            await self._billing.capture(reservation)
            return operation
        except Exception as exc:
            if reservation is not None:
                try:
                    await self._billing.release(reservation)
                except Exception as release_exc:
                    log.error(
                        "agent.billing_release_failed",
                        session_id=session.session_id,
                        operation=operation,
                        error_class=release_exc.__class__.__name__,
                    )
            log.warning(
                "agent.billing_aggregate_failed",
                session_id=session.session_id,
                operation=operation,
                error=str(exc),
            )
            return ""

    @staticmethod
    def _billing_operation_for_tool_calls(scope: str, tool_calls: list[ToolCall]) -> str:
        if normalize_agent_scope(scope) != GROUP_PERSONAL_MAP_SCOPE:
            return ""
        search_succeeded = False
        map_attempted = False
        for call in tool_calls:
            if call.name == "amap_create_personal_map":
                map_attempted = True
                if call.error or not AgentCapabilityEngine._tool_call_result_ok(call.result):
                    continue
                if AgentCapabilityEngine._amap_map_call_is_complex(call):
                    return "amap_route_map"
                return "amap_map"
            if call.name in {
                "amap_geo",
                "amap_text_search",
                "amap_regeo",
                "amap_place_detail",
                "amap_input_tips",
                "amap_around_search",
                "amap_route_plan",
                "amap_distance",
                "amap_weather",
                "amap_district",
                "amap_static_map",
                "amap_coordinate_convert",
                "amap_traffic_status",
                "amap_bus_info",
            }:
                if not call.error and AgentCapabilityEngine._tool_call_result_ok(call.result):
                    search_succeeded = True
        if map_attempted:
            return ""
        if search_succeeded:
            return "amap_search"
        return ""

    @staticmethod
    def _amap_map_call_is_complex(call: ToolCall) -> bool:
        result = call.result if isinstance(call.result, dict) else {}
        arguments = dict(call.arguments or {})
        try:
            scene_type = int(result.get("scene_type") or arguments.get("scene_type") or 0)
        except (TypeError, ValueError):
            scene_type = 0
        if scene_type in (1, 3):
            return True
        try:
            point_count = int(result.get("point_count") or 0)
        except (TypeError, ValueError):
            point_count = 0
        points = arguments.get("points")
        if point_count >= 5 or (isinstance(points, list) and len(points) >= 5):
            return True
        text = " ".join(
            str(value or "")
            for value in (
                result.get("map_name"),
                result.get("line_title"),
                arguments.get("map_name"),
                arguments.get("line_title"),
            )
        )
        return bool(re.search(r"(一日游|多日游|路线|行程|旅游|旅行|打卡|多点)", text))

    @staticmethod
    def _tool_call_result_ok(result: Any) -> bool:
        if not isinstance(result, dict):
            return result is not None
        return not bool(result.get("error") or result.get("upstream_error"))

    @staticmethod
    def _sender_name(session: Session) -> str:
        for turn in reversed(session.turns):
            if turn.role == Role.USER:
                metadata = dict(turn.metadata or {})
                return str(metadata.get("sender_name") or metadata.get("sender_wxid") or "").strip()
        return ""

    @staticmethod
    def _tool_calls_suppress_final_reply(tool_calls: list[ToolCall]) -> bool:
        for item in tool_calls:
            result = item.result
            if not isinstance(result, dict):
                continue
            if bool(
                result.get("suppress_final_reply")
                or result.get("suppress_outbound")
                or result.get("self_enqueued_reply")
            ):
                return True
        return False

    @staticmethod
    def _collect_channel_reply_effects(
        tool_calls: list[ToolCall],
        tools: dict[str, _AgentTool],
    ) -> list[dict[str, Any]]:
        effects: list[dict[str, Any]] = []
        for item in tool_calls:
            result = item.result
            if not isinstance(result, dict):
                continue
            raw_effects = result.get("channel_reply_effects")
            if not isinstance(raw_effects, list):
                continue
            tool = tools.get(item.name)
            producer_owner = str(getattr(tool, "owner", "") or "core").strip()
            for raw in raw_effects:
                if isinstance(raw, dict):
                    effect = dict(raw)
                    # Never trust a handler-supplied producer identity.  Bind
                    # deferred effects to the owner recorded by the agent tool
                    # registry so later dispatch can revalidate both the
                    # initiating plugin and the channel handler.
                    effect["producer_owner"] = producer_owner
                    effects.append(effect)
        return effects

    @staticmethod
    def _tool_result_for_llm(result: Any) -> Any:
        if not isinstance(result, dict):
            return result
        value = dict(result)
        value.pop("channel_reply_effects", None)
        return value

    @staticmethod
    def _sanitize_final_text(text: str) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        replacements = {
            "我靠": "",
            "卧槽": "",
            "卧艹": "",
            "艹": "",
            "寄了": "失败了",
            "没生成成": "没有生成成功",
            "第一把": "这次",
        }
        for old, new in replacements.items():
            value = value.replace(old, new)
        value = re.sub(r"^[，,、。！？!?\s]+", "", value).strip()
        return value

    async def _maybe_execute_auto_personal_map(
        self,
        pre: PreprocessedMessage,
        session: Session,
        *,
        scope: str,
        tools: dict[str, _AgentTool],
        executed_tool_calls: list[ToolCall],
    ) -> ToolCall | None:
        if normalize_agent_scope(scope) != GROUP_PERSONAL_MAP_SCOPE:
            return None
        if "amap_create_personal_map" not in tools:
            return None
        if any(item.name == "amap_create_personal_map" for item in executed_tool_calls):
            return None
        text = f"{pre.original_text or ''}\n{pre.cleaned_text or ''}"
        if not self._explicit_map_generation_requested(text):
            return None
        points = self._collect_personal_map_points(
            executed_tool_calls, limit=self._requested_point_limit(text)
        )
        if not points:
            return None
        map_name = self._personal_map_name(pre)
        tool_call = ToolCall(
            id=f"auto_amap_create_personal_map_{len(executed_tool_calls) + 1}",
            name="amap_create_personal_map",
            arguments={
                "map_name": map_name,
                "line_title": map_name,
                "points": points,
                "scene_type": 2,
                "send_to_group": True,
            },
        )
        return await self._execute_tool_call(session, tool_call, tools)

    @staticmethod
    def _explicit_map_generation_requested(text: str) -> bool:
        value = str(text or "")
        return bool(
            re.search(r"(生成|创建|做成|标记到|整理成).{0,8}(高德)?地图", value)
            or re.search(r"(高德)?地图.{0,8}(二维码|分享)", value)
            or re.search(r"(打卡地图|路线地图|地图二维码|生成二维码)", value)
        )

    @staticmethod
    def _requested_point_limit(text: str) -> int:
        match = re.search(
            r"(?:包含|含|找|选|规划)\s*(\d{1,2})\s*(?:个|家|处)?(?:点|地点|店|景点)?", text
        )
        if not match:
            return 5
        try:
            return max(1, min(16, int(match.group(1))))
        except ValueError:
            return 5

    @staticmethod
    def _personal_map_name(pre: PreprocessedMessage) -> str:
        text = str(pre.cleaned_text or pre.original_text or "高德地图").strip()
        text = _GROUP_MENTION_PREFIX_RE.sub("", text, count=1).strip()
        text = re.sub(r"(生成|创建|做成)?\s*(高德)?地图(二维码)?", "", text).strip(" ，,。")
        text = re.sub(r"\s+", "", text)
        return (text[:22] or "高德地图") + "地图"

    @classmethod
    def _collect_personal_map_points(
        cls, tool_calls: list[ToolCall], *, limit: int
    ) -> list[dict[str, Any]]:
        primary: list[dict[str, Any]] = []
        fallback: list[dict[str, Any]] = []
        seen: set[str] = set()
        for call in tool_calls:
            result = call.result
            if not isinstance(result, dict):
                continue
            raw_items = result.get("items")
            if isinstance(raw_items, list):
                candidates = raw_items
            else:
                candidates = [result]
            for item in candidates:
                point = cls._personal_map_point(item)
                if point is None:
                    continue
                key = str(point.get("poi_id") or "") or (
                    f"{point['name']}:{point['longitude']:.6f},{point['latitude']:.6f}"
                )
                if key in seen:
                    continue
                seen.add(key)
                if cls._looks_like_non_route_poi(point["name"]):
                    fallback.append(point)
                else:
                    primary.append(point)
                if len(primary) >= limit:
                    return primary[:limit]
        points = primary + fallback
        return points[:limit]

    @staticmethod
    def _personal_map_point(item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or item.get("formatted_address") or "").strip()
        if not name:
            return None
        try:
            longitude = float(item.get("longitude"))
            latitude = float(item.get("latitude"))
        except (TypeError, ValueError):
            return None
        return {
            "name": name,
            "longitude": longitude,
            "latitude": latitude,
            "poi_id": str(item.get("poi_id") or "").strip(),
            "address": str(item.get("address") or item.get("formatted_address") or "").strip(),
        }

    @staticmethod
    def _looks_like_non_route_poi(name: str) -> bool:
        return bool(
            re.search(r"(酒店|公寓|宾馆|地铁站|停车场|停车|入口|出口|公交站)$", str(name or ""))
        )

    @classmethod
    def _maybe_build_amap_search_after_address_prompt(
        cls,
        pre: PreprocessedMessage,
        *,
        scope: str,
        tools: dict[str, _AgentTool],
        response_text: str,
    ) -> ToolCall | None:
        if normalize_agent_scope(scope) != GROUP_PERSONAL_MAP_SCOPE:
            return None
        if "amap_text_search" not in tools:
            return None
        if not cls._looks_like_amap_address_prompt(response_text):
            return None
        query_text = str(pre.cleaned_text or pre.original_text or "").strip()
        if not cls._looks_like_amap_place_query(query_text):
            return None
        keywords = cls._amap_fallback_keywords(query_text)
        if not keywords:
            return None
        arguments: dict[str, Any] = {"keywords": keywords, "limit": 10}
        city = cls._amap_fallback_city(query_text)
        if city:
            arguments["city"] = city
        return ToolCall(
            id="auto_amap_text_search_1",
            name="amap_text_search",
            arguments=arguments,
        )

    @staticmethod
    def _looks_like_amap_address_prompt(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return False
        return bool(
            re.search(
                r"(请|麻烦|需要|提供|告诉|补充).{0,12}(地址|位置|地点|起点|终点|目的地|当前位置)",
                value,
            )
            or re.search(
                r"(地址|位置|地点|起点|终点|目的地|当前位置).{0,12}(是什么|是哪里|在哪|提供|告诉)",
                value,
            )
            or re.search(r"(要|想|准备).{0,8}(查|找|去).{0,8}(哪里|哪儿|哪个地方)", value)
        )

    @staticmethod
    def _looks_like_amap_place_query(text: str) -> bool:
        return bool(
            re.search(
                r"(地点|附近|周边|餐厅|景点|咖啡|商场|导航|路线|旅游|打卡|地图|位置|地址|在哪|哪里|怎么走|怎么去|楼栋|公司|酒店|美食|店|公园|医院|学校|大厦|广场|机场|火车站|地铁站)",
                str(text or ""),
            )
        )

    @classmethod
    def _amap_fallback_keywords(cls, text: str) -> str:
        value = _GROUP_MENTION_PREFIX_RE.sub("", str(text or "").strip(), count=1)
        replacements = [
            r"(@\S+)",
            r"(帮我|帮忙|麻烦|请|查一下|查询|找一下|找找|搜索|看一下|问一下|告诉我)",
            r"(高德地图|高德|地图二维码|二维码)",
            r"(生成|创建|做成|分享)",
            r"(的)?(具体)?(位置|地址)",
            r"(精确到楼栋|精确到楼|在哪里|在哪儿|在哪|哪里|怎么走|怎么去|怎么到|如何去|导航|路线)",
            r"(附近|周边|有几家|几家|多少家|有哪些|有什么)",
        ]
        for pattern in replacements:
            value = re.sub(pattern, " ", value)
        value = re.sub(r"[，,。！!？?；;：:\[\]【】]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip(" -_/,")
        return value[:80]

    @staticmethod
    def _amap_fallback_city(text: str) -> str:
        value = str(text or "")
        parenthesized = re.search(r"[（(]([一-鿿]{2,8})(?:市)?[）)]", value)
        if parenthesized:
            return parenthesized.group(1)
        city_match = re.search(r"([一-鿿]{2,8}市)", value)
        if city_match:
            return city_match.group(1)
        for city in (
            "北京",
            "上海",
            "广州",
            "深圳",
            "武汉",
            "长沙",
            "杭州",
            "南京",
            "成都",
            "重庆",
            "西安",
            "苏州",
        ):
            if city in value:
                return city
        return ""

    def _generation_config(
        self,
        hints: dict[str, Any] | None,
    ) -> tuple[str, float]:
        """Resolve trusted per-run generation settings with fail-closed bounds."""

        raw_tier = str((hints or {}).get("_llm_model_tier") or "").strip().lower()
        model_tier = raw_tier if raw_tier in {"tier-1", "tier-2", "tier-3"} else self._tier
        raw_temperature = (hints or {}).get("_llm_temperature")
        try:
            candidate_temperature = float(raw_temperature)
        except (TypeError, ValueError):
            candidate_temperature = self._temperature
        temperature = (
            candidate_temperature if 0.0 <= candidate_temperature <= 2.0 else self._temperature
        )
        return model_tier, temperature

    async def _available_tools(
        self,
        session: Session,
        hints: dict[str, Any] | None,
        pre: PreprocessedMessage | None = None,
    ) -> tuple[dict[str, _AgentTool], dict[str, Any]]:
        if not self._is_group_session(session):
            return {}, {}
        scope = normalize_agent_scope((hints or {}).get("agent_tool_scope"))
        scope_definitions = self._resolve_scope_definitions(scope)
        definitions = self._filter_definitions_for_session(
            scope_definitions,
            session,
        )
        if not definitions:
            if scope_definitions:
                return {}, {
                    "enabled": False,
                    "policy_configured": False,
                    "effective_tools": [],
                    "available_tools": [item.name for item in scope_definitions],
                    "denial_reason": "requester_role_required",
                }
            return {}, {"enabled": True, "effective_tools": []}
        definitions = await self._filter_definitions_for_owner_gate(
            definitions,
            session,
        )
        if not definitions:
            return {}, {
                "enabled": True,
                "policy_configured": False,
                "effective_tools": [],
                "available_tools": [],
                "denial_reason": "tool_owner_execution_denied",
            }
        tools = {
            item.name: _AgentTool(
                schema=ToolSchema(
                    name=item.name,
                    description=item.description,
                    parameters=dict(item.parameters or {}),
                ),
                handler=item.handler,
                definition=item,
                owner=self._definition_owner(item),
            )
            for item in definitions
        }
        if self._agent_store is None:
            if not self._settings.agent_tools_require_explicit_policy:
                policy = {
                    "enabled": True,
                    "policy_configured": False,
                    "effective_tools": list(tools.keys()),
                    "available_tools": list(tools.keys()),
                    "development_override": True,
                }
                return self._preselect_tools(tools, policy, pre)
            return {}, {
                "enabled": False,
                "policy_configured": False,
                "effective_tools": [],
                "available_tools": list(tools.keys()),
                "denial_reason": "policy_store_unavailable",
            }
        policy = await self._agent_store.get_session_policy(
            session.tenant_id,
            session.session_id,
            scope=scope,
            available_tools=list(tools.keys()),
        )
        if not bool(policy.get("enabled", True)):
            return {}, policy
        effective_tools = set(policy.get("effective_tools") or [])
        tools = {name: tool for name, tool in tools.items() if name in effective_tools}
        policy = dict(policy)
        policy["effective_tools"] = list(tools.keys())
        return self._preselect_tools(tools, policy, pre)

    async def _filter_definitions_for_owner_gate(
        self,
        definitions: list[AgentToolDefinition],
        session: Session,
    ) -> list[AgentToolDefinition]:
        owners = sorted(
            {owner for item in definitions if (owner := self._definition_owner(item)) != "core"}
        )
        if not owners:
            return definitions
        if self._tool_owner_gate is None:
            log.warning(
                "agent.tool_owner_gate_denied",
                owners=owners,
                session_id=session.session_id,
                phase="expose",
                reason="missing_gate",
            )
            return [
                item
                for item in definitions
                if self._definition_owner(item) == "core"
            ]
        decisions = await asyncio.gather(
            *(self._tool_owner_allowed(owner, session, phase="expose") for owner in owners)
        )
        allowed = dict(zip(owners, decisions, strict=True))
        return [
            item
            for item in definitions
            if (owner := self._definition_owner(item)) == "core" or allowed.get(owner, False)
        ]

    async def _tool_owner_allowed(
        self,
        owner: str,
        session: Session,
        *,
        phase: str,
    ) -> bool:
        normalized_owner = str(owner or "").strip() or "core"
        if normalized_owner == "core":
            return True
        if self._tool_owner_gate is None:
            log.warning(
                "agent.tool_owner_gate_denied",
                owner=normalized_owner,
                session_id=session.session_id,
                phase=phase,
                reason="missing_gate",
            )
            return False
        try:
            result = await asyncio.wait_for(
                self._tool_owner_gate(normalized_owner, session),
                timeout=self._tool_owner_gate_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            log.warning(
                "agent.tool_owner_gate_denied",
                owner=normalized_owner,
                session_id=session.session_id,
                phase=phase,
                reason="timeout",
            )
            return False
        except Exception as exc:
            log.warning(
                "agent.tool_owner_gate_denied",
                owner=normalized_owner,
                session_id=session.session_id,
                phase=phase,
                reason="error",
                error_class=exc.__class__.__name__,
            )
            return False
        if isinstance(result, bool):
            return result
        log.warning(
            "agent.tool_owner_gate_denied",
            owner=normalized_owner,
            session_id=session.session_id,
            phase=phase,
            reason="invalid_result",
        )
        return False

    def _preselect_tools(
        self,
        tools: dict[str, _AgentTool],
        policy: dict[str, Any],
        pre: PreprocessedMessage | None,
    ) -> tuple[dict[str, _AgentTool], dict[str, Any]]:
        normalized_policy = dict(policy)
        query = self._preselection_query_text(pre)
        scores = {
            name: self._score_tool_for_preselection(tool.definition, query)
            for name, tool in tools.items()
        }
        metadata_tools = {
            name
            for name, tool in tools.items()
            if self._definition_has_preselection_metadata(tool.definition)
        }
        if not query or not metadata_tools:
            normalized_policy.update(
                {
                    "effective_tools": list(tools.keys()),
                    "tool_preselection_verdict": "LOW",
                    "tool_preselection_selected": list(tools.keys()),
                    "tool_preselection_scores": scores,
                }
            )
            return tools, normalized_policy

        positive = {name: score for name, score in scores.items() if score > 0}
        if not positive:
            normalized_policy.update(
                {
                    "effective_tools": list(tools.keys()),
                    "tool_preselection_verdict": "LOW",
                    "tool_preselection_selected": list(tools.keys()),
                    "tool_preselection_scores": scores,
                }
            )
            return tools, normalized_policy

        highest = max(positive.values())
        selected_names = [name for name, score in positive.items() if score == highest]
        if highest >= 2 and len(selected_names) == 1:
            verdict = "CLEAR"
        elif len(selected_names) > 1:
            verdict = "AMBIGUOUS"
        else:
            verdict = "INSUFFICIENT"
        selected = {name: tools[name] for name in selected_names}
        normalized_policy.update(
            {
                "effective_tools": selected_names,
                "tool_preselection_verdict": verdict,
                "tool_preselection_selected": selected_names,
                "tool_preselection_scores": scores,
            }
        )
        return selected, normalized_policy

    @staticmethod
    def _preselection_query_text(pre: PreprocessedMessage | None) -> str:
        if pre is None:
            return ""
        return str(pre.cleaned_text or pre.original_text or "").strip().lower()

    @classmethod
    def _score_tool_for_preselection(
        cls, definition: AgentToolDefinition | None, query: str
    ) -> int:
        if definition is None or not query:
            return 0
        score = 0
        metadata = dict(definition.metadata or {})
        for field in ("embed_text", "tree_text", "verb_type"):
            text = (
                str(getattr(definition, field, None) or metadata.get(field) or "").strip().lower()
            )
            if not text:
                continue
            if text in query or query in text:
                score += 2 if field in {"embed_text", "tree_text"} else 1
                continue
            tokens = [item for item in re.split(r"[\s,，/|;；]+", text) if item]
            if any(item in query for item in tokens):
                score += 1
        for item in cls._definition_list_value(definition, metadata, "required_params"):
            if item.lower() in query:
                score += 1
        for item in cls._definition_list_value(definition, metadata, "scopes"):
            if item.lower() in query:
                score += 1
        return score

    @classmethod
    def _definition_has_preselection_metadata(cls, definition: AgentToolDefinition | None) -> bool:
        if definition is None:
            return False
        metadata = dict(definition.metadata or {})
        if any(
            str(getattr(definition, field, None) or metadata.get(field) or "").strip()
            for field in ("embed_text", "tree_text", "verb_type")
        ):
            return True
        return bool(
            cls._definition_list_value(definition, metadata, "required_params")
            or cls._definition_list_value(definition, metadata, "scopes")
        )

    @staticmethod
    def _definition_list_value(
        definition: AgentToolDefinition,
        metadata: dict[str, Any],
        key: str,
    ) -> list[str]:
        value = getattr(definition, key, None)
        if value is None:
            value = metadata.get(key)
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            return []
        return [str(item).strip() for item in values if str(item).strip()]

    def _resolve_scope_definitions(self, scope: str):
        if self._agent_tool_registry is not None:
            merged: list[AgentToolDefinition] = []
            seen: set[str] = set()
            for candidate_scope in agent_scope_lookup_order(scope):
                for item in self._agent_tool_registry.list_tools(candidate_scope):
                    if item.name in seen:
                        continue
                    seen.add(item.name)
                    merged.append(item)
            if merged:
                return merged
        if self._group_tools is not None and scope == DEFAULT_AGENT_SCOPE:
            return build_group_agent_tools(self._group_tools)
        if self._group_tools is not None and scope == GROUP_PLUGIN_STATUS_SCOPE:
            return build_group_plugin_status_agent_tools(self._group_tools)
        return []

    @staticmethod
    def _definition_owner(definition: AgentToolDefinition) -> str:
        metadata = dict(definition.metadata or {})
        return (
            str(metadata.get("owner") or metadata.get("source_plugin") or "core").strip() or "core"
        )

    @classmethod
    def _filter_definitions_for_session(
        cls,
        definitions: list[AgentToolDefinition],
        session: Session,
    ) -> list[AgentToolDefinition]:
        return [item for item in definitions if cls._definition_matches_session(item, session)]

    @classmethod
    def _definition_matches_session(cls, definition: AgentToolDefinition, session: Session) -> bool:
        metadata = dict(definition.metadata or {})
        allowed_channels = cls._metadata_string_set(metadata, "channels")
        if allowed_channels and cls._session_channel(session) not in allowed_channels:
            return False
        allowed_session_kinds = cls._metadata_string_set(metadata, "session_kinds")
        if allowed_session_kinds and cls._session_kind(session) not in allowed_session_kinds:
            return False
        required_group_role = str(metadata.get("required_group_role") or "").strip().lower()
        if required_group_role:
            requester_roles = cls._requester_group_roles(session)
            if required_group_role == "admin":
                if not requester_roles.intersection({"admin", "owner", "group_admin"}):
                    return False
            elif required_group_role not in requester_roles:
                return False
        return True

    @staticmethod
    def _metadata_string_set(metadata: dict[str, Any], key: str) -> set[str]:
        value = metadata.get(key)
        if isinstance(value, str):
            items = [value]
        elif isinstance(value, (list, tuple, set)):
            items = list(value)
        else:
            return set()
        return {str(item or "").strip().lower() for item in items if str(item or "").strip()}

    @staticmethod
    def _session_channel(session: Session) -> str:
        raw_channel = getattr(session, "channel", "")
        return str(getattr(raw_channel, "value", raw_channel) or "").strip().lower()

    @classmethod
    def _session_kind(cls, session: Session) -> str:
        metadata = dict(getattr(session, "metadata", {}) or {})
        kind = str(metadata.get("session_kind") or metadata.get("kind") or "").strip().lower()
        if kind in {"group", "chatroom", "channel", "guild"}:
            return "group"
        if kind in {"private", "dm", "direct"}:
            return "private"
        session_id = str(getattr(session, "session_id", "") or "")
        if cls._session_channel(session) == Channel.WECHAT.value and session_id.endswith(
            "@chatroom"
        ):
            return "group"
        return "private"

    @classmethod
    def _requester_group_roles(cls, session: Session) -> set[str]:
        metadata_sources: list[dict[str, Any]] = [
            dict(getattr(session, "metadata", {}) or {}),
        ]
        for turn in reversed(list(getattr(session, "turns", []) or [])):
            if getattr(turn, "role", None) != Role.USER:
                continue
            metadata_sources.append(dict(getattr(turn, "metadata", {}) or {}))
            break

        roles: set[str] = set()
        for metadata in metadata_sources:
            for key in ("requester_roles", "sender_roles", "group_roles"):
                roles.update(cls._metadata_string_set(metadata, key))
            raw_role = (
                str(
                    metadata.get("requester_role")
                    or metadata.get("sender_role")
                    or metadata.get("group_role")
                    or ""
                )
                .strip()
                .lower()
            )
            if raw_role:
                roles.add(raw_role)
            if bool(metadata.get("sender_is_group_admin")):
                roles.add("group_admin")
            if bool(metadata.get("sender_is_group_owner")):
                roles.add("owner")
        return roles

    @classmethod
    def _is_group_session(cls, session: Session) -> bool:
        return cls._session_kind(session) == "group"

    async def _audit_tool_calls(
        self,
        session: Session,
        tool_calls: list[ToolCall],
        final_reply_text: str,
        *,
        scope: str,
    ) -> None:
        if self._agent_store is None or not tool_calls:
            return
        try:
            for item in tool_calls:
                await self._agent_store.create_tool_audit(
                    tenant_id=session.tenant_id,
                    session_id=session.session_id,
                    user_id=str(session.user_id or ""),
                    channel=channel_id_value(session.channel),
                    scope=scope,
                    tool_name=item.name,
                    tool_args=dict(item.arguments or {}),
                    tool_result=item.result,
                    tool_error=str(item.error or ""),
                    latency_ms=int(item.latency_ms or 0),
                    trace_id=str(get_trace_id() or ""),
                    final_reply_text=final_reply_text,
                )
        except Exception as exc:
            log.warning(
                "agent.audit_failed",
                session_id=session.session_id,
                error=str(exc),
            )

    @classmethod
    def tool_catalog(cls, scope: str = DEFAULT_AGENT_SCOPE) -> list[dict[str, str]]:
        normalized = normalize_agent_scope(scope)
        if normalized == DEFAULT_AGENT_SCOPE:
            return group_tool_catalog()
        if normalized == GROUP_PLUGIN_STATUS_SCOPE:
            return group_plugin_status_tool_catalog()
        return []

    def _compose_system_prompt(self, session: Session, scope: str | None) -> str:
        base_system = chat_system_prompt(self._settings.customer_service_prompt_enabled)
        return augment_prompt_with_persona_and_memory(
            (base_system + "\n\n" + agent_scope_system_hint(scope)),
            session,
            memory_intro=(
                "以下是当前用户的历史记忆，请把它当作个性化上下文使用。"
                "但你回答的群事实必须以工具查询结果为准："
            ),
            memory_budget_chars=self._settings.memory_retrieval_budget_chars,
        )

    def _build_messages(self, pre: PreprocessedMessage, session: Session) -> list[ChatMessage]:
        messages: list[ChatMessage] = []
        current_trace_id = get_trace_id()
        history_turns = (
            max(self._history_turns, 20) if self._is_group_session(session) else self._history_turns
        )
        recent = session.turns[-history_turns:]
        current_turn = next(
            (
                turn
                for turn in reversed(recent)
                if turn.role == Role.USER and turn.trace_id and turn.trace_id == current_trace_id
            ),
            None,
        )
        for turn in recent:
            if turn.role in (Role.USER, Role.ASSISTANT):
                if turn.role == Role.USER and turn.trace_id and turn.trace_id == current_trace_id:
                    continue
                messages.append(
                    ChatMessage(
                        role=turn.role,
                        content=self._render_turn_content(session, turn, current=False),
                    )
                )
        messages.append(
            ChatMessage(
                role=Role.USER,
                content=(
                    self._render_turn_content(session, current_turn, current=True)
                    if current_turn is not None
                    else self._render_current_user_input(session, pre)
                ),
            )
        )
        return messages

    def _render_turn_content(self, session: Session, turn: Turn, *, current: bool) -> str:
        return render_conversation_turn(session, turn, current=current)

    def _render_current_user_input(self, session: Session, pre: PreprocessedMessage) -> str:
        content = str(pre.cleaned_text or pre.original_text or "").strip()
        if not content:
            return ""
        if not self._is_group_session(session):
            return content
        speaker = str(session.user_id or "当前发言人").strip()
        return f"当前发言人[{speaker}]：{content}"
