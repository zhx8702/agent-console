"""Pseudonymous device identity helpers.

The client deliberately does not inspect or return hardware, operating-system,
account, or hostname identifiers. A random identifier is generated once and is
persisted in the client's private state file by :class:`AuthClient`.
"""

from __future__ import annotations

import re
import secrets

_DEVICE_ID_PATTERN = re.compile(r"^(?:wxbot-[A-Za-z0-9_-]{32,128}|[a-f0-9]{64})$")


def valid_pseudonymous_device_id(value: object) -> bool:
    return bool(_DEVICE_ID_PATTERN.fullmatch(str(value or "").strip()))


def collect_machine_info(existing_device_id: object = "") -> dict[str, str]:
    """Return only a pseudonymous device id, reusing a valid persisted value."""

    candidate = str(existing_device_id or "").strip()
    if not valid_pseudonymous_device_id(candidate):
        candidate = f"wxbot-{secrets.token_urlsafe(32)}"
    return {"device_id": candidate}
