from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.common.image_preview import preview_url_from_thumbnail
from app.common.types import Session, Turn


@dataclass(frozen=True)
class QuoteImageSource:
    image_url: str = ""
    image_path: str = ""
    label: str = ""

    @property
    def found(self) -> bool:
        return bool(self.image_url or self.image_path)


def quote_image_source_from_metadata(
    metadata: dict[str, Any],
    *,
    session: Session | None = None,
) -> QuoteImageSource:
    metadata = dict(metadata or {})
    direct = _quote_image_source_from_single_metadata(
        metadata,
        metadata,
        allow_direct_image=False,
    )
    if direct.found:
        return direct

    quote = _record(metadata.get("quote"))
    reference_id = _quote_reference_id(quote)
    if not reference_id or session is None:
        return QuoteImageSource()

    return _quote_image_source_from_session_turns(
        session,
        reference_id=reference_id,
        current_metadata=metadata,
    )


def _quote_image_source_from_session_turns(
    session: Session,
    *,
    reference_id: str,
    current_metadata: dict[str, Any],
) -> QuoteImageSource:
    session_id = str(session.session_id or "")
    if not session_id:
        return QuoteImageSource()
    turns = list(session.turns or [])
    for turn in reversed(turns):
        if str(getattr(turn, "session_id", "") or "") != session_id:
            continue
        metadata = dict(getattr(turn, "metadata", {}) or {})
        if not _turn_matches_reference(turn, metadata, reference_id):
            continue
        resolved = _quote_image_source_from_single_metadata(
            metadata,
            current_metadata,
            allow_direct_image=True,
        )
        if resolved.found:
            return resolved
    return QuoteImageSource()


def _quote_image_source_from_single_metadata(
    source_metadata: dict[str, Any],
    current_metadata: dict[str, Any],
    *,
    allow_direct_image: bool,
) -> QuoteImageSource:
    image_url = _quote_image_url(source_metadata, allow_direct_image=allow_direct_image)
    quote_media_path = _image_path_from_records(_quote_image_records(source_metadata))
    image_path = str(
        source_metadata.get("quote_image_path")
        or quote_media_path
        or (source_metadata.get("image_path") if allow_direct_image else "")
        or ""
    ).strip()
    if not image_url and not image_path:
        return QuoteImageSource()
    return QuoteImageSource(
        image_url=image_url,
        image_path=image_path,
        label=_quote_source_label(current_metadata),
    )


def _quote_image_url(metadata: dict[str, Any], *, allow_direct_image: bool) -> str:
    direct_preview = str(
        metadata.get("quote_image_preview_url")
        or _quote_variant_url(metadata, "preview")
        or (metadata.get("image_preview_url") if allow_direct_image else "")
        or (
            _image_variant_url_from_records(_direct_image_records(metadata), "preview")
            if allow_direct_image
            else ""
        )
        or ""
    ).strip()
    if direct_preview:
        return direct_preview

    fallback = str(
        metadata.get("quote_image_thumbnail_url")
        or _quote_variant_url(metadata, "thumbnail")
        or metadata.get("quote_image_url")
        or _image_url_from_records(_quote_image_records(metadata))
        or (metadata.get("image_thumbnail_url") if allow_direct_image else "")
        or (
            _metadata_record(metadata.get("media")).get("image_thumbnail_url")
            if allow_direct_image
            else ""
        )
        or (
            _image_variant_url_from_records(_direct_image_records(metadata), "thumbnail")
            if allow_direct_image
            else ""
        )
        or (metadata.get("image_url") if allow_direct_image else "")
        or ""
    ).strip()
    return preview_url_from_thumbnail(fallback) or fallback


def _direct_image_records(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        metadata,
        _metadata_record(metadata.get("media")),
        _metadata_record(metadata.get("raw")),
    ]


def _quote_variant_url(metadata: dict[str, Any], variant: str) -> str:
    return _image_variant_url_from_records(_quote_image_records(metadata), variant)


def _quote_image_records(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    quote = _record(metadata.get("quote"))
    return [
        quote,
        _record(quote.get("message")),
        _record(quote.get("media")),
        _record(quote.get("raw")),
        _record(quote.get("quoted_message")),
        _record(quote.get("quoted")),
    ]


def _image_variant_url_from_records(records: Iterable[dict[str, Any]], variant: str) -> str:
    for record in records:
        image_variants = _record(record.get("image_variants"))
        variants = _record(record.get("variants"))
        for payload in (image_variants.get(variant), variants.get(variant), record.get(variant)):
            item = _record(payload)
            image_url = str(
                item.get("image_url")
                or item.get("url")
                or item.get("media_url")
                or ""
            ).strip()
            if image_url:
                return image_url
    return ""


def _image_url_from_records(records: Iterable[dict[str, Any]]) -> str:
    for record in records:
        image_url = str(
            record.get("image_url")
            or record.get("image_preview_url")
            or record.get("preview_url")
            or record.get("url")
            or ""
        ).strip()
        if image_url:
            return image_url
    return ""


def _image_path_from_records(records: Iterable[dict[str, Any]]) -> str:
    for record in records:
        image_path = str(
            record.get("image_path")
            or record.get("path")
            or record.get("local_path")
            or ""
        ).strip()
        if image_path:
            return image_path
    return ""


def _quote_source_label(metadata: dict[str, Any]) -> str:
    quote_id = _quote_reference_id(_record(metadata.get("quote")))
    return f"quote:{quote_id}" if quote_id else "reference"


def _quote_reference_id(quote: dict[str, Any]) -> str:
    return _first_nonempty_str(
        quote.get("refer_msg_svr_id"),
        quote.get("refer_message_id"),
        quote.get("refer_id"),
        quote.get("msg_svr_id"),
        quote.get("id"),
        quote.get("message_id"),
        (_record(quote.get("message"))).get("id"),
        (_record(quote.get("quoted_message"))).get("id"),
    )


def _turn_matches_reference(turn: Turn, metadata: dict[str, Any], reference_id: str) -> bool:
    return reference_id in {
        _clean_id(getattr(turn, "turn_id", "")),
        _clean_id(metadata.get("msg_svr_id")),
        _clean_id(metadata.get("message_id")),
        _clean_id(metadata.get("id")),
    }


def _first_nonempty_str(*values: Any) -> str:
    for value in values:
        text = _clean_id(value)
        if text:
            return text
    return ""


def _clean_id(value: Any) -> str:
    return str(value or "").strip()


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _metadata_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
