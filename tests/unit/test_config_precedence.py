from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.common.config import Settings


def test_environment_overrides_dotenv_for_secrets(
    monkeypatch,
    tmp_path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ADMIN_BEARER_TOKEN=dotenv-token\n"
        "OUTBOUND_HMAC_SECRET=dotenv-outbound\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ADMIN_BEARER_TOKEN", "environment-token")
    monkeypatch.setenv("OUTBOUND_HMAC_SECRET", "environment-outbound")

    settings = Settings(_env_file=env_file)

    assert settings.admin_bearer_token == "environment-token"
    assert settings.outbound_hmac_secret == "environment-outbound"


def test_production_effect_handlers_require_durable_fail_closed_log() -> None:
    with pytest.raises(ValidationError, match="durable PostgreSQL effect log"):
        Settings(
            app_env="prod",
            orchestrator_flow_effect_handlers_enabled=True,
            orchestrator_flow_effect_commit_backend="redis",
            orchestrator_flow_effect_log_backend="none",
        )

    settings = Settings(
        app_env="prod",
        orchestrator_flow_effect_handlers_enabled=True,
        orchestrator_flow_effect_commit_backend="redis",
        orchestrator_flow_effect_log_backend="postgres",
        orchestrator_flow_effect_log_failure_policy="fail_closed",
    )
    assert settings.orchestrator_flow_effect_handlers_enabled is True


@pytest.mark.parametrize("app_env", ["prod", "production", "staging", "qa"])
def test_non_local_environments_disable_dynamic_plugin_mutations(
    app_env: str,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env=app_env,
        plugin_dynamic_mutations_enabled=True,
    )

    assert settings.is_prod is True
    assert settings.allow_dynamic_plugin_mutations is False


@pytest.mark.parametrize("app_env", ["dev", "test"])
def test_local_environments_can_explicitly_allow_dynamic_plugin_mutations(
    app_env: str,
) -> None:
    settings = Settings(
        _env_file=None,
        app_env=app_env,
        plugin_dynamic_mutations_enabled=True,
    )

    assert settings.is_prod is False
    assert settings.allow_dynamic_plugin_mutations is True


def test_explicit_settings_kwargs_forbid_unknown_names() -> None:
    with pytest.raises(ValidationError, match="app_log_levle"):
        Settings(_env_file=None, app_env="dev", app_log_levle="DEBUG")


def test_development_dotenv_warns_and_ignores_unknown_names(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=dev\n"
        "APP_LOG_LEVLE=DEBUG\n"
        "COMPOSE_OPENAI_API_KEY=deployment-only\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="APP_LOG_LEVLE.*APP_LOG_LEVEL"):
        settings = Settings(_env_file=env_file)

    assert settings.app_log_level == "INFO"


def test_grok_gateway_aliases_map_to_openai_compatible_settings(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "XAI_API_KEY=xai-test-key\n"
        "GROK_MODELS_BASE_URL=https://sub2api.example/v1\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.openai_api_key == "xai-test-key"
    assert settings.openai_base_url == "https://sub2api.example/v1"


def test_production_dotenv_rejects_unknown_names(monkeypatch, tmp_path) -> None:
    # tests/conftest.py sets APP_ENV=test; remove it so this case exercises the
    # dotenv-selected environment rather than the higher-priority process env.
    monkeypatch.delenv("APP_ENV")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_ENV=prod\nREDUS_URL=redis://wrong-host:6379/0\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"REDUS_URL.*REDIS_URL"):
        Settings(_env_file=env_file)


def test_production_process_env_rejects_likely_setting_typo(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("APP_LOG_LEVLE", "DEBUG")

    with pytest.raises(ValueError, match="process environment: APP_LOG_LEVLE"):
        Settings(_env_file=None)


def test_production_strict_config_cannot_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("AGENT_CONSOLE_STRICT_CONFIG", "false")
    monkeypatch.setenv("REDUS_URL", "redis://wrong-host:6379/0")

    with pytest.raises(ValueError, match="REDUS_URL"):
        Settings(_env_file=None)


def test_production_ignores_unrelated_process_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PATH", "test-path")
    monkeypatch.setenv("APPDATA", "C:/Users/example/AppData")
    monkeypatch.setenv("CI_JOB_ID", "123")
    monkeypatch.setenv("AGENT_TOOLSDIRECTORY", "/opt/hostedtoolcache")
    monkeypatch.setenv("MEMORY_PRESSURE_WATCH", "1")
    monkeypatch.setenv("MEMORY_PRESSURE_WRITE", "/sys/fs/cgroup/memory.events")

    settings = Settings(_env_file=None, app_env="prod")

    assert settings.app_env == "prod"


def test_production_rejects_unknown_name_in_owned_setting_namespace(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("APP_LOG_FORMATTER", "json")

    with pytest.raises(ValueError, match="APP_LOG_FORMATTER"):
        Settings(_env_file=None)


def test_quota_environment_is_part_of_the_settings_contract(monkeypatch) -> None:
    monkeypatch.setenv("TENANT_DEFAULT_DAILY_TOKENS", "4321")

    settings = Settings(_env_file=None)

    assert settings.tenant_default_daily_tokens == 4321


def test_capability_dispatch_timeout_environment_is_part_of_settings_contract(monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_CAPABILITY_DISPATCH_TIMEOUT_SECONDS", "180")

    settings = Settings(_env_file=None)

    assert settings.orchestrator_capability_dispatch_timeout_seconds == 180.0


def test_admin_and_organization_specific_features_default_fail_closed(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ADMIN_BEARER_TOKEN")
    settings = Settings(_env_file=None, app_env="dev")

    assert settings.admin_bearer_token == ""
    assert settings.tibo_reset_enabled is False
    assert settings.tibo_reset_api_url == ""
    assert settings.wxbot_daily_report_footer == ""


def test_tibo_reset_requires_explicit_endpoint_when_enabled() -> None:
    with pytest.raises(
        ValidationError,
        match="TIBO_RESET_API_URL is required",
    ):
        Settings(
            _env_file=None,
            app_env="dev",
            tibo_reset_enabled=True,
            tibo_reset_api_url="",
        )

    settings = Settings(
        _env_file=None,
        app_env="dev",
        tibo_reset_enabled=True,
        tibo_reset_api_url="https://reset-feed.example.invalid/api/resets",
    )
    assert settings.tibo_reset_enabled is True
