from __future__ import annotations

from sqlalchemy import Column, MetaData, PrimaryKeyConstraint, String, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.sql.type_api import TypeEngine

ALEMBIC_VERSION_NUM_LENGTH = 128
ALEMBIC_VERSION_TABLE = "alembic_version"
ALEMBIC_VERSION_TABLE_SCHEMA: str | None = None


def ensure_alembic_version_table(connection: Connection) -> None:
    """Ensure Alembic can store local revision identifiers longer than 32 chars."""
    table = Table(
        ALEMBIC_VERSION_TABLE,
        MetaData(),
        Column("version_num", String(ALEMBIC_VERSION_NUM_LENGTH), nullable=False),
        PrimaryKeyConstraint("version_num", name=f"{ALEMBIC_VERSION_TABLE}_pkc"),
        schema=ALEMBIC_VERSION_TABLE_SCHEMA,
    )
    table.create(connection, checkfirst=True)

    if connection.dialect.name != "postgresql":
        return

    inspector = inspect(connection)
    version_num_column = next(
        (
            column
            for column in inspector.get_columns(
                ALEMBIC_VERSION_TABLE,
                schema=ALEMBIC_VERSION_TABLE_SCHEMA,
            )
            if column["name"] == "version_num"
        ),
        None,
    )
    if version_num_column is None or not version_num_needs_widening(version_num_column["type"]):
        return

    preparer = connection.dialect.identifier_preparer
    qualified_table = preparer.format_table(table)
    version_num = preparer.quote("version_num")
    connection.execute(
        text(
            f"ALTER TABLE {qualified_table} "
            f"ALTER COLUMN {version_num} TYPE VARCHAR({ALEMBIC_VERSION_NUM_LENGTH})"
        )
    )


def version_num_needs_widening(column_type: TypeEngine) -> bool:
    length = getattr(column_type, "length", None)
    return length is not None and length < ALEMBIC_VERSION_NUM_LENGTH
