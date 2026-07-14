"""룰 엔진 순수 함수 + /v1/rules/* 라우터 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
DB 없는 두 엔드포인트(mobility-radius / indoor-bonus)는 새 FastAPI 에
rules_router 만 include 해 TestClient 로 검증한다. DB 를 참조하는
forbidden-zones 는 rules_router 가 임포트한 zones_intersecting_polyline 을
monkeypatch 로 대체해 실제 DB 없이 검증한다.

다루는 범위:
  - filter_by_radius 경계값(도보 3000m 안/밖, 킥보드 7000m, car/transit 무제한)
  - indoor_bonus 강수 임계(PoP 49 vs 50, 실내/실외)
  - _linestring_wkt 의 경도-위도 순서
  - 세 엔드포인트의 정상/검증실패/503 경로
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.rules_router as rules_router_mod
from app.db.rules_repo import _linestring_wkt
from app.routers.rules_router import router as rules_router
from app.rules.rule_engine import (
    RADIUS_M,
    filter_by_radius,
    haversine_m,
    indoor_bonus,
)


def _client() -> TestClient:
    """rules_router 만 실은 새 FastAPI 의 TestClient 를 만든다."""
    app = FastAPI()
    app.include_router(rules_router)
    return TestClient(app)


# ── 순수 엔진: filter_by_radius ────────────────────────────────────

def test_radius_table_values():
    """이동수단별 반경 테이블이 사양과 일치한다."""
    assert RADIUS_M["foot"] == 3000
    assert RADIUS_M["walk"] == 3000
    assert RADIUS_M["bicycle"] == 10000
    assert RADIUS_M["kickboard"] == 7000
    assert RADIUS_M["car"] is None
    assert RADIUS_M["transit"] is None


def test_filter_radius_foot_boundary():
    """도보 3000m 경계 안의 점은 남고 밖의 점은 빠진다."""
    origin = (37.5, 127.0)
    inside = {"content_id": "in", "lat": 37.5, "lng": 127.033}
    outside = {"content_id": "out", "lat": 37.5, "lng": 127.035}
    # 사전 검증: 두 점이 3000m 경계 양쪽에 놓이는지 haversine 로 확인.
    assert haversine_m(37.5, 127.0, 37.5, 127.033) < 3000
    assert haversine_m(37.5, 127.0, 37.5, 127.035) > 3000
    filtered, radius = filter_by_radius(origin, "foot", [inside, outside])
    assert radius == 3000
    assert filtered == [inside]


def test_filter_radius_kickboard_7000():
    """도보(3000) 밖·킥보드(7000) 안의 점은 킥보드에서만 남는다."""
    origin = (37.5, 127.0)
    p = {"content_id": "k", "lat": 37.5, "lng": 127.05}
    d = haversine_m(37.5, 127.0, 37.5, 127.05)
    assert 3000 < d < 7000
    foot_filtered, _ = filter_by_radius(origin, "foot", [p])
    kick_filtered, kick_radius = filter_by_radius(origin, "kickboard", [p])
    assert foot_filtered == []
    assert kick_radius == 7000
    assert kick_filtered == [p]


def test_filter_radius_car_and_transit_pass_all():
    """car/transit 는 반경 무제한이라 좌표 검사 없이 전부 통과한다."""
    origin = (37.5, 127.0)
    cands = [
        {"content_id": "far", "lat": 40.0, "lng": 130.0},
        {"content_id": "no_coords"},  # 좌표 없어도 무제한이면 통과
    ]
    for mobility in ("car", "transit"):
        filtered, radius = filter_by_radius(origin, mobility, cands)
        assert radius is None
        assert filtered == cands


def test_filter_radius_excludes_missing_or_bad_coords():
    """반경 유한 시 좌표 없거나 깨진 후보는 방어적으로 제외된다."""
    origin = (37.5, 127.0)
    cands = [
        {"content_id": "ok", "lat": 37.5, "lng": 127.001},
        {"content_id": "no_coords"},
        {"content_id": "bad", "lat": "x", "lng": 127.0},
    ]
    filtered, radius = filter_by_radius(origin, "foot", cands)
    assert radius == 3000
    assert [c["content_id"] for c in filtered] == ["ok"]


# ── 순수 엔진: indoor_bonus ────────────────────────────────────────

def test_indoor_bonus_pop_threshold_and_indoor_flag():
    """PoP 49 는 가점 없음, 50 이상은 실내에만 +0.15."""
    pois = [
        {"content_id": "indoor", "indoor_flag": True, "base_score": 5.0},
        {"content_id": "outdoor", "indoor_flag": False, "base_score": 5.0},
    ]
    scored49 = indoor_bonus(pois, 49)
    assert scored49[0]["score"] == pytest.approx(5.0)
    assert scored49[1]["score"] == pytest.approx(5.0)

    scored50 = indoor_bonus(pois, 50)
    assert scored50[0]["score"] == pytest.approx(5.15)
    assert scored50[1]["score"] == pytest.approx(5.0)
    # 입력 순서가 보존된다.
    assert [s["content_id"] for s in scored50] == ["indoor", "outdoor"]


# ── WKT 빌더 ───────────────────────────────────────────────────────

def test_linestring_wkt_lng_lat_order():
    """WKT 는 x=경도, y=위도 순으로 좌표를 나열한다."""
    wkt = _linestring_wkt([(37.5, 127.0), (37.6, 127.1)])
    assert wkt == "LINESTRING(127.0 37.5, 127.1 37.6)"


# ── 엔드포인트: mobility-radius ────────────────────────────────────

def test_endpoint_mobility_radius_ok():
    """반경 안 후보만 남고 dropped 가 계산된다."""
    body = {
        "origin": {"lat": 37.5, "lng": 127.0},
        "mobility": "foot",
        "candidates": [
            {"content_id": "in", "lat": 37.5, "lng": 127.001},
            {"content_id": "out", "lat": 37.6, "lng": 127.2},
        ],
    }
    resp = _client().post("/v1/rules/filter/mobility-radius", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["radius_m"] == 3000
    assert [c["content_id"] for c in data["filtered"]] == ["in"]
    assert data["dropped"] == 1


def test_endpoint_mobility_radius_unknown_422():
    """알 수 없는 이동수단은 422 로 거절된다."""
    body = {
        "origin": {"lat": 37.5, "lng": 127.0},
        "mobility": "hoverboard",
        "candidates": [],
    }
    resp = _client().post("/v1/rules/filter/mobility-radius", json=body)
    assert resp.status_code == 422


def test_endpoint_mobility_radius_car_passthrough():
    """car 는 무제한이라 먼 후보도 남고 radius_m 은 None."""
    body = {
        "origin": {"lat": 37.5, "lng": 127.0},
        "mobility": "car",
        "candidates": [{"content_id": "far", "lat": 40.0, "lng": 130.0}],
    }
    resp = _client().post("/v1/rules/filter/mobility-radius", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["radius_m"] is None
    assert data["dropped"] == 0
    assert len(data["filtered"]) == 1


# ── 엔드포인트: indoor-bonus ───────────────────────────────────────

def test_endpoint_indoor_bonus_ok():
    """실내 가점이 응답 점수에 반영된다."""
    body = {
        "pois": [
            {"content_id": "a", "indoor_flag": True, "base_score": 5.0},
            {"content_id": "b", "indoor_flag": False, "base_score": 5.0},
        ],
        "day_pop_max": 50,
    }
    resp = _client().post("/v1/rules/score/indoor-bonus", json=body)
    assert resp.status_code == 200
    by_id = {s["content_id"]: s["score"] for s in resp.json()["scored"]}
    assert by_id["a"] == pytest.approx(5.15)
    assert by_id["b"] == pytest.approx(5.0)


def test_endpoint_indoor_bonus_base_score_out_of_range_422():
    """base_score 가 0~10 밖이면 422 로 거절된다."""
    body = {
        "pois": [
            {"content_id": "a", "indoor_flag": True, "base_score": 11.0}
        ],
        "day_pop_max": 50,
    }
    resp = _client().post("/v1/rules/score/indoor-bonus", json=body)
    assert resp.status_code == 422


# ── 엔드포인트: forbidden-zones (monkeypatch, DB 없음) ─────────────

def test_endpoint_forbidden_zones_intersects(monkeypatch):
    """교차 결과가 있으면 intersects=True 와 zones 를 반환한다."""
    async def fake(points):
        return ["금지구역A"]

    monkeypatch.setattr(
        rules_router_mod, "zones_intersecting_polyline", fake
    )
    body = {
        "polyline": [
            {"lat": 37.5, "lng": 127.0},
            {"lat": 37.6, "lng": 127.1},
        ]
    }
    resp = _client().post("/v1/rules/filter/forbidden-zones", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intersects"] is True
    assert data["zones"] == ["금지구역A"]
    assert data["suggested_detour"] is None


def test_endpoint_forbidden_zones_none(monkeypatch):
    """교차가 없으면 intersects=False, 빈 zones."""
    async def fake(points):
        return []

    monkeypatch.setattr(
        rules_router_mod, "zones_intersecting_polyline", fake
    )
    body = {
        "polyline": [
            {"lat": 37.5, "lng": 127.0},
            {"lat": 37.6, "lng": 127.1},
        ]
    }
    resp = _client().post("/v1/rules/filter/forbidden-zones", json=body)
    assert resp.status_code == 200
    data = resp.json()
    assert data["intersects"] is False
    assert data["zones"] == []


def test_endpoint_forbidden_zones_db_error_503(monkeypatch):
    """금지구역 백엔드 장애 시 503 을 반환한다."""
    async def boom(points):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        rules_router_mod, "zones_intersecting_polyline", boom
    )
    body = {
        "polyline": [
            {"lat": 37.5, "lng": 127.0},
            {"lat": 37.6, "lng": 127.1},
        ]
    }
    resp = _client().post("/v1/rules/filter/forbidden-zones", json=body)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "rules backend unavailable"


def test_endpoint_forbidden_zones_too_short_422():
    """폴리라인은 2점 미만이면 422 로 거절된다."""
    body = {"polyline": [{"lat": 37.5, "lng": 127.0}]}
    resp = _client().post("/v1/rules/filter/forbidden-zones", json=body)
    assert resp.status_code == 422
