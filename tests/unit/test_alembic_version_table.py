from __future__ import annotations

from sqlalchemy import String, create_engine, inspect

from migrations.version_table import (
    ALEMBIC_VERSION_NUM_LENGTH,
    ALEMBIC_VERSION_TABLE,
    ensure_alembic_version_table,
    version_num_needs_widening,
)


def test_version_num_needs_widening_for_short_varchar() -> None:
    assert version_num_needs_widening(String(32)) is True
    assert version_num_needs_widening(String(ALEMBIC_VERSION_NUM_LENGTH)) is False
    assert version_num_needs_widening(String(255)) is False
    assert version_num_needs_widening(String()) is False


def test_ensure_alembic_version_table_creates_sqlite_table_with_wide_version_num() -> None:
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as connection:
        ensure_alembic_version_table(connection)
        ensure_alembic_version_table(connection)

        columns = {
            column["name"]: column
            for column in inspect(connection).get_columns(ALEMBIC_VERSION_TABLE)
        }

    assert columns["version_num"]["type"].length == ALEMBIC_VERSION_NUM_LENGTH
