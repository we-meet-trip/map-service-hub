"""seed region_grid.

Revision ID: 0002_seed_region_grid
Revises: 0001_init_hub_data
Create Date: 2026-05-23

본 revision 은 0001 이 생성한 hub_data.region_grid 테이블에 행정구역 시드를
적재한다. 적재할 INSERT 문은 데이터 양이 크기 때문에 Python 코드에 직접
적지 않고 ../data/region_grid_seed.sql 외부 SQL 파일을 읽어 실행한다.

호출 관계:
  - 0001 (region_grid 테이블 생성) 이후
  - 0003 (subscribed_grids 시드, region_grid FK 참조) 이전 실행되어야 한다.
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

# Alembic revision 체인 식별자. 직전 revision 은 0001.
revision: str = "0002_seed_region_grid"
down_revision: str | None = "0001_init_hub_data"
branch_labels: str | None = None
depends_on: str | None = None

# _SEED_PATH — 외부 시드 SQL 파일의 절대 경로.
# migrations/revisions/002_seed_region_grid.py 기준 상위(migrations) 의
# data/region_grid_seed.sql 을 가리킨다.
_SEED_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "region_grid_seed.sql"
)


def upgrade() -> None:
    """_SEED_PATH 의 INSERT 문을 그대로 읽어 한 번에 실행한다.

    Python 코드와 SQL 본문을 분리해 두면, 시드 데이터 갱신 시
    Python 마이그레이션 코드는 그대로 두고 .sql 파일만 교체할 수 있다.
    """
    sql = _SEED_PATH.read_text(encoding="utf-8")
    op.execute(sql)


def downgrade() -> None:
    """region_grid 의 모든 row 를 제거한다.

    CASCADE 로 subscribed_grids 의 FK 참조 row 도 함께 삭제된다.
    구조(테이블) 자체는 그대로 둔다 — 그 책임은 0001 의 downgrade.
    """
    op.execute("TRUNCATE TABLE hub_data.region_grid CASCADE")
