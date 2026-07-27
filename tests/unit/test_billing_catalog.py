from __future__ import annotations

from app.billing.catalog import DRAW_QUALITY_COSTS
from plugins.credits import store as credits_store
from plugins.draw import store as draw_store


def test_draw_quality_cost_catalog_is_shared_without_plugin_to_plugin_import() -> None:
    assert DRAW_QUALITY_COSTS == {"low": 5, "medium": 10, "high": 20}
    assert credits_store.DRAW_QUALITY_COSTS is DRAW_QUALITY_COSTS
    assert draw_store.DRAW_QUALITY_COSTS is DRAW_QUALITY_COSTS
