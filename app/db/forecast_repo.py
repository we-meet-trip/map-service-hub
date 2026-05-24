"""hub_data forecast 테이블 read/upsert 레이어."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text

from app.db.hub_db import get_hub_db
from app.utils.kma_grid import parse_kma_fcst_at

logger = logging.getLogger(__name__)


_SHORT_TTL = timedelta(hours=6)
_MID_TTL = timedelta(hours=24)


@dataclass(slots=True)
class SubscribedGrid:
    grid_id: int
    label: str
    nx: int
    ny: int
    mid_land_reg_id: str
    mid_temp_reg_id: str


async def load_active_grids() -> list[SubscribedGrid]:
    sql = text(
        """
        SELECT grid_id, label, nx, ny, mid_land_reg_id, mid_temp_reg_id
        FROM hub_data.subscribed_grids
        WHERE is_active
        ORDER BY grid_id
        """
    )
    async with get_hub_db().session() as s:
        rows = (await s.execute(sql)).all()
    return [SubscribedGrid(*r) for r in rows]


async def is_short_term_loaded(
    nx: int, ny: int, base_at: datetime
) -> bool:
    sql = text(
        """
        SELECT 1 FROM hub_data.short_term_forecast
        WHERE nx = :nx AND ny = :ny AND base_at = :base_at
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        r = await s.execute(
            sql, {"nx": nx, "ny": ny, "base_at": base_at}
        )
        return r.first() is not None


async def is_mid_land_loaded(reg_id: str, tm_fc: datetime) -> bool:
    sql = text(
        """
        SELECT 1 FROM hub_data.mid_land_forecast
        WHERE reg_id = :reg_id AND tm_fc = :tm_fc
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        r = await s.execute(
            sql, {"reg_id": reg_id, "tm_fc": tm_fc}
        )
        return r.first() is not None


async def is_mid_temp_loaded(reg_id: str, tm_fc: datetime) -> bool:
    sql = text(
        """
        SELECT 1 FROM hub_data.mid_temp_forecast
        WHERE reg_id = :reg_id AND tm_fc = :tm_fc
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        r = await s.execute(
            sql, {"reg_id": reg_id, "tm_fc": tm_fc}
        )
        return r.first() is not None


async def upsert_short_term_items(
    nx: int,
    ny: int,
    base_at: datetime,
    items: list[dict],
) -> int:
    if not items:
        return 0
    expires_at = base_at + _SHORT_TTL
    rows: list[dict] = []
    for it in items:
        fcst_at = parse_kma_fcst_at(it["fcstDate"], it["fcstTime"])
        rows.append(
            {
                "nx": nx,
                "ny": ny,
                "fcst_at": fcst_at,
                "category": it["category"],
                "base_at": base_at,
                "fcst_value": str(it["fcstValue"]),
                "expires_at": expires_at,
            }
        )
    sql = text(
        """
        INSERT INTO hub_data.short_term_forecast
          (nx, ny, fcst_at, category, base_at, fcst_value,
           expires_at, updated_at)
        VALUES
          (:nx, :ny, :fcst_at, :category, :base_at, :fcst_value,
           :expires_at, now())
        ON CONFLICT (nx, ny, fcst_at, category) DO UPDATE SET
          base_at    = EXCLUDED.base_at,
          fcst_value = EXCLUDED.fcst_value,
          expires_at = EXCLUDED.expires_at,
          updated_at = now()
        """
    )
    async with get_hub_db().session() as s:
        await s.execute(sql, rows)
    logger.info(
        "short_term upserted nx=%s ny=%s base_at=%s rows=%d",
        nx, ny, base_at, len(rows),
    )
    return len(rows)


def _safe_int(v: object) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def upsert_mid_land(
    reg_id: str,
    tm_fc: datetime,
    payload: dict,
) -> int:
    expires_at = tm_fc + _MID_TTL
    rows: list[dict] = []
    for day in (4, 5, 6, 7):
        for ampm, suffix in (("AM", "Am"), ("PM", "Pm")):
            rows.append(
                {
                    "reg_id": reg_id,
                    "tm_fc": tm_fc,
                    "fcst_day_offset": day,
                    "am_pm": ampm,
                    "weather": payload.get(f"wf{day}{suffix}"),
                    "rain_prob_pct": _safe_int(
                        payload.get(f"rnSt{day}{suffix}")
                    ),
                    "expires_at": expires_at,
                }
            )
    for day in (8, 9, 10):
        rows.append(
            {
                "reg_id": reg_id,
                "tm_fc": tm_fc,
                "fcst_day_offset": day,
                "am_pm": "NA",
                "weather": payload.get(f"wf{day}"),
                "rain_prob_pct": _safe_int(payload.get(f"rnSt{day}")),
                "expires_at": expires_at,
            }
        )
    sql = text(
        """
        INSERT INTO hub_data.mid_land_forecast
          (reg_id, tm_fc, fcst_day_offset, am_pm, weather,
           rain_prob_pct, expires_at, updated_at)
        VALUES
          (:reg_id, :tm_fc, :fcst_day_offset, :am_pm, :weather,
           :rain_prob_pct, :expires_at, now())
        ON CONFLICT (reg_id, tm_fc, fcst_day_offset, am_pm) DO UPDATE
        SET weather       = EXCLUDED.weather,
            rain_prob_pct = EXCLUDED.rain_prob_pct,
            expires_at    = EXCLUDED.expires_at,
            updated_at    = now()
        """
    )
    async with get_hub_db().session() as s:
        await s.execute(sql, rows)
    logger.info(
        "mid_land upserted reg_id=%s tm_fc=%s rows=%d",
        reg_id, tm_fc, len(rows),
    )
    return len(rows)


async def upsert_mid_temp(
    reg_id: str,
    tm_fc: datetime,
    payload: dict,
) -> int:
    expires_at = tm_fc + _MID_TTL
    rows: list[dict] = []
    for day in range(4, 11):
        rows.append(
            {
                "reg_id": reg_id,
                "tm_fc": tm_fc,
                "fcst_day_offset": day,
                "ta_min": _safe_int(payload.get(f"taMin{day}")),
                "ta_min_low": _safe_int(
                    payload.get(f"taMin{day}Low")
                ),
                "ta_min_high": _safe_int(
                    payload.get(f"taMin{day}High")
                ),
                "ta_max": _safe_int(payload.get(f"taMax{day}")),
                "ta_max_low": _safe_int(
                    payload.get(f"taMax{day}Low")
                ),
                "ta_max_high": _safe_int(
                    payload.get(f"taMax{day}High")
                ),
                "expires_at": expires_at,
            }
        )
    sql = text(
        """
        INSERT INTO hub_data.mid_temp_forecast
          (reg_id, tm_fc, fcst_day_offset, ta_min, ta_min_low,
           ta_min_high, ta_max, ta_max_low, ta_max_high,
           expires_at, updated_at)
        VALUES
          (:reg_id, :tm_fc, :fcst_day_offset, :ta_min, :ta_min_low,
           :ta_min_high, :ta_max, :ta_max_low, :ta_max_high,
           :expires_at, now())
        ON CONFLICT (reg_id, tm_fc, fcst_day_offset) DO UPDATE
        SET ta_min      = EXCLUDED.ta_min,
            ta_min_low  = EXCLUDED.ta_min_low,
            ta_min_high = EXCLUDED.ta_min_high,
            ta_max      = EXCLUDED.ta_max,
            ta_max_low  = EXCLUDED.ta_max_low,
            ta_max_high = EXCLUDED.ta_max_high,
            expires_at  = EXCLUDED.expires_at,
            updated_at  = now()
        """
    )
    async with get_hub_db().session() as s:
        await s.execute(sql, rows)
    logger.info(
        "mid_temp upserted reg_id=%s tm_fc=%s rows=%d",
        reg_id, tm_fc, len(rows),
    )
    return len(rows)


async def housekeeping_expire() -> int:
    sqls = [
        text("DELETE FROM hub_data.short_term_forecast "
             "WHERE expires_at < now()"),
        text("DELETE FROM hub_data.mid_land_forecast "
             "WHERE expires_at < now()"),
        text("DELETE FROM hub_data.mid_temp_forecast "
             "WHERE expires_at < now()"),
    ]
    total = 0
    async with get_hub_db().session() as s:
        for q in sqls:
            r = await s.execute(q)
            total += r.rowcount or 0
    logger.info("housekeeping deleted=%d", total)
    return total
