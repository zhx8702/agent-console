"""Small, deterministic policy helpers for hosted web search.

Web search is an expensive capability.  The provider still owns the actual
tool shape, but the application decides whether the current turn has enough
evidence that live data was requested.  Keeping this decision in a dependency
free module lets the LLM, RAG, and FAQ routes agree without importing a
channel plugin into the core.
"""
from __future__ import annotations

import re
from typing import Any

_EXPLICIT_SEARCH_RE = re.compile(
    r"(?:联网|上网|网上|网络).{0,8}(?:搜|搜索|查|查询|检索)"
    r"|(?:搜|搜索|查|查询|检索).{0,12}(?:今天|今日|最新|实时|刚刚|热点|当前)"
    r"|(?:今天|今日|最新|实时|刚刚|热点|当前|现在).{0,12}"
    r"(?:搜|搜索|查|查询|检索)"
)
_FRESHNESS_RE = re.compile(
    r"(?:今天|今日|最新|实时|刚刚|热点|当前|现在|近期|最近|本周|本月|今年)"
)
_CURRENT_FACT_RE = re.compile(
    r"(?:天气|新闻|价格|股价|汇率|赛程|比赛|排名|公告|官网|来源|链接|政策|版本|模型|发布|更新)"
    r"|(?:是多少|是什么|怎么样|如何|哪家|哪个|几号|几时|吗[？?]?)"
)
_NEGATED_SEARCH_RE = re.compile(
    r"(?:不要|别|无需|无须|不用|不必|不需要|禁止|请勿|切勿|不准|不允许|不想|没必要|避免).{0,14}$"
)
_LOCAL_DATA_RE = re.compile(r"(?:群|聊天|会话|历史).{0,4}(?:消息|记录)|知识库|本地(?:文件|数据)|模型记忆")
_CLAUSE_SPLIT_RE = re.compile(r"[,，。！？!?；;\n]|(?:但是|但|不过|然而|而是|改成|改为|然后|请(?!勿))")


def live_web_search_requested(
    text: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Return whether this turn explicitly needs live web evidence.

    A request-level ``*_web_search`` flag is authoritative.  Otherwise the
    helper accepts explicit search wording and common freshness questions such
    as “现在北京天气怎么样”.  Local transcript/knowledge-base queries are
    deliberately excluded unless the user also says “联网/网上”.
    """

    raw_metadata = metadata if isinstance(metadata, dict) else {}
    if raw_metadata.get("openai_web_search_required") is True:
        return True
    if raw_metadata.get("web_search_required") is True:
        return True
    for key in ("openai_web_search", "web_search", "web_search_requested"):
        if key in raw_metadata:
            return bool(raw_metadata.get(key))

    value = str(text or "").strip()
    if not value:
        return False
    for clause in _CLAUSE_SPLIT_RE.split(value):
        candidate = str(clause or "").strip()
        if not candidate:
            continue
        for match in _EXPLICIT_SEARCH_RE.finditer(candidate):
            prefix = candidate[: match.start()]
            if _NEGATED_SEARCH_RE.search(prefix):
                continue
            if _LOCAL_DATA_RE.search(candidate) and not re.search(r"(?:联网|上网|网上|网络)", match.group(0)):
                continue
            return True
        if _FRESHNESS_RE.search(candidate) and _CURRENT_FACT_RE.search(candidate):
            if not _NEGATED_SEARCH_RE.search(candidate):
                return True
    return False
