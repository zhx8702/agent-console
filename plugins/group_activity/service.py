
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any, Protocol, TypeVar
from zoneinfo import ZoneInfo

import httpx
from prometheus_client import Counter

from app.agent.scopes import DEFAULT_AGENT_SCOPE, normalize_agent_scope
from app.channel import (
    ChannelOutbound,
    ChannelRegistry,
    ChannelSendOptions,
    ChannelTarget,
)
from app.channel.identity import (
    LEGACY_WXBOT_CONNECTION_ID,
    require_legacy_wxbot_history_scope,
)
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.types import Channel, PreprocessedMessage, Role, Session, Turn
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request
from app.social import (
    ParticipationContext,
    ParticipationDecision,
    ParticipationStatus,
    SocialParticipationService,
)
from app.social.rollout import HumanizationFeatures, resolve_humanization_features
from app.social.store import SocialPolicyStore
from app.social.telemetry import observe_runtime_event_persistence
from plugins.group_activity.store import (
    GroupActivityStore,
    normalize_group_activity_identity,
)
from plugins.wxbot.message_reader import WxbotMessageReader

log = get_logger(__name__)
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_BAD_OUTPUT_RE = re.compile(r"(以下是|总结|群聊记录|聊天记录|系统提示|系统指令|提示词)", re.IGNORECASE)
_DECEPTIVE_IDENTITY_RE = re.compile(
    r"(?:我(?:就)?是|本助手是|其实是|这里是).{0,4}(?:真人|人类|人工客服)|"
    r"(?:我不是|并非).{0,4}(?:AI|人工智能|机器人|程序)|"
    r"(?:已|已经).{0,4}(?:转接|切换|接入).{0,4}(?:人工|真人)",
    re.IGNORECASE,
)
_AI_DISCLOSURE_RE = re.compile(r"(?:我是|作为|来自).{0,4}(?:AI|人工智能|机器人)", re.IGNORECASE)
_IDENTITY_CONTEXT_RE = re.compile(
    r"(?:你|您|助手|机器人|这个(?:助手|机器人)|bot)(?:"
    r".{0,8}(?:真人|人类|AI|人工智能|机器人).{0,4}(?:吗|嘛|么|呢|\?|？)|"
    r".{0,4}(?:是不是|是否是).{0,4}(?:真人|人类|AI|人工智能|机器人))",
    re.IGNORECASE,
)
_AI_ACTIVITY_PREFIX = "（AI 助手自动暖场）"
_TOPIC_SIMILARITY_THRESHOLD = 0.82
_PLUGIN_OWNER = "group_activity"
_WXBOT_OWNER = "wxbot"
_BOUNDARY_OWNERS = (_PLUGIN_OWNER, _WXBOT_OWNER)
_T = TypeVar("_T")

GROUP_ACTIVITY_DECISIONS = Counter(
    "cs_group_activity_decisions_total",
    "Group activity scheduler decisions",
    ["status", "reason"],
)


@dataclass(frozen=True)
class GroupActivityDecision:
    status: str
    reason: str
    config: dict[str, Any]
    messages: list[dict[str, Any]]
    prompt: str = ""
    generated_text: str = ""
    event_id: int | None = None
    reply_queue_id: int | None = None
    command_id: str = ""
    voice_profile_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "reason_code": self.reason,
            "config": self.config,
            "message_count": len(self.messages),
            "messages": self.messages[-10:],
            "prompt": self.prompt,
            "generated_text": self.generated_text,
            "event_id": self.event_id,
            "reply_queue_id": self.reply_queue_id,
            "command_id": self.command_id,
            "voice_profile_reason": self.voice_profile_reason,
        }


class GroupActivityOwnersScopeGate(Protocol):
    async def __call__(
        self,
        owners: tuple[str, ...],
        *,
        tenant_id: str,
        session_id: str,
    ) -> bool: ...


class GroupActivityObservationStore(Protocol):
    async def list_recent_group_observations(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class _ChannelIdentity:
    adapter_id: str
    connection_id: str
    external_session_id: str


@dataclass(frozen=True, slots=True)
class _MessageHistory:
    messages: list[dict[str, Any]]
    source: str


class _ScopeExecutionDenied(RuntimeError):
    pass


class _ChannelIdentityUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "channel_identity_unavailable")
        super().__init__(self.reason)


class GroupActivityService:
    def __init__(
        self,
        *,
        store: GroupActivityStore,
        settings: Any,
        agent_engine: Any,
        outbound: ChannelOutbound,
        message_reader: WxbotMessageReader | None = None,
        wxbot_store: GroupActivityObservationStore | None = None,
        social_policy_store: SocialPolicyStore | None = None,
        owners_scope_execution_allowed: GroupActivityOwnersScopeGate | None = None,
        channel_registry: ChannelRegistry | None = None,
        execution_owner_versions: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._agent_engine = agent_engine
        self._outbound = outbound
        self._message_reader = message_reader or WxbotMessageReader(settings)
        self._wxbot_store = wxbot_store
        self._social_policy_store = social_policy_store
        self._owners_scope_execution_allowed = owners_scope_execution_allowed
        self._channel_registry = channel_registry
        supplied_versions = dict(execution_owner_versions or {})
        self._execution_owner_versions = {
            owner: str(supplied_versions.get(owner) or "").strip()
            for owner in _BOUNDARY_OWNERS
        }
        self._sem = asyncio.Semaphore(2)

    @staticmethod
    def _decision(
        status: str,
        reason: str,
        config: dict[str, Any],
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> GroupActivityDecision:
        GROUP_ACTIVITY_DECISIONS.labels(status=status, reason=reason).inc()
        return GroupActivityDecision(status, reason, config, messages, **kwargs)

    async def _scope_allowed(self, tenant_id: str, session_id: str) -> bool:
        if any(not self._execution_owner_versions[owner] for owner in _BOUNDARY_OWNERS):
            log.error(
                "group_activity.execution_owner_versions_missing",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        gate = self._owners_scope_execution_allowed
        if gate is None:
            log.error(
                "group_activity.scope_gate_missing",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            allowed = await gate(
                _BOUNDARY_OWNERS,
                tenant_id=tenant_id,
                session_id=session_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "group_activity.scope_gate_failed",
                tenant_id=tenant_id,
                session_id=session_id,
                error_class=exc.__class__.__name__,
            )
            return False
        if allowed is not True:
            log.info(
                "group_activity.scope_execution_denied",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        return True

    async def _require_scope(self, tenant_id: str, session_id: str) -> None:
        if not await self._scope_allowed(tenant_id, session_id):
            raise _ScopeExecutionDenied("scope_execution_denied")

    async def _guarded_external_call(
        self,
        tenant_id: str,
        session_id: str,
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        """Bracket one wxbot/LLM boundary with fresh atomic owner snapshots."""

        await self._require_scope(tenant_id, session_id)
        try:
            result = await operation()
        except asyncio.CancelledError as cancellation:
            # Cancellation does not waive the required post-boundary snapshot.
            # Run it in a child task so the caller's cancellation cannot abort
            # the audit/gate read halfway through, then preserve cancellation.
            post_gate = asyncio.create_task(
                self._scope_allowed(tenant_id, session_id),
                name="group-activity-post-cancel-owner-gate",
            )
            while not post_gate.done():
                try:
                    await asyncio.shield(post_gate)
                except asyncio.CancelledError:
                    continue
            try:
                post_gate.result()
            except Exception as exc:
                log.error(
                    "group_activity.post_cancel_scope_gate_failed",
                    tenant_id=tenant_id,
                    session_id=session_id,
                    error_class=exc.__class__.__name__,
                )
            raise cancellation
        except Exception as operation_error:
            try:
                await self._require_scope(tenant_id, session_id)
            except _ScopeExecutionDenied as scope_error:
                raise scope_error from operation_error
            raise
        await self._require_scope(tenant_id, session_id)
        return result

    def _outbound_for_target(self, target: ChannelTarget) -> ChannelOutbound:
        if self._channel_registry is not None:
            registered = self._channel_registry.outbound_for_target(target)
            if registered is not None:
                return registered
        try:
            require_legacy_wxbot_history_scope(
                self._settings,
                tenant_id=target.tenant_id,
                connection_id=target.connection_id,
            )
        except ValueError as exc:
            raise _ChannelIdentityUnavailable(
                "connection_scoped_outbound_unavailable"
            ) from exc
        return self._outbound

    def _channel_identity(self, cfg: dict[str, Any]) -> _ChannelIdentity:
        tenant_id = str(cfg.get("tenant_id") or "").strip()
        session_id = str(cfg.get("session_id") or "").strip()
        channel_id = str(cfg.get("channel_id") or "").strip().lower()
        if channel_id and channel_id != Channel.WECHAT.value:
            raise _ChannelIdentityUnavailable("channel_identity_mismatch")

        try:
            identity = normalize_group_activity_identity(
                self._settings,
                tenant_id=tenant_id,
                session_id=session_id,
                connection_id=str(cfg.get("connection_id") or ""),
                adapter_id=str(cfg.get("adapter_id") or ""),
                external_session_id=str(
                    cfg.get("external_session_id")
                    or cfg.get("external_conversation_id")
                    or ""
                ),
            )
        except ValueError as exc:
            raise _ChannelIdentityUnavailable(str(exc)) from exc
        return _ChannelIdentity(
            adapter_id=identity["adapter_id"],
            connection_id=identity["connection_id"],
            external_session_id=identity["external_session_id"],
        )

    async def _mark_cancelled_event(self, event_id: int) -> None:
        mark_task = asyncio.create_task(
            self._store.mark_event(
                event_id,
                status="failed",
                reason_code="cancelled",
                error="cancelled",
            ),
            name=f"group-activity-cancel-{event_id}",
        )
        while not mark_task.done():
            try:
                await asyncio.shield(mark_task)
            except asyncio.CancelledError:
                continue
        try:
            mark_task.result()
        except Exception as exc:
            log.error(
                "group_activity.cancel_cleanup_failed",
                event_id=event_id,
                error_class=exc.__class__.__name__,
            )

    async def process_due_sessions(self, *, limit: int = 200) -> dict[str, Any]:
        configs = await self._store.list_enabled_configs(limit=limit)
        if not configs:
            return {"processed": 0, "items": []}
        results: list[dict[str, Any] | None] = [None] * len(configs)
        queue: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue()
        for index, cfg in enumerate(configs):
            queue.put_nowait((index, cfg))

        async def worker() -> None:
            while True:
                try:
                    index, cfg = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    async with self._sem:
                        try:
                            results[index] = (
                                await self.process_session(cfg, dry_run=False)
                            ).as_dict()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.warning(
                                "group_activity.session_failed",
                                tenant_id=cfg.get("tenant_id"),
                                session_id=cfg.get("session_id"),
                                error=str(exc),
                            )
                            GROUP_ACTIVITY_DECISIONS.labels(
                                status="failed",
                                reason="internal_error",
                            ).inc()
                            results[index] = {
                                "status": "failed",
                                "reason": "internal_error",
                                "reason_code": "internal_error",
                                "config": cfg,
                            }
                finally:
                    queue.task_done()

        worker_count = min(2, len(configs))
        connection_scope = getattr(
            self._store,
            "independent_runtime_connections",
            None,
        )
        if callable(connection_scope):
            scope_manager = connection_scope()
        else:
            scope_manager = nullcontext()
        with scope_manager:
            async with asyncio.TaskGroup() as task_group:
                for _ in range(worker_count):
                    task_group.create_task(worker())

        items = [item for item in results if item is not None]
        return {"processed": len(items), "items": items}

    async def process_session(
        self,
        config_or_tenant: dict[str, Any] | str,
        session_id: str | None = None,
        *,
        dry_run: bool = True,
        force: bool = False,
    ) -> GroupActivityDecision:
        fallback_config = (
            dict(config_or_tenant)
            if isinstance(config_or_tenant, dict)
            else {
                "tenant_id": str(config_or_tenant),
                "session_id": str(session_id or ""),
            }
        )
        try:
            return await self._process_session(
                config_or_tenant,
                session_id,
                dry_run=dry_run,
                force=force,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "group_activity.process_failed_closed",
                tenant_id=fallback_config.get("tenant_id"),
                session_id=fallback_config.get("session_id"),
                error_class=exc.__class__.__name__,
            )
            return self._decision(
                "failed",
                "internal_error",
                fallback_config,
                [],
            )

    async def _process_session(
        self,
        config_or_tenant: dict[str, Any] | str,
        session_id: str | None = None,
        *,
        dry_run: bool = True,
        force: bool = False,
    ) -> GroupActivityDecision:
        cfg = (
            dict(config_or_tenant)
            if isinstance(config_or_tenant, dict)
            else await self._store.get_config(str(config_or_tenant), str(session_id or ""))
        )
        tenant_id = str(cfg.get("tenant_id") or "")
        sid = str(cfg.get("session_id") or session_id or "")
        if not tenant_id or not sid:
            return self._decision("skipped", "missing_scope", cfg, [])
        if not await self._scope_allowed(tenant_id, sid):
            return self._decision("skipped", "scope_execution_denied", cfg, [])
        if not bool(cfg.get("enabled")) and not force:
            return self._decision("skipped", "disabled", cfg, [])
        try:
            identity = self._channel_identity(cfg)
        except _ChannelIdentityUnavailable as exc:
            return self._decision("skipped", exc.reason, cfg, [])
        cfg.update(
            adapter_id=identity.adapter_id,
            connection_id=identity.connection_id,
            external_session_id=identity.external_session_id,
        )
        social_runtime = await self._load_social_runtime(tenant_id, sid)
        if social_runtime is None:
            return self._decision(
                "skipped",
                "social_policy_unavailable",
                cfg,
                [],
            )
        policy_document, humanization_features = social_runtime
        if not policy_document.effective_enabled:
            return self._decision(
                "skipped",
                "social_participation_disabled",
                cfg,
                [],
            )
        if not bool(policy_document.policy.proactive_enabled):
            return self._decision(
                "skipped",
                "proactive_policy_disabled",
                cfg,
                [],
            )
        if not humanization_features.proactive_enabled:
            return self._decision(
                "skipped",
                "proactive_rollout_disabled",
                cfg,
                [],
            )
        if self._in_quiet_window(cfg):
            return self._decision("skipped", "quiet_hours", cfg, [])
        if not force and not self._in_active_window(cfg):
            return self._decision("skipped", "outside_active_window", cfg, [])

        idle_minutes = max(180, int(cfg.get("idle_minutes") or 180))
        try:
            history = await self._load_messages(cfg, idle_minutes=idle_minutes)
            bot_wxids = await self._resolve_bot_wxids(
                history.messages,
                cfg=cfg,
                history_source=history.source,
            )
        except _ScopeExecutionDenied:
            return self._decision(
                "skipped",
                "scope_execution_denied",
                cfg,
                [],
            )
        except _ChannelIdentityUnavailable as exc:
            return self._decision("skipped", exc.reason, cfg, [])
        messages = history.messages
        if bot_wxids is None:
            return self._decision(
                "skipped",
                "bot_identity_unavailable",
                cfg,
                [],
            )
        user_messages = self._filter_user_messages(messages, bot_wxids=bot_wxids)
        min_messages = 1 if force else 2
        if len(user_messages) < min_messages:
            return self._decision(
                "skipped",
                "not_enough_context",
                cfg,
                user_messages,
            )
        now_ts = int(time.time())
        last_ts = max(int(item.get("ts") or 0) for item in user_messages)
        source_message = max(
            user_messages,
            key=lambda item: int(item.get("ts") or 0),
        )
        source_message_id = str(source_message.get("message_id") or "").strip()
        if not source_message_id:
            return self._decision(
                "skipped",
                "source_message_unavailable",
                cfg,
                user_messages,
            )
        if not force and now_ts - last_ts < idle_minutes * 60:
            return self._decision(
                "skipped",
                "group_not_idle",
                cfg,
                user_messages,
            )
        completed_today = await self._store.count_completed_today(
            tenant_id,
            sid,
            timezone=str(cfg.get("timezone") or "Asia/Shanghai"),
        )
        proactive_decision = SocialParticipationService().decide(
            ParticipationContext(
                tenant_id=tenant_id,
                session_id=sid,
                message_id=source_message_id,
                now=datetime.now(UTC),
                requested_proactive=True,
                proactive_messages_today=int(completed_today),
                group_silence_seconds=max(0, now_ts - last_ts),
            ),
            policy_document.policy.to_domain(
                enabled=policy_document.effective_enabled
            ),
        )
        if not proactive_decision.should_generate:
            return self._decision(
                "skipped",
                str(proactive_decision.reason_codes[-1]),
                cfg,
                user_messages,
            )
        if (
            proactive_decision.not_before is None
            or proactive_decision.expires_at is None
        ):
            return self._decision(
                "skipped",
                "participation_timing_unavailable",
                cfg,
                user_messages,
            )
        if not humanization_features.send_revalidation_enabled:
            return self._decision(
                "skipped",
                "send_revalidation_unavailable",
                cfg,
                user_messages,
            )
        latest_activity = await self._store.latest_completed_event(tenant_id, sid)
        if (
            not force
            and latest_activity is not None
            and last_ts <= int(latest_activity.get("last_user_message_ts") or 0)
        ):
            return self._decision(
                "skipped",
                "awaiting_human_response",
                cfg,
                user_messages,
            )

        interval = max(60, int(cfg.get("min_send_interval_minutes") or 180))
        if not force and await self._store.recent_event_exists(tenant_id, sid, minutes=interval):
            return self._decision(
                "skipped",
                "cooldown_active",
                cfg,
                user_messages,
            )
        max_per_day = max(1, int(cfg.get("max_per_day") or 1))
        if not force and completed_today >= max_per_day:
            return self._decision(
                "skipped",
                "daily_limit_reached",
                cfg,
                user_messages,
            )

        identity_context = self._identity_disclosure_requested(user_messages)
        # The configured VoiceProfile may intentionally use high-similarity
        # expression.  Do not add an unsolicited identity prefix merely because
        # this is the first proactive post; disclose contextually when the group
        # is actually asking about the bot's identity, and never permit deception.
        disclose_identity = identity_context
        voice_profile_reason = "voice_profile_not_configured"
        voice_profile: dict[str, Any] = {}
        if policy_document.voice_profile is not None:
            voice_profile_reason = policy_document.voice_profile.runtime_reason(
                session_id=sid,
                now=datetime.now(UTC),
            )
            if voice_profile_reason == "voice_profile_active":
                voice_profile = policy_document.voice_profile.runtime_style_payload()
                if (
                    str(voice_profile.get("identity_disclosure") or "").lower()
                    == "always"
                ):
                    disclose_identity = True
        prompt = self._build_prompt(
            cfg,
            user_messages,
            idle_minutes=idle_minutes,
            disclose_identity=disclose_identity,
            voice_profile=voice_profile,
        )
        if dry_run:
            return self._decision(
                "dry_run",
                "would_trigger",
                cfg,
                user_messages,
                prompt=prompt,
                voice_profile_reason=voice_profile_reason,
            )

        trace_id = new_trace_id()
        slot_key = self._slot_key(cfg)
        event = await self._store.try_create_event(
            tenant_id=tenant_id,
            session_id=sid,
            session_name=str(cfg.get("session_name") or sid),
            slot_key=slot_key,
            last_user_message_ts=last_ts,
            message_count=len(user_messages),
            trace_id=trace_id,
        )
        if event is None:
            return self._decision(
                "skipped",
                "slot_already_claimed",
                cfg,
                user_messages,
                prompt=prompt,
            )
        event_id = int(event["id"])
        try:
            started = await self._store.try_start_event(event_id)
            if started is None:
                return self._decision(
                    "skipped",
                    "event_already_running",
                    cfg,
                    user_messages,
                    prompt=prompt,
                    event_id=event_id,
                )

            event_id = int(started["id"])
            raw_generated = await self._generate_with_group_skill(
                cfg,
                user_messages,
                prompt=prompt,
                trace_id=trace_id,
            )
            generated, validation_reason = self._clean_output_with_reason(raw_generated)
            if not generated:
                await self._store.mark_event(
                    event_id,
                    status="skipped",
                    reason_code=validation_reason,
                    error=validation_reason,
                )
                return self._decision(
                    "skipped",
                    validation_reason,
                    cfg,
                    user_messages,
                    prompt=prompt,
                    event_id=event_id,
                )
            repeat_window = max(
                60,
                int(cfg.get("topic_repeat_window_minutes") or 1440),
            )
            recent_topics = await self._store.list_recent_generated_texts(
                tenant_id,
                sid,
                minutes=repeat_window,
                limit=20,
            )
            if self._is_repeated_topic(generated, recent_topics):
                await self._store.mark_event(
                    event_id,
                    status="skipped",
                    reason_code="duplicate_topic",
                    error="duplicate_topic",
                    generated_text=generated,
                )
                return self._decision(
                    "skipped",
                    "duplicate_topic",
                    cfg,
                    user_messages,
                    prompt=prompt,
                    generated_text=generated,
                    event_id=event_id,
                )
            generated = self._apply_identity_transparency(
                generated,
                required=disclose_identity,
            )
            if not force and not await self._still_idle(
                cfg,
                idle_minutes=idle_minutes,
                bot_wxids=bot_wxids,
            ):
                await self._store.mark_event(
                    event_id,
                    status="skipped",
                    reason_code="new_message_before_send",
                    error="new_message_before_send",
                    generated_text=generated,
                )
                return self._decision(
                    "skipped",
                    "new_message_before_send",
                    cfg,
                    user_messages,
                    prompt=prompt,
                    generated_text=generated,
                    event_id=event_id,
                )
            command_id = f"group_activity:{tenant_id}:{sid}:{slot_key}"
            await self._record_runtime_event(
                tenant_id=tenant_id,
                session_id=sid,
                policy_version=int(policy_document.version),
                features=humanization_features,
                trace_id=trace_id,
                decision=proactive_decision,
                event_id=event_id,
            )
            target = ChannelTarget(
                tenant_id=tenant_id,
                channel=Channel.WECHAT.value,
                session_id=sid,
                adapter_id=identity.adapter_id,
                connection_id=identity.connection_id,
                external_conversation_id=identity.external_session_id,
                canonical_conversation_id=sid,
                session_name=str(cfg.get("session_name") or sid),
                session_kind="group",
                metadata={
                    "adapter_id": identity.adapter_id,
                    "connection_id": identity.connection_id,
                    "external_session_id": identity.external_session_id,
                    "external_conversation_id": identity.external_session_id,
                    "canonical_conversation_id": sid,
                },
            )
            options = ChannelSendOptions(
                    trace_id=trace_id,
                    mention_sender=False,
                    idempotency_key=command_id,
                    source_message={
                        "message_id": source_message_id,
                        "msg_svr_id": str(
                            source_message.get("provider_message_id")
                            or source_message_id
                        ),
                        "connection_id": identity.connection_id,
                        "external_conversation_id": identity.external_session_id,
                        "canonical_conversation_id": sid,
                        "sender_wxid": str(
                            source_message.get("sender_wxid") or ""
                        ),
                        "text": str(source_message.get("text") or "")[:1000],
                    },
                    delivery_metadata={
                        "source": "group_activity",
                        "execution_owners": list(_BOUNDARY_OWNERS),
                        "execution_owner_versions": dict(
                            self._execution_owner_versions
                        ),
                        "execution_tenant_id": tenant_id,
                        "execution_session_id": sid,
                        "speech_output_kind": "proactive",
                        "speech_class": "scheduled",
                        "speech_budget_enabled": (
                            humanization_features.speech_budget_enabled
                        ),
                        "duplicate_guard_enabled": (
                            humanization_features.duplicate_guard_enabled
                        ),
                        "duplicate_guard_outcome": "topic_guard_passed",
                        "humanization_stage": humanization_features.stage.value,
                        "humanization_cohort": humanization_features.cohort,
                        "participation_policy_version": int(policy_document.version),
                        "participation_status": proactive_decision.status.value,
                        "participation_score": int(proactive_decision.score),
                        "participation_reason_codes": list(
                            proactive_decision.reason_codes
                        ),
                        "requested_proactive": True,
                        "source_message_id": source_message_id,
                        "connection_id": identity.connection_id,
                        "adapter_id": identity.adapter_id,
                        "external_conversation_id": identity.external_session_id,
                        "canonical_conversation_id": sid,
                        "not_before": proactive_decision.not_before.isoformat(),
                        "expires_at": proactive_decision.expires_at.isoformat(),
                        "deferred_candidate": (
                            proactive_decision.status is ParticipationStatus.DEFER
                        ),
                        "send_revalidation_enabled": True,
                        "voice_profile": voice_profile,
                        "voice_profile_reason": voice_profile_reason,
                        "style_eligible": True,
                        "explicitly_detailed": False,
                        "slot_key": slot_key,
                        "event_id": event_id,
                        "automated": True,
                        "ai_generated": True,
                        "identity_disclosed": disclose_identity,
                        "reason_code": "queued",
                        "agent_tool_scope": str(cfg.get("agent_tool_scope") or DEFAULT_AGENT_SCOPE),
                    },
            )
            result = await self._guarded_external_call(
                tenant_id,
                sid,
                lambda: self._outbound_for_target(target).send_text(
                    target,
                    generated,
                    options,
                ),
            )
            if bool(result.metadata.get("suppressed")):
                reason = str(result.metadata.get("reason") or "speech_budget_denied")
                await self._store.mark_event(
                    event_id,
                    status="skipped",
                    reason_code=reason,
                    error=reason,
                    generated_text=generated,
                )
                return self._decision(
                    "skipped",
                    reason,
                    cfg,
                    user_messages,
                    prompt=prompt,
                    generated_text=generated,
                    event_id=event_id,
                    command_id=command_id,
                )
            reply_queue_id = int(result.metadata.get("reply_queue_id") or 0) or None
            await self._store.complete_event(
                event_id,
                generated_text=generated,
                reply_queue_id=reply_queue_id,
                command_id=command_id,
                prompt_text=prompt,
            )
            return self._decision(
                "completed",
                "queued",
                cfg,
                user_messages,
                prompt=prompt,
                generated_text=generated,
                event_id=event_id,
                reply_queue_id=reply_queue_id,
                command_id=command_id,
                voice_profile_reason=voice_profile_reason,
            )
        except asyncio.CancelledError:
            await self._mark_cancelled_event(event_id)
            raise
        except _ScopeExecutionDenied:
            await self._store.mark_event(
                event_id,
                status="skipped",
                reason_code="scope_execution_denied",
                error="scope_execution_denied",
            )
            return self._decision(
                "skipped",
                "scope_execution_denied",
                cfg,
                user_messages,
                prompt=prompt,
                event_id=event_id,
            )
        except _ChannelIdentityUnavailable as exc:
            await self._store.mark_event(
                event_id,
                status="skipped",
                reason_code=exc.reason,
                error=exc.reason,
            )
            return self._decision(
                "skipped",
                exc.reason,
                cfg,
                user_messages,
                prompt=prompt,
                event_id=event_id,
            )
        except Exception as exc:
            await self._store.mark_event(
                event_id,
                status="failed",
                reason_code="internal_error",
                error=str(exc),
            )
            log.exception(
                "group_activity.event_failed_closed",
                tenant_id=tenant_id,
                session_id=sid,
                event_id=event_id,
                error_class=exc.__class__.__name__,
            )
            return self._decision(
                "failed",
                "internal_error",
                cfg,
                user_messages,
                prompt=prompt,
                event_id=event_id,
            )

    async def _load_social_runtime(
        self,
        tenant_id: str,
        session_id: str,
    ) -> tuple[Any, HumanizationFeatures] | None:
        if self._social_policy_store is None:
            return None
        try:
            document = await self._social_policy_store.get_group_policy(
                tenant_id,
                session_id,
            )
            features = resolve_humanization_features(
                tenant_id=tenant_id,
                session_id=session_id,
                stage=document.policy.rollout_stage,
                opted_in=document.policy.rollout_opt_in,
                kill_switches=document.kill_switches,
                proactive_percent=document.policy.proactive_rollout_percent,
            )
        except Exception as exc:
            log.warning(
                "group_activity.social_policy_unavailable",
                tenant_id=tenant_id,
                session_id=session_id,
                error_class=exc.__class__.__name__,
            )
            return None
        return document, features

    async def _record_runtime_event(
        self,
        *,
        tenant_id: str,
        session_id: str,
        policy_version: int,
        features: HumanizationFeatures,
        trace_id: str,
        decision: ParticipationDecision,
        event_id: int,
    ) -> None:
        if self._social_policy_store is None:
            observe_runtime_event_persistence(succeeded=False, obligation=True)
            raise RuntimeError("proactive_participation_audit_unavailable")
        try:
            await self._social_policy_store.record_participation_event(
                tenant_id=tenant_id,
                session_id=session_id,
                policy_version=max(0, int(policy_version)),
                event_kind="runtime",
                decision=decision,
                signal_summary={
                    "requested_proactive": True,
                    "rollout_stage": features.stage.value,
                    "cohort": features.cohort,
                    "speech_class": "scheduled",
                    "duplicate_guard_enabled": (
                        features.duplicate_guard_enabled
                    ),
                    "duplicate_guard_outcome": "topic_guard_passed",
                    "source_message_bound": True,
                    "not_before_set": decision.not_before is not None,
                    "expires_at_set": decision.expires_at is not None,
                    "deferred_candidate": (
                        decision.status is ParticipationStatus.DEFER
                    ),
                    "send_revalidation_enabled": (
                        features.send_revalidation_enabled
                    ),
                    "group_activity_event_id": int(event_id),
                },
                trace_id=str(trace_id or ""),
                runtime_stage="decision",
                delivery_stage="queue_requested",
            )
        except Exception as exc:
            observe_runtime_event_persistence(succeeded=False, obligation=True)
            log.warning(
                "group_activity.runtime_event_failed",
                tenant_id=tenant_id,
                session_id=session_id,
                error_class=exc.__class__.__name__,
            )
            raise RuntimeError("proactive_participation_audit_failed") from exc
        observe_runtime_event_persistence(succeeded=True, obligation=True)

    async def _load_messages(
        self,
        cfg: dict[str, Any],
        *,
        idle_minutes: int,
    ) -> _MessageHistory:
        # The lookback must extend beyond the idle boundary; otherwise a group
        # that just became eligible has no context by construction.
        lookback = max(
            idle_minutes + 60,
            int(cfg.get("lookback_minutes") or 120),
        )
        tenant_id = str(cfg.get("tenant_id") or "")
        session_id = str(cfg.get("session_id") or "")
        identity = self._channel_identity(cfg)
        if self._wxbot_store is not None:
            observations = await self._guarded_external_call(
                tenant_id,
                session_id,
                lambda: self._wxbot_store.list_recent_group_observations(
                    tenant_id,
                    session_id,
                    limit=200,
                ),
            )
            cutoff_ts = max(0, int(time.time()) - lookback * 60)
            messages = [
                self._observation_message(item)
                for item in reversed(observations)
                if str(item.get("msg_type") or "text").lower() == "text"
                and int(item.get("occurred_ts") or 0) >= cutoff_ts
                and str(item.get("content") or "").strip()
            ]
            if messages or identity.connection_id != LEGACY_WXBOT_CONNECTION_ID:
                return _MessageHistory(messages=messages, source="observations")

        try:
            require_legacy_wxbot_history_scope(
                self._settings,
                tenant_id=tenant_id,
                connection_id=identity.connection_id,
            )
        except ValueError as exc:
            raise _ChannelIdentityUnavailable(str(exc)) from exc

        reader_query = getattr(self._message_reader, "query_rows", None)
        if callable(reader_query):
            async def scoped_query_rows(**kwargs: Any) -> list[dict[str, Any]]:
                return await self._guarded_external_call(
                    tenant_id,
                    session_id,
                    lambda: reader_query(**kwargs),
                )

            reader = WxbotMessageReader(
                self._settings,
                query_rows=scoped_query_rows,
            )
            messages = await reader.load_group_text_messages(
                identity.external_session_id,
                member_name_map={},
                hours=max(1, (lookback + 59) // 60),
                limit=200,
            )
        else:
            messages = await self._guarded_external_call(
                tenant_id,
                session_id,
                lambda: self._message_reader.load_group_text_messages(
                    identity.external_session_id,
                    member_name_map={},
                    hours=max(1, (lookback + 59) // 60),
                    limit=200,
                ),
            )
        return _MessageHistory(
            messages=[
                {**dict(item), "source": "legacy_sdk"}
                for item in messages
                if isinstance(item, dict)
            ],
            source="legacy_sdk",
        )

    @staticmethod
    def _observation_message(item: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(item.get("metadata") or {})
        occurred_ts = int(item.get("occurred_ts") or 0)
        return {
            "message_id": str(item.get("message_id") or ""),
            "provider_message_id": str(
                metadata.get("external_message_id")
                or metadata.get("msg_svr_id")
                or ""
            ),
            "sender_wxid": str(item.get("sender_wxid") or ""),
            "sender_name": str(item.get("sender_name") or ""),
            "text": str(item.get("content") or "")[:1000],
            "timestamp": (
                datetime.fromtimestamp(occurred_ts, tz=UTC).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if occurred_ts > 0
                else ""
            ),
            "ts": occurred_ts,
            "is_self_sent": bool(item.get("is_self_sent")),
            "source": "wxbot_observation",
            "metadata": metadata,
        }

    def _filter_user_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        bot_wxids: set[str],
    ) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        for item in messages:
            sender = str(item.get("sender_wxid") or "").lower()
            text = str(item.get("text") or "").strip()
            source = str(item.get("source") or "").lower()
            if bool(item.get("is_self_sent")):
                continue
            if not text or sender in bot_wxids:
                continue
            if sender in {"group_activity", "wxbot", "bot"}:
                continue
            if source == "group_activity" or text.startswith("[group_activity]"):
                continue
            filtered.append(item)
        return sorted(filtered, key=lambda row: int(row.get("ts") or 0))

    async def _resolve_bot_wxids(
        self,
        messages: list[dict[str, Any]],
        *,
        cfg: dict[str, Any],
        history_source: str,
    ) -> set[str] | None:
        if history_source == "observations":
            return {
                str(item.get("sender_wxid") or "").strip().lower()
                for item in messages
                if bool(item.get("is_self_sent"))
                and str(item.get("sender_wxid") or "").strip()
            }
        candidates: set[str] = set()
        rowids: set[int] = set()
        for item in messages:
            if item.get("identity_resolved") is not True:
                continue
            value = str(item.get("self_wxid") or "").strip().lower()
            try:
                rowid = int(item.get("self_rowid"))
            except (TypeError, ValueError):
                continue
            if value and value != "auto" and rowid > 0:
                candidates.add(value)
                rowids.add(rowid)
        if len(candidates) == 1 and len(rowids) == 1:
            return candidates
        if candidates or rowids:
            log.warning(
                "group_activity.bot_identity_conflict",
                reason_code="bot_identity_unavailable",
            )
            return None

        tenant_id = str(cfg.get("tenant_id") or "")
        session_id = str(cfg.get("session_id") or "")
        identity = self._channel_identity(cfg)
        try:
            require_legacy_wxbot_history_scope(
                self._settings,
                tenant_id=tenant_id,
                connection_id=identity.connection_id,
            )
        except ValueError as exc:
            raise _ChannelIdentityUnavailable(str(exc)) from exc

        base_url = str(
            getattr(self._settings, "wxbot_sdk_url", "http://127.0.0.1:5080")
            or ""
        ).rstrip("/")
        try:
            async def load_status() -> httpx.Response:
                async with httpx.AsyncClient(
                    timeout=2.0,
                    trust_env=False,
                ) as client:
                    return await safe_trusted_service_request(
                        client,
                        "GET",
                        base_url,
                        "/status",
                        headers={
                            "Accept": "application/json",
                            **wxbot_sdk_headers(self._settings),
                        },
                        timeout_seconds=2.0,
                        max_response_bytes=2 * 1024 * 1024,
                        allowed_response_content_types=(
                            "application/json",
                            "application/problem+json",
                            "text/plain",
                        ),
                    )

            response = await self._guarded_external_call(
                tenant_id,
                session_id,
                load_status,
            )
            response.raise_for_status()
            payload = response.json()
            identity = payload.get("identity") if isinstance(payload, dict) else None
            if not isinstance(identity, dict) or identity.get("ready") is not True:
                return None
            value = str(identity.get("self_wxid") or "").strip().lower()
            try:
                rowid = int(identity.get("self_rowid"))
            except (TypeError, ValueError):
                return None
            if value and value != "auto" and rowid > 0:
                return {value}
        except _ScopeExecutionDenied:
            raise
        except Exception as exc:
            log.warning(
                "group_activity.bot_identity_lookup_failed",
                reason_code="bot_identity_unavailable",
                error_class=exc.__class__.__name__,
            )
        return None

    async def _generate_with_group_skill(
        self,
        cfg: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        prompt: str,
        trace_id: str,
    ) -> str:
        if self._agent_engine is None:
            raise RuntimeError("agent capability unavailable")
        sid = str(cfg.get("session_id") or "")
        tenant_id = str(cfg.get("tenant_id") or "")
        identity = self._channel_identity(cfg)
        session = Session(
            session_id=sid,
            tenant_id=tenant_id,
            user_id="group_activity",
            channel=Channel.WECHAT,
            adapter_id=identity.adapter_id,
            connection_id=identity.connection_id,
            conversation_id=sid,
            external_conversation_id=identity.external_session_id,
            canonical_conversation_id=sid,
            metadata={
                "session_name": str(cfg.get("session_name") or sid),
                "session_kind": "group",
                "adapter_id": identity.adapter_id,
                "connection_id": identity.connection_id,
                "external_session_id": identity.external_session_id,
                "external_conversation_id": identity.external_session_id,
                "canonical_conversation_id": sid,
            },
            turns=[
                Turn(
                    session_id=sid,
                    role=Role.USER,
                    content=f"{item.get('sender_name') or item.get('sender_wxid')}：{item.get('text')}",
                    trace_id=trace_id,
                )
                for item in messages[-8:]
            ],
        )
        pre = PreprocessedMessage(original_text=prompt, cleaned_text=prompt, language="zh")
        hints = {
            "agent_tool_scope": normalize_agent_scope(
                str(cfg.get("agent_tool_scope") or DEFAULT_AGENT_SCOPE)
            ),
            "_llm_model_tier": str(cfg.get("llm_model_tier") or "tier-2"),
            "_llm_temperature": float(
                0.9 if cfg.get("temperature") is None else cfg["temperature"]
            ),
        }
        result = await self._guarded_external_call(
            tenant_id,
            sid,
            lambda: self._agent_engine.answer(pre, session, hints=hints),
        )
        return str(result.reply_text or "")

    async def _still_idle(
        self,
        cfg: dict[str, Any],
        *,
        idle_minutes: int,
        bot_wxids: set[str],
    ) -> bool:
        history = await self._load_messages(cfg, idle_minutes=idle_minutes)
        messages = self._filter_user_messages(
            history.messages,
            bot_wxids=bot_wxids,
        )
        if not messages:
            return False
        last_ts = max(int(item.get("ts") or 0) for item in messages)
        return int(time.time()) - last_ts >= idle_minutes * 60

    def _build_prompt(
        self,
        cfg: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        idle_minutes: int,
        disclose_identity: bool,
        voice_profile: dict[str, Any] | None = None,
    ) -> str:
        tz = self._zone(str(cfg.get("timezone") or "Asia/Shanghai"))
        local_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        lines = []
        for item in messages[-30:]:
            ts = str(item.get("timestamp") or "")[-8:-3] or "--:--"
            sender = str(item.get("sender_name") or item.get("sender_wxid") or "群友")[:24]
            text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()[:180]
            lines.append(f"[{ts}] {sender}：{text}")
        voice_instruction = self._voice_profile_instruction(voice_profile or {})
        return (
            "这是由 AI 助手执行的群内自动暖场任务，需要走当前群配置的 Agent skill。\n"
            f"群名：{cfg.get('session_name') or cfg.get('session_id')}\n"
            f"当前时间：{local_time}\n"
            f"群里已经约 {idle_minutes} 分钟没人发言。\n"
            "最近一段时间的聊天记录，按时间从旧到新：\n"
            f"{chr(10).join(lines)}\n\n"
            "请基于当前群 skill 能查到的信息和上面的聊天上下文，生成一句适合直接发到群里的中文暖场话题。"
            "只输出一句话，不要@任何人，不要解释，长度 10 到 45 个中文字符。"
            "不得声称自己是真人、人类或已经发生人工接管。"
            "不得生成或猜测付款、授权、身份核验、账户状态、凭据等高风险事实。"
            + (
                "这次需要自然地明确 AI 助手身份，系统会添加一次透明标识，不得规避或否认。"
                if disclose_identity
                else "不必重复身份前缀；保持自然且透明，若被问到身份必须如实说明是 AI 助手。"
            )
            + voice_instruction
        )

    @staticmethod
    def _voice_profile_instruction(profile: dict[str, Any]) -> str:
        if not profile:
            return ""
        tone = str(profile.get("tone") or "natural")[:64]
        verbosity = str(profile.get("verbosity") or "concise")[:16]
        preferences = profile.get("phrase_preferences")
        phrases = [
            re.sub(r"\s+", " ", str(value)).strip()[:80]
            for value in (preferences if isinstance(preferences, list) else [])[:6]
            if str(value).strip()
        ]
        phrase_text = "、".join(phrases)
        return (
            f"表达风格：语气 {tone}，详略 {verbosity}；"
            + (f"可自然参考这些表达偏好：{phrase_text}；" if phrase_text else "")
            + "表达偏好只能影响措辞，不得覆盖安全、隐私、身份透明或事实约束。"
        )

    def _clean_output_with_reason(self, text: str) -> tuple[str, str]:
        value = str(text or "").strip()
        value = re.sub(r"^[-*\d.、\s]+", "", value)
        value = value.strip(" \t\r\n\"'“”‘’")
        value = re.split(r"[\r\n]+", value, maxsplit=1)[0].strip()
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            return "", "generation_empty"
        if _DECEPTIVE_IDENTITY_RE.search(value):
            return "", "generation_identity_deception"
        if _BAD_OUTPUT_RE.search(value):
            return "", "generation_prompt_leak"
        if len(value) > 80:
            return "", "generation_too_long"
        return value, "generation_valid"

    def _clean_output(self, text: str) -> str:
        return self._clean_output_with_reason(text)[0]

    @staticmethod
    def _identity_disclosure_requested(messages: list[dict[str, Any]]) -> bool:
        return any(
            _IDENTITY_CONTEXT_RE.search(str(item.get("text") or ""))
            for item in messages[-3:]
        )

    @staticmethod
    def _apply_identity_transparency(text: str, *, required: bool) -> str:
        value = str(text or "").strip()
        if not required or value.startswith(_AI_ACTIVITY_PREFIX):
            return value
        return f"{_AI_ACTIVITY_PREFIX}{value}"

    @staticmethod
    def _topic_key(text: str) -> str:
        value = str(text or "").replace(_AI_ACTIVITY_PREFIX, "")
        return re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "", value).lower()

    @classmethod
    def _is_repeated_topic(cls, generated: str, recent: list[str]) -> bool:
        candidate = cls._topic_key(generated)
        if not candidate:
            return True
        for item in recent:
            previous = cls._topic_key(item)
            if not previous:
                continue
            if candidate == previous:
                return True
            if min(len(candidate), len(previous)) >= 8 and SequenceMatcher(
                None,
                candidate,
                previous,
            ).ratio() >= _TOPIC_SIMILARITY_THRESHOLD:
                return True
        return False

    def _in_active_window(self, cfg: dict[str, Any]) -> bool:
        tz = self._zone(str(cfg.get("timezone") or "Asia/Shanghai"))
        now = datetime.now(tz)
        start = self._minutes(str(cfg.get("active_start") or "08:00"))
        end = self._minutes(str(cfg.get("active_end") or "17:00"))
        current = now.hour * 60 + now.minute
        if start <= end:
            return start <= current < end
        return current >= start or current < end

    def _in_quiet_window(self, cfg: dict[str, Any]) -> bool:
        start_text = str(cfg.get("quiet_start") or "23:00").strip()
        end_text = str(cfg.get("quiet_end") or "08:00").strip()
        if not start_text or not end_text or start_text == end_text:
            return False
        tz = self._zone(str(cfg.get("timezone") or "Asia/Shanghai"))
        now = datetime.now(tz)
        start = self._minutes(start_text)
        end = self._minutes(end_text)
        current = now.hour * 60 + now.minute
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _slot_key(self, cfg: dict[str, Any]) -> str:
        tz = self._zone(str(cfg.get("timezone") or "Asia/Shanghai"))
        now = datetime.now(tz)
        slot_minute = 0
        return now.replace(minute=slot_minute, second=0, microsecond=0).isoformat(timespec="minutes")

    @staticmethod
    def _minutes(value: str) -> int:
        if not _TIME_RE.match(value):
            raise ValueError("time must be HH:MM")
        hour, minute = value.split(":", 1)
        h = int(hour)
        m = int(minute)
        if h > 23 or m > 59:
            raise ValueError("time must be HH:MM")
        return h * 60 + m

    @staticmethod
    def _zone(value: str) -> ZoneInfo:
        try:
            return ZoneInfo(value or "Asia/Shanghai")
        except Exception:
            return ZoneInfo("Asia/Shanghai")
