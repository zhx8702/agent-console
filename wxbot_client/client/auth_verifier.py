from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

from auth_envelope import AuthEnvelopeError, verify_envelope

from client.auth_public_key import AUTH_SIGNING_KEY_ID, AUTH_SIGNING_PUBLIC_KEY_PEM


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_runtime_context(settings: dict) -> dict:
    manifest_path = Path(settings.get("compiled_hash_manifest_path", ""))
    host_path = Path(sys.executable if getattr(sys, "frozen", False) or globals().get("__compiled__") else __file__).resolve()
    context = {
        "host_path_name": host_path.name,
        "host_sha256": "",
        "module_manifest_sha256": "",
    }

    try:
        if host_path.exists():
            context["host_sha256"] = _sha256_file(host_path)
    except Exception:
        context["host_sha256"] = ""

    try:
        if manifest_path and manifest_path.exists():
            context["module_manifest_sha256"] = _sha256_file(manifest_path)
    except Exception:
        context["module_manifest_sha256"] = ""

    return context


class AuthEnvelopeVerifier:
    def __init__(self, machine: dict, settings: dict):
        self.machine = dict(machine or {})
        self.settings = settings
        self.runtime_context = collect_runtime_context(settings)

    def verify(self, response_body: dict, expected_kind: str, client_nonce: str) -> dict:
        claims = verify_envelope(
            response_body.get("signed_envelope"),
            AUTH_SIGNING_PUBLIC_KEY_PEM,
            expected_key_id=AUTH_SIGNING_KEY_ID,
        )

        if claims.get("kind") != expected_kind:
            raise AuthEnvelopeError(
                f"unexpected envelope kind: expected {expected_kind!r}, got {claims.get('kind')!r}"
            )
        if str(claims.get("client_nonce", "") or "") != client_nonce:
            raise AuthEnvelopeError("client_nonce mismatch")
        if str(claims.get("device_id", "") or "") != str(self.machine.get("device_id", "") or ""):
            raise AuthEnvelopeError("device_id mismatch")

        expected_context = self.runtime_context
        actual_context = claims.get("runtime_context") or {}
        if actual_context != expected_context:
            raise AuthEnvelopeError("runtime_context mismatch")

        issued_ts = float(claims.get("issued_ts", 0) or 0)
        if issued_ts and abs(time.time() - issued_ts) > 3600:
            raise AuthEnvelopeError("signed response is too far from current time")

        return claims

    @staticmethod
    def claims_summary(claims: dict) -> str:
        return json.dumps(
            {
                "kind": claims.get("kind"),
                "customer": claims.get("customer"),
                "expires_ts": claims.get("expires_ts"),
                "capabilities": claims.get("capabilities") or [],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
