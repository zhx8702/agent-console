"""Prompt hashing, redaction, Unicode-safe chunking, and result aggregation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass

from plugins.prompt_audit.contracts import (
    AuditDecisionKind,
    AuditRequest,
    AuditRisk,
    RiskCategory,
    ScanResult,
)

_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_TOKEN_RE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}\b", re.I)
_SEPARATOR = "\n\n--- prior user context ---\n\n"


@dataclass(frozen=True, slots=True)
class AuditSnapshot:
    request_id: str
    prompt_hash: str
    redacted_preview: str
    prompt_length: int
    segment_count: int
    scan_text: str

    def redacted(self) -> AuditSnapshot:
        return AuditSnapshot(
            request_id=self.request_id,
            prompt_hash=self.prompt_hash,
            redacted_preview=self.redacted_preview,
            prompt_length=self.prompt_length,
            segment_count=self.segment_count,
            scan_text="",
        )


@dataclass(frozen=True, slots=True)
class AuditChunk:
    index: int
    total: int
    text: str


class PromptTooLargeError(ValueError):
    pass


def redact_preview(value: str, limit: int = 240) -> str:
    text = _TOKEN_RE.sub("<SECRET>", value)
    text = _EMAIL_RE.sub("<EMAIL>", text)
    text = _PHONE_RE.sub("<PHONE>", text)
    if limit <= 0:
        return ""
    graphemes = _split_graphemes(text)
    if len(graphemes) <= limit:
        return text
    return "".join(graphemes[: max(0, limit - 1)]) + "…"


def build_snapshot(
    request: AuditRequest,
    *,
    preview_chars: int = 240,
    max_input_chars: int = 65_536,
    max_prior_segments: int = 32,
) -> AuditSnapshot:
    if len(request.prior_text) > max_prior_segments:
        raise PromptTooLargeError("prompt_audit_too_many_segments")
    segments = tuple(
        text for text in (request.text, *request.prior_text) if str(text or "").strip()
    )
    total_chars = sum(len(segment) for segment in segments)
    total_chars += max(0, len(segments) - 1) * len(_SEPARATOR)
    if total_chars > max_input_chars:
        raise PromptTooLargeError("prompt_audit_input_too_large")
    scan_text = _SEPARATOR.join(segments)
    return AuditSnapshot(
        request_id=request.request_id,
        prompt_hash=hashlib.sha256(scan_text.encode("utf-8")).hexdigest(),
        redacted_preview=redact_preview(scan_text, preview_chars),
        prompt_length=len(_split_graphemes(scan_text)),
        segment_count=len(segments),
        scan_text=scan_text,
    )


def chunk_snapshot(snapshot: AuditSnapshot, limit: int) -> tuple[AuditChunk, ...]:
    if limit <= 0:
        raise ValueError("chunk limit must be positive")
    raw_chunks: list[str] = []
    for segment in snapshot.scan_text.split(_SEPARATOR):
        graphemes = _split_graphemes(segment)
        raw_chunks.extend(
            "".join(graphemes[start : start + limit])
            for start in range(0, len(graphemes), limit)
        )
    raw_chunks = [chunk for chunk in raw_chunks if chunk]
    total = len(raw_chunks)
    return tuple(
        AuditChunk(index=index, total=total, text=text)
        for index, text in enumerate(raw_chunks, start=1)
    )


def aggregate_scan_results(results: list[ScanResult]) -> ScanResult:
    if not results:
        raise ValueError("cannot aggregate empty prompt-audit results")
    severity = {
        AuditDecisionKind.ALLOW: 1,
        AuditDecisionKind.FLAG: 2,
        AuditDecisionKind.BLOCK: 3,
    }
    risk_severity = {
        AuditRisk.LOW: 1,
        AuditRisk.MEDIUM: 2,
        AuditRisk.HIGH: 3,
        AuditRisk.CRITICAL: 4,
    }
    worst = max(results, key=lambda result: severity[result.kind])
    risk = max(results, key=lambda result: risk_severity[result.risk]).risk
    categories: set[RiskCategory] = set()
    unknown: set[str] = set()
    for result in results:
        categories.update(result.categories)
        unknown.update(result.unknown_categories)
    return ScanResult(
        kind=worst.kind,
        risk=risk,
        safety=worst.safety,
        categories=tuple(sorted(categories, key=lambda item: item.value)),
        unknown_categories=tuple(sorted(unknown)),
        endpoint_id=worst.endpoint_id,
        latency_ms=sum(max(0, result.latency_ms) for result in results),
        chunk_count=len(results),
    )


def _split_graphemes(value: str) -> list[str]:
    """Split without separating combining marks, variation selectors, or ZWJ chains."""

    clusters: list[str] = []
    regional_count = 0
    for char in value:
        codepoint = ord(char)
        is_mark = bool(unicodedata.combining(char)) or unicodedata.category(char).startswith("M")
        is_variation = 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF
        is_regional = 0x1F1E6 <= codepoint <= 0x1F1FF
        joins_previous = bool(clusters) and (
            is_mark
            or is_variation
            or char == "\u200d"
            or clusters[-1].endswith("\u200d")
            or (is_regional and regional_count % 2 == 1)
        )
        if joins_previous:
            clusters[-1] += char
        else:
            clusters.append(char)
        regional_count = regional_count + 1 if is_regional else 0
    return clusters
