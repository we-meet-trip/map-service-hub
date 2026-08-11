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
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.cache.hub_cache import RedisCache
from app.clients.hub_clients import (
    AirKoreaClient,
    GooglePlacesClient,
    KakaoLocalClient,
    KMAClient,
    NaverBlogClient,
    OdsayClient,
    OsrmClient,
    PmClient,
    SeoulBikeClient,
)
from app.config import settings
from app.db.hub_db import dispose_hub_db
from app.hub_dependencies import (
    clear_place_clients,
    set_google_client,
    set_naver_client,
    set_odsay_clients,
    set_osrm_clients,
    set_place_clients,
    set_pm_client,
    set_seoul_bike_client,
    set_weather_clients,
)
from app.place_stubs import places_stub_active
from app.route_stubs import routing_stub_active, transit_stub_active
from app.routers.hub_routers import router as hub_router
from app.routers.internal_admin_router import router as internal_admin_router
from app.routers.internal_router import router as internal_router
from app.routers.rules_router import router as rules_router
from app.scheduler.hub_scheduler import (
    AIR_TASK_NAME,
    MID_TASK_NAME,
    NOWCAST_TASK_NAME,
    SHORT_TASK_NAME,
    air_polling_loop,
    build_scheduler,
    mid_term_polling_loop,
    nowcast_polling_loop,
    short_term_polling_loop,
)
from app.scheduler.places_sync import durunubi_sync_loop

logger = logging.getLogger(__name__)


class _CoordinateRedactingFilter(logging.Filter):
    """로그에 실린 좌표와 외부 인증키를 가린다.

    좌표: 현재 날씨 조회는 기기 위치를 쿼리로 받는데, 접근 로그는 요청 URL 을
    그대로 남기므로 아무 조치를 하지 않으면 기기 위치가 로그에 쌓인다.
    좌표는 격자로 바꾼 뒤 버린다는 규칙이 요청 처리 안에서만 지켜지고
    로그에서 새는 것을 막는다.

    인증키: 나가는 요청을 남기는 로거(httpx)가 URL 을 통째로 찍는다. 키를
    쿼리나 경로에 실어 보내는 발급처가 여럿이라, 클라이언트 쪽에서 오류
    본문만 가려서는 부족하다 — 정상 응답 한 줄마다 키가 그대로 남는다.
    URL 을 만들어 내는 자리가 아니라 로그로 나가는 길목에서 한 번에 막는다.

    키가 퍼센트 인코딩된 채 찍히는 경우까지 잡아야 한다. 인코딩된 문자를
    빼고 매칭하면 값의 앞부분만 가려지고 나머지가 남는다.

    좌표를 담는 이름은 lat/lng 만이 아니다. 출발·도착을 함께 받는 조회는
    start_lat 처럼 앞에 말을 붙여 쓰는데, 이름 앞이 밑줄이면 단어 경계가
    생기지 않아 짧은 이름만 찾는 규칙에는 걸리지 않는다. 앞에 붙는 말까지
    이름의 일부로 보아 함께 잡는다.
    """

    _PATTERN = re.compile(
        r"\b(\w*(?:latitude|longitude|lat|lng))=-?\d+(?:\.\d+)?"
    )
    # 값에 나타날 수 있는 문자 집합. 퍼센트 인코딩(%2F 등)까지 포함한다.
    _SECRET_PATTERNS = (
        re.compile(r"\b(serviceKey|apiKey|api_key|appkey)=[^&\s\"'<>]+"),
        # 서울 열린데이터광장처럼 키가 경로 한 칸을 차지하는 경우.
        re.compile(r"(:8088/)[^/\s\"'<>]+(/)"),
    )

    @classmethod
    def _scrub(cls, text: str) -> str:
        """한 문자열에서 좌표와 인증키를 자리표시자로 바꾼다."""
        text = cls._PATTERN.sub(r"\1=***", text)
        text = cls._SECRET_PATTERNS[0].sub(r"\1=***", text)
        return cls._SECRET_PATTERNS[1].sub(r"\1***\2", text)

    def filter(self, record: logging.LogRecord) -> bool:
        """레코드의 인자와 본문에 섞인 좌표·인증키를 가린다.

        인자가 문자열이 아닐 수 있다. 나가는 요청 로그는 주소를 문자열이
        아니라 URL 객체로 넘기는데, 문자열만 훑으면 그 인자가 그대로 통과해
        완성된 문장에는 값이 남는다. 그래서 문자열 인자를 가린 뒤 문장을
        만들어 보고, 그래도 가릴 것이 남아 있으면 그때는 문장을 미리 조립해
        통째로 가린다.

        미리 조립하는 것은 가릴 것이 남은 레코드에만 한다. 모든 레코드를
        조립하면 지연 서식의 이점이 사라지고, 인자를 따로 보는 처리기가
        있으면 그 값도 함께 잃는다.
        """
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            record.args = tuple(
                self._scrub(a) if isinstance(a, str) else a
                for a in record.args
            )
            try:
                rendered = record.getMessage()
            except (TypeError, ValueError):
                # 서식과 인자가 맞지 않는 레코드다. 여기서 막을 것은 없고,
                # 원래대로 두면 로깅 쪽이 자기 방식으로 알린다.
                return True
            cleaned = self._scrub(rendered)
            if cleaned != rendered:
                record.msg = cleaned
                record.args = ()
        return True


logging.getLogger("uvicorn.access").addFilter(_CoordinateRedactingFilter())
# 나가는 요청 로그도 같은 길목을 지나게 한다. 이 로거는 자기 핸들러를 두지
# 않고 루트로 올려 보내지만, 필터는 레코드를 만든 로거에서 먼저 도므로
# 여기에 걸어야 args 가 문자열로 남아 있는 동안 가릴 수 있다.
logging.getLogger("httpx").addFilter(_CoordinateRedactingFilter())


def _configure_logging(level: str) -> None:
    """루트 로거에 stdout 핸들러를 붙인다(핸들러가 없을 때만).

    uvicorn 의 기본 로깅 설정은 `uvicorn*` 로거만 구성하고 루트 로거는
    건드리지 않는다. 그래서 이 함수 없이는 `app.*` 로거로 남긴 기록이
    출력 대상을 못 찾아 전량 유실된다 — 폴링 성공·실패, 외부 API 오류,
    캐시 적중이 모두 보이지 않게 된다.

    좌표 가림 필터를 이 핸들러에도 건다. 접근 로그에만 걸어 두면 애플리케이션
    로그로 나가는 좌표는 그대로 남아, 로그에서 위치를 지운다는 규칙이 반쪽이 된다.

    이미 핸들러가 있으면(uvicorn `--log-config`, 테스트 하니스, 상위 호스트가
    설정한 경우) 그 설정을 존중하고 아무것도 하지 않는다 — 핸들러를 덧붙이면
    같은 로그가 두 줄씩 출력된다.
    """
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(_CoordinateRedactingFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())


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
    # 루트 로거를 가장 먼저 세운다. 이 아래 단계에서 나는 기록(부팅 실패 사유,
    # 스케줄러 기동, 클라이언트 스텁 전환)이 전부 app.* 로거를 쓴다.
    _configure_logging(settings.LOG_LEVEL)

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

    # 장소 사진(Google) 클라이언트. 키가 비어 있으면 스텁으로 동작하므로
    # 클라이언트를 만들지 않는다.
    google_key = settings.GOOGLE_MAPS_API_KEY.get_secret_value()
    google = (
        None
        if places_stub_active(google_key)
        else GooglePlacesClient(google_key)
    )
    set_google_client(google)

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

    # 지하철 경로(ODsay) 클라이언트. 키가 비어 있으면 스텁으로 동작하므로
    # 클라이언트를 만들지 않는다. 예비 키를 채워 두면 두 번째 클라이언트를
    # 함께 만들어, 주 키가 막혔을 때 한 번 더 시도할 수 있게 한다.
    odsay_key = settings.ODSAY_API_KEY.get_secret_value()
    odsay = None if transit_stub_active(odsay_key) else OdsayClient(odsay_key)
    odsay_alt_key = settings.ODSAY_API_KEY_FALLBACK.get_secret_value()
    odsay_fallback = (
        OdsayClient(odsay_alt_key)
        if odsay is not None and odsay_alt_key
        else None
    )
    set_odsay_clients(odsay, odsay_fallback)

    # 따릉이 대여소 클라이언트. 키가 비어 있으면 스텁으로 동작하므로
    # 클라이언트를 만들지 않는다.
    seoul_key = settings.SEOUL_OPENAPI_KEY.get_secret_value()
    seoul_bike = (
        None if places_stub_active(seoul_key) else SeoulBikeClient(seoul_key)
    )
    set_seoul_bike_client(seoul_bike)

    # 공유 킥보드 클라이언트. 전용 키가 비어 있으면 기상청 키를 그대로 쓴다 —
    # 같은 발급처의 한 계정 키로 여러 서비스가 열려 있는 경우가 흔하다.
    pm_key = settings.PM_SERVICE_KEY.get_secret_value() or kma_key
    pm = None if places_stub_active(pm_key) else PmClient(pm_key)
    set_pm_client(pm)

    # 부팅 직후 1회 즉시 폴링/코스 동기화. create_task 결과를 강참조로
    # 보관하지 않으면 이벤트 루프가 약참조만 들고 있어, await asyncio.sleep
    # 구간 등에서 GC 가 실행 중 태스크를 수거할 수 있다. app.state 집합에
    # 담고 완료 시 자동 제거한다.
    _app.state.bg_tasks = set()

    def _retire(task: asyncio.Task) -> None:
        """태스크를 집합에서 빼면서, 죽은 이유가 있으면 남긴다.

        결과를 아무도 확인하지 않으면 파이썬은 GC 시점에야
        "Task exception was never retrieved" 를 흘린다. 폴링이 예외로
        멎었다는 사실이 몇 분 뒤 엉뚱한 자리에 뜨거나 아예 묻힌다.
        """
        _app.state.bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "startup task %s failed: %s",
                task.get_name(), exc, exc_info=exc,
            )

    # 실황·대기오염도 부팅 때 한 번 받아 둔다. 조회는 저장된 값만 읽으므로,
    # 이것 없이 뜨면 첫 폴링이 도는 시각까지 두 항목이 화면에서 비어 있다.
    for _name, _coro in (
        (SHORT_TASK_NAME, short_term_polling_loop()),
        (MID_TASK_NAME, mid_term_polling_loop()),
        (NOWCAST_TASK_NAME, nowcast_polling_loop()),
        (AIR_TASK_NAME, air_polling_loop()),
        ("durunubi_sync", durunubi_sync_loop()),
    ):
        _task = asyncio.create_task(_coro, name=_name)
        _app.state.bg_tasks.add(_task)
        _task.add_done_callback(_retire)
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
        if google is not None:
            await google.aclose()
        if osrm_foot is not None:
            await osrm_foot.aclose()
        if osrm_bicycle is not None:
            await osrm_bicycle.aclose()
        if kma_now is not None:
            await kma_now.aclose()
        if airkorea is not None:
            await airkorea.aclose()
        if odsay is not None:
            await odsay.aclose()
        if odsay_fallback is not None:
            await odsay_fallback.aclose()
        if seoul_bike is not None:
            await seoul_bike.aclose()
        if pm is not None:
            await pm.aclose()
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
