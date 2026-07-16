"""/v1/directions/batch (OSRM 경로) 라우터·정규화·단순화 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
대신 새 FastAPI 에 hub_router 만 include 해 TestClient 로 라우트만 검증한다.
conftest 는 OSRM base URL 을 채우지 않으므로 라우트는 스텁 경로로 동작하며,
캐시(get_place_cache)는 lifespan 미기동 상태에서 None 이라 자연히 우회된다.

다루는 범위:
  - 스텁 모드 200 응답 형태 + 결정성
  - legs 경계(0/21)·mode literal·좌표/이름 결측 검증 실패(422)
  - OsrmClient._normalize_route 의 [lng,lat]→[lat,lng] 스왑·거리/시간 추출
    (MockTransport 순수 단위) 및 code!="Ok"/500점 단순화
  - _route_cache_key 결정성(같은 입력 → 같은 키)
  - polyline_simplify.simplify 순수 단위
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clients.hub_clients import OsrmApiError, OsrmClient
from app.routers.hub_routers import _route_cache_key
from app.routers.hub_routers import router as hub_router
from app.schemas.hub_schemas import DirectionsLeg
from app.utils.polyline_simplify import simplify


def _client() -> TestClient:
    """hub_router 만 실은 새 FastAPI 의 TestClient 를 만든다."""
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


def _leg(
    slat=37.5796, slng=126.9770, glat=37.5826, glng=126.9831
) -> dict:
    """배치 요청 본문의 leg 하나를 만든다."""
    return {
        "start": {"lat": slat, "lng": slng},
        "goal": {"lat": glat, "lng": glng},
        "start_name": "경복궁",
        "goal_name": "북촌한옥마을",
    }


# ── 스텁 모드 200 형태 / 결정성 ─────────────────────────────────────

def test_directions_stub_shape():
    """base URL 미설정 → 스텁 경로로 200 과 기대 형태를 반환한다."""
    body = {"mode": "walk", "legs": [_leg(), _leg(37.60, 127.00, 37.61, 127.01)]}
    resp = _client().post("/v1/directions/batch", json=body)
    assert resp.status_code == 200
    routes = resp.json()["routes"]
    assert len(routes) == 2  # legs 와 같은 길이
    r0 = routes[0]
    assert r0 is not None
    # 스텁은 지그재그 4점(출발+굴곡2+도착).
    assert len(r0["path"]) == 4
    # 첫 점=출발, 끝 점=도착 좌표.
    assert r0["path"][0] == [37.5796, 126.9770]
    assert r0["path"][-1] == [37.5826, 126.9831]
    assert r0["distance_m"] > 0
    assert r0["duration_s"] > 0


def test_directions_stub_deterministic():
    """같은 입력은 같은 스텁 결과를 낸다(결정적)."""
    body = {"mode": "bicycle", "legs": [_leg()]}
    c = _client()
    a = c.post("/v1/directions/batch", json=body).json()
    b = c.post("/v1/directions/batch", json=body).json()
    assert a == b


def test_directions_stub_identical_points_no_bend():
    """출발=도착이면 굴곡 없이 두 점만 반환한다(0길이 무해)."""
    body = {"mode": "walk", "legs": [_leg(37.5, 127.0, 37.5, 127.0)]}
    r0 = _client().post("/v1/directions/batch", json=body).json()["routes"][0]
    assert len(r0["path"]) == 2
    assert r0["distance_m"] == 0


# ── 검증 실패(422) ──────────────────────────────────────────────────

def test_directions_empty_legs_422():
    """legs 는 최소 1개이므로 빈 배열은 422 이다."""
    resp = _client().post(
        "/v1/directions/batch", json={"mode": "walk", "legs": []}
    )
    assert resp.status_code == 422


def test_directions_too_many_legs_422():
    """legs 는 최대 20개이므로 21개는 422 이다."""
    resp = _client().post(
        "/v1/directions/batch",
        json={"mode": "walk", "legs": [_leg()] * 21},
    )
    assert resp.status_code == 422


def test_directions_bad_mode_422():
    """mode 는 walk/bicycle/scooter 외 값(bus)이면 422 이다."""
    resp = _client().post(
        "/v1/directions/batch", json={"mode": "bus", "legs": [_leg()]}
    )
    assert resp.status_code == 422


def test_directions_missing_goal_name_422():
    """leg 의 goal_name 이 없으면 422 이다."""
    leg = _leg()
    del leg["goal_name"]
    resp = _client().post(
        "/v1/directions/batch", json={"mode": "walk", "legs": [leg]}
    )
    assert resp.status_code == 422


def test_directions_lat_out_of_korea_range_422():
    """좌표가 한국 위도 범위(33~43) 밖이면 422 이다."""
    resp = _client().post(
        "/v1/directions/batch",
        json={"mode": "walk", "legs": [_leg(slat=10.0)]},
    )
    assert resp.status_code == 422


def test_directions_lng_out_of_korea_range_422():
    """좌표가 한국 경도 범위(124~132) 밖이면 422 이다."""
    resp = _client().post(
        "/v1/directions/batch",
        json={"mode": "walk", "legs": [_leg(glng=200.0)]},
    )
    assert resp.status_code == 422


# ── OsrmClient._normalize_route (MockTransport 순수 단위) ────────────

def _osrm_ok_body(coords: list[list[float]], distance=1420.0, duration=1230.0):
    """OSRM 정상 응답(code=Ok) 본문을 만든다. coords 는 [lng,lat] 목록."""
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": distance,
                "duration": duration,
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        ],
    }


def test_osrm_normalize_swaps_and_extracts():
    """[lng,lat] → [lat,lng] 스왑, distance/duration 정수 추출."""
    coords = [[126.9770, 37.5796], [126.9800, 37.5810], [126.9831, 37.5826]]
    body = _osrm_ok_body(coords, distance=1420.4, duration=1229.6)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = OsrmClient("http://osrm-foot:5000")
    client._client = httpx.AsyncClient(
        base_url="http://osrm-foot:5000",
        transport=httpx.MockTransport(handler),
    )
    try:
        out = asyncio.run(client.route(37.5796, 126.9770, 37.5826, 126.9831))
    finally:
        asyncio.run(client.aclose())
    # 스왑 확인: 첫 점은 [lat,lng].
    assert out["path"][0] == [37.5796, 126.9770]
    assert out["path"][-1] == [37.5826, 126.9831]
    assert out["distance_m"] == 1420  # round(1420.4)
    assert out["duration_s"] == 1230  # round(1229.6)


def test_osrm_normalize_simplifies_over_200():
    """500점 지오메트리는 ≤200점으로 단순화된다."""
    # 위도만 미세 증가하는 준-직선(단순화가 강하게 먹도록) 500점.
    coords = [[126.977 + i * 1e-6, 37.5796 + i * 1e-5] for i in range(500)]
    body = _osrm_ok_body(coords)

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = OsrmClient("http://osrm-foot:5000")
    client._client = httpx.AsyncClient(
        base_url="http://osrm-foot:5000",
        transport=httpx.MockTransport(handler),
    )
    try:
        out = asyncio.run(client.route(37.5796, 126.977, 37.58455, 126.9775))
    finally:
        asyncio.run(client.aclose())
    assert len(out["path"]) <= 200
    assert len(out["path"]) >= 2


def test_osrm_code_not_ok_raises():
    """code!="Ok"(NoRoute 등)는 OsrmApiError 로 변환된다."""
    body = {"code": "NoRoute", "message": "no route found", "routes": []}

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = OsrmClient("http://osrm-foot:5000")
    client._client = httpx.AsyncClient(
        base_url="http://osrm-foot:5000",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OsrmApiError) as ei:
            asyncio.run(client.route(37.5, 127.0, 37.6, 127.1))
        assert ei.value.code == "CODE_NoRoute"
    finally:
        asyncio.run(client.aclose())


def test_osrm_http_500_raises():
    """200 이외 응답은 OsrmApiError(HTTP_<status>)로 변환된다."""

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    client = OsrmClient("http://osrm-foot:5000")
    client._client = httpx.AsyncClient(
        base_url="http://osrm-foot:5000",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OsrmApiError) as ei:
            asyncio.run(client.route(37.5, 127.0, 37.6, 127.1))
        assert ei.value.code == "HTTP_500"
    finally:
        asyncio.run(client.aclose())


# ── 캐시 키 결정성 ───────────────────────────────────────────────────

def test_route_cache_key_deterministic_and_profile():
    """같은 leg 는 같은 키, walk/scooter 는 프로파일이 달라 프리픽스가 다르다."""
    leg = DirectionsLeg(**_leg())
    k1 = _route_cache_key("walk", leg)
    k2 = _route_cache_key("walk", leg)
    assert k1 == k2
    assert k1.startswith("osrm:foot:")
    # scooter/bicycle 은 bicycle 프로파일 캐시를 공유.
    assert _route_cache_key("scooter", leg).startswith("osrm:bicycle:")
    assert _route_cache_key("bicycle", leg) == _route_cache_key("scooter", leg)
    # 다른 좌표 → 다른 키.
    other = DirectionsLeg(**_leg(37.60, 127.00, 37.61, 127.01))
    assert _route_cache_key("walk", other) != k1


# ── polyline_simplify 순수 단위 ─────────────────────────────────────

def test_simplify_collapses_straight_line():
    """완전한 직선 위 다수 점은 양 끝 2점으로 줄어든다."""
    pts = [[37.5 + i * 0.001, 127.0 + i * 0.001] for i in range(10)]
    out = simplify(pts)
    assert out == [[37.5, 127.0], [37.5 + 9 * 0.001, 127.0 + 9 * 0.001]]


def test_simplify_keeps_significant_bend():
    """임계값 이상 벗어난 굴곡점은 보존된다."""
    pts = [[0.0, 0.0], [0.5, 0.5], [1.0, 0.0]]  # 뾰족한 꼭짓점
    out = simplify(pts)
    assert out == pts


def test_simplify_enforces_max_points():
    """지그재그로 DP 가 못 줄여도 max_points 로 상한을 강제한다."""
    # 인접 점마다 방향이 바뀌어 DP 로 거의 못 줄이는 톱니 패턴 400점.
    pts = [[i * 0.001, (i % 2) * 0.01] for i in range(400)]
    out = simplify(pts, max_points=50)
    assert len(out) <= 50
    assert out[0] == pts[0]
    assert out[-1] == pts[-1]


def test_simplify_short_input_unchanged():
    """2점 이하는 그대로 반환한다."""
    assert simplify([[1.0, 2.0]]) == [[1.0, 2.0]]
    assert simplify([[1.0, 2.0], [3.0, 4.0]]) == [[1.0, 2.0], [3.0, 4.0]]
