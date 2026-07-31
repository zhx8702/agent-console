from __future__ import annotations

from pathlib import Path

import yaml

from app.plugin.state import (
    PLUGIN_COMPATIBILITY_ADAPTER_NAMES,
    PLUGIN_CORE_SYSTEM_NAMES,
    PLUGIN_SYSTEM_NAMES,
)

ROOT = Path(__file__).resolve().parents[2]


class _ComposeLoader(yaml.SafeLoader):
    pass


def _construct_override(
    loader: _ComposeLoader,
    node: yaml.Node,
) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _construct_override)


def _compose(name: str) -> dict[str, object]:
    payload = yaml.load(
        (ROOT / name).read_text(encoding="utf-8"),
        Loader=_ComposeLoader,
    )
    assert isinstance(payload, dict)
    return payload


def test_core_compose_profile_has_no_wxbot_worker_or_secret_injection() -> None:
    compose = _compose("docker-compose.yml")
    core_env = compose["x-app-env"]
    assert isinstance(core_env, dict)
    assert {
        str(name) for name in core_env if str(name).startswith("WXBOT_")
    } == {
        "WXBOT_DAILY_REPORT_FOOTER",
        "WXBOT_FILE_DOWNLOAD_MAX_BYTES",
        "WXBOT_OUTBOUND_FILE_CLEANUP_GRACE_SECONDS",
        "WXBOT_OUTBOUND_FILE_DIR",
        "WXBOT_OUTBOUND_FILE_MAX_BYTES",
        "WXBOT_OUTBOUND_FILE_RETENTION_SECONDS",
        "WXBOT_SDK_URL",
    }
    assert core_env["CHANNEL_CONNECTION_ID"] == "${COMPOSE_CHANNEL_CONNECTION_ID:-}"
    assert core_env["READINESS_REQUIRED_WORKER_ROLES"] == (
        "${COMPOSE_READINESS_REQUIRED_WORKER_ROLES:-inbound,outbound,scheduler}"
    )

    services = compose["services"]
    assert isinstance(services, dict)
    bridge = services["wxbot-bridge-worker"]
    assert bridge["profiles"] == ["wxbot"]
    assert bridge["environment"]["WXBOT_API_TOKEN"] == "${COMPOSE_WXBOT_API_TOKEN:-}"
    assert bridge["environment"]["CHANNEL_CONNECTION_ID"] == (
        "${COMPOSE_CHANNEL_CONNECTION_ID:-}"
    )
    for unrelated_secret in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "QDRANT_API_KEY",
        "OUTBOUND_HMAC_SECRET",
        "ADMIN_BEARER_TOKEN",
    ):
        assert unrelated_secret not in bridge["environment"]
    assert "configdata:/data/config" not in bridge.get("volumes", [])

    for name in ("migrate", "api", "inbound-worker", "outbound-worker", "scheduler"):
        environment = services[name]["environment"]
        assert environment["WXBOT_SDK_URL"] == (
            "${COMPOSE_WXBOT_SDK_URL:-http://host.docker.internal:5080}"
        ), name
        assert environment["CHANNEL_CONNECTION_ID"] == (
            "${COMPOSE_CHANNEL_CONNECTION_ID:-}"
        ), name
        assert "WXBOT_API_TOKEN" not in environment, name

    source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert source.count(
        "WORKER_INSTANCE_ID: ${WXBOT_BRIDGE_WORKER_INSTANCE_ID:-}"
    ) == 1


def test_default_developer_commands_do_not_embed_the_wechat_connector() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    up_app = makefile.split("up-app:\n", 1)[1].split("\n\n", 1)[0]
    dev_start = makefile.split("dev-start:\n", 1)[1].split("\n\n", 1)[0]
    assert "wxbot-bridge-worker" not in up_app
    assert "scripts/dev-stack.sh start" in dev_start
    assert "up-wxbot:" in makefile
    assert "dev-start-wxbot:" in makefile

    dev_stack = (ROOT / "scripts" / "dev-stack.sh").read_text(encoding="utf-8")
    assert "CORE_SERVICES=(api frontend inbound outbound scheduler)" in dev_stack
    assert 'printf \'%s\\n\' "${CORE_SERVICES[@]}"' in dev_stack
    assert "printf '%s\\n' inbound outbound scheduler\n" in dev_stack


def test_production_core_contract_does_not_require_wechat() -> None:
    production = _compose("docker-compose.production.yml")
    required = production["x-production-required-env"]
    assert isinstance(required, dict)
    assert "WXBOT_API_TOKEN" not in required
    assert required["READINESS_REQUIRED_WORKER_ROLES"] == "inbound,outbound,scheduler"

    source = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    assert "COMPOSE_WXBOT_API_TOKEN:?" not in source
    assert "inbound,outbound,scheduler,wxbot_bridge" not in source

    bridge_required = production["x-production-wxbot-env"]
    assert set(bridge_required) == {"APP_ENV", "DB_DSN", "REDIS_URL"}
    bridge = production["services"]["wxbot-bridge-worker"]
    assert bridge["environment"] == bridge_required
    for unrelated_secret in (
        "OUTBOUND_HMAC_SECRET",
        "ADMIN_BEARER_TOKEN",
        "ADMIN_SESSION_SIGNING_SECRET",
        "MEDIA_ID_SIGNING_SECRET",
        "TENANT_DEMO_SECRET",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        assert unrelated_secret not in bridge["environment"]


def test_server_deployment_keeps_scheduler_on_sdk_network() -> None:
    server = _compose("docker-compose.server.yml")
    services = server["services"]
    assert isinstance(services, dict)

    for service_name in (
        "api",
        "inbound-worker",
        "outbound-worker",
        "scheduler",
        "wxbot-bridge-worker",
    ):
        assert services[service_name]["networks"] == ["default", "wxbot-sdk"]

    sdk_network = server["networks"]["wxbot-sdk"]
    assert sdk_network["external"] is True
    assert sdk_network["name"] == (
        "${COMPOSE_WXBOT_DOCKER_NETWORK:-wx-bot-linux-xfce_default}"
    )

    deploy_script = (ROOT / "scripts" / "deploy-server.sh").read_text(
        encoding="utf-8"
    )
    assert "-f docker-compose.server.yml" in deploy_script
    assert deploy_script.index("COMPOSE+=(-f docker-compose.server.yml)") < (
        deploy_script.index('SITE_OVERRIDE="${AGENT_CONSOLE_SITE_OVERRIDE_FILE')
    )
    assert "merged scheduler config is missing wxbot-sdk" in deploy_script
    assert "scheduler -> wxbot SDK" in deploy_script


def test_builtin_wxbot_is_only_a_protected_compatibility_adapter() -> None:
    assert PLUGIN_CORE_SYSTEM_NAMES == frozenset({"commands"})
    assert PLUGIN_COMPATIBILITY_ADAPTER_NAMES == frozenset({"wxbot"})
    assert PLUGIN_SYSTEM_NAMES == frozenset({"commands", "wxbot"})


def test_message_platform_documentation_covers_secret_and_network_boundaries() -> None:
    source = (ROOT / "docs" / "message-platform-deployment.md").read_text(
        encoding="utf-8"
    )

    assert "--profile app --profile wxbot" in source
    assert "host.docker.internal:5080" in source
    assert "127.0.0.1" in source
    assert "operators are never asked for a configuration-file path" in source
    assert "platform credential: not required" in source
    assert "adapter ID: `wechat-sdk`" in source
    assert "does **not** mean" in source
    assert "never copied into the connection row" in source
