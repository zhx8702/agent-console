"""Safe helpers for development-only runtime environment overrides."""

from __future__ import annotations

import importlib
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_ENV_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def runtime_config_writes_allowed(settings: Any) -> bool:
    """Allow mutable local configuration only in explicit dev/test modes."""

    return str(getattr(settings, "app_env", "") or "").strip().lower() in {
        "dev",
        "test",
    }


def runtime_env_file_path(settings: Any) -> str:
    """Resolve the mutable env file, honoring the runtime override."""

    configured = str(os.getenv("AGENT_CONSOLE_ENV_FILE") or "").strip()
    if not configured:
        model_config = getattr(settings.__class__, "model_config", {})
        configured = str(model_config.get("env_file") or ".env")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(settings.project_root) / path
    return str(path)


def serialize_env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value or "")
    if text == "":
        return '""'
    if any(ch.isspace() for ch in text) or "#" in text or '"' in text:
        return json.dumps(text, ensure_ascii=False)
    return text


def write_env_overrides_atomic(
    env_path: str,
    values: Mapping[str, Any],
    *,
    process_env_values: Mapping[str, Any] | None = None,
) -> None:
    """Merge selected keys under a cross-thread/process lock and atomically replace.

    Only keys present in ``values`` are changed, so independent concurrent
    updates cannot restore stale values for unrelated settings.
    """

    updates = {str(key): value for key, value in values.items()}
    if not updates:
        return

    path = Path(env_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock(path)
    lock_path = path.with_name(f".{path.name}.lock")
    with thread_lock, _process_file_lock(lock_path):
        try:
            with path.open(encoding="utf-8") as stream:
                lines = stream.readlines()
        except FileNotFoundError:
            lines = []

        rendered = _merge_env_lines(lines, updates)
        _atomic_replace_text(path, "".join(rendered))
        for key, value in (process_env_values or {}).items():
            os.environ[str(key)] = str(value if value is not None else "")


def _merge_env_lines(lines: list[str], values: Mapping[str, Any]) -> list[str]:
    remaining = dict(values)
    updated_keys: set[str] = set()
    updated_lines: list[str] = []
    for raw_line in lines:
        match = _ENV_ASSIGNMENT_RE.match(raw_line)
        if not match:
            updated_lines.append(raw_line)
            continue
        key = match.group(1)
        if key not in values:
            updated_lines.append(raw_line)
            continue
        if key in updated_keys:
            continue
        updated_lines.append(f"{key}={serialize_env_value(values[key])}\n")
        updated_keys.add(key)
        remaining.pop(key, None)

    if remaining and updated_lines and not updated_lines[-1].endswith(("\n", "\r")):
        updated_lines[-1] += "\n"
    for key, value in remaining.items():
        updated_lines.append(f"{key}={serialize_env_value(value)}\n")
    return updated_lines


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


@contextmanager
def _process_file_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as stream:
        if os.name == "nt":
            module = importlib.import_module("msvcrt")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            module.locking(stream.fileno(), module.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                module.locking(stream.fileno(), module.LK_UNLCK, 1)
            return

        module = importlib.import_module("fcntl")
        module.flock(stream.fileno(), module.LOCK_EX)
        try:
            yield
        finally:
            module.flock(stream.fileno(), module.LOCK_UN)


def _atomic_replace_text(path: Path, content: str) -> None:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = None
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
