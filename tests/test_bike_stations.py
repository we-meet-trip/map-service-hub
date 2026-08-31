"""/v1/mobility/bike-stations (따릉이 대여소) 라우터·클라이언트 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
대신 새 FastAPI 에 hub_router 만 include 해 TestClient 로 라우트만 검증한다.
conftest 는 서울 열린데이터 키를 채우지 않으므로 기본 경로는 스텁으로
동작하고, 캐시(get_place_cache)는 lifespan 미기동 상태에서 None 이다.

다루는 범위:
  - 스텁 모드 200 응답 형태와 요청 좌표 주변에 놓이는지
  - 좌표·반경 경계 검증 실패(422)
  - 반경 밖 대여소가 잘려 나가는지, 서비스 지역 밖이 실패가 아닌지
  - 나눠 받기의 마지막 장 판정을 버리기 전 행 수로 하는지
  - 페이지 상한에서 멈추는지
  - 도중 실패 시 받은 만큼으로 답하고, 아무것도 못 받으면 unavailable 인지
  - 캐시를 좌표와 무관하게 한 벌만 쓰는지
  - 전체 제한 시간 초과가 5xx 가 아니라 unavailable 로 흡수되는지
  - 인증키가 오류 메시지에 남지 않는지(키가 주소 경로에 실린다)
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.clients.hub_clients import SeoulBikeApiError, SeoulBikeClient
from app.config import settings
from app.place_stubs import seoul_bike_stub
from app.routers import hub_routers
from app.routers.hub_routers import router as hub_router
from app.rules.rule_engine import haversine_m
from app.schemas.hub_schemas import BikeStation

# 서울 시청. 따릉이 서비스 지역 안이면 값 자체는 검증에 영향이 없다.
_LAT = 37.5665
_LNG = 126.9780


def _client() -> TestClient:
    """hub_router 만 실은 새 FastAPI 의 TestClient 를 만든다."""
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


def _params(**over) -> dict:
    """기본 좌표가 채워진 질의 파라미터를 만든다."""
    base = {"lat": _LAT, "lng": _LNG}
    base.update(over)
    return base


def _station(idx: int, lat: float, lng: float) -> dict:
    """정규화된 대여소 한 건을 만든다."""
    return {
        "station_id": f"ST-{idx}",
        "name": f"대여소 {idx}",
        "rack_total": 10,
        "parking_bike_total": 4,
        "lat": lat,
        "lng": lng,
    }


class _FakeCache:
    """라우터가 쓰는 캐시 동작만 흉내 내는 최소 구현."""

    def __init__(self) -> None:
        self.json: dict = {}

    async def get_json(self, key: str):
        return self.json.get(key)

    async def set_json(self, key: str, value, ttl: int) -> None:
        self.json[key] = (value, ttl)

    async def incr_by(self, key: str, count: int, ttl: int) -> int:
        return count


class _CacheReadingOnly(_FakeCache):
    """set_json 이 담은 값을 다음 get_json 이 그대로 돌려주게 한다."""

    async def get_json(self, key: str):
        hit = self.json.get(key)
        return hit[0] if hit is not None else None


class _SpyBike:
    """장별 응답을 정해 두고 호출 횟수를 세는 대역."""

    def __init__(self, pages: list) -> None:
        self.pages = pages
        self.calls = 0

    async def fetch_page(self, start: int, end: int):
        idx = self.calls
        self.calls += 1
        if idx >= len(self.pages):
            raise SeoulBikeApiError("API_INFO-200", "범위 밖")
        page = self.pages[idx]
        if isinstance(page, Exception):
            raise page
        return page


@pytest.fixture()
def real_key(monkeypatch):
    """스텁 경로를 벗어나 실호출 분기를 타게 한다."""
    monkeypatch.setattr(settings, "SEOUL_OPENAPI_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)


def test_bike_stations_stub_mode_shape():
    """키 미설정 → 스텁 경로로 200 과 기대 형태를 반환한다."""
    resp = _client().get("/v1/mobility/bike-stations", params=_params())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["count"] == len(body["stations"])
    assert body["count"] >= 1
    assert set(body["stations"][0].keys()) == {
        "station_id",
        "name",
        "rack_total",
        "parking_bike_total",
        "lat",
        "lng",
    }


def test_bike_stub_is_near_requested_point():
    """스텁은 요청 좌표 주변에 놓인다.

    고정 좌표를 쓰면 지도가 다른 곳을 비출 때 화면이 비어 보여, 스텁으로
    확인하려던 것을 확인하지 못한다.
    """
    for s in seoul_bike_stub(_LAT, _LNG):
        assert haversine_m(_LAT, _LNG, s["lat"], s["lng"]) < 1000


def test_bike_stub_matches_schema():
    """스텁 데이터가 응답 스키마와 맞는다."""
    for s in seoul_bike_stub(_LAT, _LNG):
        BikeStation(**s)


def test_bike_stub_includes_empty_station():
    """대여 가능 0 인 곳을 섞어 둔다 — 걸러내기 동작을 볼 수 있어야 한다."""
    rows = seoul_bike_stub(_LAT, _LNG)
    assert any(s["parking_bike_total"] == 0 for s in rows)
    assert any(s["parking_bike_total"] > 0 for s in rows)


@pytest.mark.parametrize("missing", ["lat", "lng"])
def test_bike_missing_coordinate_422(missing):
    """좌표는 필수다."""
    params = _params()
    params.pop(missing)
    resp = _client().get("/v1/mobility/bike-stations", params=params)
    # 좌표를 감쌀 수 있게 되면서 lat/lng 는 선택 인자가 됐다. 그래서 빠졌을 때
    # 걸리는 자리가 형식 검증기(422)에서 좌표 해석기(400)로 옮겨졌다.
    # 둘 다 요청이 잘못됐다는 뜻이라 부르는 쪽 동작은 달라지지 않는다.
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize("lat", [32.9, 43.1])
def test_bike_lat_out_of_range_422(lat):
    """위도가 국내 범위를 벗어나면 422 로 거절된다."""
    resp = _client().get(
        "/v1/mobility/bike-stations", params=_params(lat=lat)
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("radius", [99, 20001])
def test_bike_radius_out_of_range_422(radius):
    """반경이 허용 구간을 벗어나면 422 로 거절된다."""
    resp = _client().get(
        "/v1/mobility/bike-stations", params=_params(radius_m=radius)
    )
    assert resp.status_code == 422


def test_radius_filter_drops_far_stations(real_key, monkeypatch):
    """반경 밖 대여소는 잘려 나간다."""
    near = _station(1, _LAT + 0.002, _LNG)
    far = _station(2, _LAT + 0.5, _LNG)
    spy = _SpyBike([([near, far], 2)])
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_seoul_bike_client", lambda: spy)

    body = (
        _client()
        .get("/v1/mobility/bike-stations", params=_params(radius_m=5000))
        .json()
    )
    assert body["status"] == "ok"
    assert [s["station_id"] for s in body["stations"]] == ["ST-1"]


def test_outside_service_area_is_ok_with_empty_list(real_key, monkeypatch):
    """서비스 지역 밖은 빈 목록이며, 그것은 실패가 아니다."""
    spy = _SpyBike([([_station(1, _LAT, _LNG)], 1)])
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_seoul_bike_client", lambda: spy)

    # 부산 좌표로 물으면 서울 대여소는 모두 반경 밖이다.
    body = (
        _client()
        .get(
            "/v1/mobility/bike-stations",
            params={"lat": 35.1796, "lng": 129.0756},
        )
        .json()
    )
    assert body["status"] == "ok"
    assert body["count"] == 0


def test_last_page_is_judged_by_raw_row_count(real_key, monkeypatch):
    """마지막 장 판정은 버리기 전 행 수로 한다.

    좌표가 없어 버린 행이 섞이면 목록 길이가 한 장 크기보다 작아진다.
    그 길이로 판정하면 아직 뒤에 장이 남았는데도 끝난 줄 알고 멈춰,
    뒤쪽 대여소를 통째로 놓친다.
    """
    monkeypatch.setattr(settings, "SEOUL_BIKE_PAGE_SIZE", 2)
    monkeypatch.setattr(settings, "SEOUL_BIKE_MAX_PAGES", 5)
    # 첫 장: 두 행이 왔지만 하나는 좌표가 없어 버려져 목록은 하나.
    page1 = ([_station(1, _LAT, _LNG)], 2)
    page2 = ([_station(2, _LAT, _LNG)], 1)
    spy = _SpyBike([page1, page2])
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_seoul_bike_client", lambda: spy)

    body = _client().get(
        "/v1/mobility/bike-stations", params=_params()
    ).json()
    assert spy.calls == 2
    assert body["count"] == 2


def test_page_cap_stops_paging(real_key, monkeypatch):
    """장이 끝없이 이어져도 상한에서 멈춘다."""
    monkeypatch.setattr(settings, "SEOUL_BIKE_PAGE_SIZE", 1)
    monkeypatch.setattr(settings, "SEOUL_BIKE_MAX_PAGES", 3)
    pages = [([_station(i, _LAT, _LNG)], 1) for i in range(10)]
    spy = _SpyBike(pages)
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_seoul_bike_client", lambda: spy)

    body = _client().get(
        "/v1/mobility/bike-stations", params=_params()
    ).json()
    assert spy.calls == 3
    assert body["count"] == 3


def test_partial_pages_still_answer(real_key, monkeypatch):
    """도중에 실패해도 받은 만큼으로 답한다."""
    monkeypatch.setattr(settings, "SEOUL_BIKE_PAGE_SIZE", 1)
    monkeypatch.setattr(settings, "SEOUL_BIKE_MAX_PAGES", 5)
    spy = _SpyBike(
        [
            ([_station(1, _LAT, _LNG)], 1),
            SeoulBikeApiError("HTTP_ERR", "boom"),
        ]
    )
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_seoul_bike_client", lambda: spy)

    body = _client().get(
        "/v1/mobility/bike-stations", params=_params()
    ).json()
    assert body["status"] == "ok"
    assert body["count"] == 1


def test_partial_snapshot_is_cached_briefly(real_key, monkeypatch):
    """온전하지 않은 스냅샷은 짧게만 담아 다음 요청에서 다시 채운다."""
    monkeypatch.setattr(settings, "SEOUL_BIKE_PAGE_SIZE", 1)
    monkeypatch.setattr(settings, "SEOUL_BIKE_MAX_PAGES", 5)
    cache = _FakeCache()
    spy = _SpyBike(
        [
            ([_station(1, _LAT, _LNG)], 1),
            SeoulBikeApiError("HTTP_ERR", "boom"),
        ]
    )
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_seoul_bike_client", lambda: spy)

    _client().get("/v1/mobility/bike-stations", params=_params())
    stored, ttl = cache.json["seoulbike:all"]
    assert stored["status"] == "partial"
    assert ttl == settings.SEOUL_BIKE_PARTIAL_CACHE_TTL_SEC


def test_all_pages_failed_is_unavailable(real_key, monkeypatch):
    """아무것도 못 받으면 unavailable 이다."""
    spy = _SpyBike([SeoulBikeApiError("HTTP_ERR", "boom")])
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_seoul_bike_client", lambda: spy)

    body = _client().get(
        "/v1/mobility/bike-stations", params=_params()
    ).json()
    assert body["status"] == "unavailable"
    assert body["count"] == 0


def test_cache_is_shared_across_coordinates(real_key, monkeypatch):
    """좌표가 달라도 캐시는 한 벌만 쓴다.

    좌표별로 담으면 지도를 조금 움직일 때마다 여러 장을 다시 받아 와서
    하루 호출 한도가 곧 바닥난다.
    """
    cache = _CacheReadingOnly()
    spy = _SpyBike([([_station(1, _LAT, _LNG)], 1)])
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_seoul_bike_client", lambda: spy)

    client = _client()
    client.get("/v1/mobility/bike-stations", params=_params())
    client.get(
        "/v1/mobility/bike-stations",
        params=_params(lat=_LAT + 0.01, lng=_LNG + 0.01),
    )
    assert spy.calls == 1
    assert list(cache.json.keys()) == ["seoulbike:all"]


def test_budget_timeout_is_absorbed(real_key, monkeypatch):
    """전체 제한 시간을 넘겨도 5xx 가 아니라 unavailable 로 답한다."""

    async def _slow():
        await asyncio.sleep(0.5)
        return "ok", []

    monkeypatch.setattr(hub_routers, "_seoul_bike_all", _slow)
    monkeypatch.setattr(settings, "SEOUL_BIKE_TOTAL_BUDGET_SEC", 0.01)

    resp = _client().get("/v1/mobility/bike-stations", params=_params())
    assert resp.status_code == 200
    assert resp.json()["status"] == "unavailable"


def test_normalize_drops_rows_without_coordinates():
    """좌표가 없는 행은 지도에 찍을 수 없으므로 버린다."""
    body = {
        "rentBikeStatus": {
            "RESULT": {"CODE": "INFO-000"},
            "row": [
                {
                    "stationId": "ST-1",
                    "stationName": "가",
                    "rackTotCnt": "15",
                    "parkingBikeTotCnt": "5",
                    "stationLatitude": "37.55564880",
                    "stationLongitude": "126.91062927",
                },
                {"stationId": "ST-2", "stationName": "나"},
                {
                    "stationId": "ST-3",
                    "stationLatitude": "0",
                    "stationLongitude": "0",
                },
            ],
        }
    }
    rows, raw = SeoulBikeClient._normalize(body)
    assert raw == 3
    assert [r["station_id"] for r in rows] == ["ST-1"]
    # 수치가 문자열로 오므로 정규화 단계에서 정수로 고정한다.
    assert rows[0]["rack_total"] == 15
    assert rows[0]["parking_bike_total"] == 5


def test_normalize_raises_on_bad_result_code():
    """결과 코드가 정상이 아니면 실패로 올린다."""
    body = {
        "rentBikeStatus": {
            "RESULT": {"CODE": "INFO-200", "MESSAGE": "데이터 없음"},
            "row": [],
        }
    }
    with pytest.raises(SeoulBikeApiError) as got:
        SeoulBikeClient._normalize(body)
    assert got.value.code == "API_INFO-200"


def test_normalize_raises_on_unknown_body():
    """범위를 벗어나면 본문 모양 자체가 달라진다 — 그 경우도 실패다."""
    with pytest.raises(SeoulBikeApiError):
        SeoulBikeClient._normalize({"RESULT": {"CODE": "INFO-200"}})


def test_api_key_is_not_left_in_error_message():
    """인증키가 주소 경로에 실리므로, 오류 메시지에 남으면 안 된다.

    쿼리에 실리는 다른 발급처와 모양이 달라, 쿼리만 가리는 규칙으로는
    걸리지 않는다.
    """
    leaked = "7165587761646d6c3639447a796650"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"error at {request.url}")

    client = SeoulBikeClient(leaked)
    client._client = httpx.AsyncClient(
        base_url=SeoulBikeClient.HOST,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(SeoulBikeApiError) as got:
            asyncio.run(client.fetch_page(1, 1000))
    finally:
        asyncio.run(client.aclose())
    assert leaked not in got.value.msg
    assert "***" in got.value.msg
