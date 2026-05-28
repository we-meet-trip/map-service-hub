"""init hub_data schema.

Revision ID: 0001_init_hub_data
Revises:
Create Date: 2026-05-23
"""
from __future__ import annotations

from alembic import op

revision: str = "0001_init_hub_data"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS hub_data")

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
    op.execute("CREATE INDEX ix_rg_lv1     ON hub_data.region_grid(lv1)")
    op.execute("CREATE INDEX ix_rg_lv1_lv2 ON hub_data.region_grid(lv1, lv2)")
    op.execute("CREATE INDEX ix_rg_grid    ON hub_data.region_grid(nx, ny)")

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
    op.execute(
        "CREATE INDEX ix_sg_active ON hub_data.subscribed_grids(is_active) "
        "WHERE is_active"
    )

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
    op.execute(
        "CREATE INDEX ix_stf_base ON hub_data.short_term_forecast(base_at)"
    )
    op.execute(
        "CREATE INDEX ix_stf_expire "
        "ON hub_data.short_term_forecast(expires_at)"
    )

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
    op.execute(
        "CREATE INDEX ix_mlf_tm ON hub_data.mid_land_forecast(tm_fc)"
    )
    op.execute(
        "CREATE INDEX ix_mlf_expire "
        "ON hub_data.mid_land_forecast(expires_at)"
    )

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
    op.execute(
        "CREATE INDEX ix_mtf_tm ON hub_data.mid_temp_forecast(tm_fc)"
    )
    op.execute(
        "CREATE INDEX ix_mtf_expire "
        "ON hub_data.mid_temp_forecast(expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS hub_data.mid_temp_forecast")
    op.execute("DROP TABLE IF EXISTS hub_data.mid_land_forecast")
    op.execute("DROP TABLE IF EXISTS hub_data.short_term_forecast")
    op.execute("DROP TABLE IF EXISTS hub_data.subscribed_grids")
    op.execute("DROP TABLE IF EXISTS hub_data.region_grid")
