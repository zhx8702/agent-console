"""Deterministic, bounded staging for wxbot message-summary exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import stat
import unicodedata
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_MAX_EXPORT_BYTES = 10 * 1024 * 1024
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_CLEANUP_GRACE_SECONDS = 5 * 60

_IDENTITY_HASH_LENGTH = 24
_STAGED_FILE_NAME = "message-export.txt"
_STAGED_FILE_RE = re.compile(r"message-export\.(?:txt|md|csv|json)$")
_STAGED_FILE_TEMP_RE = re.compile(r"\.message-export\.(?:txt|md|csv|json)\.[0-9a-f]+\.tmp$")
_GENERIC_STAGED_FILE_RE = re.compile(r"artifact\.(?:txt|md|csv|json)$")
_GENERIC_STAGED_TEMP_RE = re.compile(r"\.artifact\.(?:txt|md|csv|json)\.[0-9a-f]+\.tmp$")
_DISPLAY_NAME_FILE_RE = re.compile(r"display-name\.txt$")
_DISPLAY_NAME_TEMP_RE = re.compile(r"\.display-name\.txt\.[0-9a-f]+\.tmp$")
_HASHED_DIRECTORY_RE = re.compile(r"^(?:tenant|session|request)-[0-9a-f]{24}$")
_HOUR_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_MESSAGE_TYPE_LABELS = {
    "text": "文字",
    "image": "图片",
    "audio": "语音",
    "video": "视频",
    "file": "文件",
    "event": "事件",
}
_MESSAGE_TYPE_ORDER = tuple(_MESSAGE_TYPE_LABELS)
_SUMMARY_TZ = ZoneInfo("Asia/Shanghai")


class MessageExportError(RuntimeError):
    """Base error for message export staging."""


class InvalidMessageExportPath(MessageExportError, ValueError):
    """The configured staging root or derived artifact path is unsafe."""


class MessageExportConflict(MessageExportError):
    """The request id is already bound to different immutable content."""


class MessageExportTooLarge(MessageExportError):
    """The encoded export exceeds its configured byte limit."""


def _require_identity(value: object, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _identity_directory(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_IDENTITY_HASH_LENGTH]
    return f"{prefix}-{digest}"


def _root_path(root_dir: str | os.PathLike[str], *, create: bool) -> Path:
    root = Path(root_dir).expanduser()
    if not root.is_absolute():
        raise InvalidMessageExportPath("message export staging root must be absolute")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise InvalidMessageExportPath("message export staging root must be a real directory")
    return root.resolve(strict=root.exists())


def _assert_under_root(path: Path, root: Path) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise InvalidMessageExportPath("message export path escapes the staging root") from exc
    return resolved


def _ensure_private_directory(path: Path, root: Path) -> None:
    root_stat = root.stat()
    root_gid = root_stat.st_gid
    inherit_group = os.name == "posix" and bool(root_stat.st_mode & stat.S_ISGID)
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise InvalidMessageExportPath("message export directory contains an unsafe link")
        else:
            current.mkdir(mode=0o750, exist_ok=True)
        if current.is_symlink() or not current.is_dir():
            raise InvalidMessageExportPath("message export directory contains an unsafe link")
        if os.name == "posix" and current.stat().st_gid != root_gid:
            os.chown(current, -1, root_gid)
        desired_mode = 0o2750 if inherit_group else 0o750
        if stat.S_IMODE(current.stat().st_mode) != desired_mode:
            current.chmod(desired_mode)
    _assert_under_root(path, root)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _safe_filename_component(value: object, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    normalized = _WINDOWS_FORBIDDEN_RE.sub("_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    normalized = normalized or fallback
    if normalized.upper() in _WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    return _truncate_utf8(normalized, 72).rstrip(" .") or fallback


def _display_filename(session_name: object, period: object, extension: str = "txt") -> str:
    session_component = _safe_filename_component(session_name, fallback="当前会话")
    period_component = _safe_filename_component(period, fallback="消息记录")
    stem = _truncate_utf8(f"消息汇总-{session_component}-{period_component}", 216).rstrip(" .")
    return f"{stem}.{extension}"


def _single_line(value: object, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFC", str(value or ""))
    normalized = re.sub(r"[\x00-\x1f\x7f]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or fallback


def _normalized_text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")


def _csv_cell(value: object) -> object:
    """Keep exported CSV cells from being interpreted as spreadsheet formulas."""

    if not isinstance(value, str):
        return value
    return f"'{value}" if value[:1] in {"=", "+", "-", "@"} else value


def _file_export_metadata(item: Mapping[str, Any]) -> dict[str, object] | None:
    """Expose bounded file facts while intentionally omitting SDK URLs/paths."""

    msg_type = str(item.get("msg_type") or "").strip().lower()
    if msg_type != "file":
        return None
    attachment = item.get("file_attachment")
    attachment = attachment if isinstance(attachment, Mapping) else {}
    raw_size = item.get("file_size", attachment.get("size"))
    try:
        size = max(0, int(raw_size or 0))
    except (TypeError, ValueError):
        size = 0
    return {
        "name": _single_line(
            item.get("file_name") or attachment.get("name"),
            fallback="",
        ),
        "size": size,
        "sha256": _single_line(
            item.get("file_sha256") or attachment.get("sha256"),
            fallback="",
        ).lower(),
        "status": _single_line(
            item.get("file_status")
            or item.get("file_download_status")
            or attachment.get("download_status")
            or item.get("media_status"),
            fallback="",
        ).lower(),
    }


def _message_text(item: Mapping[str, Any]) -> str:
    msg_type = str(item.get("msg_type") or "text").strip().lower()
    text = _normalized_text(item.get("text")).strip()
    if text:
        return text.replace("\n", "\n    ")
    placeholders = {
        "image": "[图片]",
        "audio": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "event": "[事件]",
    }
    return placeholders.get(msg_type, f"[{msg_type or '消息'}]")


def _message_lines(messages: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in messages:
        if not isinstance(item, Mapping):
            raise TypeError("each message export item must be a mapping")
        if bool(item.get("is_self_sent")):
            continue
        sender_name = _single_line(
            item.get("sender_name") or item.get("sender_wxid"),
            fallback="未知成员",
        )
        timestamp = _single_line(item.get("timestamp"), fallback="")
        prefix = f"[{timestamp}] " if timestamp else ""
        lines.append(f"{prefix}{sender_name}: {_message_text(item)}")
    return lines


def _summary_message_hour(item: Mapping[str, Any]) -> int | None:
    for key in ("ts", "occurred_ts"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value:
            timestamp = float(value)
            if timestamp > 1_000_000_000_000:
                timestamp /= 1000.0
            try:
                return datetime.fromtimestamp(timestamp, tz=_SUMMARY_TZ).hour
            except (OverflowError, OSError, ValueError):
                continue

    raw = str(item.get("timestamp") or item.get("occurred_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(_SUMMARY_TZ)
        return parsed.hour
    except ValueError:
        match = _HOUR_RE.search(raw)
        if not match:
            return None
        hour = int(match.group(1))
        return hour if 0 <= hour <= 23 else None


def _summary_sender(item: Mapping[str, Any]) -> tuple[str, str]:
    sender_id = _single_line(
        item.get("sender_wxid") or item.get("sender_name"),
        fallback="unknown",
    )
    display_name = _single_line(
        item.get("sender_name") or item.get("sender_wxid"),
        fallback="未知成员",
    )
    return sender_id, display_name


def _summary_message_type(item: Mapping[str, Any]) -> str:
    return _single_line(item.get("msg_type") or "text", fallback="text").lower()


def build_message_export_summary(
    session_name: str,
    period: str,
    messages: Sequence[Mapping[str, Any]],
    report_type: str = "daily",
) -> str:
    """Build a deterministic summary from the same raw payload exported to the file."""

    included: list[Mapping[str, Any]] = []
    for item in messages:
        if not isinstance(item, Mapping):
            raise TypeError("each message export item must be a mapping")
        if not bool(item.get("is_self_sent")):
            included.append(item)

    normalized_report_type = str(report_type or "daily").strip().lower()
    report_title = {
        "recent": "最近消息汇总",
        "daily": "日报",
        "weekly": "周报",
        "monthly": "月报",
    }.get(normalized_report_type, "消息汇总")
    title = (
        f"[{_single_line(session_name, fallback='当前会话')}] "
        f"{report_title} · {_single_line(period, fallback='未指定')}"
    )
    if not included:
        return f"{title}\n\n暂无可汇总的消息记录。"

    sender_counts: Counter[str] = Counter()
    sender_names: dict[str, str] = {}
    message_type_counts: Counter[str] = Counter()
    hours: Counter[int] = Counter()
    for item in included:
        sender_id, sender_name = _summary_sender(item)
        sender_counts[sender_id] += 1
        sender_names.setdefault(sender_id, sender_name)
        message_type_counts[_summary_message_type(item)] += 1
        hour = _summary_message_hour(item)
        if hour is not None:
            hours[hour] += 1

    type_order = {msg_type: index for index, msg_type in enumerate(_MESSAGE_TYPE_ORDER)}
    type_parts = [
        f"{_MESSAGE_TYPE_LABELS.get(msg_type, msg_type)} {count} 条"
        for msg_type, count in sorted(
            message_type_counts.items(),
            key=lambda item: (type_order.get(item[0], len(type_order)), item[0]),
        )
    ]
    ranked_senders = sorted(
        sender_counts.items(),
        key=lambda item: (-item[1], item[0], sender_names[item[0]]),
    )[:5]
    lines = [
        title,
        "",
        f"消息总数：{len(included)} 条",
        f"参与人数：{len(sender_counts)} 人",
        f"消息类型：{'、'.join(type_parts)}",
    ]
    if normalized_report_type == "daily":
        if hours:
            peak_hour, peak_count = min(hours.items(), key=lambda item: (-item[1], item[0]))
            lines.append(
                f"高峰时段：{peak_hour:02d}:00 - {(peak_hour + 1) % 24:02d}:00（{peak_count} 条）"
            )
        else:
            lines.append("高峰时段：时间信息不足")
    lines.extend(["", "活跃发送者 Top 5："])
    lines.extend(
        f"{index}. {sender_names[sender_id]} — {count} 条"
        for index, (sender_id, count) in enumerate(ranked_senders, start=1)
    )
    return "\n".join(lines)


def _render_export(
    *,
    session_id: str,
    session_name: object,
    period: object,
    summary_text: object,
    messages: Sequence[Mapping[str, Any]],
    export_format: str = "txt",
) -> tuple[bytes, int]:
    message_lines = _message_lines(messages)
    display_session = _single_line(session_name or session_id, fallback="当前会话")
    display_period = _single_line(period, fallback="未指定")
    summary = _normalized_text(summary_text).strip() or "（无汇总内容）"
    lines = [
        "消息汇总",
        f"会话：{display_session}",
        f"时间范围：{display_period}",
        f"消息数量：{len(message_lines)}",
        "",
        "一、汇总",
        summary,
        "",
        "二、原始消息记录",
        *message_lines,
        "",
    ]
    normalized_format = str(export_format or "txt").strip().lower()
    if normalized_format == "txt":
        return b"\xef\xbb\xbf" + "\n".join(lines).encode("utf-8"), len(message_lines)
    included = [
        item for item in messages if isinstance(item, Mapping) and not item.get("is_self_sent")
    ]
    if normalized_format == "md":
        markdown = [
            f"# {_single_line(session_name or session_id, fallback='当前会话')} 消息汇总",
            f"- 时间范围：{display_period}",
            f"- 消息数量：{len(message_lines)}",
            "",
            "## 汇总",
            summary,
            "",
            "## 原始消息记录",
            "| 时间 | 发送者 | 类型 | 内容 |",
            "| --- | --- | --- | --- |",
        ]
        for item in included:
            timestamp = _single_line(item.get("timestamp"), fallback="")
            sender = _single_line(
                item.get("sender_name") or item.get("sender_wxid"), fallback="未知成员"
            )
            msg_type = _single_line(item.get("msg_type") or "text", fallback="text")
            content = _message_text(item).replace("|", "\\|").replace("\n", "<br>")
            markdown.append(f"| {timestamp} | {sender} | {msg_type} | {content} |")
        return ("\n".join(markdown) + "\n").encode("utf-8"), len(message_lines)
    if normalized_format == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["timestamp", "sender", "type", "content"])
        for item in included:
            writer.writerow(
                [
                    _csv_cell(_single_line(item.get("timestamp"), fallback="")),
                    _csv_cell(
                        _single_line(
                            item.get("sender_name") or item.get("sender_wxid"),
                            fallback="未知成员",
                        )
                    ),
                    _csv_cell(_single_line(item.get("msg_type") or "text", fallback="text")),
                    _csv_cell(_normalized_text(item.get("text"))),
                ]
            )
        return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), len(message_lines)
    if normalized_format == "json":
        payload = {
            "title": _single_line(session_name or session_id, fallback="当前会话"),
            "period": display_period,
            "message_count": len(message_lines),
            "summary": summary,
            "messages": [],
        }
        for item in included:
            message_payload: dict[str, object] = {
                "timestamp": _single_line(item.get("timestamp"), fallback=""),
                "sender": _single_line(
                    item.get("sender_name") or item.get("sender_wxid"),
                    fallback="未知成员",
                ),
                "type": _single_line(item.get("msg_type") or "text", fallback="text"),
                "content": _normalized_text(item.get("text")),
            }
            file_metadata = _file_export_metadata(item)
            if file_metadata is not None:
                message_payload["file"] = file_metadata
            payload["messages"].append(message_payload)
        return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), len(
            message_lines
        )
    raise ValueError("export_format must be txt, md, csv or json")


def _existing_artifact_matches(path: Path, content: bytes) -> bool:
    if path.is_symlink() or not path.is_file():
        raise MessageExportConflict("the request path is occupied by a non-artifact")
    if path.stat().st_size != len(content):
        return False
    return path.read_bytes() == content


def _publish_immutable(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        if _existing_artifact_matches(path, content):
            return
        raise MessageExportConflict("the request id is already bound to different content")

    temporary = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        if os.name == "posix":
            os.fchown(descriptor, -1, path.parent.stat().st_gid)
            os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not _existing_artifact_matches(path, content):
                raise MessageExportConflict(
                    "the request id was concurrently bound to different content"
                ) from None
        try:
            if os.name == "posix" and path.stat().st_gid != path.parent.stat().st_gid:
                os.chown(path, -1, path.parent.stat().st_gid)
            path.chmod(0o640)
        except OSError:
            pass
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def stage_message_export(
    root_dir: str | os.PathLike[str],
    tenant_id: str,
    session_id: str,
    request_id: str,
    session_name: str,
    period: str,
    summary_text: str,
    messages: Sequence[Mapping[str, Any]],
    max_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
    *,
    export_format: str = "txt",
) -> dict[str, Any]:
    """Stage one immutable export and return ChannelFile metadata."""

    tenant = _require_identity(tenant_id, field="tenant_id")
    session = _require_identity(session_id, field="session_id")
    request = _require_identity(request_id, field="request_id")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    normalized_format = str(export_format or "txt").strip().lower()
    if normalized_format not in {"txt", "md", "csv", "json"}:
        raise ValueError("export_format must be txt, md, csv or json")

    content, message_count = _render_export(
        session_id=session,
        session_name=session_name,
        period=period,
        summary_text=summary_text,
        messages=messages,
        export_format=normalized_format,
    )
    if len(content) > max_bytes:
        raise MessageExportTooLarge(
            f"message export is {len(content)} bytes; limit is {max_bytes} bytes"
        )

    root = _root_path(root_dir, create=True)
    request_directory = (
        root
        / _identity_directory("tenant", tenant)
        / _identity_directory("session", session)
        / _identity_directory("request", request)
    )
    _ensure_private_directory(request_directory, root)
    artifact_path = _assert_under_root(
        request_directory / f"message-export.{normalized_format}",
        root,
    )
    _publish_immutable(artifact_path, content)
    if os.name == "posix" and artifact_path.stat().st_gid != root.stat().st_gid:
        os.chown(artifact_path, -1, root.stat().st_gid)
    artifact_path.chmod(0o640)

    return {
        "file_path": str(artifact_path),
        "file_name": _display_filename(session_name or session, period, normalized_format),
        "file_size": len(content),
        "file_md5": hashlib.md5(content).hexdigest(),
        "file_sha256": hashlib.sha256(content).hexdigest(),
        "message_count": message_count,
        "format": normalized_format,
    }


def _seconds(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative number")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative number") from exc
    if seconds < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return seconds


def _now_timestamp(now: datetime | float | int | None) -> float:
    if now is None:
        return datetime.now(UTC).timestamp()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("now datetime must be timezone-aware")
        return now.timestamp()
    return float(now)


def _is_owned_artifact(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return (
        len(relative.parts) == 4
        and relative.parts[0].startswith("tenant-")
        and relative.parts[1].startswith("session-")
        and relative.parts[2].startswith("request-")
        and all(_HASHED_DIRECTORY_RE.fullmatch(part) for part in relative.parts[:3])
        and (
            bool(_STAGED_FILE_RE.fullmatch(relative.name))
            or bool(_GENERIC_STAGED_FILE_RE.fullmatch(relative.name))
            or bool(_STAGED_FILE_TEMP_RE.fullmatch(relative.name))
            or bool(_GENERIC_STAGED_TEMP_RE.fullmatch(relative.name))
            or bool(_DISPLAY_NAME_FILE_RE.fullmatch(relative.name))
            or bool(_DISPLAY_NAME_TEMP_RE.fullmatch(relative.name))
        )
    )


def cleanup_message_exports(
    root_dir: str | os.PathLike[str],
    *,
    protected_paths: Collection[str | os.PathLike[str]] = (),
    retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    cleanup_grace_seconds: float = DEFAULT_CLEANUP_GRACE_SECONDS,
    now: datetime | float | int | None = None,
) -> dict[str, Any]:
    """Remove expired unprotected artifacts while preserving in-flight queue files."""

    retention = _seconds(retention_seconds, field="retention_seconds")
    cleanup_grace = _seconds(cleanup_grace_seconds, field="cleanup_grace_seconds")
    delete_after = retention + cleanup_grace
    now_timestamp = _now_timestamp(now)
    root = _root_path(root_dir, create=False)
    result: dict[str, Any] = {
        "removed_count": 0,
        "removed_bytes": 0,
        "retained_count": 0,
        "removed_paths": [],
        "errors": [],
    }
    if not root.exists():
        return result

    protected: set[Path] = set()
    for raw_path in protected_paths:
        path = Path(raw_path)
        if not path.is_absolute():
            raise InvalidMessageExportPath("protected artifact paths must be absolute")
        protected.add(_assert_under_root(path, root))
    protected_directories = {path.parent for path in protected}

    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = [
            name for name in directory_names if not (directory_path / name).is_symlink()
        ]
        for file_name in file_names:
            path = directory_path / file_name
            if not _is_owned_artifact(path, root):
                continue
            try:
                file_stat = path.lstat()
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                resolved_path = path.resolve(strict=False)
                if resolved_path in protected or (
                    path.name == "display-name.txt"
                    and resolved_path.parent in protected_directories
                ):
                    result["retained_count"] += 1
                    continue
                age_seconds = max(0.0, now_timestamp - file_stat.st_mtime)
                if age_seconds < delete_after:
                    result["retained_count"] += 1
                    continue
                path.unlink()
                result["removed_count"] += 1
                result["removed_bytes"] += file_stat.st_size
                result["removed_paths"].append(str(path))
            except OSError as exc:
                result["errors"].append({"file_path": str(path), "error": str(exc)})

    for directory, _, _ in os.walk(root, topdown=False, followlinks=False):
        directory_path = Path(directory)
        if directory_path == root or directory_path.is_symlink():
            continue
        try:
            relative = directory_path.relative_to(root)
            if relative.parts and all(
                _HASHED_DIRECTORY_RE.fullmatch(part) for part in relative.parts
            ):
                directory_path.rmdir()
        except OSError:
            pass
    return result
