"""init hub_data schema.

Revision ID: 0001_init_hub_data
Revises:
Create Date: 2026-05-23

본 revision 은 hub_data 스키마의 기저 테이블 5종을 한꺼번에 만든다:
  region_grid           — 행정구역 ↔ KMA 격자 매핑 (시드는 0002)
  subscribed_grids      — KMA 폴링 대상 격자/지역코드 (시드는 0003)
  short_term_forecast   — 단기예보 raw row
  mid_land_forecast     — 중기 육상예보 raw row
  mid_temp_forecast     — 중기 기온예보 raw row

호출 관계:
  - migrations/env.py 가 본 모듈을 발견하고 upgrade()/downgrade() 실행
  - app.db.forecast_repo / app.scheduler.hub_scheduler 가 본 테이블들을 사용
"""
from __future__ import annotations

from alembic import op

# Alembic 식별자: revision 체인은 down_revision 으로 선형 정렬된다.
# 본 revision 이 최초이므로 down_revision 은 None.
revision: str = "0001_init_hub_data"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """hub_data 스키마와 5개 테이블/관련 인덱스를 생성한다.

    스키마 생성 자체는 env.py 의 online 모드에서 한 번 수행되지만,
    offline/idempotent 실행을 위해 본 함수에서도 IF NOT EXISTS 로 보장한다.
    이후 각 테이블별로 CREATE TABLE → CREATE INDEX 를 순차 실행한다.
    """
    op.execute("CREATE SCHEMA IF NOT EXISTS hub_data")

    # region_grid — 행정구역 한 row 당 대표 KMA 격자(nx, ny)와 좌표(lat/lon).
    # PK admin_code 는 시군구·읍면동 등 행정 표준코드(10자리).
    # subscribed_grids 가 admin_code 를 FK 로 참조한다.

    op.execute(
        """
        CREATE TABLE hub_data.region_grid (
          admin_code   VARCHAR(10) PRIMARY KEY,
          lv1          TEXT        NOT NULL,
          lv2          TEXT        NOT NULL DEFAULT '',
          lv3          TEXT        NOT NULL DEFAULT '',
          nx           SMALLINT    NOT NULL CHECK (nx BETWEEN 1 AND 149),
          ny           SMALLINT    NOT NULL CHECK (ny BETWEEN 1 AND 253),
          lon          DOUBLE PRECISION NOT NULL,
          lat          DOUBLE PRECISION NOT NULL
        )
        """
    )
    # 행정구역 명/격자 좌표 기준 조회를 위한 보조 인덱스.
    op.execute("CREATE INDEX ix_rg_lv1     ON hub_data.region_grid(lv1)")
    op.execute("CREATE INDEX ix_rg_lv1_lv2 ON hub_data.region_grid(lv1, lv2)")
    op.execute("CREATE INDEX ix_rg_grid    ON hub_data.region_grid(nx, ny)")

    # subscribed_grids — KMA 폴링 대상 격자 + 중기예보 reg_id 한 쌍.
    # label: 사람이 읽을 수 있는 식별자(UNIQUE).
    # admin_code: region_grid 의 대표 행정구역으로 외래키.
    # mid_land_reg_id / mid_temp_reg_id: 중기예보 호출 키. 코드 체계는 서로 다름.
    op.execute(
        """
        CREATE TABLE hub_data.subscribed_grids (
          grid_id          BIGSERIAL PRIMARY KEY,
          label            TEXT NOT NULL UNIQUE,
          admin_code       VARCHAR(10) NOT NULL
                             REFERENCES hub_data.region_grid(admin_code),
          lat              DOUBLE PRECISION NOT NULL,
          lng              DOUBLE PRECISION NOT NULL,
          nx               SMALLINT NOT NULL CHECK (nx BETWEEN 1 AND 149),
          ny               SMALLINT NOT NULL CHECK (ny BETWEEN 1 AND 253),
          mid_land_reg_id  CHAR(8)  NOT NULL,
          mid_temp_reg_id  CHAR(8)  NOT NULL,
          is_active        BOOLEAN  NOT NULL DEFAULT TRUE,
          created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # 부분 인덱스 — is_active=TRUE 인 row 만 인덱스에 들어가 활성 grid 조회 비용 절감.
    op.execute(
        "CREATE INDEX ix_sg_active ON hub_data.subscribed_grids(is_active) "
        "WHERE is_active"
    )

    # short_term_forecast — 단기예보 raw row.
    # PK는 (nx, ny, fcst_at, category) — 같은 좌표/시각/카테고리 중복 방지.
    # CHECK category IN (...) — KMA 단기예보가 사용하는 14개 코드만 허용.
    # base_at: 발표 시각, expires_at: 캐시 만료 시각.
    op.execute(
        """
        CREATE TABLE hub_data.short_term_forecast (
          nx           SMALLINT    NOT NULL,
          ny           SMALLINT    NOT NULL,
          fcst_at      TIMESTAMPTZ NOT NULL,
          category     TEXT        NOT NULL CHECK (category IN
                         ('PCP','POP','PTY','REH','SKY','SNO','TMN','TMP',
                          'TMX','UUU','VEC','VVV','WAV','WSD')),
          base_at      TIMESTAMPTZ NOT NULL,
          fcst_value   TEXT        NOT NULL,
          expires_at   TIMESTAMPTZ NOT NULL,
          updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (nx, ny, fcst_at, category)
        )
        """
    )
    # base_at 인덱스: 발표분 단위 조회 / is_short_term_loaded 가속.
    # expires_at 인덱스: housekeeping_expire 의 만료 row 스캔 가속.
    op.execute(
        "CREATE INDEX ix_stf_base ON hub_data.short_term_forecast(base_at)"
    )
    op.execute(
        "CREATE INDEX ix_stf_expire "
        "ON hub_data.short_term_forecast(expires_at)"
    )

    # mid_land_forecast — 중기 육상예보 raw row.
    # PK는 (reg_id, tm_fc, fcst_day_offset, am_pm) — 같은 발표분 내 일자/AM·PM 중복 방지.
    # fcst_day_offset 4..10: 본 테이블이 다루는 D+N 의 N 범위 (CHECK 로 강제).
    # am_pm: D+4..7 은 AM/PM 분리, D+8..10 은 NA 한 건. (CHECK 로 강제)
    op.execute(
        """
        CREATE TABLE hub_data.mid_land_forecast (
          reg_id            CHAR(8)     NOT NULL,
          tm_fc             TIMESTAMPTZ NOT NULL,
          fcst_day_offset   SMALLINT    NOT NULL
                              CHECK (fcst_day_offset BETWEEN 4 AND 10),
          am_pm             TEXT        NOT NULL
                              CHECK (am_pm IN ('AM','PM','NA')),
          weather           TEXT,
          rain_prob_pct     SMALLINT,
          expires_at        TIMESTAMPTZ NOT NULL,
          updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (reg_id, tm_fc, fcst_day_offset, am_pm)
        )
        """
    )
    # tm_fc 인덱스: 발표분 단위 조회 / fetch_mid_land_range 의 MAX(tm_fc) 가속.
    # expires_at 인덱스: housekeeping 가속.
    op.execute(
        "CREATE INDEX ix_mlf_tm ON hub_data.mid_land_forecast(tm_fc)"
    )
    op.execute(
        "CREATE INDEX ix_mlf_expire "
        "ON hub_data.mid_land_forecast(expires_at)"
    )

    # mid_temp_forecast — 중기 기온예보 raw row.
    # PK는 (reg_id, tm_fc, fcst_day_offset) — am_pm 구분이 없다(일별 1건).
    # ta_min* / ta_max* 6개 컬럼: 평균/하한/상한 기온.
    op.execute(
        """
        CREATE TABLE hub_data.mid_temp_forecast (
          reg_id            CHAR(8)     NOT NULL,
          tm_fc             TIMESTAMPTZ NOT NULL,
          fcst_day_offset   SMALLINT    NOT NULL
                              CHECK (fcst_day_offset BETWEEN 4 AND 10),
          ta_min            SMALLINT,
          ta_min_low        SMALLINT,
          ta_min_high       SMALLINT,
          ta_max            SMALLINT,
          ta_max_low        SMALLINT,
          ta_max_high       SMALLINT,
          expires_at        TIMESTAMPTZ NOT NULL,
          updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
          PRIMARY KEY (reg_id, tm_fc, fcst_day_offset)
        )
        """
    )
    # tm_fc / expires_at 인덱스 — 위 mid_land_forecast 인덱스와 같은 의도.
    op.execute(
        "CREATE INDEX ix_mtf_tm ON hub_data.mid_temp_forecast(tm_fc)"
    )
    op.execute(
        "CREATE INDEX ix_mtf_expire "
        "ON hub_data.mid_temp_forecast(expires_at)"
    )


def downgrade() -> None:
    """upgrade 의 역연산. FK 의존성 순서대로 자식 → 부모 순으로 drop.

    region_grid 가 subscribed_grids 의 FK 대상이므로 region_grid 를 마지막에 drop.
    스키마 자체는 남겨 둔다(다른 마이그레이션이 보유할 수 있어 안전을 위해).
    """
    op.execute("DROP TABLE IF EXISTS hub_data.mid_temp_forecast")
    op.execute("DROP TABLE IF EXISTS hub_data.mid_land_forecast")
    op.execute("DROP TABLE IF EXISTS hub_data.short_term_forecast")
    op.execute("DROP TABLE IF EXISTS hub_data.subscribed_grids")
    op.execute("DROP TABLE IF EXISTS hub_data.region_grid")
