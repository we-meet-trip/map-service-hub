"""걷기/자전거 코스 데이터를 주기적으로 사전 적재하는 동기화 잡.

코스 목록을 페이지 단위로 모두 가져와 각 코스의 GPX 에서 대표 좌표와
경계 상자를 계산한 뒤 hub_data.places 에 upsert 한다. 좌표를 못 구한
코스는 지도에 표현할 수 없으므로 건너뛴다.

호출 관계:
  - app.main.lifespan 이 startup 1회 즉시 실행
  - app.scheduler.hub_scheduler.build_scheduler 가 주기 실행으로 등록
"""
from __future__ import annotations

import logging

import httpx

from app.clients.hub_clients import DurunubiApiError, DurunubiClient
from app.config import settings
from app.db.places_repo import count_courses, upsert_course
from app.place_stubs import durunubi_course_stub, places_stub_active
from app.utils.gpx import fetch_geometry

logger = logging.getLogger(__name__)

# 한 번의 동기화에서 가져올 최대 페이지 수(폭주 방지 상한).
_MAX_PAGES = 50


def _to_int(v: object) -> int | None:
    """임의 값을 안전하게 int 로 변환한다(실패 시 None)."""
    try:
        return int(float(v))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_float(v: object) -> float | None:
    """임의 값을 안전하게 float 로 변환한다(실패 시 None)."""
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _course_category(brd_div: str | None) -> str | None:
    """걷기/자전거 구분 코드를 사람이 읽는 분류 문자열로 바꾼다."""
    if brd_div == "DNBW":
        return "자전거길"
    if brd_div == "DNWW":
        return "걷기길"
    return None


async def _build_course(
    item: dict, gpx_client: httpx.AsyncClient
) -> dict | None:
    """코스 raw item 을 적재용 정규화 dict 로 만든다.

    GPX 좌표를 못 구하면 None 을 돌려 호출자가 건너뛰게 한다.
    """
    crs_idx = item.get("crsIdx")
    gpx_url = item.get("gpxpath")
    if not crs_idx or not gpx_url:
        return None
    geom = await fetch_geometry(gpx_client, gpx_url)
    if geom is None:
        return None
    brd_div = item.get("brdDiv")
    return {
        "content_id": f"durunubi:{crs_idx}",
        "name": item.get("crsKorNm") or crs_idx,
        "address": item.get("sigun") or "",
        "lat": geom.start_lat,
        "lng": geom.start_lng,
        "category": _course_category(brd_div),
        "crs_dstnc_km": _to_float(item.get("crsDstnc")),
        "crs_total_min": _to_int(item.get("crsTotlRqrmHour")),
        "crs_level": _to_int(item.get("crsLevel")),
        "brd_div": brd_div,
        "gpx_url": gpx_url,
        "route_idx": item.get("routeIdx") or None,
        "bbox_min_lat": geom.min_lat,
        "bbox_min_lng": geom.min_lng,
        "bbox_max_lat": geom.max_lat,
        "bbox_max_lng": geom.max_lng,
    }


async def _safe_upsert(course: dict) -> bool:
    """코스 한 건을 적재하되 실패는 흡수한다(전체 동기화 중단 방지).

    한 row 의 DB 오류가 나머지 코스 적재까지 막지 않도록 개별 예외를
    경고로만 남긴다.
    """
    try:
        await upsert_course(course)
        return True
    except Exception as e:  # noqa: BLE001 - 한 건 실패가 전체를 막지 않게 흡수
        logger.warning(
            "durunubi upsert failed id=%s err=%s",
            course.get("content_id"), e,
        )
        return False


async def durunubi_sync_loop(force: bool = False) -> None:
    """코스 전수를 한 번 동기화한다.

    force: 부팅 시(False)에는 이미 적재돼 있으면 재다운로드를 건너뛰어
        재기동 루프가 외부에 반복 요청하는 것을 막는다. 주기 동기화(True)는
        변경 반영을 위해 항상 다시 적재한다.

    키가 없으면 스텁 코스를 적재한다. 키가 있으면 코스 목록을 페이지로
    순회하며 각 코스의 GPX 좌표를 채워 upsert 한다. 한 코스/페이지의
    실패는 경고만 남기고 가능한 범위까지 계속 진행한다.

    호출처:
      - app.main.lifespan (startup 1회 즉시 실행, force=False)
      - build_scheduler 가 등록한 주기 잡 (force=True)
    """
    if not force:
        existing = await count_courses()
        if existing > 0:
            logger.info(
                "durunubi sync skipped (already loaded=%d)", existing
            )
            return

    key = settings.TOUR_API_SERVICE_KEY.get_secret_value()
    if places_stub_active(key):
        for course in durunubi_course_stub():
            await _safe_upsert(course)
        logger.info(
            "durunubi sync (stub) total=%d", await count_courses()
        )
        return

    num_rows = settings.DURUNUBI_NUMOFROWS
    loaded = 0
    async with DurunubiClient(key) as client, httpx.AsyncClient(
        timeout=settings.DURUNUBI_GPX_TIMEOUT_SEC
    ) as gpx_client:
        for page in range(1, _MAX_PAGES + 1):
            try:
                items = await client.fetch_courses(
                    num_rows=num_rows, page_no=page
                )
            except DurunubiApiError as e:
                logger.warning(
                    "durunubi page=%d fetch failed code=%s msg=%s",
                    page, e.code, e.msg,
                )
                break
            if not items:
                break
            for item in items:
                course = await _build_course(item, gpx_client)
                if course is None:
                    continue
                if await _safe_upsert(course):
                    loaded += 1
            # 마지막 페이지(요청 수보다 적게 옴)면 종료.
            if len(items) < num_rows:
                break
    logger.info(
        "durunubi sync done upserted=%d total=%d",
        loaded, await count_courses(),
    )
