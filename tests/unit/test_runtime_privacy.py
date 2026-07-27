from __future__ import annotations

import json

from app.common.logging import redact_log_event
from wxbot_client.client.machine import collect_machine_info, valid_pseudonymous_device_id


def test_structured_logging_redacts_nested_private_values() -> None:
    private_values = {
        "text": "private chat text",
        "prompt": "private image prompt",
        "address": "private street address",
        "auth": {"session_token": "remote-token"},
        "runtime": {"host_path": r"C:\Users\private\client.exe"},
        "identity": {"self_wxid": "wxid_private", "my_names": ["Private User"]},
        "media_path": r"C:\Users\private\image.png",
        "error": "private exception body",
        "callback_url": "https://example.test/private/path",
    }
    event = {
        "event": "privacy.test",
        "trace_id": "safe-trace",
        "nested": private_values,
        "url": "https://user:password@example.test/path?token=query-secret",
    }

    redacted = redact_log_event(None, "info", event)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert redacted["event"] == "privacy.test"
    assert redacted["trace_id"] == "safe-trace"
    for value in (
        "private chat text",
        "private image prompt",
        "private street address",
        "remote-token",
        "wxid_private",
        "Private User",
        "query-secret",
        "private exception body",
        "example.test/private/path",
        r"C:\Users\private",
    ):
        assert value not in serialized
    assert "[redacted]" in serialized


def test_machine_info_contains_only_a_random_pseudonymous_id() -> None:
    first = collect_machine_info()
    second = collect_machine_info(first["device_id"])

    assert set(first) == {"device_id"}
    assert valid_pseudonymous_device_id(first["device_id"])
    assert second == first
