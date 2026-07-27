"""Load config.json and expose values. Auto-detect WeChat paths when 'auto'."""

import glob
import json
import os
import re
import threading
from pathlib import Path

try:
    from wxbot_client.secure_files import atomic_write_private_text
except ImportError:  # pragma: no cover - direct client launch
    from secure_files import atomic_write_private_text

ROOT = Path(__file__).parent.resolve()
CONFIG_PATH = Path(os.environ.get("WXBOT_CONFIG", str(ROOT / "config.json"))).resolve()
_SAVE_LOCK = threading.RLock()


def _load_raw():
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"{CONFIG_PATH} not found. Copy config.example.json and fill in values."
        )
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in {"1", "true", "yes", "on"}:
            return True
        if cleaned in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _env_or_raw(raw_key: str, env_key: str, default=None):
    env_value = os.environ.get(env_key)
    if env_value is not None and env_value != "":
        return env_value
    return _raw.get(raw_key, default)


def _save_raw():
    with _SAVE_LOCK:
        atomic_write_private_text(
            CONFIG_PATH,
            json.dumps(_raw, ensure_ascii=False, indent=2) + "\n",
        )


def _autodetect_data_dir():
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat") as k:
            path, _ = winreg.QueryValueEx(k, "FileSavePath")
        if path and path not in ("MyDocument:", ""):
            base = Path(path) / "xwechat_files"
        else:
            base = Path(os.environ["USERPROFILE"]) / "Documents" / "xwechat_files"
    except Exception:
        base = Path(os.environ.get("USERPROFILE", "")) / "Documents" / "xwechat_files"

    if base.exists():
        accounts = [d for d in base.iterdir() if d.is_dir() and (d / "db_storage").exists()]
        if accounts:
            accounts.sort(
                key=lambda d: sum(f.stat().st_size for f in d.rglob("*") if f.is_file()),
                reverse=True,
            )
            return str(accounts[0])

    for pat in [
        r"C:\Users\*\Documents\xwechat_files\*",
        r"D:\*\xwechat_files\*",
        r"E:\*\xwechat_files\*",
    ]:
        for hit in glob.glob(pat):
            if os.path.exists(os.path.join(hit, "db_storage")):
                return hit
    return None


def _autodetect_self_wxid(data_dir):
    if not data_dir:
        return ""
    name = Path(data_dir).name
    m = re.match(r"^(.+)_[A-Za-z0-9]+$", name)
    return m.group(1) if m else name


_raw = _load_raw()

# ── Remote auth server (runtime activation) ──
AUTH_BASE_URL = _env_or_raw("auth_base_url", "WXBOT_AUTH_BASE_URL", "")
ACTIVATION_CODE = _env_or_raw("activation_code", "WXBOT_ACTIVATION_CODE", "")
DEVICE_BINDING_CONSENT = _as_bool(
    _env_or_raw("device_binding_consent", "WXBOT_DEVICE_BINDING_CONSENT", False),
    False,
)
HEARTBEAT_INTERVAL_SEC = int(
    _env_or_raw("heartbeat_interval_sec", "WXBOT_HEARTBEAT_INTERVAL_SEC", 60)
)
HEARTBEAT_TIMEOUT_SEC = int(_env_or_raw("heartbeat_timeout_sec", "WXBOT_HEARTBEAT_TIMEOUT_SEC", 8))
AUTH_GRACE_SEC = int(_env_or_raw("auth_grace_sec", "WXBOT_AUTH_GRACE_SEC", 180))

# ── cs-system API (optional, for forwarding) ──
CS_API_BASE_URL = _env_or_raw("cs_api_base_url", "CS_API_BASE_URL", "http://127.0.0.1:8000")
CS_API_TOKEN = _env_or_raw("cs_api_token", "CS_API_TOKEN", "")
CS_TENANT_ID = _env_or_raw("cs_tenant_id", "CS_TENANT_ID", "")

# ── SDK API server ──
API_HOST = _env_or_raw("api_host", "WXBOT_API_HOST", "127.0.0.1")
API_PORT = int(_env_or_raw("api_port", "WXBOT_API_PORT", 5080))
API_TOKEN = _env_or_raw("api_token", "WXBOT_API_TOKEN", "")

# ── WeChat data ──
_data_dir = _raw.get("wechat_data_dir", "auto")
if _data_dir == "auto":
    _data_dir = _autodetect_data_dir()
WECHAT_DATA_DIR = _data_dir

_self = _raw.get("self_wxid", "auto")
if _self == "auto":
    _self = _autodetect_self_wxid(WECHAT_DATA_DIR)
SELF_WXID = _self

_decrypted = _raw.get("decrypted_dir", "./data/decrypted")
DECRYPTED_DIR = str((ROOT / _decrypted).resolve()) if _decrypted.startswith(".") else _decrypted

MY_NAMES = list(_raw.get("my_names", []))
# Capture and reply are separate decisions. New installations capture every
# group message so the backend can build context; the backend reply policy still
# decides whether the bot should speak.
GROUP_REQUIRE_AT_ME = _as_bool(_raw.get("group_require_at_me"), False)
INGEST_INTERVAL = int(_raw.get("ingest_interval_sec", 10))
SEND_INTERVAL = int(_raw.get("send_interval_sec", 5))
SESSION_NAME_OVERRIDES = dict(_raw.get("session_name_overrides") or {})
IMAGE_DECRYPT_THUMBNAIL_FALLBACK = _as_bool(
    _raw.get("image_decrypt_thumbnail_fallback"),
    True,
)

# ── Local queue DB ──
QUEUE_DB_PATH = str(ROOT / "data" / "queue.db")


def auth_settings() -> dict:
    return {
        "auth_base_url": AUTH_BASE_URL,
        "activation_code": ACTIVATION_CODE,
        "device_binding_consent": DEVICE_BINDING_CONSENT,
        "device_name": _raw.get("device_name", "wxbot-device"),
        "cache_path": str(ROOT / "data" / "client_state.json"),
        "heartbeat_interval_sec": HEARTBEAT_INTERVAL_SEC,
        "heartbeat_timeout_sec": HEARTBEAT_TIMEOUT_SEC,
        "auth_grace_sec": AUTH_GRACE_SEC,
        "compiled_hash_manifest_path": str(
            ROOT / _raw.get("compiled_hash_manifest_path", "build/protected/module_hashes.json")
        ),
        "require_compiled_sender": _raw.get("require_compiled_sender", False),
        "require_compiled_ingest": _raw.get("require_compiled_ingest", False),
    }


def summary():
    return {
        "auth_server": AUTH_BASE_URL,
        "api_endpoint": f"http://{API_HOST}:{API_PORT}",
        "wechat_data_dir": WECHAT_DATA_DIR,
        "decrypted_dir": DECRYPTED_DIR,
        "self_wxid": SELF_WXID,
        "my_names": MY_NAMES,
        "group_require_at_me": GROUP_REQUIRE_AT_ME,
        "image_decrypt_thumbnail_fallback": IMAGE_DECRYPT_THUMBNAIL_FALLBACK,
    }


def trigger_debug_summary() -> dict:
    return {
        "group_require_at_me": GROUP_REQUIRE_AT_ME,
        "group_capture_mode": "mention_or_command" if GROUP_REQUIRE_AT_ME else "all_group_messages",
        "my_names": MY_NAMES,
        "config_path": str(CONFIG_PATH),
    }


def set_group_require_at_me(enabled: bool) -> dict:
    global GROUP_REQUIRE_AT_ME
    GROUP_REQUIRE_AT_ME = bool(enabled)
    _raw["group_require_at_me"] = GROUP_REQUIRE_AT_ME
    _save_raw()
    return trigger_debug_summary()
