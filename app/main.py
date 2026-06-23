"""hub-service ASGI 진입점.

본 모듈은 FastAPI 애플리케이션 객체 `app` 을 구성하고, 두 라우터
(hub_router, internal_router)와 헬스체크 엔드포인트를 등록한다.

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
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app.db.hub_db import dispose_hub_db
from app.routers.hub_routers import router as hub_router
from app.routers.internal_router import router as internal_router
from app.scheduler.hub_scheduler import (
    build_scheduler,
    mid_term_polling_loop,
    short_term_polling_loop,
)

logger = logging.getLogger(__name__)


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
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("hub: APScheduler started")
    # 부팅 직후 1회 즉시 폴링. create_task 결과를 강참조로 보관하지
    # 않으면 이벤트 루프가 약참조만 들고 있어, await asyncio.sleep 구간
    # 등에서 GC 가 실행 중 태스크를 수거할 수 있다. app.state 집합에
    # 담고 완료 시 자동 제거한다.
    _app.state.bg_tasks = set()
    for _coro in (short_term_polling_loop(), mid_term_polling_loop()):
        _task = asyncio.create_task(_coro)
        _app.state.bg_tasks.add(_task)
        _task.add_done_callback(_app.state.bg_tasks.discard)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await dispose_hub_db()
        logger.info("hub: scheduler/db disposed")


# 외부 라이브러리(uvicorn)가 임포트해야 하는 ASGI 객체.
# title/version 은 OpenAPI 문서에도 노출된다.
app = FastAPI(title="map-service-hub", version="0.0.1-poc", lifespan=lifespan)
# 외부 게이트웨이 라우터(hub_routers) → /weather 등 공개 endpoint.
app.include_router(hub_router)
# 내부 운영 라우터(internal_router) → /internal/* (CIDR 화이트리스트로 보호).
app.include_router(internal_router)


@app.get("/health")
async def health() -> dict[str, str]:
    """liveness 헬스체크.

    Dockerfile HEALTHCHECK 와 docker-compose 의 hub healthcheck 가
    본 endpoint(200 응답)로 컨테이너 상태를 판정한다.

    반환: {"status": "ok", "service": "hub"}
    """
    return {"status": "ok", "service": "hub"}
