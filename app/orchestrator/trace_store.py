"""Redis-backed FlowRunner trace snapshots.

The snapshot is intentionally small and safe for admin troubleshooting: it
keeps step/effect status metadata, not message payloads or user text.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.orchestrator.runner import FlowRunResult, FlowRunStepTrace

TRACE_SNAPSHOT_SCHEMA_VERSION = 1
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
