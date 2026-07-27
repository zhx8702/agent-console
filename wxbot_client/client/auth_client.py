import json
from pathlib import Path

from auth_envelope import AuthEnvelopeError, new_client_nonce

from client.auth_verifier import AuthEnvelopeVerifier
from client.machine import collect_machine_info
from client.safe_http import safe_json_post

try:
    from wxbot_client.secure_files import atomic_write_private_text
except ImportError:  # pragma: no cover - direct client launch
    from secure_files import atomic_write_private_text


class AuthClientError(RuntimeError):
    pass


class AuthClient:
    def __init__(self, settings):
        self.settings = settings
        if settings.get("device_binding_consent") is not True:
            raise AuthClientError(
                "device binding consent is required; set device_binding_consent=true"
            )
        self.base_url = settings["auth_base_url"].rstrip("/")
        if not self.base_url:
            raise AuthClientError("auth_base_url missing")
        self.timeout = settings.get("heartbeat_timeout_sec", 8)
        self.cache_path = Path(settings.get("cache_path", "./data/client_state.json"))
        self.state = self._load_state()
        existing_device_id = (
            self.state.get("device_id")
            or (self.state.get("activate_claims") or {}).get("device_id")
            or (self.state.get("session_claims") or {}).get("device_id")
        )
        self.machine = collect_machine_info(existing_device_id)
        self.state["device_id"] = self.machine["device_id"]
        self.verifier = AuthEnvelopeVerifier(self.machine, settings)
        self._save_state()

    def _load_state(self):
        if not self.cache_path.exists():
            return {}
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        atomic_write_private_text(
            self.cache_path,
            json.dumps(self.state, ensure_ascii=False, indent=2) + "\n",
        )

    def _post(self, path, payload):
        try:
            response = safe_json_post(
                self.base_url,
                path,
                payload,
                timeout_seconds=self.timeout,
                max_response_bytes=1024 * 1024,
            )
        except Exception:
            raise AuthClientError("auth service request failed") from None
        body = response.payload
        if response.status_code >= 400 or not body.get("ok"):
            raise AuthClientError(f"auth service rejected request (HTTP {response.status_code})")
        return body

    def _runtime_context(self):
        return dict(self.verifier.runtime_context)

    def _verify_claims(self, body, expected_kind, client_nonce):
        try:
            return self.verifier.verify(body, expected_kind, client_nonce)
        except AuthEnvelopeError as e:
            raise AuthClientError(str(e)) from e

    def ensure_device_bound(self):
        token = (self.state.get("device_token") or "").strip()
        if token:
            return token
        activation_code = self.settings["activation_code"]
        if not activation_code:
            raise AuthClientError("activation_code missing")
        client_nonce = new_client_nonce()
        payload = {
            "activation_code": activation_code,
            "device_id": self.machine["device_id"],
            "device_name": self._device_name(),
            "client_nonce": client_nonce,
            "runtime_context": self._runtime_context(),
        }
        body = self._post("/api/v1/activate", payload)
        claims = self._verify_claims(body, "activate", client_nonce)
        self.state["device_token"] = claims["device_token"]
        self.state["customer"] = claims.get("customer")
        self.state["features"] = claims.get("features") or []
        self.state["capabilities"] = claims.get("capabilities") or []
        self.state["activate_claims"] = claims
        self._save_state()
        return self.state["device_token"]

    def _device_name(self):
        configured = str(self.settings.get("device_name") or "").strip()
        if not configured or configured == "auto":
            return "wxbot-device"
        return configured[:64]

    def login(self):
        device_token = self.ensure_device_bound()
        client_nonce = new_client_nonce()
        payload = {
            "device_token": device_token,
            "device_id": self.machine["device_id"],
            "device_name": self._device_name(),
            "client_nonce": client_nonce,
            "runtime_context": self._runtime_context(),
        }
        body = self._post("/api/v1/session/login", payload)
        claims = self._verify_claims(body, "session_login", client_nonce)
        self.state["session_token"] = claims["session_token"]
        self.state["session_expires_ts"] = claims["expires_ts"]
        self.state["customer"] = claims.get("customer")
        self.state["features"] = claims.get("features") or []
        self.state["capabilities"] = claims.get("capabilities") or []
        self.state["session_claims"] = claims
        self._save_state()
        return claims

    def heartbeat(self):
        session_token = (self.state.get("session_token") or "").strip()
        if not session_token:
            return self.login()
        client_nonce = new_client_nonce()
        payload = {
            "session_token": session_token,
            "device_id": self.machine["device_id"],
            "client_nonce": client_nonce,
            "runtime_context": self._runtime_context(),
        }
        body = self._post("/api/v1/session/heartbeat", payload)
        claims = self._verify_claims(body, "session_heartbeat", client_nonce)
        self.state["session_token"] = claims["session_token"]
        self.state["session_expires_ts"] = claims["expires_ts"]
        self.state["customer"] = claims.get("customer")
        self.state["features"] = claims.get("features") or []
        self.state["capabilities"] = claims.get("capabilities") or []
        self.state["session_claims"] = claims
        self._save_state()
        return claims
