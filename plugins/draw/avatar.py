from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request

_AVATAR_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:群里(?:的)?|本群|这个群)\s*@?(?P<name>[\w\u4e00-\u9fff.-]{1,32})的?头像"),
    re.compile(r"@(?P<name>[^\s\u2005\u00a0]{1,32})\s*的?头像"),
    re.compile(r"(?:基于|参考|用|使用|拿|以)\s*@?(?P<name>[\w\u4e00-\u9fff.-]{1,32})的?头像"),
    re.compile(r"(?P<name>[\w\u4e00-\u9fff.-]{1,32})的头像"),
)

_AVATAR_QUERY_PREFIX_NOISE = (
    "群里",
    "本群",
    "这个群",
    "基于",
    "参考",
    "使用",
    "用",
    "拿",
    "以",
)


@dataclass(frozen=True)
class DrawAvatarReference:
    query: str
    display_name: str
    wxid: str
    avatar_url: str
    image_path: str
    source_label: str


def extract_avatar_query(prompt: str) -> str:
    text = str(prompt or "").strip()
    if "头像" not in text:
        return ""
    for pattern in _AVATAR_QUERY_PATTERNS:
        matched = pattern.search(text)
        if not matched:
            continue
        query = str(matched.group("name") or "").strip().strip("@")
        for prefix in _AVATAR_QUERY_PREFIX_NOISE:
            if query.startswith(prefix):
                query = query[len(prefix):].strip()
        query = query.rstrip("的").strip()
        if query and query not in {"一张", "一个", "头像", "微信", "聊天记录"}:
            return query
    return ""


async def resolve_prompt_avatar_reference(
    store: Any,
    *,
    session_id: str,
    prompt: str,
    trace_id: str,
) -> DrawAvatarReference | None:
    query = extract_avatar_query(prompt)
    if not query:
        return None

    return await resolve_group_avatar_reference(
        store.settings,
        session_id=session_id,
        query=query,
        trace_id=trace_id,
        cache_reference_image=store.cache_reference_image,
    )


async def resolve_group_avatar_reference(
    settings: Any,
    *,
    session_id: str,
    query: str = "",
    wxid: str = "",
    trace_id: str = "",
    cache_reference_image: Callable[..., Awaitable[Any]] | None = None,
) -> DrawAvatarReference | None:
    query = str(query or "").strip()
    wxid = str(wxid or "").strip()
    if not (query or wxid) or not str(session_id or "").endswith("@chatroom"):
        return None

    base_url = str(getattr(settings, "wxbot_sdk_url", "http://127.0.0.1:5080") or "").rstrip("/")
    if not base_url:
        return None

    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await safe_trusted_service_request(
                client,
                "GET",
                base_url,
                f"/ext/roster/groups/{quote(session_id, safe='')}/members",
                headers={
                    "Accept": "application/json",
                    **wxbot_sdk_headers(settings),
                },
                timeout_seconds=10.0,
                max_response_bytes=2 * 1024 * 1024,
                allowed_response_content_types=(
                    "application/json",
                    "application/problem+json",
                    "text/plain",
                ),
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    members = payload.get("members") or payload.get("items") or payload.get("candidates") or []
    if not isinstance(members, list):
        return None

    member = _match_member_by_id(members, wxid) if wxid else _match_member(members, query)
    if not member:
        return None
    member_wxid = _member_wxid(member) or wxid
    display_name = _member_display_name(member) or member_wxid or query or wxid
    avatar = member.get("avatar") if isinstance(member.get("avatar"), dict) else {}
    avatar_url = _avatar_url(base_url, member_wxid, avatar)
    if not avatar_url:
        avatar_url = _absolute_url(base_url, str(member.get("avatar_url") or "").strip())
    if not avatar_url:
        return None

    source_label = f"avatar:{member_wxid or display_name}"
    image_path = str(
        member.get("avatar_file_path")
        or member.get("avatar_path")
        or member.get("image_path")
        or ""
    ).strip()
    if cache_reference_image is None:
        return DrawAvatarReference(
            query=query or wxid,
            display_name=display_name,
            wxid=member_wxid,
            avatar_url=avatar_url,
            image_path=image_path,
            source_label=source_label,
        )
    try:
        cached = await cache_reference_image(
            image_url=avatar_url,
            source_label=source_label,
            trace_id=trace_id,
        )
    except Exception as exc:
        if not _is_draw_api_error(exc):
            raise
        return DrawAvatarReference(
            query=query or wxid,
            display_name=display_name,
            wxid=member_wxid,
            avatar_url=avatar_url,
            image_path=image_path,
            source_label=source_label,
        )
    return DrawAvatarReference(
        query=query or wxid,
        display_name=display_name,
        wxid=member_wxid,
        avatar_url=avatar_url,
        image_path=str(cached.file_name or ""),
        source_label=source_label,
    )


def _member_wxid(item: dict[str, Any]) -> str:
    return str(item.get("wxid") or item.get("user_id") or item.get("member_wxid") or "").strip()


def _is_draw_api_error(exc: Exception) -> bool:
    try:
        from plugins.draw.store import DrawApiError
    except ImportError:
        return (
            exc.__class__.__name__ == "DrawApiError"
            and exc.__class__.__module__ == "plugins.draw.store"
        )
    return isinstance(exc, DrawApiError)


def _match_member_by_id(members: list[Any], wxid: str) -> dict[str, Any]:
    lowered = str(wxid or "").strip().lower()
    if not lowered:
        return {}
    for item in members:
        if not isinstance(item, dict):
            continue
        if lowered in {
            str(item.get("wxid") or "").strip().lower(),
            str(item.get("user_id") or "").strip().lower(),
            str(item.get("member_wxid") or "").strip().lower(),
        }:
            return dict(item)
    return {}


def _match_member(members: list[Any], query: str) -> dict[str, Any]:
    lowered = str(query or "").strip().lower()
    if not lowered:
        return {}
    normalized = [item for item in members if isinstance(item, dict)]
    for item in normalized:
        if lowered in {
            str(item.get("wxid") or "").strip().lower(),
            str(item.get("user_id") or "").strip().lower(),
            str(item.get("member_wxid") or "").strip().lower(),
            _member_display_name(item).lower(),
            str(item.get("remark") or "").strip().lower(),
            str(item.get("alias") or "").strip().lower(),
            str(item.get("nick_name") or "").strip().lower(),
            str(item.get("name") or "").strip().lower(),
        }:
            return dict(item)
    for item in normalized:
        id_values = [
            str(item.get("wxid") or ""),
            str(item.get("user_id") or ""),
            str(item.get("member_wxid") or ""),
        ]
        name_values = [
            _member_display_name(item),
            str(item.get("remark") or ""),
            str(item.get("alias") or ""),
            str(item.get("nick_name") or ""),
            str(item.get("name") or ""),
        ]
        if any(lowered in value.lower() for value in id_values):
            return dict(item)
        if any(
            value
            and (lowered in value.lower() or value.lower() in lowered)
            for value in name_values
        ):
            return dict(item)
    return {}


def _member_display_name(item: dict[str, Any]) -> str:
    return str(
        item.get("display_name")
        or item.get("group_nickname")
        or item.get("group_nick_name")
        or item.get("group_remark")
        or item.get("nickname")
        or item.get("nick_name")
        or item.get("remark")
        or item.get("alias")
        or item.get("name")
        or item.get("member_name")
        or ""
    ).strip()


def _absolute_url(base_url: str, value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    return f"{base_url}/{url.lstrip('/')}"


def _avatar_url(base_url: str, wxid: str, avatar: dict[str, Any]) -> str:
    direct_url = str(avatar.get("avatar_url") or "").strip()
    if direct_url:
        return _absolute_url(base_url, direct_url)
    cache_url = str(avatar.get("cache_url") or "").strip()
    if cache_url:
        return _absolute_url(base_url, cache_url)
    for key in ("big_head_url", "small_head_url"):
        value = str(avatar.get(key) or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    if wxid and bool(avatar.get("cached")):
        return f"{base_url}/ext/roster/avatars/{quote(wxid, safe='')}"
    return ""
