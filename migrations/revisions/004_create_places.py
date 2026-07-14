# flake8: noqa: E501
"""create hub_data.places.

Revision ID: 0004_create_places
Revises: 0003_seed_subscribed_grids
Create Date: 2026-06-27

장소 후보를 영속 저장하는 hub_data.places 테이블을 만든다. 두 종류의
출처를 한 테이블에 담으며 source 컬럼으로 구분한다:
  - 코스 출처(durunubi): 걷기/자전거 코스를 주기 동기화로 사전 적재한다.
  - 점 장소 출처(kakao): 평소에는 짧은 캐시로만 다루지만, 공용 표현을
    공유할 수 있도록 컬럼은 함께 정의해 둔다.

좌표는 lat/lng 컬럼과 PostGIS geom(Point, 4326) 을 함께 보유한다.
반경 질의는 geom 의 GIST 인덱스를 사용한다.
"""
from __future__ import annotations

from alembic import op

# Alembic revision 체인 식별자. 직전 revision 은 0003.
revision: str = "0004_create_places"
down_revision: str | None = "0003_seed_subscribed_grids"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """places 테이블과 공간/조회 인덱스를 생성한다.

    geom 컬럼을 위해 PostGIS 확장을 보장한 뒤 테이블을 만들고,
    반경 질의용 GIST 인덱스와 출처/구분 필터용 보조 인덱스를 만든다.
    """
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # content_id: 출처별 식별자에 접두사를 붙여 충돌을 막는다
    #   (예: "durunubi:T_CRS_MNG...", "kakao:1234").
    # crs_* / brd_div / gpx_url / route_idx 는 코스 출처 전용 메타이며
    #   점 장소 출처에서는 NULL 로 남는다.
    # bbox_* 는 코스 전체 트랙의 경계 상자로, 지도에서 코스를 감싸는
    #   영역을 잡을 때 쓴다.
    op.execute(
        """
        CREATE TABLE hub_data.places (
          content_id           TEXT PRIMARY KEY,
          source               TEXT NOT NULL
                                 CHECK (source IN ('kakao', 'durunubi')),
          name                 TEXT NOT NULL,
          address              TEXT NOT NULL DEFAULT '',
          road_address         TEXT,
          lat                  DOUBLE PRECISION NOT NULL,
          lng                  DOUBLE PRECISION NOT NULL,
          geom                 geometry(Point, 4326) NOT NULL,
          category             TEXT,
          category_group_code  TEXT,
          phone                TEXT,
          place_url            TEXT,
          crs_dstnc_km         DOUBLE PRECISION,
          crs_total_min        INTEGER,
          crs_level            SMALLINT,
          brd_div              TEXT,
          gpx_url              TEXT,
          route_idx            TEXT,
          bbox_min_lat         DOUBLE PRECISION,
          bbox_min_lng         DOUBLE PRECISION,
          bbox_max_lat         DOUBLE PRECISION,
          bbox_max_lng         DOUBLE PRECISION,
          updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # 반경(주변) 질의용 공간 인덱스.
    op.execute(
        "CREATE INDEX ix_places_geom ON hub_data.places USING GIST (geom)"
    )
    # 출처/걷기·자전거 구분 필터용 보조 인덱스.
    op.execute("CREATE INDEX ix_places_source ON hub_data.places(source)")
    op.execute("CREATE INDEX ix_places_brd ON hub_data.places(brd_div)")


def downgrade() -> None:
    """upgrade 의 역연산. 테이블만 제거하고 확장은 남겨 둔다."""
    op.execute("DROP TABLE IF EXISTS hub_data.places")
