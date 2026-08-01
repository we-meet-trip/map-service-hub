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

from datetime import date, datetime
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

class _FakeKma:
    """실황 응답을 고정값으로 돌려주는 대역."""

    def __init__(self, obs: dict[str, str]) -> None:
        self.obs = obs
        self.calls: list[tuple] = []

    async def fetch_nowcast(self, nx, ny, base_date, base_time):
        self.calls.append((nx, ny, base_date, base_time))
        return self.obs


def _stub_route(monkeypatch, *, obs, snapshot=None, region=None, air=None):
    """라우트가 부르는 외부 의존을 전부 대역으로 갈아끼운다."""
    monkeypatch.setattr(
        hub_routers, "get_kma_client", lambda: _FakeKma(obs)
    )
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)

    async def _upsert(*_a, **_k):
        return None

    async def _snapshot(*_a, **_k):
        return snapshot

    async def _region(*_a, **_k):
        return region

    async def _short(*_a, **_k):
        return []

    async def _air(*_a, **_k):
        return air

    monkeypatch.setattr(hub_routers, "upsert_nowcast_snapshot", _upsert)
    monkeypatch.setattr(hub_routers, "fetch_nowcast_snapshot", _snapshot)
    monkeypatch.setattr(hub_routers, "lookup_region_by_grid", _region)
    monkeypatch.setattr(hub_routers, "fetch_short_term_range", _short)
    monkeypatch.setattr(hub_routers, "_fetch_air", _air)


def test_weather_now_returns_observation(monkeypatch):
    """실황이 있으면 지금 기온과 격자를 담아 200 을 준다."""
    _stub_route(monkeypatch, obs={"T1H": "28", "PTY": "0"})
    resp = _client().get(
        "/v1/weather/now", params={"lat": 37.5665, "lng": 126.9780}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["now"]["temp_c"] == 28.0
    assert body["now"]["pty"] == 0
    assert body["nx"] == 60 and body["ny"] == 127


def test_weather_now_omits_yesterday_when_no_record(monkeypatch):
    """어제 기록이 없으면 비교 항목을 아예 싣지 않는다."""
    _stub_route(monkeypatch, obs={"T1H": "28"}, snapshot=None)
    body = _client().get(
        "/v1/weather/now", params={"lat": 37.5665, "lng": 126.9780}
    ).json()
    assert body["yesterday"] is None


def test_weather_now_includes_yesterday_when_recorded(monkeypatch):
    """어제 기록이 있으면 그 기온과 시각을 함께 싣는다."""
    _stub_route(
        monkeypatch,
        obs={"T1H": "28"},
        snapshot={"temp_c": 30.1, "hour_kst": 9},
    )
    body = _client().get(
        "/v1/weather/now", params={"lat": 37.5665, "lng": 126.9780}
    ).json()
    assert body["yesterday"] == {"temp_c": 30.1, "hour_kst": 9}


def test_weather_now_omits_today_without_region(monkeypatch):
    """격자로 행정구역을 못 찾으면 예보 요약을 비운다."""
    _stub_route(monkeypatch, obs={"T1H": "28"}, region=None)
    body = _client().get(
        "/v1/weather/now", params={"lat": 37.5665, "lng": 126.9780}
    ).json()
    assert body["today"] is None
    assert body["province"] is None


def test_weather_now_rejects_out_of_country(monkeypatch):
    """국내 범위 밖 좌표는 검증 단계에서 거절한다."""
    _stub_route(monkeypatch, obs={"T1H": "28"})
    resp = _client().get(
        "/v1/weather/now", params={"lat": 10.0, "lng": 100.0}
    )
    assert resp.status_code == 422


def test_weather_now_fails_when_temperature_missing(monkeypatch):
    """기온이 없는 실황은 카드를 그릴 수 없어 실패로 돌린다."""
    _stub_route(monkeypatch, obs={"REH": "80"})
    resp = _client().get(
        "/v1/weather/now", params={"lat": 37.5665, "lng": 126.9780}
    )
    assert resp.status_code == 502


def test_snapshot_date_uses_base_not_wall_clock(monkeypatch):
    """스냅샷은 발표 일자로 남긴다 — 자정 직후 요청이 어제로 기록된다."""
    captured: dict = {}

    async def _capture(day, hour, nx, ny, temp_c, pty):
        captured.update(
            {"day": day, "hour": hour, "nx": nx, "ny": ny}
        )

    _stub_route(monkeypatch, obs={"T1H": "28"})
    monkeypatch.setattr(hub_routers, "upsert_nowcast_snapshot", _capture)
    monkeypatch.setattr(
        hub_routers,
        "resolve_nowcast_base",
        lambda _now: ("20260731", "2300"),
    )
    _client().get(
        "/v1/weather/now", params={"lat": 37.5665, "lng": 126.9780}
    )
    assert captured["day"] == date(2026, 7, 31)
    assert captured["hour"] == 23
