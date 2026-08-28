from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.channel import get_reply_policy_override
from app.channel.reply_policy import match_reply_policy as _match_reply_policy
from app.common.logging import get_logger
from app.common.types import Channel, RouteType
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookPoint
from app.social import ParticipationContext, ParticipationStatus, SocialParticipationService
from app.social.speech_ledger import GroupSpeechBudgetExceeded
from plugins.wxbot.channel import (
    GroupParticipationPolicyReader,
    group_policy_delivery_contract,
)
from plugins.wxbot.group_file_policy import (
    GroupFileSendDenied,
    require_group_file_send_enabled,
)
from plugins.wxbot.hook_context import (
    _SOFT_REPLY_MAX_CHARS,
    _SOFT_REPLY_MAX_LINES,
    _event_mentioned_me,
    _event_policy_session_id,
    _fallback_must_reply_decision,
    _record_participation_decision,
)
from plugins.wxbot.reply_serialization import (
    _collect_wxbot_messages,
    _group_text_stats,
    _staggered_not_before,
)
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)


def _merge_obligation_text_segments(
    messages: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Keep a mandatory answer to one text bubble whenever possible."""

    text_items = [item for item in messages if item.get("msg_type") == "text"]
    if len(text_items) <= 1:
        return messages
    merged_text = "\n".join(
        str(item.get("reply_text") or "").strip()
        for item in text_items
        if str(item.get("reply_text") or "").strip()
    )
    merged: list[dict[str, str]] = []
    inserted = False
    for item in messages:
        if item.get("msg_type") != "text":
            merged.append(item)
            continue
        if not inserted and merged_text:
            merged.append(
                {
                    "msg_type": "text",
                    "reply_text": merged_text,
                    "image_path": "",
                    "image_url": "",
                }
            )
            inserted = True
    return merged


@dataclass
class WxbotReplyQueueHook:
    store: WxbotStore
    effect_only: bool = False
    social_policy_store: GroupParticipationPolicyReader | None = field(
        default=None,
        repr=False,
    )
    name: str = "wxbot.reply_queue"
    point: HookPoint = HookPoint.AFTER_POSTPROCESS
    priority: int = 90

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.event.channel != Channel.WECHAT:
            return
        if ctx.reply is None:
            return
        result_metadata = dict(ctx.result.metadata or {}) if ctx.result is not None else {}
        if bool(
            result_metadata.get("suppress_outbound") or result_metadata.get("suppress_final_reply")
        ):
            ctx.extras["suppress_outbound"] = True
            if bool(result_metadata.get("skip_assistant_turn")):
                ctx.extras["skip_assistant_turn"] = True
            logger.info(
                "wxbot.reply_queue.suppressed_by_result_metadata",
                session_id=ctx.event.session_id,
                trace_id=ctx.trace_id,
                route=result_metadata.get("route"),
            )
            return

        is_command_reply = bool(ctx.extras.get("_command_token"))
        is_group = str(ctx.event.session_id or "").endswith("@chatroom")
        participation_state = ctx.extras.get("wxbot_participation")
        if is_group and is_command_reply and not isinstance(participation_state, dict):
            participation_state = _record_participation_decision(
                ctx,
                _fallback_must_reply_decision(ctx),
            )
        reply_override = get_reply_policy_override(ctx.extras)
        force_send = (
            bool(is_command_reply)
            or bool(reply_override.get("force_send"))
            or bool(ctx.extras.get("wxbot_force_send"))
        )
        override_mention_sender = reply_override.get("mention_sender")
        if not isinstance(override_mention_sender, bool):
            override_mention_sender = None

        policy_state = ctx.extras.get("wxbot_reply_policy")
        if is_group and is_command_reply and not isinstance(policy_state, dict):
            try:
                command_policy = await self.store.get_session_policy(
                    ctx.event.tenant_id,
                    _event_policy_session_id(ctx),
                )
                policy_state = {
                    "effective_mention_sender": bool(command_policy.get("effective_mention_sender"))
                }
            except Exception as exc:
                policy_state = {}
                logger.warning(
                    "wxbot.reply_queue.command_policy_unavailable",
                    session_id=ctx.event.session_id,
                    error_class=exc.__class__.__name__,
                )
        if is_group and force_send and self.social_policy_store is not None:
            captured_version = (
                policy_state.get("participation_policy_version")
                if isinstance(policy_state, dict)
                else None
            )
            captured_revalidation = (
                policy_state.get("send_revalidation_enabled")
                if isinstance(policy_state, dict)
                else None
            )
            try:
                policy_contract_complete = (
                    not isinstance(captured_version, bool)
                    and int(captured_version or 0) > 0
                    and isinstance(captured_revalidation, bool)
                )
            except (TypeError, ValueError):
                policy_contract_complete = False
            if not policy_contract_complete:
                document = await self.social_policy_store.get_group_policy(
                    ctx.event.tenant_id,
                    _event_policy_session_id(ctx),
                )
                current_contract = group_policy_delivery_contract(
                    document,
                    tenant_id=ctx.event.tenant_id,
                    session_id=_event_policy_session_id(ctx),
                )
                policy_state = {
                    **(policy_state if isinstance(policy_state, dict) else {}),
                    **current_contract,
                }
                ctx.extras["wxbot_reply_policy"] = policy_state
        if (
            not force_send
            and isinstance(policy_state, dict)
            and policy_state.get("allowed") is False
        ):
            ctx.extras["suppress_outbound"] = True
            ctx.extras["skip_assistant_turn"] = True
            logger.info(
                "wxbot.reply_queue.skipped_by_policy",
                session_id=ctx.event.session_id,
                msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
                reason=policy_state.get("reason"),
            )
            return

        participation_approved = bool(
            isinstance(participation_state, dict)
            and participation_state.get("status")
            in {
                ParticipationStatus.MUST_REPLY.value,
                ParticipationStatus.MAY_REPLY.value,
                ParticipationStatus.DEFER.value,
            }
        )
        if is_group and not is_command_reply and not force_send and not participation_approved:
            policy = await self.store.get_session_policy(
                ctx.event.tenant_id,
                _event_policy_session_id(ctx),
            )
            mode = str(policy.get("effective_mode") or "off")
            keywords = [
                str(item).strip()
                for item in (policy.get("trigger_keywords") or [])
                if str(item).strip()
            ]
            allowed, reason = _match_reply_policy(
                mode,
                str(ctx.event.message.content or ""),
                keywords,
                mentioned_me=_event_mentioned_me(ctx),
                is_group=True,
            )
            if not allowed:
                ctx.extras["suppress_outbound"] = True
                ctx.extras["skip_assistant_turn"] = True
                ctx.extras["wxbot_reply_policy"] = {
                    "session_id": ctx.event.session_id,
                    "reply_mode": mode,
                    "keywords": keywords,
                    "mentioned_me": _event_mentioned_me(ctx),
                    "allowed": False,
                    "reason": reason,
                }
                logger.info(
                    "wxbot.reply_queue.skipped_by_group_guard",
                    session_id=ctx.event.session_id,
                    msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
                    reason=reason,
                    reply_mode=mode,
                )
                return

        session_name = str(ctx.event.metadata.get("session_name") or "")
        sender_name = str(ctx.event.metadata.get("sender_name") or "")
        sender_wxid = str(ctx.event.metadata.get("sender_wxid") or "")
        reply_to_msg_svr_id = str(ctx.event.metadata.get("msg_svr_id") or "")
        session_kind = str(
            ctx.event.metadata.get("session_kind") or ("group" if is_group else "private")
        )
        decision_mention_sender = (
            participation_state.get("mention_sender")
            if isinstance(participation_state, dict)
            else None
        )
        if isinstance(decision_mention_sender, bool):
            # The social decision owns normal group mentions.  A global channel
            # preference must not turn every reply into a notification.
            mention_sender = bool(is_group and decision_mention_sender)
        else:
            mention_sender = bool(
                is_group
                and isinstance(policy_state, dict)
                and policy_state.get("effective_mention_sender")
            )
        if override_mention_sender is not None:
            mention_sender = override_mention_sender
        if is_group and bool(ctx.extras.get("wxbot_member_no_group_mentions")):
            # Member privacy is the final authority.  No plugin-level reply
            # override may turn an opted-out group reply into an @ notification.
            mention_sender = False
        source_message = ctx.event.model_dump(mode="json")
        source_message_id = str(
            ctx.event.message_id or reply_to_msg_svr_id or ctx.trace_id or ""
        ).strip()
        messages = _collect_wxbot_messages(ctx.reply)
        if not messages:
            return
        if is_group and any(item.get("msg_type") == "file" for item in messages):
            try:
                await require_group_file_send_enabled(
                    self.social_policy_store,
                    tenant_id=ctx.event.tenant_id,
                    session_id=_event_policy_session_id(ctx),
                )
            except GroupFileSendDenied as exc:
                ctx.extras["suppress_outbound"] = True
                ctx.extras["skip_assistant_turn"] = True
                ctx.extras["wxbot_file_send_denial_reason"] = exc.reason
                logger.warning(
                    "wxbot.reply_queue.group_file_send_suppressed",
                    session_id=ctx.event.session_id,
                    trace_id=ctx.trace_id,
                    reason=exc.reason,
                )
                return
        participation_status = str(
            participation_state.get("status") if isinstance(participation_state, dict) else ""
        )
        if is_group and (
            force_send or participation_status == ParticipationStatus.MUST_REPLY.value
        ):
            messages = _merge_obligation_text_segments(messages)
        response_kind = str(result_metadata.get("response_kind") or "").strip().lower()
        if response_kind not in {"tool_progress", "tool_result"}:
            tool_count = int(result_metadata.get("tool_count") or 0)
            if tool_count > 0 or bool(ctx.result is not None and ctx.result.tool_calls):
                response_kind = "tool_result"
            else:
                response_kind = "short"
        retimed_not_before = ""
        retimed_expires_at = ""
        if (
            is_group
            and participation_status
            in {
                ParticipationStatus.MUST_REPLY.value,
                ParticipationStatus.MAY_REPLY.value,
            }
            and response_kind in {"tool_progress", "tool_result"}
        ):
            retimed_context = ParticipationContext(
                tenant_id=ctx.event.tenant_id,
                session_id=ctx.event.session_id,
                message_id=source_message_id,
                now=datetime.now(UTC),
                response_kind=response_kind,
            )
            retimed_status = ParticipationStatus(participation_status)
            retimed_not_before_value, retimed_expires_at_value = (
                SocialParticipationService().timing_for(
                    retimed_context,
                    retimed_status,
                )
            )
            retimed_not_before = retimed_not_before_value.isoformat()
            if retimed_status is not ParticipationStatus.MUST_REPLY:
                retimed_expires_at = retimed_expires_at_value.isoformat()
        feature_state = ctx.extras.get("wxbot_humanization_features")
        style_guard_enabled = bool(
            not isinstance(feature_state, dict) or feature_state.get("style_guard_enabled")
        )
        speech_budget_enabled = bool(
            not isinstance(feature_state, dict) or feature_state.get("speech_budget_enabled")
        )
        duplicate_guard_enabled = bool(
            not isinstance(feature_state, dict) or feature_state.get("duplicate_guard_enabled")
        )
        high_risk_fact_guard = bool(
            ctx.extras.get("high_risk_fact_guard")
            or result_metadata.get("high_risk_fact_guard")
            or ctx.event.metadata.get("high_risk_fact_guard")
        )
        if is_group and participation_status == ParticipationStatus.MAY_REPLY.value:
            text_chars, text_lines = _group_text_stats(messages)
            if text_chars > _SOFT_REPLY_MAX_CHARS or text_lines > _SOFT_REPLY_MAX_LINES:
                guard = {
                    "allowed": False,
                    "reason": "soft_reply_too_long",
                    "text_chars": text_chars,
                    "text_lines": text_lines,
                    "max_chars": _SOFT_REPLY_MAX_CHARS,
                    "max_lines": _SOFT_REPLY_MAX_LINES,
                }
                ctx.extras["suppress_outbound"] = True
                ctx.extras["skip_assistant_turn"] = True
                ctx.extras["wxbot_outbound_guard"] = guard
                ctx.signals["wxbot_outbound_guard"] = guard
                logger.info(
                    "wxbot.reply_queue.soft_reply_suppressed",
                    session_id=ctx.event.session_id,
                    message_id=source_message_id,
                    reason_code=guard["reason"],
                    text_chars=text_chars,
                    text_lines=text_lines,
                )
                return

        if is_group and not is_command_reply and not force_send and not participation_approved:
            claim_reply = getattr(self.store, "claim_interactive_reply", None)
            if callable(claim_reply):
                state = policy_state if isinstance(policy_state, dict) else {}
                cooldown = 0.0
                if str(state.get("reply_mode") or "") == "all" and not bool(
                    state.get("mentioned_me")
                ):
                    cooldown = float(
                        getattr(
                            getattr(self.store, "settings", None),
                            "wxbot_group_reply_cooldown_seconds",
                            1.0,
                        )
                        or 0.0
                    )
                claimed = await claim_reply(
                    tenant_id=ctx.event.tenant_id,
                    session_id=ctx.event.session_id,
                    message_id=source_message_id,
                    cooldown_seconds=cooldown,
                )
                if not claimed:
                    ctx.extras["suppress_outbound"] = True
                    ctx.extras["skip_assistant_turn"] = True
                    ctx.extras["wxbot_reply_stale"] = True
                    logger.info(
                        "wxbot.reply_queue.superseded_or_cooled_down",
                        session_id=ctx.event.session_id,
                        message_id=source_message_id,
                        cooldown_seconds=cooldown,
                    )
                    return
        enqueued_count = 0
        effect_items: list[dict[str, object]] = []
        for index, item in enumerate(messages):
            if item["msg_type"] == "image" and not item["image_path"] and not item.get("image_url"):
                logger.warning(
                    "wxbot.reply_queue.skip_image_without_media_locator",
                    session_id=ctx.event.session_id,
                    trace_id=ctx.trace_id,
                )
                continue
            if item["msg_type"] == "file" and not item.get("file_path"):
                logger.warning(
                    "wxbot.reply_queue.skip_file_without_sdk_path",
                    session_id=ctx.event.session_id,
                    trace_id=ctx.trace_id,
                )
                continue
            command_id = f"wxbot-reply:{ctx.event.tenant_id}:{source_message_id}:{index}"
            override_reason = str(reply_override.get("reason") or "")
            speech_output_kind = (
                "repeater" if override_reason == "repeater_triggered" else "ordinary"
            )
            delivery = {
                "channel": "wechat",
                "adapter_id": ctx.event.adapter_id or "wechat-sdk",
                "connection_id": ctx.event.connection_id,
                "command_id": command_id,
                "idempotency_key": command_id,
                "tenant_id": ctx.event.tenant_id,
                "session_id": ctx.event.session_id,
                "external_conversation_id": (
                    ctx.event.external_conversation_id or ctx.event.session_id
                ),
                "canonical_conversation_id": (
                    ctx.event.canonical_conversation_id or ctx.event.session_id
                ),
                "session_name": session_name,
                "session_kind": session_kind,
                "sender_name": sender_name,
                "sender_wxid": sender_wxid,
                "mention_sender": mention_sender,
                "reply_to_msg_svr_id": reply_to_msg_svr_id,
            }
            if force_send:
                delivery["force_send"] = True
            if session_kind == "group":
                delivery.update(
                    {
                        "reply_policy_reason": override_reason,
                        "speech_output_kind": speech_output_kind,
                        "participation_policy_version": int(
                            policy_state.get("participation_policy_version") or 0
                        )
                        if isinstance(policy_state, dict)
                        else 0,
                        "participation_policy_source": str(
                            policy_state.get("participation_policy_source") or ""
                        )
                        if isinstance(policy_state, dict)
                        else "",
                        "humanization_stage": str(
                            policy_state.get("humanization_stage") or "legacy"
                        )
                        if isinstance(policy_state, dict)
                        else "legacy",
                        "humanization_cohort": str(
                            policy_state.get("humanization_cohort") or "legacy"
                        )
                        if isinstance(policy_state, dict)
                        else "legacy",
                        "preserve_baseline_participation": bool(
                            policy_state.get("preserve_baseline_participation")
                        )
                        if isinstance(policy_state, dict)
                        else False,
                        # Repeater output already has its own opt-in scope,
                        # per-content cooldown, two-human trigger and loop
                        # guards. The generic conversational budget used to
                        # silently stop it after 2/10m or 6/hour.
                        "speech_budget_enabled": (
                            speech_budget_enabled and speech_output_kind != "repeater"
                        ),
                        "duplicate_guard_enabled": duplicate_guard_enabled,
                        "response_kind": response_kind,
                        "send_revalidation_enabled": bool(
                            policy_state.get("send_revalidation_enabled", True)
                        )
                        if isinstance(policy_state, dict)
                        else True,
                        "style_guard_enabled": style_guard_enabled,
                        "high_risk_fact_guard": high_risk_fact_guard,
                        "privacy_control": bool(ctx.extras.get("wxbot_natural_feedback")),
                        # Shape only opportunistic LLM conversation. Commands,
                        # repeater text, tool/factual results, safety responses,
                        # and hard-address replies remain untouched.
                        "style_eligible": bool(
                            style_guard_enabled
                            and not high_risk_fact_guard
                            and speech_output_kind == "ordinary"
                            and participation_status
                            in {
                                ParticipationStatus.MAY_REPLY.value,
                                ParticipationStatus.DEFER.value,
                            }
                            and ctx.result is not None
                            and ctx.result.route == RouteType.LLM
                        ),
                        "voice_profile": (
                            dict(ctx.extras["wxbot_voice_profile"])
                            if isinstance(ctx.extras.get("wxbot_voice_profile"), dict)
                            else {}
                        ),
                    }
                )
            if participation_approved and isinstance(participation_state, dict):
                delivery.update(
                    {
                        "participation_status": str(participation_state.get("status") or ""),
                        "participation_score": int(participation_state.get("score") or 0),
                        "participation_reason_codes": list(
                            participation_state.get("reason_codes") or []
                        ),
                        "source_message_id": source_message_id,
                        "not_before": str(participation_state.get("not_before") or ""),
                        "expires_at": str(participation_state.get("expires_at") or ""),
                        "speech_class": (
                            "obligation"
                            if participation_status == ParticipationStatus.MUST_REPLY.value
                            else (
                                "scheduled"
                                if participation_status == ParticipationStatus.DEFER.value
                                else "soft"
                            )
                        ),
                        "deferred_candidate": participation_status
                        == ParticipationStatus.DEFER.value,
                    }
                )
                if retimed_not_before:
                    delivery["not_before"] = retimed_not_before
                    if participation_status == ParticipationStatus.MUST_REPLY.value:
                        delivery["expires_at"] = ""
                    elif retimed_expires_at:
                        delivery["expires_at"] = retimed_expires_at
            if is_group and index > 0:
                delivery["not_before"] = _staggered_not_before(
                    delivery.get("not_before"),
                    index=index,
                )
                delivery["segment_sequence"] = index + 1
                delivery["segment_count"] = len(messages)
                delivery["staggered"] = True
            persist_inline = (not self.effect_only) or bool(
                ctx.extras.get("degraded_reply_pending")
            )
            if bool(ctx.extras.get("degraded_reply_pending")):
                delivery["speech_budget_enabled"] = False
                delivery["speech_class"] = "obligation"
                delivery["wxbot_force_send"] = True
            if not persist_inline:
                effect_item: dict[str, object] = {
                    "tenant_id": ctx.event.tenant_id,
                    "channel": "wechat",
                    "adapter_id": ctx.event.adapter_id or "wechat-sdk",
                    "connection_id": ctx.event.connection_id,
                    "session_id": ctx.event.session_id,
                    "external_conversation_id": (
                        ctx.event.external_conversation_id or ctx.event.session_id
                    ),
                    "canonical_conversation_id": (
                        ctx.event.canonical_conversation_id or ctx.event.session_id
                    ),
                    "session_name": session_name,
                    "session_kind": session_kind,
                    "user_id": ctx.event.user_id,
                    "sender_name": sender_name,
                    "sender_wxid": sender_wxid,
                    "reply_to_msg_svr_id": reply_to_msg_svr_id,
                    "body": {"type": item["msg_type"], "text": item["reply_text"]},
                    "media": {
                        "image_path": item["image_path"],
                        "image_url": str(item.get("image_url") or ""),
                    },
                    "trace_id": ctx.trace_id,
                    "mention_sender": mention_sender,
                    "source_message": source_message,
                    "delivery": delivery,
                    "command_id": command_id,
                }
                if item["msg_type"] == "file":
                    effect_item["file"] = {
                        "file_path": str(item.get("file_path") or ""),
                        "file_name": str(item.get("file_name") or ""),
                        "file_size": item.get("file_size"),
                        "file_md5": str(item.get("file_md5") or ""),
                        "file_sha256": str(item.get("file_sha256") or ""),
                    }
                effect_items.append(effect_item)
            else:
                enqueue_kwargs: dict[str, object] = {
                    "tenant_id": ctx.event.tenant_id,
                    "session_id": ctx.event.session_id,
                    "session_name": session_name,
                    "sender_name": sender_name,
                    "sender_wxid": sender_wxid,
                    "reply_text": item["reply_text"],
                    "trace_id": ctx.trace_id,
                    "msg_type": item["msg_type"],
                    "image_path": item["image_path"],
                    "image_url": str(item.get("image_url") or ""),
                    "mention_sender": mention_sender,
                    "reply_to_msg_svr_id": reply_to_msg_svr_id,
                    "session_kind": session_kind,
                    "source_message": source_message,
                    "delivery": delivery,
                    "command_id": command_id,
                }
                if item["msg_type"] == "file":
                    enqueue_kwargs.update(
                        {
                            "file_path": str(item.get("file_path") or ""),
                            "file_name": str(item.get("file_name") or ""),
                            "file_size": item.get("file_size"),
                            "file_md5": str(item.get("file_md5") or ""),
                            "file_sha256": str(item.get("file_sha256") or ""),
                        }
                    )
                try:
                    await self.store.enqueue_reply(**enqueue_kwargs)
                except GroupSpeechBudgetExceeded as exc:
                    if (
                        participation_status == ParticipationStatus.MUST_REPLY.value
                        and exc.reason == "third_consecutive_bot_message"
                    ):
                        deferred_delivery = {
                            **delivery,
                            "deferred_candidate": True,
                            "not_before": (datetime.now(UTC) + timedelta(seconds=45)).isoformat(),
                            "expires_at": "",
                            "obligation_deferred_reason": exc.reason,
                        }
                        try:
                            await self.store.enqueue_reply(
                                **{
                                    **enqueue_kwargs,
                                    "delivery": deferred_delivery,
                                }
                            )
                        except GroupSpeechBudgetExceeded as deferred_exc:
                            exc = deferred_exc
                        else:
                            ctx.extras["wxbot_speech_budget"] = {
                                "allowed": False,
                                "reason": exc.reason,
                                "output_kind": exc.output_kind,
                                "deferred": True,
                            }
                            logger.info(
                                "wxbot.reply_queue.obligation_deferred",
                                session_id=ctx.event.session_id,
                                reason=exc.reason,
                                output_kind=exc.output_kind,
                            )
                            enqueued_count += 1
                            continue
                    ctx.extras["suppress_outbound"] = True
                    ctx.extras["skip_assistant_turn"] = True
                    ctx.extras["wxbot_speech_budget"] = {
                        "allowed": False,
                        "reason": exc.reason,
                        "output_kind": exc.output_kind,
                    }
                    logger.info(
                        "wxbot.reply_queue.speech_budget_suppressed",
                        session_id=ctx.event.session_id,
                        reason=exc.reason,
                        output_kind=exc.output_kind,
                    )
                    break
            enqueued_count += 1
        # WeChat replies are delivered by the wxbot SDK bridge, so they must
        # not also be published to the generic outbound webhook stream.
        ctx.extras["suppress_outbound"] = True
        ctx.extras["wxbot_reply_queued_count"] = enqueued_count
        if self.effect_only:
            ctx.extras["wxbot_reply_effect_items"] = effect_items
        if enqueued_count > 0:
            logger.info(
                "wxbot.reply_queue.enqueued",
                session_id=ctx.event.session_id,
                trace_id=ctx.trace_id,
                count=enqueued_count,
                persisted=bool(
                    (not self.effect_only) or ctx.extras.get("degraded_reply_pending")
                ),
            )
        else:
            logger.info(
                "wxbot.reply_queue.not_enqueued",
                session_id=ctx.event.session_id,
                trace_id=ctx.trace_id,
                degraded=bool(ctx.extras.get("degraded_reply_pending")),
            )
