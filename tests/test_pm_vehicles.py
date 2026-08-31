"""/v1/mobility/pm-vehicles (공유 킥보드) 라우터·클라이언트 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
대신 새 FastAPI 에 hub_router 만 include 해 TestClient 로 라우트만 검증한다.
conftest 가 KMA_SERVICE_KEY 를 채우므로 전용 키가 비어 있어도 실호출 분기를
타는 점에 주의한다 — 스텁을 보려면 두 키를 함께 비워야 한다.

다루는 범위:
  - 전용 키가 없을 때 기상청 키로 대신 도는지
  - 스텁 모드 200 응답 형태와 요청 좌표 주변에 놓이는지
  - 좌표·반경 경계 검증 실패(422)
  - 반경 밖 기기가 잘려 나가는지
  - 사업자별로 나눠 묻고 결과를 합치는지
  - 일부 사업자만 실패하면 받은 만큼으로 답하고, 전부 실패해야 조회 불가인지
  - 캐시를 좌표와 무관하게 쓰는지
  - 전체 제한 시간 초과가 5xx 가 아니라 unavailable 로 흡수되는지
  - 인증 단계에서 막힌 응답을 "결과 없음"으로 오인하지 않는지
  - 인증키가 오류 메시지에 남지 않는지
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.clients.hub_clients import PmApiError, PmClient
from app.config import settings
from app.place_stubs import pm_vehicle_stub
from app.routers import hub_routers
from app.routers.hub_routers import _pm_cache_key, _pm_providers
from app.routers.hub_routers import router as hub_router
from app.rules.rule_engine import haversine_m
from app.schemas.hub_schemas import PmVehicle

# 서울 시청.
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


def _vehicle(idx: int, lat: float, lng: float, provider: str = "Beam") -> dict:
    """정규화된 기기 한 대를 만든다."""
    return {
        "provider": provider,
        "device_id": f"PM-{idx}",
        "battery_level": 70,
        "vehicle_type": "전동킥보드",
        "lat": lat,
        "lng": lng,
    }


class _FakeCache:
    """라우터가 쓰는 캐시 동작만 흉내 내는 최소 구현."""

    def __init__(self) -> None:
        self.json: dict = {}

    async def get_json(self, key: str):
        hit = self.json.get(key)
        return hit[0] if hit is not None else None

    async def set_json(self, key: str, value, ttl: int) -> None:
        self.json[key] = (value, ttl)

    async def incr_by(self, key: str, count: int, ttl: int) -> int:
        return count


class _SpyPm:
    """사업자별 응답을 정해 두고 호출을 기록하는 대역."""

    def __init__(self, by_provider: dict) -> None:
        self.by_provider = by_provider
        self.seen: list[str] = []

    async def fetch_by_provider(self, provider, *, city=None, num_of_rows=0):
        self.seen.append(provider)
        result = self.by_provider.get(provider, [])
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def stub_mode(monkeypatch):
    """두 키를 모두 비워 스텁 경로를 타게 한다."""
    monkeypatch.setattr(settings, "PM_SERVICE_KEY", SecretStr(""))
    monkeypatch.setattr(settings, "KMA_SERVICE_KEY", SecretStr(""))
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)


@pytest.fixture()
def real_key(monkeypatch):
    """스텁 경로를 벗어나 실호출 분기를 타게 한다."""
    monkeypatch.setattr(settings, "PM_SERVICE_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)


def test_pm_stub_mode_shape(stub_mode):
    """키 미설정 → 스텁 경로로 200 과 기대 형태를 반환한다."""
    resp = _client().get("/v1/mobility/pm-vehicles", params=_params())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["count"] == len(body["vehicles"])
    assert body["count"] >= 1
    assert set(body["vehicles"][0].keys()) == {
        "provider",
        "device_id",
        "battery_level",
        "vehicle_type",
        "lat",
        "lng",
    }


def test_pm_falls_back_to_weather_key(monkeypatch):
    """전용 키가 없으면 기상청 키로 대신 돈다.

    같은 발급처의 한 계정 키로 여러 서비스가 열려 있는 경우가 흔해, 키를 두
    번 적지 않아도 되게 한다. 대기오염 쪽과 같은 방식이다.
    """
    monkeypatch.setattr(settings, "PM_SERVICE_KEY", SecretStr(""))
    monkeypatch.setattr(settings, "KMA_SERVICE_KEY", SecretStr("weather-key"))
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    spy = _SpyPm({"Beam": [_vehicle(1, _LAT, _LNG)]})
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_pm_client", lambda: spy)
    monkeypatch.setattr(settings, "PM_PROVIDERS", "Beam")

    body = _client().get(
        "/v1/mobility/pm-vehicles", params=_params()
    ).json()
    # 스텁으로 빠지지 않고 실호출 분기를 탔다.
    assert spy.seen == ["Beam"]
    assert body["count"] == 1


def test_pm_stub_is_near_requested_point(stub_mode):
    """스텁은 요청 좌표 주변에 놓인다."""
    for v in pm_vehicle_stub(_LAT, _LNG):
        assert haversine_m(_LAT, _LNG, v["lat"], v["lng"]) < 500


def test_pm_stub_matches_schema(stub_mode):
    """스텁 데이터가 응답 스키마와 맞는다."""
    for v in pm_vehicle_stub(_LAT, _LNG):
        PmVehicle(**v)


def test_pm_stub_spreads_battery_levels(stub_mode):
    """배터리 잔량을 넓게 흩어 둔다 — 잔량별 표시를 키 없이 볼 수 있어야 한다."""
    levels = [v["battery_level"] for v in pm_vehicle_stub(_LAT, _LNG)]
    assert min(levels) < 30 and max(levels) > 80


@pytest.mark.parametrize("missing", ["lat", "lng"])
def test_pm_missing_coordinate_422(missing):
    """좌표는 필수다."""
    params = _params()
    params.pop(missing)
    resp = _client().get("/v1/mobility/pm-vehicles", params=params)
    # 좌표를 감쌀 수 있게 되면서 lat/lng 는 선택 인자가 됐다. 그래서 빠졌을 때
    # 걸리는 자리가 형식 검증기(422)에서 좌표 해석기(400)로 옮겨졌다.
    # 둘 다 요청이 잘못됐다는 뜻이라 부르는 쪽 동작은 달라지지 않는다.
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize("lat", [32.9, 43.1])
def test_pm_lat_out_of_range_422(lat):
    """위도가 국내 범위를 벗어나면 422 로 거절된다."""
    resp = _client().get("/v1/mobility/pm-vehicles", params=_params(lat=lat))
    assert resp.status_code == 422


@pytest.mark.parametrize("radius", [99, 20001])
def test_pm_radius_out_of_range_422(radius):
    """반경이 허용 구간을 벗어나면 422 로 거절된다."""
    resp = _client().get(
        "/v1/mobility/pm-vehicles", params=_params(radius_m=radius)
    )
    assert resp.status_code == 422


def test_pm_queries_every_provider_and_merges(real_key, monkeypatch):
    """사업자마다 따로 묻고 결과를 합친다.

    발급처가 사업자를 필수로 받고 목록을 주는 오퍼레이션이 없어서, 설정에
    적어 둔 사업자를 하나씩 물어야 한다.
    """
    monkeypatch.setattr(settings, "PM_PROVIDERS", "Beam,GCOO,SWING")
    spy = _SpyPm(
        {
            "Beam": [_vehicle(1, _LAT, _LNG, "Beam")],
            "GCOO": [_vehicle(2, _LAT, _LNG, "GCOO")],
            "SWING": [],
        }
    )
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_pm_client", lambda: spy)

    body = _client().get(
        "/v1/mobility/pm-vehicles", params=_params()
    ).json()
    assert sorted(spy.seen) == ["Beam", "GCOO", "SWING"]
    assert body["status"] == "ok"
    assert body["count"] == 2


def test_pm_radius_filter_drops_far_vehicles(real_key, monkeypatch):
    """반경 밖 기기는 잘려 나간다."""
    monkeypatch.setattr(settings, "PM_PROVIDERS", "Beam")
    spy = _SpyPm(
        {
            "Beam": [
                _vehicle(1, _LAT + 0.001, _LNG),
                _vehicle(2, _LAT + 0.5, _LNG),
            ]
        }
    )
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_pm_client", lambda: spy)

    body = (
        _client()
        .get("/v1/mobility/pm-vehicles", params=_params(radius_m=1000))
        .json()
    )
    assert [v["device_id"] for v in body["vehicles"]] == ["PM-1"]


def test_pm_partial_failure_still_answers(real_key, monkeypatch):
    """일부 사업자만 실패하면 받은 만큼으로 답한다.

    한 사업자의 장애로 나머지가 함께 사라지면 화면이 실제보다 비어 보인다.
    """
    monkeypatch.setattr(settings, "PM_PROVIDERS", "Beam,GCOO")
    spy = _SpyPm(
        {
            "Beam": [_vehicle(1, _LAT, _LNG)],
            "GCOO": PmApiError("HTTP_ERR", "boom"),
        }
    )
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_pm_client", lambda: spy)

    body = _client().get(
        "/v1/mobility/pm-vehicles", params=_params()
    ).json()
    assert body["status"] == "ok"
    assert body["count"] == 1


def test_pm_partial_result_is_cached_briefly(real_key, monkeypatch):
    """온전하지 않은 결과는 짧게만 담아 다음 요청에서 다시 채운다."""
    monkeypatch.setattr(settings, "PM_PROVIDERS", "Beam,GCOO")
    cache = _FakeCache()
    spy = _SpyPm(
        {
            "Beam": [_vehicle(1, _LAT, _LNG)],
            "GCOO": PmApiError("HTTP_ERR", "boom"),
        }
    )
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_pm_client", lambda: spy)

    _client().get("/v1/mobility/pm-vehicles", params=_params())
    stored, ttl = cache.json[_pm_cache_key(None)]
    assert stored["status"] == "partial"
    assert ttl == settings.PM_FAIL_CACHE_TTL_SEC


def test_pm_all_providers_failed_is_unavailable(real_key, monkeypatch):
    """사업자 전부에서 실패해야 조회 불가다."""
    monkeypatch.setattr(settings, "PM_PROVIDERS", "Beam,GCOO")
    spy = _SpyPm(
        {
            "Beam": PmApiError("HTTP_ERR", "boom"),
            "GCOO": PmApiError("HTTP_ERR", "boom"),
        }
    )
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_pm_client", lambda: spy)

    body = _client().get(
        "/v1/mobility/pm-vehicles", params=_params()
    ).json()
    assert body["status"] == "unavailable"
    assert body["count"] == 0


def test_pm_cache_is_shared_across_coordinates(real_key, monkeypatch):
    """좌표가 달라도 캐시는 지역 단위로 한 벌만 쓴다.

    좌표별로 담으면 지도를 조금 움직일 때마다 사업자 수만큼의 호출이 통째로
    다시 나간다.
    """
    monkeypatch.setattr(settings, "PM_PROVIDERS", "Beam")
    cache = _FakeCache()
    spy = _SpyPm({"Beam": [_vehicle(1, _LAT, _LNG)]})
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_pm_client", lambda: spy)

    client = _client()
    client.get("/v1/mobility/pm-vehicles", params=_params())
    client.get(
        "/v1/mobility/pm-vehicles",
        params=_params(lat=_LAT + 0.002, lng=_LNG + 0.002),
    )
    assert spy.seen == ["Beam"]


def test_pm_budget_timeout_is_absorbed(real_key, monkeypatch):
    """전체 제한 시간을 넘겨도 5xx 가 아니라 unavailable 로 답한다."""

    async def _slow(_city):
        await asyncio.sleep(0.5)
        return "ok", []

    monkeypatch.setattr(hub_routers, "_pm_vehicles", _slow)
    monkeypatch.setattr(settings, "PM_TOTAL_BUDGET_SEC", 0.01)

    resp = _client().get("/v1/mobility/pm-vehicles", params=_params())
    assert resp.status_code == 200
    assert resp.json()["status"] == "unavailable"


def test_provider_list_ignores_blank_entries(monkeypatch):
    """설정의 빈 항목과 앞뒤 공백은 걸러진다."""
    monkeypatch.setattr(settings, "PM_PROVIDERS", " Beam , ,GCOO, ")
    assert _pm_providers() == ["Beam", "GCOO"]


def test_normalize_reads_items_and_drops_missing_coordinates():
    """좌표가 없는 기기는 지도에 찍을 수 없으므로 버린다."""
    body = {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
            "body": {
                "items": {
                    "item": [
                        {
                            "providerName": "Beam",
                            "deviceId": "D-1",
                            "batteryLevel": "77",
                            "vehicleType": "전동킥보드",
                            "lat": "37.5665",
                            "lng": "126.9780",
                        },
                        {"providerName": "Beam", "deviceId": "D-2"},
                    ]
                }
            },
        }
    }
    rows = PmClient._normalize(body, "Beam")
    assert [r["device_id"] for r in rows] == ["D-1"]
    # 수치가 문자열로 오므로 정규화 단계에서 형태를 고정한다.
    assert rows[0]["battery_level"] == 77
    assert rows[0]["lat"] == 37.5665


def test_normalize_accepts_single_item_object():
    """항목이 하나뿐이면 배열이 아니라 객체 하나로 오기도 한다."""
    body = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {
                "items": {
                    "item": {
                        "deviceId": "D-9",
                        "latitude": "37.5",
                        "longitude": "127.0",
                    }
                }
            },
        }
    }
    rows = PmClient._normalize(body, "SWING")
    assert len(rows) == 1
    # 사업자명이 빠지면 물어본 사업자로 채운다.
    assert rows[0]["provider"] == "SWING"
    # 배터리 잔량이 없을 수 있다.
    assert rows[0]["battery_level"] is None


def test_normalize_empty_items_is_not_an_error():
    """기기가 없는 사업자는 빈 목록이며 실패가 아니다."""
    body = {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
            "body": {"items": {"item": []}, "totalCount": 0},
        }
    }
    assert PmClient._normalize(body, "Beam") == []


def test_normalize_detects_gateway_auth_failure():
    """인증 단계에서 막힌 응답을 "결과 없음"으로 오인하지 않는다.

    이때는 본문 껍데기 자체가 달라진다. 정상 경로로 읽으면 조회가 된 줄 알고
    화면이 조용히 빈 채로 남아, 키가 막힌 사실을 아무도 모르게 된다.
    """
    body = {
        "OpenAPI_ServiceResponse": {
            "cmmMsgHeader": {
                "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
                "returnAuthMsg": "등록되지 않은 서비스키",
                "returnReasonCode": "30",
            }
        }
    }
    with pytest.raises(PmApiError) as got:
        PmClient._normalize(body, "Beam")
    assert got.value.code == "AUTH"


def test_normalize_raises_on_bad_result_code():
    """결과 코드가 정상이 아니면 실패로 올린다."""
    body = {
        "response": {
            "header": {"resultCode": "99", "resultMsg": "UNKNOWN_ERROR."},
        }
    }
    with pytest.raises(PmApiError) as got:
        PmClient._normalize(body, "Beam")
    assert got.value.code == "API_99"


def test_api_key_is_not_left_in_error_message():
    """인증키가 쿼리로 나가므로, 오류 메시지에 그대로 남으면 안 된다."""
    leaked = "2f82e9261530e44f3c6439acfa570bc4"

    def handler(request: httpx.Request) -> httpx.Response:
        # 이 발급처는 오류 페이지에 요청 주소를 그대로 되비추기도 한다.
        return httpx.Response(500, text=f"error at {request.url}")

    client = PmClient(leaked)
    client._client = httpx.AsyncClient(
        base_url=PmClient.HOST,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PmApiError) as got:
            asyncio.run(client.fetch_by_provider("Beam"))
    finally:
        asyncio.run(client.aclose())
    assert leaked not in got.value.msg
    assert "serviceKey=***" in got.value.msg
