# APScheduler 기반 백그라운드 잡을 본 모듈에 정의한다.
# 외부 예보 API를 cron 주기로 폴링하여 캐시를 사전 적재한다.
#
# 본 모듈이 제공하는 것:
#   resolve_short_term_base / resolve_mid_tm_fc — KST 기준 발표 시각 계산
#   short_term_polling_loop / mid_term_polling_loop — 1회 라운드 폴링 루프
#   housekeeping_job  — 만료 row 삭제 잡
#   build_scheduler   — AsyncIOScheduler 생성 + cron job 3종 등록
#
# 호출 관계:
#   - app.main.lifespan 이 build_scheduler() / 두 폴링 루프를 startup 에 호출
#   - app.routers.internal_router 가 두 폴링 루프 / housekeeping_job 을 trigger
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.clients.hub_clients import KMAApiError, KMAClient
from app.config import settings
from app.db.forecast_repo import (
    housekeeping_expire,
    is_mid_land_loaded,
    is_mid_temp_loaded,
    loaded_short_term_grids,
    load_active_grids,
    upsert_mid_land,
    upsert_mid_temp,
    upsert_short_term_items,
)
from app.scheduler.places_sync import durunubi_sync_loop
from app.utils.kma_grid import KST, parse_kma_base_at, parse_kma_tm_fc

logger = logging.getLogger(__name__)

# 단기예보 KMA 발표 시각(시) 목록. 8회/일.
# resolve_short_term_base 가 본 튜플을 역순 탐색해 가장 최근 slot 을 결정한다.
_SHORT_SLOTS = (2, 5, 8, 11, 14, 17, 20, 23)


def resolve_short_term_base(now: datetime) -> tuple[str, str]:
    """resolve_short_term_base — 현재 시각 기준 가장 최근 단기예보 발표분 계산

    now: KST timezone-aware 현재 시각.
    동작:
      - 발표 후 약 10분의 안전 마진(cutoff = now - 10분)을 두고,
        cutoff 이하인 _SHORT_SLOTS 중 가장 늦은 시각을 선택.
      - 오늘의 어떤 slot 도 cutoff 이하가 아니면 전날 23:00 으로 fallback
        (예: 새벽 0~2시대 호출).

    반환: (base_date, base_time) — "YYYYMMDD", "HHMM" 형식 문자열.
        KMAClient.fetch_short_term 의 base_date/base_time 파라미터로 그대로 사용.

    호출처: short_term_polling_loop / 테스트 test_resolve_base.
    """
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
    """resolve_mid_tm_fc — 현재 시각 기준 가장 최근 중기예보 발표분 계산

    중기예보는 매일 06:00 / 18:00 2회 발표된다.

    now: KST timezone-aware 현재 시각.
    동작:
      - cutoff = now - 10분
      - cutoff.hour >= 18 → 오늘 18:00 발표분
      - cutoff.hour >=  6 → 오늘 06:00 발표분
      - 그 외(새벽) → 전날 18:00 발표분

    반환: "YYYYMMDDHHMM" 12자리 문자열 (KMA tmFc 파라미터 형식).
    호출처: mid_term_polling_loop / 테스트 test_resolve_base.
    """
    cutoff = now - timedelta(minutes=10)
    if cutoff.hour >= 18:
        return cutoff.strftime("%Y%m%d") + "1800"
    if cutoff.hour >= 6:
        return cutoff.strftime("%Y%m%d") + "0600"
    prev = cutoff - timedelta(days=1)
    return prev.strftime("%Y%m%d") + "1800"


async def short_term_polling_loop() -> None:
    """short_term_polling_loop — 단기예보 1발표분에 대한 폴링 루프

    동작 개요:
      1) resolve_short_term_base 로 현재 시점의 발표(base_at) 결정
      2) settings.KMA_RETRY_MAX_DURATION_SEC 후를 deadline 으로 설정
      3) deadline 이전까지 반복:
         a. load_active_grids 로 활성 grid 전체 조회
         b. loaded_short_term_grids 로 이미 적재된 격자를 한 번에 걸러 pending
         c. pending 이 비면 정상 종료(모두 적재 완료)
         d. KMAClient 로 pending grid 의 단기예보를 한 건씩 fetch +
            upsert_short_term_items 로 저장. KMA API 실패는 warning 만 남기고 계속.
         e. KMA_POLL_INTERVAL_SEC 만큼 grid 사이 대기,
            한 라운드 끝나면 KMA_RETRY_INTERVAL_SEC 대기 후 재시도
      4) deadline 까지 모두 처리하지 못하면 error 로그 후 종료

    호출처:
      - app.main.lifespan (startup 1회 즉시 실행)
      - build_scheduler 가 cron(2,5,8,...,23 시 :10) 으로 자동 실행
      - routers.internal_router.run_now(which="short") 가 즉시 트리거
    """
    base_date, base_time = resolve_short_term_base(datetime.now(KST))
    base_at = parse_kma_base_at(base_date, base_time)
    deadline = datetime.now(KST) + timedelta(
        seconds=settings.KMA_RETRY_MAX_DURATION_SEC
    )
    logger.info(
        "short_term loop start base_at=%s deadline=%s",
        base_at, deadline,
    )
    # 마지막 라운드에서 끝내 실패한 격자. 루프가 한 번도 돌지 않아도
    # 아래 error 로그가 참조하므로 루프 밖에서 초기화한다.
    failed: list = []
    while datetime.now(KST) < deadline:
        grids = await load_active_grids()
        # 이미 적재된 격자는 한 번의 조회로 가려낸다. 격자마다 물어보면
        # 라운드마다 격자 수만큼 트랜잭션이 열려 커넥션 풀을 잠식한다.
        loaded = await loaded_short_term_grids(base_at)
        pending = [g for g in grids if (g.nx, g.ny) not in loaded]
        if not pending:
            logger.info(
                "short_term base_at=%s all %d grids loaded, exit",
                base_at, len(grids),
            )
            return
        failed = []
        async with KMAClient(settings.KMA_SERVICE_KEY.get_secret_value()) as kma:
            for g in pending:
                try:
                    items = await kma.fetch_short_term(
                        g.nx, g.ny, base_date, base_time
                    )
                    await upsert_short_term_items(
                        g.nx, g.ny, base_at, items
                    )
                except KMAApiError as e:
                    failed.append(g)
                    logger.warning(
                        "short_term retry %s nx=%s ny=%s code=%s msg=%s",
                        g.label, g.nx, g.ny, e.code, e.msg,
                    )
                except Exception:
                    # 격자 하나의 실패가 나머지를 막지 않게 한다. 예전에는
                    # 적재 중 DB 예외가 루프 밖으로 튀어 그 발표분의 남은
                    # 격자가 통째로 비었고, 이 코루틴은 백그라운드 태스크라
                    # 그 사실조차 로그에 남지 않았다.
                    failed.append(g)
                    logger.exception(
                        "short_term grid failed %s nx=%s ny=%s",
                        g.label, g.nx, g.ny,
                    )
                await asyncio.sleep(settings.KMA_POLL_INTERVAL_SEC)
        if not failed:
            logger.info(
                "short_term base_at=%s round complete, %d grids polled",
                base_at, len(pending),
            )
            return
        await asyncio.sleep(settings.KMA_RETRY_INTERVAL_SEC)
    logger.error(
        "short_term base_at=%s deadline reached, %d grids unloaded: %s",
        base_at, len(failed), [g.label for g in failed[:10]],
    )


async def mid_term_polling_loop() -> None:
    """mid_term_polling_loop — 중기예보 1발표분에 대한 폴링 루프

    동작 개요:
      1) resolve_mid_tm_fc 로 현재 시점의 발표(tm_fc) 결정
      2) settings.KMA_RETRY_MAX_DURATION_SEC 후를 deadline 으로 설정
      3) deadline 이전까지 반복:
         a. load_active_grids 로 활성 grid 전체 조회
         b. land_pending: 미적재인 mid_land_reg_id 집합(중복 제거 후 정렬)
            temp_pending: 미적재인 mid_temp_reg_id 집합(중복 제거 후 정렬)
            서로 다른 grid 가 같은 reg_id 를 가질 수 있어 dict 가 아닌 set/sorted 로 dedupe.
         c. 두 set 이 모두 비면 정상 종료
         d. KMAClient 로 land → temp 순서로 한 건씩 fetch + upsert
         e. 한 격자의 실패는 그 격자만 다음 라운드로 미루고 계속
      4) deadline 도달 시 error 로그 후 종료

    호출처:
      - app.main.lifespan (startup 1회 즉시 실행)
      - build_scheduler 가 cron(6, 18 시 :10) 으로 자동 실행
      - routers.internal_router.run_now(which="mid") 가 즉시 트리거
    """
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
        async with KMAClient(settings.KMA_SERVICE_KEY.get_secret_value()) as kma:
            for rid in land_pending:
                try:
                    payload = await kma.fetch_mid_land(rid, tm_fc_str)
                    await upsert_mid_land(rid, tm_fc, payload)
                except KMAApiError as e:
                    logger.warning(
                        "mid_land retry reg=%s code=%s msg=%s",
                        rid, e.code, e.msg,
                    )
                except Exception:
                    # 구역 하나의 실패가 나머지 구역과 기온 적재까지
                    # 막지 않게 한다(단기 루프와 같은 계약).
                    logger.exception("mid_land failed reg=%s", rid)
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
                except Exception:
                    logger.exception("mid_temp failed reg=%s", rid)
                await asyncio.sleep(settings.KMA_POLL_INTERVAL_SEC)
        await asyncio.sleep(settings.KMA_RETRY_INTERVAL_SEC)
    logger.error("mid_term tm_fc=%s deadline reached", tm_fc)


async def housekeeping_job() -> None:
    """housekeeping_job — 만료된 forecast row 삭제 잡

    forecast_repo.housekeeping_expire 를 호출하고 삭제 row 수를 로그로 남긴다.

    호출처:
      - build_scheduler 가 cron(매시 :05) 으로 자동 실행
      - routers.internal_router.run_now(which="housekeep") 가 즉시 트리거
    """
    deleted = await housekeeping_expire()
    logger.info("kma housekeeping deleted=%d", deleted)


def build_scheduler() -> AsyncIOScheduler:
    """build_scheduler — APScheduler 인스턴스 + cron job 3종 등록

    KST(Asia/Seoul) 기준 cron 으로 다음 잡을 등록한다:

      kma_short      — short_term_polling_loop
        cron: 02,05,08,11,14,17,20,23 시 정각 + 10분
        (KMA 단기예보 발표시각의 ~10분 후를 의도)
      kma_mid        — mid_term_polling_loop
        cron: 06, 18 시 정각 + 10분 (중기예보 발표시각의 ~10분 후)
      kma_housekeep  — housekeeping_job
        cron: 매시 :05 (만료된 row 정리)

    공통 옵션:
      max_instances=1     — 동일 잡 중복 실행 차단
      coalesce=True       — 누적 미발화건은 1회로 합쳐 실행
      misfire_grace_time  — 단기/중기 잡은 5분 유예 허용

    반환: 시작되지 않은 AsyncIOScheduler 인스턴스. 호출자가 .start() 한다.
    호출처: app.main.lifespan.
    """
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
    # durunubi_sync — 걷기/자전거 코스 사전 적재.
    # 코스 데이터는 거의 변하지 않으므로 긴 주기(기본 주 1회)로 갱신한다.
    # 주기 잡은 force=True 로 이미 적재돼 있어도 변경분을 다시 받아온다
    # (부팅 시 동기화는 force 없이 호출되어 적재돼 있으면 건너뛴다).
    sched.add_job(
        durunubi_sync_loop,
        IntervalTrigger(
            hours=settings.DURUNUBI_SYNC_INTERVAL_HOURS, timezone=KST
        ),
        id="durunubi_sync",
        kwargs={"force": True},
        max_instances=1,
        coalesce=True,
    )
    return sched
