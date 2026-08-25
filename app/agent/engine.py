from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.agent.scopes import (
    DEFAULT_AGENT_SCOPE,
    FILE_ANALYSIS_SCOPE,
    GROUP_PERSONAL_MAP_SCOPE,
    GROUP_PLUGIN_STATUS_SCOPE,
    MESSAGE_EXPORT_SCOPE,
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
    Citation,
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
MIN_CLEAR_TOOL_PRESELECTION_SCORE = 2
MIN_CLEAR_TOOL_PRESELECTION_MARGIN = 2
AMBIGUOUS_TOOL_PRESELECTION_TOP_K = 3
MAX_REQUIRED_MESSAGE_EXPORT_MINUTES = 24 * 60
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

    async def preview_availability(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the final tool exposure decision without invoking the LLM or a tool."""

        tools, policy = await self._available_tools(session, hints, pre)
        effective_tools = list(tools.keys())
        return {
            "effective_tool_count": len(effective_tools),
            "policy_allowed": bool(policy.get("enabled", True)),
            "denial_reason": str(policy.get("denial_reason") or ""),
            "effective_tools": effective_tools,
            "tool_preselection_verdict": str(policy.get("tool_preselection_verdict") or "LOW"),
        }

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        scope = normalize_agent_scope((hints or {}).get("agent_tool_scope"))
        required_effect = self._required_agent_effect(hints, scope)
        required_web_search = required_effect.get("web_search_required") is True
        model_tier, temperature = self._generation_config(hints)
        tools, policy = await self._available_tools(session, hints, pre)
        if not tools:
            if required_effect:
                return CapabilityResult(
                    route=RouteType.AGENT,
                    reply_text="当前会话的文件生成或发送能力暂时不可用，请稍后再试。",
                    tool_calls=[],
                    usage=ChatUsage(),
                    metadata={
                        "agent_tool_scope": scope,
                        "effective_tools": [],
                        "required_effect": required_effect["type"],
                        "required_effect_satisfied": False,
                        "required_effect_failure": "required_tool_unavailable",
                    },
                )
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

        if required_web_search and not self._required_web_search_configured():
            return self._required_web_search_failure_result(
                scope=scope,
                required_effect=required_effect,
                policy=policy,
                reason="required_web_search_not_configured",
            )

        system_prompt = self._compose_system_prompt(session, scope)
        if required_web_search:
            system_prompt += (
                "\n\n本轮用户明确要求实时联网搜索。必须先使用联网搜索取得当前资料，"
                "再按标题、摘要和可访问的来源链接整理正文；不得使用模型记忆冒充实时结果。"
            )
        messages = self._build_messages(pre, session)
        aggregate_usage = ChatUsage()
        executed_tool_calls: list[ToolCall] = []
        final_response = None
        terminal_tool_effect = False
        required_web_search_timeout_seconds = (
            min(
                self._settings.agent_required_web_search_timeout_seconds,
                max(1.0, self._settings.orchestrator_handle_timeout_seconds - 15.0),
            )
            if required_web_search
            else 0.0
        )
        required_web_search_deadline = (
            time.monotonic() + required_web_search_timeout_seconds if required_web_search else 0.0
        )
        round_limit = (
            self._settings.agent_required_web_search_max_attempts
            if required_web_search
            else self._max_tool_rounds
        )

        for round_index in range(round_limit):
            request_metadata: dict[str, Any] = {
                "agent_round": round_index + 1,
                "agent_scope": scope,
            }
            request_tools = [tool.schema for tool in tools.values()]
            if required_web_search:
                # Search is phase one. File creation stays local and
                # deterministic after fresh citations are verified.
                request_tools = []
                request_metadata.update(
                    {
                        "openai_web_search": True,
                        "openai_web_search_required": True,
                        "disable_openai_fallback": True,
                        "required_web_search_attempt": round_index + 1,
                    }
                )
            request = ChatRequest(
                tenant_id=session.tenant_id,
                trace_id=get_trace_id() or new_trace_id(),
                model_tier=model_tier,
                messages=messages,
                system=system_prompt,
                max_tokens=(
                    self._settings.agent_required_web_search_max_output_tokens
                    if required_web_search
                    else self._max_tokens
                ),
                temperature=temperature,
                tools=request_tools,
                cache_system=True,
                metadata=request_metadata,
            )
            try:
                if required_web_search:
                    remaining_timeout = required_web_search_deadline - time.monotonic()
                    if remaining_timeout <= 0:
                        raise TimeoutError
                    response = await self._chat_with_hard_timeout(
                        request,
                        timeout=remaining_timeout,
                    )
                else:
                    response = await self._llm.chat(request)
            except Exception as exc:
                if not required_web_search:
                    raise
                log.warning(
                    "agent.required_web_search_failed",
                    session_id=session.session_id,
                    error_class=exc.__class__.__name__,
                    timeout_seconds=required_web_search_timeout_seconds,
                )
                reason = (
                    "required_web_search_timeout"
                    if isinstance(exc, TimeoutError)
                    else "required_web_search_failed"
                )
                return self._required_web_search_failure_result(
                    scope=scope,
                    required_effect=required_effect,
                    policy=policy,
                    reason=reason,
                    usage=aggregate_usage,
                )
            final_response = response
            if required_web_search:
                log.info(
                    "agent.required_web_search_completed",
                    session_id=session.session_id,
                    finish_reason=str(response.finish_reason or ""),
                    citation_count=len(response.citations),
                    content_chars=len(response.content or ""),
                    latency_ms=response.latency_ms,
                    attempt=round_index + 1,
                )
            aggregate_usage.input_tokens += int(response.usage.input_tokens or 0)
            aggregate_usage.output_tokens += int(response.usage.output_tokens or 0)
            aggregate_usage.cache_read_tokens += int(response.usage.cache_read_tokens or 0)
            aggregate_usage.cache_write_tokens += int(response.usage.cache_write_tokens or 0)
            aggregate_usage.cost_usd += float(response.usage.cost_usd or 0.0)

            if required_web_search and response.tool_calls:
                # No custom function tools are exposed during the hosted-search
                # phase. A proxy/model returning one anyway must not be allowed
                # to execute a local side effect before search evidence is
                # verified.
                log.warning(
                    "agent.required_web_search_unexpected_tool_call",
                    session_id=session.session_id,
                    tool_names=[item.name for item in response.tool_calls],
                )
                return self._required_web_search_failure_result(
                    scope=scope,
                    required_effect=required_effect,
                    policy=policy,
                    reason="required_web_search_invalid_response",
                    usage=aggregate_usage,
                )

            if not response.tool_calls:
                if (
                    required_web_search
                    and not self._required_web_search_satisfied(response)
                    and round_index + 1 < round_limit
                ):
                    log.warning(
                        "agent.required_web_search_retrying_without_evidence",
                        session_id=session.session_id,
                        finish_reason=str(response.finish_reason or ""),
                        citation_count=len(response.citations),
                        attempt=round_index + 1,
                        max_attempts=round_limit,
                    )
                    continue
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
                if self._tool_calls_suppress_final_reply([executed]):
                    terminal_tool_effect = True
                    break
            if terminal_tool_effect:
                break

        if final_response is not None and final_response.tool_calls and not terminal_tool_effect:
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

        required_effect_auto_fulfilled = False
        required_effect_failure = ""
        required_web_search_satisfied = (
            self._required_web_search_satisfied(final_response) if required_web_search else True
        )
        required_effect_satisfied = bool(
            required_web_search_satisfied
            and self._required_effect_satisfied(
                required_effect,
                executed_tool_calls,
            )
        )
        if required_web_search and not required_web_search_satisfied:
            required_effect_failure = "required_web_search_evidence_missing"
            log.warning(
                "agent.required_web_search_evidence_missing",
                session_id=session.session_id,
                finish_reason=str(getattr(final_response, "finish_reason", "") or ""),
                citation_count=len(list(getattr(final_response, "citations", []) or [])),
                content_chars=len(str(getattr(final_response, "content", "") or "")),
            )
        elif required_effect and not required_effect_satisfied:
            auto_tool_call, required_effect_failure = await self._maybe_fulfill_required_effect(
                session,
                required_effect=required_effect,
                tools=tools,
                executed_tool_calls=executed_tool_calls,
                response_text=str(getattr(final_response, "content", "") or ""),
                response_citations=list(getattr(final_response, "citations", []) or []),
            )
            if auto_tool_call is not None:
                executed_tool_calls.append(auto_tool_call)
                required_effect_satisfied = self._required_effect_satisfied(
                    required_effect,
                    executed_tool_calls,
                )
                required_effect_auto_fulfilled = required_effect_satisfied
                if not required_effect_satisfied and not required_effect_failure:
                    required_effect_failure = "required_tool_failed"

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
        if required_effect and not required_effect_satisfied:
            suppress_final_reply = False
            final_text = self._required_effect_failure_reply(required_effect_failure)
        elif suppress_final_reply:
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
            citations=list(final_response.citations),
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
                **(
                    {
                        "required_effect": required_effect["type"],
                        "required_effect_satisfied": required_effect_satisfied,
                        "required_effect_auto_fulfilled": required_effect_auto_fulfilled,
                        "required_effect_failure": required_effect_failure,
                        "required_web_search": required_web_search,
                        "required_web_search_satisfied": required_web_search_satisfied,
                    }
                    if required_effect
                    else {}
                ),
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
        arguments = dict(tool_call.arguments or {})
        tool = tools.get(tool_call.name)
        if tool is None:
            return ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=arguments,
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
                arguments=arguments,
                error="tool_owner_execution_denied",
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        try:
            # Keep one mutable argument object for the handler and the audit
            # record. Tools may normalize trusted request arguments before
            # executing an external side effect.
            result = await tool.handler(session, arguments)
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
                    arguments=arguments,
                    error="tool_owner_execution_denied",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            return ToolCall(
                id=tool_call.id,
                name=tool_call.name,
                arguments=arguments,
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
                arguments=arguments,
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
                    session_id=(session.external_conversation_id or session.session_id),
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
    def _required_agent_effect(
        hints: dict[str, Any] | None,
        scope: str,
    ) -> dict[str, Any]:
        raw = (hints or {}).get("agent_required_effect")
        if not isinstance(raw, dict):
            return {}
        effect_type = str(raw.get("type") or "").strip().lower()
        contract_scope = normalize_agent_scope(str(raw.get("scope") or ""))
        operation = str(raw.get("operation") or "").strip().lower()
        tool_name = str(raw.get("tool") or "").strip()
        expected_tools = {
            (MESSAGE_EXPORT_SCOPE, "export_history"): "export_current_messages_file",
            (FILE_ANALYSIS_SCOPE, "convert"): "convert_current_file",
            (FILE_ANALYSIS_SCOPE, "generate"): "generate_text_file",
        }
        if (
            effect_type != "outbound_file"
            or contract_scope != normalize_agent_scope(scope)
            or expected_tools.get((contract_scope, operation)) != tool_name
        ):
            return {}
        effect: dict[str, Any] = {
            "type": effect_type,
            "scope": contract_scope,
            "operation": operation,
            "tool": tool_name,
            "format": str(raw.get("format") or "txt").strip().lower() or "txt",
            "web_search_required": bool(
                raw.get("web_search_required") is True
                and contract_scope == FILE_ANALYSIS_SCOPE
                and operation == "generate"
            ),
        }
        if contract_scope == MESSAGE_EXPORT_SCOPE and operation == "export_history":
            raw_recent_minutes = raw.get("recent_minutes")
            recent_minutes = (
                int(raw_recent_minutes)
                if (
                    isinstance(raw_recent_minutes, int) and not isinstance(raw_recent_minutes, bool)
                )
                or (isinstance(raw_recent_minutes, str) and raw_recent_minutes.isdigit())
                else 0
            )
            if 1 <= recent_minutes <= MAX_REQUIRED_MESSAGE_EXPORT_MINUTES:
                effect["recent_minutes"] = recent_minutes
        return effect

    def _required_web_search_configured(self) -> bool:
        return bool(
            self._settings.llm_provider == "openai"
            and self._settings.openai_api_mode == "responses"
            and self._settings.openai_web_search_enabled
            and self._settings.openai_web_search_live_enabled
            and self._settings.openai_web_search_tool in {"web_search", "web_search_preview"}
        )

    async def _chat_with_hard_timeout(
        self,
        request: ChatRequest,
        *,
        timeout: float,
    ) -> Any:
        task = asyncio.ensure_future(self._llm.chat(request))
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            self._cancel_background_task(task)
            raise
        if task not in done:
            self._cancel_background_task(task)
            raise TimeoutError
        return task.result()

    @classmethod
    def _cancel_background_task(cls, task: asyncio.Future[Any]) -> None:
        task.add_done_callback(cls._consume_background_task_result)
        task.cancel()

    @staticmethod
    def _consume_background_task_result(task: asyncio.Future[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.debug(
                "agent.background_llm_task_finished_with_error",
                error_class=exc.__class__.__name__,
            )

    @staticmethod
    def _valid_https_citation_url(value: object) -> bool:
        try:
            parsed = urlsplit(str(value or "").strip())
            _port = parsed.port
        except ValueError:
            return False
        return bool(
            parsed.scheme.lower() == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )

    @staticmethod
    def _required_web_search_satisfied(response: Any) -> bool:
        if response is None:
            return False
        if str(getattr(response, "finish_reason", "") or "").strip().lower() not in {
            "completed",
            "stop",
        }:
            return False
        for citation in list(getattr(response, "citations", []) or []):
            if str(
                getattr(citation, "source", "") or ""
            ).strip() == "openai_web_search" and AgentCapabilityEngine._valid_https_citation_url(
                getattr(citation, "url", "")
            ):
                return True
        return False

    @staticmethod
    def _required_web_search_failure_result(
        *,
        scope: str,
        required_effect: dict[str, Any],
        policy: dict[str, Any],
        reason: str,
        usage: ChatUsage | None = None,
    ) -> CapabilityResult:
        if reason == "required_web_search_not_configured":
            reply = (
                "当前未启用可用的实时联网搜索。请先在 Web 的“模型配置”中开启"
                "“OpenAI 联网搜索”和“实时网页访问”，保存并重启服务后再试；"
                "这次没有生成文件。"
            )
        elif reason == "required_web_search_timeout":
            reply = "实时联网搜索超时了，这次没有生成文件，请稍后重试。"
        else:
            reply = "实时联网搜索暂时不可用，这次没有生成文件，请稍后重试。"
        return CapabilityResult(
            route=RouteType.AGENT,
            reply_text=reply,
            tool_calls=[],
            usage=usage if usage is not None else ChatUsage(),
            metadata={
                "agent_tool_scope": scope,
                "effective_tools": policy.get("effective_tools") or [],
                "required_effect": required_effect.get("type", "outbound_file"),
                "required_effect_satisfied": False,
                "required_effect_failure": reason,
                "required_web_search": True,
                "required_web_search_satisfied": False,
            },
        )

    @staticmethod
    def _required_effect_satisfied(
        required_effect: dict[str, Any],
        tool_calls: list[ToolCall],
    ) -> bool:
        if not required_effect:
            return True
        required_tool = required_effect.get("tool", "")
        for item in tool_calls:
            if item.name != required_tool or item.error:
                continue
            result = item.result
            if not isinstance(result, dict):
                continue
            if (
                result.get("ok") is True
                and result.get("sent_to_current_session") is True
                and str(result.get("delivery_status") or "").strip().lower()
                in {"queued", "sent", "delivered"}
            ):
                required_minutes = required_effect.get("recent_minutes")
                if required_tool == "export_current_messages_file" and required_minutes:
                    result_minutes = result.get("minutes")
                    if (
                        str(result.get("report_type") or "").strip().lower() != "recent"
                        or isinstance(result_minutes, bool)
                        or not isinstance(result_minutes, int)
                        or result_minutes != required_minutes
                    ):
                        continue
                return True
        return False

    async def _maybe_fulfill_required_effect(
        self,
        session: Session,
        *,
        required_effect: dict[str, Any],
        tools: dict[str, _AgentTool],
        executed_tool_calls: list[ToolCall],
        response_text: str,
        response_citations: list[Citation] | None = None,
    ) -> tuple[ToolCall | None, str]:
        tool_name = required_effect.get("tool", "")
        if any(item.name == tool_name for item in executed_tool_calls):
            return None, "required_tool_failed"
        if tool_name not in tools:
            return None, "required_tool_unavailable"
        # A recent-message export carries a trusted, deterministic range from
        # the intent hook, so it can be completed safely if the model omits the
        # tool call. Other history ranges and conversions still fail closed
        # instead of guessing structured arguments or a source file.
        if tool_name == "export_current_messages_file":
            recent_minutes = int(required_effect.get("recent_minutes") or 0)
            if not 1 <= recent_minutes <= MAX_REQUIRED_MESSAGE_EXPORT_MINUTES:
                return None, "required_tool_not_called"
            arguments = {
                "report_type": "recent",
                "minutes": recent_minutes,
                "format": required_effect.get("format") or "txt",
            }
        elif tool_name == "generate_text_file":
            content = str(response_text or "").strip()
            if not content:
                return None, "required_content_missing"
            if required_effect.get("web_search_required") is True:
                content = self._append_citation_sources(
                    content,
                    list(response_citations or []),
                )
            arguments = {
                "content": content,
                "format": required_effect.get("format") or "txt",
            }
        else:
            return None, "required_tool_not_called"
        tool_call = ToolCall(
            id=f"auto_required_file_delivery_{len(executed_tool_calls) + 1}",
            name=tool_name,
            arguments=arguments,
        )
        executed = await self._execute_tool_call(session, tool_call, tools)
        if executed.error or not self._required_effect_satisfied(required_effect, [executed]):
            return executed, "required_tool_failed"
        return executed, ""

    @staticmethod
    def _append_citation_sources(content: str, citations: list[Citation]) -> str:
        value = str(content or "").strip()
        source_lines: list[str] = []
        seen_urls: set[str] = set()
        for citation in citations:
            url = str(citation.url or "").strip()
            if not AgentCapabilityEngine._valid_https_citation_url(url) or url in seen_urls:
                continue
            seen_urls.add(url)
            if url in value:
                continue
            title = str(citation.title or "来源").strip() or "来源"
            source_lines.append(f"{len(source_lines) + 1}. {title}\n   {url}")
        if not source_lines:
            return value
        return value + "\n\n来源链接：\n" + "\n".join(source_lines)

    @staticmethod
    def _required_effect_failure_reply(reason: str) -> str:
        if reason == "required_web_search_evidence_missing":
            return "这次没有取得可验证的实时来源链接，因此没有生成文件。请稍后重试。"
        if reason == "required_content_missing":
            return "这次没有整理出可写入文件的正文，请重新说明要生成的内容。"
        if reason == "required_tool_unavailable":
            return "当前会话的文件生成或发送能力暂时不可用，请稍后再试。"
        return "这次文件没有生成或发送成功，请稍后重试。"

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
    def _tool_result_for_audit(tool_name: str, result: Any) -> Any:
        """Keep extracted file text out of durable tool-audit records."""

        if tool_name != "inspect_current_file" or not isinstance(result, dict):
            return result
        value = dict(result)
        raw_content = value.pop("content", None)
        if isinstance(raw_content, str):
            encoded = raw_content.encode("utf-8", errors="replace")
            value.update(
                {
                    "content_redacted": True,
                    "content_sha256": hashlib.sha256(encoded).hexdigest(),
                    "content_chars": len(raw_content),
                }
            )
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
            return [item for item in definitions if self._definition_owner(item) == "core"]
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

        tool_order = {name: index for index, name in enumerate(tools)}
        ranked_names = sorted(positive, key=lambda name: (-positive[name], tool_order[name]))
        highest = positive[ranked_names[0]]
        top_names = [name for name in ranked_names if positive[name] == highest]
        runner_up = positive[ranked_names[1]] if len(ranked_names) > 1 else 0
        clear = (
            highest >= MIN_CLEAR_TOOL_PRESELECTION_SCORE
            and len(top_names) == 1
            and highest - runner_up >= MIN_CLEAR_TOOL_PRESELECTION_MARGIN
        )
        if clear:
            verdict = "CLEAR"
            selected_names = top_names
        elif highest >= MIN_CLEAR_TOOL_PRESELECTION_SCORE:
            verdict = "AMBIGUOUS"
            cutoff_index = min(AMBIGUOUS_TOOL_PRESELECTION_TOP_K, len(ranked_names)) - 1
            cutoff_score = positive[ranked_names[cutoff_index]]
            selected_names = [name for name in ranked_names if positive[name] >= cutoff_score]
        else:
            # A weak substring/verb match is useful diagnostic evidence, but it
            # is not enough to hide otherwise valid tools from the model.
            verdict = "INSUFFICIENT"
            selected_names = list(tools.keys())
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
        session_kind = cls._session_kind(session)
        # Private tool access is explicit opt-in.  Legacy definitions without
        # session metadata remain group-compatible, but can never become
        # private tools merely because the blanket private guard was removed.
        if session_kind == "private" and "private" not in allowed_session_kinds:
            return False
        if allowed_session_kinds and session_kind not in allowed_session_kinds:
            return False
        required_group_role = str(metadata.get("required_group_role") or "").strip().lower()
        if required_group_role and session_kind == "group":
            requester_roles = cls._requester_group_roles(session)
            if required_group_role == "admin":
                if not requester_roles.intersection({"admin", "owner", "group_admin"}):
                    return False
            elif required_group_role not in requester_roles:
                return False
        if bool(metadata.get("requires_group_file_send")) and session_kind == "group":
            session_metadata = dict(getattr(session, "metadata", {}) or {})
            if session_metadata.get("group_file_send_enabled") is not True:
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
                    tool_result=self._tool_result_for_audit(item.name, item.result),
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
