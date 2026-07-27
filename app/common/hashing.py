from __future__ import annotations

import hashlib
import hmac


def hmac_sha256(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_hmac(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac_sha256(secret, body)
    return hmac.compare_digest(expected, signature)


def stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
