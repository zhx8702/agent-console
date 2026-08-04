"""Safe, deterministic staging and conversion for wxbot file artifacts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from plugins.wxbot.message_exports import (
    MessageExportConflict,
    MessageExportTooLarge,
    _assert_under_root,
    _ensure_private_directory,
    _identity_directory,
    _publish_immutable,
    _root_path,
    _safe_filename_component,
)

SUPPORTED_FILE_FORMATS = ("txt", "md", "csv", "json")
_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,8}$")
_MIME_TYPES = {
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json; charset=utf-8",
}


def _csv_cell(value: Any) -> Any:
    """Prevent spreadsheet formula execution without changing numeric cells."""

    if not isinstance(value, str):
        return value
    return f"'{value}" if value[:1] in {"=", "+", "-", "@"} else value


class FileArtifactError(RuntimeError):
    """Base error for generated file artifacts."""


class FileArtifactConflict(FileArtifactError, MessageExportConflict):
    """The request id already points at different immutable bytes."""


class FileArtifactTooLarge(FileArtifactError, MessageExportTooLarge):
    """The generated artifact exceeds the configured limit."""


def normalize_file_format(value: object, *, default: str = "txt") -> str:
    normalized = str(value or default).strip().lower().lstrip(".")
    if normalized == "markdown":
        normalized = "md"
    if normalized not in SUPPORTED_FILE_FORMATS:
        raise ValueError(
            f"file format must be one of {', '.join(SUPPORTED_FILE_FORMATS)}"
        )
    return normalized


def _decode_text(content: bytes) -> str:
    if not content:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _source_format(file_name: str, content_type: str = "") -> str:
    extension = Path(str(file_name or "")).suffix.lower().lstrip(".")
    if extension in SUPPORTED_FILE_FORMATS:
        return extension
    media_type = str(content_type or "").split(";", 1)[0].strip().lower()
    return {
        "text/plain": "txt",
        "text/markdown": "md",
        "text/csv": "csv",
        "application/json": "json",
    }.get(media_type, "")


def infer_file_format(file_name: str, content_type: str = "") -> str:
    """Infer one of the bounded text formats from a name or safe MIME type."""

    return _source_format(file_name, content_type)


def _parse_source(content: bytes, *, file_name: str, content_type: str = "") -> Any:
    source_format = _source_format(file_name, content_type)
    text = _decode_text(content).replace("\r\n", "\n").replace("\r", "\n")
    if source_format == "json":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if source_format == "csv":
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error:
            return text
        if rows and len(rows) > 1:
            header = rows[0]
            if header and len(set(header)) == len(header):
                return [dict(zip(header, row, strict=False)) for row in rows[1:]]
        return rows
    return text


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def convert_file_bytes(
    content: bytes,
    *,
    source_name: str,
    target_format: str,
    source_content_type: str = "",
) -> bytes:
    """Convert bounded text/CSV/JSON content using only the standard library."""

    target = normalize_file_format(target_format)
    parsed = _parse_source(content, file_name=source_name, content_type=source_content_type)
    if target == "json":
        if isinstance(parsed, str):
            value: Any = {"text": parsed}
        else:
            value = parsed
        return (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode(
            "utf-8"
        )
    if target == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        if isinstance(parsed, list) and parsed and all(isinstance(item, dict) for item in parsed):
            keys: list[str] = []
            for item in parsed:
                for key in item:
                    if str(key) not in keys:
                        keys.append(str(key))
            writer.writerow([_csv_cell(key) for key in keys])
            for item in parsed:
                writer.writerow([_csv_cell(item.get(key, "")) for key in keys])
        elif isinstance(parsed, list) and all(isinstance(item, list) for item in parsed):
            writer.writerows([[_csv_cell(value) for value in row] for row in parsed])
        else:
            writer.writerow(["content"])
            writer.writerow([_csv_cell(_as_text(parsed))])
        return output.getvalue().encode("utf-8-sig")
    if target == "md":
        if isinstance(parsed, list) and parsed and all(isinstance(item, dict) for item in parsed):
            keys: list[str] = []
            for item in parsed:
                for key in item:
                    if str(key) not in keys:
                        keys.append(str(key))
            lines = [
                "| " + " | ".join(keys) + " |",
                "| " + " | ".join("---" for _ in keys) + " |",
            ]
            lines.extend(
                "| " + " | ".join(str(item.get(key, "")).replace("|", "\\|") for key in keys) + " |"
                for item in parsed
            )
            return ("\n".join(lines) + "\n").encode("utf-8")
        return ("# 文件内容\n\n" + _as_text(parsed).strip() + "\n").encode("utf-8")
    if isinstance(parsed, str):
        return parsed.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return (_as_text(parsed).rstrip() + "\n").encode("utf-8")


def stage_outbound_artifact(
    root_dir: str | os.PathLike[str],
    *,
    tenant_id: str,
    session_id: str,
    request_id: str,
    file_name: str,
    content: bytes,
    file_format: str,
    max_bytes: int,
    reuse_existing_on_conflict: bool = False,
) -> dict[str, Any]:
    """Atomically publish a private, idempotent artifact under the outbox."""

    if not isinstance(content, bytes):
        raise TypeError("artifact content must be bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if len(content) > max_bytes:
        raise FileArtifactTooLarge(
            f"file artifact is {len(content)} bytes; limit is {max_bytes} bytes"
        )
    normalized_format = normalize_file_format(file_format)
    tenant = str(tenant_id or "").strip()
    session = str(session_id or "").strip()
    request = str(request_id or "").strip()
    if not tenant or not session or not request:
        raise ValueError("tenant_id, session_id and request_id must not be empty")
    root = _root_path(root_dir, create=True)
    directory = root / _identity_directory("tenant", tenant) / _identity_directory(
        "session", session
    ) / _identity_directory("request", request)
    _ensure_private_directory(directory, root)
    safe_name = _safe_filename_component(file_name, fallback="artifact")
    stem = Path(safe_name).stem or "artifact"
    extension = normalized_format
    if reuse_existing_on_conflict:
        name_path = _assert_under_root(directory / "display-name.txt", root)
        try:
            _publish_immutable(name_path, stem.encode("utf-8"))
        except MessageExportConflict as exc:
            if name_path.is_symlink() or not name_path.is_file():
                raise FileArtifactConflict(str(exc)) from exc
            name_size = name_path.stat().st_size
            if name_size <= 0 or name_size > 256:
                raise FileArtifactConflict("stored artifact name is invalid") from exc
            try:
                stored_name = name_path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as decode_exc:
                raise FileArtifactConflict("stored artifact name is invalid") from decode_exc
            if _safe_filename_component(stored_name, fallback="文件") != stored_name:
                raise FileArtifactConflict("stored artifact name is invalid") from exc
            stem = stored_name
    artifact_path = _assert_under_root(directory / f"artifact.{extension}", root)
    artifact_content = content
    try:
        _publish_immutable(artifact_path, content)
    except MessageExportConflict as exc:
        if (
            not reuse_existing_on_conflict
            or artifact_path.is_symlink()
            or not artifact_path.is_file()
        ):
            raise FileArtifactConflict(str(exc)) from exc
        # A source-message retry may produce slightly different model text.
        # Reuse the first immutable artifact so the effect payload remains
        # byte-for-byte stable under the same delivery idempotency key.
        artifact_size = artifact_path.stat().st_size
        if artifact_size > max_bytes:
            raise FileArtifactTooLarge(
                f"file artifact is {artifact_size} bytes; limit is {max_bytes} bytes"
            ) from exc
        artifact_content = artifact_path.read_bytes()
        if len(artifact_content) != artifact_size:
            raise FileArtifactConflict("existing artifact changed while being read") from exc
    if os.name == "posix" and artifact_path.stat().st_gid != root.stat().st_gid:
        os.chown(artifact_path, -1, root.stat().st_gid)
    artifact_path.chmod(0o640)
    display_name = f"{_safe_filename_component(stem, fallback='文件')}.{extension}"
    return {
        "file_path": str(artifact_path),
        "file_name": display_name,
        "file_size": len(artifact_content),
        "file_md5": hashlib.md5(artifact_content).hexdigest(),
        "file_sha256": hashlib.sha256(artifact_content).hexdigest(),
        "format": normalized_format,
        "mime": _MIME_TYPES.get(normalized_format)
        or mimetypes.guess_type(display_name)[0]
        or "application/octet-stream",
    }


__all__ = [
    "SUPPORTED_FILE_FORMATS",
    "FileArtifactConflict",
    "FileArtifactError",
    "FileArtifactTooLarge",
    "convert_file_bytes",
    "infer_file_format",
    "normalize_file_format",
    "stage_outbound_artifact",
]
