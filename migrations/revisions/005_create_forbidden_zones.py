# flake8: noqa: E501
"""create hub_data.forbidden_zones.

Revision ID: 0005_create_forbidden_zones
Revises: 0004_create_places
Create Date: 2026-07-07

경로가 지나가면 안 되는 금지구역(면)을 영속 저장하는
hub_data.forbidden_zones 테이블을 만든다. 룰 엔진의 금지구역 교차 판정
(rules_repo.zones_intersecting_polyline)이 본 테이블을 참조한다.

zone 은 PostGIS geometry(Polygon, 4326) 이며, 폴리라인과의 교차 질의는
zone 의 GIST 인덱스를 사용한다.

주의: 이 테이블이 비어 있으면(초기/시드 전) 어떤 폴리라인도 교차하지
않으므로 금지구역 필터링은 사실상 비활성(무필터)으로 동작한다. 시드
데이터는 별도 적재 단계에서 채운다.
"""
from __future__ import annotations

from alembic import op

# Alembic revision 체인 식별자. 직전 revision 은 0004.
revision: str = "0005_create_forbidden_zones"
down_revision: str | None = "0004_create_places"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """forbidden_zones 테이블과 공간 인덱스를 생성한다.

    zone 컬럼을 위해 PostGIS 확장을 보장한 뒤 테이블을 만들고, 폴리라인
    교차(ST_Intersects) 질의용 GIST 인덱스를 만든다.
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # name: 금지구역 이름(응답에 그대로 노출).
    # reason: 금지 사유(선택). 운영/디버깅용 메모.
    # zone: 금지 영역 폴리곤(4326). 폴리라인과의 교차 판정 대상.
    op.execute(
        """
        CREATE TABLE hub_data.forbidden_zones (
          zone_id     BIGSERIAL PRIMARY KEY,
          name        TEXT NOT NULL,
          reason      TEXT,
          zone        geometry(Polygon, 4326) NOT NULL,
          created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # 폴리라인 교차 질의용 공간 인덱스.
    op.execute(
        "CREATE INDEX ix_forbidden_zones_zone "
        "ON hub_data.forbidden_zones USING GIST (zone)"
    )


def downgrade() -> None:
    """upgrade 의 역연산. 테이블만 제거하고 확장은 남겨 둔다."""
    op.execute("DROP TABLE IF EXISTS hub_data.forbidden_zones")
