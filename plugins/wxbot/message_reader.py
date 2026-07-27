from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx

from app.common.config import Settings
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request

try:
    import zstandard as zstd

    _DCTX = zstd.ZstdDecompressor()
except ImportError:  # pragma: no cover
    _DCTX = None


_GROUP_PREFIX_RE = re.compile(r"^([a-zA-Z0-9_@]+):\n(.*)$", re.DOTALL)
QueryRows = Callable[..., Awaitable[list[dict[str, Any]]]]


def message_table_name(session_id: str) -> str:
    return "Msg_" + hashlib.md5(session_id.encode()).hexdigest()


def parse_group_body(content: str) -> tuple[str | None, str]:
    matched = _GROUP_PREFIX_RE.match(content or "")
    if matched:
        return matched.group(1), matched.group(2)
    return None, content or ""


def decode_message_hex(raw_hex: str, compression_type: Any) -> str:
    value = str(raw_hex or "").strip()
    if not value:
        return ""
    try:
        raw = bytes.fromhex(value)
    except Exception:
        return ""
    if compression_type == 4 and _DCTX is not None:
        try:
            return _DCTX.decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def format_timestamp(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts or 0)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


class WxbotMessageReader:
    def __init__(self, settings: Settings, *, query_rows: QueryRows | None = None) -> None:
        self._settings = settings
        self._query_rows = query_rows

    async def query_rows(
        self,
        *,
        database: str,
        sql: str,
        params: list[Any] | dict[str, Any] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if self._query_rows is not None:
            return await self._query_rows(
                database=database,
                sql=sql,
                params=params,
                limit=limit,
            )
        base_url = str(getattr(self._settings, "wxbot_sdk_url", "http://127.0.0.1:5080") or "").rstrip("/")
        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                trust_env=False,
            ) as client:
                response = await safe_trusted_service_request(
                    client,
                    "POST",
                    base_url,
                    "/ext/query/read",
                    json={
                        "database": database,
                        "sql": sql,
                        "params": params,
                        "limit": max(1, min(int(limit or 200), 500)),
                    },
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        **wxbot_sdk_headers(self._settings),
                    },
                    timeout_seconds=20.0,
                    max_response_bytes=10 * 1024 * 1024,
                    allowed_response_content_types=(
                        "application/json",
                        "application/problem+json",
                        "text/plain",
                    ),
                )
        except httpx.HTTPError as exc:
            raise ValueError("wxbot sdk unavailable") from exc
        if response.status_code >= 400:
            raise ValueError(f"wxbot sdk returned HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise ValueError("wxbot sdk query returned invalid payload")
        rows = payload.get("rows") or []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    async def load_group_text_messages(
        self,
        session_id: str,
        *,
        member_name_map: dict[str, str] | None = None,
        hours: int = 1,
        limit: int = 200,
        since_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        table = message_table_name(session_id)
        table_exists = await self.query_rows(
            database="message",
            sql="SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = ?",
            params=[table],
            limit=1,
        )
        if not table_exists:
            return []

        cutoff_ts = int(since_ts) if since_ts is not None else max(0, int(time.time()) - max(1, hours) * 3600)
        rows = await self.query_rows(
            database="message",
            sql=(
                f"SELECT server_id, create_time, real_sender_id, local_type, "
                f"hex(message_content) AS message_content_hex, "
                f"WCDB_CT_message_content AS compression_type "
                f"FROM [{table}] "
                f"WHERE local_type = 1 AND create_time >= ? "
                f"ORDER BY create_time DESC"
            ),
            params=[cutoff_ts],
            limit=max(1, min(limit, 500)),
        )

        names = member_name_map or {}
        messages: list[dict[str, Any]] = []
        for row in rows:
            body = decode_message_hex(
                str(row.get("message_content_hex") or ""),
                row.get("compression_type"),
            )
            if not body:
                continue
            sender_wxid, content = parse_group_body(body)
            text = str(content or "").strip()
            if not sender_wxid or not text:
                continue
            sender_name = names.get(sender_wxid) or sender_wxid
            messages.append(
                {
                    "message_id": str(row.get("server_id") or ""),
                    "sender_wxid": sender_wxid,
                    "sender_name": sender_name,
                    "text": text[:1000],
                    "timestamp": format_timestamp(row.get("create_time")),
                    "ts": int(row.get("create_time") or 0),
                }
            )
        return messages
