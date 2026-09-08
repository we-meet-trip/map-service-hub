"""One-shot Alembic entrypoint; no application settings/providers are imported."""
import os
import sys

from alembic import command

from app.db.schema_contract import migration_config


def main() -> int:
    if not os.environ.get("HUB_MIGRATION_DATABASE_URL"):
        print("HUB_MIGRATION_DATABASE_URL is required", file=sys.stderr)
        return 1
    if os.environ.get("HUB_DATABASE_URL"):
        print("Hub migration job must not receive the runtime DSN", file=sys.stderr)
        return 1
    try:
        command.upgrade(migration_config(), "head")
    except Exception:
        print("Hub migration failed; preserve database and inspect with the operator", file=sys.stderr)
        return 1
    print("Hub migration completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
