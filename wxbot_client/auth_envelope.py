from __future__ import annotations

import base64
import json
import secrets
from functools import lru_cache

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa


class AuthEnvelopeError(RuntimeError):
    pass


def canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def new_client_nonce() -> str:
    return secrets.token_urlsafe(18)


@lru_cache(maxsize=8)
def _import_private_key(private_pem: str):
    return ECC.import_key(private_pem)


@lru_cache(maxsize=8)
def _import_public_key(public_pem: str):
    return ECC.import_key(public_pem)


def sign_payload(payload: dict, private_pem: str, key_id: str) -> dict:
    try:
        payload_bytes = canonical_json_bytes(payload)
        signer = eddsa.new(_import_private_key(private_pem), "rfc8032")
        signature = signer.sign(payload_bytes)
    except Exception as e:
        raise AuthEnvelopeError(f"failed to sign payload: {e}") from e

    return {
        "alg": "Ed25519",
        "key_id": key_id,
        "payload_b64": base64.b64encode(payload_bytes).decode("ascii"),
        "sig_b64": base64.b64encode(signature).decode("ascii"),
    }


def verify_envelope(envelope: dict, public_pem: str, expected_key_id: str | None = None) -> dict:
    if not isinstance(envelope, dict):
        raise AuthEnvelopeError("signed_envelope missing or invalid")
    if envelope.get("alg") != "Ed25519":
        raise AuthEnvelopeError(f"unsupported signing alg: {envelope.get('alg')!r}")

    key_id = str(envelope.get("key_id", "") or "").strip()
    if expected_key_id and key_id != expected_key_id:
        raise AuthEnvelopeError(f"unexpected signing key_id: {key_id!r}")

    try:
        payload_bytes = base64.b64decode(envelope["payload_b64"])
        signature = base64.b64decode(envelope["sig_b64"])
    except Exception as e:
        raise AuthEnvelopeError(f"invalid envelope encoding: {e}") from e

    try:
        verifier = eddsa.new(_import_public_key(public_pem), "rfc8032")
        verifier.verify(payload_bytes, signature)
    except Exception as e:
        raise AuthEnvelopeError(f"signature verification failed: {e}") from e

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as e:
        raise AuthEnvelopeError(f"invalid envelope payload json: {e}") from e

    if not isinstance(payload, dict):
        raise AuthEnvelopeError("signed payload is not an object")
    return payload
