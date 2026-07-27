from __future__ import annotations

import time
from datetime import UTC, datetime


class Clock:
    """Injectable clock for testability."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def unix(self) -> float:
        return time.time()

    def unix_ms(self) -> int:
        return int(time.time() * 1000)


DEFAULT_CLOCK = Clock()
