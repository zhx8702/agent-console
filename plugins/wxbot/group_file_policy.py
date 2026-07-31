from __future__ import annotations

from typing import Any, Protocol


class GroupFilePolicyReader(Protocol):
    async def get_group_policy(
        self,
        tenant_id: str,
        session_id: str,
    ) -> Any: ...


class GroupFileSendDenied(RuntimeError):
    """Fail-closed denial raised by the shared group file delivery gate."""

    def __init__(self, reason: str = "group_file_send_disabled") -> None:
        self.reason = str(reason or "group_file_send_disabled")
        super().__init__(self.reason)


async def require_group_file_send_enabled(
    policy_reader: GroupFilePolicyReader | None,
    *,
    tenant_id: str,
    session_id: str,
) -> None:
    """Require the explicit, versioned group file switch.

    Callers invoke this only for a verified group target.  Missing policy
    infrastructure and read failures are denied rather than silently enabling
    file delivery.
    """

    if policy_reader is None:
        raise GroupFileSendDenied("group_file_policy_unavailable")
    try:
        document = await policy_reader.get_group_policy(
            str(tenant_id or "").strip(),
            str(session_id or "").strip(),
        )
    except GroupFileSendDenied:
        raise
    except Exception as exc:
        raise GroupFileSendDenied("group_file_policy_unavailable") from exc
    policy = getattr(document, "policy", None)
    if not bool(getattr(policy, "file_send_enabled", False)):
        raise GroupFileSendDenied()
