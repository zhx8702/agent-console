from __future__ import annotations

import os
from pathlib import Path

import pytest

from wxbot_client.secure_files import atomic_write_private_text, resolve_relative_file


def test_atomic_private_write_replaces_complete_file(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    atomic_write_private_text(target, '{"session_token":"secret"}\n')

    assert target.read_text(encoding="utf-8") == '{"session_token":"secret"}\n'
    assert list(tmp_path.glob(".state.json.*.tmp")) == []
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0


def test_atomic_private_write_preserves_previous_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        atomic_write_private_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_image_resolution_rejects_traversal_prefix_and_absolute_paths(tmp_path: Path) -> None:
    images = tmp_path / "images"
    sibling = tmp_path / "images-private"
    images.mkdir()
    sibling.mkdir()
    allowed = images / "room" / "preview.png"
    allowed.parent.mkdir()
    allowed.write_bytes(b"image")
    secret = sibling / "secret.png"
    secret.write_bytes(b"secret")

    assert resolve_relative_file(str(images), "room/preview.png") == allowed.resolve()
    assert resolve_relative_file(str(images), "../images-private/secret.png") is None
    assert resolve_relative_file(str(images), str(secret)) is None
    assert resolve_relative_file(str(images), "%2e%2e/images-private/secret.png") is None
