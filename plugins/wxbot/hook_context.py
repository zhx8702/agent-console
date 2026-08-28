from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime

from app.agent.scopes import (
    DEFAULT_AGENT_SCOPE,
    GROUP_DRAW_GENERATION_SCOPE,
    GROUP_PERSONAL_MAP_SCOPE,
    GROUP_PLUGIN_STATUS_SCOPE,
    GROUP_VIDEO_GENERATION_SCOPE,
    MESSAGE_EXPORT_SCOPE,
)
from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_runtime import is_confident
from app.orchestrator.pipeline import PipelineContext
from app.social import (
    ParticipationContext,
    ParticipationDecision,
    SocialParticipationService,
    VoiceProfile,
)
from app.social.feedback import NaturalFeedbackAction, NaturalFeedbackResult
from plugins.wxbot.file_intent import FileIntent, classify_file_intent

_MENTION_PREFIX_RE = re.compile(r"^\s*(?:@\S+[\s\u2005\u00a0]+)+")


_FIRST_MENTION_PREFIX_RE = re.compile(r"^\s*@\S+[\s\u2005\u00a0]+")


_SLASH_COMMAND_RE = re.compile(r"^\s*(?:@\S+[\s\u2005\u00a0]+)*\s*/([^\s]+)")


_BAN_SAFE_COMMANDS = {"/unban", "/解禁", "/banlist", "/禁言列表"}


_REPLY_POLICY_LOOKUP_TIMEOUT_SECONDS = 0.8


_PARTICIPATION_CONTEXT_TIMEOUT_SECONDS = 0.8


_SOFT_REPLY_MAX_CHARS = 180


_SOFT_REPLY_MAX_LINES = 3


_GROUP_SEGMENT_STAGGER_SECONDS = 1.2


_QUESTION_RE = re.compile(
    r"(?:[?？]|(?:吗|嘛|么|呢)\s*$|(?:什么|怎么|为何|为什么|能否|可以|有没有|多少|谁|哪(?:里|儿|个)))"
)


_EXPLICIT_BOT_QUESTION_RE = re.compile(
    r"^\s*(?:小?(?:助手|机器人)|bot|ai)(?:\s*助手)?[\s，,:：]+",
    re.IGNORECASE,
)


_SCOPE_BY_DOMAIN = {
    IntentDomain.MAP: GROUP_PERSONAL_MAP_SCOPE,
    IntentDomain.DRAW: GROUP_DRAW_GENERATION_SCOPE,
    IntentDomain.VIDEO: GROUP_VIDEO_GENERATION_SCOPE,
    IntentDomain.GROUP_INFO: DEFAULT_AGENT_SCOPE,
    IntentDomain.GROUP_PLUGIN_STATUS: GROUP_PLUGIN_STATUS_SCOPE,
    IntentDomain.AVATAR: DEFAULT_AGENT_SCOPE,
}


def _strip_group_mention_prefix(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    # Legacy payloads do not tell us the bot's exact display name. Remove only
    # the first leading mention so another explicitly mentioned group member is
    # never silently deleted.
    return _FIRST_MENTION_PREFIX_RE.sub("", raw, count=1).strip()


def _has_leading_mention_prefix(text: str) -> bool:
    return bool(_MENTION_PREFIX_RE.match(str(text or "")))


def _event_mentioned_me(ctx: PipelineContext) -> bool:
    metadata = dict(ctx.event.metadata or {})
    if bool(metadata.get("mentioned_me") or metadata.get("bot_mentioned")):
        return True

    at_wxids = {
        str(value or "").strip()
        for value in (metadata.get("at_wxids") or [])
        if str(value or "").strip()
    }
    bot_wxids = {
        str(metadata.get(key) or "").strip()
        for key in ("bot_wxid", "self_wxid", "wxbot_self_wxid")
        if str(metadata.get(key) or "").strip()
    }
    return bool(at_wxids & bot_wxids)


def _event_policy_session_id(ctx: PipelineContext) -> str:
    """Return the operator-facing conversation ID used by wxbot policies.

    Managed channel events persist against a connection-scoped canonical ID,
    while the admin UI and SDK roster configure policies by the external
    WeChat conversation ID. Policy reads must not accidentally treat the
    canonical ID as an unconfigured group (which is a deliberate opt-out).
    """

    event = ctx.event
    metadata = dict(event.metadata or {})
    session_metadata = dict(ctx.session.metadata or {}) if ctx.session is not None else {}
    for value in (
        event.external_conversation_id,
        metadata.get("external_conversation_id"),
        metadata.get("external_session_id"),
        getattr(ctx.session, "external_conversation_id", "") if ctx.session is not None else "",
        session_metadata.get("external_conversation_id"),
        session_metadata.get("external_session_id"),
        event.session_id,
    ):
        session_id = str(value or "").strip()
        if session_id:
            return session_id
    return ""


def _event_directly_addressed(ctx: PipelineContext) -> bool:
    """Distinguish a leading/direct mention from an inline reference."""

    if not _event_mentioned_me(ctx):
        return False
    addressed = ctx.event.metadata.get("bot_addressed")
    if addressed is not None:
        return bool(addressed)
    return str(ctx.event.metadata.get("bot_mention_position") or "") != "inline"


def _event_sender_wxid(ctx: PipelineContext) -> str:
    return str(
        ctx.event.metadata.get("sender_wxid")
        or ctx.event.metadata.get("sender_id")
        or ctx.event.user_id
        or ""
    ).strip()


def _event_command_token(ctx: PipelineContext) -> str:
    texts = [
        str(ctx.event.message.content or ""),
        str(ctx.event.metadata.get("wxbot_normalized_content") or ""),
    ]
    if ctx.pre is not None:
        texts.extend([ctx.pre.cleaned_text, ctx.pre.original_text])
    for text in texts:
        match = _SLASH_COMMAND_RE.match(str(text or ""))
        if match:
            return f"/{match.group(1).strip().lower()}"
    return ""


def _event_replied_to_bot(ctx: PipelineContext) -> bool:
    metadata = ctx.event.metadata
    for key in (
        "replied_to_bot",
        "reply_to_bot",
        "quoted_bot",
        "quote_is_self_sent",
    ):
        if bool(metadata.get(key)):
            return True

    quote = metadata.get("quote")
    if not isinstance(quote, dict):
        return False
    candidates = [
        quote,
        quote.get("message"),
        quote.get("quoted_message"),
        quote.get("quoted"),
        quote.get("raw"),
    ]
    bot_wxids = {
        str(value or "").strip().lower()
        for value in (
            metadata.get("bot_wxid"),
            metadata.get("self_wxid"),
            metadata.get("wxbot_self_wxid"),
        )
        if str(value or "").strip()
    }
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if any(
            bool(candidate.get(key))
            for key in ("is_self_sent", "sender_is_self", "from_bot", "is_bot")
        ):
            return True
        quoted_sender = (
            str(
                candidate.get("sender_wxid")
                or candidate.get("sender_id")
                or candidate.get("from_wxid")
                or ""
            )
            .strip()
            .lower()
        )
        if quoted_sender and quoted_sender in bot_wxids:
            return True
    return False


def _looks_like_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(str(text or "").strip()))


def _explicit_question_to_bot(ctx: PipelineContext, text: str) -> bool:
    if not _looks_like_question(text):
        return False
    metadata = ctx.event.metadata
    return bool(
        metadata.get("explicit_question_to_bot")
        or metadata.get("bot_question_addressed")
        or _EXPLICIT_BOT_QUESTION_RE.search(str(text or ""))
    )


def _voice_profile_prompt(profile: Mapping[str, object]) -> str:
    tone = str(profile.get("tone") or "natural")[:64]
    verbosity = str(profile.get("verbosity") or "concise")[:16]
    list_policy = str(profile.get("list_format_policy") or "avoid_by_default")[:32]
    phrases = list(
        dict.fromkeys(
            str(item).strip()[:80]
            for item in (profile.get("phrase_preferences") or [])
            if str(item).strip()
        )
    )[:30]
    phrase_line = "、".join(phrases) if phrases else "无固定口头禅"
    identity_disclosure = str(profile.get("identity_disclosure") or "contextual").strip()
    identity_rule = (
        "每次普通回复都要明确以“我是 AI 助手”开头，不得暗示自己是真人"
        if identity_disclosure == "always"
        else (
            "当前若已启用蒸馏 COS，问你是谁、是不是真人、真实身份，都按这个人自己来答；"
            "没有 COS 时，普通的“你是谁/你叫什么”按当前人格自然回答，不要固定复读“我是 AI 助手”"
        )
    )
    return (
        "群级 VoiceProfile（仅控制表面表达，不得改变事实、工具结果、安全决定、"
        "权限或记忆受众）："
        f"语气={tone}；详略={verbosity}；列表策略={list_policy}；"
        f"可参考表达={phrase_line}；身份说明={identity_rule}。"
    )


def _clear_applied_voice_profile(ctx: PipelineContext) -> None:
    if ctx.session is None:
        return
    previous_instruction = str(
        ctx.session.variables.pop("_wxbot_voice_profile_instruction", "") or ""
    ).strip()
    current = str(ctx.session.variables.get("persona_skill") or "").strip()
    if previous_instruction and current:
        current = current.replace(f"\n\n{previous_instruction}", "", 1)
        if current == previous_instruction:
            current = ""
        if current:
            ctx.session.variables["persona_skill"] = current.strip()
        else:
            ctx.session.variables.pop("persona_skill", None)
    ctx.session.variables.pop("voice_profile", None)


def _apply_voice_profile_to_session(ctx: PipelineContext) -> dict[str, object]:
    raw = ctx.extras.get("wxbot_voice_profile")
    raw_profile = dict(raw) if isinstance(raw, dict) else {}
    profile: VoiceProfile | None = None
    reason = "voice_profile_not_configured"
    if raw_profile:
        try:
            profile = VoiceProfile.model_validate(raw_profile)
        except (TypeError, ValueError):
            reason = "voice_profile_contract_invalid"
    if profile is not None:
        reason = profile.runtime_reason(
            session_id=str(ctx.event.session_id or ""),
            now=datetime.now(UTC),
        )

    _clear_applied_voice_profile(ctx)
    profile_payload = profile.runtime_style_payload() if profile is not None else {}
    signal: dict[str, object] = {
        "applied": False,
        "reason": reason,
        "profile_id": str(profile_payload.get("profile_id") or ""),
        "version": int(profile_payload.get("version") or 0),
        "enabled": bool(profile.enabled) if profile is not None else False,
        "sample_source": str(profile.sample_source) if profile is not None else "",
        "sample_scope": str(profile.sample_scope) if profile is not None else "",
    }
    if ctx.session is not None and profile is not None and reason == "voice_profile_active":
        instruction = _voice_profile_prompt(profile_payload)
        current = str(ctx.session.variables.get("persona_skill") or "").strip()
        ctx.session.variables["persona_skill"] = (
            f"{current}\n\n{instruction}" if current else instruction
        )
        ctx.session.variables["_wxbot_voice_profile_instruction"] = instruction
        ctx.session.variables["voice_profile"] = profile_payload
        signal.update(
            {
                "applied": True,
                "tone": str(profile_payload.get("tone") or "natural"),
                "verbosity": str(profile_payload.get("verbosity") or "concise"),
                "identity_disclosure": str(
                    profile_payload.get("identity_disclosure") or "contextual"
                ),
            }
        )
    elif ctx.session is None and reason == "voice_profile_active":
        signal["reason"] = "voice_profile_session_unavailable"
    ctx.signals.setdefault("channel", {}).setdefault("wechat", {})["voice_profile"] = signal
    return signal


def _participation_payload(decision: ParticipationDecision) -> dict[str, object]:
    return {
        "status": decision.status.value,
        "score": decision.score,
        "reason_codes": list(decision.reason_codes),
        "not_before": decision.not_before.isoformat() if decision.not_before is not None else "",
        "expires_at": decision.expires_at.isoformat() if decision.expires_at is not None else "",
        "mention_sender": decision.mention_sender,
    }


def _record_participation_decision(
    ctx: PipelineContext,
    decision: ParticipationDecision,
) -> dict[str, object]:
    payload = _participation_payload(decision)
    ctx.extras["wxbot_participation"] = payload
    policy_state = ctx.extras.get("wxbot_reply_policy")
    if isinstance(policy_state, dict):
        policy_state.update(
            {
                "participation_status": payload["status"],
                "participation_score": payload["score"],
                "participation_reason_codes": payload["reason_codes"],
                "participation_not_before": payload["not_before"],
                "participation_expires_at": payload["expires_at"],
            }
        )
    ctx.signals["participation"] = payload
    ctx.signals.setdefault("channel", {}).setdefault("wechat", {})["participation"] = payload
    return payload


def _natural_feedback_reply(results: list[NaturalFeedbackResult]) -> str:
    if not results:
        return "这次设置没改成功，请稍后再试。"
    confirmation_candidates = sum(
        max(0, result.memory_candidate_count)
        for result in results
        if result.memory_confirmation_required
    )
    if confirmation_candidates:
        return f"我找到 {confirmation_candidates} 条可能相关的记忆，暂时没有改动。你具体指哪一条？"
    if any(result.memory_action_pending for result in results):
        if any(
            result.signal.action == NaturalFeedbackAction.FORGET_MEMBER and result.applied
            for result in results
        ):
            return "我已经停用你的记忆；历史记录删除暂时没完成。"
        return "这次更正没能保存，请稍后再试。"
    if len(results) > 1:
        return "好，已按你的要求调整。"
    result = results[0]
    replies = {
        NaturalFeedbackAction.REDUCE_REPLIES: "好，我少说点。",
        NaturalFeedbackAction.DISABLE_PROACTIVE: "好，我不主动插话了。",
        NaturalFeedbackAction.FORGET_MEMBER: "好，关于你的记忆已删除。",
        NaturalFeedbackAction.KEEP_OUT_OF_GROUP: "好，群里不再提你的记忆。",
        NaturalFeedbackAction.CORRECT_MEMORY: (
            "你说得对，刚才那条记忆已作废。"
            if result.memory_items_changed > 0
            else "我没找到可作废的近期记忆。"
        ),
    }
    return replies[result.signal.action]


def _fallback_must_reply_decision(ctx: PipelineContext) -> ParticipationDecision:
    return SocialParticipationService().decide(
        ParticipationContext(
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            message_id=str(
                ctx.event.message_id or ctx.event.metadata.get("msg_svr_id") or ctx.trace_id or ""
            ),
            now=ctx.event.received_at,
            explicit_command=True,
        )
    )


def _agent_query_text(ctx: PipelineContext) -> str:
    if ctx.pre is not None:
        return str(
            ctx.event.metadata.get("wxbot_normalized_content")
            or ctx.pre.cleaned_text
            or ctx.pre.original_text
            or ""
        ).strip()
    return str(
        ctx.event.metadata.get("wxbot_normalized_content") or ctx.event.message.content or ""
    ).strip()


def _normalize_agent_scope_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    return re.sub(r"\s+", " ", value)


def _message_export_requested(
    text: str = "",
    *,
    decision: IntentDecision | None = None,
) -> bool:
    _ = text
    intent = classify_file_intent(decision=decision)
    return intent.operation == "export_history" and intent.delivery_required


def _video_question_requested(
    text: str = "",
    *,
    decision: IntentDecision | None = None,
) -> bool:
    _ = text
    return bool(
        decision is not None
        and is_confident(decision)
        and decision.domain is IntentDomain.VIDEO
        and decision.action == "question"
    )


def _file_intent_requested(
    text: str = "",
    *,
    has_attachment: bool = False,
    decision: IntentDecision | None = None,
) -> FileIntent:
    """Return the structured file decision used by the wxbot intent hook."""

    _ = text
    return classify_file_intent(
        has_attachment=has_attachment,
        decision=decision,
    )


def _map_scope_requested(
    text: str = "",
    *,
    decision: IntentDecision | None = None,
) -> bool:
    _ = text
    return bool(
        decision is not None
        and is_confident(decision)
        and decision.domain is IntentDomain.MAP
    )


def _resolve_group_agent_scope(
    text: str = "",
    *,
    decision: IntentDecision | None = None,
) -> str | None:
    value = _normalize_agent_scope_text(text)
    if value.startswith("/"):
        return None
    if decision is None or not is_confident(decision):
        return None
    if decision.domain is IntentDomain.VIDEO and decision.action == "question":
        return None
    if decision.domain is IntentDomain.FILE and decision.action == "export_history":
        return MESSAGE_EXPORT_SCOPE
    return _SCOPE_BY_DOMAIN.get(decision.domain)


def _explicit_map_generation_requested(
    text: str = "",
    *,
    decision: IntentDecision | None = None,
) -> bool:
    _ = text
    return bool(
        decision is not None
        and is_confident(decision)
        and decision.domain is IntentDomain.MAP
        and decision.action == "generate"
    )


def _sync_wxbot_reply_policy_signal(ctx: PipelineContext) -> dict[str, object]:
    policy = ctx.extras.get("wxbot_reply_policy")
    signal = dict(policy) if isinstance(policy, dict) else {}
    ctx.signals["reply_policy"] = signal
    ctx.signals.setdefault("channel", {}).setdefault("wechat", {})["reply_policy"] = signal
    return signal
