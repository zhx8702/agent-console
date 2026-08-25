"""M13 Postprocessor: PII restore, citation formatting, length guard, envelope."""
from __future__ import annotations

import re

from app.common.context import get_trace_id
from app.common.logging import get_logger
from app.common.types import (
    CapabilityResult,
    OutboundReply,
    ReplySegment,
    ReplyType,
    Session,
)
from app.common.utils import truncate

log = get_logger(__name__)

_MAX_CHARS = 4000
_TRUNCATE_SUFFIX = "\u2026"  # single-char ellipsis
_PII_PLACEHOLDER_RE = re.compile(r"<PII:[^>]+>")
_LEAKED_REPLACEMENT = "[敏感信息]"
_WEB_SEARCH_CITATION_SOURCES = frozenset({"openai_web_search", "grok_web_search"})
_WEB_SEARCH_CITATION_SOURCES_LOWER = frozenset(
    source.lower() for source in _WEB_SEARCH_CITATION_SOURCES
)
_INLINE_WEB_CITATION_RE = re.compile(
    r"(?:\[\[\s*\d+\s*\]\]|【\s*\d+\s*】|\[\s*\d+\s*\])"
    r"(?:\([^\n)]*\))?"
)
_INLINE_WEB_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(https?://[^)\s]+\)")
_INLINE_WEB_URL_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
_WEB_SEARCH_REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:参考资料|参考来源|来源列表|sources?|references?)\s*[:：]?\s*$",
    re.IGNORECASE,
)
_WEB_SEARCH_SOURCE_LINE_RE = re.compile(
    r"^\s*(?:来源|source|sources?|reference|references?)\s*[:：]\s*.+$",
    re.IGNORECASE,
)
_WEB_SEARCH_URL_LINE_RE = re.compile(
    r"^\s*(?:\[\s*\d+\s*\]\s*)?(?:[^\n]{0,160}\s+-\s+)?https?://\S+\s*$",
    re.IGNORECASE,
)


def _restore_pii(text: str, pii_map: dict[str, str]) -> str:
    if not text or not pii_map:
        return text
    # Replace longer placeholders first to avoid prefix collisions
    # (e.g. ``<PII:phone:1>`` vs ``<PII:phone:10>``).
    for placeholder in sorted(pii_map.keys(), key=len, reverse=True):
        if placeholder in text:
            text = text.replace(placeholder, pii_map[placeholder])
    return text


def _strip_leaked_placeholders(text: str) -> str:
    return _PII_PLACEHOLDER_RE.sub(_LEAKED_REPLACEMENT, text)


def _is_web_search_citation(citation: object) -> bool:
    return (
        str(getattr(citation, "source", "") or "").strip().lower()
        in _WEB_SEARCH_CITATION_SOURCES_LOWER
    )


def _strip_web_search_artifacts(text: str) -> str:
    """Keep the answer while hiding raw search/citation scaffolding."""

    compact = _INLINE_WEB_CITATION_RE.sub("", str(text or ""))
    compact = _INLINE_WEB_MARKDOWN_LINK_RE.sub(r"\1", compact)
    kept_lines: list[str] = []
    for line in compact.splitlines():
        if _WEB_SEARCH_REFERENCE_HEADING_RE.match(line):
            break
        if _WEB_SEARCH_SOURCE_LINE_RE.match(line) or _WEB_SEARCH_URL_LINE_RE.match(line):
            continue
        kept_lines.append(_INLINE_WEB_URL_RE.sub("", line).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()


def _format_citations(citations: list) -> str:
    if not citations:
        return ""
    lines = ["", "", "参考资料："]
    for i, c in enumerate(citations, start=1):
        title = getattr(c, "title", None) or getattr(c, "source", None) or getattr(c, "id", "")
        url = getattr(c, "url", None)
        if url:
            lines.append(f"[{i}] {title} - {url}")
        else:
            lines.append(f"[{i}] {title}")
    return "\n".join(lines)


def _normalize_text(text: str, pii_map: dict[str, str]) -> str:
    text = _restore_pii(text, pii_map)
    if "<PII:" in text:
        text = _strip_leaked_placeholders(text)
    return truncate(text, _MAX_CHARS, suffix=_TRUNCATE_SUFFIX)


def _build_segments(
    result: CapabilityResult,
    session: Session,
    citations: list,
    *,
    hide_web_search_artifacts: bool = False,
) -> tuple[list[ReplySegment], ReplyType]:
    raw_segments = result.metadata.get("reply_segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raw_text = result.reply_text or ""
        if hide_web_search_artifacts:
            raw_text = _strip_web_search_artifacts(raw_text)
        text = _normalize_text(raw_text, session.pii_map or {})
        footer = _format_citations(citations)
        if footer:
            text = truncate(text + footer, _MAX_CHARS, suffix=_TRUNCATE_SUFFIX)
        reply_type = ReplyType.MARKDOWN if citations else ReplyType.TEXT
        return [ReplySegment(type=reply_type, content=text)], reply_type

    segments: list[ReplySegment] = []
    first_text_idx: int | None = None
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        metadata = raw.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        segment_type_raw = str(raw.get("type") or ReplyType.TEXT.value)
        try:
            segment_type = ReplyType(segment_type_raw)
        except ValueError:
            segment_type = ReplyType.TEXT

        content = str(raw.get("content") or "")
        wxbot_msg_type = str(
            metadata.get("wxbot_msg_type") or metadata.get("msg_type") or ""
        ).strip().lower()
        if wxbot_msg_type != "image":
            if hide_web_search_artifacts:
                content = _strip_web_search_artifacts(content)
            content = _normalize_text(content, session.pii_map or {})
            if first_text_idx is None:
                first_text_idx = len(segments)

        segments.append(
            ReplySegment(
                type=segment_type,
                content=content,
                metadata=metadata,
            )
        )

    if not segments:
        raw_text = result.reply_text or ""
        if hide_web_search_artifacts:
            raw_text = _strip_web_search_artifacts(raw_text)
        text = _normalize_text(raw_text, session.pii_map or {})
        reply_type = ReplyType.MARKDOWN if citations else ReplyType.TEXT
        return [ReplySegment(type=reply_type, content=text)], reply_type

    footer = _format_citations(citations)
    if footer:
        if first_text_idx is not None and first_text_idx < len(segments):
            segments[first_text_idx].content = truncate(
                segments[first_text_idx].content + footer,
                _MAX_CHARS,
                suffix=_TRUNCATE_SUFFIX,
            )
            if segments[first_text_idx].type == ReplyType.TEXT:
                segments[first_text_idx].type = ReplyType.MARKDOWN
        else:
            segments.append(ReplySegment(type=ReplyType.MARKDOWN, content=footer.strip()))

    if len(segments) > 1:
        return segments, ReplyType.MULTI
    return segments, segments[0].type


class Postprocessor:
    async def run(self, result: CapabilityResult, session: Session) -> OutboundReply:
        citations = list(result.citations or [])
        hide_web_search_artifacts = any(_is_web_search_citation(citation) for citation in citations)
        display_citations = [
            citation for citation in citations if not _is_web_search_citation(citation)
        ]
        segments, reply_type = _build_segments(
            result,
            session,
            display_citations,
            hide_web_search_artifacts=hide_web_search_artifacts,
        )

        metadata: dict = {
            "route": result.route.value,
            "tool_calls": [
                {"name": tc.name, "id": tc.id, "error": tc.error}
                for tc in (result.tool_calls or [])
            ],
        }
        # Surface any upstream metadata too.
        for k, v in (result.metadata or {}).items():
            metadata.setdefault(k, v)

        reply = OutboundReply(
            tenant_id=session.tenant_id,
            channel=session.channel,
            adapter_id=session.adapter_id,
            connection_id=session.connection_id,
            user_id=session.user_id,
            session_id=session.session_id,
            conversation_id=session.conversation_id,
            external_conversation_id=session.external_conversation_id,
            canonical_conversation_id=session.canonical_conversation_id,
            external_user_id=session.external_user_id,
            external_participant_id=session.external_participant_id,
            canonical_participant_id=session.canonical_participant_id,
            type=reply_type,
            segments=segments,
            citations=citations,
            trace_id=get_trace_id(),
            metadata=metadata,
        )

        log.debug(
            "postprocessor.done",
            route=result.route.value,
            reply_type=reply_type.value,
            length=sum(len(segment.content) for segment in segments),
            citations=len(citations),
        )
        return reply


def build_postprocessor() -> Postprocessor:
    return Postprocessor()
