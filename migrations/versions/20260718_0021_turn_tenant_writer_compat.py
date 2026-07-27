"""fence legacy turn writers behind tenant-scoped session ownership

Revision ID: 0021_turn_tenant_writer_compat
Revises: 0020_message_effect_intent
Create Date: 2026-07-18

This forward compatibility trigger runs after the tenant composite-key
contract.  A one-release legacy writer that omits ``turns.tenant_id`` may
continue only while its ``session_id`` maps to exactly one tenant.  Ambiguous,
missing, or explicitly mismatched ownership fails closed.

It does not retroactively change the historical 0011 -> 0014 rolling window;
operators must still drain pre-tenant writers before that contract step, as
documented in ``docs/session-tenant-online-migration.md``.
"""
from __future__ import annotations

from alembic import op

revision = "0021_turn_tenant_writer_compat"
down_revision = "0020_message_effect_intent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION cs_normalize_turn_tenant_scope()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            inferred_tenant_id VARCHAR(64);
            matching_sessions BIGINT;
        BEGIN
            IF NEW.tenant_id IS NULL OR btrim(NEW.tenant_id) = '' THEN
                SELECT min(session_row.tenant_id), count(*)
                INTO inferred_tenant_id, matching_sessions
                FROM sessions AS session_row
                WHERE session_row.session_id = NEW.session_id;

                IF matching_sessions = 0 THEN
                    RAISE EXCEPTION
                        'turn session does not exist for legacy tenant inference'
                        USING ERRCODE = '23503';
                END IF;
                IF matching_sessions > 1 THEN
                    RAISE EXCEPTION
                        'turn session is ambiguous across tenants; tenant_id is required'
                        USING ERRCODE = '23514';
                END IF;

                NEW.tenant_id := inferred_tenant_id;
                RAISE LOG 'legacy turn writer inferred tenant scope';
            ELSIF NOT EXISTS (
                SELECT 1
                FROM sessions AS session_row
                WHERE session_row.tenant_id = NEW.tenant_id
                  AND session_row.session_id = NEW.session_id
            ) THEN
                RAISE EXCEPTION
                    'turn tenant/session ownership mismatch'
                    USING ERRCODE = '23503';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_turns_tenant_writer_compat ON turns
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_turns_tenant_writer_compat
        BEFORE INSERT OR UPDATE ON turns
        FOR EACH ROW
        EXECUTE FUNCTION cs_normalize_turn_tenant_scope()
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_turns_tenant_writer_compat ON turns
        """
    )
    op.execute("DROP FUNCTION IF EXISTS cs_normalize_turn_tenant_scope()")
