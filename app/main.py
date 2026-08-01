"""hub-service ASGI 진입점.

본 모듈은 FastAPI 애플리케이션 객체 `app` 을 구성하고, 세 라우터
(hub_router, rules_router, internal_router)와 헬스체크 엔드포인트를
등록한다.

lifespan 컨텍스트로 다음 라이프사이클을 관리한다:
  startup  — APScheduler 기동 + 단기/중기 폴링 루프 1회 즉시 실행
  shutdown — 스케줄러 정지 + DB 엔진 dispose

호출 관계:
  - Dockerfile ENTRYPOINT 의 uvicorn 이 본 모듈의 `app` 을 임포트한다.
  - 라우터 정의는 app.routers.* 에서 가져와 include 한다.
  - 폴링 루프와 housekeeping job 정의는 app.scheduler.hub_scheduler 에 있다.
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.cache.hub_cache import RedisCache
from app.clients.hub_clients import (
    AirKoreaClient,
    KakaoLocalClient,
    KMAClient,
    NaverBlogClient,
    OsrmClient,
)
from app.config import settings
from app.db.hub_db import dispose_hub_db
from app.hub_dependencies import (
    clear_place_clients,
    set_naver_client,
    set_osrm_clients,
    set_place_clients,
    set_weather_clients,
)
from app.place_stubs import places_stub_active
from app.route_stubs import routing_stub_active
from app.routers.hub_routers import router as hub_router
from app.routers.internal_admin_router import router as internal_admin_router
from app.routers.internal_router import router as internal_router
from app.routers.rules_router import router as rules_router
from app.scheduler.hub_scheduler import (
    build_scheduler,
    mid_term_polling_loop,
    short_term_polling_loop,
)
from app.scheduler.places_sync import durunubi_sync_loop

logger = logging.getLogger(__name__)


class _CoordinateRedactingFilter(logging.Filter):
    """접근 로그에 실린 좌표를 가린다.

    현재 날씨 조회는 기기 위치를 쿼리로 받는데, 접근 로그는 요청 URL 을
    그대로 남기므로 아무 조치를 하지 않으면 기기 위치가 로그에 쌓인다.
    좌표는 격자로 바꾼 뒤 버린다는 규칙이 요청 처리 안에서만 지켜지고
    로그에서 새는 것을 막는다.
    """

    _PATTERN = re.compile(r"\b(lat|lng)=-?\d+(?:\.\d+)?")

    def filter(self, record: logging.LogRecord) -> bool:
        """레코드의 인자에 섞인 좌표를 자리표시자로 바꾼다."""
        if record.args:
            record.args = tuple(
                self._PATTERN.sub(r"\1=***", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._PATTERN.sub(r"\1=***", record.msg)
        return True


logging.getLogger("uvicorn.access").addFilter(_CoordinateRedactingFilter())


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """앱 라이프사이클 컨텍스트.

    _app: FastAPI 가 자동 주입하는 앱 인스턴스. 부팅 폴링 태스크를
          `_app.state.bg_tasks` 집합에 강참조로 보관한다(GC 수거 방지).

    동작:
      startup
        1) build_scheduler() 로 cron job 3종(단기/중기/housekeeping) 등록
        2) scheduler.start() 로 백그라운드 실행 시작
        3) 부팅 직후 cron 이 돌기 전 1회 즉시 폴링하도록
           short/mid 폴링 루프를 asyncio.create_task 로 분리 실행
      shutdown
        1) scheduler.shutdown(wait=False) — 진행 중 잡 중단 없이 종료
        2) dispose_hub_db() — 비동기 엔진 자원 반환

    호출처: FastAPI 본체가 startup/shutdown 시점에 자동 호출한다.
    """
    # AUTH_ENFORCED=true 인데 공유 비밀이 비어 있으면 공개 endpoint 가
    # 사실상 무인증으로 열리므로, 부팅을 중단해(fail-fast) 설정 오류를 막는다.
    if (
        settings.AUTH_ENFORCED
        and not settings.INTERNAL_SERVICE_TOKEN.get_secret_value()
    ):
        raise RuntimeError(
            "AUTH_ENFORCED=true requires INTERNAL_SERVICE_TOKEN"
        )

    scheduler = build_scheduler()
    scheduler.start()
    logger.info("hub: APScheduler started")

    # 요청 경로에서 쓰는 장소 캐시와 카카오 클라이언트를 만들어 주입한다.
    # 키가 없으면 카카오는 스텁으로 동작하므로 클라이언트를 만들지 않는다.
    cache = RedisCache(settings.REDIS_URL, settings.REDIS_DB_CACHE)
    kakao_key = settings.KAKAO_REST_API_KEY.get_secret_value()
    kakao = (
        None
        if places_stub_active(kakao_key)
        else KakaoLocalClient(kakao_key)
    )
    set_place_clients(kakao, cache)

    # 네이버 블로그 리뷰 클라이언트. 자격증명(ID/시크릿) 중 하나라도 비어
    # 있으면 스텁으로 동작하므로 클라이언트를 만들지 않는다.
    naver_id = settings.NAVER_CLIENT_ID.get_secret_value()
    naver_secret = settings.NAVER_CLIENT_SECRET.get_secret_value()
    naver = (
        None
        if places_stub_active(naver_id) or places_stub_active(naver_secret)
        else NaverBlogClient(naver_id, naver_secret)
    )
    set_naver_client(naver)

    # 경로 라우팅(OSRM) 클라이언트. 프로파일별 base URL 이 비어 있으면
    # 스텁으로 동작하므로 클라이언트를 만들지 않는다(라우터가 스텁 폴백).
    foot_url = settings.OSRM_FOOT_BASE_URL
    bicycle_url = settings.OSRM_BICYCLE_BASE_URL
    osrm_foot = None if routing_stub_active(foot_url) else OsrmClient(foot_url)
    osrm_bicycle = (
        None
        if routing_stub_active(bicycle_url)
        else OsrmClient(bicycle_url)
    )
    set_osrm_clients(osrm_foot, osrm_bicycle)

    # 현재 날씨 조회용 클라이언트. 실황은 폴링이 아니라 요청 때마다 부른다.
    # 대기오염 키가 비어 있으면 기상청 키를 그대로 쓴다 — 두 서비스가 한
    # 계정 키로 열려 있는 경우가 흔해 키를 두 번 적지 않아도 되게 한다.
    kma_key = settings.KMA_SERVICE_KEY.get_secret_value()
    air_key = (
        settings.AIRKOREA_SERVICE_KEY.get_secret_value() or kma_key
    )
    kma_now = None if places_stub_active(kma_key) else KMAClient(kma_key)
    airkorea = (
        None if places_stub_active(air_key) else AirKoreaClient(air_key)
    )
    set_weather_clients(kma_now, airkorea)

    # 부팅 직후 1회 즉시 폴링/코스 동기화. create_task 결과를 강참조로
    # 보관하지 않으면 이벤트 루프가 약참조만 들고 있어, await asyncio.sleep
    # 구간 등에서 GC 가 실행 중 태스크를 수거할 수 있다. app.state 집합에
    # 담고 완료 시 자동 제거한다.
    _app.state.bg_tasks = set()
    for _coro in (
        short_term_polling_loop(),
        mid_term_polling_loop(),
        durunubi_sync_loop(),
    ):
        _task = asyncio.create_task(_coro)
        _app.state.bg_tasks.add(_task)
        _task.add_done_callback(_app.state.bg_tasks.discard)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        # 진행 중인 부팅 태스크(폴링/동기화)를 먼저 취소·수거한 뒤 자원을
        # 정리한다. 엔진을 태스크 실행 도중에 dispose 하지 않도록 보장한다.
        tasks = list(_app.state.bg_tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        clear_place_clients()
        await cache.aclose()
        if kakao is not None:
            await kakao.aclose()
        if naver is not None:
            await naver.aclose()
        if osrm_foot is not None:
            await osrm_foot.aclose()
        if osrm_bicycle is not None:
            await osrm_bicycle.aclose()
        if kma_now is not None:
            await kma_now.aclose()
        if airkorea is not None:
            await airkorea.aclose()
        await dispose_hub_db()
        logger.info("hub: scheduler/db disposed")


# 외부 라이브러리(uvicorn)가 임포트해야 하는 ASGI 객체.
# title/version 은 OpenAPI 문서에도 노출된다.
app = FastAPI(title="map-service-hub", version="0.0.1-poc", lifespan=lifespan)
# 외부 게이트웨이 라우터(hub_routers) → /weather 등 공개 endpoint.
app.include_router(hub_router)
# 룰 엔진 라우터(rules_router) → /v1/rules/* 공개 endpoint.
app.include_router(rules_router)
# 내부 운영 라우터(internal_router) → /internal/kma/* (CIDR 화이트리스트로 보호).
app.include_router(internal_router)
# 내부 운영(admin) 라우터 → /internal/grids/* · /internal/forbidden-zones/*
# (동일 internal_guard 재사용). admin 콘솔의 hub_data 쓰기 위임 대상.
app.include_router(internal_admin_router)

# Prometheus 계측 → GET /metrics (인증 없음, map-net 내부 Prometheus 가 스크레이프).
# 기본 HTTP 지표(요청수/지연 히스토그램/진행중 요청)를 노출한다.
Instrumentator().instrument(app).expose(
    app, endpoint="/metrics", include_in_schema=False
)


@app.get("/health")
async def health() -> dict[str, str]:
    """liveness 헬스체크.

    Dockerfile HEALTHCHECK 와 docker-compose 의 hub healthcheck 가
    본 endpoint(200 응답)로 컨테이너 상태를 판정한다.

    반환: {"status": "ok", "service": "hub"}
    """
    return {"status": "ok", "service": "hub"}
