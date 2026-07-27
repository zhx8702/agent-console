from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def wxbot_main(monkeypatch: pytest.MonkeyPatch):
    queue_store = types.ModuleType("queue_store")
    runtime_guard = types.ModuleType("client.runtime_guard")
    runtime_guard.AuthorizationError = type("AuthorizationError", (RuntimeError,), {})
    runtime_guard.RuntimeAuthGuard = object
    runtime = types.ModuleType("sealed_core.runtime")
    sealed_core = types.ModuleType("sealed_core")
    sealed_core.runtime = runtime
    config = types.ModuleType("config")
    monkeypatch.setitem(sys.modules, "queue_store", queue_store)
    monkeypatch.setitem(sys.modules, "client.runtime_guard", runtime_guard)
    monkeypatch.setitem(sys.modules, "sealed_core", sealed_core)
    monkeypatch.setitem(sys.modules, "sealed_core.runtime", runtime)
    monkeypatch.setitem(sys.modules, "config", config)

    path = Path(__file__).parents[2] / "wxbot_client" / "main.py"
    spec = importlib.util.spec_from_file_location("_wxbot_main_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("", "configured=no, validated_host=none"),
        ("https://AUTH.example/", "configured=yes, validated_host=auth.example"),
        ("http://auth.example", "configured=yes, validated_host=invalid"),
        ("https://user:secret@auth.example", "configured=yes, validated_host=invalid"),
        ("https://auth.example/path?token=secret", "configured=yes, validated_host=invalid"),
        ("https://auth.example:not-a-port", "configured=yes, validated_host=invalid"),
    ],
)
def test_auth_server_status_never_echoes_raw_credentials(
    wxbot_main: types.ModuleType,
    raw_url: str,
    expected: str,
) -> None:
    result = wxbot_main._auth_server_status(raw_url)

    assert result == expected
    assert "user" not in result
    assert "secret" not in result


def test_startup_banner_never_prints_the_raw_auth_url(
    wxbot_main: types.ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wxbot_main.config.AUTH_BASE_URL = "https://operator:banner-secret@auth.example"
    wxbot_main.config.API_HOST = "127.0.0.1"
    wxbot_main.config.API_PORT = 5080
    wxbot_main.config.SELF_WXID = "wxid_bot"
    wxbot_main.config.MY_NAMES = ["PrivateDisplayName"]
    wxbot_main.config.QUEUE_DB_PATH = "queue.db"

    def stop_after_banner(_path: str) -> None:
        raise RuntimeError("stop after banner")

    wxbot_main.qs.init = stop_after_banner

    with pytest.raises(RuntimeError, match="stop after banner"):
        wxbot_main.main()

    banner = capsys.readouterr().out
    assert "auth server:  configured=yes, validated_host=invalid" in banner
    assert "operator" not in banner
    assert "banner-secret" not in banner
    assert "wxid_bot" not in banner
    assert "PrivateDisplayName" not in banner


def test_identity_preflight_accepts_only_verified_wxid_and_rowid(
    wxbot_main: types.ModuleType,
) -> None:
    loader = types.ModuleType("sealed_core.ingest_loader")
    loader.resolve_self_identity = lambda: {
        "ready": True,
        "self_wxid": "wxid_bot",
        "self_rowid": 11,
        "reason": "",
    }
    sys.modules["sealed_core"].ingest_loader = loader

    assert wxbot_main._identity_preflight() == {
        "ready": True,
        "self_wxid": "wxid_bot",
        "self_rowid": 11,
        "reason": "",
    }


def test_identity_preflight_fails_closed_for_missing_rowid(
    wxbot_main: types.ModuleType,
) -> None:
    loader = types.ModuleType("sealed_core.ingest_loader")
    loader.resolve_self_identity = lambda: {
        "ready": False,
        "self_wxid": "",
        "self_rowid": None,
        "reason": "self_rowid_missing",
    }
    sys.modules["sealed_core"].ingest_loader = loader

    result = wxbot_main._identity_preflight()

    assert result["ready"] is False
    assert result["self_rowid"] is None
    assert result["reason"] == "self_rowid_missing"
