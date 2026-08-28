from __future__ import annotations

import json

from plugins.local_agent.sidecar.backends import (
    parse_codex_output,
    parse_grok_output,
    probe_backend,
)


def test_parse_codex_output_uses_last_agent_message() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "first"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "OKAY"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 8}}),
        ]
    )
    assert parse_codex_output(stdout) == "OKAY"


def test_parse_grok_output_strips_text() -> None:
    assert parse_grok_output("  hello \n") == "hello"


def test_probe_unknown_backend() -> None:
    result = probe_backend("claude")
    assert result.ok is False
    assert result.error == "unknown_backend"
