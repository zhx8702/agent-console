"""Late-stage response guards for abnormal assistant output."""
from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from app.common.types import OutboundReply, ReplySegment, ReplyType, RouteType
from app.common.prompting import persona_response_language

_DEFAULT_BOT_ALIASES = ("zzz",)
_TEXT_REPLY_TYPES = {ReplyType.TEXT, ReplyType.MARKDOWN}
_MODEL_QUESTION_RE = re.compile(
    r"(模型|model|后台|backend|后端|接入|连接|运行环境|model\s*id|llm|gpt|claude)",
    re.IGNORECASE,
)
_QUESTION_HINT_RE = re.compile(
    r"(\?|？|吗|么|什么|怎么|怎样|如何|谁|哪|几|多少|为何|为什么|模型|model)",
    re.IGNORECASE,
)
_SUCCESS_CONFIRM_RE = re.compile(
    r"^(签到成功|打卡成功|成功|已完成|已处理|已收到|收到|好的|好|OK|ok|画好了)([，。,.!！]?\s*.*)?$"
)
_CREDIT_ACCOUNT_INTENT_RE = re.compile(
    r"(积分|余额|credits?\b|balance\b|账户|账号|account\b)",
    re.IGNORECASE,
)
_IDENTITY_QUESTION_RE = re.compile(
    r"(?:你|您)(?:到底|究竟|真)?(?:是|是不是|算不算|属于)?"
    r".{0,8}(?:真人|人类|机器人|人工智能|AI|ai|助手)"
    r"|(?:真人|人类|机器人|人工智能|AI|ai)(?:吗|么|？|\?)",
)
_HIGH_RISK_FACT_REQUEST_RE = re.compile(
    r"(?:付款|支付|扣款|退款|转账|收款|到账|余额|订单|账户|账号).{0,14}"
    r"(?:状态|成功|失败|完成|通过|到账|冻结|启用|封禁|多少|是否|有没有|了吗|了没)"
    r"|(?:授权|权限|批准|审批).{0,12}(?:状态|成功|失败|通过|有效|生效|是否|有没有|了吗|了没)"
    r"|(?:实名|身份核验|身份验证|认证|KYC).{0,12}(?:状态|成功|失败|通过|有效|完成|是否|有没有|了吗|了没)"
    r"|(?:密码|验证码|token|令牌|密钥|凭据|access\s*key|api\s*key|secret).{0,10}"
    r"(?:是多少|发我|给我|显示|查看|导出|有效吗|是否有效|还能用|过期了吗)",
    re.IGNORECASE,
)
_HIGH_RISK_FACT_ASSERTION_RE = re.compile(
    r"(?:已|已经|确认|显示|当前|你的|该).{0,8}"
    r"(?:付款|支付|扣款|退款|转账|到账|余额|订单|账户|账号|授权|权限|审批|实名|身份核验|认证)"
    r".{0,12}(?:成功|失败|完成|通过|有效|生效|到账|冻结|启用|封禁|为|是)"
    r"|(?:你的|该|当前).{0,8}(?:密码|验证码|token|令牌|密钥|凭据|access\s*key|api\s*key|secret)"
    r".{0,6}(?:是|为|如下|有效)",
    re.IGNORECASE,
)
_SAFE_UNCERTAINTY_RE = re.compile(
    r"(?:无法|不能|没法|尚未|暂时无法).{0,12}(?:核实|确认|查询|验证|判断)"
    r"|(?:需要|请).{0,14}(?:工具|系统|人工|管理员|客服).{0,8}(?:核实|确认|查询|验证)"
    r"|(?:不知道|不确定|没有可验证|无权查看|不会提供|不能提供)",
    re.IGNORECASE,
)
_VERIFIED_FACT_ROUTES = frozenset({RouteType.FAQ, RouteType.RAG, RouteType.CANNED})
_HIGH_RISK_FACT_FALLBACK = (
    "这涉及付款、授权、身份核验、账户状态或凭据，我目前没有可验证的数据源，"
    "不能替你确认；请使用已授权工具或由人工核验。"
)
_HIGH_RISK_FACT_FALLBACK_EN = (
    "This involves payment, authorization, identity verification, account status, or credentials. "
    "I do not have a verifiable data source to confirm it; please use an authorized tool or have a human verify it."
)
_ENGLISH_LANGUAGE_FALLBACK = "I can only reply in English. Please send that again."
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def apply_response_guards(ctx: Any, *, settings: Any | None = None) -> None:
    """Mutate ``ctx.reply`` in place when a generated reply is unsafe to send.

    The guard intentionally runs after postprocessing and before outbound hooks
    or publish steps, so channel-specific queues see the corrected text.
    """

    reply = getattr(ctx, "reply", None)
    if reply is None:
        return
    segment = _first_guardable_text_segment(reply)
    if segment is None:
        return

    user_text = _current_user_text(ctx)
    reply_text = str(segment.content or "").strip()
    aliases = _bot_aliases(ctx, settings=settings)
    identity = _preferred_identity(aliases)
    english_only = _requires_english(ctx)

    replacement = None
    reason = "identity_transparency"
    if _is_high_risk_fact_exchange(user_text, reply_text):
        verified = _has_verified_fact_source(ctx)
        _mark_high_risk_fact(ctx, reply, verified=verified)
        if not verified and not _SAFE_UNCERTAINTY_RE.search(reply_text):
            replacement = _HIGH_RISK_FACT_FALLBACK_EN if english_only else _HIGH_RISK_FACT_FALLBACK
            reason = "high_risk_fact_unverified"
    if _IDENTITY_QUESTION_RE.search(user_text):
        replacement = _identity_transparency_reply(english_only)
        reason = "identity_transparency"
    if replacement is None:
        replacement = _self_reference_replacement(
            user_text=user_text,
            reply_text=reply_text,
            aliases=aliases,
            identity=identity,
            english_only=english_only,
        )
        reason = "self_reference"
    if replacement is None and _should_replace_echo(ctx, user_text, reply_text, aliases):
        replacement = _echo_fallback(user_text, english_only=english_only)
        reason = "echo"

    if replacement is not None:
        segment.content = replacement
        _sync_guarded_reply_metadata(ctx, reply, replacement, reason)

    if english_only:
        invalid_segments = [
            candidate
            for candidate in _text_reply_segments(reply)
            if _contains_cjk(candidate.content)
        ]
        if invalid_segments:
            for candidate in invalid_segments:
                candidate.content = _ENGLISH_LANGUAGE_FALLBACK
            _sync_guarded_reply_metadata(
                ctx,
                reply,
                reply.primary_text,
                "persona_language_guard",
            )


def _requires_english(ctx: Any) -> bool:
    session = getattr(ctx, "session", None)
    return session is not None and persona_response_language(session) == "en"


def _contains_cjk(value: object) -> bool:
    return bool(_CJK_RE.search(str(value or "")))


def _text_reply_segments(reply: OutboundReply) -> list[ReplySegment]:
    segments: list[ReplySegment] = []
    for candidate in reply.segments:
        if candidate.type not in _TEXT_REPLY_TYPES:
            continue
        metadata = candidate.metadata or {}
        msg_type = str(metadata.get("wxbot_msg_type") or metadata.get("msg_type") or "").lower()
        if msg_type != "image":
            segments.append(candidate)
    return segments


def _identity_transparency_reply(english_only: bool) -> str:
    if english_only:
        return "I am an AI running in Tibo's style, not the real Tibo."
    return "我是 AI 助手，不是真人。我会尽量自然地参与对话，也会明确说明能力边界。"


def _is_high_risk_fact_exchange(user_text: str, reply_text: str) -> bool:
    return bool(
        _HIGH_RISK_FACT_REQUEST_RE.search(str(user_text or ""))
        or _HIGH_RISK_FACT_ASSERTION_RE.search(str(reply_text or ""))
    )


def _has_verified_fact_source(ctx: Any) -> bool:
    result = getattr(ctx, "result", None)
    route = getattr(result, "route", None)
    if route in _VERIFIED_FACT_ROUTES:
        return True
    if route == RouteType.AGENT and (
        bool(getattr(result, "tool_calls", None))
        or bool(getattr(result, "citations", None))
    ):
        return True
    metadata = getattr(result, "metadata", {}) or {}
    return bool(
        isinstance(metadata, dict)
        and any(
            metadata.get(key) is True
            for key in (
                "fact_source_verified",
                "tool_result_verified",
                "trusted_source",
            )
        )
    )


def _mark_high_risk_fact(
    ctx: Any,
    reply: OutboundReply,
    *,
    verified: bool,
) -> None:
    marker = {"detected": True, "source_verified": verified}
    extras = getattr(ctx, "extras", None)
    if isinstance(extras, dict):
        extras["high_risk_fact_guard"] = marker
    reply.metadata["high_risk_fact_guard"] = marker
    result = getattr(ctx, "result", None)
    if result is not None:
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["high_risk_fact_guard"] = marker
        result.metadata = metadata


def _first_guardable_text_segment(reply: OutboundReply) -> ReplySegment | None:
    for segment in reply.segments:
        if segment.type not in _TEXT_REPLY_TYPES:
            continue
        metadata = segment.metadata or {}
        msg_type = str(metadata.get("wxbot_msg_type") or metadata.get("msg_type") or "").lower()
        if msg_type == "image":
            continue
        return segment
    return None


def _has_media_segment(reply: OutboundReply) -> bool:
    for segment in reply.segments:
        metadata = segment.metadata or {}
        msg_type = str(metadata.get("wxbot_msg_type") or metadata.get("msg_type") or "").lower()
        if segment.type not in _TEXT_REPLY_TYPES or msg_type == "image":
            return True
    return False


def _current_user_text(ctx: Any) -> str:
    pre = getattr(ctx, "pre", None)
    cleaned = str(getattr(pre, "cleaned_text", "") or "").strip()
    if cleaned:
        return cleaned
    event = getattr(ctx, "event", None)
    metadata = getattr(event, "metadata", {}) or {}
    normalized = str(metadata.get("wxbot_normalized_content") or "").strip()
    if normalized:
        return normalized
    message = getattr(event, "message", None)
    return str(getattr(message, "content", "") or "").strip()


def _bot_aliases(ctx: Any, *, settings: Any | None = None) -> tuple[str, ...]:
    values: list[str] = []
    values.extend(_split_aliases(getattr(settings, "response_guard_bot_aliases", "")))

    event = getattr(ctx, "event", None)
    session = getattr(ctx, "session", None)
    reply = getattr(ctx, "reply", None)
    sources = [
        getattr(event, "metadata", {}) or {},
        getattr(session, "metadata", {}) or {},
        getattr(session, "variables", {}) or {},
        getattr(reply, "metadata", {}) or {},
    ]
    keys = (
        "bot_name",
        "bot_display_name",
        "bot_mention",
        "bot_alias",
        "bot_aliases",
        "bot_names",
        "assistant_name",
        "assistant_display_name",
        "assistant_aliases",
        "self_name",
        "self_names",
        "my_name",
        "my_names",
        "wxbot_my_names",
    )
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            values.extend(_split_aliases(source.get(key)))

    values.extend(_DEFAULT_BOT_ALIASES)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = _clean_alias(value)
        if len(alias) < 2 or alias in seen:
            continue
        seen.add(alias)
        normalized.append(alias)
    return tuple(normalized)


def _split_aliases(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item for item in re.split(r"[,，|/\s]+", value) if item]
    if isinstance(value, Iterable):
        return [str(item) for item in value if str(item or "").strip()]
    return [str(value)]


def _clean_alias(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = text.removeprefix("@").strip()
    return re.sub(r"[\s\u200b\u200c\u200d]+", "", text)


def _preferred_identity(aliases: tuple[str, ...]) -> str:
    if "zzz" in aliases:
        return "zzz"
    return aliases[0] if aliases else "zzz"


def _self_reference_replacement(
    *,
    user_text: str,
    reply_text: str,
    aliases: tuple[str, ...],
    identity: str,
    english_only: bool = False,
) -> str | None:
    if not aliases:
        return None
    if not _tells_user_to_ask_self(reply_text, aliases):
        return None
    if _MODEL_QUESTION_RE.search(user_text):
        if english_only:
            return (
                f"I am {identity}. The exact backend model ID needs to be checked by an administrator; "
                "if this runtime exposes it, I can look it up."
            )
        return (
            f"我就是 {identity}。具体后台 model id 需要管理员在后台查看；"
            "如果当前运行环境暴露了 model id，我可以直接查。"
        )
    if _is_credit_account_intent(user_text):
        if english_only:
            return f"I am {identity}. Ask me to check the credit balance."
        return f"我就是 {identity}。要查积分余额，请发送 /余额。"
    if english_only:
        return f"I am {identity}. Send me what you need handled directly."
    return f"我就是 {identity}。我不能把问题转给自己；请直接把要处理的内容发给我。"


def _is_credit_account_intent(text: str) -> bool:
    return bool(_CREDIT_ACCOUNT_INTENT_RE.search(str(text or "")))


def _tells_user_to_ask_self(reply_text: str, aliases: tuple[str, ...]) -> bool:
    text = unicodedata.normalize("NFKC", str(reply_text or "")).lower()
    for alias in aliases:
        escaped = re.escape(alias)
        target = rf"@?\s*{escaped}"
        patterns = (
            rf"(这|这个|这事|这问题)?\s*(得|要|需要|应该)?\s*再?\s*(问|找|咨询|联系)\s*{target}",
            rf"(让|叫|请)\s*{target}\s*(看|看看|处理|回复|回答|来)?",
            rf"{target}\s*(看|看看|处理|回复|回答|知道|来)",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            return True
    return False


def _should_replace_echo(
    ctx: Any,
    user_text: str,
    reply_text: str,
    aliases: tuple[str, ...],
) -> bool:
    if not user_text or not reply_text:
        return False
    if bool(getattr(ctx, "extras", {}).get("_command_token")):
        return False
    result = getattr(ctx, "result", None)
    result_metadata = getattr(result, "metadata", {}) or {}
    if isinstance(result_metadata, dict) and bool(result_metadata.get("response_guard_allow_echo")):
        return False
    reply = getattr(ctx, "reply", None)
    if isinstance(reply, OutboundReply) and _has_media_segment(reply):
        return False
    if _SUCCESS_CONFIRM_RE.match(reply_text.strip()):
        return False
    if _is_explicit_repeater_reply(ctx, reply_text, aliases):
        return False
    if _is_reply_to_repeated_user_chain(user_text, reply_text, aliases):
        return False

    user_norm = _compact_for_echo(user_text, aliases)
    reply_norm = _compact_for_echo(reply_text, aliases)
    if not user_norm or not reply_norm:
        return False
    if len(user_norm) < 6 and not _QUESTION_HINT_RE.search(user_text):
        return False

    stripped_reply = _strip_echo_scaffold(reply_norm)
    if reply_norm == user_norm or stripped_reply == user_norm:
        return True
    if user_norm in stripped_reply:
        extra_len = len(stripped_reply) - len(user_norm)
        return extra_len <= max(4, int(len(user_norm) * 0.2))
    length_ratio = len(stripped_reply) / max(len(user_norm), 1)
    if 0.75 <= length_ratio <= 1.25:
        similarity = difflib.SequenceMatcher(None, user_norm, stripped_reply).ratio()
        return similarity >= 0.88
    return False


def _is_explicit_repeater_reply(ctx: Any, reply_text: str, aliases: tuple[str, ...]) -> bool:
    for source in (getattr(ctx, "extras", {}) or {}, getattr(ctx, "signals", {}) or {}):
        if not isinstance(source, dict):
            continue
        if _repeater_signal_allows_echo(source.get("repeater"), reply_text, aliases):
            return True

    reply = getattr(ctx, "reply", None)
    if isinstance(reply, OutboundReply) and _metadata_marks_repeater(reply.metadata):
        return True

    result = getattr(ctx, "result", None)
    result_text = str(getattr(result, "reply_text", "") or "")
    result_route = getattr(result, "route", None)
    if _metadata_marks_repeater(getattr(result, "metadata", {}) or {}):
        return not result_text or _same_echo_text(result_text, reply_text, aliases)
    if result_route == RouteType.CANNED and _metadata_reason_is_repeater(
        getattr(result, "metadata", {}) or {}
    ):
        return not result_text or _same_echo_text(result_text, reply_text, aliases)
    return False


def _repeater_signal_allows_echo(signal: Any, reply_text: str, aliases: tuple[str, ...]) -> bool:
    if not isinstance(signal, dict) or signal.get("triggered") is not True:
        return False
    content = str(signal.get("content") or "")
    if content:
        return _same_echo_text(content, reply_text, aliases)
    return str(signal.get("reason") or "") in {"repeat_match", "repeater_triggered"}


def _metadata_marks_repeater(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    for key in ("plugin", "source", "owner", "route_owner", "capability", "handler"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip().lower() == "repeater":
            return True
    return _metadata_reason_is_repeater(metadata)


def _metadata_reason_is_repeater(metadata: Any) -> bool:
    if not isinstance(metadata, dict):
        return False
    for key in ("reason", "route_reason", "stop_reason"):
        if str(metadata.get(key) or "") in {"repeat_match", "repeater_triggered"}:
            return True
    return False


def _is_reply_to_repeated_user_chain(
    user_text: str,
    reply_text: str,
    aliases: tuple[str, ...],
) -> bool:
    unit = _repeated_chain_unit(user_text)
    if unit is None:
        return False
    unit_norm = _compact_for_echo(unit, aliases)
    if len(unit_norm) < 6 and not _QUESTION_HINT_RE.search(unit):
        return False
    return bool(unit_norm) and _same_echo_text(unit, reply_text, aliases)


def _repeated_chain_unit(text: str) -> str | None:
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not value:
        return None

    sentence_parts = [
        _normalize_repeat_unit(match.group(0))
        for match in re.finditer(r"[^。！？!?]+[。！？!?]?", value)
    ]
    sentence_parts = [part for part in sentence_parts if part]
    if _all_same_repeat_unit(sentence_parts):
        return sentence_parts[0]

    whitespace_parts = [_normalize_repeat_unit(part) for part in value.split()]
    whitespace_parts = [part for part in whitespace_parts if part]
    if _all_same_repeat_unit(whitespace_parts):
        return whitespace_parts[0]
    return None


def _normalize_repeat_unit(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _all_same_repeat_unit(parts: list[str]) -> bool:
    if len(parts) < 3:
        return False
    first = parts[0]
    return all(part == first for part in parts[1:])


def _same_echo_text(left: str, right: str, aliases: tuple[str, ...]) -> bool:
    return _compact_for_echo(left, aliases) == _compact_for_echo(right, aliases)


def _compact_for_echo(text: str, aliases: tuple[str, ...]) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = re.sub(r"[\u200b\u200c\u200d]", "", value)
    for alias in aliases:
        value = re.sub(rf"^\s*@\s*{re.escape(alias)}\b\s*", "", value)
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _strip_echo_scaffold(value: str) -> str:
    prefixes = (
        "你问的是",
        "您问的是",
        "你刚才问的是",
        "您刚才问的是",
        "你是想问",
        "您是想问",
        "你的问题是",
        "您的问题是",
        "问题是",
    )
    stripped = value
    for prefix in prefixes:
        if stripped.startswith(prefix) and len(stripped) > len(prefix):
            stripped = stripped[len(prefix) :]
            break
    for suffix in ("对吗", "是吗"):
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            break
    return stripped


def _echo_fallback(user_text: str, *, english_only: bool = False) -> str:
    if english_only:
        return "I did not generate a valid answer just now. Please send the question again."
    question = re.sub(r"\s+", " ", str(user_text or "")).strip() or "刚才的问题"
    if len(question) > 120:
        question = f"{question[:117]}..."
    question = question.rstrip("？?。.!！")
    return f"我刚才没有生成有效答案。你是想问：{question}？我可以重新查一下。"


def _sync_guarded_reply_metadata(
    ctx: Any,
    reply: OutboundReply,
    replacement: str,
    reason: str,
) -> None:
    reply.metadata["response_guard"] = {"applied": True, "reason": reason}
    result = getattr(ctx, "result", None)
    if result is not None:
        result.reply_text = replacement
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["response_guard"] = {"applied": True, "reason": reason}
        result.metadata = metadata
