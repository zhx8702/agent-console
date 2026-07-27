"""Safety keyword list loader."""
from __future__ import annotations

from pathlib import Path

from app.common.config import get_settings
from app.common.exceptions import ConfigError


def load_keywords(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.is_absolute():
        p = get_settings().project_root / p
    if not p.exists():
        raise ConfigError(f"safety keywords file not found: {p}")

    keywords: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        keywords.append(s)
    return keywords
