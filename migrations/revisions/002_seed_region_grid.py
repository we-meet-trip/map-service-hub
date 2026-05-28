"""seed region_grid.

Revision ID: 0002_seed_region_grid
Revises: 0001_init_hub_data
Create Date: 2026-05-23
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision: str = "0002_seed_region_grid"
down_revision: str | None = "0001_init_hub_data"
branch_labels: str | None = None
depends_on: str | None = None

_SEED_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "region_grid_seed.sql"
)


def upgrade() -> None:
    sql = _SEED_PATH.read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    op.execute("TRUNCATE TABLE hub_data.region_grid CASCADE")
