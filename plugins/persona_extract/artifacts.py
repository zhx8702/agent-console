from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

_PINYIN_MAP = {
    "\u963f": "a", "\u5b89": "an", "\u6602": "ang", "\u5965": "ao",
    "\u5df4": "ba", "\u767d": "bai", "\u534a": "ban", "\u5305": "bao",
    "\u5317": "bei", "\u672c": "ben", "\u5175": "bing", "\u6ce2": "bo",
    "\u4e0d": "bu",
    "\u624d": "cai", "\u66f9": "cao", "\u5e38": "chang", "\u671d": "chao",
    "\u8f66": "che", "\u6210": "cheng", "\u6c60": "chi", "\u5d07": "chong",
    "\u51fa": "chu", "\u6625": "chun", "\u6b64": "ci", "\u4ece": "cong",
    "\u5927": "da", "\u5f85": "dai", "\u4e39": "dan", "\u5f53": "dang",
    "\u5200": "dao", "\u5fb7": "de", "\u5f97": "de", "\u5730": "di",
    "\u7535": "dian", "\u4e01": "ding", "\u4e1c": "dong", "\u90fd": "dou",
    "\u675c": "du", "\u6bb5": "duan",
    "\u5c14": "er",
    "\u53d1": "fa", "\u65b9": "fang", "\u98de": "fei", "\u4e30": "feng",
    "\u51af": "feng", "\u798f": "fu", "\u4ed8": "fu",
    "\u7518": "gan", "\u521a": "gang", "\u9ad8": "gao", "\u6208": "ge",
    "\u7ed9": "gei", "\u5de5": "gong", "\u53e4": "gu", "\u5173": "guan",
    "\u5149": "guang", "\u8d35": "gui", "\u56fd": "guo", "\u679c": "guo",
    "\u6d77": "hai", "\u97e9": "han", "\u822a": "hang", "\u597d": "hao",
    "\u8c6a": "hao", "\u4f55": "he", "\u548c": "he", "\u9ed1": "hei",
    "\u7ea2": "hong", "\u4faf": "hou", "\u80e1": "hu", "\u534e": "hua",
    "\u82b1": "hua", "\u9ec4": "huang", "\u6167": "hui", "\u706b": "huo",
    "\u5409": "ji", "\u5bb6": "jia", "\u8d3e": "jia", "\u5efa": "jian",
    "\u6c5f": "jiang", "\u59dc": "jiang", "\u5a07": "jiao", "\u6770": "jie",
    "\u91d1": "jin", "\u4eac": "jing", "\u666f": "jing", "\u9759": "jing",
    "\u5c45": "ju", "\u519b": "jun", "\u4fca": "jun",
    "\u5f00": "kai", "\u5eb7": "kang", "\u79d1": "ke", "\u53ef": "ke",
    "\u5b54": "kong", "\u5321": "kuang",
    "\u6765": "lai", "\u5170": "lan", "\u90ce": "lang", "\u52b3": "lao",
    "\u4e50": "le", "\u96f7": "lei", "\u51b7": "leng", "\u674e": "li",
    "\u4e3d": "li", "\u529b": "li", "\u8fde": "lian", "\u6881": "liang",
    "\u4eae": "liang", "\u6797": "lin", "\u4e34": "lin", "\u7075": "ling",
    "\u5218": "liu", "\u67f3": "liu", "\u9f99": "long", "\u9646": "lu",
    "\u8def": "lu", "\u9c81": "lu", "\u7eff": "lv", "\u5415": "lv",
    "\u7f57": "luo",
    "\u9a6c": "ma", "\u9ebb": "ma", "\u6ee1": "man", "\u6bdb": "mao",
    "\u6885": "mei", "\u7f8e": "mei", "\u7684": "de", "\u5b5f": "meng",
    "\u68a6": "meng", "\u7c73": "mi", "\u5999": "miao", "\u6c11": "min",
    "\u660e": "ming", "\u83ab": "mo", "\u6728": "mu", "\u6155": "mu",
    "\u5357": "nan", "\u5e74": "nian", "\u5b81": "ning", "\u725b": "niu",
    "\u519c": "nong",
    "\u6b27": "ou",
    "\u6f58": "pan", "\u5e9e": "pang", "\u88f4": "pei", "\u9e4f": "peng",
    "\u5e73": "ping",
    "\u9f50": "qi", "\u7947": "qi", "\u94b1": "qian", "\u5f3a": "qiang",
    "\u79e6": "qin", "\u9752": "qing", "\u6e05": "qing", "\u5e86": "qing",
    "\u4e18": "qiu", "\u79cb": "qiu", "\u5168": "quan",
    "\u4efb": "ren", "\u4eba": "ren", "\u65e5": "ri", "\u8363": "rong",
    "\u5982": "ru", "\u9510": "rui", "\u6da6": "run", "\u82e5": "ruo",
    "\u4e09": "san", "\u68ee": "sen", "\u6c99": "sha", "\u5c71": "shan",
    "\u5c1a": "shang", "\u90b5": "shao", "\u5c11": "shao", "\u7533": "shen",
    "\u6c88": "shen", "\u751f": "sheng", "\u58eb": "shi", "\u65f6": "shi",
    "\u53f2": "shi", "\u4e16": "shi", "\u5bff": "shou", "\u4e66": "shu",
    "\u53cc": "shuang", "\u6c34": "shui", "\u5b8b": "song", "\u677e": "song",
    "\u82cf": "su", "\u5b59": "sun",
    "\u592a": "tai", "\u8c2d": "tan", "\u6c64": "tang", "\u5510": "tang",
    "\u6843": "tao", "\u6d9b": "tao", "\u7530": "tian", "\u5929": "tian",
    "\u94c1": "tie", "\u4e07": "wan", "\u738b": "wang", "\u671b": "wang",
    "\u97e6": "wei", "\u4f1f": "wei", "\u5a01": "wei", "\u6587": "wen",
    "\u6b66": "wu", "\u5434": "wu", "\u4f0d": "wu",
    "\u897f": "xi", "\u590f": "xia", "\u5148": "xian", "\u8d24": "xian",
    "\u7965": "xiang", "\u5411": "xiang", "\u5c0f": "xiao", "\u6653": "xiao",
    "\u8c22": "xie", "\u65b0": "xin", "\u5174": "xing", "\u661f": "xing",
    "\u90a2": "xing", "\u718a": "xiong", "\u4fee": "xiu", "\u5f90": "xu",
    "\u65ed": "xu", "\u8bb8": "xu", "\u5ba3": "xuan", "\u5b66": "xue",
    "\u96ea": "xue", "\u859b": "xue",
    "\u4e25": "yan", "\u8a00": "yan", "\u989c": "yan", "\u6768": "yang",
    "\u9633": "yang", "\u6d0b": "yang", "\u59da": "yao", "\u8000": "yao",
    "\u53f6": "ye", "\u4e00": "yi", "\u6613": "yi", "\u4e49": "yi",
    "\u5c39": "yin", "\u94f6": "yin", "\u82f1": "ying", "\u6c38": "yong",
    "\u52c7": "yong", "\u7528": "yong", "\u4f18": "you", "\u6e38": "you",
    "\u4e8e": "yu", "\u4f59": "yu", "\u7389": "yu", "\u8bed": "yu",
    "\u8fdc": "yuan", "\u5143": "yuan", "\u8881": "yuan", "\u6708": "yue",
    "\u4e91": "yun",
    "\u66fe": "zeng", "\u5f20": "zhang", "\u8d75": "zhao", "\u90d1": "zheng",
    "\u6b63": "zheng", "\u77e5": "zhi", "\u5fd7": "zhi", "\u4e2d": "zhong",
    "\u5468": "zhou", "\u6731": "zhu", "\u7956": "zu", "\u5b97": "zong",
}

_FENCED_BLOCK_RE = re.compile(r"^```(?:markdown|md|json)?\s*(.*?)\s*```$", re.DOTALL)


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def strip_frontmatter(text: str) -> str:
    stripped = (text or "").lstrip("\ufeff")
    lines = stripped.splitlines(keepends=True)
    if not lines or lines[0].strip("\r\n") != "---":
        return stripped.strip()
    for idx in range(1, len(lines)):
        if lines[idx].strip("\r\n") == "---":
            return "".join(lines[idx + 1 :]).strip()
    return stripped.strip()


def unwrap_fenced_block(text: str) -> str:
    value = _clean_text(text)
    match = _FENCED_BLOCK_RE.match(value)
    if match:
        return match.group(1).strip()
    return value


def sanitize_markdown(text: str) -> str:
    value = unwrap_fenced_block(text)
    return value.strip()


def name_to_slug(name: str) -> str:
    if not name:
        return ""
    parts: list[str] = []
    for ch in name:
        if ch in _PINYIN_MAP:
            parts.append(_PINYIN_MAP[ch])
            continue
        if re.match(r"[a-zA-Z0-9]", ch):
            parts.append(ch.lower())
    return "".join(parts)


def resolve_skill_slug(
    target_user_id: str,
    target_name: str,
    existing_slugs: set[str] | None = None,
    preferred_slug: str = "",
) -> str:
    slug = _clean_text(preferred_slug) or name_to_slug(target_name)
    if not slug:
        compact = re.sub(r"[^a-zA-Z0-9]+", "", target_user_id or "").lower()
        slug = f"wxid-{compact[:8] or 'unknown'}"
    taken = {item for item in existing_slugs or set() if item}
    final = slug
    counter = 2
    while final in taken:
        final = f"{slug}-{counter}"
        counter += 1
    return final


def build_skill_frontmatter(slug: str, target_name: str, body: str) -> str:
    description = f'{target_name} — 基于聊天记录蒸馏'
    frontmatter = (
        "---\n"
        f"name: colleague-{slug}\n"
        f'description: "{description}"\n'
        "user-invocable: true\n"
        "---\n\n"
    )
    return frontmatter + strip_frontmatter(body).strip() + "\n"


def format_message_line(message: dict[str, Any], fallback_name: str) -> str:
    timestamp = _clean_text(message.get("timestamp"))
    sender_name = _clean_text(message.get("sender_name")) or fallback_name or "User"
    text = _clean_text(message.get("text"))
    if not text:
        return ""
    if timestamp:
        return f"[{timestamp}] {sender_name}: {text}"
    return f"{sender_name}: {text}"


def merge_message_lines(existing_lines: list[str], new_lines: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for line in [*existing_lines, *new_lines]:
        clean = _clean_text(line)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        merged.append(clean)
    return merged


def serialize_artifact(artifact: dict[str, Any] | None) -> str:
    if not artifact:
        return ""
    return json.dumps(artifact, ensure_ascii=False)


def parse_artifact(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not value:
        return None
    try:
        parsed = json.loads(str(value))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def infer_impression(skill_prompt: str, persona_md: str, target_name: str) -> str:
    for candidate in (skill_prompt, persona_md):
        for raw_line in candidate.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("-") or line.startswith("*"):
                continue
            if target_name and target_name in line:
                return line[:160]
            return line[:160]
    return ""


def build_meta(
    *,
    target_name: str,
    target_user_id: str,
    slug: str,
    session_name: str,
    session_id: str,
    message_count: int,
    first_timestamp: str,
    last_timestamp: str,
    previous_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    previous = previous_meta or {}
    created_at = _clean_text(previous.get("created_at")) or now_iso()
    updated_at = now_iso()
    display_session_name = session_name or session_id
    date_min = first_timestamp[:10] if first_timestamp else "?"
    date_max = last_timestamp[:10] if last_timestamp else "?"

    tags = previous.get("tags")
    if not isinstance(tags, dict):
        tags = {"personality": [], "culture": []}

    profile = previous.get("profile")
    if not isinstance(profile, dict):
        profile = {}

    source_sessions = previous.get("source_sessions")
    if not isinstance(source_sessions, list):
        source_sessions = []
    if session_id and session_id not in source_sessions:
        source_sessions.append(session_id)

    return {
        "name": target_name,
        "slug": slug,
        "wxid": target_user_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "version": _clean_text(previous.get("version")) or "v1",
        "profile": profile,
        "tags": tags,
        "impression": _clean_text(previous.get("impression")),
        "knowledge_sources": [
            f"{display_session_name} — {message_count} 条 ({date_min} ~ {date_max})"
        ],
        "message_count": message_count,
        "source_sessions": source_sessions,
        "corrections_count": int(previous.get("corrections_count") or 0),
    }


def build_artifact(
    *,
    slug: str,
    target_user_id: str,
    target_name: str,
    tenant_id: str,
    session_id: str,
    session_name: str,
    mode: str,
    channel: str,
    source_key: str,
    source_label: str,
    job_id: int | None,
    skill_prompt: str,
    skill_md: str,
    work_md: str,
    persona_md: str,
    meta: dict[str, Any],
    knowledge_lines: list[str],
    first_timestamp: str,
    last_timestamp: str,
    message_count: int | None = None,
) -> dict[str, Any]:
    return {
        "version": "persona-skill-v1",
        "generated_at": now_iso(),
        "slug": slug,
        "mode": mode,
        "target": {
            "user_id": target_user_id,
            "name": target_name,
        },
        "source": {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "session_name": session_name,
            "channel": channel,
            "source_key": source_key,
            "source_label": source_label,
            "job_id": job_id,
        },
        "knowledge": {
            "message_count": (
                max(0, int(message_count))
                if message_count is not None
                else len(knowledge_lines)
            ),
            "first_timestamp": first_timestamp,
            "last_timestamp": last_timestamp,
            "messages_text": "\n".join(knowledge_lines),
            "knowledge_sources": meta.get("knowledge_sources") or [],
            "source_sessions": meta.get("source_sessions") or [],
        },
        "files": {
            "SKILL.md": skill_md,
            "skill_prompt": skill_prompt,
            "work.md": work_md,
            "persona.md": persona_md,
        },
        "meta": meta,
    }


def build_manual_artifact(
    *,
    prompt_text: str,
    tenant_id: str,
    session_id: str,
    session_name: str,
    channel: str,
    source_key: str,
    source_label: str,
    target_user_id: str,
    target_name: str,
    skill_slug: str,
    previous_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seed = previous_artifact or {}
    files = seed.get("files") if isinstance(seed.get("files"), dict) else {}
    meta = seed.get("meta") if isinstance(seed.get("meta"), dict) else {}
    source = seed.get("source") if isinstance(seed.get("source"), dict) else {}
    slug = skill_slug or seed.get("slug") or resolve_skill_slug(target_user_id, target_name)
    body = strip_frontmatter(prompt_text)
    skill_md = build_skill_frontmatter(slug, target_name or slug, body)
    knowledge = seed.get("knowledge") if isinstance(seed.get("knowledge"), dict) else {}
    knowledge_lines = _clean_text(knowledge.get("messages_text")).splitlines()
    result_meta = build_meta(
        target_name=target_name or _clean_text(meta.get("name")) or slug,
        target_user_id=target_user_id or _clean_text(meta.get("wxid")),
        slug=slug,
        session_name=session_name or _clean_text(source.get("session_name")),
        session_id=session_id,
        message_count=max(len(knowledge_lines), int(knowledge.get("message_count") or 0)),
        first_timestamp=_clean_text(knowledge.get("first_timestamp")),
        last_timestamp=_clean_text(knowledge.get("last_timestamp")),
        previous_meta=meta,
    )
    if not result_meta.get("impression"):
        result_meta["impression"] = infer_impression(body, _clean_text(files.get("persona.md")), target_name)
    return build_artifact(
        slug=slug,
        target_user_id=target_user_id or _clean_text(result_meta.get("wxid")),
        target_name=target_name or _clean_text(result_meta.get("name")) or slug,
        tenant_id=tenant_id,
        session_id=session_id,
        session_name=session_name,
        mode=_clean_text(seed.get("mode")) or "manual",
        channel=channel,
        source_key=source_key,
        source_label=source_label,
        job_id=None,
        skill_prompt=body,
        skill_md=skill_md,
        work_md=_clean_text(files.get("work.md")),
        persona_md=_clean_text(files.get("persona.md")),
        meta=result_meta,
        knowledge_lines=knowledge_lines,
        first_timestamp=_clean_text(knowledge.get("first_timestamp")),
        last_timestamp=_clean_text(knowledge.get("last_timestamp")),
    )
