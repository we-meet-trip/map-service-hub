# flake8: noqa: E501
"""create hub_data.weather_nowcast_snapshots.

Revision ID: 0006_create_weather_nowcast_snapshots
Revises: 0005_create_forbidden_zones
Create Date: 2026-08-01

시각별 실황 기온을 격자 단위로 남겨, "어제 같은 시간대와 비교" 문구를
만들 수 있게 한다. 실황 API 는 현재 시각만 돌려주고 과거를 조회할 수단이
없어서, 조회할 때마다 그 시점 값을 남겨 두는 방식으로 과거를 확보한다.

좌표는 격자(nx, ny)로만 저장한다. 요청에 실려 온 기기 위경도는 격자로
바꾼 뒤 버리므로, 이 테이블에 개인을 특정할 수 있는 위치가 남지 않는다.

비교 대상이 없는 날(첫날, 또는 그 시각에 아무도 조회하지 않은 날)은 row 가
없고, 호출 측이 비교 문구를 생략한다.
"""
from __future__ import annotations

from alembic import op

# Alembic revision 체인 식별자. 직전 revision 은 0005.
revision: str = "0006_create_weather_nowcast_snapshots"
down_revision: str | None = "0005_create_forbidden_zones"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """실황 스냅샷 테이블을 만든다.

    (일자, 시각, 격자) 를 기본키로 두어 같은 시간대에 여러 번 조회해도 한
    row 만 남고 마지막 값으로 덮인다.
    """
    # date_kst / hour_kst: 관측 발표 시각을 KST 일자와 시로 쪼갠 값.
    #   어제 같은 시각을 찾는 조회가 이 두 컬럼만 보면 되도록 나눠 둔다.
    # nx / ny: 격자 좌표. 예보 테이블들과 같은 범위 제약을 건다.
    # temp_c: 관측 기온(섭씨).
    # pty: 강수 형태 코드. 비교 문구에는 쓰지 않지만 같은 관측분이라 함께 남긴다.
    op.execute(
        """
        CREATE TABLE hub_data.weather_nowcast_snapshots (
          date_kst    DATE     NOT NULL,
          hour_kst    SMALLINT NOT NULL CHECK (hour_kst BETWEEN 0 AND 23),
          nx          SMALLINT NOT NULL CHECK (nx BETWEEN 1 AND 149),
          ny          SMALLINT NOT NULL CHECK (ny BETWEEN 1 AND 253),
          temp_c      REAL     NOT NULL,
          pty         SMALLINT,
          captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (date_kst, hour_kst, nx, ny)
        )
        """
    )
    # 보관 기간이 지난 row 를 일자 기준으로 걷어내기 위한 인덱스.
    op.execute(
        "CREATE INDEX ix_wns_date "
        "ON hub_data.weather_nowcast_snapshots(date_kst)"
    )


def downgrade() -> None:
    """upgrade 의 역연산."""
    op.execute("DROP TABLE IF EXISTS hub_data.weather_nowcast_snapshots")
