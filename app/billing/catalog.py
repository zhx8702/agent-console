"""Shared billing catalog values for cross-plugin capabilities.

Capability providers and billing providers may both need the same resource
vocabulary.  Keeping that vocabulary in the core billing boundary avoids one
plugin importing another plugin's persistence module just to read constants.
"""

from __future__ import annotations

from typing import Final

DRAW_QUALITY_COSTS: Final[dict[str, int]] = {
    "low": 5,
    "medium": 10,
    "high": 20,
}
