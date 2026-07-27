from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

_MENTION_NAME_RE = re.compile(r"@([^\s\u2005\u00a0]+)")


_RESEARCH_QUOTED_RE = re.compile(r"[\"“”'‘’《》〈〉「」『』](.{1,40}?)[\"“”'‘’《》〈〉「」『』]")


_RESEARCH_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9_./:-]{1,31}", re.IGNORECASE)


_RESEARCH_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,16}")


_RESEARCH_COMMAND_PREFIXES = (
    "/research",
    "/查记录",
    "/搜记录",
    "/查聊天",
    "/搜聊天",
)


_RESEARCH_GENERIC_TERMS = (
    "帮我",
    "给我",
    "帮忙",
    "麻烦",
    "找一下",
    "查一下",
    "查查",
    "查找",
    "找找",
    "搜索",
    "搜一下",
    "搜搜",
    "检索",
    "研究",
    "research",
    "最近",
    "聊天记录",
    "群聊记录",
    "消息记录",
    "记录",
    "消息",
    "内容",
    "情况",
    "有没有",
    "有无",
    "能不能",
    "是否",
    "相关",
    "一下",
    "看看",
    "关于",
    "里面",
    "里",
    "提到",
    "说过",
    "聊过",
    "讨论过",
    "讨论",
    "怎么配到",
    "怎么配置",
    "怎么配",
    "如何配置",
    "如何配",
    "怎么用",
    "如何用",
    "里面用",
    "来用",
    "一下子",
)


_RESEARCH_LOW_VALUE_ASCII_TERMS = {
    "app",
    "bot",
    "max",
    "plus",
    "pro",
    "vip",
}


_RESEARCH_ACTION_TERMS = (
    "封号",
    "封禁",
    "封了",
    "被封",
    "封",
    "禁言",
    "拉黑",
    "ban",
)


_RESEARCH_QUANTITY_TERMS = (
    "几个",
    "几位",
    "几人",
    "几次",
    "多少",
    "数量",
)


_RESEARCH_CJK_BIGRAM_STOP_CHARS = set("我你他她它的了么吗嘛呢吧啊呀说讲问")


_RESEARCH_EXPLICIT_NUMBER_RE = re.compile(r"(?:\d+|[一二两三四五六七八九十百千万]+)")


_RESEARCH_SOLUTION_HINTS = (
    "可以",
    "直接",
    "用",
    "改成",
    "切到",
    "设置",
    "配置",
    "接入",
    "加上",
    "加个",
    "需要",
    "就是",
    "走",
    "改为",
    "命令",
    "-m",
    "--",
    "放在",
    "下面",
    "目录",
    "文件",
    "json",
    ".json",
    ".codex",
    "config",
)


_PROFILE_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


_PROFILE_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


_PROFILE_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\w)")


_PROFILE_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{12,}|[a-z0-9_-]{24,}\.[a-z0-9_-]{12,}\.[a-z0-9_-]{12,}|[a-f0-9]{32,})\b"
)

_PROFILE_NAMED_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|密钥|令牌|密码)"
    r"\s*[:=]\s*[^&\s]+"
)


_PROFILE_ADDRESS_RE = re.compile(
    r"[\u4e00-\u9fff]{2,}(?:省|市|区|县|镇|乡|街道|路|街|弄|号楼?|单元|室)"
)


_PROFILE_TOPIC_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_.-]{2,30}|[\u4e00-\u9fff]{2,8}")


_PROFILE_TOPIC_STOPWORDS = {
    "这个",
    "那个",
    "今天",
    "最近",
    "有人",
    "我们",
    "你们",
    "他们",
    "大家",
    "一下",
    "可以",
    "还是",
    "就是",
    "不是",
    "没有",
    "需要",
    "怎么",
    "什么",
    "群里",
    "功能",
    "讨论",
    "提到",
    "我的",
    "联系",
}


def _coerce_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _normalize_research_question(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    lowered = text.lower()
    for prefix in _RESEARCH_COMMAND_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix) :].strip()
            lowered = text.lower()
            break

    text = re.sub(
        r"^(帮我|给我|帮忙|麻烦)?(找一下|找找|查一下|查查|搜一下|搜搜|搜索|检索|研究一下|研究|看看|看下|看一下)?",
        "",
        text,
    ).strip()
    text = re.sub(r"^(最近\s*(24\s*小时|一天|1天)内?)", "", text).strip()
    text = re.sub(r"^(有没有|有无|能不能|是否)\s*", "", text).strip()
    text = re.sub(r"[？?。!！]+$", "", text).strip()
    return re.sub(r"\s+", " ", text)


def _looks_like_research_command_message(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in _RESEARCH_COMMAND_PREFIXES)


def _append_research_candidate(candidates: list[str], token: str) -> None:
    cleaned = str(token or "").strip()
    if cleaned:
        candidates.append(cleaned)


def _extract_research_entity_terms(question: str) -> set[str]:
    normalized = _normalize_research_question(question)
    entities: set[str] = set()
    for pattern in (
        r"([\u4e00-\u9fff]{2,6})(?:说|讲|问|提到|表示|发|封|禁言)",
        r"(?:找|查|搜)([\u4e00-\u9fff]{2,6})(?:说|讲|问|提到|表示|发|封|禁言)",
    ):
        for match in re.findall(pattern, normalized):
            token = str(match or "").strip()
            if 2 <= len(token) <= 6:
                entities.add(token)
    return entities


def _question_has_semantic_anchor(question: str) -> bool:
    normalized = _normalize_research_question(question)
    if not _RESEARCH_CJK_RE.search(normalized):
        return False
    return bool(
        _extract_research_entity_terms(normalized)
        or any(term in normalized for term in _RESEARCH_ACTION_TERMS)
        or any(term in normalized for term in _RESEARCH_QUANTITY_TERMS)
    )


def _extract_research_keywords(question: str) -> list[str]:
    normalized = _normalize_research_question(question)
    if not normalized:
        return []

    candidates: list[str] = []
    for raw in _RESEARCH_QUOTED_RE.findall(normalized):
        _append_research_candidate(candidates, raw)

    lowered = normalized.lower()
    for token in _RESEARCH_ASCII_RE.findall(lowered):
        cleaned = token.strip()
        if len(cleaned) >= 2:
            _append_research_candidate(candidates, cleaned)

    for token in _extract_research_entity_terms(normalized):
        _append_research_candidate(candidates, token)

    for token in _RESEARCH_ACTION_TERMS:
        if token in normalized or token in lowered:
            _append_research_candidate(candidates, token)

    for token in _RESEARCH_QUANTITY_TERMS:
        if token in normalized:
            _append_research_candidate(candidates, token)

    if re.search(r"(怎么|如何).{0,4}配", normalized):
        _append_research_candidate(candidates, "配置")
    if re.search(r"(怎么|如何).{0,4}(用|使用)", normalized):
        _append_research_candidate(candidates, "使用")
    if "接入" in normalized:
        _append_research_candidate(candidates, "接入")

    text_for_cjk = normalized
    for token in _RESEARCH_GENERIC_TERMS:
        text_for_cjk = text_for_cjk.replace(token, " ")
    for token in _RESEARCH_CJK_RE.findall(text_for_cjk):
        cleaned = str(token or "").strip()
        if len(cleaned) >= 2:
            _append_research_candidate(candidates, cleaned)
            for index in range(0, len(cleaned) - 1):
                phrase = cleaned[index : index + 2]
                if any(char in _RESEARCH_CJK_BIGRAM_STOP_CHARS for char in phrase):
                    continue
                _append_research_candidate(candidates, phrase)
            if len(cleaned) > 4:
                _append_research_candidate(candidates, cleaned[-4:])
                _append_research_candidate(candidates, cleaned[-2:])
                _append_research_candidate(candidates, cleaned[:4])

    deduped: list[str] = []
    seen: set[str] = set()
    generic_terms = {item.lower() for item in _RESEARCH_GENERIC_TERMS}
    for token in candidates:
        normalized_token = token.strip().lower()
        if not normalized_token or normalized_token in seen or normalized_token in generic_terms:
            continue
        seen.add(normalized_token)
        deduped.append(token.strip())
    return deduped[:12]


def _research_keyword_category(question: str, token: str) -> str:
    lowered = str(token or "").strip().lower()
    if not lowered:
        return "other"
    if lowered in _RESEARCH_QUANTITY_TERMS:
        return "quantity"
    if lowered in _RESEARCH_ACTION_TERMS:
        return "action"
    if re.search(r"[a-z]", lowered) or re.search(r"\d", lowered):
        if lowered in _RESEARCH_LOW_VALUE_ASCII_TERMS and _question_has_semantic_anchor(question):
            return "low_value_ascii"
        return "ascii"
    if lowered in _extract_research_entity_terms(question):
        return "entity"
    return "cjk"


def _research_keyword_weight(question: str, token: str) -> int:
    category = _research_keyword_category(question, token)
    if category == "entity":
        return 9
    if category == "action":
        return 8
    if category == "quantity":
        return 4
    if category == "low_value_ascii":
        return 1
    if category == "ascii":
        return 6
    lowered = str(token or "").strip().lower()
    if len(lowered) >= 4:
        return 4
    return 2


def _score_research_message(
    question: str, keywords: list[str], message: dict[str, Any]
) -> tuple[int, list[str]]:
    text = str(message.get("text") or "")
    if _looks_like_research_command_message(text):
        return 0, []

    text_lower = text.lower()
    question_lower = _normalize_research_question(question).lower()
    matched_keywords: list[str] = []
    matched_categories: set[str] = set()
    score = 0

    if question_lower and len(question_lower) >= 4 and question_lower in text_lower:
        score += 10

    for token in keywords:
        lowered = str(token or "").strip().lower()
        if not lowered:
            continue
        if lowered in text_lower:
            matched_keywords.append(token)
            category = _research_keyword_category(question, lowered)
            matched_categories.add(category)
            score += _research_keyword_weight(question, lowered)

    if len(matched_keywords) >= 2:
        score += 4
    if len(matched_keywords) >= 3:
        score += 3
    if "entity" in matched_categories and "action" in matched_categories:
        score += 8
    if "action" in matched_categories and "quantity" in matched_categories:
        score += 4
    if "low_value_ascii" in matched_categories and len(matched_categories) == 1:
        score = min(score, 2)
    return score, matched_keywords


def _question_asks_quantity(question: str) -> bool:
    normalized = _normalize_research_question(question)
    return any(token in normalized for token in _RESEARCH_QUANTITY_TERMS)


def _message_has_explicit_quantity_evidence(question: str, text: str) -> bool:
    if not _question_asks_quantity(question):
        return True
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    number = _RESEARCH_EXPLICIT_NUMBER_RE.pattern
    action_terms = [
        re.escape(term)
        for term in _RESEARCH_ACTION_TERMS
        if term in _normalize_research_question(question).lower()
        or term in _normalize_research_question(question)
    ]
    if not action_terms:
        action_terms = [re.escape(term) for term in _RESEARCH_ACTION_TERMS]
    action = "(?:" + "|".join(action_terms) + ")"
    patterns = (
        rf"{action}.{{0,12}}{number}\s*(?:个|位|人|次|条|pro|号)?",
        rf"{number}\s*(?:个|位|人|次|条|pro|号)?.{{0,12}}{action}",
    )
    return any(re.search(pattern, cleaned, re.IGNORECASE) for pattern in patterns)


def _build_research_summary(
    *,
    question: str,
    hours: int,
    total: int,
    top_messages: list[dict[str, Any]],
    keywords: list[str],
) -> str:
    if total <= 0 or not top_messages:
        keyword_text = "、".join(keywords[:5]) if keywords else "无"
        return (
            f"最近 {hours} 小时内没有查到和“{question}”明显相关的聊天记录。"
            f"已尝试关键词：{keyword_text}。"
        )

    senders = [
        str(item.get("sender_name") or item.get("sender_wxid") or "未知成员")
        for item in top_messages
        if str(item.get("sender_name") or item.get("sender_wxid") or "").strip()
    ]
    sender_summary = "、".join(list(dict.fromkeys(senders))[:4]) or "未知成员"
    excerpt = str(top_messages[0].get("text") or "").strip()
    if len(excerpt) > 48:
        excerpt = excerpt[:48] + "..."
    if _question_asks_quantity(question) and not any(
        _message_has_explicit_quantity_evidence(question, str(item.get("text") or ""))
        for item in top_messages
    ):
        return (
            f"最近 {hours} 小时内查到 {total} 条疑似相关消息，但未找到明确数量证据。"
            f"最相关线索是：{excerpt}"
        )
    return (
        f"最近 {hours} 小时内查到 {total} 条疑似相关消息，主要发送者有 {sender_summary}。"
        f"最相关线索是：{excerpt}"
    )


def _looks_like_solution_message(text: str, matched_keywords: list[str]) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if _looks_like_research_command_message(cleaned):
        return False
    if len(matched_keywords) < 1:
        return False
    if cleaned.endswith(("?", "？")) and not any(
        token in lowered for token in ("codex -m", ".codex", "json", ".json", "config", "配置")
    ):
        return False
    return any(hint.lower() in lowered for hint in _RESEARCH_SOLUTION_HINTS)


def _score_solution_message(text: str, matched_keywords: list[str]) -> int:
    cleaned = str(text or "").strip()
    if not cleaned:
        return 0
    lowered = cleaned.lower()
    score = 0
    score += min(len(matched_keywords), 3) * 3
    if "codex -m" in lowered:
        score += 8
    if any(
        token in lowered for token in (".codex", "/.codex", "\\.codex", "json", ".json", "config")
    ):
        score += 7
    if any(
        token in lowered
        for token in ("放在", "下面", "目录", "文件", "配置", "设置", "切到", "改成", "改为")
    ):
        score += 5
    if any(token in lowered for token in ("可以", "直接", "就行", "即可", "先", "然后")):
        score += 2
    if cleaned.endswith(("?", "？")):
        score -= 4
    if len(cleaned) <= 6:
        score -= 2
    return score


def _extract_research_solutions(
    *,
    question: str,
    top_messages: list[dict[str, Any]],
) -> list[dict[str, str]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    normalized_question = _normalize_research_question(question).lower()
    for item in top_messages:
        text = str(item.get("text") or "").strip()
        matched_keywords = [
            str(keyword or "").strip()
            for keyword in (item.get("matched_keywords") or [])
            if str(keyword or "").strip()
        ]
        if not _looks_like_solution_message(text, matched_keywords):
            continue
        normalized_text = text.lower()
        if normalized_question and normalized_text == normalized_question:
            continue
        dedupe_key = normalized_text[:120]
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        excerpt = text if len(text) <= 100 else text[:100] + "..."
        candidates.append(
            {
                "score": _score_solution_message(text, matched_keywords),
                "ts": int(item.get("ts") or 0),
                "sender_name": str(
                    item.get("sender_name") or item.get("sender_wxid") or "未知成员"
                ).strip()
                or "未知成员",
                "timestamp": str(item.get("timestamp") or "").strip(),
                "text": excerpt,
            }
        )
    candidates.sort(
        key=lambda item: (
            int(item.get("score") or 0),
            int(item.get("ts") or 0),
        ),
        reverse=True,
    )
    solutions: list[dict[str, str]] = []
    for item in candidates[:3]:
        solutions.append(
            {
                "sender_name": str(item.get("sender_name") or "未知成员"),
                "timestamp": str(item.get("timestamp") or ""),
                "text": str(item.get("text") or ""),
            }
        )
    return solutions


def _profile_redact_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    text = _PROFILE_EMAIL_RE.sub("[redacted-email]", text)
    text = _PROFILE_PHONE_RE.sub("[redacted-phone]", text)
    text = _PROFILE_ID_CARD_RE.sub("[redacted-id]", text)
    text = _PROFILE_TOKEN_RE.sub("[redacted-token]", text)
    text = _PROFILE_NAMED_SECRET_RE.sub(r"\1=[redacted-token]", text)
    text = _PROFILE_ADDRESS_RE.sub("[redacted-address]", text)
    return re.sub(r"\s+", " ", text).strip()


def _profile_normalize_text(value: Any) -> str:
    return re.sub(r"[\s_\-—–·.]+", "", str(value or "").strip().lower())


def _profile_aliases_from_name(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    candidates = [text]
    candidates.extend(
        item.strip()
        for item in re.split(r"[\s/_|,，;；()（）\[\]【】<>《》:：\-—–]+", text)
        if item.strip()
    )
    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        key = item.lower()
        if len(item) < 2 or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:8]


def _profile_extract_terms(texts: list[str], aliases: set[str], *, limit: int = 8) -> list[str]:
    counter: Counter[str] = Counter()
    alias_keys = {_profile_normalize_text(item) for item in aliases if item}
    for text in texts:
        redacted = _profile_redact_text(text)
        for raw in _PROFILE_TOPIC_RE.findall(redacted):
            token = raw.strip()
            lowered = token.lower()
            if len(token) < 2 or lowered in _PROFILE_TOPIC_STOPWORDS:
                continue
            if _profile_normalize_text(token) in alias_keys:
                continue
            if re.fullmatch(r"\d+", token):
                continue
            counter[token] += 1
    return [token for token, _count in counter.most_common(limit)]


def _profile_evidence_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.md5("|".join(str(part or "") for part in parts).encode()).hexdigest()[:12]
    return f"evidence:{prefix}:{digest}"


def _profile_facet(
    *,
    facet_type: str,
    claim: str,
    confidence: float,
    evidence_refs: list[str],
    source_types: list[str],
    status: str = "candidate",
) -> dict[str, Any]:
    return {
        "facet_id": "facet:"
        + hashlib.md5(f"{facet_type}|{claim}|{'|'.join(evidence_refs)}".encode()).hexdigest()[:12],
        "type": facet_type,
        "claim": _profile_redact_text(claim),
        "confidence": round(max(0.0, min(float(confidence), 1.0)), 2),
        "sensitivity": "normal",
        "status": status,
        "source_types": source_types,
        "evidence_refs": evidence_refs,
    }


def _profile_candidate_text(candidate: dict[str, Any]) -> str:
    values = [
        candidate.get("display_name"),
        candidate.get("name"),
        candidate.get("platform"),
        candidate.get("url"),
        candidate.get("public_summary"),
        candidate.get("summary"),
        candidate.get("description"),
    ]
    values.extend(candidate.get("keywords") or [])
    values.extend(candidate.get("match_signals") or [])
    return " ".join(str(item or "") for item in values)


def _profile_candidate_url_platform(candidate: dict[str, Any]) -> str:
    platform = str(candidate.get("platform") or "").strip().lower()
    url = str(candidate.get("url") or "").strip().lower()
    if platform:
        return platform
    if "github.com" in url:
        return "github"
    if "bilibili.com" in url or "b23.tv" in url:
        return "bilibili"
    if "twitter.com" in url or "x.com" in url:
        return "x"
    if url:
        return "website"
    return "other"


def _profile_score_external_candidate(
    candidate: dict[str, Any],
    *,
    display_name: str,
    aliases: list[str],
    project_keywords: list[str],
) -> tuple[float, list[str], str]:
    text = _profile_candidate_text(candidate)
    text_key = _profile_normalize_text(text)
    display_key = _profile_normalize_text(display_name)
    signals: list[str] = []
    score = 0.0

    candidate_name = str(candidate.get("display_name") or candidate.get("name") or "").strip()
    if display_key and display_key == _profile_normalize_text(candidate_name):
        score += 0.3
        signals.append("exact_display_name")
    elif display_key and display_key in text_key:
        score += 0.25
        signals.append("display_name_mentioned")

    for alias in aliases:
        alias_key = _profile_normalize_text(alias)
        if not alias_key or alias_key == display_key:
            continue
        if alias_key in text_key:
            score += 0.1
            signals.append(f"alias:{alias}")

    for keyword in project_keywords[:6]:
        keyword_key = _profile_normalize_text(keyword)
        if keyword_key and keyword_key in text_key:
            score += 0.08
            signals.append(f"project_keyword:{keyword}")

    provided_signals = [
        str(item or "").strip()
        for item in (candidate.get("match_signals") or [])
        if str(item or "").strip()
    ]
    lowered_signals = {item.lower() for item in provided_signals}
    if "verified_by_group_evidence" in lowered_signals:
        score += 0.45
        signals.append("verified_by_group_evidence")
    if provided_signals:
        for signal in provided_signals:
            if signal not in signals:
                signals.append(signal)

    only_name_match = bool(signals) and all(
        item == "exact_display_name" or item.startswith("alias:") for item in signals
    )
    if only_name_match:
        score = min(score, 0.45)

    score = max(0.0, min(score, 0.95))
    if "verified_by_group_evidence" in lowered_signals and score >= 0.85:
        status = "matched"
    elif score >= 0.6:
        status = "needs_human_review"
    else:
        status = "candidate"
    return round(score, 2), list(dict.fromkeys(signals))[:10], status
