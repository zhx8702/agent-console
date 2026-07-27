from __future__ import annotations

import pytest

from app.channel.secrets import (
    ChannelSecretReferenceError,
    resolve_channel_secret_ref,
)


def test_channel_secret_ref_resolves_only_the_worker_environment() -> None:
    environment = {"WXBOT_API_TOKEN": "connector-secret"}

    assert (
        resolve_channel_secret_ref(
            "env://WXBOT_API_TOKEN",
            environ=environment,
        )
        == "connector-secret"
    )
    assert (
        resolve_channel_secret_ref(
            "env:WXBOT_API_TOKEN",
            environ=environment,
        )
        == "connector-secret"
    )


def test_channel_secret_ref_fails_closed_for_unavailable_provider_or_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WXBOT_API_TOKEN", "ambient-secret-must-not-leak")
    with pytest.raises(ChannelSecretReferenceError, match="provider is not available"):
        resolve_channel_secret_ref("vault://team/wxbot", environ={})
    with pytest.raises(ChannelSecretReferenceError, match="is not configured"):
        resolve_channel_secret_ref("env://WXBOT_API_TOKEN", environ={})


def test_channel_secret_ref_never_treats_inline_text_as_a_secret() -> None:
    with pytest.raises(ChannelSecretReferenceError, match="provider is not available"):
        resolve_channel_secret_ref("plaintext:connector-secret", environ={})


def test_channel_secret_ref_honors_the_adapter_environment_allowlist() -> None:
    environment = {
        "WXBOT_API_TOKEN": "connector-secret",
        "OPENAI_API_KEY": "unrelated-high-value-secret",
    }

    assert (
        resolve_channel_secret_ref(
            "env://WXBOT_API_TOKEN",
            environ=environment,
            allowed_environment_variables={"WXBOT_API_TOKEN"},
        )
        == "connector-secret"
    )
    with pytest.raises(ChannelSecretReferenceError, match="not allowed"):
        resolve_channel_secret_ref(
            "env://OPENAI_API_KEY",
            environ=environment,
            allowed_environment_variables={"WXBOT_API_TOKEN"},
        )
