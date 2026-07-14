"""내부 운영(admin) 라우터 — hub_data 쓰기 위임 엔드포인트.

map-service-admin(운영 콘솔)이 위임 호출하는 hub_data 쓰기 작업을 제공한다:
  PATCH  /internal/grids/{grid_id}         — subscribed_grids.is_active 토글
  GET    /internal/forbidden-zones         — 금지구역 목록
  GET    /internal/forbidden-zones/{id}    — 금지구역 단건
  POST   /internal/forbidden-zones         — 금지구역 생성
  PUT    /internal/forbidden-zones/{id}    — 금지구역 전체 교체
  DELETE /internal/forbidden-zones/{id}    — 금지구역 삭제

보호: 기존 internal_router.internal_guard(CIDR 화이트리스트 + X-Internal-Token
상수시간 비교)를 그대로 재사용한다(KMA run-now 와 동일 가드).

호출 관계:
  - app.main 이 본 모듈 router 를 include
  - 핸들러 → app.db.admin_ops_repo 위임
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.db import admin_ops_repo
from app.db.admin_ops_repo import GeometryError
from app.routers.internal_router import internal_guard
from app.schemas.internal_admin_schemas import (
    ForbiddenZone,
    ForbiddenZoneCreate,
    ForbiddenZoneUpdate,
    GridRow,
    GridToggleRequest,
    GridToggleResponse,
)

logger = logging.getLogger(__name__)

# KMA run-now 라우터와 동일 prefix/가드. 두 라우터가 함께 /internal/* 를 이룬다.
router = APIRouter(prefix="/internal", dependencies=[Depends(internal_guard)])


@router.patch("/grids/{grid_id}", response_model=GridToggleResponse)
async def toggle_grid(grid_id: int, body: GridToggleRequest) -> GridToggleResponse:
    """PATCH /internal/grids/{grid_id} — 폴링 구독 격자 활성/비활성 토글.

    변경 전 행(before)과 후 행(after)을 함께 반환한다. admin 은 이 둘을
    audit_logs 의 before_json/after_json 에 기록한다. 대상 격자가 없으면 404.
    changed 는 실제 is_active 값이 바뀌었는지(멱등 판별).
    """
    before = await admin_ops_repo.get_grid(grid_id)
    if before is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"subscribed_grid {grid_id} not found",
        )
    after = await admin_ops_repo.set_grid_active(grid_id, body.is_active)
    if after is None:  # 경합으로 사이에 삭제된 극단 케이스
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"subscribed_grid {grid_id} not found",
        )
    return GridToggleResponse(
        changed=before["is_active"] != after["is_active"],
        before=GridRow(**before),
        after=GridRow(**after),
    )


@router.get("/forbidden-zones", response_model=list[ForbiddenZone])
async def list_zones() -> list[ForbiddenZone]:
    """GET /internal/forbidden-zones — 전체 금지구역 조회."""
    rows = await admin_ops_repo.list_forbidden_zones()
    return [ForbiddenZone(**r) for r in rows]


@router.get("/forbidden-zones/{zone_id}", response_model=ForbiddenZone)
async def get_zone(zone_id: int) -> ForbiddenZone:
    """GET /internal/forbidden-zones/{id} — 금지구역 단건(없으면 404)."""
    row = await admin_ops_repo.get_forbidden_zone(zone_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"forbidden_zone {zone_id} not found",
        )
    return ForbiddenZone(**row)


@router.post(
    "/forbidden-zones",
    response_model=ForbiddenZone,
    status_code=status.HTTP_201_CREATED,
)
async def create_zone(body: ForbiddenZoneCreate) -> ForbiddenZone:
    """POST /internal/forbidden-zones — 금지구역 생성.

    geometry 는 유효한 GeoJSON Polygon(4326)이어야 한다. 파싱 실패/유효성
    위반 시 422.
    """
    try:
        row = await admin_ops_repo.create_forbidden_zone(
            body.name, body.reason, body.geometry
        )
    except GeometryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return ForbiddenZone(**row)


@router.put("/forbidden-zones/{zone_id}", response_model=ForbiddenZone)
async def update_zone(
    zone_id: int, body: ForbiddenZoneUpdate
) -> ForbiddenZone:
    """PUT /internal/forbidden-zones/{id} — 금지구역 전체 교체(없으면 404)."""
    try:
        row = await admin_ops_repo.update_forbidden_zone(
            zone_id, body.name, body.reason, body.geometry
        )
    except GeometryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"forbidden_zone {zone_id} not found",
        )
    return ForbiddenZone(**row)


@router.delete("/forbidden-zones/{zone_id}", status_code=status.HTTP_200_OK)
async def delete_zone(zone_id: int) -> dict[str, object]:
    """DELETE /internal/forbidden-zones/{id} — 금지구역 삭제(없으면 404)."""
    ok = await admin_ops_repo.delete_forbidden_zone(zone_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"forbidden_zone {zone_id} not found",
        )
    return {"ok": True, "deleted": zone_id}
