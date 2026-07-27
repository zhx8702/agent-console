"""Small dependency-free authentication helpers for the local SDK API."""

from __future__ import annotations

import hmac
import math
from collections import Counter

MIN_API_TOKEN_LENGTH = 32
MIN_API_TOKEN_ENTROPY_BITS = 128.0
_PLACEHOLDER_TOKENS = frozenset(
    {
        "change-me",
        "changeme",
        "replace-me",
        "replace_with_a_random_token",
        "secret",
        "token",
    }
)


def api_token_is_high_entropy(api_token: str) -> bool:
    token = str(api_token or "").strip()
    if len(token) < MIN_API_TOKEN_LENGTH:
        return False
    normalized = token.lower().replace("-", "_")
    if normalized in _PLACEHOLDER_TOKENS or normalized.startswith("replace_with_"):
        return False
    counts = Counter(token)
    entropy_per_character = -sum(
        (count / len(token)) * math.log2(count / len(token))
        for count in counts.values()
    )
    return entropy_per_character * len(token) >= MIN_API_TOKEN_ENTROPY_BITS


def token_required_for_host(api_host: str, api_token: str) -> bool:
    """Return whether startup must fail because the API token is unsafe.

    ``api_host`` is retained for compatibility; authentication is mandatory on
    every interface, including loopback.
    """

    del api_host
    return not api_token_is_high_entropy(api_token)


def request_token_authorized(
    expected_token: str,
    *,
    authorization: str = "",
    header_token: str = "",
) -> bool:
    expected = str(expected_token or "").strip()
    if not api_token_is_high_entropy(expected):
        return False
    authorization = str(authorization or "").strip()
    bearer = (
        authorization[7:].strip()
        if authorization.lower().startswith("bearer ")
        else ""
    )
    supplied = bearer or str(header_token or "").strip()
    return bool(supplied and hmac.compare_digest(supplied, expected))
