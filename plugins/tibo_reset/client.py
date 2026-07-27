from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.common.safe_url import OutboundURLPolicy
from app.egress.safe_http import safe_http_request


class TiboResetClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class TiboResetEntry:
    tweet_id: str
    text: str
    created_at: str
    source_url: str = ""
    confidence: float | None = None
    evidence: str = ""
    stated_reason: str = ""
    reset_type: str = ""
    beneficiaries: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_PRODUCT_RE = re.compile(r"\b(?:Codex|ChatGPT Work)\b", re.IGNORECASE)
_SOURCE_RE = re.compile(r"^https://x\.com/thsottiaux/status/(\d+)$", re.IGNORECASE)


def _normalize_evidence(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def notification_validation(entry: TiboResetEntry) -> tuple[bool, str]:
    source_match = _SOURCE_RE.fullmatch(entry.source_url)
    if source_match is None or source_match.group(1) != entry.tweet_id:
        return False, "invalid_source_url"
    if entry.confidence is None or entry.confidence < 0.95:
        return False, "low_confidence"
    if _PRODUCT_RE.search(entry.text) is None:
        return False, "product_not_mentioned"
    evidence = _normalize_evidence(entry.evidence)
    if not evidence or evidence not in _normalize_evidence(entry.text):
        return False, "evidence_not_in_tweet"
    return True, "verified"


def _required_text(item: dict[str, Any], key: str) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise TiboResetClientError(f"reset entry missing {key}")
    return value


def _validate_created_at(value: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TiboResetClientError("reset entry has invalid createdAt") from exc
    return value


def parse_reset_payload(payload: Any) -> list[TiboResetEntry]:
    if not isinstance(payload, dict) or not isinstance(payload.get("resets"), list):
        raise TiboResetClientError("reset response must contain a resets array")

    entries: list[TiboResetEntry] = []
    seen_ids: set[str] = set()
    for raw in payload["resets"]:
        if not isinstance(raw, dict):
            raise TiboResetClientError("reset entry must be an object")
        tweet_id = _required_text(raw, "id")
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)
        confidence_raw = raw.get("confidence")
        confidence = float(confidence_raw) if confidence_raw is not None else None
        entries.append(
            TiboResetEntry(
                tweet_id=tweet_id,
                text=_required_text(raw, "text"),
                created_at=_validate_created_at(_required_text(raw, "createdAt")),
                source_url=str(raw.get("sourceUrl") or "").strip(),
                confidence=confidence,
                evidence=str(raw.get("evidence") or "").strip(),
                stated_reason=str(raw.get("statedReason") or "").strip(),
                reset_type=str(raw.get("resetType") or "").strip(),
                beneficiaries=str(raw.get("beneficiaries") or "").strip(),
            )
        )
    return entries


class TiboResetClient:
    def __init__(
        self,
        api_url: str,
        *,
        timeout_seconds: float = 15.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_url = str(api_url or "").strip()
        if not self.api_url:
            raise ValueError("tibo reset api url is required")
        parsed_url = urlsplit(self.api_url)
        hostname = str(parsed_url.hostname or "").strip().lower()
        if not hostname:
            raise ValueError("tibo reset api url must include a hostname")
        timeout = max(1.0, float(timeout_seconds or 15.0))
        self._policy = OutboundURLPolicy(
            require_https=True,
            allowed_hosts=frozenset({hostname}),
            max_redirects=2,
            max_response_bytes=1024 * 1024,
            timeout_seconds=timeout,
            allowed_response_content_types=("application/json",),
        )
        self._owns_client = http_client is None
        self._etag = ""
        self._cached_entries: list[TiboResetEntry] = []
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def fetch_resets(self) -> list[TiboResetEntry]:
        try:
            headers = {
                "Accept": "application/json",
                "User-Agent": "agent-console-tibo-reset/0.1.0",
            }
            if self._etag and self._cached_entries:
                headers["If-None-Match"] = self._etag
            response = await safe_http_request(
                self._client,
                "GET",
                self.api_url,
                headers=headers,
                policy=self._policy,
            )
            if response.status_code == 304:
                return list(self._cached_entries)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TiboResetClientError("failed to fetch tibo reset feed") from exc
        entries = parse_reset_payload(payload)
        self._etag = str(response.headers.get("etag") or "").strip()
        self._cached_entries = list(entries)
        return entries

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
