"""
API authentication for cs-system communication.

Runtime authorization (sealed_core capabilities) is handled by the
remote auth server via client.runtime_guard.RuntimeAuthGuard.

This module only provides the API token header for cs-system
inbound/outbound requests.
"""
import config


def api_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.CS_API_TOKEN}"}
