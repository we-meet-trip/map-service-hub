# APScheduler 기반 백그라운드 잡을 본 모듈에 정의한다.
# 외부 예보 API를 cron 주기로 폴링하여 캐시를 사전 적재한다.
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.clients.hub_clients import KMAApiError, KMAClient
from app.config import settings
from app.db.forecast_repo import (
    housekeeping_expire,
    is_mid_land_loaded,
    is_mid_temp_loaded,
    is_short_term_loaded,
    load_active_grids,
    upsert_mid_land,
    upsert_mid_temp,
    upsert_short_term_items,
)
from app.utils.kma_grid import KST, parse_kma_base_at, parse_kma_tm_fc

logger = logging.getLogger(__name__)

_SHORT_SLOTS = (2, 5, 8, 11, 14, 17, 20, 23)


def resolve_short_term_base(now: datetime) -> tuple[str, str]:
    cutoff = now - timedelta(minutes=10)
    for h in reversed(_SHORT_SLOTS):
        slot = cutoff.replace(hour=h, minute=0, second=0, microsecond=0)
        if slot <= cutoff:
            return slot.strftime("%Y%m%d"), f"{h:02d}00"
    prev = (now - timedelta(days=1)).replace(
        hour=23, minute=0, second=0, microsecond=0
    )
    return prev.strftime("%Y%m%d"), "2300"


def resolve_mid_tm_fc(now: datetime) -> str:
    cutoff = now - timedelta(minutes=10)
    if cutoff.hour >= 18:
        return cutoff.strftime("%Y%m%d") + "1800"
    if cutoff.hour >= 6:
        return cutoff.strftime("%Y%m%d") + "0600"
    prev = cutoff - timedelta(days=1)
    return prev.strftime("%Y%m%d") + "1800"


async def short_term_polling_loop() -> None:
    base_date, base_time = resolve_short_term_base(datetime.now(KST))
    base_at = parse_kma_base_at(base_date, base_time)
    deadline = datetime.now(KST) + timedelta(
        seconds=settings.KMA_RETRY_MAX_DURATION_SEC
    )
    logger.info(
        "short_term loop start base_at=%s deadline=%s",
        base_at, deadline,
    )
    while datetime.now(KST) < deadline:
        grids = await load_active_grids()
        pending: list = []
        for g in grids:
            if not await is_short_term_loaded(g.nx, g.ny, base_at):
                pending.append(g)
        if not pending:
            logger.info(
                "short_term base_at=%s all %d grids loaded, exit",
                base_at, len(grids),
            )
            return
        async with KMAClient(settings.KMA_SERVICE_KEY) as kma:
            for g in pending:
                try:
                    items = await kma.fetch_short_term(
                        g.nx, g.ny, base_date, base_time
                    )
                    await upsert_short_term_items(
                        g.nx, g.ny, base_at, items
                    )
                except KMAApiError as e:
                    logger.warning(
                        "short_term retry %s nx=%s ny=%s code=%s msg=%s",
                        g.label, g.nx, g.ny, e.code, e.msg,
                    )
                await asyncio.sleep(settings.KMA_POLL_INTERVAL_SEC)
        await asyncio.sleep(settings.KMA_RETRY_INTERVAL_SEC)
    logger.error(
        "short_term base_at=%s deadline reached, %d grids unloaded",
        base_at, len(pending),
    )


async def mid_term_polling_loop() -> None:
    tm_fc_str = resolve_mid_tm_fc(datetime.now(KST))
    tm_fc = parse_kma_tm_fc(tm_fc_str)
    deadline = datetime.now(KST) + timedelta(
        seconds=settings.KMA_RETRY_MAX_DURATION_SEC
    )
    logger.info(
        "mid_term loop start tm_fc=%s deadline=%s", tm_fc, deadline
    )
    while datetime.now(KST) < deadline:
        grids = await load_active_grids()
        land_pending = sorted({
            g.mid_land_reg_id for g in grids
            if not await is_mid_land_loaded(g.mid_land_reg_id, tm_fc)
        })
        temp_pending = sorted({
            g.mid_temp_reg_id for g in grids
            if not await is_mid_temp_loaded(g.mid_temp_reg_id, tm_fc)
        })
        if not land_pending and not temp_pending:
            logger.info("mid_term tm_fc=%s all loaded, exit", tm_fc)
            return
        async with KMAClient(settings.KMA_SERVICE_KEY) as kma:
            for rid in land_pending:
                try:
                    payload = await kma.fetch_mid_land(rid, tm_fc_str)
                    await upsert_mid_land(rid, tm_fc, payload)
                except KMAApiError as e:
                    logger.warning(
                        "mid_land retry reg=%s code=%s msg=%s",
                        rid, e.code, e.msg,
                    )
                await asyncio.sleep(settings.KMA_POLL_INTERVAL_SEC)
            for rid in temp_pending:
                try:
                    payload = await kma.fetch_mid_temp(rid, tm_fc_str)
                    await upsert_mid_temp(rid, tm_fc, payload)
                except KMAApiError as e:
                    logger.warning(
                        "mid_temp retry reg=%s code=%s msg=%s",
                        rid, e.code, e.msg,
                    )
                await asyncio.sleep(settings.KMA_POLL_INTERVAL_SEC)
        await asyncio.sleep(settings.KMA_RETRY_INTERVAL_SEC)
    logger.error("mid_term tm_fc=%s deadline reached", tm_fc)


async def housekeeping_job() -> None:
    deleted = await housekeeping_expire()
    logger.info("kma housekeeping deleted=%d", deleted)


def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=KST)
    sched.add_job(
        short_term_polling_loop,
        CronTrigger(
            hour="2,5,8,11,14,17,20,23", minute=10, timezone=KST
        ),
        id="kma_short",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    sched.add_job(
        mid_term_polling_loop,
        CronTrigger(hour="6,18", minute=10, timezone=KST),
        id="kma_mid",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    sched.add_job(
        housekeeping_job,
        CronTrigger(minute="5", timezone=KST),
        id="kma_housekeep",
        max_instances=1,
        coalesce=True,
    )
    return sched


async def kma_polling_job() -> None:
    """기상청 단기·중기 예보를 폴링하여 캐시에 적재하는 잡.

    발표 시각 + 일정 지연 후 cron으로 실행되며, 사전 등록된 좌표 집합에
    대해서만 호출하여 외부 API 사용량을 최소화한다.
    """
    await short_term_polling_loop()
    await mid_term_polling_loop()
