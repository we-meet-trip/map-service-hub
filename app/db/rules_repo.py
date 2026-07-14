"""hub_data.forbidden_zones 교차 조회 레이어.

경로 폴리라인이 금지구역(면)과 교차하는지 PostGIS 로 판정한다. 룰 엔진의
순수 함수와 달리 DB 를 참조하므로 별도 모듈로 둔다.

호출 관계:
  - app.routers.rules_router 의 forbidden-zones 엔드포인트 →
    zones_intersecting_polyline

주의: forbidden_zones 테이블이 비어 있으면 어떤 폴리라인도 교차하지 않아
빈 리스트가 반환된다(금지구역 필터 사실상 비활성).
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.db.hub_db import get_hub_db

logger = logging.getLogger(__name__)


def _linestring_wkt(points: list[tuple[float, float]]) -> str:
    """(위도, 경도) 목록을 WKT LINESTRING 문자열로 만든다.

    WKT 는 x=경도, y=위도 순이므로 "lng lat" 순으로 좌표를 나열한다.
    각 값을 float 로 변환해 숫자가 아닌 입력을 걸러낸다(WKT 주입 방지).
    """
    coords = ", ".join(
        f"{float(lng)} {float(lat)}" for lat, lng in points
    )
    return f"LINESTRING({coords})"


async def zones_intersecting_polyline(
    points: list[tuple[float, float]]
) -> list[str]:
    """폴리라인과 교차하는 금지구역 이름 목록을 조회한다.

    points: (위도, 경도) 튜플 리스트. 최소 2점 이상이어야 선(LINESTRING)이
        된다(호출부에서 길이 검증).

    반환: 교차하는 금지구역 name 리스트(교차 없음이면 빈 리스트).
    """
    wkt = _linestring_wkt(points)
    sql = text(
        """
        SELECT name
        FROM hub_data.forbidden_zones
        WHERE ST_Intersects(
          zone,
          ST_SetSRID(ST_GeomFromText(:wkt), 4326)
        )
        """
    )
    async with get_hub_db().session() as s:
        rows = (await s.execute(sql, {"wkt": wkt})).all()
    return [r.name for r in rows]
