"""Strict, non-executable metadata contract for generic plugin controls."""

from __future__ import annotations

from typing import Any

_ALLOWED_KEYS = frozenset({"scope", "label", "summary"})
_ALLOWED_SCOPES = frozenset({"global", "tenant", "session", "group"})


def validate_plugin_admin_ui(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("plugin admin_ui must be an object")
    unknown = sorted(set(value) - _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"plugin admin_ui contains unsupported key: {unknown[0]}")
    if not value:
        return
    scope = value.get("scope")
    if not isinstance(scope, str) or scope not in _ALLOWED_SCOPES:
        raise ValueError("plugin admin_ui scope is invalid")
    for key, maximum in (("label", 128), ("summary", 1024)):
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > maximum:
            raise ValueError(f"plugin admin_ui {key} must be a bounded string")


__all__ = ["validate_plugin_admin_ui"]
