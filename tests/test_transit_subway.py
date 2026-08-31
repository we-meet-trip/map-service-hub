"""/v1/transit/subway (ODsay 지하철 경로) 라우터·클라이언트 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
대신 새 FastAPI 에 hub_router 만 include 해 TestClient 로 라우트만 검증한다.
conftest 는 ODsay 키를 채우지 않으므로 기본 경로는 스텁으로 동작하고,
캐시(get_place_cache)는 lifespan 미기동 상태에서 None 이라 자연히 우회된다.

다루는 범위:
  - 스텁 모드 200 응답 형태
  - 좌표 경계 검증 실패(422)
  - 캐시 키 결정성과 좌표 반올림(같은 건물 출발이 한 칸으로 모이는지)
  - 하루 카운터 키가 날짜별로 갈리는지
  - 본문에 담겨 오는 오류를 "경로 없음"이 아니라 실패로 읽는지
  - 지하철 단독 경로가 없을 때와 있을 때의 구분, 가장 빠른 것 선택
  - 구간 정규화(이동 방식 매핑·노선명·역 수 없음)
  - 하루 상한 초과 시 외부 호출이 나가지 않고 unavailable 로 흡수되는지
  - 주 키가 막혔을 때 예비 키로 한 번 더 시도하는지, 키와 무관한 실패에는
    다시 부르지 않는지
  - 전체 제한 시간 초과가 5xx 가 아니라 unavailable 로 흡수되는지
  - 인증키가 오류 메시지에 남지 않는지
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.clients.hub_clients import OdsayApiError, OdsayClient
from app.config import settings
from app.route_stubs import subway_route_stub
from app.routers import hub_routers
from app.routers.hub_routers import (
    _odsay_budget_key,
    _odsay_cache_key,
    _odsay_is_key_failure,
)
from app.routers.hub_routers import router as hub_router
from app.schemas.hub_schemas import SubwayRoute

_KST = ZoneInfo("Asia/Seoul")

# 강남역 → 시청역. 국내 좌표 범위 안이면 값 자체는 검증에 영향이 없다.
_START = (37.4979, 127.0276)
_END = (37.5663, 126.9779)


def _client() -> TestClient:
    """hub_router 만 실은 새 FastAPI 의 TestClient 를 만든다."""
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


def _params(**over) -> dict:
    """기본 좌표가 채워진 질의 파라미터를 만든다."""
    base = {
        "start_lat": _START[0],
        "start_lng": _START[1],
        "end_lat": _END[0],
        "end_lng": _END[1],
    }
    base.update(over)
    return base


class _FakeCache:
    """라우터가 쓰는 캐시 동작만 흉내 내는 최소 구현.

    실제 캐시는 lifespan 이 만들어 주는데 테스트는 lifespan 을 띄우지
    않는다. 캐시가 없으면 하루 상한을 셀 수 없어 호출 경로가 그대로
    막히므로, 상한 동작을 보려면 셀 수 있는 자리를 하나 넣어야 한다.
    """

    def __init__(self) -> None:
        self.json: dict = {}
        self.counters: dict = {}

    async def get_json(self, key: str):
        return self.json.get(key)

    async def set_json(self, key: str, value, ttl: int) -> None:
        self.json[key] = value

    async def incr_by(self, key: str, count: int, ttl: int) -> int:
        self.counters[key] = self.counters.get(key, 0) + count
        return self.counters[key]


class _SpyOdsay:
    """호출 횟수를 세고 정해진 값을 돌려주는 대역."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def fastest_subway_route(self, *_args) -> dict | None:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture()
def real_key(monkeypatch):
    """스텁 경로를 벗어나 실호출 분기를 타게 한다."""
    monkeypatch.setattr(settings, "ODSAY_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)


def test_subway_stub_mode_shape():
    """키 미설정 → 스텁 경로로 200 과 기대 형태를 반환한다."""
    resp = _client().get("/v1/transit/subway", params=_params())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    route = body["route"]
    assert set(route.keys()) == {
        "total_time_min",
        "fare",
        "transfer_count",
        "total_walk_m",
        "steps",
    }
    assert route["steps"]
    assert set(route["steps"][0].keys()) == {
        "type",
        "line_name",
        "start_name",
        "end_name",
        "section_time_min",
        "station_count",
    }


def test_subway_stub_matches_schema():
    """스텁 데이터가 응답 스키마와 맞는다."""
    SubwayRoute(**subway_route_stub(*_START, *_END))


def test_subway_stub_scales_with_distance():
    """먼 좌표일수록 소요시간이 길게 나온다(화면 확인용 값의 최소 성질)."""
    near = subway_route_stub(37.5663, 126.9779, 37.5700, 126.9800)
    far = subway_route_stub(37.5663, 126.9779, 37.2000, 127.5000)
    assert far["total_time_min"] > near["total_time_min"]


@pytest.mark.parametrize(
    "name", ["start_lat", "start_lng", "end_lat", "end_lng"]
)
def test_subway_missing_coordinate_422(name):
    """네 좌표는 모두 필수다."""
    params = _params()
    params.pop(name)
    resp = _client().get("/v1/transit/subway", params=params)
    # 좌표를 감쌀 수 있게 되면서 lat/lng 는 선택 인자가 됐다. 그래서 빠졌을 때
    # 걸리는 자리가 형식 검증기(422)에서 좌표 해석기(400)로 옮겨졌다.
    # 둘 다 요청이 잘못됐다는 뜻이라 부르는 쪽 동작은 달라지지 않는다.
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize("lat", [32.9, 43.1])
def test_subway_lat_out_of_range_422(lat):
    """위도가 국내 범위를 벗어나면 422 로 거절된다."""
    resp = _client().get("/v1/transit/subway", params=_params(start_lat=lat))
    assert resp.status_code == 422


@pytest.mark.parametrize("lng", [123.9, 132.1])
def test_subway_lng_out_of_range_422(lng):
    """경도가 국내 범위를 벗어나면 422 로 거절된다."""
    resp = _client().get("/v1/transit/subway", params=_params(end_lng=lng))
    assert resp.status_code == 422


def test_odsay_cache_key_deterministic():
    """같은 입력은 같은 키를, 다른 입력은 다른 키를 만든다."""
    k1 = _odsay_cache_key(*_START, *_END)
    k2 = _odsay_cache_key(*_START, *_END)
    assert k1 == k2
    assert k1.startswith("odsay:subway:")
    assert _odsay_cache_key(*_END, *_START) != k1


def test_odsay_cache_key_rounds_coordinates():
    """반올림 자릿수 밖의 미세 편차는 같은 키로 모인다.

    모이지 않으면 같은 건물에서 출발한 요청마다 새 키가 되어 캐시가 비어
    있는 것과 같아지고, 하루 호출 한도가 금방 바닥난다.
    """
    base = _odsay_cache_key(*_START, *_END)
    tiny = _odsay_cache_key(
        _START[0] + 0.000001, _START[1], _END[0], _END[1]
    )
    assert tiny == base
    coarse = _odsay_cache_key(_START[0] + 0.01, _START[1], _END[0], _END[1])
    assert coarse != base


def test_odsay_budget_key_changes_daily():
    """카운터 키는 날짜별로 갈려 하루가 지나면 새로 센다."""
    d1 = datetime(2026, 8, 9, 23, 59, tzinfo=_KST)
    d2 = datetime(2026, 8, 10, 0, 1, tzinfo=_KST)
    assert _odsay_budget_key(d1) != _odsay_budget_key(d2)
    assert _odsay_budget_key(d1).endswith("20260809")


def test_error_body_is_failure_not_empty_result():
    """본문에 담겨 오는 오류를 실패로 읽는다.

    이 발급처는 인증 실패도 상태코드 200 으로 주고 본문에 오류를 담는다.
    그대로 두면 "정상 응답인데 경로가 없다"로 읽혀 사용자에게 "갈 수 있는
    길이 없다"로 표시된다.
    """
    body = {"error": [{"message": "인증키가 유효하지 않습니다."}]}
    with pytest.raises(OdsayApiError) as got:
        OdsayClient._normalize(body)
    assert got.value.code == "API_ERR"


def test_error_body_as_object_is_also_failure():
    """오류가 배열이 아니라 객체 하나로 와도 실패로 읽는다."""
    with pytest.raises(OdsayApiError):
        OdsayClient._normalize({"error": {"msg": "quota exceeded"}})


def test_normalize_returns_none_when_no_subway_only_path():
    """버스가 섞인 경로만 있으면 경로 없음(None)이다."""
    body = {
        "result": {
            "path": [
                {"pathType": 2, "info": {"totalTime": 30}},
                {"pathType": 3, "info": {"totalTime": 25}},
            ]
        }
    }
    assert OdsayClient._normalize(body) is None


def test_normalize_picks_fastest_subway_only_path():
    """지하철 단독 경로 중 가장 빠른 것을 고른다."""
    body = {
        "result": {
            "path": [
                {"pathType": 1, "info": {"totalTime": 55}, "subPath": []},
                {"pathType": 2, "info": {"totalTime": 10}, "subPath": []},
                {
                    "pathType": 1,
                    "info": {
                        "totalTime": 42,
                        "payment": 1500,
                        "subwayTransitCount": 1,
                        "totalWalk": 620,
                    },
                    "subPath": [],
                },
            ]
        }
    }
    got = OdsayClient._normalize(body)
    assert got is not None
    assert got["total_time_min"] == 42
    assert got["fare"] == 1500
    assert got["transfer_count"] == 1
    assert got["total_walk_m"] == 620


def test_normalize_maps_step_types_and_line_name():
    """구간 종류를 이동 방식으로 옮기고, 노선명은 배열 첫 항목에서 꺼낸다."""
    body = {
        "result": {
            "path": [
                {
                    "pathType": 1,
                    "info": {"totalTime": 20},
                    "subPath": [
                        {
                            "trafficType": 3,
                            "startName": "출발",
                            "endName": "강남",
                            "sectionTime": 5,
                        },
                        {
                            "trafficType": 1,
                            "lane": [{"name": "수도권 2호선"}],
                            "startName": "강남",
                            "endName": "사당",
                            "sectionTime": 12,
                            "stationCount": 6,
                        },
                        {
                            "trafficType": 2,
                            "lane": [{"name": "간선 401"}],
                            "startName": "사당",
                            "endName": "도착",
                            "sectionTime": 3,
                        },
                    ],
                }
            ]
        }
    }
    steps = OdsayClient._normalize(body)["steps"]
    assert [s["type"] for s in steps] == ["walk", "subway", "bus"]
    # 걷는 구간에는 노선명도 역 수도 없다.
    assert steps[0]["line_name"] is None
    assert steps[0]["station_count"] is None
    assert steps[1]["line_name"] == "수도권 2호선"
    assert steps[1]["station_count"] == 6


def test_normalize_tolerates_string_numbers():
    """수치가 문자열로 와도 정규화 단계에서 정수로 고정된다."""
    body = {
        "result": {
            "path": [
                {
                    "pathType": 1,
                    "info": {"totalTime": "33", "payment": "1400"},
                    "subPath": [
                        {
                            "trafficType": "1",
                            "startName": "가",
                            "endName": "나",
                            "sectionTime": "7",
                        }
                    ],
                }
            ]
        }
    }
    got = OdsayClient._normalize(body)
    assert got["total_time_min"] == 33
    assert got["fare"] == 1400
    assert got["steps"][0]["section_time_min"] == 7
    assert got["steps"][0]["type"] == "subway"


def test_daily_cap_blocks_upstream_call(real_key, monkeypatch):
    """하루 상한을 넘기면 외부 호출을 하지 않고 unavailable 로 답한다."""
    cache = _FakeCache()
    spy = _SpyOdsay(result=None)
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: spy)
    monkeypatch.setattr(settings, "ODSAY_DAILY_CALL_CAP", 0)

    resp = _client().get("/v1/transit/subway", params=_params())
    assert resp.status_code == 200
    assert resp.json()["status"] == "unavailable"
    assert resp.json()["route"] is None
    assert spy.calls == 0


def test_daily_cap_refunds_rejected_attempt(real_key, monkeypatch):
    """상한에 걸려 거절한 요청은 쓰지도 않은 몫을 차지하지 않는다."""
    cache = _FakeCache()
    spy = _SpyOdsay(result=None)
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: spy)
    monkeypatch.setattr(settings, "ODSAY_DAILY_CALL_CAP", 1)

    client = _client()
    client.get("/v1/transit/subway", params=_params())
    # 캐시에 남은 답이 재사용되지 않도록 좌표를 바꿔 두 번째를 보낸다.
    client.get("/v1/transit/subway", params=_params(end_lat=37.5000))
    client.get("/v1/transit/subway", params=_params(end_lat=37.5100))

    counted = list(cache.counters.values())[0]
    # 상한이 1 이므로 실제로 나간 호출은 한 번뿐이고, 거절된 두 번은
    # 되돌려져 카운터가 상한을 넘지 않는다.
    assert spy.calls == 1
    assert counted == 1


def test_upstream_failure_is_unavailable_not_not_found(real_key, monkeypatch):
    """외부 실패는 unavailable 이다 — 경로 없음과 합치지 않는다."""
    cache = _FakeCache()
    spy = _SpyOdsay(error=OdsayApiError("HTTP_500", "boom"))
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: spy)

    body = _client().get("/v1/transit/subway", params=_params()).json()
    assert body["status"] == "unavailable"
    assert body["route"] is None


def test_no_subway_path_is_not_found(real_key, monkeypatch):
    """지하철만으로 갈 수 없으면 not_found 다."""
    cache = _FakeCache()
    spy = _SpyOdsay(result=None)
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: spy)

    body = _client().get("/v1/transit/subway", params=_params()).json()
    assert body["status"] == "not_found"
    assert body["route"] is None


def test_second_call_is_served_from_cache(real_key, monkeypatch):
    """같은 좌표를 다시 물으면 외부 호출이 늘지 않는다."""
    cache = _FakeCache()
    spy = _SpyOdsay(result=subway_route_stub(*_START, *_END))
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: spy)

    client = _client()
    first = client.get("/v1/transit/subway", params=_params()).json()
    second = client.get("/v1/transit/subway", params=_params()).json()
    assert first == second
    assert first["status"] == "ok"
    assert spy.calls == 1


def _auth_failed() -> OdsayApiError:
    """발급처가 키 인증 실패로 돌려주는 실제 본문 모양."""
    return OdsayApiError(
        "API_ERR", "[ApiKeyAuthFailed] ApiKey authentication failed."
    )


@pytest.mark.parametrize(
    "error,expected",
    [
        (_auth_failed(), True),
        (OdsayApiError("API_ERR", "[DailyLimitExceeded] quota"), True),
        # 좌표가 틀린 것 같은 실패는 키를 바꿔도 결과가 같다.
        (OdsayApiError("API_ERR", "[InvalidCoordinate] bad SX"), False),
        # 연결이 끊긴 경우도 키와 무관하다.
        (OdsayApiError("HTTP_ERR", "connect timeout"), False),
    ],
)
def test_key_failure_detection(error, expected):
    """예비 키로 다시 부를 실패인지 가린다.

    키와 무관한 실패까지 다시 부르면 남은 하루치만 두 배로 쓴다.
    """
    assert _odsay_is_key_failure(error) is expected


def test_fallback_key_serves_when_primary_is_rejected(real_key, monkeypatch):
    """주 키가 거절되면 예비 키로 한 번 더 시도해 결과를 낸다."""
    cache = _FakeCache()
    primary = _SpyOdsay(error=_auth_failed())
    fallback = _SpyOdsay(result=subway_route_stub(*_START, *_END))
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: primary)
    monkeypatch.setattr(
        hub_routers, "get_odsay_fallback_client", lambda: fallback
    )

    body = _client().get("/v1/transit/subway", params=_params()).json()
    assert body["status"] == "ok"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_fallback_is_not_used_for_unrelated_failure(real_key, monkeypatch):
    """키와 무관한 실패에는 예비 키를 쓰지 않는다."""
    cache = _FakeCache()
    primary = _SpyOdsay(error=OdsayApiError("API_ERR", "[InvalidCoordinate]"))
    fallback = _SpyOdsay(result=subway_route_stub(*_START, *_END))
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: primary)
    monkeypatch.setattr(
        hub_routers, "get_odsay_fallback_client", lambda: fallback
    )

    body = _client().get("/v1/transit/subway", params=_params()).json()
    assert body["status"] == "unavailable"
    assert fallback.calls == 0


def test_no_fallback_configured_is_unavailable(real_key, monkeypatch):
    """예비 키를 채우지 않았으면 그대로 조회 불가다."""
    cache = _FakeCache()
    primary = _SpyOdsay(error=_auth_failed())
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: primary)
    monkeypatch.setattr(
        hub_routers, "get_odsay_fallback_client", lambda: None
    )

    body = _client().get("/v1/transit/subway", params=_params()).json()
    assert body["status"] == "unavailable"


def test_fallback_call_also_counts_against_daily_cap(real_key, monkeypatch):
    """예비 키 호출도 외부 호출이므로 하루 상한을 함께 쓴다."""
    cache = _FakeCache()
    primary = _SpyOdsay(error=_auth_failed())
    fallback = _SpyOdsay(result=subway_route_stub(*_START, *_END))
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_odsay_client", lambda: primary)
    monkeypatch.setattr(
        hub_routers, "get_odsay_fallback_client", lambda: fallback
    )
    # 상한이 1 이면 주 키가 한 번 쓰고 예비 키 차례에서는 남는 몫이 없다.
    monkeypatch.setattr(settings, "ODSAY_DAILY_CALL_CAP", 1)

    body = _client().get("/v1/transit/subway", params=_params()).json()
    assert body["status"] == "unavailable"
    assert primary.calls == 1
    assert fallback.calls == 0


def test_budget_timeout_is_absorbed(real_key, monkeypatch):
    """전체 제한 시간을 넘겨도 5xx 가 아니라 unavailable 로 답한다."""

    async def _slow(*_args, **_kwargs):
        await asyncio.sleep(0.5)
        return "ok", None

    monkeypatch.setattr(hub_routers, "_subway_route", _slow)
    monkeypatch.setattr(settings, "ODSAY_TOTAL_BUDGET_SEC", 0.01)

    resp = _client().get("/v1/transit/subway", params=_params())
    assert resp.status_code == 200
    assert resp.json()["status"] == "unavailable"


def test_api_key_is_not_left_in_error_message():
    """인증키가 쿼리로 나가므로, 오류 메시지에 그대로 남으면 안 된다."""
    leaked = "SUPERSECRETKEY123456"

    def handler(request: httpx.Request) -> httpx.Response:
        # 이 발급처는 오류 본문에 요청 주소를 그대로 되비추기도 한다.
        return httpx.Response(500, text=f"error at {request.url}")

    client = OdsayClient(leaked)
    client._client = httpx.AsyncClient(
        base_url=OdsayClient.HOST,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(OdsayApiError) as got:
            asyncio.run(client.fastest_subway_route(*_START, *_END))
    finally:
        asyncio.run(client.aclose())
    assert leaked not in got.value.msg
    assert "apiKey=***" in got.value.msg
