from __future__ import annotations

import re
from dataclasses import dataclass

from app.rag.retriever import RetrievalHit

_REFERENCE_RE = re.compile(r"\[(\d+)\]")
_ASCII_RE = re.compile(r"[a-z0-9][a-z0-9_-]*", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


@dataclass(frozen=True)
class CitationValidation:
    valid: bool
    references: tuple[int, ...]
    invalid_references: tuple[int, ...]
    unsupported_references: tuple[int, ...]
    reason: str = ""


def _semantic_tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    tokens = {match.group(0) for match in _ASCII_RE.finditer(lowered)}
    chars = [match.group(0) for match in _CJK_RE.finditer(lowered)]
    tokens.update(chars)
    tokens.update("".join(chars[index : index + 2]) for index in range(len(chars) - 1))
    return tokens


def _support_ratio(answer: str, evidence: str) -> float:
    answer_tokens = _semantic_tokens(_REFERENCE_RE.sub("", answer))
    if len(answer_tokens) < 2:
        # Very short acknowledgements have too little material for lexical
        # entailment; structural citation validation still applies.
        return 1.0
    evidence_tokens = _semantic_tokens(evidence)
    return len(answer_tokens.intersection(evidence_tokens)) / max(1, len(answer_tokens))


def validate_cited_answer(
    answer: str,
    hits: list[RetrievalHit],
    *,
    support_threshold: float = 0.08,
) -> CitationValidation:
    references = tuple(dict.fromkeys(int(value) for value in _REFERENCE_RE.findall(answer or "")))
    if not references:
        return CitationValidation(False, (), (), (), "missing_citation")
    invalid = tuple(ref for ref in references if ref < 1 or ref > len(hits))
    if invalid:
        return CitationValidation(False, references, invalid, (), "unknown_citation")
    unsupported = tuple(
        ref
        for ref in references
        if _support_ratio(answer, hits[ref - 1].content) < support_threshold
    )
    if unsupported:
        return CitationValidation(False, references, (), unsupported, "unsupported_citation")
    return CitationValidation(True, references, (), (), "ok")
