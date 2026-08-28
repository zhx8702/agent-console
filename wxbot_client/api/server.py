"""Flask HTTP API for wxbot SDK.

Exposes message receive/send operations as REST endpoints so external
consumers can integrate via HTTP without touching WeChat internals.

Endpoints:
  GET  /messages           - Pull inbound messages (cursor-based pagination)
  POST /send               - Queue a single outbound message (text, image, or video)
  POST /send/batch         - Queue multiple outbound messages
  GET  /sessions           - List active sessions from WeChat DB
  GET  /status             - SDK health / auth status
  GET  /images/<path>      - Serve decrypted image files
  GET  /queue/stats        - Outbound queue statistics
  GET  /queue/messages     - Outbound queue message list
  POST /queue/clear        - Mark outbound queue messages as cleared
  GET  /debug/trigger-config - Trigger filter debug config
  POST /debug/trigger-config - Update trigger filter debug config
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

try:
    from .auth import request_token_authorized, token_required_for_host
except ImportError:  # pragma: no cover - direct ``python api/server.py`` launch
    from auth import request_token_authorized, token_required_for_host

try:
    from wxbot_client.secure_files import resolve_relative_file
except ImportError:  # pragma: no cover - direct client launch
    from secure_files import resolve_relative_file


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    import queue_store as qs
    from sealed_core import runtime

    import config

    images_base = os.path.join(os.path.dirname(config.DECRYPTED_DIR), "images")
    api_token = str(getattr(config, "API_TOKEN", "") or "").strip()
    api_host = str(getattr(config, "API_HOST", "127.0.0.1") or "").strip().lower()
    if token_required_for_host(api_host, api_token):
        raise RuntimeError(
            "WXBOT_API_TOKEN must be a high-entropy token of at least 32 characters"
        )

    def _identity_snapshot(*, refresh=False):
        try:
            from sealed_core import ingest_loader as ingest

            value = (
                ingest.resolve_self_identity()
                if refresh
                else ingest.identity_status(refresh=False)
            )
        except Exception:
            value = None
        if not isinstance(value, dict):
            return {
                "ready": False,
                "self_wxid": "",
                "self_rowid": None,
                "reason": "self_identity_unavailable",
                "checked_at": 0,
            }
        try:
            rowid = int(value.get("self_rowid"))
        except (TypeError, ValueError):
            rowid = -1
        wxid = str(value.get("self_wxid") or "").strip()
        ready = value.get("ready") is True and bool(wxid) and rowid > 0
        return {
            "ready": ready,
            "self_wxid": wxid if ready else "",
            "self_rowid": rowid if ready else None,
            "reason": "" if ready else str(
                value.get("reason") or "self_identity_unavailable"
            )[:64],
            "checked_at": int(value.get("checked_at") or 0),
        }

    @app.before_request
    def _require_sdk_token():
        if request.method == "OPTIONS":
            return None
        if request_token_authorized(
            api_token,
            authorization=str(request.headers.get("Authorization") or ""),
            header_token=str(request.headers.get("X-Wxbot-Token") or ""),
        ):
            return None
        response = jsonify({"error": "wxbot_api_unauthorized"})
        response.status_code = 401
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.before_request
    def _require_resolved_identity_for_send():
        if request.method != "POST" or not request.path.startswith("/send"):
            return None
        identity = _identity_snapshot(refresh=True)
        if identity["ready"] is True:
            return None
        return (
            jsonify(
                {
                    "error": "self_identity_unavailable",
                    "identity": identity,
                }
            ),
            503,
        )

    def _image_relative_path(image_path):
        normalized = str(image_path or "").strip().replace("\\", "/")
        lower = normalized.lower()
        marker = "/images/"
        marker_index = lower.rfind(marker)
        if marker_index >= 0:
            normalized = normalized[marker_index + len(marker) :]
        elif lower.startswith("images/"):
            normalized = normalized[len("images/") :]
        return normalized.lstrip("/")

    def _image_url(image_path):
        relative = _image_relative_path(image_path)
        return f"/images/{relative}" if relative else ""

    def _existing_relative_image(relative_path):
        relative = _image_relative_path(relative_path)
        if not relative:
            return ""
        full_path = os.path.normpath(os.path.join(images_base, relative))
        try:
            if os.path.commonpath([os.path.normpath(images_base), full_path]) != os.path.normpath(
                images_base
            ):
                return ""
        except ValueError:
            return ""
        return relative if os.path.isfile(full_path) else ""

    def _thumbnail_relative_path(image_path):
        relative = _image_relative_path(image_path)
        if not relative:
            return ""
        path = Path(relative)
        if path.stem.endswith("_thumb"):
            return relative
        thumb_name = f"{path.stem}_thumb{path.suffix}"
        return _existing_relative_image(str(path.with_name(thumb_name)))

    def _preview_relative_path(image_path):
        relative = _image_relative_path(image_path)
        if not relative:
            return ""
        path = Path(relative)
        if not path.stem.endswith("_thumb"):
            return relative
        preview_name = f"{path.stem[:-6]}{path.suffix}"
        return _existing_relative_image(str(path.with_name(preview_name))) or relative

    def _decorate_message(msg):
        item = dict(msg)
        identity = _identity_snapshot(refresh=False)
        sender_wxid = str(item.get("sender_wxid") or "").strip()
        is_self_sent = bool(item.get("is_self_sent")) or (
            identity["ready"] is True
            and bool(sender_wxid)
            and sender_wxid.casefold()
            == str(identity["self_wxid"]).casefold()
        )
        item["is_self_sent"] = is_self_sent
        item["identity_resolved"] = identity["ready"]
        item["self_wxid"] = identity["self_wxid"]
        item["self_rowid"] = identity["self_rowid"]
        item["bot_wxid"] = identity["self_wxid"]
        if identity["ready"] is not True:
            item["capture_allowed"] = False
            item["capture_reason"] = "self_identity_unavailable"
        image_path = str(item.get("image_path") or "")
        if image_path and not item.get("image_url"):
            item["image_url"] = _image_url(image_path)
        media_status = str(item.get("media_status") or "")
        if item.get("msg_type") == "image" and not media_status:
            media_status = "ready" if image_path else "pending"
            item["media_status"] = media_status
        item.setdefault("image_failure_reason", "")
        item.setdefault("media_variant", "")
        if item.get("msg_type") == "image" and image_path:
            preview_relative = _preview_relative_path(image_path)
            thumbnail_relative = _thumbnail_relative_path(image_path)
            if preview_relative:
                item["image_preview_path"] = preview_relative
                item["image_preview_url"] = _image_url(preview_relative)
            if thumbnail_relative:
                item["image_thumbnail_path"] = thumbnail_relative
                item["image_thumbnail_url"] = _image_url(thumbnail_relative)
            elif media_status == "thumbnail" or str(item.get("media_variant") or "") == "thumbnail":
                item["image_thumbnail_path"] = image_path
                item["image_thumbnail_url"] = item.get("image_url") or _image_url(image_path)
        return item

    def _stream_envelope(msg):
        item = _decorate_message(msg)
        media_status = str(item.get("media_status") or "")
        media_variant = str(item.get("media_variant") or "")
        message = {
            "id": str(item.get("msg_svr_id") or ""),
            "type": str(item.get("msg_type") or "text"),
            "text": str(item.get("msg_text") or ""),
            "image_path": item.get("image_path") or "",
            "image_url": item.get("image_url") or "",
            "image_preview_path": item.get("image_preview_path") or "",
            "image_preview_url": item.get("image_preview_url") or "",
            "image_thumbnail_path": item.get("image_thumbnail_path") or "",
            "image_thumbnail_url": item.get("image_thumbnail_url") or "",
            "media_status": media_status,
            "image_failure_reason": item.get("image_failure_reason") or "",
            "media_variant": media_variant,
            "recv_ts": int(item.get("recv_ts") or 0),
            "mentioned_me": bool(item.get("mentioned_me")),
            "mention_mode": item.get("mention_mode") or "",
            "is_self_sent": bool(item.get("is_self_sent")),
            "at_wxids": list(item.get("at_wxids") or []),
            "quote": item.get("quote"),
            "bot_mentioned": bool(item.get("bot_mentioned") or item.get("mentioned_me")),
            "bot_addressed": bool(item.get("bot_addressed")),
            "bot_mention_position": item.get("bot_mention_position") or "",
            "bot_mention_names": list(item.get("bot_mention_names") or []),
            "bot_normalized_content": item.get("bot_normalized_content") or "",
            "bot_wxid": item.get("bot_wxid") or "",
            "identity_resolved": bool(item.get("identity_resolved")),
            "self_wxid": item.get("self_wxid") or "",
            "self_rowid": item.get("self_rowid"),
            "capture_allowed": bool(item.get("capture_allowed", True)),
            "capture_reason": item.get("capture_reason") or "",
        }
        media = None
        if item.get("msg_type") == "image":
            media = {
                "type": "image",
                "status": media_status,
                "variant": media_variant,
                "image_path": item.get("image_path") or "",
                "image_url": item.get("image_url") or "",
                "image_preview_path": item.get("image_preview_path") or "",
                "image_preview_url": item.get("image_preview_url") or "",
                "image_thumbnail_path": item.get("image_thumbnail_path") or "",
                "image_thumbnail_url": item.get("image_thumbnail_url") or "",
                "failure_reason": item.get("image_failure_reason") or "",
            }
        return {
            "id": int(item.get("id") or 0),
            "event_id": f"stream:{item.get('id')}",
            "event_type": "message.received",
            "source": "wxbot-sdk",
            "identity": {
                "ready": bool(item.get("identity_resolved")),
                "self_wxid": item.get("self_wxid") or "",
                "self_rowid": item.get("self_rowid"),
            },
            "occurred_ts": int(item.get("recv_ts") or 0),
            "occurred_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z",
                time.localtime(int(item.get("recv_ts") or time.time())),
            ),
            "session": {
                "id": item.get("session_id") or "",
                "name": item.get("session_name") or "",
                "kind": "group"
                if str(item.get("session_id") or "").endswith("@chatroom")
                else "private",
            },
            "sender": {
                "id": item.get("sender_wxid") or "",
                "name": item.get("sender_name") or "",
            },
            "message": message,
            "media": media,
            "raw": {"msg_svr_id": item.get("msg_svr_id") or ""},
            "meta": {
                "legacy_source": "inbound_queue",
                "media_status": media_status,
                "media_variant": media_variant,
                "identity_resolved": bool(item.get("identity_resolved")),
            },
        }

    def _sse_response(generator):
        return Response(generator(), mimetype="text/event-stream")

    def _json_object():
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else None

    def _command_id(data, *, fallback=""):
        delivery = data.get("delivery") if isinstance(data.get("delivery"), dict) else {}
        explicit_values = [
            str(candidate).strip()
            for candidate in (
                data.get("command_id"),
                delivery.get("command_id"),
                delivery.get("idempotency_key"),
            )
            if str(candidate or "").strip()
        ]
        if len(set(explicit_values)) > 1:
            raise ValueError("conflicting_command_ids")
        value = str(
            (explicit_values[0] if explicit_values else "")
            or fallback
            or request.headers.get("Idempotency-Key")
            or ""
        ).strip()
        if not value:
            raise ValueError("idempotency_key_required")
        if len(value) > 128:
            raise ValueError("invalid_idempotency_key")
        return value

    def _required_idempotency_key():
        value = str(request.headers.get("Idempotency-Key") or "").strip()
        if len(value) < 8 or len(value) > 128:
            raise ValueError("valid Idempotency-Key header required")
        return value

    def _mutation_response(result):
        response = jsonify(result.response)
        response.headers["Idempotency-Replayed"] = "true" if result.replayed else "false"
        return response

    def _reject_unknown_fields(value, allowed, label):
        unknown = sorted(set(value).difference(allowed))
        if unknown:
            raise ValueError(f"unknown_{label}_fields:{','.join(unknown)}")

    def _normalize_flat_message(data, *, fallback_command_id=""):
        allowed = {
            "session_id",
            "session_name",
            "session_kind",
            "sender_name",
            "sender_wxid",
            "mention_sender",
            "reply_to_msg_svr_id",
            "text",
            "reply_text",
            "msg_type",
            "image_path",
            "image_url",
            "video_path",
            "video_url",
            "source_message",
            "delivery",
            "command_id",
        }
        unknown = sorted(set(data).difference(allowed))
        if unknown:
            raise ValueError(f"unknown_fields:{','.join(unknown)}")
        session_id = str(data.get("session_id") or "").strip()
        if not session_id or len(session_id) > 256:
            raise ValueError("valid_session_id_required")
        session_name = str(data.get("session_name") or "")
        sender_name = str(data.get("sender_name") or "")
        sender_wxid = data.get("sender_wxid")
        session_kind = data.get("session_kind")
        reply_to_msg_svr_id = data.get("reply_to_msg_svr_id")
        image_url = data.get("image_url")
        video_url = data.get("video_url")
        for label, value in {
            "sender_wxid": sender_wxid,
            "session_kind": session_kind,
            "reply_to_msg_svr_id": reply_to_msg_svr_id,
            "image_url": image_url,
            "video_url": video_url,
        }.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{label}_must_be_string")
        if len(session_name) > 256 or len(sender_name) > 256 or len(str(sender_wxid or "")) > 256:
            raise ValueError("name_too_long")
        if len(str(reply_to_msg_svr_id or "")) > 256:
            raise ValueError("reply_reference_too_long")
        if len(str(image_url or "")) > 2048:
            raise ValueError("image_url_too_long")
        if len(str(video_url or "")) > 2048:
            raise ValueError("video_url_too_long")
        if str(session_kind or "") not in {"", "group", "private"}:
            raise ValueError("invalid_session_kind")
        for label in ("source_message", "delivery"):
            value = data.get(label)
            if value is not None and not isinstance(value, dict):
                raise ValueError(f"{label}_must_be_object")
        mention_sender = data.get("mention_sender", False)
        if not isinstance(mention_sender, bool):
            raise ValueError("mention_sender_must_be_boolean")
        msg_type = str(data.get("msg_type") or "text").strip().lower()
        if msg_type not in {"text", "image", "video"}:
            raise ValueError("msg_type_must_be_text_image_or_video")
        reply_text = data.get("text")
        if reply_text is None:
            reply_text = data.get("reply_text")
        if reply_text is not None and not isinstance(reply_text, str):
            raise ValueError("text_must_be_string")
        image_path = data.get("image_path")
        video_path = data.get("video_path")
        if video_path is not None and not isinstance(video_path, str):
            raise ValueError("video_path_must_be_string")
        if msg_type == "video":
            image_path = video_path if video_path is not None else image_path
            image_url = data.get("video_url", image_url)
            if not str(image_path or "").strip() and str(image_url or "").strip():
                # Keep the legacy SQLite queue schema while allowing the new
                # SDK sender to receive a remote video URL.
                image_path = image_url
        if image_path is not None and not isinstance(image_path, str):
            raise ValueError("image_path_must_be_string")
        if isinstance(reply_text, str) and len(reply_text) > 8000:
            raise ValueError("text_too_long")
        if isinstance(image_path, str) and len(image_path) > 2048:
            raise ValueError("image_path_too_long")
        if msg_type == "image" and not str(image_path or "").strip():
            raise ValueError("image_path_required_for_image_messages")
        if msg_type == "video" and not str(image_path or "").strip():
            raise ValueError("video_path_or_url_required_for_video_messages")
        if msg_type == "text" and not str(reply_text or "").strip():
            raise ValueError("text_required_for_text_messages")
        return {
            "session_id": session_id,
            "session_name": session_name,
            "sender_name": sender_name,
            "mention_sender": mention_sender,
            "reply_text": reply_text,
            "image_path": image_path,
            "msg_type": msg_type,
            "command_id": _command_id(data, fallback=fallback_command_id),
        }

    def _enqueue_flat_message(data, *, fallback_command_id=""):
        result = qs.enqueue_outbound(
            **_normalize_flat_message(
                data,
                fallback_command_id=fallback_command_id,
            )
        )
        return {
            "queued": True,
            "id": result.row_id,
            "idempotent_replayed": result.replayed,
        }

    def _flatten_envelope(data):
        allowed = {
            "target",
            "sender",
            "content",
            "reply",
            "source_message",
            "delivery",
            "command_id",
            "metadata",
        }
        unknown = sorted(set(data).difference(allowed))
        if unknown:
            raise ValueError(f"unknown_fields:{','.join(unknown)}")
        target = data.get("target")
        sender = data.get("sender") or {}
        content = data.get("content")
        reply = data.get("reply") or {}
        if not isinstance(target, dict) or not isinstance(content, dict):
            raise ValueError("target_and_content_objects_required")
        if not isinstance(sender, dict) or not isinstance(reply, dict):
            raise ValueError("sender_and_reply_must_be_objects")
        source_message = data.get("source_message", {})
        delivery = data.get("delivery", {})
        metadata = data.get("metadata", {})
        if not isinstance(source_message, dict):
            raise ValueError("source_message_must_be_object")
        if not isinstance(delivery, dict):
            raise ValueError("delivery_must_be_object")
        if not isinstance(metadata, dict):
            raise ValueError("metadata_must_be_object")
        _reject_unknown_fields(
            target,
            {"session_id", "session_name", "session_kind"},
            "target",
        )
        _reject_unknown_fields(sender, {"name", "wxid"}, "sender")
        _reject_unknown_fields(
            content,
            {
                "text",
                "msg_type",
                "image_path",
                "image_url",
                "video_path",
                "video_url",
            },
            "content",
        )
        _reject_unknown_fields(
            reply,
            {"mention_sender", "reply_to_msg_svr_id"},
            "reply",
        )
        _reject_unknown_fields(
            metadata,
            {"tenant_id", "trace_id", "command_id", "protocol"},
            "metadata",
        )
        return {
            "session_id": target.get("session_id"),
            "session_name": target.get("session_name", ""),
            "session_kind": target.get("session_kind", ""),
            "sender_name": sender.get("name", ""),
            "sender_wxid": sender.get("wxid", ""),
            "mention_sender": reply.get("mention_sender", False),
            "reply_to_msg_svr_id": reply.get("reply_to_msg_svr_id", ""),
            "text": content.get("text"),
            "msg_type": content.get("msg_type", "text"),
            "image_path": content.get("image_path"),
            "image_url": content.get("image_url"),
            "video_path": content.get("video_path"),
            "video_url": content.get("video_url"),
            "source_message": source_message,
            "delivery": delivery,
            "command_id": data.get("command_id", ""),
        }

    @app.route("/messages", methods=["GET"])
    def get_messages():
        cursor = request.args.get("cursor", 0, type=int)
        limit = request.args.get("limit", 100, type=int)
        limit = min(limit, 500)
        messages = [_decorate_message(m) for m in qs.pull_inbound(cursor=cursor, limit=limit)]
        next_cursor = messages[-1]["id"] if messages else cursor
        return jsonify(
            {
                "messages": messages,
                "cursor": next_cursor,
                "count": len(messages),
            }
        )

    @app.route("/messages/stream", methods=["GET"])
    def stream_messages():
        start_cursor = request.args.get("cursor", 0, type=int)

        def _gen():
            cursor = start_cursor
            while True:
                messages = qs.pull_inbound(cursor=cursor, limit=100)
                for msg in messages:
                    item = _decorate_message(msg)
                    cursor = max(cursor, int(item.get("id") or cursor))
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                time.sleep(1)

        return _sse_response(_gen)

    @app.route("/stream", methods=["GET"])
    def unified_stream():
        start_cursor = request.args.get("cursor", 0, type=int)

        def _gen():
            cursor = start_cursor
            while True:
                messages = qs.pull_inbound(cursor=cursor, limit=100)
                for msg in messages:
                    envelope = _stream_envelope(msg)
                    cursor = max(cursor, int(envelope.get("id") or cursor))
                    event_type = str(envelope.get("event_type") or "message.received")
                    yield f"id: {envelope['id']}\n"
                    yield f"event: {event_type}\n"
                    yield f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
                time.sleep(1)

        return _sse_response(_gen)

    @app.route("/send", methods=["POST"])
    def send_message():
        data = _json_object()
        if data is None:
            return jsonify({"error": "json_object_required"}), 400
        try:
            return jsonify(_enqueue_flat_message(data))
        except qs.OutboundIdempotencyConflict:
            return jsonify({"error": "idempotency_key_conflict"}), 409
        except ValueError as exc:
            status = 428 if str(exc) == "idempotency_key_required" else 400
            return jsonify({"error": str(exc)}), status

    @app.route("/send/batch", methods=["POST"])
    def send_batch():
        data = _json_object()
        if data is None or set(data).difference({"messages"}):
            return jsonify({"error": "strict_json_object_required"}), 400
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 100:
            return jsonify({"error": "messages array required"}), 400
        try:
            batch_key = _required_idempotency_key()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 428
        normalized_messages = []
        for index, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return jsonify({"error": f"message_{index}_must_be_object"}), 400
            item_key = f"batch:{hashlib.sha256(batch_key.encode()).hexdigest()}:{index}"
            try:
                normalized_messages.append(
                    _normalize_flat_message(msg, fallback_command_id=item_key)
                )
            except ValueError as exc:
                return jsonify({"error": str(exc), "index": index}), 400
        try:
            queued = qs.enqueue_outbound_batch(normalized_messages)
        except qs.OutboundIdempotencyConflict:
            return jsonify({"error": "idempotency_key_conflict"}), 409
        results = [
            {
                "queued": True,
                "id": item.row_id,
                "idempotent_replayed": item.replayed,
            }
            for item in queued
        ]
        return jsonify({"results": results, "count": len(results)})

    @app.route("/send/envelope", methods=["POST"])
    def send_envelope():
        data = _json_object()
        if data is None:
            return jsonify({"error": "json_object_required"}), 400
        try:
            return jsonify(_enqueue_flat_message(_flatten_envelope(data)))
        except qs.OutboundIdempotencyConflict:
            return jsonify({"error": "idempotency_key_conflict"}), 409
        except ValueError as exc:
            status = 428 if str(exc) == "idempotency_key_required" else 400
            return jsonify({"error": str(exc)}), status

    @app.route("/send/envelope/batch", methods=["POST"])
    def send_envelope_batch():
        data = _json_object()
        if data is None or set(data).difference({"messages", "envelopes"}):
            return jsonify({"error": "strict_json_object_required"}), 400
        envelopes = data.get("envelopes", data.get("messages"))
        if not isinstance(envelopes, list) or not envelopes or len(envelopes) > 100:
            return jsonify({"error": "envelopes array required"}), 400
        try:
            batch_key = _required_idempotency_key()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 428
        normalized_messages = []
        for index, envelope in enumerate(envelopes):
            if not isinstance(envelope, dict):
                return jsonify({"error": f"envelope_{index}_must_be_object"}), 400
            item_key = f"batch:{hashlib.sha256(batch_key.encode()).hexdigest()}:{index}"
            try:
                normalized_messages.append(
                    _normalize_flat_message(
                        _flatten_envelope(envelope),
                        fallback_command_id=item_key,
                    )
                )
            except ValueError as exc:
                return jsonify({"error": str(exc), "index": index}), 400
        try:
            queued = qs.enqueue_outbound_batch(normalized_messages)
        except qs.OutboundIdempotencyConflict:
            return jsonify({"error": "idempotency_key_conflict"}), 409
        results = [
            {
                "queued": True,
                "id": item.row_id,
                "idempotent_replayed": item.replayed,
            }
            for item in queued
        ]
        return jsonify({"results": results, "count": len(results)})

    @app.route("/sessions", methods=["GET"])
    def list_sessions():
        try:
            from sealed_core import ingest_loader as ingest

            mapping = ingest.build_session_mapping()
            sessions = []
            for _table, info in mapping.items():
                sessions.append(
                    {
                        "session_id": info["session_id"],
                        "session_name": info["session_name"],
                        "kind": info["kind"],
                    }
                )
            return jsonify({"sessions": sessions, "count": len(sessions)})
        except Exception:
            return jsonify({"error": "session_roster_unavailable", "sessions": []}), 503

    @app.route("/status", methods=["GET"])
    def status():
        auth_active = runtime.is_active()
        identity = _identity_snapshot(refresh=True)
        snap = {}
        if auth_active:
            try:
                with runtime._lock:
                    g = runtime._guard
                if g:
                    snap = g.status_snapshot()
            except Exception:
                pass

        inbound_stats = qs.inbound_image_stats()
        outbound = qs.outbound_stats()
        max_inbound_id = int(inbound_stats.pop("max_inbound_id", 0))
        payload = {
                "status": "running" if identity["ready"] else "unhealthy",
                "auth_active": auth_active,
                "identity": identity,
                "capabilities": snap.get("capabilities", []),
                "queue": {
                    "max_inbound_id": max_inbound_id,
                    "max_event_id": 0,
                    "max_stream_id": max_inbound_id,
                    "outbound_stats": outbound,
                    "inbound_image_stats": inbound_stats,
                },
                "config": config.summary(),
            }
        return jsonify(payload), (200 if identity["ready"] else 503)

    @app.route("/images/<path:filepath>", methods=["GET"])
    def serve_image(filepath):
        full_path = resolve_relative_file(images_base, filepath)
        if full_path is None:
            abort(403)
        return send_file(full_path)

    @app.route("/queue/stats", methods=["GET"])
    def queue_stats():
        stats = qs.outbound_stats()
        return jsonify(stats)

    @app.route("/queue/messages", methods=["GET"])
    def queue_messages():
        status = request.args.get("status", "", type=str)
        limit = request.args.get("limit", 100, type=int)
        if status not in {"", "pending", "running", "uncertain", "failed", "sent", "cleared"}:
            return jsonify({"error": "invalid_status"}), 400
        if limit is None or not 1 <= limit <= 500:
            return jsonify({"error": "limit_out_of_range"}), 400
        items = qs.list_outbound(status=status, limit=limit)
        return jsonify({"items": items, "count": len(items)})

    @app.route("/queue/messages/<int:row_id>", methods=["GET"])
    def queue_message(row_id):
        item = qs.get_outbound(row_id)
        if item is None:
            return jsonify({"error": "outbound_not_found"}), 404
        return jsonify(item)

    @app.route("/queue/clear", methods=["POST"])
    def clear_queue():
        data = _json_object()
        if data is None or set(data).difference({"status", "session_id"}):
            return jsonify({"error": "strict_json_object_required"}), 400
        status = str(data.get("status") or "pending").strip() or "pending"
        if status not in {"pending", "uncertain", "failed", "sent", "all"}:
            return jsonify({"error": "status must be pending/uncertain/failed/sent/all"}), 400
        session_id = str(data.get("session_id") or "").strip()
        if len(session_id) > 256:
            return jsonify({"error": "session_id_too_long"}), 400
        try:
            result = qs.clear_outbound_idempotent(
                status,
                session_id,
                idempotency_key=_required_idempotency_key(),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 428
        except qs.LocalMutationIdempotencyConflict:
            return jsonify({"error": "idempotency_key_conflict"}), 409
        return _mutation_response(result)

    @app.route("/queue/messages/<int:row_id>/reconcile", methods=["POST"])
    def reconcile_queue_message(row_id):
        data = _json_object()
        if data is None or set(data) != {"action"}:
            return jsonify({"error": "action required"}), 400
        idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
        if not idempotency_key or len(idempotency_key) > 128:
            return jsonify({"error": "valid Idempotency-Key header required"}), 428
        try:
            return jsonify(
                qs.reconcile_outbound(
                    row_id,
                    str(data.get("action") or ""),
                    idempotency_key=idempotency_key,
                )
            )
        except KeyError:
            return jsonify({"error": "outbound_not_found"}), 404
        except qs.OutboundIdempotencyConflict:
            return jsonify({"error": "idempotency_key_conflict"}), 409
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409

    @app.route("/debug/trigger-config", methods=["GET"])
    def trigger_debug_config():
        return jsonify(config.trigger_debug_summary())

    @app.route("/debug/trigger-config", methods=["POST"])
    def update_trigger_debug_config():
        data = _json_object()
        if data is None or set(data) != {"group_require_at_me"}:
            return jsonify({"error": "group_require_at_me required"}), 400
        if not isinstance(data["group_require_at_me"], bool):
            return jsonify({"error": "group_require_at_me must be boolean"}), 400
        payload = {"group_require_at_me": data["group_require_at_me"]}
        try:
            idempotency_key = _required_idempotency_key()
            replay = qs.begin_local_mutation(
                "trigger-config.update",
                idempotency_key,
                payload,
            )
            if replay is not None:
                return _mutation_response(replay)
            saved = {
                **config.set_group_require_at_me(data["group_require_at_me"]),
                "saved": True,
            }
            result = qs.complete_local_mutation(
                "trigger-config.update",
                idempotency_key,
                payload,
                saved,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 428
        except qs.LocalMutationIdempotencyConflict:
            return jsonify({"error": "idempotency_key_conflict"}), 409
        return _mutation_response(result)

    return app


def run_server(host: str = "127.0.0.1", port: int = 5080):
    app = create_app()
    print(f"[api] starting HTTP server on {host}:{port}")
    app.run(host=host, port=port, threaded=True)
