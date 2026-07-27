from __future__ import annotations

from app.common.hashing import stable_hash

GLOBAL_SCOPE_SESSION_ID = ""


def normalize_scope_session_id(session_id: str | None) -> str:
    return str(session_id or "").strip()


def is_global_scope(session_id: str | None) -> bool:
    return normalize_scope_session_id(session_id) == GLOBAL_SCOPE_SESSION_ID


def scope_kind(session_id: str | None) -> str:
    return "global" if is_global_scope(session_id) else "session"


def scoped_collection_name(prefix: str, tenant_id: str, session_id: str | None = None) -> str:
    normalized = normalize_scope_session_id(session_id)
    base = f"{prefix}{tenant_id}"
    if not normalized:
        return base
    return f"{base}__s_{stable_hash(normalized)[:16]}"


def scope_payload(tenant_id: str, session_id: str | None = None) -> dict[str, str | None]:
    normalized = normalize_scope_session_id(session_id)
    return {
        "tenant_id": tenant_id,
        "scope": scope_kind(normalized),
        "session_id": normalized or None,
    }
