# flake8: noqa: E501
"""create hub_data.air_quality_snapshots.

Revision ID: 0010_air_quality_snap
Revises: 0009_expand_subscribed_grids
Create Date: 2026-08-11

대기오염 측정값을 시각별로 남긴다. 지금까지는 요청이 올 때마다 발급처를
불러 짧게 캐시만 했는데, 그러면 발급처가 멈춰 있는 동안 화면에서 미세먼지가
통째로 사라지고 회복될 때까지 돌아오지 않았다. 미리 받아 두면 발급처가
잠시 멈춰도 직전 측정값으로 화면을 채울 수 있다.

측정소 단위로 남긴다. 발급처가 시도 한 번 호출에 그 시도의 모든 측정소를
돌려주므로 호출 한 번이 그대로 한 시도치 적재가 되고, 사용자의 시군구에
가까운 측정소를 고르는 판단은 조회할 때 한다.

data_time 은 발급처가 알려 주는 측정 시각이다. 우리가 받은 시각(captured_at)
과 나누어 두는 이유는, 발급처가 갱신을 멈추면 받은 시각만 계속 새로워지고
측정 시각은 멈춰 있기 때문이다. 화면에 내보낼지 말지는 측정 시각으로 판단해야
한다.
"""
from __future__ import annotations

from alembic import op

# Alembic revision 체인 식별자. 직전 revision 은 0009.
# alembic_version 컬럼이 32자라 식별자를 그 안에 맞춘다.
revision: str = "0010_air_quality_snap"
down_revision: str | None = "0009_expand_subscribed_grids"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """대기오염 스냅샷 테이블을 만든다.

    (시도, 측정소, 측정시각) 을 기본키로 두어 같은 발표분을 여러 번 받아도
    한 row 만 남는다.
    """
    # sido: 발급처가 쓰는 시도 표기("서울", "경기" …). 조회 키.
    # station_name: 측정소 이름. 시군구 이름과 같은 경우가 많아 가까운
    #   측정소를 고르는 데 쓴다.
    # data_time: 발급처가 알려 준 측정 시각.
    # pm10 / pm25: 농도(㎍/㎥). 측정소가 값을 못 낸 시각이 있어 NULL 을 받는다.
    op.execute(
        """
        CREATE TABLE hub_data.air_quality_snapshots (
          sido         VARCHAR(10)  NOT NULL,
          station_name VARCHAR(60)  NOT NULL,
          data_time    TIMESTAMPTZ  NOT NULL,
          pm10         SMALLINT,
          pm25         SMALLINT,
          captured_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
          PRIMARY KEY (sido, station_name, data_time)
        )
        """
    )
    # 조회는 언제나 "이 시도의 가장 최근 측정분"을 찾는 모양이다.
    op.execute(
        "CREATE INDEX ix_aqs_sido_time "
        "ON hub_data.air_quality_snapshots(sido, data_time DESC)"
    )
    # 보관 기간이 지난 row 를 걷어내기 위한 인덱스.
    op.execute(
        "CREATE INDEX ix_aqs_data_time "
        "ON hub_data.air_quality_snapshots(data_time)"
    )


def downgrade() -> None:
    """upgrade 의 역연산."""
    op.execute("DROP TABLE IF EXISTS hub_data.air_quality_snapshots")
