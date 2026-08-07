"""hub_data 운영(admin) 쓰기 레이어 — grids 토글 · forbidden_zones CRUD.

map-service-admin(운영 콘솔)이 /internal/* 로 위임하는 hub_data 쓰기를
수행한다. hub 는 hub_data 의 유일한 쓰기 소유자이므로, admin 은
DB 를 직접 쓰지 않고 본 레이어를 경유한다.

호출 관계:
  - app.routers.internal_admin_router 의 grids/forbidden-zones 핸들러 →
    본 모듈 함수

에러 규약:
  - 대상 미존재 → None 반환(라우터가 404 로 매핑)
  - 잘못된 GeoJSON/유효하지 않은 지오메트리 → GeometryError(라우터가 422)
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db.hub_db import get_hub_db

logger = logging.getLogger(__name__)


class GeometryError(ValueError):
    """GeoJSON 파싱 실패 또는 지오메트리 유효성 위반(→ 422)."""


# ---------------------------------------------------------------------------
# subscribed_grids — 폴링 대상 활성/비활성 토글
# ---------------------------------------------------------------------------

_GRID_COLS = (
    "grid_id, label, admin_code, lat, lng, nx, ny, "
    "mid_land_reg_id, mid_temp_reg_id, is_active, created_at, updated_at"
)


async def get_grid(grid_id: int) -> dict[str, Any] | None:
    """구독 격자 한 행을 조회한다(없으면 None)."""
    sql = text(f"SELECT {_GRID_COLS} FROM hub_data.subscribed_grids WHERE grid_id = :gid")
    async with get_hub_db().session() as s:
        row = (await s.execute(sql, {"gid": grid_id})).mappings().first()
    return dict(row) if row is not None else None


async def set_grid_active(grid_id: int, is_active: bool) -> dict[str, Any] | None:
    """subscribed_grids.is_active 를 갱신하고 갱신된 행을 반환한다.

    대상 grid_id 가 없으면 None(라우터 404). updated_at 을 now() 로 갱신한다.
    """
    sql = text(
        f"""
        UPDATE hub_data.subscribed_grids
           SET is_active = :active, updated_at = now()
         WHERE grid_id = :gid
        RETURNING {_GRID_COLS}
        """
    )
    async with get_hub_db().session() as s:
        row = (
            await s.execute(sql, {"gid": grid_id, "active": is_active})
        ).mappings().first()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# forbidden_zones — 금지구역(폴리곤) CRUD
# ---------------------------------------------------------------------------

# GeoJSON → geometry(4326) 변환식. GeoJSON 에 crs 가 없으면 SRID 0 이 되므로
# ST_SetSRID 로 4326 을 강제한다.
_GEOM_FROM_GEOJSON = "ST_SetSRID(ST_GeomFromGeoJSON(:gj), 4326)"
# geometry → GeoJSON(json) 변환식(응답용).
_ZONE_SELECT = (
    "zone_id, name, reason, ST_AsGeoJSON(zone)::json AS geometry, created_at"
)


async def _validate_polygon(s, geojson: dict[str, Any]) -> None:
    """GeoJSON 이 유효한 Polygon(4326)인지 검사한다(실패 시 GeometryError).

    같은 트랜잭션 세션 s 에서 검증 쿼리를 실행한다. ST_GeomFromGeoJSON 이
    파싱에 실패하면 DBAPIError 가 나며 이를 GeometryError 로 변환한다.
    """
    try:
        chk = (
            await s.execute(
                text(
                    f"SELECT ST_IsValid(g) AS valid, GeometryType(g) AS gtype "
                    f"FROM (SELECT {_GEOM_FROM_GEOJSON} AS g) t"
                ),
                {"gj": json.dumps(geojson)},
            )
        ).mappings().first()
    except DBAPIError as exc:  # ST_GeomFromGeoJSON 파싱 실패 등
        raise GeometryError("invalid GeoJSON geometry") from exc
    if chk is None or not chk["valid"]:
        raise GeometryError("geometry is not valid (ST_IsValid=false)")
    if (chk["gtype"] or "").upper() != "POLYGON":
        raise GeometryError(f"geometry must be a Polygon (got {chk['gtype']})")


async def list_forbidden_zones() -> list[dict[str, Any]]:
    """모든 금지구역을 zone_id 순으로 조회한다."""
    sql = text(
        f"SELECT {_ZONE_SELECT} FROM hub_data.forbidden_zones ORDER BY zone_id"
    )
    async with get_hub_db().session() as s:
        rows = (await s.execute(sql)).mappings().all()
    return [dict(r) for r in rows]


async def get_forbidden_zone(zone_id: int) -> dict[str, Any] | None:
    """금지구역 한 건을 조회한다(없으면 None)."""
    sql = text(
        f"SELECT {_ZONE_SELECT} FROM hub_data.forbidden_zones WHERE zone_id = :zid"
    )
    async with get_hub_db().session() as s:
        row = (await s.execute(sql, {"zid": zone_id})).mappings().first()
    return dict(row) if row is not None else None


async def create_forbidden_zone(
    name: str, reason: str | None, geometry: dict[str, Any]
) -> dict[str, Any]:
    """금지구역을 생성하고 생성된 행(GeoJSON 포함)을 반환한다.

    geometry 는 유효한 Polygon 이어야 한다(아니면 GeometryError → 422).
    """
    async with get_hub_db().session() as s:
        await _validate_polygon(s, geometry)
        row = (
            await s.execute(
                text(
                    f"""
                    INSERT INTO hub_data.forbidden_zones (name, reason, zone)
                    VALUES (:name, :reason, {_GEOM_FROM_GEOJSON})
                    RETURNING {_ZONE_SELECT}
                    """
                ),
                {"name": name, "reason": reason, "gj": json.dumps(geometry)},
            )
        ).mappings().first()
    return dict(row)


async def update_forbidden_zone(
    zone_id: int, name: str, reason: str | None, geometry: dict[str, Any]
) -> dict[str, Any] | None:
    """금지구역을 전체 교체(PUT)한다. 대상 없으면 None(404)."""
    async with get_hub_db().session() as s:
        await _validate_polygon(s, geometry)
        row = (
            await s.execute(
                text(
                    f"""
                    UPDATE hub_data.forbidden_zones
                       SET name = :name, reason = :reason, zone = {_GEOM_FROM_GEOJSON}
                     WHERE zone_id = :zid
                    RETURNING {_ZONE_SELECT}
                    """
                ),
                {
                    "zid": zone_id,
                    "name": name,
                    "reason": reason,
                    "gj": json.dumps(geometry),
                },
            )
        ).mappings().first()
    return dict(row) if row is not None else None


async def delete_forbidden_zone(zone_id: int) -> bool:
    """금지구역을 삭제한다. 삭제된 행이 있으면 True, 없으면 False(404)."""
    async with get_hub_db().session() as s:
        res = await s.execute(
            text("DELETE FROM hub_data.forbidden_zones WHERE zone_id = :zid"),
            {"zid": zone_id},
        )
    return (res.rowcount or 0) > 0
