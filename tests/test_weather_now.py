"""/v1/weather/now 라우터와 그 부속 로직 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
대신 새 FastAPI 에 hub_router 만 include 해 라우트만 검증하고, DB·외부
호출은 monkeypatch 로 갈아끼운다.

다루는 범위:
  - 실황 발표 시각 계산(45분 경계, 자정 넘김)
  - 미세먼지 등급 경계와 시도 명칭 변환
  - 대표 측정소 선택(값이 빈 측정소 회피)
  - 라우트 응답 형태 — 어제 기록 유무, 예보/대기 누락 시 생략
  - 좌표 범위 밖 요청 거절
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.codes.air_codes import grade_pm10, grade_pm25, sido_name
from app.routers import hub_routers
from app.routers.hub_routers import _pick_air_station
from app.routers.hub_routers import router as hub_router
from app.utils.kma_grid import resolve_nowcast_base

KST = ZoneInfo("Asia/Seoul")


def _client() -> TestClient:
    """hub_router 만 실은 새 FastAPI 의 TestClient 를 만든다."""
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


# ── 발표 시각 계산 ────────────────────────────────────────────────

def test_nowcast_base_before_publish_steps_back_one_hour():
    """45분 이전이면 아직 안 나온 이번 시각 대신 한 시간 전을 고른다."""
    now = datetime(2026, 8, 1, 10, 30, tzinfo=KST)
    assert resolve_nowcast_base(now) == ("20260801", "0900")


def test_nowcast_base_after_publish_uses_current_hour():
    """45분을 넘기면 이번 시각 관측분을 고른다."""
    now = datetime(2026, 8, 1, 10, 45, tzinfo=KST)
    assert resolve_nowcast_base(now) == ("20260801", "1000")


def test_nowcast_base_crosses_midnight():
    """자정 직후에는 전날 마지막 시각으로 넘어간다."""
    now = datetime(2026, 8, 1, 0, 10, tzinfo=KST)
    assert resolve_nowcast_base(now) == ("20260731", "2300")


# ── 대기오염 등급·명칭 ────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [(0, "좋음"), (30, "좋음"), (31, "보통"), (80, "보통"),
     (81, "나쁨"), (150, "나쁨"), (151, "매우나쁨"), (None, None)],
)
def test_pm10_grade_boundaries(value, expected):
    """미세먼지 등급이 구간 경계에서 정확히 갈린다."""
    assert grade_pm10(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(15, "좋음"), (16, "보통"), (35, "보통"), (36, "나쁨"),
     (75, "나쁨"), (76, "매우나쁨")],
)
def test_pm25_grade_boundaries(value, expected):
    """초미세먼지 등급이 구간 경계에서 정확히 갈린다."""
    assert grade_pm25(value) == expected


def test_sido_name_maps_official_names():
    """정식 명칭을 대기오염 API 의 축약형으로 바꾼다."""
    assert sido_name("서울특별시") == "서울"
    assert sido_name("강원특별자치도") == "강원"
    assert sido_name("전북특별자치도") == "전북"


def test_sido_name_unknown_is_none():
    """매핑에 없는 값은 None — 호출 측이 대기 조회를 건너뛴다."""
    assert sido_name("없는도") is None
    assert sido_name(None) is None


def test_pick_air_station_skips_blank_values():
    """값이 빈 측정소를 건너뛰고 둘 다 유효한 곳을 대표로 고른다."""
    items = [
        {"stationName": "점검중", "pm10Value": "-", "pm25Value": "-"},
        {"stationName": "일부만", "pm10Value": "40", "pm25Value": "-"},
        {"stationName": "정상", "pm10Value": "21", "pm25Value": "11"},
    ]
    assert _pick_air_station(items)["stationName"] == "정상"


def test_pick_air_station_falls_back_to_partial():
    """둘 다 유효한 곳이 없으면 미세먼지만이라도 있는 곳을 쓴다."""
    items = [
        {"stationName": "점검중", "pm10Value": "-", "pm25Value": "-"},
        {"stationName": "일부만", "pm10Value": "40", "pm25Value": "-"},
    ]
    assert _pick_air_station(items)["stationName"] == "일부만"


# ── 라우트 ────────────────────────────────────────────────────────
#
# 이 엔드포인트는 저장소만 읽는다. 실황과 대기오염은 hub 가 매시 미리 받아
# 두고, 화면이 열릴 때는 그 값을 꺼내 온다. 그래서 여기 대역은 발급처가
# 아니라 저장소다.


class _Region:
    """격자 역조회 결과 대역. 예보·대기오염 조회가 이 값으로 갈라진다."""

    nx = 60
    ny = 127
    lv1 = "서울특별시"
    lv2 = "종로구"


def _observed(hours_ago: float = 0.5) -> datetime:
    """지금으로부터 [hours_ago] 시간 전의 KST 시각."""
    return datetime.now(KST) - timedelta(hours=hours_ago)


def _stub_route(
    monkeypatch,
    *,
    nowcast=None,
    snapshot=None,
    region=None,
    air_rows=None,
    sido_grids=None,
    short_rows=None,
):
    """라우트가 읽는 저장소를 전부 대역으로 갈아끼운다.

    nowcast: fetch_recent_nowcast 가 돌려줄 값(None 이면 저장된 실황 없음).
    air_rows: fetch_recent_air 가 돌려줄 측정소 목록(None 이면 신선한 값 없음).
    """

    async def _recent_nowcast(nx, ny, max_age):
        return nowcast

    async def _snapshot(*_a, **_k):
        return snapshot

    async def _region(*_a, **_k):
        return region

    async def _short(*_a, **_k):
        return (short_rows or []), None

    async def _air(sido, max_age):
        return air_rows

    async def _sido_grids():
        return sido_grids or []

    monkeypatch.setattr(hub_routers, "fetch_recent_nowcast", _recent_nowcast)
    monkeypatch.setattr(hub_routers, "fetch_nowcast_snapshot", _snapshot)
    monkeypatch.setattr(hub_routers, "lookup_region_by_grid", _region)
    monkeypatch.setattr(hub_routers, "fetch_short_term_range", _short)
    monkeypatch.setattr(hub_routers, "fetch_recent_air", _air)
    monkeypatch.setattr(hub_routers, "load_sido_grids", _sido_grids)


def _get(lat=37.5665, lng=126.9780):
    return _client().get("/v1/weather/now", params={"lat": lat, "lng": lng})


def test_weather_now_returns_stored_observation(monkeypatch):
    """저장해 둔 실황이 있으면 그 값과 관측 시각을 함께 돌려준다."""
    when = _observed(0.5)
    _stub_route(
        monkeypatch,
        nowcast={"temp_c": 28.0, "pty": 0, "observed_at": when},
    )
    resp = _get()
    assert resp.status_code == 200
    body = resp.json()
    assert body["now"]["temp_c"] == 28.0
    assert body["now"]["pty"] == 0
    assert body["now"]["observed_at"].startswith(when.strftime("%Y-%m-%d"))
    assert body["nx"] == 60 and body["ny"] == 127


def test_weather_now_reports_times_in_kst(monkeypatch):
    """관측 시각은 KST 로 내보낸다.

    저장소는 시각을 UTC 로 돌려준다. 그대로 쓰면 발표 시각이 아홉 시간
    어긋나 화면에 "16시 기준" 대신 "07시 기준" 이 뜨고, 어제 비교도 엉뚱한
    시각을 찾는다. 겉으로는 숫자가 그럴듯해 눈치채기 어렵다.
    """
    from datetime import timezone

    # 16:00 KST 와 같은 순간을 UTC 로 적어 둔 값.
    utc_form = datetime(2026, 8, 11, 7, 0, tzinfo=timezone.utc)
    _stub_route(
        monkeypatch,
        nowcast={"temp_c": 33.5, "pty": 0, "observed_at": utc_form},
        region=_Region(),
        air_rows=[{
            "station_name": "종로구",
            "pm10": 11,
            "pm25": 4,
            "data_time": utc_form,
        }],
    )
    body = _get().json()
    assert body["now"]["base_time"] == "1600"
    assert body["now"]["base_date"] == "20260811"
    assert body["now"]["observed_at"].startswith("2026-08-11T16:00")
    assert body["air"]["observed_at"].startswith("2026-08-11T16:00")


def test_weather_now_omits_observation_when_nothing_stored(monkeypatch):
    """저장된 실황이 없으면 기온 자리를 비운다 — 실패가 아니다.

    발급처를 그 자리에서 부르지 않으므로 여기서 502 를 낼 이유가 없다.
    화면은 비어 온 항목을 감추고 나머지를 그린다.
    """
    _stub_route(monkeypatch, nowcast=None)
    resp = _get()
    assert resp.status_code == 200
    assert resp.json()["now"] is None


def test_weather_now_falls_back_to_sido_grid(monkeypatch):
    """요청 격자에 값이 없으면 같은 시도의 대표 격자 값으로 갈음한다.

    매시 받아 두는 대상이 시도 대표 격자뿐이라, 그 밖의 격자는 언제나
    이 길로 넘어온다. 이것이 없으면 대부분의 좌표에서 기온이 비어 버린다.
    """
    when = _observed(0.5)
    asked: list = []

    async def _recent_nowcast(nx, ny, max_age):
        asked.append((nx, ny))
        # 요청 격자(60,127)에는 없고 대표 격자(99,88)에만 있다.
        if (nx, ny) == (99, 88):
            return {"temp_c": 31.0, "pty": None, "observed_at": when}
        return None

    class _Grid:
        label = "서울특별시"
        nx = 99
        ny = 88

    _stub_route(monkeypatch, region=_Region(), sido_grids=[_Grid()])
    monkeypatch.setattr(hub_routers, "fetch_recent_nowcast", _recent_nowcast)

    body = _get().json()
    assert body["now"]["temp_c"] == 31.0
    assert asked == [(60, 127), (99, 88)], "요청 격자를 먼저 보고 나서 대표로 간다"


def test_weather_now_includes_stored_air(monkeypatch):
    """저장해 둔 대기오염이 있으면 농도·등급·측정 시각을 싣는다."""
    when = _observed(1)
    _stub_route(
        monkeypatch,
        nowcast={"temp_c": 28.0, "pty": 0, "observed_at": _observed(0.5)},
        region=_Region(),
        air_rows=[
            {
                "station_name": "종로구",
                "pm10": 11,
                "pm25": 6,
                "data_time": when,
            },
            {
                "station_name": "중구",
                "pm10": 40,
                "pm25": 20,
                "data_time": when,
            },
        ],
    )
    air = _get().json()["air"]
    assert air["pm10"] == 11
    assert air["pm25"] == 6
    assert air["pm10_grade"] == "좋음"
    assert air["station"] == "종로구", "요청 시군구와 이름이 같은 측정소를 고른다"
    assert air["observed_at"].startswith(when.strftime("%Y-%m-%d"))


def test_weather_now_omits_air_when_stale(monkeypatch):
    """신선한 대기오염 기록이 없으면 그 항목을 비운다.

    발급처가 오래 멈춰 있을 때 어제 농도를 지금 농도로 보여 주지 않는다.
    """
    _stub_route(
        monkeypatch,
        nowcast={"temp_c": 28.0, "pty": 0, "observed_at": _observed(0.5)},
        region=_Region(),
        air_rows=None,
    )
    assert _get().json()["air"] is None


def test_weather_now_does_not_call_upstream(monkeypatch):
    """조회 경로에서 발급처를 부르지 않는다.

    이 경로가 발급처를 부르면 발급처가 느린 동안 홈 화면 첫 진입이 함께
    느려지고, 발급처가 멈추면 화면이 실패한다. 그래서 읽기만 하도록 두고,
    여기서 그 사실을 고정한다.
    """
    called: list = []

    def _boom(*_a, **_k):
        called.append(1)
        raise AssertionError("조회 경로가 발급처를 불렀다")

    _stub_route(
        monkeypatch,
        nowcast={"temp_c": 28.0, "pty": 0, "observed_at": _observed(0.5)},
        region=_Region(),
    )
    # 발급처 접근 통로를 전부 막아 둔다.
    for name in ("get_kma_client", "get_airkorea_client"):
        if hasattr(hub_routers, name):
            monkeypatch.setattr(hub_routers, name, _boom)

    assert _get().status_code == 200
    assert not called


def test_weather_now_omits_yesterday_when_no_record(monkeypatch):
    """어제 기록이 없으면 비교 항목을 아예 싣지 않는다."""
    _stub_route(
        monkeypatch,
        nowcast={"temp_c": 28.0, "pty": 0, "observed_at": _observed(0.5)},
        snapshot=None,
    )
    assert _get().json()["yesterday"] is None


def test_weather_now_includes_yesterday_when_recorded(monkeypatch):
    """어제 기록이 있으면 그 기온과 시각을 함께 싣는다."""
    _stub_route(
        monkeypatch,
        nowcast={"temp_c": 28.0, "pty": 0, "observed_at": _observed(0.5)},
        snapshot={"temp_c": 30.1, "hour_kst": 9},
    )
    assert _get().json()["yesterday"] == {"temp_c": 30.1, "hour_kst": 9}


def test_weather_now_omits_yesterday_without_observation(monkeypatch):
    """실황이 없으면 어제 비교도 싣지 않는다.

    무엇과 비교하는지 정할 기준 시각이 없다. 기준 없는 비교는 화면에서
    "어제보다 ?도" 가 되어 사용자를 헷갈리게 한다.
    """
    _stub_route(
        monkeypatch,
        nowcast=None,
        snapshot={"temp_c": 30.1, "hour_kst": 9},
    )
    assert _get().json()["yesterday"] is None


def test_weather_now_omits_today_without_region(monkeypatch):
    """격자로 행정구역을 못 찾으면 예보 요약을 비운다."""
    _stub_route(
        monkeypatch,
        nowcast={"temp_c": 28.0, "pty": 0, "observed_at": _observed(0.5)},
        region=None,
    )
    body = _get().json()
    assert body["today"] is None
    assert body["province"] is None


def test_weather_now_rejects_out_of_country(monkeypatch):
    """국내 범위 밖 좌표는 검증 단계에서 거절한다."""
    _stub_route(monkeypatch)
    assert _get(lat=10.0, lng=100.0).status_code == 422


def test_coordinates_inside_box_but_off_grid_are_rejected(monkeypatch):
    """허용 위경도 안이어도 격자판을 벗어나면 422 로 거절한다.

    허용 사각형의 남동쪽 모서리는 격자로 바꾸면 판 밖으로 나간다.
    """
    _stub_route(monkeypatch)
    assert _get(lat=33.0, lng=132.0).status_code == 422
