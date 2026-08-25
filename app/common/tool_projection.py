"""Bound tool output before it is placed back into a model-visible message."""
from __future__ import annotations

import json
from typing import Any

_LONG_TEXT_KEYS = frozenset({"content", "text", "body", "stdout", "stderr", "markdown"})
_DROP_KEYS = frozenset({"channel_reply_effects"})


def project_tool_result(result: Any, *, max_chars: int = 6_000) -> Any:
    """Return a deterministic, JSON-sized projection of a tool result.

    Tool handlers may return a complete file, a large search payload, or a
    nested SDK response.  The full value remains available to the handler and
    durable audit path; only the model-visible projection is capped.
    """

    limit = max(256, int(max_chars or 6_000))
    projected = _project_value(result, depth=0)
    encoded = _encode(projected)
    if len(encoded) <= limit:
        return projected
    preview_limit = max(64, limit - 120)
    return {
        "_projection": "truncated",
        "original_chars": len(encoded),
        "preview": encoded[:preview_limit].rstrip() + "…",
    }


def _project_value(value: Any, *, depth: int) -> Any:
    if depth >= 4:
        return _compact_scalar(value, limit=240)
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:80]:
            key = str(raw_key or "")[:120]
            if not key or key in _DROP_KEYS:
                continue
            projected[key] = _project_value(raw_value, depth=depth + 1)
        return projected
    if isinstance(value, (list, tuple, set)):
        return [_project_value(item, depth=depth + 1) for item in list(value)[:32]]
    if isinstance(value, str):
        limit = 4_000 if depth <= 1 else 1_200
        return _compact_scalar(value, limit=limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _compact_scalar(value, limit=600)


def _compact_scalar(value: Any, *, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _encode(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return str(value)
