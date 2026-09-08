import asyncio
from contextlib import asynccontextmanager

import pytest
from alembic.script import ScriptDirectory

from app.db import migrate
from app.db.schema_contract import migration_config, validate_runtime_schema


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def scalars(self):
        return self.value


class Database:
    def __init__(self, versions, elevated=False, failure=False):
        self.versions, self.elevated, self.failure = versions, elevated, failure
        self.statements = []

    @asynccontextmanager
    async def session(self):
        yield self

    async def execute(self, statement):
        statement = str(statement)
        self.statements.append(statement)
        assert statement.strip().startswith("SELECT")
        if self.failure:
            raise RuntimeError("private-password-and-database-location")
        return Result(self.elevated if "FROM pg_roles" in statement else self.versions)


def test_runtime_accepts_only_shipped_revision_without_ddl(monkeypatch):
    monkeypatch.delenv("HUB_MIGRATION_DATABASE_URL", raising=False)
    db = Database(ScriptDirectory.from_config(migration_config()).get_heads())
    asyncio.run(validate_runtime_schema(db))
    assert len(db.statements) == 2


@pytest.mark.parametrize("versions,elevated,failure", [([], False, False), (["old"], False, False), (["future"], False, False), ([], True, False), ([], False, True)])
def test_runtime_fails_closed_safely(monkeypatch, versions, elevated, failure):
    monkeypatch.delenv("HUB_MIGRATION_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="Hub runtime database contract failed") as error:
        asyncio.run(validate_runtime_schema(Database(versions, elevated, failure)))
    assert "private-password" not in str(error.value)
    assert error.value.__suppress_context__


def test_migration_secret_is_rejected_before_database_access(monkeypatch):
    monkeypatch.setenv("HUB_MIGRATION_DATABASE_URL", "private")
    db = Database([])
    with pytest.raises(RuntimeError, match="must not receive"):
        asyncio.run(validate_runtime_schema(db))
    assert not db.statements


def test_migration_never_falls_back_to_runtime_dsn(monkeypatch, capsys):
    monkeypatch.delenv("HUB_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setenv("HUB_DATABASE_URL", "private-runtime")
    assert migrate.main() == 1
    assert "private" not in capsys.readouterr().err


def test_migration_rejects_mixed_credentials(monkeypatch):
    monkeypatch.setenv("HUB_MIGRATION_DATABASE_URL", "private-migrator")
    monkeypatch.setenv("HUB_DATABASE_URL", "private-runtime")
    assert migrate.main() == 1
