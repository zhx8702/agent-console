from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

from alembic import context as alembic_context


class _Config:
    config_file_name = None
    config_ini_section = "alembic"

    def __init__(self) -> None:
        self.options = {"sqlalchemy.url": "postgresql+asyncpg://test"}

    def set_main_option(self, key: str, value: str) -> None:
        self.options[key] = value

    def get_main_option(self, key: str) -> str:
        return self.options[key]

    def get_section(self, _name: str) -> dict[str, str]:
        return dict(self.options)


def _load_offline_environment(monkeypatch) -> tuple[ModuleType, list[str]]:
    events: list[str] = []

    @contextmanager
    def begin_transaction():
        events.append("begin")
        yield

    monkeypatch.setattr(alembic_context, "config", _Config(), raising=False)
    monkeypatch.setattr(alembic_context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(
        alembic_context,
        "configure",
        lambda **_kwargs: events.append("configure"),
    )
    monkeypatch.setattr(alembic_context, "begin_transaction", begin_transaction)
    monkeypatch.setattr(
        alembic_context,
        "run_migrations",
        lambda: events.append("migrate"),
    )

    path = Path(__file__).resolve().parents[2] / "migrations" / "env.py"
    spec = importlib.util.spec_from_file_location("_migration_environment_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, events


def test_offline_environment_renders_without_opening_online_lock(monkeypatch) -> None:
    module, events = _load_offline_environment(monkeypatch)

    assert events == ["configure", "begin", "migrate"]
    assert callable(module.do_run_migrations)


def test_online_postgres_environment_serializes_and_bounds_migration(
    monkeypatch,
) -> None:
    module, events = _load_offline_environment(monkeypatch)
    events.clear()

    class _Connection:
        dialect = SimpleNamespace(name="postgresql")

        def __init__(self) -> None:
            self.transaction = False
            self.calls: list[tuple[str, dict[str, object] | None]] = []

        def execute(self, statement, parameters=None):
            self.calls.append((str(statement), parameters))
            self.transaction = True
            return None

        def in_transaction(self) -> bool:
            return self.transaction

        def commit(self) -> None:
            events.append("commit")
            self.transaction = False

        def rollback(self) -> None:
            events.append("rollback")
            self.transaction = False

    connection = _Connection()
    monkeypatch.setattr(
        module,
        "ensure_alembic_version_table",
        lambda _connection: events.append("ensure_version_table"),
    )
    module.do_run_migrations(connection)

    sql = [statement for statement, _parameters in connection.calls]
    assert sql[0] == "SET lock_timeout = '30s'"
    assert sql[1] == "SET statement_timeout = '15min'"
    assert "pg_advisory_lock" in sql[2]
    assert "pg_advisory_unlock" in sql[-1]
    assert connection.calls[2][1] == {
        "lock_name": "agent-console:alembic-migration"
    }
    assert events == [
        "commit",
        "ensure_version_table",
        "configure",
        "begin",
        "migrate",
        "commit",
    ]
