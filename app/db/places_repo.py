"""hub_data.places 테이블 read/upsert 레이어.

코스 출처(걷기/자전거)의 사전 적재와 반경 기반 조회, 그리고 행정구역
중심 좌표 조회를 한 곳에 모은다. 점 장소 출처(카카오)는 평소 짧은 캐시로
다루므로 본 모듈은 주로 코스 데이터를 다룬다.

호출 관계:
  - app.scheduler 의 코스 동기화 → upsert_course
  - app.routers.hub_routers 의 장소 병합 조회 → lookup_region_centroid /
    search_courses_nearby
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.db.hub_db import get_hub_db

logger = logging.getLogger(__name__)


async def lookup_region_centroid(
    province: str, city: str
) -> tuple[float, float] | None:
    """(광역시도, 시군구) → 대표 중심 좌표(lat, lng) 조회.

    region_grid 에서 (lv1=province, lv2=city, lv3='') 대표 row 를 찾고,
    없으면 (lv1=province, lv2='', lv3='') 광역 대표 row 로 대체한다.
    두 단계 모두 없으면 None.

    반환: (lat, lng) 또는 None.
    """
    primary_sql = text(
        """
        SELECT lat, lon
        FROM hub_data.region_grid
        WHERE lv1 = :province AND lv2 = :city AND lv3 = ''
        LIMIT 1
        """
    )
    fallback_sql = text(
        """
        SELECT lat, lon
        FROM hub_data.region_grid
        WHERE lv1 = :province AND lv2 = '' AND lv3 = ''
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        row = (
            await s.execute(
                primary_sql, {"province": province, "city": city}
            )
        ).first()
        if row is None:
            row = (
                await s.execute(fallback_sql, {"province": province})
            ).first()
    if row is None:
        return None
    return (float(row.lat), float(row.lon))


async def upsert_course(course: dict) -> None:
    """코스 한 건을 places 테이블에 upsert 한다.

    course: 동기화 단계가 만든 정규화 dict. 다음 키를 가진다 —
        content_id, name, address, lat, lng, category, crs_dstnc_km,
        crs_total_min, crs_level, brd_div, gpx_url, route_idx,
        bbox_min_lat, bbox_min_lng, bbox_max_lat, bbox_max_lng.

    geom 은 lng/lat 로부터 만들고, content_id 충돌 시 좌표·메타·
    updated_at 을 갱신한다.
    """
    sql = text(
        """
        INSERT INTO hub_data.places
          (content_id, source, name, address, lat, lng, geom,
           category, crs_dstnc_km, crs_total_min, crs_level, brd_div,
           gpx_url, route_idx, bbox_min_lat, bbox_min_lng,
           bbox_max_lat, bbox_max_lng, updated_at)
        VALUES
          (:content_id, 'durunubi', :name, :address, :lat, :lng,
           ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
           :category, :crs_dstnc_km, :crs_total_min, :crs_level,
           :brd_div, :gpx_url, :route_idx, :bbox_min_lat, :bbox_min_lng,
           :bbox_max_lat, :bbox_max_lng, now())
        ON CONFLICT (content_id) DO UPDATE SET
          name          = EXCLUDED.name,
          address       = EXCLUDED.address,
          lat           = EXCLUDED.lat,
          lng           = EXCLUDED.lng,
          geom          = EXCLUDED.geom,
          category      = EXCLUDED.category,
          crs_dstnc_km  = EXCLUDED.crs_dstnc_km,
          crs_total_min = EXCLUDED.crs_total_min,
          crs_level     = EXCLUDED.crs_level,
          brd_div       = EXCLUDED.brd_div,
          gpx_url       = EXCLUDED.gpx_url,
          route_idx     = EXCLUDED.route_idx,
          bbox_min_lat  = EXCLUDED.bbox_min_lat,
          bbox_min_lng  = EXCLUDED.bbox_min_lng,
          bbox_max_lat  = EXCLUDED.bbox_max_lat,
          bbox_max_lng  = EXCLUDED.bbox_max_lng,
          updated_at    = now()
        """
    )
    async with get_hub_db().session() as s:
        await s.execute(sql, course)


async def count_courses() -> int:
    """적재된 코스(durunubi) 수를 센다. 동기화 결과 확인용."""
    sql = text(
        "SELECT count(*) AS c FROM hub_data.places WHERE source = 'durunubi'"
    )
    async with get_hub_db().session() as s:
        row = (await s.execute(sql)).first()
    return int(row.c) if row else 0


async def search_courses_nearby(
    lat: float,
    lng: float,
    radius_m: int,
    *,
    brd_div: str | None = None,
    limit: int = 15,
) -> list[dict]:
    """중심 좌표 주변의 코스를 가까운 순으로 조회한다.

    lat / lng: 검색 중심.
    radius_m: 검색 반경(미터). geom 을 geography 로 캐스트해 미터 단위
        거리로 비교한다.
    brd_div: 걷기("DNWW")/자전거("DNBW") 구분 필터. None 이면 전체.
    limit: 최대 반환 수.

    반환: 공용 장소 표현 dict 리스트(source="durunubi").
    """
    where_brd = "AND brd_div = :brd_div" if brd_div else ""
    sql = text(
        f"""
        SELECT
          content_id, name, address, road_address, lat, lng,
          category, category_group_code, phone, place_url,
          crs_dstnc_km, crs_total_min, crs_level, brd_div,
          gpx_url, route_idx,
          ST_Distance(
            geom::geography,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography
          ) AS distance_m
        FROM hub_data.places
        WHERE source = 'durunubi'
          AND ST_DWithin(
            geom::geography,
            ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography,
            :radius_m
          )
          {where_brd}
        ORDER BY distance_m
        LIMIT :limit
        """
    )
    params: dict = {
        "lat": lat,
        "lng": lng,
        "radius_m": radius_m,
        "limit": limit,
    }
    if brd_div:
        params["brd_div"] = brd_div
    async with get_hub_db().session() as s:
        rows = (await s.execute(sql, params)).all()
    return [
        {
            "content_id": r.content_id,
            "source": "durunubi",
            "name": r.name,
            "address": r.address,
            "road_address": r.road_address,
            "lat": float(r.lat),
            "lng": float(r.lng),
            "category": r.category,
            "category_group_code": r.category_group_code,
            "phone": r.phone,
            "place_url": r.place_url,
            "distance_m": (
                int(r.distance_m) if r.distance_m is not None else None
            ),
            "crs_dstnc_km": r.crs_dstnc_km,
            "crs_total_min": r.crs_total_min,
            "crs_level": r.crs_level,
            "brd_div": r.brd_div,
            "gpx_url": r.gpx_url,
            "route_idx": r.route_idx,
        }
        for r in rows
    ]
