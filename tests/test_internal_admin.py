"""internal_admin_router (grids 토글 · forbidden_zones CRUD) 라우터 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않고,
새 FastAPI 에 internal_admin_router 만 include 해 라우트만 검증한다. DB 접근
(admin_ops_repo)은 monkeypatch 로 대체한다(실 DB 불필요).

다루는 범위:
  - internal_guard 403 (신뢰되지 않는 TestClient IP / 토큰 미첨부)
  - guard 우회(dependency_overrides) 후 grid 토글 성공/멱등/404
  - forbidden_zones 목록/단건/생성/교체/삭제 성공 및 404
  - 잘못된 GeoJSON(GeometryError) → 422
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import admin_ops_repo
from app.db.admin_ops_repo import GeometryError
from app.routers.internal_admin_router import router as internal_admin_router
from app.routers.internal_router import internal_guard

_NOW = datetime(2026, 7, 13, 0, 0, 0, tzinfo=timezone.utc)


def _grid(grid_id=1, is_active=True) -> dict:
    """subscribed_grids 한 행(GridRow 스키마와 정합)."""
    return {
        "grid_id": grid_id,
        "label": "서울특별시",
        "admin_code": "1100000000",
        "lat": 37.5665,
        "lng": 126.9780,
        "nx": 60,
        "ny": 127,
        "mid_land_reg_id": "11B00000",
        "mid_temp_reg_id": "11B10101",
        "is_active": is_active,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _zone(zone_id=1) -> dict:
    """forbidden_zones 한 행(ForbiddenZone 스키마와 정합, geometry=GeoJSON)."""
    return {
        "zone_id": zone_id,
        "name": "공사구역",
        "reason": "안전",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[126.9, 37.5], [127.0, 37.5], [127.0, 37.6], [126.9, 37.5]]],
        },
        "created_at": _NOW,
    }


_POLYGON_GEOJSON = {
    "type": "Polygon",
    "coordinates": [[[126.9, 37.5], [127.0, 37.5], [127.0, 37.6], [126.9, 37.5]]],
}


def _guarded_client() -> TestClient:
    """가드 그대로(우회 없음) — 403 검증용."""
    app = FastAPI()
    app.include_router(internal_admin_router)
    return TestClient(app)


def _open_client() -> TestClient:
    """internal_guard 를 no-op 으로 override — 성공 경로 검증용."""
    app = FastAPI()
    app.include_router(internal_admin_router)
    app.dependency_overrides[internal_guard] = lambda: None
    return TestClient(app)


# ── 가드 403 매트릭스 ────────────────────────────────────────────────

def test_guard_denies_untrusted_client():
    """TestClient IP('testclient')는 신뢰 CIDR 밖 → 모든 라우트 403."""
    c = _guarded_client()
    assert c.patch("/internal/grids/1", json={"is_active": False}).status_code == 403
    assert c.get("/internal/forbidden-zones").status_code == 403
    assert c.post("/internal/forbidden-zones", json={
        "name": "x", "geometry": _POLYGON_GEOJSON}).status_code == 403


# ── grids 토글 ───────────────────────────────────────────────────────

def test_grid_toggle_changes(monkeypatch):
    """활성→비활성 토글: before/after 반환 + changed=True."""
    async def fake_get(gid):
        return _grid(gid, is_active=True)

    async def fake_set(gid, active):
        return _grid(gid, is_active=active)

    monkeypatch.setattr(admin_ops_repo, "get_grid", fake_get)
    monkeypatch.setattr(admin_ops_repo, "set_grid_active", fake_set)

    resp = _open_client().patch("/internal/grids/1", json={"is_active": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["changed"] is True
    assert body["before"]["is_active"] is True
    assert body["after"]["is_active"] is False


def test_grid_toggle_idempotent(monkeypatch):
    """이미 활성인 격자를 활성으로 토글: changed=False(멱등)."""
    async def fake_get(gid):
        return _grid(gid, is_active=True)

    async def fake_set(gid, active):
        return _grid(gid, is_active=active)

    monkeypatch.setattr(admin_ops_repo, "get_grid", fake_get)
    monkeypatch.setattr(admin_ops_repo, "set_grid_active", fake_set)

    resp = _open_client().patch("/internal/grids/1", json={"is_active": True})
    assert resp.status_code == 200
    assert resp.json()["changed"] is False


def test_grid_toggle_not_found(monkeypatch):
    """대상 격자 없음 → 404."""
    async def fake_get(gid):
        return None

    monkeypatch.setattr(admin_ops_repo, "get_grid", fake_get)
    resp = _open_client().patch("/internal/grids/999", json={"is_active": False})
    assert resp.status_code == 404


# ── forbidden_zones CRUD ─────────────────────────────────────────────

def test_zones_list(monkeypatch):
    async def fake_list():
        return [_zone(1), _zone(2)]

    monkeypatch.setattr(admin_ops_repo, "list_forbidden_zones", fake_list)
    resp = _open_client().get("/internal/forbidden-zones")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["geometry"]["type"] == "Polygon"


def test_zone_get_not_found(monkeypatch):
    async def fake_get(zid):
        return None

    monkeypatch.setattr(admin_ops_repo, "get_forbidden_zone", fake_get)
    assert _open_client().get("/internal/forbidden-zones/9").status_code == 404


def test_zone_create_ok(monkeypatch):
    async def fake_create(name, reason, geometry):
        return _zone(7)

    monkeypatch.setattr(admin_ops_repo, "create_forbidden_zone", fake_create)
    resp = _open_client().post(
        "/internal/forbidden-zones",
        json={"name": "공사구역", "reason": "안전", "geometry": _POLYGON_GEOJSON},
    )
    assert resp.status_code == 201
    assert resp.json()["zone_id"] == 7


def test_zone_create_invalid_geometry(monkeypatch):
    """GeometryError → 422."""
    async def fake_create(name, reason, geometry):
        raise GeometryError("geometry must be a Polygon (got LINESTRING)")

    monkeypatch.setattr(admin_ops_repo, "create_forbidden_zone", fake_create)
    resp = _open_client().post(
        "/internal/forbidden-zones",
        json={"name": "x", "geometry": {"type": "LineString", "coordinates": []}},
    )
    assert resp.status_code == 422


def test_zone_update_not_found(monkeypatch):
    async def fake_update(zid, name, reason, geometry):
        return None

    monkeypatch.setattr(admin_ops_repo, "update_forbidden_zone", fake_update)
    resp = _open_client().put(
        "/internal/forbidden-zones/9",
        json={"name": "x", "geometry": _POLYGON_GEOJSON},
    )
    assert resp.status_code == 404


def test_zone_delete_ok_and_404(monkeypatch):
    async def fake_delete_ok(zid):
        return True

    async def fake_delete_missing(zid):
        return False

    monkeypatch.setattr(admin_ops_repo, "delete_forbidden_zone", fake_delete_ok)
    assert _open_client().delete("/internal/forbidden-zones/1").status_code == 200

    monkeypatch.setattr(admin_ops_repo, "delete_forbidden_zone", fake_delete_missing)
    assert _open_client().delete("/internal/forbidden-zones/9").status_code == 404
