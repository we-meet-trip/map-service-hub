"""칸이 밀려 들어간 강원 9개 시군구 대표행 교정.

시드의 시군구 대표행은 읍면동 칸을 빈 문자열로 두는데, 아래 아홉 행만
그 칸이 통째로 빠진 채 만들어졌다. 그래서 뒤 값들이 한 칸씩 앞으로
당겨졌고, 줄 끝에 붙어 있던 갱신 일자가 위도 자리까지 밀려 들어왔다.

    ('5119000000','강원특별자치도','태백시','95',119,128,37.16...,20230611.0)
                                            └읍면동  └nx  └ny  └경도  └위도

    정상 행은 이런 모양이다.
    ('5117000000','강원특별자치도','동해시','',97,127,129.11...,37.52...)

**밀린 값에서 원래 값을 되짚을 수 있다.**
    읍면동 칸의 '95'  → 격자 nx
    격자 nx 의 119    → 격자 ny
    경도의 37.16      → 위도
경도만은 격자 ny 자리에 정수부(128)만 남아 소수점 아래가 사라졌다.
대신 같은 군의 읍면동 중 **되짚은 격자와 좌표가 일치하는 행**에서 가져온다.
시군구 대표 좌표는 원래 청사 위치이고, 그 격자에 있는 읍·동이 바로 청사가
있는 곳이라 같은 값이 나온다(고성군→간성읍, 태백시→황지동 등 아홉 곳 모두
한 곳씩만 대응된다).

**방치하면 생기는 일.**
  - 이 아홉 군은 읍면동 칸이 비어 있지 않아 "시군구 대표행"으로 잡히지
    않는다. 지역 조회가 읍면동 폴백으로 흘러가고, 대표행을 전제로 하는
    집계는 이 아홉 곳을 아예 세지 못한다.
  - 격자가 (95,119) 대신 (119,128) 로 들어가 있어 예보 조회가 엉뚱한
    좌표로 나간다. 두 값 모두 격자판 범위 안이라 제약에도 걸리지 않는다.
  - 위도가 2 천만이라 좌표를 쓰는 계산은 전부 무의미해진다.

Revision ID: 0008_fix_shifted_region_rows
Revises: 0007_fix_mid_land_reg_ids
Create Date: 2026-08-05

호출 관계:
  - alembic upgrade head 가 0007 다음으로 본 리비전을 적용한다.
  - 교정 대상은 hub_data.region_grid 의 아홉 행뿐이며, 읍면동 행과
    다른 시도는 건드리지 않는다.
"""
from __future__ import annotations

from alembic import op

revision: str = "0008_fix_shifted_region_rows"
down_revision: str | None = "0007_fix_mid_land_reg_ids"
branch_labels = None
depends_on = None

# 위도 자리에 갱신 일자가 들어온 행만 고른다. 정상 행의 위도는 국내
# 범위(33~39)라 이 기준으로 손상 행만 정확히 걸러진다.
_BROKEN = "lat > 1000"


def upgrade() -> None:
    """밀린 아홉 행을 원래 자리로 되돌린다."""
    op.execute(
        f"""
        WITH broken AS (
          SELECT admin_code, lv1, lv2,
                 lv3::int AS real_nx,
                 nx       AS real_ny,
                 lon      AS real_lat
          FROM hub_data.region_grid
          WHERE {_BROKEN}
        ),
        recovered AS (
          SELECT b.admin_code, b.real_nx, b.real_ny, b.real_lat,
                 (SELECT r.lon
                    FROM hub_data.region_grid r
                   WHERE r.lv1 = b.lv1 AND r.lv2 = b.lv2
                     AND r.lv3 <> ''
                     AND r.nx = b.real_nx AND r.ny = b.real_ny
                   ORDER BY r.admin_code
                   LIMIT 1) AS real_lon
          FROM broken b
        )
        UPDATE hub_data.region_grid g
           SET lv3 = '',
               nx  = r.real_nx,
               ny  = r.real_ny,
               lat = r.real_lat,
               lon = COALESCE(r.real_lon, g.ny)
          FROM recovered r
         WHERE g.admin_code = r.admin_code
        """
    )


def downgrade() -> None:
    """교정 전 상태(한 칸씩 밀린 값)로 되돌린다.

    경도의 소수점 아래는 원래 없던 값이라 정수부만 남긴다 — 교정 전과
    같은 모양이 된다.
    """
    op.execute(
        """
        UPDATE hub_data.region_grid
           SET lv3 = nx::text,
               nx  = ny,
               ny  = trunc(lon)::int,
               lon = lat,
               lat = 20230611.0
         WHERE admin_code IN (
           '5119000000', '5172000000', '5177000000', '5178000000',
           '5179000000', '5180000000', '5181000000', '5182000000',
           '5183000000'
         )
        """
    )
