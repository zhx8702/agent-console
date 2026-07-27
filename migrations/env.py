from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.common.config import get_settings
from app.models import Base
from migrations.version_table import ALEMBIC_VERSION_TABLE, ensure_alembic_version_table

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override URL from settings so tests / env switching works consistently.
config.set_main_option("sqlalchemy.url", get_settings().db_dsn)

target_metadata = Base.metadata

_POSTGRES_MIGRATION_LOCK_NAME = "agent-console:alembic-migration"
_POSTGRES_LOCK_TIMEOUT = "30s"
_POSTGRES_STATEMENT_TIMEOUT = "15min"


def _acquire_postgres_migration_lock(connection: Connection) -> None:
    """Serialize schema changes and bound waits on live database locks."""

    connection.execute(
        text(f"SET lock_timeout = '{_POSTGRES_LOCK_TIMEOUT}'")
    )
    connection.execute(
        text(f"SET statement_timeout = '{_POSTGRES_STATEMENT_TIMEOUT}'")
    )
    connection.execute(
        text(
            "SELECT pg_advisory_lock("
            "hashtextextended(CAST(:lock_name AS TEXT), 0))"
        ),
        {"lock_name": _POSTGRES_MIGRATION_LOCK_NAME},
    )


def _release_postgres_migration_lock(connection: Connection) -> None:
    connection.execute(
        text(
            "SELECT pg_advisory_unlock("
            "hashtextextended(CAST(:lock_name AS TEXT), 0))"
        ),
        {"lock_name": _POSTGRES_MIGRATION_LOCK_NAME},
    )


def do_run_migrations(connection: Connection) -> None:
    postgres = connection.dialect.name == "postgresql"
    lock_acquired = False
    try:
        if postgres:
            _acquire_postgres_migration_lock(connection)
            lock_acquired = True
            # The advisory lock is session scoped and survives this commit;
            # SET timeouts also remain active for the migration transaction.
            connection.commit()

        ensure_alembic_version_table(connection)
        if connection.in_transaction():
            connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table=ALEMBIC_VERSION_TABLE,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        # A timed-out DDL statement leaves the transaction aborted.  Roll it
        # back before releasing the session-level lock on the same connection.
        if connection.in_transaction():
            connection.rollback()
        if lock_acquired:
            _release_postgres_migration_lock(connection)
            connection.commit()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
