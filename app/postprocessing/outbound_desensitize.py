"""Mask politically and sexually sensitive tokens in outbound chat text."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.common.config import get_settings

_DEFAULT_KEYWORDS_PATH = Path("config/outbound_desensitize_keywords.txt")
_LATIN_RE = re.compile(r"[A-Za-z]")
# Everyday 操 compounds stay visible; remaining 操 is treated as vulgar.
_操_PATTERN = r"操(?![作持劳盘场控纵练心守办神舵])"
_BUILTIN_KEYWORDS = (
    "做爱",
    "性交",
    "性爱",
    "操逼",
    "操你",
    "操她",
    "操死",
    "鸡巴",
    "阴茎",
    "阴道",
    "口交",
    "肛交",
    "乳交",
    "颜射",
    "射精",
    "自慰",
    "手淫",
    "强奸",
    "轮奸",
    "迷奸",
    "援交",
    "约炮",
    "卖淫",
    "嫖娼",
    "淫秽",
    "色情",
    "黄片",
    "裸照",
    "裸体",
    "乱伦",
    "兽交",
    "三级片",
    "成人片",
    "porn",
    "porno",
    "hentai",
    "fellatio",
    "cunnilingus",
    "习近平",
    "习总书记",
    "习主席",
    "毛泽东",
    "邓小平",
    "江泽民",
    "胡锦涛",
    "温家宝",
    "李克强",
    "赵紫阳",
    "周恩来",
    "六四事件",
    "六四",
    "天安门事件",
    "法轮大法",
    "法轮功",
    "台独",
    "藏独",
    "疆独",
    "港独",
    "反共",
    "灭共",
    "xi jinping",
    "falun gong",
    "falun dafa",
)


def desensitize_outbound_text(text: str) -> tuple[str, tuple[str, ...]]:
    """Replace porn/political key terms with ``*`` of the same visible length."""

    value = str(text or "")
    if not value:
        return value, ()
    pattern = _compiled_pattern()
    matched: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        matched.append(token)
        return "*" * max(1, len(token))

    sanitized = pattern.sub(_replace, value)
    return sanitized, tuple(matched)


def load_outbound_desensitize_keywords(path: str | Path | None = None) -> tuple[str, ...]:
    resolved = _resolve_keywords_path(path)
    if resolved is None or not resolved.exists():
        return _BUILTIN_KEYWORDS
    keywords: list[str] = []
    seen: set[str] = set()
    for line in resolved.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(item)
    return tuple(keywords) or _BUILTIN_KEYWORDS


@lru_cache(maxsize=1)
def _compiled_pattern() -> re.Pattern[str]:
    parts = [_操_PATTERN]
    for keyword in sorted(load_outbound_desensitize_keywords(), key=len, reverse=True):
        escaped = re.escape(keyword)
        if _LATIN_RE.search(keyword):
            parts.append(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])")
        else:
            parts.append(escaped)
    return re.compile("|".join(parts), re.IGNORECASE)


def _resolve_keywords_path(path: str | Path | None) -> Path | None:
    raw = Path(path) if path is not None else _DEFAULT_KEYWORDS_PATH
    if raw.is_absolute():
        return raw
    try:
        return get_settings().project_root / raw
    except Exception:
        return Path(__file__).resolve().parents[2] / raw
