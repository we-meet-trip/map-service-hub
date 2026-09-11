"""Read-only startup contract for the schema shipped with this image."""
from pathlib import Path
import os

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text


def migration_config() -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("version_locations", str(root / "migrations" / "revisions"))
    config.set_main_option("path_separator", "os")
    return config


async def validate_runtime_schema(db) -> None:
    # A serving container must never carry a migration credential, even unused.
    if os.environ.get("HUB_MIGRATION_DATABASE_URL"):
        raise RuntimeError("Hub serving must not receive migration credentials")
    expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
    try:
        async with db.session() as session:
            role = (await session.execute(text("""
                SELECT current_user <> 'map_hub_runtime'
                     OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication
                     OR rolbypassrls
                     OR has_database_privilege(current_user, current_database(), 'CREATE')
                     OR has_schema_privilege(current_user, 'hub_data', 'CREATE')
                     OR has_schema_privilege(current_user, 'public', 'CREATE')
                     OR pg_has_role(current_user, 'map_hub_owner', 'MEMBER')
                     OR (SELECT nspowner <> 'map_hub_owner'::regrole FROM pg_namespace WHERE nspname='hub_data')
                       AS elevated
                FROM pg_roles WHERE rolname = current_user
            """))).scalar_one()
            if role:
                raise RuntimeError("elevated role")
            actual = set((await session.execute(
                text("SELECT version_num FROM hub_data.alembic_version")
            )).scalars())
            if actual != expected:
                raise RuntimeError("schema version mismatch")
    except Exception:
        # Database errors can include DSNs/SQL parameters; startup emits one safe code.
        raise RuntimeError("Hub runtime database contract failed; run the isolated migration job") from None
