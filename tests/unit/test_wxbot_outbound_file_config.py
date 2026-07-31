from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.common.config import Settings

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTAINER_DIR = "/data/wxbot-outbound"
DEFAULT_HOST_DIR = "/opt/wxbot-shared/outbox"


class _ComposeLoader(yaml.SafeLoader):
    pass


def _construct_override(
    loader: _ComposeLoader,
    node: yaml.nodes.Node,
) -> Any:
    if isinstance(node, yaml.nodes.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.nodes.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _construct_override)


def _load_compose(path: Path, *, allow_override: bool = False) -> dict[str, Any]:
    loader = _ComposeLoader if allow_override else yaml.SafeLoader
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    assert isinstance(payload, dict)
    return payload


def test_wxbot_outbound_file_settings_have_bounded_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.wxbot_outbound_file_dir == DEFAULT_CONTAINER_DIR
    assert settings.wxbot_outbound_file_max_bytes == 10 * 1024 * 1024
    assert settings.wxbot_outbound_file_retention_seconds == 24 * 60 * 60
    assert settings.wxbot_outbound_file_cleanup_grace_seconds == 5 * 60

    with pytest.raises(ValidationError, match="wxbot_outbound_file_max_bytes"):
        Settings(_env_file=None, wxbot_outbound_file_max_bytes=1024)


def test_base_compose_uses_a_portable_named_volume_for_export_writers() -> None:
    compose = _load_compose(ROOT / "docker-compose.yml")
    services = compose["services"]
    expected_mount = "wxbotoutbound:${WXBOT_OUTBOUND_FILE_DIR:-/data/wxbot-outbound}"

    assert "wxbotoutbound" in compose["volumes"]
    for service_name in ("api", "inbound-worker", "scheduler"):
        service = services[service_name]
        assert expected_mount in service["volumes"]
        assert (
            service["environment"]["WXBOT_OUTBOUND_FILE_DIR"]
            == "${WXBOT_OUTBOUND_FILE_DIR:-/data/wxbot-outbound}"
        )

    for forwarding_service in ("outbound-worker", "wxbot-bridge-worker"):
        mounts = services[forwarding_service].get("volumes", [])
        assert all("wxbotoutbound" not in str(mount) for mount in mounts)


def test_server_overlay_replaces_writer_volume_with_shared_host_bind() -> None:
    overlay = _load_compose(
        ROOT / "docker-compose.server.yml",
        allow_override=True,
    )
    services = overlay["services"]

    for service_name in ("api", "inbound-worker", "scheduler"):
        mounts = services[service_name]["volumes"]
        assert mounts == [
            {
                "type": "bind",
                "source": (f"${{WXBOT_OUTBOUND_FILE_HOST_DIR:-{DEFAULT_HOST_DIR}}}"),
                "target": (f"${{WXBOT_OUTBOUND_FILE_DIR:-{DEFAULT_CONTAINER_DIR}}}"),
                "bind": {"create_host_path": True},
            }
        ]

    for forwarding_service in ("outbound-worker", "wxbot-bridge-worker"):
        assert "volumes" not in services[forwarding_service]


def test_env_example_documents_sdk_shared_outbox_contract() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert f"WXBOT_OUTBOUND_FILE_DIR={DEFAULT_CONTAINER_DIR}" in example
    assert f"WXBOT_OUTBOUND_FILE_HOST_DIR={DEFAULT_HOST_DIR}" in example
    assert "COMPOSE_WXBOT_OUTBOUND_FILE_MAX_BYTES=10485760" in example
    assert "file sending" in example
