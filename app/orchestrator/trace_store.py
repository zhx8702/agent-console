"""Redis-backed FlowRunner trace snapshots.

The snapshot is intentionally small and safe for admin troubleshooting: it
keeps step/effect status metadata, not message payloads or user text.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

from app.orchestrator.runner import FlowRunResult, FlowRunStepTrace

TRACE_SNAPSHOT_SCHEMA_VERSION = 2
TRACE_SNAPSHOT_MODES = ("runtime", "shadow")


def flow_trace_snapshot_key(prefix: str, trace_id: str, mode: str) -> str:
    return f"{_clean_prefix(prefix)}:{_clean_key_part(trace_id)}:{_clean_key_part(mode)}"


async def write_flow_trace_snapshot(
    redis: Any,
    result: FlowRunResult,
    *,
    mode: str,
    ttl_seconds: int,
    key_prefix: str = "cs:flow:trace",
) -> dict[str, Any]:
    trace_id = str(result.trace_id or "").strip()
    normalized_mode = _normalize_mode(mode)
    if not trace_id:
        return {"stored": False, "reason": "missing_trace_id"}
    payload = flow_run_result_snapshot(result, mode=normalized_mode)
    key = flow_trace_snapshot_key(key_prefix, trace_id, normalized_mode)
    await redis.set(
        key,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ex=max(1, int(ttl_seconds or 1)),
    )
    return {"stored": True, "key": key, "mode": normalized_mode, "trace_id": trace_id}


async def read_flow_trace_snapshot(
    redis: Any,
    trace_id: str,
    *,
    mode: str,
    key_prefix: str = "cs:flow:trace",
) -> dict[str, Any] | None:
    normalized_trace_id = str(trace_id or "").strip()
    if not normalized_trace_id:
        return None
    key = flow_trace_snapshot_key(key_prefix, normalized_trace_id, _normalize_mode(mode))
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def read_flow_trace_snapshots(
    redis: Any,
    trace_id: str,
    *,
    key_prefix: str = "cs:flow:trace",
) -> dict[str, dict[str, Any] | None]:
    return {
        mode: await read_flow_trace_snapshot(
            redis,
            trace_id,
            mode=mode,
            key_prefix=key_prefix,
        )
        for mode in TRACE_SNAPSHOT_MODES
    }


def flow_run_result_snapshot(result: FlowRunResult, *, mode: str) -> dict[str, Any]:
    return {
        "schema_version": TRACE_SNAPSHOT_SCHEMA_VERSION,
        "mode": _normalize_mode(mode),
        "stored_at": datetime.now(UTC).isoformat(),
        "flow_name": result.flow_name,
        "flow_version": result.flow_version,
        "status": result.status,
        "ok": result.ok,
        "trace_id": result.trace_id,
        "tenant_id": result.tenant_id,
        "session_id": result.session_id,
        "stop_reason": result.stop_reason,
        "error": result.error,
        "decision_trace": _safe_decision_trace(result.decision_trace),
        "steps": [_step_to_snapshot(step) for step in result.steps],
        "effect_commits": [_safe_effect_record(item) for item in result.effect_commits],
        "effect_dispatches": [_safe_effect_record(item) for item in result.effect_dispatches],
    }


def _step_to_snapshot(step: FlowRunStepTrace) -> dict[str, Any]:
    return {
        "id": step.id,
        "kind": step.kind,
        "owner": step.owner,
        "status": step.status,
        "action": step.action,
        "reason": step.reason,
        "error": step.error,
        "elapsed_ms": step.elapsed_ms,
        "attempts": step.attempts,
    }


def _safe_effect_record(item: dict[str, object]) -> dict[str, object]:
    allowed_keys = {
        "type",
        "owner",
        "idempotency_key",
        "status",
        "commit_status",
        "error",
        "dry_run",
    }
    return {
        key: value
        for key, value in item.items()
        if key in allowed_keys and _safe_scalar(value)
    }


_DECISION_TRACE_FIELDS: dict[str, frozenset[str]] = {
    "intent": frozenset({"coarse", "language", "sensitive"}),
    "route": frozenset(
        {
            "type",
            "confidence",
            "reason",
            "rule",
            "confidence_basis",
            "matched_conditions",
        }
    ),
    "router_signals": frozenset(
        {
            "faq_matched",
            "faq_similarity",
            "faq_verdict",
            "faq_preview_failed",
            "faq_preview_error_class",
            "tool_intent_matched",
            "tools_available",
            "effective_tool_count",
            "policy_allowed",
            "tool_denial_reason",
            "tool_preflight_failed",
            "tool_preflight_error_class",
            "consecutive_fallbacks",
        }
    ),
    "agent": frozenset({"tool_scope"}),
    "result": frozenset(
        {
            "route",
            "fallback_from",
            "fallback_reason",
            "degradation_reason",
            "tool_preselection_verdict",
            "tool_preselection_selected",
            "tool_preselection_scores",
        }
    ),
}


def _safe_decision_trace(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, object] = {}
    for section, allowed_fields in _DECISION_TRACE_FIELDS.items():
        raw_section = value.get(section)
        if not isinstance(raw_section, dict):
            continue
        safe_section: dict[str, object] = {}
        for key in allowed_fields:
            if key not in raw_section:
                continue
            raw_value = raw_section[key]
            if key in {"matched_conditions", "tool_preselection_selected"}:
                selected = _safe_scalar_list(raw_value)
                if selected:
                    safe_section[key] = selected
                continue
            if key == "tool_preselection_scores":
                scores = _safe_score_map(raw_value)
                if scores:
                    safe_section[key] = scores
                continue
            scalar = _safe_trace_scalar(raw_value)
            if scalar is not None:
                safe_section[key] = scalar
        if safe_section:
            safe[section] = safe_section
    return safe


def _safe_scalar_list(value: object, *, limit: int = 32) -> list[object]:
    if not isinstance(value, list | tuple | set):
        return []
    return [
        scalar
        for item in list(value)[:limit]
        if (scalar := _safe_trace_scalar(item)) is not None
    ]


def _safe_score_map(value: object, *, limit: int = 32) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, float] = {}
    for raw_name, raw_score in list(value.items())[:limit]:
        name = str(raw_name or "").strip()[:128]
        if not name or isinstance(raw_score, bool):
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            safe[name] = score
    return safe


def _safe_trace_scalar(value: object) -> object | None:
    if isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value.strip()[:256]
    return None


def _safe_scalar(value: object) -> bool:
    return isinstance(value, str | int | float | bool) or value is None


def _normalize_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    return normalized if normalized in TRACE_SNAPSHOT_MODES else "runtime"


def _clean_prefix(prefix: str) -> str:
    return str(prefix or "cs:flow:trace").strip().rstrip(":") or "cs:flow:trace"


def _clean_key_part(value: str) -> str:
    cleaned = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in cleaned)
