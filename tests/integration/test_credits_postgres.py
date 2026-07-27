from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.common.config import get_settings
from app.infra.db import get_engine
from plugins.credits.store import CreditStore
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]


@pytest.mark.asyncio
async def test_postgres_balance_initializers_use_unambiguous_config_binds(
    _reset_singletons,
) -> None:
    """Existing and new balances must survive asyncpg parameter inference."""

    scope = uuid4().hex
    tenant_id = f"credit-pg-{scope[:12]}"
    session_id = f"room-{scope}@chatroom"
    existing_user = f"existing-{scope}"
    adjusted_user = f"adjusted-{scope}"
    recipient_user = f"recipient-{scope}"
    engine = get_engine()
    store = CreditStore(get_settings())

    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO plugin_credits_config "
                    "(tenant_id, session_id, enabled, cost_per_chat, initial_credits) "
                    "VALUES (:tid, :sid, TRUE, 5, 20)"
                ),
                {"tid": tenant_id, "sid": session_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO plugin_credits_balance "
                    "(tenant_id, session_id, user_id, display_name, credits) "
                    "VALUES (:tid, :sid, :uid, 'Existing', 870)"
                ),
                {"tid": tenant_id, "sid": session_id, "uid": existing_user},
            )

        reservation = await store.reserve_charge(
            tenant_id,
            session_id,
            existing_user,
            5,
            reason="chat",
            display_name="Existing",
            idempotency_key=f"reserve-{scope}",
        )
        assert reservation["balance"] == 865
        captured = await store.capture_reservation(
            reservation["reservation_id"],
            reference="postgres-regression",
            display_name="Existing",
        )
        assert captured is not None
        assert captured["status"] == "captured"

        adjusted = await store.adjust(
            tenant_id,
            session_id,
            adjusted_user,
            5,
            "postgres-regression",
            display_name="Adjusted",
            idempotency_key=f"adjust-{scope}",
        )
        assert adjusted == 25

        transferred = await store.transfer(
            tenant_id,
            session_id,
            adjusted_user,
            recipient_user,
            5,
            actor="postgres-regression",
            idempotency_key=f"transfer-{scope}",
        )
        assert transferred == {
            "from_balance": 20,
            "to_balance": 25,
        }
    finally:
        async with engine.begin() as conn:
            cleanup = {"tid": tenant_id, "sid": session_id}
            await conn.execute(
                text(
                    "DELETE FROM plugin_credits_reservation "
                    "WHERE tenant_id = :tid AND session_id = :sid"
                ),
                cleanup,
            )
            await conn.execute(
                text(
                    "DELETE FROM plugin_credits_ledger "
                    "WHERE tenant_id = :tid AND session_id = :sid"
                ),
                cleanup,
            )
            await conn.execute(
                text(
                    "DELETE FROM plugin_credits_checkin "
                    "WHERE tenant_id = :tid AND session_id = :sid"
                ),
                cleanup,
            )
            await conn.execute(
                text(
                    "DELETE FROM plugin_credits_balance "
                    "WHERE tenant_id = :tid AND session_id = :sid"
                ),
                cleanup,
            )
            await conn.execute(
                text(
                    "DELETE FROM plugin_credits_config "
                    "WHERE tenant_id = :tid AND session_id = :sid"
                ),
                cleanup,
            )
