from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COLUMN_CONTRACTS,
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_CONTRACT_NAME,
    RUNTIME_SCHEMA_INDEX_CONTRACTS,
    RUNTIME_SCHEMA_INDEXES,
    RUNTIME_SCHEMA_REVISION,
    RUNTIME_SCHEMA_TABLES,
    RuntimeSchemaError,
    verify_runtime_compatibility,
    verify_runtime_schema,
)

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_DDL = re.compile(
    r"\b(?:CREATE\s+(?:TABLE|INDEX)|ALTER\s+TABLE|DROP\s+(?:TABLE|INDEX))\b",
    re.IGNORECASE,
)


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class _Connection:
    dialect = SimpleNamespace(name="postgresql")

    def __init__(
        self,
        revision: str | tuple[str, ...],
        tables: set[str],
        indexes: set[str],
        compatibility_level: int | None,
        column_contracts: dict[tuple[str, str], bool],
        index_contracts: dict[str, tuple[str, tuple[str, ...], str]],
    ) -> None:
        self.revision = revision
        self.tables = tables
        self.indexes = indexes
        self.compatibility_level = compatibility_level
        self.column_contracts = column_contracts
        self.index_contracts = index_contracts
        self.sql: list[str] = []

    async def execute(self, statement, parameters=None) -> _Rows:
        sql = str(statement)
        self.sql.append(sql)
        if "alembic_version" in sql:
            revisions = (
                self.revision
                if isinstance(self.revision, tuple)
                else ((self.revision,) if self.revision else ())
            )
            return _Rows([(revision,) for revision in revisions])
        if "app_schema_contract" in sql:
            assert parameters == {
                "contract_name": RUNTIME_SCHEMA_CONTRACT_NAME,
            }
            return _Rows(
                [(str(self.compatibility_level),)]
                if self.compatibility_level is not None
                else []
            )
        if "runtime_required_columns" in sql:
            return _Rows(
                [
                    (
                        table_name,
                        column_name,
                        (table_name, column_name) in self.column_contracts,
                        self.column_contracts.get((table_name, column_name)),
                    )
                    for table_name, column_name, _nullable in RUNTIME_SCHEMA_COLUMN_CONTRACTS
                ]
            )
        if "runtime_required_indexes" in sql:
            rows: list[tuple[object, ...]] = []
            for index_name, _table_name, _columns, _predicate in RUNTIME_SCHEMA_INDEX_CONTRACTS:
                contract = self.index_contracts.get(index_name)
                rows.append(
                    (
                        index_name,
                        contract is not None,
                        contract[0] if contract is not None else None,
                        contract[1] if contract is not None else (),
                        contract[2] if contract is not None else None,
                    )
                )
            return _Rows(rows)
        if "pg_indexes" in sql:
            return _Rows([(index,) for index in sorted(self.indexes)])
        return _Rows([(table,) for table in sorted(self.tables)])


class _Connect:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _Engine:
    def __init__(
        self,
        revision: str | tuple[str, ...],
        tables: set[str],
        indexes: set[str] | None = None,
        compatibility_level: int | None = RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
        column_contracts: dict[tuple[str, str], bool] | None = None,
        index_contracts: dict[str, tuple[str, tuple[str, ...], str]] | None = None,
    ) -> None:
        self.connection = _Connection(
            revision,
            tables,
            set(RUNTIME_SCHEMA_INDEXES) if indexes is None else indexes,
            compatibility_level,
            (
                {
                    (table_name, column_name): nullable
                    for table_name, column_name, nullable in RUNTIME_SCHEMA_COLUMN_CONTRACTS
                }
                if column_contracts is None
                else column_contracts
            ),
            (
                {
                    index_name: (table_name, columns, predicate)
                    for index_name, table_name, columns, predicate
                    in RUNTIME_SCHEMA_INDEX_CONTRACTS
                }
                if index_contracts is None
                else index_contracts
            ),
        )

    def connect(self) -> _Connect:
        return _Connect(self.connection)


def _production_store_files() -> list[Path]:
    return [
        *sorted((ROOT / "app").rglob("*.py")),
        *sorted((ROOT / "plugins").rglob("*.py")),
        *[
            path
            for path in sorted((ROOT / "wxbot_client").rglob("*.py"))
            if path.name != "queue_migrations.py"
        ],
    ]


def _capture_sql_migration_statements(name: str) -> list[str]:
    path = ROOT / "migrations" / "versions" / name
    spec = importlib.util.spec_from_file_location(f"_test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements: list[str] = []
    module._execute = lambda *items: statements.extend(str(item) for item in items)
    module.upgrade()
    return statements


def test_production_store_sources_contain_no_schema_ddl() -> None:
    violations: list[str] = []
    for path in _production_store_files():
        for match in FORBIDDEN_DDL.finditer(path.read_text(encoding="utf-8")):
            line = path.read_text(encoding="utf-8")[: match.start()].count("\n") + 1
            violations.append(f"{path.relative_to(ROOT)}:{line}:{match.group(0)}")
    assert violations == []


def test_runtime_schema_migrations_create_every_owned_table() -> None:
    migration_paths = sorted((ROOT / "migrations" / "versions").glob("*.py"))
    migration_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in migration_paths
    )
    missing = [
        table
        for table in sorted(RUNTIME_SCHEMA_TABLES)
        if not (
            re.search(
                rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\b",
                migration_text,
                re.IGNORECASE,
            )
            or re.search(
                rf"op\.create_table\(\s*[\"']{re.escape(table)}[\"']",
                migration_text,
                re.IGNORECASE,
            )
        )
    ]
    assert missing == []
    missing_indexes = [
        index
        for index in sorted(RUNTIME_SCHEMA_INDEXES)
        if not (
            re.search(
                rf"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+"
                rf"{re.escape(index)}\b",
                migration_text,
                re.IGNORECASE,
            )
            or re.search(
                rf"op\.create_index\(\s*[\"']{re.escape(index)}[\"']",
                migration_text,
                re.IGNORECASE,
            )
        )
    ]
    assert missing_indexes == []
    assert 'revision = "0016_wxbot_schema"' in migration_text
    assert 'down_revision = "0015_runtime_plugin_schema"' in migration_text
    revision_values: list[str] = []
    down_revisions: set[str] = set()
    for path in migration_paths:
        assignments: dict[str, object] = {}
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value_node = node.value
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    assignments[target.id] = ast.literal_eval(value_node)

        revision = assignments.get("revision")
        assert isinstance(revision, str), f"{path.name} has no string revision"
        revision_values.append(revision)
        down_revision = assignments.get("down_revision")
        if isinstance(down_revision, str):
            down_revisions.add(down_revision)
        elif isinstance(down_revision, tuple):
            assert all(isinstance(parent, str) for parent in down_revision)
            down_revisions.update(down_revision)
        else:
            assert down_revision is None

    revisions = set(revision_values)
    assert len(revisions) == len(revision_values), "duplicate Alembic revision ids"
    assert revisions - down_revisions == {RUNTIME_SCHEMA_REVISION}


@pytest.mark.parametrize(
    "migration_name",
    [
        "20260718_0015_runtime_plugin_schema.py",
        "20260718_0016_wxbot_schema.py",
    ],
)
def test_fresh_schema_contains_every_compatibility_column(
    migration_name: str,
) -> None:
    statements = _capture_sql_migration_statements(migration_name)
    create_bodies: dict[str, str] = {}
    for statement in statements:
        match = re.match(
            r"\s*CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*)\)\s*$",
            statement,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            create_bodies[match.group(1)] = match.group(2)

    missing: list[str] = []
    for statement in statements:
        table_match = re.match(
            r"\s*ALTER\s+TABLE\s+(\w+)",
            statement,
            re.IGNORECASE,
        )
        if not table_match:
            continue
        table = table_match.group(1)
        for column in re.findall(
            r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)",
            statement,
            re.IGNORECASE,
        ):
            if not re.search(
                rf"\b{re.escape(column)}\b",
                create_bodies.get(table, ""),
                re.IGNORECASE,
            ):
                missing.append(f"{table}.{column}")
    assert missing == []


def test_group_session_identifiers_are_widened_consistently() -> None:
    tables = {
        "plugin_credits_config",
        "plugin_credits_balance",
        "plugin_credits_ledger",
        "plugin_credits_checkin",
        "plugin_credits_reservation",
        "plugin_moderation_config",
        "plugin_moderation_keywords",
        "plugin_moderation_events",
        "plugin_persona_jobs",
        "plugin_persona_profiles",
    }
    statements = _capture_sql_migration_statements(
        "20260718_0015_runtime_plugin_schema.py"
    )
    migration_sql = "\n".join(statements)

    for table in sorted(tables):
        create_match = re.search(
            rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*"
            r"\((.*?)\)\s*(?:ALTER|CREATE|INSERT|DELETE|DO|$)",
            migration_sql,
            re.IGNORECASE | re.DOTALL,
        )
        assert create_match is not None
        assert re.search(
            r"\bsession_id\s+VARCHAR\(256\)\s+NOT\s+NULL\b",
            create_match.group(1),
            re.IGNORECASE,
        )
        assert any(
            re.search(
                rf"ALTER\s+TABLE\s+{re.escape(table)}\s+"
                r"ALTER\s+COLUMN\s+session_id\s+TYPE\s+VARCHAR\(256\)",
                statement,
                re.IGNORECASE,
            )
            for statement in statements
        )


@pytest.mark.asyncio
async def test_runtime_compatibility_verification_is_bounded_and_read_only() -> None:
    engine = _Engine(RUNTIME_SCHEMA_REVISION, set())

    await verify_runtime_compatibility(engine, component="worker readiness")  # type: ignore[arg-type]

    assert len(engine.connection.sql) == 2
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in engine.connection.sql)


@pytest.mark.asyncio
async def test_runtime_compatibility_verification_rejects_multiple_heads() -> None:
    engine = _Engine(("head-a", "head-b"), set())

    with pytest.raises(RuntimeSchemaError, match="requires one Alembic revision"):
        await verify_runtime_compatibility(engine, component="worker readiness")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_compatibility_verification_rejects_contract_drift() -> None:
    engine = _Engine(
        RUNTIME_SCHEMA_REVISION,
        set(),
        compatibility_level=RUNTIME_SCHEMA_COMPATIBILITY_LEVEL + 1,
    )

    with pytest.raises(RuntimeSchemaError, match="compatibility level"):
        await verify_runtime_compatibility(engine, component="worker readiness")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_schema_verification_is_read_only() -> None:
    engine = _Engine(RUNTIME_SCHEMA_REVISION, set(RUNTIME_SCHEMA_TABLES))

    await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]

    assert len(engine.connection.sql) == 6
    assert all(
        statement.lstrip().upper().startswith(("SELECT", "WITH", "PRAGMA"))
        for statement in engine.connection.sql
    )


@pytest.mark.asyncio
async def test_runtime_schema_verification_accepts_newer_compatible_revision() -> None:
    engine = _Engine("0018_future_compatible", set(RUNTIME_SCHEMA_TABLES))

    await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_schema_verification_rejects_missing_contract() -> None:
    engine = _Engine(
        "0015_runtime_plugin_schema",
        set(RUNTIME_SCHEMA_TABLES),
        compatibility_level=None,
    )

    with pytest.raises(RuntimeSchemaError, match="alembic upgrade head"):
        await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_schema_verification_rejects_breaking_contract() -> None:
    engine = _Engine(
        "0018_breaking",
        set(RUNTIME_SCHEMA_TABLES),
        compatibility_level=RUNTIME_SCHEMA_COMPATIBILITY_LEVEL + 1,
    )

    with pytest.raises(RuntimeSchemaError, match="compatibility level"):
        await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_schema_verification_rejects_missing_table() -> None:
    tables = set(RUNTIME_SCHEMA_TABLES) - {"plugin_wxbot_reply_queue"}
    engine = _Engine(RUNTIME_SCHEMA_REVISION, tables)

    with pytest.raises(RuntimeSchemaError, match="plugin_wxbot_reply_queue"):
        await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_schema_verification_rejects_missing_index() -> None:
    indexes = set(RUNTIME_SCHEMA_INDEXES) - {"idx_wxbot_reply_queue_due"}
    engine = _Engine(RUNTIME_SCHEMA_REVISION, set(RUNTIME_SCHEMA_TABLES), indexes)

    with pytest.raises(RuntimeSchemaError, match="idx_wxbot_reply_queue_due"):
        await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]


def test_runtime_schema_contract_includes_global_lifecycle_index() -> None:
    assert "ix_plugin_lifecycle_in_progress_created" in RUNTIME_SCHEMA_INDEXES


@pytest.mark.asyncio
async def test_runtime_schema_verification_rejects_missing_critical_column() -> None:
    columns = {
        (table_name, column_name): nullable
        for table_name, column_name, nullable in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    }
    columns.pop(("message_effect_intent", "producer_owner"))
    engine = _Engine(
        RUNTIME_SCHEMA_REVISION,
        set(RUNTIME_SCHEMA_TABLES),
        column_contracts=columns,
    )

    with pytest.raises(
        RuntimeSchemaError,
        match=r"message_effect_intent\.producer_owner missing",
    ):
        await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_schema_verification_rejects_nullable_fencing_column() -> None:
    columns = {
        (table_name, column_name): nullable
        for table_name, column_name, nullable in RUNTIME_SCHEMA_COLUMN_CONTRACTS
    }
    columns[("plugin_wxbot_report_jobs", "run_attempt")] = True
    engine = _Engine(
        RUNTIME_SCHEMA_REVISION,
        set(RUNTIME_SCHEMA_TABLES),
        column_contracts=columns,
    )

    with pytest.raises(RuntimeSchemaError, match="run_attempt nullable=true expected=false"):
        await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("columns", "predicate"),
    [
        (("created_at", "updated_at"), "status='in_progress'"),
        (("created_at",), "status='completed'"),
    ],
)
async def test_runtime_schema_verification_rejects_wrong_critical_index_definition(
    columns: tuple[str, ...],
    predicate: str,
) -> None:
    contracts = {
        index_name: (table_name, expected_columns, expected_predicate)
        for index_name, table_name, expected_columns, expected_predicate
        in RUNTIME_SCHEMA_INDEX_CONTRACTS
    }
    contracts["ix_plugin_lifecycle_in_progress_created"] = (
        "plugin_lifecycle_operation",
        columns,
        predicate,
    )
    engine = _Engine(
        RUNTIME_SCHEMA_REVISION,
        set(RUNTIME_SCHEMA_TABLES),
        index_contracts=contracts,
    )

    with pytest.raises(
        RuntimeSchemaError,
        match="ix_plugin_lifecycle_in_progress_created",
    ):
        await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_runtime_schema_verification_accepts_postgres_rendered_predicate_casts() -> None:
    contracts = {
        index_name: (table_name, columns, predicate)
        for index_name, table_name, columns, predicate in RUNTIME_SCHEMA_INDEX_CONTRACTS
    }
    contracts["idx_wxbot_report_jobs_running_lease"] = (
        "plugin_wxbot_report_jobs",
        ("updated_at",),
        "((status)::text = 'running'::text)",
    )
    engine = _Engine(
        RUNTIME_SCHEMA_REVISION,
        set(RUNTIME_SCHEMA_TABLES),
        index_contracts=contracts,
    )

    await verify_runtime_schema(engine, component="test")  # type: ignore[arg-type]
