"""Hash whitelist helpers for compiled sealed modules."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _get_setting(key, default=None):
    try:
        import config
        raw = getattr(config, "_raw", {}) or {}
        settings = config.auth_settings()
        return settings.get(key, raw.get(key, default))
    except Exception:
        return default


def _manifest_path() -> Path:
    return Path(_get_setting("compiled_hash_manifest_path", "./build/protected/module_hashes.json"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest() -> dict:
    path = _manifest_path()
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def verify_binary(module_key: str, path: Path) -> tuple[bool, str]:
    manifest_path = _manifest_path()
    manifest = load_manifest()
    entry = manifest.get(module_key)

    if not entry:
        require_flag = f"require_compiled_{module_key}"
        if _get_setting(require_flag, False):
            return False, f"missing hash manifest entry for {module_key} in {manifest_path}"
        return True, "hash manifest entry not required"

    expected_name = str(entry.get("filename", "") or "").strip()
    expected_sha256 = str(entry.get("sha256", "") or "").strip().lower()
    if expected_name and path.name != expected_name:
        return False, f"filename mismatch for {module_key}: expected {expected_name}, got {path.name}"
    if not expected_sha256:
        return False, f"missing sha256 for {module_key} in {manifest_path}"

    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        return False, f"sha256 mismatch for {module_key}: expected {expected_sha256}, got {actual_sha256}"
    return True, "verified"
