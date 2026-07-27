"""Runtime-only resolution for channel credential references.

The control plane persists references, never credential values.  Connector
processes call this module inside their own least-privilege environment.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ChannelSecretReferenceError(RuntimeError):
    """A credential reference cannot be resolved by this connector process."""


def resolve_channel_secret_ref(
    secret_ref: str,
    *,
    environ: Mapping[str, str] | None = None,
    allowed_environment_variables: frozenset[str] | set[str] | None = None,
) -> str:
    """Resolve an ``env:``/``env://`` reference without logging its value.

    Vault and cloud secret-manager schemes are valid control-plane references,
    but require a deployment-specific provider integration and therefore fail
    closed in the bundled worker.
    """

    reference = str(secret_ref or "").strip()
    if not reference:
        return ""
    lowered = reference.lower()
    if lowered.startswith("env://"):
        variable = reference[6:]
    elif lowered.startswith("env:"):
        variable = reference[4:]
    else:
        scheme = reference.split(":", 1)[0].lower() or "unknown"
        raise ChannelSecretReferenceError(
            f"channel secret provider is not available in this worker: {scheme}"
        )
    if not _ENV_NAME.fullmatch(variable):
        raise ChannelSecretReferenceError("invalid channel environment secret reference")
    if (
        allowed_environment_variables is not None
        and variable not in allowed_environment_variables
    ):
        raise ChannelSecretReferenceError(
            "channel environment secret reference is not allowed for this adapter"
        )
    source = os.environ if environ is None else environ
    value = str(source.get(variable, "") or "").strip()
    if not value:
        raise ChannelSecretReferenceError(
            f"referenced channel environment secret is not configured: {variable}"
        )
    return value


__all__ = ["ChannelSecretReferenceError", "resolve_channel_secret_ref"]
