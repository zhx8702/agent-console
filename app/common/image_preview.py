from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

from app.common.safe_url import safe_get

_THUMBNAIL_SUFFIX_RE = re.compile(r"_(?:thumbnail|thumb)(?=\.[^/?#]+(?:[?#]|$))")


@dataclass
class FetchedImage:
    url: str
    content: bytes
    media_type: str


def preview_url_from_thumbnail(url: str) -> str:
    return _THUMBNAIL_SUFFIX_RE.sub("_preview", str(url or "").strip())


def is_http_url(url: str) -> bool:
    return str(url or "").strip().lower().startswith(("http://", "https://"))


def preview_first_urls(image_url: str) -> list[str]:
    image_url = str(image_url or "").strip()
    if not image_url:
        return []
    preview_url = preview_url_from_thumbnail(image_url)
    urls = [preview_url, image_url] if preview_url != image_url else [image_url]
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url and url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def content_type_media_type(value: str | None) -> str:
    media_type = str(value or "image/png").split(";", 1)[0].strip().lower()
    return media_type if media_type.startswith("image/") else "image/png"


async def fetch_image_once(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int | None = None,
) -> FetchedImage:
    response = await safe_get(
        client,
        url,
        headers={"Accept": "image/*,*/*"},
    )
    response.raise_for_status()
    media_type = content_type_media_type(response.headers.get("content-type"))
    content = response.content
    if not content:
        raise httpx.HTTPError("empty image response")
    if max_bytes is not None and len(content) > max_bytes:
        raise httpx.HTTPError(f"image response too large: {len(content)} > {max_bytes}")
    return FetchedImage(url=url, content=content, media_type=media_type)


async def wait_for_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    wait_seconds: float,
    poll_interval_seconds: float,
    max_bytes: int | None = None,
) -> FetchedImage:
    wait_seconds = max(0.0, float(wait_seconds or 0.0))
    poll_interval_seconds = max(0.1, float(poll_interval_seconds or 0.5))
    deadline = asyncio.get_running_loop().time() + wait_seconds
    last_error: Exception | None = None

    while True:
        try:
            return await fetch_image_once(client, url, max_bytes=max_bytes)
        except Exception as exc:
            last_error = exc
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(min(poll_interval_seconds, max(0.0, deadline - asyncio.get_running_loop().time())))

    if isinstance(last_error, Exception):
        raise last_error
    raise httpx.HTTPError("image fetch failed")
