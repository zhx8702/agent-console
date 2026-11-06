"""Structural evidence checks; semantic truth still requires a reviewer.

This module never infers a preference from keywords or message counts. Source
attribution and literal grounding are prerequisites, not a semantic verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

EVIDENCE_POLICY_VERSION = "group-evidence-v2"
LLM_SOURCE = "llm_group_window"
_PLACEHOLDERS = frozenset(
    {
        "[图片]",
        "[语音]",
        "[视频]",
        "[表情]",
        "[文件]",
        "[image]",
        "[audio]",
        "[video]",
    }
)


def _text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _contains_term(body: str, term: str) -> bool:
    # Prevent a short Latin product name from matching inside an unrelated word.
    normalized = _text(term)
    pattern = re.escape(normalized)
    if re.fullmatch(r"[\w .+/-]+", normalized, flags=re.ASCII):
        pattern = rf"(?<![a-z0-9_]){pattern}(?![a-z0-9_])"
    return bool(pattern and re.search(pattern, _text(body)))


def assess_group_evidence(relation: dict, evidence: list[dict]) -> dict[str, Any]:
    """Assess cited rows normalized by the store, without returning their text.

    Each row carries source/id/member/text/type. Missing sources must be passed
    as rows with an empty body; context from a different speaker never counts
    as the subject's own supporting statement.
    """
    subject = str(relation.get("subject") or "")
    obj = str(relation.get("object") or "")
    reasons: set[str] = set()
    seen_ids: set[tuple[str, str]] = set()
    subject_texts: set[str] = set()
    speakers: set[str] = set()
    grounded = False
    usable = 0
    for row in evidence:
        key = (str(row.get("source") or ""), str(row.get("id") or ""))
        if not all(key) or key in seen_ids:
            reasons.add("ambiguous_evidence")
            continue
        seen_ids.add(key)
        body = _text(row.get("text"))
        if (
            not body
            or body in _PLACEHOLDERS
            or row.get("is_self_sent")
            or str(row.get("type") or "text") not in {"text", "1"}
        ):
            reasons.add("non_text_or_missing_evidence")
            continue
        member = str(row.get("member") or "")
        speakers.add(member)
        usable += 1
        if member == subject and subject:
            subject_texts.add(body)
            if obj and _contains_term(body, obj):
                grounded = True
    if not evidence:
        reasons.add("missing_evidence")
    if not subject_texts:
        reasons.add("no_subject_statement")
    if relation.get("object_type", "person") == "person":
        grounded = bool(obj and obj in speakers)
    if not grounded:
        reasons.add("object_not_grounded")
    return {
        "version": EVIDENCE_POLICY_VERSION,
        "status": "needs_review" if reasons else "grounded",
        "reasons": sorted(reasons),
        "cited_count": len(evidence),
        "usable_count": usable,
        "subject_statement_count": len(subject_texts),
        "object_grounded": grounded,
    }


def item_value(item: dict) -> dict:
    value = item.get("value", item.get("value_json"))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def human_reviewed(acceptance: dict) -> bool:
    actor = str(acceptance.get("reviewed_by") or "").strip()
    return bool(actor and not actor.startswith("system/"))


def relation_review_fingerprint(value: dict) -> str:
    relation = dict(value.get("relation") or {})
    relation.pop("typesafe_shadow", None)
    reviewed = {
        "relation": relation,
        "events": value.get("source_event_ids"),
        "observations": value.get("source_observation_ids"),
    }
    return hashlib.sha256(
        json.dumps(reviewed, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def current_jev_review_accepts(value: dict) -> bool:
    relation = value.get("relation") or {}
    audit = value.get("jev") or {}
    if not isinstance(relation, dict) or not isinstance(audit, dict):
        return False
    quality = relation.get("evidence_quality") or {}
    return bool(
        isinstance(quality, dict)
        and audit.get("applied") is True
        and audit.get("effective_decision") == "accepted"
        and audit.get("evidence_policy_version") == EVIDENCE_POLICY_VERSION
        and quality.get("status") == "grounded"
        and quality.get("version") == EVIDENCE_POLICY_VERSION
        and audit.get("relation_hash") == relation_review_fingerprint(value)
    )


def group_recall_block_reason(item: dict) -> str:
    """Keep unreviewed inferred preferences out of every prompt retrieval path.

    Old rows stay available to management/review; this does not rewrite them.
    Pinning does not substitute for accepting a relationship's factual claim.
    """
    if item.get("source_type") != LLM_SOURCE:
        return ""
    value = item_value(item)
    relation = value.get("relation") or {}
    acceptance = value.get("acceptance") or {}
    if not isinstance(relation, dict) or not isinstance(acceptance, dict):
        return "group_evidence_requires_review"
    if human_reviewed(acceptance):
        return ""
    quality = relation.get("evidence_quality") or {}
    if not isinstance(quality, dict):
        return "group_evidence_requires_review"
    if quality.get("version") == EVIDENCE_POLICY_VERSION and quality.get("status") != "grounded":
        return "group_evidence_requires_review"
    if relation.get("predicate") == "interested_in":
        if not (
            acceptance.get("reviewed_by") == "system/jev" and current_jev_review_accepts(value)
        ):
            return "group_preference_requires_review"
    return ""
