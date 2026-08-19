"""/v1/transit/routes (대중교통 통합 길찾기) 라우터·정규화·스텁 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
대신 새 FastAPI 에 hub_router 만 include 해 TestClient 로 라우트만 검증한다.
캐시(get_place_cache)는 lifespan 미기동 상태에서 None 이라 자연히 우회된다.

스텁 경로를 타는 테스트는 conftest 의 stub_mode 픽스처로 고정한다.
"인증키가 비어 있으면 스텁"이라는 성질에 기대지 않는 이유는, Settings 가
env_file=".env" 를 읽어 개발자 로컬에 실 키가 있으면 같은 테스트가 실호출
분기로 흘러 결과가 환경에 따라 달라지기 때문이다.

다루는 범위:
  - 스텁 모드 200 응답 형태 + 결정성, 도보 구간 geometry 공백
  - 좌표 국내 범위 이탈·필수 파라미터 누락 검증 실패(422)
  - _transit_routes_cache_key 결정성 및 지하철 전용 키와의 네임스페이스 분리
  - OdsayClient._normalize_routes 의 소요시간 정렬·ROUTE_OPTIONS_MAX 절단
  - _to_route_option 의 modes 구성(지하철 먼저 / 도보뿐이면 walk)과
    transfer_count 합산(버스+지하철)
  - _normalize_step 의 trafficType 매핑·노선명 추출·stationCount 결측 보존
  - _step_geometry 의 [x,y]→[lat,lng] 스왑, passStopList 부재 시 시작/끝 대체,
    좌표 자체가 없으면 빈 리스트
  - _step_stop_names 가 시작/끝으로 대체하지 않고 빈 리스트를 두는 계약
  - 오류 본문의 OdsayApiError 승격
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clients.hub_clients import OdsayApiError, OdsayClient
from app.route_stubs import transit_routes_stub
from app.routers.hub_routers import (
    _odsay_cache_key,
    _transit_routes_cache_key,
)
from app.routers.hub_routers import router as hub_router

# 서울 시청 → 여의도역. 국내 범위 안이면 값 자체는 의미가 없다.
START = {"start_lat": 37.5665, "start_lng": 126.9780}
GOAL = {"end_lat": 37.5228, "end_lng": 126.9227}
QUERY = {**START, **GOAL}


def _client() -> TestClient:
    """hub_router 만 실은 새 FastAPI 의 TestClient 를 만든다."""
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


def _step(
    traffic: int,
    *,
    lane: object = None,
    station_count: object = None,
    stops: object = None,
    section_time: int = 5,
    start_name: str = "출발역",
    end_name: str = "도착역",
    coords: bool = True,
) -> dict:
    """ODsay subPath 한 구간의 원본 형태를 만든다."""
    step: dict = {
        "trafficType": traffic,
        "sectionTime": section_time,
        "startName": start_name,
        "endName": end_name,
    }
    if lane is not None:
        step["lane"] = lane
    if station_count is not None:
        step["stationCount"] = station_count
    if stops is not None:
        step["passStopList"] = {"stations": stops}
    if coords:
        step.update(
            {
                "startX": 126.9780,
                "startY": 37.5665,
                "endX": 126.9227,
                "endY": 37.5228,
            }
        )
    return step


def _path(total_time: int, sub_path: list[dict], **info: int) -> dict:
    """ODsay result.path 한 건의 원본 형태를 만든다."""
    return {
        "info": {"totalTime": total_time, **info},
        "subPath": sub_path,
    }


# ── 스텁 모드 200 형태 / 결정성 ─────────────────────────────────────

def test_transit_routes_stub_shape(stub_mode):
    """스텁 모드에서 200 과 기대 형태를 반환한다."""
    res = _client().get("/v1/transit/routes", params=QUERY)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert len(body["routes"]) >= 1
    route = body["routes"][0]
    assert set(route) >= {
        "total_time_min",
        "fare",
        "transfer_count",
        "total_walk_m",
        "modes",
        "legs",
    }
    assert route["modes"]
    leg = route["legs"][0]
    assert set(leg) >= {
        "type",
        "start_name",
        "end_name",
        "section_time_min",
        "geometry",
        "stops",
    }


def test_transit_routes_stub_is_deterministic(stub_mode):
    """같은 좌표를 두 번 물으면 같은 본문이 온다(스텁은 입력만의 함수)."""
    client = _client()
    first = client.get("/v1/transit/routes", params=QUERY).json()
    second = client.get("/v1/transit/routes", params=QUERY).json()
    assert first == second


def test_transit_routes_stub_covers_subway_and_bus(stub_mode):
    """스텁은 지하철 전용·버스 전용 후보를 함께 준다.

    목록 화면의 이동수단 아이콘 분기를 실호출 없이 확인하려는 값이다.
    """
    routes = _client().get("/v1/transit/routes", params=QUERY).json()["routes"]
    modes = [tuple(r["modes"]) for r in routes]
    assert ("subway",) in modes
    assert ("bus",) in modes


def test_transit_routes_stub_walk_leg_has_no_geometry():
    """도보 구간은 그릴 좌표가 없어 geometry 가 빈 리스트다."""
    routes = transit_routes_stub(37.5665, 126.9780, 37.5228, 126.9227)
    walk_legs = [
        leg for r in routes for leg in r["legs"] if leg["type"] == "walk"
    ]
    assert walk_legs
    assert all(leg["geometry"] == [] for leg in walk_legs)


def test_transit_routes_stub_total_time_matches_legs():
    """후보의 총 소요시간은 구간 소요시간의 합이다."""
    for route in transit_routes_stub(37.5665, 126.9780, 37.5228, 126.9227):
        assert route["total_time_min"] == sum(
            leg["section_time_min"] for leg in route["legs"]
        )


# ── 좌표/파라미터 검증 ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "field, value",
    [
        ("start_lat", 32.9),
        ("start_lat", 43.1),
        ("start_lng", 123.9),
        ("start_lng", 132.1),
        ("end_lat", 32.9),
        ("end_lat", 43.1),
        ("end_lng", 123.9),
        ("end_lng", 132.1),
    ],
)
def test_transit_routes_rejects_out_of_range_coords(field, value):
    """국내 위경도 범위를 벗어난 좌표는 422 로 막는다."""
    params = {**QUERY, field: value}
    assert _client().get("/v1/transit/routes", params=params).status_code == 422


@pytest.mark.parametrize(
    "missing", ["start_lat", "start_lng", "end_lat", "end_lng"]
)
def test_transit_routes_requires_all_coords(missing):
    """네 좌표는 모두 필수다."""
    params = {k: v for k, v in QUERY.items() if k != missing}
    assert _client().get("/v1/transit/routes", params=params).status_code == 422


# ── 캐시 키 ────────────────────────────────────────────────────────

def test_transit_routes_cache_key_is_deterministic():
    """같은 좌표는 같은 키가 된다."""
    args = (37.5665, 126.9780, 37.5228, 126.9227)
    assert _transit_routes_cache_key(*args) == _transit_routes_cache_key(*args)


def test_transit_routes_cache_key_namespace_differs_from_subway():
    """같은 좌표라도 지하철 전용 키와 겹치지 않는다.

    겹치면 "지하철만" 결과가 "전체 보기" 응답으로 새어 나간다.
    """
    args = (37.5665, 126.9780, 37.5228, 126.9227)
    routes_key = _transit_routes_cache_key(*args)
    assert routes_key.startswith("odsay:routes:")
    assert routes_key != _odsay_cache_key(*args)


def test_transit_routes_cache_key_differs_by_coords():
    """좌표가 다르면 키도 다르다."""
    a = _transit_routes_cache_key(37.5665, 126.9780, 37.5228, 126.9227)
    b = _transit_routes_cache_key(37.5665, 126.9780, 37.5000, 127.0000)
    assert a != b


# ── _normalize_routes (정렬·절단) ──────────────────────────────────

def test_normalize_routes_sorts_by_total_time():
    """후보는 소요시간 오름차순으로 나온다."""
    data = {
        "result": {
            "path": [
                _path(50, [_step(1)]),
                _path(20, [_step(2)]),
                _path(35, [_step(1)]),
            ]
        }
    }
    times = [r["total_time_min"] for r in OdsayClient._normalize_routes(data)]
    assert times == [20, 35, 50]


def test_normalize_routes_truncates_to_max():
    """상한(ROUTE_OPTIONS_MAX)까지만 돌려준다."""
    paths = [_path(i, [_step(1)]) for i in range(1, 20)]
    routes = OdsayClient._normalize_routes({"result": {"path": paths}})
    assert len(routes) == OdsayClient.ROUTE_OPTIONS_MAX


def test_normalize_routes_keeps_bus_only_paths():
    """지하철 전용 조회와 달리 버스 전용 후보를 거르지 않는다."""
    data = {"result": {"path": [_path(30, [_step(2)])]}}
    routes = OdsayClient._normalize_routes(data)
    assert len(routes) == 1
    assert routes[0]["modes"] == ["bus"]


def test_normalize_routes_empty_when_no_path():
    """경로가 없으면 빈 목록이다(예외가 아니다)."""
    assert OdsayClient._normalize_routes({"result": {}}) == []


def test_normalize_routes_raises_on_error_body():
    """오류 본문은 OdsayApiError 로 올린다."""
    body = {"error": [{"message": "-8 mapObject 형식이 잘못되었습니다"}]}
    with pytest.raises(OdsayApiError):
        OdsayClient._normalize_routes(body)


# ── _to_route_option (modes / transfer_count) ──────────────────────

def test_to_route_option_modes_put_subway_first():
    """modes 는 등장 여부만 보고 지하철·버스 순으로 담는다."""
    path = _path(40, [_step(2), _step(1), _step(3)])
    assert OdsayClient._to_route_option(path)["modes"] == ["subway", "bus"]


def test_to_route_option_modes_fall_back_to_walk():
    """도보뿐인 후보는 방어적으로 walk 를 채운다."""
    path = _path(10, [_step(3)])
    assert OdsayClient._to_route_option(path)["modes"] == ["walk"]


def test_to_route_option_sums_transfer_counts():
    """환승 횟수는 버스와 지하철 몫을 합친다."""
    path = _path(40, [_step(1)], busTransitCount=2, subwayTransitCount=1)
    assert OdsayClient._to_route_option(path)["transfer_count"] == 3


# ── _normalize_step (매핑·결측) ────────────────────────────────────

@pytest.mark.parametrize(
    "traffic, expected",
    [(1, "subway"), (2, "bus"), (3, "walk"), (99, "walk")],
)
def test_normalize_step_maps_traffic_type(traffic, expected):
    """trafficType 을 우리 구간 종류로 옮긴다(모르는 값은 도보로 본다)."""
    assert OdsayClient._normalize_step(_step(traffic))["type"] == expected


def test_normalize_step_takes_first_lane_name():
    """노선명은 배열의 첫 항목에서 가져온다."""
    step = _step(1, lane=[{"name": "2호선"}, {"name": "무시됨"}])
    assert OdsayClient._normalize_step(step)["line_name"] == "2호선"


def test_normalize_step_line_name_none_when_absent():
    """걷는 구간처럼 노선 정보가 없으면 None 이다."""
    assert OdsayClient._normalize_step(_step(3))["line_name"] is None


def test_normalize_step_keeps_missing_station_count_as_none():
    """stationCount 가 아예 없으면 0 이 아니라 None 으로 남긴다.

    0 으로 만들면 "정류장 0개"라는 뜻이 되어 화면이 잘못 표시한다.
    """
    assert OdsayClient._normalize_step(_step(3))["station_count"] is None
    assert (
        OdsayClient._normalize_step(_step(1, station_count=4))["station_count"]
        == 4
    )


# ── _step_geometry (좌표 스왑·대체) ────────────────────────────────

def test_step_geometry_uses_pass_stops_and_swaps_xy():
    """passStopList 가 있으면 그 순서대로 [lat,lng] 로 담는다."""
    stops = [
        {"x": 126.9780, "y": 37.5665, "stationName": "시청"},
        {"x": 126.9227, "y": 37.5228, "stationName": "여의도"},
    ]
    geometry = OdsayClient._normalize_step(_step(1, stops=stops))["geometry"]
    assert geometry == [[37.5665, 126.9780], [37.5228, 126.9227]]


def test_step_geometry_falls_back_to_endpoints():
    """passStopList 가 비면 시작/끝 두 점으로 대체한다(버스가 가끔 그렇다)."""
    geometry = OdsayClient._normalize_step(_step(2, stops=[]))["geometry"]
    assert geometry == [[37.5665, 126.9780], [37.5228, 126.9227]]


def test_step_geometry_empty_without_coords():
    """좌표 필드가 아예 없는 순수 도보 연결 구간은 빈 리스트다."""
    step = _step(3, coords=False)
    assert OdsayClient._normalize_step(step)["geometry"] == []


def test_step_geometry_skips_unparsable_points():
    """좌표로 읽히지 않는 항목은 건너뛴다."""
    stops = [
        {"x": "bad", "y": "bad"},
        {"x": 126.9227, "y": 37.5228},
    ]
    geometry = OdsayClient._normalize_step(_step(1, stops=stops))["geometry"]
    assert geometry == [[37.5228, 126.9227]]


# ── _step_stop_names (대체하지 않는 계약) ──────────────────────────

def test_step_stop_names_keeps_order():
    """지나는 역/정류장 이름을 순서대로 담는다."""
    stops = [
        {"x": 126.97, "y": 37.56, "stationName": "시청"},
        {"x": 126.95, "y": 37.54, "stationName": "충정로"},
        {"x": 126.92, "y": 37.52, "stationName": "여의도"},
    ]
    names = OdsayClient._normalize_step(_step(1, stops=stops))["stops"]
    assert names == ["시청", "충정로", "여의도"]


def test_step_stop_names_empty_instead_of_endpoints():
    """이름 목록이 없으면 시작/끝으로 대체하지 않고 빈 리스트로 둔다.

    geometry 와 다르게 두는 지점이다. "N개 정류장" 표시가 시작/끝만으로
    채워지면 실제로 몇 곳을 지나는지 오인시킨다.
    """
    step = _step(2, stops=[], start_name="출발 정류장", end_name="도착 정류장")
    normalized = OdsayClient._normalize_step(step)
    assert normalized["stops"] == []
    # geometry 는 같은 상황에서 시작/끝으로 대체된다 — 둘의 처리가 다르다.
    assert normalized["geometry"] != []


def test_step_stop_names_skips_nameless_entries():
    """이름이 비어 있는 항목은 넣지 않는다."""
    stops = [
        {"x": 126.97, "y": 37.56, "stationName": "시청"},
        {"x": 126.95, "y": 37.54},
        {"x": 126.92, "y": 37.52, "stationName": ""},
    ]
    assert OdsayClient._normalize_step(_step(1, stops=stops))["stops"] == [
        "시청"
    ]
