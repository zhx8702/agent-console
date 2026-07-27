from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace

from app.channel.reply_policy import match_reply_policy as _match_reply_policy
from app.common.identity import (
    AI_IDENTITY_DISCLOSURE,
    GROUP_HANDOFF_UNAVAILABLE,
    GroupHumanIntent,
    GroupHumanIntentType,
    classify_group_human_intent,
)
from app.common.logging import get_logger
from app.common.types import Channel, IntentCoarse
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint
from app.social import (
    ParticipationContext,
    ParticipationDecision,
    ParticipationPolicy,
    ParticipationStatus,
    SocialParticipationService,
)
from app.social.feedback import (
    NaturalFeedbackAction,
    NaturalFeedbackResult,
    NaturalFeedbackService,
    NaturalFeedbackSignal,
    detect_natural_feedback,
)
from app.social.rollout import HumanizationFeatures, resolve_humanization_features
from app.social.store import SocialPolicyStore
from app.social.telemetry import (
    observe_participation_decision,
    observe_privacy_action,
    observe_runtime_event_persistence,
)
from plugins.wxbot.hook_context import (
    _PARTICIPATION_CONTEXT_TIMEOUT_SECONDS,
    _REPLY_POLICY_LOOKUP_TIMEOUT_SECONDS,
    _event_command_token,
    _event_directly_addressed,
    _event_mentioned_me,
    _event_policy_session_id,
    _event_replied_to_bot,
    _explicit_question_to_bot,
    _has_leading_mention_prefix,
    _natural_feedback_reply,
    _participation_payload,
    _record_participation_decision,
    _sync_wxbot_reply_policy_signal,
)
from plugins.wxbot.store import WxbotStore, normalize_group_participation_policy

logger = get_logger(__name__)


_EXPLICIT_BOT_VOCATIVE_RE = re.compile(
    r"^\s*(?:\u5c0f?(?:\u52a9\u624b|\u673a\u5668\u4eba)|bot|ai(?:\s*\u52a9\u624b)?)"
    r"(?:\s*[,\uff0c:\uff1a\u3001]\s*|\s+)",
    re.IGNORECASE,
)


def _explicitly_addresses_bot(ctx: PipelineContext, text: str) -> bool:
    """Recognize a deliberate bot vocative without treating ordinary group talk as one."""

    metadata = ctx.event.metadata
    if any(
        bool(metadata.get(key))
        for key in (
            "bot_addressed",
            "explicit_bot_addressed",
            "explicit_question_to_bot",
            "bot_question_addressed",
        )
    ):
        return True
    return bool(_EXPLICIT_BOT_VOCATIVE_RE.match(str(text or "")))


def _configured_participation_policy(
    value: object,
    *,
    enabled: bool,
) -> ParticipationPolicy:
    config = normalize_group_participation_policy(value)
    try:
        return ParticipationPolicy(
            enabled=enabled,
            threshold=int(config["threshold"]),
            quiet_start_hour=int(config["quiet_start_hour"]),
            quiet_end_hour=int(config["quiet_end_hour"]),
            timezone=str(config["timezone"]),
            max_soft_replies_10m=int(config["max_soft_replies_10m"]),
            max_soft_replies_hour=int(config["max_soft_replies_hour"]),
            max_bot_ratio_last_40=float(config["max_bot_ratio_last_40"]),
            max_consecutive_bot_messages=int(config["max_consecutive_bot_messages"]),
        )
    except (TypeError, ValueError):
        logger.warning(
            "wxbot.participation.invalid_policy_fallback",
            reason_code="participation_policy_invalid",
        )
        return ParticipationPolicy(enabled=enabled)


def _record_group_human_intent(
    ctx: PipelineContext,
    intent: GroupHumanIntent,
) -> None:
    payload = {
        "type": intent.type.value,
        "reason_code": intent.reason_code,
    }
    ctx.extras["wxbot_group_human_intent"] = payload
    ctx.event.metadata["wxbot_group_human_intent"] = intent.type.value
    ctx.event.metadata["wxbot_policy_reason_code"] = intent.reason_code


def _neutralize_group_handoff_preprocess_intent(
    ctx: PipelineContext,
    intent: GroupHumanIntent,
) -> None:
    if intent.type not in {
        GroupHumanIntentType.IDENTITY_INQUIRY,
        GroupHumanIntentType.HANDOFF_REQUEST,
        GroupHumanIntentType.HANDOFF_NON_REQUEST,
    }:
        return
    if ctx.pre is None or ctx.pre.intent_coarse != IntentCoarse.HANDOFF_REQUEST:
        return
    ctx.pre.intent_coarse = IntentCoarse.UNKNOWN
    ctx.extras["wxbot_preprocessed_intent_override"] = {
        "from": IntentCoarse.HANDOFF_REQUEST.value,
        "to": IntentCoarse.UNKNOWN.value,
        "reason_code": intent.reason_code,
    }


@dataclass
class WxbotReplyPolicyHook:
    store: WxbotStore
    social_policy_store: SocialPolicyStore | None = field(
        default=None,
        repr=False,
    )
    name: str = "wxbot.reply_policy"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    priority: int = 20
    participation_service: SocialParticipationService = field(
        default_factory=SocialParticipationService,
        repr=False,
    )
    natural_feedback_service: NaturalFeedbackService | None = field(
        default=None,
        repr=False,
    )

    async def _record_interaction_cursor(self, ctx: PipelineContext) -> bool:
        recorder = getattr(self.store, "record_interactive_inbound", None)
        if not callable(recorder):
            return False
        message_id = str(
            ctx.event.message_id or ctx.event.metadata.get("msg_svr_id") or ctx.trace_id or ""
        ).strip()
        if not message_id:
            return False
        try:
            await asyncio.wait_for(
                recorder(
                    tenant_id=ctx.event.tenant_id,
                    session_id=ctx.event.session_id,
                    message_id=message_id,
                ),
                timeout=_PARTICIPATION_CONTEXT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "wxbot.participation.cursor_unavailable",
                session_id=ctx.event.session_id,
                message_id=message_id,
                error_class=exc.__class__.__name__,
            )
            return False
        return True

    async def _load_participation_snapshot(
        self,
        ctx: PipelineContext,
    ) -> dict[str, object]:
        loader = getattr(self.store, "get_participation_snapshot", None)
        if not callable(loader):
            raise RuntimeError("participation snapshot unavailable")
        value = await asyncio.wait_for(
            loader(
                ctx.event.tenant_id,
                ctx.event.session_id,
                now=ctx.event.received_at,
            ),
            timeout=_PARTICIPATION_CONTEXT_TIMEOUT_SECONDS,
        )
        if not isinstance(value, dict):
            raise TypeError("participation snapshot must be a mapping")
        analyzer = getattr(self.store, "get_group_reply_revalidation", None)
        if callable(analyzer):
            source_message_id = str(
                ctx.event.message_id or ctx.event.metadata.get("msg_svr_id") or ctx.trace_id or ""
            ).strip()
            revalidation = await asyncio.wait_for(
                analyzer(
                    tenant_id=ctx.event.tenant_id,
                    session_id=ctx.event.session_id,
                    source_message_id=source_message_id,
                    participation_status=ParticipationStatus.MAY_REPLY.value,
                ),
                timeout=_PARTICIPATION_CONTEXT_TIMEOUT_SECONDS,
            )
            if not isinstance(revalidation, dict):
                raise TypeError("group reply revalidation must be a mapping")
            value.update(
                {
                    "revalidation_context_available": bool(revalidation.get("context_available")),
                    "valid_member_answer_exists": bool(
                        revalidation.get("valid_member_answer_exists")
                    ),
                    "topic_changed": bool(revalidation.get("topic_changed")),
                    "superseded_by_newer_message": bool(
                        revalidation.get("superseded_by_newer_message")
                    ),
                }
            )
        return value

    async def _load_runtime_participation_policy(
        self,
        ctx: PipelineContext,
        *,
        legacy_config: dict[str, object],
        legacy_enabled: bool,
    ) -> tuple[
        ParticipationPolicy,
        dict[str, object],
        int,
        HumanizationFeatures | None,
        dict[str, object],
        str,
    ]:
        if self.social_policy_store is None:
            logger.warning(
                "wxbot.participation.legacy_policy_adapter",
                reason_code="social_policy_store_not_injected",
            )
            return (
                _configured_participation_policy(
                    legacy_config,
                    enabled=legacy_enabled,
                ),
                dict(legacy_config),
                0,
                None,
                {},
                "legacy_adapter",
            )
        try:
            policy_session_id = _event_policy_session_id(ctx)
            document = await asyncio.wait_for(
                self.social_policy_store.get_group_policy(
                    ctx.event.tenant_id,
                    policy_session_id,
                ),
                timeout=_PARTICIPATION_CONTEXT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            fallback_enabled = bool(
                getattr(
                    getattr(self.store, "settings", None),
                    "social_policy_legacy_wxbot_fallback_enabled",
                    False,
                )
            )
            if not fallback_enabled:
                raise RuntimeError("social_participation_policy_unavailable") from exc
            logger.warning(
                "wxbot.participation.legacy_policy_fallback",
                reason_code="social_policy_store_unavailable",
                error_class=exc.__class__.__name__,
            )
            return (
                _configured_participation_policy(
                    legacy_config,
                    enabled=legacy_enabled,
                ),
                dict(legacy_config),
                0,
                None,
                {},
                "legacy_compatibility_fallback",
            )

        features = resolve_humanization_features(
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            stage=document.policy.rollout_stage,
            opted_in=document.policy.rollout_opt_in,
            kill_switches=document.kill_switches,
            proactive_percent=document.policy.proactive_rollout_percent,
        )
        domain = document.policy.to_domain(enabled=document.effective_enabled)
        if domain.proactive_enabled and not features.proactive_enabled:
            domain = replace(domain, proactive_enabled=False)
        return (
            domain,
            document.policy.model_dump(mode="json"),
            int(document.version),
            features,
            (
                document.voice_profile.model_dump(mode="json")
                if document.voice_profile is not None
                else {}
            ),
            "social_policy_store",
        )

    async def _persist_runtime_participation_event(
        self,
        ctx: PipelineContext,
        *,
        decision: ParticipationDecision,
        context: ParticipationContext,
        policy_version: int,
        stage: str,
        cohort: str,
    ) -> None:
        if self.social_policy_store is None:
            observe_runtime_event_persistence(
                succeeded=False,
                obligation=decision.status is ParticipationStatus.MUST_REPLY,
            )
            return
        signal_summary: dict[str, bool | int | float | str] = {
            "mentioned_me": bool(context.mentioned_me),
            "replied_to_bot": bool(context.replied_to_bot),
            "explicit_command": bool(context.explicit_command),
            "safety_response_required": bool(context.safety_response_required),
            "explicit_question_to_bot": bool(context.explicit_question_to_bot),
            "keyword_triggered": bool(context.keyword_triggered),
            "rapid_multi_party_chat": bool(context.rapid_multi_party_chat),
            "valid_member_answer_exists": bool(context.valid_member_answer_exists),
            "bot_messages_last_40": int(context.bot_messages_last_40),
            "total_messages_last_40": int(context.total_messages_last_40),
            "rollout_stage": str(stage or "legacy")[:32],
            "cohort": str(cohort or "legacy")[:32],
        }
        try:
            await self.social_policy_store.record_participation_event(
                tenant_id=ctx.event.tenant_id,
                session_id=ctx.event.session_id,
                policy_version=max(0, int(policy_version)),
                event_kind="runtime",
                decision=decision,
                signal_summary=signal_summary,
                trace_id=str(ctx.trace_id or ctx.event.trace_id or ""),
                runtime_stage="decision",
                delivery_stage="not_applicable",
            )
        except Exception as exc:
            observe_runtime_event_persistence(
                succeeded=False,
                obligation=decision.status is ParticipationStatus.MUST_REPLY,
            )
            logger.warning(
                "wxbot.participation.runtime_event_failed",
                tenant_id=ctx.event.tenant_id,
                session_id=ctx.event.session_id,
                error_class=exc.__class__.__name__,
            )
            return
        observe_runtime_event_persistence(
            succeeded=True,
            obligation=decision.status is ParticipationStatus.MUST_REPLY,
        )

    async def _apply_natural_feedback(
        self,
        ctx: PipelineContext,
        signals: tuple[NaturalFeedbackSignal, ...],
    ) -> None:
        results: list[NaturalFeedbackResult] = []
        error_reason = ""
        if self.natural_feedback_service is None:
            error_reason = "natural_feedback_service_unavailable"
            for signal in signals:
                observe_privacy_action(signal.action.value, succeeded=False)
        else:
            for signal in signals:
                try:
                    results.append(
                        await self.natural_feedback_service.apply(
                            signal,
                            tenant_id=ctx.event.tenant_id,
                            session_id=ctx.event.session_id,
                            user_id=str(
                                ctx.event.metadata.get("sender_wxid") or ctx.event.user_id or ""
                            ),
                            message_id=str(
                                ctx.event.message_id
                                or ctx.event.metadata.get("msg_svr_id")
                                or ctx.trace_id
                                or ""
                            ),
                            correction_text=str(ctx.event.message.content or ""),
                            trace_id=str(ctx.trace_id or ctx.event.trace_id or ""),
                        )
                    )
                except Exception as exc:
                    error_reason = "natural_feedback_apply_failed"
                    observe_privacy_action(signal.action.value, succeeded=False)
                    results.append(
                        NaturalFeedbackResult(
                            signal=signal,
                            applied=False,
                            memory_action_pending=True,
                        )
                    )
                    logger.warning(
                        "wxbot.natural_feedback.apply_failed",
                        tenant_id=ctx.event.tenant_id,
                        session_id=ctx.event.session_id,
                        user_id=ctx.event.user_id,
                        action=signal.action.value,
                        error_class=exc.__class__.__name__,
                    )

        applied_count = sum(1 for result in results if result.applied)
        all_applied = bool(results) and applied_count == len(results) and not error_reason
        if error_reason:
            outcome_reason = error_reason
        elif any(result.memory_confirmation_required for result in results):
            outcome_reason = "natural_feedback_confirmation_required"
        elif any(result.memory_action_pending for result in results):
            outcome_reason = "natural_feedback_action_pending"
        elif all_applied:
            outcome_reason = "natural_feedback_applied"
        else:
            outcome_reason = "natural_feedback_not_applied"

        payload = {
            "matched": True,
            "applied": all_applied,
            "applied_count": applied_count,
            "actions": [result.signal.action.value for result in results],
            "policy_versions": [result.policy_version for result in results],
            "memory_items_changed": sum(result.memory_items_changed for result in results),
            "memory_action_pending": any(result.memory_action_pending for result in results),
            "reason": outcome_reason,
        }
        if any(signal.action is NaturalFeedbackAction.KEEP_OUT_OF_GROUP for signal in signals):
            # Honor the current request even if its durable update fails.  The
            # persisted member policy protects subsequent turns.
            ctx.extras["wxbot_member_no_group_mentions"] = True
        ctx.extras["wxbot_natural_feedback"] = payload
        ctx.signals["natural_feedback"] = payload
        ctx.extras["memory_control_handled"] = True
        ctx.extras["skip_state_transition"] = True
        ctx.extras["wxbot_force_send"] = True
        ctx.extras["interaction_mode"] = "addressed"
        ctx.event.metadata["reply_allowed"] = True
        existing_policy = ctx.extras.get("wxbot_reply_policy")
        runtime_policy = {
            key: existing_policy[key]
            for key in (
                "participation_policy_version",
                "participation_policy_source",
                "humanization_stage",
                "humanization_cohort",
                "send_revalidation_enabled",
                "style_guard_enabled",
            )
            if isinstance(existing_policy, dict) and key in existing_policy
        }
        ctx.extras["wxbot_reply_policy"] = {
            "session_id": ctx.event.session_id,
            "reply_mode": "natural_feedback",
            "keywords": [],
            "mentioned_me": _event_mentioned_me(ctx),
            "effective_mention_sender": False,
            "allowed": True,
            "reason": payload["reason"],
            **runtime_policy,
        }
        participation_context = ParticipationContext(
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            message_id=str(ctx.event.message_id or ctx.trace_id or ""),
            now=ctx.event.received_at,
            explicit_command=True,
            response_kind="short",
        )
        decision = self.participation_service.decide(participation_context)
        observe_participation_decision(participation_context, decision)
        _record_participation_decision(ctx, decision)
        await self._persist_runtime_participation_event(
            ctx,
            decision=decision,
            context=participation_context,
            policy_version=int(runtime_policy.get("participation_policy_version") or 0),
            stage=str(runtime_policy.get("humanization_stage") or "legacy"),
            cohort=str(runtime_policy.get("humanization_cohort") or "legacy"),
        )
        _sync_wxbot_reply_policy_signal(ctx)
        raise HookAbort(
            _natural_feedback_reply(results),
            reason=str(payload["reason"]),
        )

    async def _resolve_replied_to_bot(self, ctx: PipelineContext) -> bool:
        if _event_replied_to_bot(ctx):
            return True
        quote = ctx.event.metadata.get("quote")
        resolver = getattr(self.store, "quote_targets_bot", None)
        if not isinstance(quote, dict) or not callable(resolver):
            return False
        try:
            return bool(
                await asyncio.wait_for(
                    resolver(
                        ctx.event.tenant_id,
                        ctx.event.session_id,
                        quote,
                    ),
                    timeout=_PARTICIPATION_CONTEXT_TIMEOUT_SECONDS,
                )
            )
        except Exception as exc:
            logger.warning(
                "wxbot.participation.quote_lookup_unavailable",
                session_id=ctx.event.session_id,
                msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
                error_class=exc.__class__.__name__,
            )
            return False

    @staticmethod
    def _stop_silently(
        ctx: PipelineContext,
        *,
        reason: str,
        mode: str,
    ) -> None:
        ctx.extras["interaction_mode"] = "observed"
        ctx.event.metadata["reply_allowed"] = False
        ctx.extras["suppress_outbound"] = True
        ctx.extras["skip_assistant_turn"] = True
        ctx.extras["skip_state_transition"] = True
        logger.info(
            "wxbot.reply_policy.participation_suppressed",
            session_id=ctx.event.session_id,
            msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
            reason=reason,
            reply_mode=mode,
        )
        _sync_wxbot_reply_policy_signal(ctx)
        raise HookAbort("", reason=reason)

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.event.channel != Channel.WECHAT:
            return

        session_id = ctx.event.session_id
        policy_session_id = _event_policy_session_id(ctx)
        content = str(ctx.event.message.content or "")
        is_self_sent = bool(ctx.event.metadata.get("is_self_sent"))
        if is_self_sent:
            ctx.extras["interaction_mode"] = "observed"
            ctx.event.metadata["reply_allowed"] = False
            ctx.extras["wxbot_reply_policy"] = {
                "session_id": session_id,
                "reply_mode": "audit_only",
                "keywords": [],
                "mentioned_me": _event_mentioned_me(ctx),
                "allowed": False,
                "reason": "self_sent_audit_only",
                "is_self_sent": True,
            }
            participation_context = ParticipationContext(
                tenant_id=ctx.event.tenant_id,
                session_id=session_id,
                message_id=str(ctx.event.message_id or ""),
                now=ctx.event.received_at,
                mentioned_me=_event_mentioned_me(ctx),
                is_self_sent=True,
            )
            decision = self.participation_service.decide(participation_context)
            observe_participation_decision(participation_context, decision)
            _record_participation_decision(ctx, decision)
            logger.info(
                "wxbot.reply_policy.self_sent_suppressed",
                session_id=session_id,
                msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
            )
            self._stop_silently(
                ctx,
                reason="self_sent_audit_only",
                mode="audit_only",
            )

        is_group = str(session_id or "").endswith("@chatroom")
        command_signal = ctx.signals.get("command")
        command_state = command_signal if isinstance(command_signal, dict) else {}
        command_token = str(command_state.get("command") or _event_command_token(ctx)).strip()
        command_candidate = bool(command_state.get("candidate") or command_token)
        if is_group and command_candidate:
            # Authorized commands finalize in the preceding command-dispatch
            # step. A slash candidate that reaches participation is therefore
            # rejected, disabled, unknown, or came through a custom flow that
            # omitted the command center. The slash namespace must never fall
            # through to conversational Agent/LLM handling.
            command_reason = str(command_state.get("reason") or "unhandled_command")
            ctx.extras["wxbot_reply_policy"] = {
                "session_id": session_id,
                "reply_mode": "command_gate",
                "keywords": [],
                "mentioned_me": _event_mentioned_me(ctx),
                "allowed": False,
                "reason": "group_slash_command_suppressed",
                "command": command_token,
                "command_reason": command_reason,
            }
            self._stop_silently(
                ctx,
                reason="group_slash_command_suppressed",
                mode="command_gate",
            )

        try:
            policy = await asyncio.wait_for(
                self.store.get_session_policy(ctx.event.tenant_id, policy_session_id),
                timeout=_REPLY_POLICY_LOOKUP_TIMEOUT_SECONDS,
            )
            if not isinstance(policy, dict):
                raise TypeError("wxbot session policy must be a mapping")
        except Exception as exc:
            reason = "wxbot_reply_policy_unavailable"
            ctx.extras["interaction_mode"] = "observed"
            ctx.event.metadata["reply_allowed"] = False
            ctx.extras["wxbot_reply_policy"] = {
                "session_id": session_id,
                "reply_mode": "unavailable",
                "keywords": [],
                "mentioned_me": _event_mentioned_me(ctx),
                "allowed": False,
                "reason": reason,
            }
            ctx.extras["suppress_outbound"] = True
            ctx.extras["skip_assistant_turn"] = True
            ctx.extras["skip_state_transition"] = True
            participation_context = ParticipationContext(
                tenant_id=ctx.event.tenant_id,
                session_id=session_id,
                message_id=str(ctx.event.message_id or ctx.trace_id or ""),
                now=ctx.event.received_at,
            )
            decision = ParticipationDecision(
                status=ParticipationStatus.OBSERVE_ONLY,
                score=0,
                reason_codes=(reason,),
            )
            observe_participation_decision(participation_context, decision)
            _record_participation_decision(ctx, decision)
            _sync_wxbot_reply_policy_signal(ctx)
            logger.exception(
                "wxbot.reply_policy.load_failed",
                session_id=session_id,
                msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
                error_class=exc.__class__.__name__,
            )
            raise HookAbort("", reason=reason) from exc
        mode = str(policy.get("effective_mode") or "off")
        mentioned_me = _event_mentioned_me(ctx)
        keywords = [
            str(item).strip()
            for item in (policy.get("trigger_keywords") or [])
            if str(item).strip()
        ]
        participation_config = normalize_group_participation_policy(
            policy.get("participation_policy")
        )
        allowed, reason = _match_reply_policy(
            mode,
            content,
            keywords,
            mentioned_me=mentioned_me,
            is_group=is_group,
        )
        human_intent = (
            classify_group_human_intent(content)
            if is_group
            else GroupHumanIntent(
                GroupHumanIntentType.NONE,
                "group_human_intent_none",
                "",
            )
        )
        if is_group and human_intent.type != GroupHumanIntentType.NONE:
            _record_group_human_intent(ctx, human_intent)
            _neutralize_group_handoff_preprocess_intent(ctx, human_intent)

        ctx.extras["wxbot_reply_policy"] = {
            "session_id": session_id,
            "reply_mode": mode,
            "keywords": keywords,
            "mentioned_me": mentioned_me,
            "effective_mention_sender": bool(policy.get("effective_mention_sender")),
            "effective_reply_cooldown_seconds": policy.get("effective_reply_cooldown_seconds"),
            "effective_coalesce_window_ms": policy.get("effective_coalesce_window_ms"),
            "effective_adaptive_cooldown_enabled": policy.get(
                "effective_adaptive_cooldown_enabled"
            ),
            "participation_policy": participation_config,
            "allowed": allowed,
            "reason": reason,
        }

        if not is_group:
            _sync_wxbot_reply_policy_signal(ctx)
            if allowed:
                ctx.extras["interaction_mode"] = "addressed" if mentioned_me else "triggered"
                ctx.event.metadata["reply_allowed"] = True
                return
            self._stop_silently(ctx, reason=reason, mode=mode)

        replied_to_bot = await self._resolve_replied_to_bot(ctx)
        explicit_command = bool(_event_command_token(ctx))
        directly_addressed = _event_directly_addressed(ctx)
        explicit_question_candidate = _explicit_question_to_bot(ctx, content)
        intent_is_addressed = bool(
            directly_addressed
            or replied_to_bot
            or explicit_command
            or explicit_question_candidate
            or _explicitly_addresses_bot(ctx, content)
        )
        safety_response_required = bool(human_intent.should_short_circuit and intent_is_addressed)
        hard_addressed = bool(
            directly_addressed or replied_to_bot or explicit_command or safety_response_required
        )
        explicit_question_to_bot = bool(not hard_addressed and explicit_question_candidate)

        try:
            (
                runtime_participation_policy,
                runtime_participation_payload,
                participation_policy_version,
                humanization_features,
                voice_profile,
                participation_policy_source,
            ) = await self._load_runtime_participation_policy(
                ctx,
                legacy_config=participation_config,
                legacy_enabled=(mode in {"all", "contains"} or safety_response_required),
            )
        except Exception as exc:
            policy_reason = "social_participation_policy_unavailable"
            participation_context = ParticipationContext(
                tenant_id=ctx.event.tenant_id,
                session_id=session_id,
                message_id=str(ctx.event.message_id or ctx.trace_id or ""),
                now=ctx.event.received_at,
                mentioned_me=directly_addressed,
                replied_to_bot=replied_to_bot,
                explicit_command=explicit_command,
                safety_response_required=safety_response_required,
            )
            decision = ParticipationDecision(
                status=ParticipationStatus.OBSERVE_ONLY,
                score=0,
                reason_codes=(policy_reason,),
            )
            policy_state = ctx.extras["wxbot_reply_policy"]
            policy_state["allowed"] = False
            policy_state["reason"] = policy_reason
            policy_state["participation_policy_source"] = "unavailable"
            observe_participation_decision(participation_context, decision)
            _record_participation_decision(ctx, decision)
            logger.warning(
                "wxbot.participation.public_policy_unavailable",
                session_id=session_id,
                error_class=exc.__class__.__name__,
            )
            self._stop_silently(ctx, reason=policy_reason, mode=mode)

        features_payload: dict[str, object] = {}
        if humanization_features is not None:
            features_payload = {
                "stage": humanization_features.stage.value,
                "bucket_percent": humanization_features.bucket_percent,
                "shadow_only": humanization_features.shadow_only,
                "preserve_baseline_participation": (
                    humanization_features.preserve_baseline_participation
                ),
                "cohort": humanization_features.cohort,
                "privacy_controls_enabled": (humanization_features.privacy_controls_enabled),
                "send_revalidation_enabled": (humanization_features.send_revalidation_enabled),
                "style_guard_enabled": humanization_features.style_guard_enabled,
                "speech_budget_enabled": (humanization_features.speech_budget_enabled),
                "duplicate_guard_enabled": (humanization_features.duplicate_guard_enabled),
                "contextual_soft_reply_enabled": (
                    humanization_features.contextual_soft_reply_enabled
                ),
                "proactive_enabled": humanization_features.proactive_enabled,
                "reason": humanization_features.reason,
            }
            ctx.extras["wxbot_humanization_features"] = features_payload
        if voice_profile:
            ctx.extras["wxbot_voice_profile"] = dict(voice_profile)
        policy_state = ctx.extras["wxbot_reply_policy"]
        policy_state.update(
            {
                "participation_policy": runtime_participation_payload,
                "participation_policy_version": participation_policy_version,
                "participation_policy_source": participation_policy_source,
                "humanization_stage": features_payload.get("stage", "legacy"),
                "humanization_cohort": features_payload.get("cohort", "legacy"),
                "preserve_baseline_participation": bool(
                    features_payload.get("preserve_baseline_participation")
                ),
                "speech_budget_enabled": bool(
                    humanization_features is None or humanization_features.speech_budget_enabled
                ),
                "duplicate_guard_enabled": bool(
                    humanization_features is None or humanization_features.duplicate_guard_enabled
                ),
                "send_revalidation_enabled": bool(
                    humanization_features is None or humanization_features.send_revalidation_enabled
                ),
                "style_guard_enabled": bool(
                    humanization_features is None or humanization_features.style_guard_enabled
                ),
            }
        )

        feedback_enabled = bool(
            humanization_features is None or humanization_features.privacy_controls_enabled
        )
        if feedback_enabled:
            feedback_signals = detect_natural_feedback(content)
            if feedback_signals:
                await self._apply_natural_feedback(ctx, feedback_signals)

        member_soft_reply_opt_out = False
        member_no_group_mentions = False
        member_proactive_enabled = False
        member_privacy_error = False
        if feedback_enabled and self.natural_feedback_service is not None:
            try:
                member_document = await asyncio.wait_for(
                    self.natural_feedback_service.get_member_policy(
                        ctx.event.tenant_id,
                        session_id,
                        str(ctx.event.metadata.get("sender_wxid") or ctx.event.user_id or ""),
                    ),
                    timeout=_PARTICIPATION_CONTEXT_TIMEOUT_SECONDS,
                )
                member_soft_reply_opt_out = bool(member_document.policy.soft_reply_opt_out)
                member_no_group_mentions = bool(member_document.policy.no_group_mentions)
                member_proactive_enabled = bool(
                    member_document.policy.proactive_participation_enabled
                )
            except Exception as exc:
                member_privacy_error = True
                member_no_group_mentions = True
                logger.warning(
                    "wxbot.participation.member_privacy_unavailable",
                    tenant_id=ctx.event.tenant_id,
                    session_id=session_id,
                    user_id=ctx.event.user_id,
                    error_class=exc.__class__.__name__,
                )
        # Queue-time plugin overrides are applied later, so carry this member
        # constraint forward for a final, non-overridable clamp.
        ctx.extras["wxbot_member_no_group_mentions"] = member_no_group_mentions

        requested_proactive = bool(ctx.event.metadata.get("requested_proactive"))
        rollout_proactive_enabled = bool(
            humanization_features is None or humanization_features.proactive_enabled
        )
        proactive_reply_enabled = bool(
            requested_proactive
            and rollout_proactive_enabled
            and member_proactive_enabled
            and runtime_participation_policy.proactive_enabled
        )
        soft_reply_enabled = bool(
            humanization_features is None
            or humanization_features.shadow_only
            or humanization_features.contextual_soft_reply_enabled
        )
        cursor_ready = await self._record_interaction_cursor(ctx)
        snapshot: dict[str, object] = {}
        context_error = ""
        if (
            (allowed or explicit_question_to_bot)
            and not hard_addressed
            and not member_soft_reply_opt_out
            and soft_reply_enabled
        ):
            if not cursor_ready:
                context_error = "participation_cursor_unavailable"
            elif member_privacy_error:
                context_error = "member_privacy_policy_unavailable"
            else:
                try:
                    snapshot = await self._load_participation_snapshot(ctx)
                    if snapshot.get("revalidation_context_available") is False:
                        context_error = "participation_revalidation_context_unavailable"
                except Exception as exc:
                    context_error = "participation_context_unavailable"
                    logger.warning(
                        "wxbot.participation.snapshot_unavailable",
                        session_id=session_id,
                        msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
                        error_class=exc.__class__.__name__,
                    )

        if context_error:
            participation_context = ParticipationContext(
                tenant_id=ctx.event.tenant_id,
                session_id=session_id,
                message_id=str(
                    ctx.event.message_id
                    or ctx.event.metadata.get("msg_svr_id")
                    or ctx.trace_id
                    or ""
                ),
                now=ctx.event.received_at,
                mentioned_me=directly_addressed,
                replied_to_bot=replied_to_bot,
                explicit_command=explicit_command,
                safety_response_required=safety_response_required,
            )
            decision = ParticipationDecision(
                status=ParticipationStatus.OBSERVE_ONLY,
                score=0,
                reason_codes=(context_error,),
            )
        else:
            keyword_triggered = reason == "reply_mode_contains_match"
            participation_context = ParticipationContext(
                tenant_id=ctx.event.tenant_id,
                session_id=session_id,
                message_id=str(
                    ctx.event.message_id
                    or ctx.event.metadata.get("msg_svr_id")
                    or ctx.trace_id
                    or ""
                ),
                now=ctx.event.received_at,
                mentioned_me=directly_addressed,
                replied_to_bot=replied_to_bot,
                explicit_command=explicit_command,
                safety_response_required=safety_response_required,
                explicit_question_to_bot=explicit_question_to_bot,
                keyword_triggered=keyword_triggered,
                topic_continuation=bool(ctx.event.metadata.get("topic_continuation")),
                unfinished_task_continuation=bool(
                    ctx.event.metadata.get("unfinished_task_continuation")
                ),
                directed_to_other_member=bool(
                    not mentioned_me and _has_leading_mention_prefix(content)
                ),
                rapid_multi_party_chat=bool(snapshot.get("rapid_multi_party_chat")),
                bot_replied_within_60s=bool(snapshot.get("bot_replied_within_60s")),
                valid_member_answer_exists=bool(snapshot.get("valid_member_answer_exists")),
                requested_proactive=proactive_reply_enabled,
                topic_changed=bool(snapshot.get("topic_changed")),
                superseded_by_newer_message=bool(snapshot.get("superseded_by_newer_message")),
                base_eligible=bool(
                    (allowed or explicit_question_to_bot)
                    and not member_soft_reply_opt_out
                    and soft_reply_enabled
                    and (not requested_proactive or proactive_reply_enabled)
                ),
                base_reason=(
                    "member_soft_reply_opt_out"
                    if member_soft_reply_opt_out
                    else (
                        "proactive_participation_not_enabled"
                        if requested_proactive and not proactive_reply_enabled
                        else (
                            "rollout_contextual_not_enabled" if not soft_reply_enabled else reason
                        )
                    )
                ),
                bot_messages_last_40=int(snapshot.get("bot_messages_last_40") or 0),
                total_messages_last_40=int(snapshot.get("total_messages_last_40") or 0),
                soft_replies_last_10m=int(snapshot.get("soft_replies_last_10m") or 0),
                soft_replies_last_hour=int(snapshot.get("soft_replies_last_hour") or 0),
                consecutive_bot_messages=int(snapshot.get("consecutive_bot_messages") or 0),
            )
            decision = self.participation_service.decide(
                participation_context,
                runtime_participation_policy,
            )
        if (
            humanization_features is not None
            and humanization_features.preserve_baseline_participation
        ):
            counterfactual = _participation_payload(decision)
            ctx.extras["wxbot_humanization_counterfactual_decision"] = counterfactual
            if humanization_features.shadow_only:
                ctx.extras["wxbot_humanization_shadow_decision"] = counterfactual
            baseline_allowed = bool(
                (allowed or explicit_command or safety_response_required)
                and not member_soft_reply_opt_out
                and (not requested_proactive or proactive_reply_enabled)
            )
            if baseline_allowed and hard_addressed:
                baseline_status = ParticipationStatus.MUST_REPLY
            elif baseline_allowed:
                baseline_status = ParticipationStatus.MAY_REPLY
            else:
                baseline_status = ParticipationStatus.OBSERVE_ONLY
            decision = ParticipationDecision(
                status=baseline_status,
                score=decision.score,
                reason_codes=(
                    "baseline_participation_preserved",
                    f"baseline:{reason}",
                    f"counterfactual:{decision.status.value}",
                ),
                mention_sender=bool(
                    baseline_status is ParticipationStatus.MUST_REPLY
                    and (replied_to_bot or participation_context.reply_target_ambiguous)
                ),
            )
        if member_no_group_mentions and decision.mention_sender:
            decision = replace(decision, mention_sender=False)
        observe_participation_decision(participation_context, decision)
        _record_participation_decision(ctx, decision)
        await self._persist_runtime_participation_event(
            ctx,
            decision=decision,
            context=participation_context,
            policy_version=participation_policy_version,
            stage=str(features_payload.get("stage") or "legacy"),
            cohort=str(features_payload.get("cohort") or "legacy"),
        )

        if safety_response_required and decision.status == ParticipationStatus.MUST_REPLY:
            policy_reason = reason
            reason = human_intent.reason_code
            reply_text = (
                AI_IDENTITY_DISCLOSURE
                if human_intent.type == GroupHumanIntentType.IDENTITY_INQUIRY
                else GROUP_HANDOFF_UNAVAILABLE
            )
            ctx.extras["interaction_mode"] = "addressed" if mentioned_me else "triggered"
            ctx.event.metadata["reply_allowed"] = True
            ctx.extras["skip_state_transition"] = True
            # The outbound hook performs its own group-policy check. Preserve
            # this safety response even when the ordinary reply mode is off.
            ctx.extras["wxbot_force_send"] = True
            ctx.extras["wxbot_reply_policy"] = {
                "session_id": session_id,
                "reply_mode": mode,
                "keywords": keywords,
                "mentioned_me": mentioned_me,
                "effective_mention_sender": bool(policy.get("effective_mention_sender")),
                "participation_policy": runtime_participation_payload,
                "allowed": True,
                "reason": reason,
                "base_reason": policy_reason,
                "human_intent": human_intent.type.value,
                "safety_override": not allowed,
                "participation_status": decision.status.value,
                "participation_score": decision.score,
                "participation_reason_codes": list(decision.reason_codes),
                "participation_not_before": (
                    decision.not_before.isoformat() if decision.not_before is not None else ""
                ),
                "participation_expires_at": (
                    decision.expires_at.isoformat() if decision.expires_at is not None else ""
                ),
            }
            if participation_policy_source != "legacy_adapter":
                ctx.extras["wxbot_reply_policy"].update(
                    {
                        "participation_policy_version": (participation_policy_version),
                        "participation_policy_source": (participation_policy_source),
                        "humanization_stage": features_payload.get("stage", "legacy"),
                        "humanization_cohort": features_payload.get("cohort", "legacy"),
                        "preserve_baseline_participation": bool(
                            features_payload.get("preserve_baseline_participation")
                        ),
                        "speech_budget_enabled": bool(
                            humanization_features is None
                            or humanization_features.speech_budget_enabled
                        ),
                        "duplicate_guard_enabled": bool(
                            humanization_features is None
                            or humanization_features.duplicate_guard_enabled
                        ),
                        "send_revalidation_enabled": bool(
                            humanization_features is None
                            or humanization_features.send_revalidation_enabled
                        ),
                        "style_guard_enabled": bool(
                            humanization_features is None
                            or humanization_features.style_guard_enabled
                        ),
                    }
                )
            _sync_wxbot_reply_policy_signal(ctx)
            logger.info(
                "wxbot.reply_policy.deterministic_group_response",
                session_id=session_id,
                msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
                human_intent=human_intent.type.value,
                reason_code=reason,
                safety_override=not allowed,
            )
            raise HookAbort(reply_text, reason=reason)

        if decision.should_generate:
            policy_state = ctx.extras["wxbot_reply_policy"]
            policy_state["allowed"] = True
            policy_state["reason"] = reason
            ctx.extras["interaction_mode"] = "addressed" if hard_addressed else "triggered"
            ctx.event.metadata["reply_allowed"] = True
            _sync_wxbot_reply_policy_signal(ctx)
            return

        terminal_reason = str(decision.reason_codes[-1])
        policy_state = ctx.extras["wxbot_reply_policy"]
        policy_state["allowed"] = False
        policy_state["base_reason"] = reason
        policy_state["reason"] = terminal_reason
        self._stop_silently(ctx, reason=terminal_reason, mode=mode)
