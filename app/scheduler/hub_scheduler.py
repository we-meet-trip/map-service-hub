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
from app import metrics
from app.db.forecast_repo import (
    housekeeping_expire,
    latest_mid_tm_fc,
    loaded_mid_land_regs,
    loaded_mid_temp_regs,
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

# 폴링 태스크 이름. 부팅 직후 띄우는 태스크, 수동 실행, 신선도 감시가
# 서로의 진행 여부를 알아보려면 같은 이름을 써야 한다.
SHORT_TASK_NAME = "short_term_polling"
MID_TASK_NAME = "mid_term_polling"


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
            metrics.record_unloaded("short", 0)
            return
        failed = []
        async with KMAClient(settings.KMA_SERVICE_KEY.get_secret_value()) as kma:
            for g in pending:
                # 격자가 많아지면 한 라운드가 시간 예산을 통째로 넘길 수
                # 있다. 라운드 경계에서만 확인하면 그 초과를 못 끊는다.
                if datetime.now(KST) >= deadline:
                    failed.extend(
                        pending[pending.index(g):]
                    )
                    break
                try:
                    items = await kma.fetch_short_term(
                        g.nx, g.ny, base_date, base_time
                    )
                    await upsert_short_term_items(
                        g.nx, g.ny, base_at, items
                    )
                    metrics.record_success(
                        "short", datetime.now(KST).timestamp()
                    )
                except KMAApiError as e:
                    failed.append(g)
                    metrics.record_failure("short")
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
                    metrics.record_failure("short")
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
            metrics.record_unloaded("short", 0)
            return
        await asyncio.sleep(settings.KMA_RETRY_INTERVAL_SEC)
    # 실패 대상을 잘라서 남기면 어느 지역이 비어 있는지 알 수 없다.
    metrics.record_unloaded("short", len(failed))
    logger.error(
        "short_term base_at=%s deadline reached, %d grids unloaded: %s",
        base_at, len(failed), [g.label for g in failed],
    )


async def mid_term_polling_loop() -> None:
    """mid_term_polling_loop — 중기예보 1발표분에 대한 폴링 루프

    동작 개요:
      1) resolve_mid_tm_fc 로 현재 시점의 발표(tm_fc) 결정
      2) settings.KMA_RETRY_MAX_DURATION_SEC 후를 deadline 으로 설정
      3) deadline 이전까지 반복:
         a. load_active_grids 로 활성 grid 전체 조회
         b. 격자가 가진 reg_id 를 먼저 중복 제거한 뒤, 이미 적재된 구역을
            한 번의 조회로 걸러 pending 을 만든다. 서로 다른 grid 가 같은
            reg_id 를 공유하므로 중복 제거가 먼저다
         c. 두 pending 이 모두 비면 정상 종료
         d. KMAClient 로 land → temp 순서로 한 건씩 fetch + upsert
         e. 한 구역의 실패는 그 구역만 다음 라운드로 미루고 계속
      4) 한 라운드에서 실패가 없으면 즉시 종료, deadline 도달 시 실패
         목록과 함께 error 로그 후 종료

    호출처:
      - app.main.lifespan (startup 1회 즉시 실행)
      - build_scheduler 가 cron(6, 18 시 :10) 으로 자동 실행
      - build_scheduler 의 신선도 감시가 낡음을 발견했을 때
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
    failed: list[str] = []
    while datetime.now(KST) < deadline:
        grids = await load_active_grids()
        # 적재 여부는 구역 단위로 한 번씩만 확인한다. 격자마다 물어보면
        # 같은 구역을 공유하는 격자 수만큼 같은 질문을 반복하게 된다.
        land_loaded = await loaded_mid_land_regs(tm_fc)
        temp_loaded = await loaded_mid_temp_regs(tm_fc)
        land_pending = sorted(
            {g.mid_land_reg_id for g in grids} - land_loaded
        )
        temp_pending = sorted(
            {g.mid_temp_reg_id for g in grids} - temp_loaded
        )
        if not land_pending and not temp_pending:
            logger.info("mid_term tm_fc=%s all loaded, exit", tm_fc)
            metrics.record_unloaded("mid_land", 0)
            metrics.record_unloaded("mid_temp", 0)
            return
        failed = []
        async with KMAClient(settings.KMA_SERVICE_KEY.get_secret_value()) as kma:
            for rid in land_pending:
                if datetime.now(KST) >= deadline:
                    failed.extend(land_pending[land_pending.index(rid):])
                    break
                try:
                    payload = await kma.fetch_mid_land(rid, tm_fc_str)
                    await upsert_mid_land(rid, tm_fc, payload)
                    metrics.record_success(
                        "mid_land", datetime.now(KST).timestamp()
                    )
                except KMAApiError as e:
                    failed.append(rid)
                    metrics.record_failure("mid_land")
                    logger.warning(
                        "mid_land retry reg=%s code=%s msg=%s",
                        rid, e.code, e.msg,
                    )
                except Exception:
                    # 구역 하나의 실패가 나머지 구역과 기온 적재까지
                    # 막지 않게 한다(단기 루프와 같은 계약).
                    failed.append(rid)
                    metrics.record_failure("mid_land")
                    logger.exception("mid_land failed reg=%s", rid)
                await asyncio.sleep(settings.KMA_POLL_INTERVAL_SEC)
            for rid in temp_pending:
                if datetime.now(KST) >= deadline:
                    failed.extend(temp_pending[temp_pending.index(rid):])
                    break
                try:
                    payload = await kma.fetch_mid_temp(rid, tm_fc_str)
                    await upsert_mid_temp(rid, tm_fc, payload)
                    metrics.record_success(
                        "mid_temp", datetime.now(KST).timestamp()
                    )
                except KMAApiError as e:
                    failed.append(rid)
                    metrics.record_failure("mid_temp")
                    logger.warning(
                        "mid_temp retry reg=%s code=%s msg=%s",
                        rid, e.code, e.msg,
                    )
                except Exception:
                    failed.append(rid)
                    metrics.record_failure("mid_temp")
                    logger.exception("mid_temp failed reg=%s", rid)
                await asyncio.sleep(settings.KMA_POLL_INTERVAL_SEC)
        if not failed:
            # 다 받았으면 다음 라운드를 기다릴 이유가 없다. 예전에는 재시도
            # 간격만큼 잔 뒤 같은 조회를 한 번 더 하고서야 끝났다.
            logger.info(
                "mid_term tm_fc=%s round complete, land=%d temp=%d",
                tm_fc, len(land_pending), len(temp_pending),
            )
            metrics.record_unloaded("mid_land", 0)
            metrics.record_unloaded("mid_temp", 0)
            return
        await asyncio.sleep(settings.KMA_RETRY_INTERVAL_SEC)
    metrics.record_unloaded("mid_land", len(failed))
    logger.error(
        "mid_term tm_fc=%s deadline reached, %d regions unloaded: %s",
        tm_fc, len(failed), failed,
    )


async def mid_freshness_watchdog() -> None:
    """mid_freshness_watchdog — 중기예보가 낡았으면 폴링을 다시 돌린다

    중기는 하루 두 번만 발표되어, 그 시각의 잡을 한 번 놓치면 다음
    발표까지 열두 시간이 빈다. 잡이 늦게 깨어나 폐기되는 경우뿐 아니라
    폴링이 돌긴 했는데 실패로 끝난 경우, 적재분이 지워진 경우에도 같은
    공백이 생긴다. 그래서 원인을 가리지 않고 "지금 있어야 할 발표분이
    있는가"만 보고 없으면 다시 채운다.

    이미 중기 루프가 돌고 있으면 아무것도 하지 않는다 — 같은 발표분을
    두 번 긁어 외부 호출만 늘리게 된다.

    호출처: build_scheduler 가 매시 :20 으로 자동 실행.
    """
    expected = parse_kma_tm_fc(resolve_mid_tm_fc(datetime.now(KST)))
    current = await latest_mid_tm_fc()
    if current is not None and current >= expected:
        return
    for task in asyncio.all_tasks():
        if task.get_name() == MID_TASK_NAME and not task.done():
            logger.info(
                "mid_term stale but a loop is already running expected=%s",
                expected,
            )
            return
    logger.warning(
        "mid_term stale, refilling expected=%s current=%s",
        expected, current,
    )
    await mid_term_polling_loop()


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
    # 중기는 하루 두 번뿐이라 한 번 놓치면 열두 시간이 빈다. 발표 잡과
    # 별개로 매시 한 번 "지금 있어야 할 발표분이 있는지"만 확인하고,
    # 없으면 원인을 가리지 않고 다시 채운다. 정상일 때는 조회 한 번으로
    # 끝난다.
    sched.add_job(
        mid_freshness_watchdog,
        CronTrigger(minute="20", timezone=KST),
        id="kma_mid_watch",
        max_instances=1,
        coalesce=True,
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
