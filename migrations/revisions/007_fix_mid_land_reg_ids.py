"""영남권 중기 육상예보 지역코드 교정.

0003 시드가 대구·경북과 부산·울산·경남의 중기 육상예보 지역코드를 서로
바꿔 넣었다. 그 결과 영남 전역에서 D+3 이후의 하늘상태·강수확률이 반대편
권역 값으로 나가고 있었다. 기온은 제대로 된 코드를 쓰고 있어서, 한 응답
안에 "부산·경남의 하늘"과 "대구의 기온"이 섞였다.

**판단 근거 — 시드 자신의 자기모순.**
중기 기온 코드는 육상 코드 아래로 번호가 붙는다. 다른 시도는 두 코드의
계열이 모두 맞는데(강원 11D1/11D1, 충북 11C1/11C1, 전북 11F1/11F1),
영남권 다섯 행만 어긋나 있었다.

    경상북도    land 11H20000  temp 11H10501(안동)   ← 계열 불일치
    대구광역시  land 11H20000  temp 11H10701(대구)   ← 계열 불일치
    경상남도    land 11H10000  temp 11H20301(창원)   ← 계열 불일치
    부산광역시  land 11H10000  temp 11H20201(부산)   ← 계열 불일치
    울산광역시  land 11H10000  temp 11H20101(울산)   ← 계열 불일치

기온 코드는 지역명과 정확히 맞으므로 틀린 쪽은 육상 코드다. 따라서
11H10000 은 대구·경상북도, 11H20000 은 부산·울산·경상남도로 바로잡는다.
(서울·인천·경기는 육상 코드가 세 시도 통합 코드라, 전남은 기온 코드가
도서 계열이라 계열이 달라 보이지만 정상이다.)

**적용 후 확인 절차.**
같은 날짜에 두 코드의 예보가 실제로 다른 값인지, 그리고 각 권역의 기온과
아귀가 맞는지 본다. 폴링을 한 번 돌린 뒤:

    SELECT reg_id, fcst_day_offset, weather, rain_prob_pct
    FROM hub_data.mid_land_forecast
    WHERE reg_id IN ('11H10000','11H20000')
      AND tm_fc = (SELECT MAX(tm_fc) FROM hub_data.mid_land_forecast)
    ORDER BY reg_id, fcst_day_offset;

두 코드의 값이 동일하면 이 교정으로 달라지는 것이 없다는 뜻이고, 다르면
어느 쪽이 대구·경북인지는 그날 실제 날씨와 대조해 확인할 수 있다.

Revision ID: 0007_fix_mid_land_reg_ids
Revises: 0006_weather_nowcast_snap
Create Date: 2026-08-04

호출 관계:
  - alembic upgrade head 가 0006 다음으로 본 리비전을 적용한다.
  - 교정 대상은 hub_data.subscribed_grids 의 다섯 행뿐이며, 격자 좌표와
    기온 코드는 건드리지 않는다.
"""
from __future__ import annotations

from alembic import op

revision: str = "0007_fix_mid_land_reg_ids"
down_revision: str | None = "0006_weather_nowcast_snap"
branch_labels = None
depends_on = None

# label 로 지목한다. admin_code 는 강원 두 행이 공유하듯 유일하지 않고,
# grid_id 는 환경마다 값이 달라질 수 있다.
_DAEGU_GYEONGBUK = ("경상북도", "대구광역시")
_BUSAN_ULSAN_GYEONGNAM = ("경상남도", "부산광역시", "울산광역시")


def upgrade() -> None:
    """영남권 다섯 행의 mid_land_reg_id 를 올바른 권역 코드로 바꾼다."""
    op.execute(
        """
        UPDATE hub_data.subscribed_grids
           SET mid_land_reg_id = '11H10000', updated_at = now()
         WHERE label IN ('경상북도', '대구광역시')
        """
    )
    op.execute(
        """
        UPDATE hub_data.subscribed_grids
           SET mid_land_reg_id = '11H20000', updated_at = now()
         WHERE label IN ('경상남도', '부산광역시', '울산광역시')
        """
    )
    # 코드가 바뀌면 예전 코드로 쌓아 둔 예보는 그 지역 것이 아니다.
    # 남겨 두면 다음 발표까지 최대 24시간 동안 반대편 권역 값이 계속 나간다.
    op.execute(
        """
        DELETE FROM hub_data.mid_land_forecast
         WHERE reg_id IN ('11H10000', '11H20000')
        """
    )


def downgrade() -> None:
    """교정 전 상태(서로 뒤바뀐 코드)로 되돌린다."""
    op.execute(
        """
        UPDATE hub_data.subscribed_grids
           SET mid_land_reg_id = '11H20000', updated_at = now()
         WHERE label IN ('경상북도', '대구광역시')
        """
    )
    op.execute(
        """
        UPDATE hub_data.subscribed_grids
           SET mid_land_reg_id = '11H10000', updated_at = now()
         WHERE label IN ('경상남도', '부산광역시', '울산광역시')
        """
    )
    op.execute(
        """
        DELETE FROM hub_data.mid_land_forecast
         WHERE reg_id IN ('11H10000', '11H20000')
        """
    )
