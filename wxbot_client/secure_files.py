"""Crash-safe local persistence for wxbot credentials and configuration."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from urllib.parse import unquote


def atomic_write_private_text(
    path: str | os.PathLike[str],
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace ``path`` and restrict newly-created files to their owner."""

    target = Path(path).expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            # Windows may not expose POSIX mode bits; the file still inherits
            # the current user's directory ACL and is never created in /tmp.
            pass
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def resolve_relative_file(
    base_directory: str | os.PathLike[str],
    untrusted_path: str,
) -> Path | None:
    """Resolve an existing relative file without traversal or symlink escapes."""

    raw = unquote(str(untrusted_path or "")).strip().replace("\\", os.sep)
    if not raw or "\0" in raw:
        return None
    relative = Path(raw)
    if relative.is_absolute() or relative.drive:
        return None
    base = Path(base_directory).resolve(strict=False)
    candidate = (base / relative).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None
