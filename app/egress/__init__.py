"""Outbound delivery and fail-closed HTTP egress primitives."""

from app.egress.safe_http import (
    safe_http_request,
    safe_trusted_service_request,
    safe_trusted_service_stream,
    trusted_service_policy,
    trusted_service_url,
)

__all__ = [
    "safe_http_request",
    "safe_trusted_service_request",
    "safe_trusted_service_stream",
    "trusted_service_policy",
    "trusted_service_url",
]
