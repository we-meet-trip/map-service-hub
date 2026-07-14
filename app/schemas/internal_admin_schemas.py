"""내부 운영(admin) 엔드포인트 요청/응답 스키마.

map-service-admin(운영 콘솔)이 hub 의 hub_data 쓰기를 위임 호출할 때
사용하는 /internal/* (grids 토글·forbidden_zones CRUD)의 Pydantic 모델.

경계(SoT B9): hub 는 hub_data 의 유일한 쓰기 소유자이며, admin 은 본
스키마로 직렬화된 요청을 internal_guard(CIDR+X-Internal-Token) 뒤에서만
호출한다. admin 은 변경 전/후(before/after) 스냅샷을 audit_logs 에 기록한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class GridToggleRequest(BaseModel):
    """subscribed_grids.is_active 토글 요청."""

    is_active: bool = Field(
        ..., description="폴링 대상 활성/비활성 (true=폴링, false=스킵)"
    )


class GridRow(BaseModel):
    """subscribed_grids 한 행 스냅샷(토글 전/후 비교용)."""

    grid_id: int
    label: str
    admin_code: str | None
    lat: float
    lng: float
    nx: int
    ny: int
    mid_land_reg_id: str | None
    mid_temp_reg_id: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class GridToggleResponse(BaseModel):
    """토글 결과. admin 이 before/after 를 감사에 기록한다."""

    ok: bool = True
    changed: bool = Field(
        ..., description="실제로 is_active 값이 바뀌었는지(멱등 판별)"
    )
    before: GridRow
    after: GridRow


class ForbiddenZoneCreate(BaseModel):
    """금지구역 생성 요청. geometry 는 GeoJSON Polygon."""

    name: str = Field(..., min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)
    geometry: dict[str, Any] = Field(
        ..., description="GeoJSON Polygon geometry 객체(예: {'type':'Polygon',...})"
    )


class ForbiddenZoneUpdate(BaseModel):
    """금지구역 전체 교체(PUT). name+geometry 필수, reason 선택."""

    name: str = Field(..., min_length=1, max_length=200)
    reason: str | None = Field(default=None, max_length=1000)
    geometry: dict[str, Any] = Field(..., description="GeoJSON Polygon geometry")


class ForbiddenZone(BaseModel):
    """금지구역 응답. geometry 는 GeoJSON(dict)로 반환."""

    zone_id: int
    name: str
    reason: str | None
    geometry: dict[str, Any]
    created_at: datetime
