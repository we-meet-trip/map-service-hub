"""실황·대기오염 사전 적재 폴링 테스트.

이 두 값은 화면이 열릴 때가 아니라 매시 미리 받아 둔다. 조회 경로는 저장된
값만 읽으므로, 여기서 적재가 끊기면 화면에서 그 항목이 사라진다.

다루는 범위:
  - 시도 대표 격자만 실황을 받는다(격자 전체를 받지 않는다)
  - 한 격자/한 시도의 실패가 나머지 적재를 막지 않는다
  - 기온이 없는 발표분은 남기지 않는다
  - 키가 없으면 발급처를 부르지 않는다
  - 발급처 측정 시각 문자열 해석(24시 표기 포함)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.clients.hub_clients import AirKoreaApiError, KMAApiError
from app.db.forecast_repo import _parse_air_data_time
from app.scheduler import hub_scheduler
from app.utils.kma_grid import KST


class _Grid:
    """시도 대표 격자 대역."""

    def __init__(self, label: str, nx: int, ny: int) -> None:
        self.label = label
        self.nx = nx
        self.ny = ny


def _stub_common(monkeypatch, grids):
    """폴링 루프가 건드리는 바깥 세계를 전부 대역으로 갈아끼운다."""

    async def _sido_grids():
        return grids

    async def _sleep(*_a, **_k):
        return None

    monkeypatch.setattr(hub_scheduler, "load_sido_grids", _sido_grids)
    monkeypatch.setattr(hub_scheduler.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        hub_scheduler, "places_stub_active", lambda _k: False
    )


class _FakeKma:
    """실황 응답 대역. 격자별로 값을 다르게 주거나 예외를 던진다."""

    def __init__(self, by_grid: dict) -> None:
        self.by_grid = by_grid
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def fetch_nowcast(self, nx, ny, base_date, base_time):
        self.calls.append((nx, ny))
        value = self.by_grid.get((nx, ny))
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_nowcast_polls_only_sido_grids(monkeypatch):
    """대표 격자 수만큼만 발급처를 부르고, 받은 값을 그대로 남긴다."""
    grids = [_Grid("서울특별시", 60, 127), _Grid("부산광역시", 98, 76)]
    _stub_common(monkeypatch, grids)

    kma = _FakeKma({
        (60, 127): {"T1H": "28.5", "PTY": "0"},
        (98, 76): {"T1H": "30", "PTY": "1"},
    })
    monkeypatch.setattr(hub_scheduler, "KMAClient", lambda _k: kma)

    saved: list = []

    async def _upsert(day, hour, nx, ny, temp_c, pty):
        saved.append((nx, ny, temp_c, pty))

    monkeypatch.setattr(hub_scheduler, "upsert_nowcast_snapshot", _upsert)

    await hub_scheduler.nowcast_polling_loop()

    assert kma.calls == [(60, 127), (98, 76)]
    assert saved == [(60, 127, 28.5, 0), (98, 76, 30.0, 1)]


@pytest.mark.asyncio
async def test_nowcast_one_grid_failure_does_not_stop_the_rest(monkeypatch):
    """한 격자가 실패해도 나머지 격자는 계속 받는다.

    실패한 격자는 다음 시각에 다시 받으며, 그 사이 조회는 직전 값으로
    답한다. 여기서 루프가 멎으면 뒤쪽 시도 전체가 빈 채로 남는다.
    """
    grids = [
        _Grid("서울특별시", 60, 127),
        _Grid("부산광역시", 98, 76),
        _Grid("제주특별자치도", 52, 38),
    ]
    _stub_common(monkeypatch, grids)

    kma = _FakeKma({
        (60, 127): KMAApiError("HTTP_ERR", "boom"),
        (98, 76): {"T1H": "30"},
        (52, 38): {"T1H": "27"},
    })
    monkeypatch.setattr(hub_scheduler, "KMAClient", lambda _k: kma)

    saved: list = []

    async def _upsert(day, hour, nx, ny, temp_c, pty):
        saved.append((nx, ny))

    monkeypatch.setattr(hub_scheduler, "upsert_nowcast_snapshot", _upsert)

    await hub_scheduler.nowcast_polling_loop()

    assert kma.calls == [(60, 127), (98, 76), (52, 38)]
    assert saved == [(98, 76), (52, 38)]


@pytest.mark.asyncio
async def test_nowcast_skips_observation_without_temperature(monkeypatch):
    """기온이 없는 발표분은 남기지 않는다 — 남겨도 쓸 데가 없다."""
    _stub_common(monkeypatch, [_Grid("서울특별시", 60, 127)])
    kma = _FakeKma({(60, 127): {"REH": "80"}})
    monkeypatch.setattr(hub_scheduler, "KMAClient", lambda _k: kma)

    saved: list = []

    async def _upsert(*a, **k):
        saved.append(a)

    monkeypatch.setattr(hub_scheduler, "upsert_nowcast_snapshot", _upsert)

    await hub_scheduler.nowcast_polling_loop()
    assert saved == []


@pytest.mark.asyncio
async def test_nowcast_skips_entirely_without_key(monkeypatch):
    """키가 없으면 발급처를 부르지 않는다(스텁 모드)."""
    _stub_common(monkeypatch, [_Grid("서울특별시", 60, 127)])
    monkeypatch.setattr(
        hub_scheduler, "places_stub_active", lambda _k: True
    )

    def _boom(_k):
        raise AssertionError("키가 없는데 발급처를 불렀다")

    monkeypatch.setattr(hub_scheduler, "KMAClient", _boom)
    await hub_scheduler.nowcast_polling_loop()


class _FakeAir:
    """대기오염 응답 대역."""

    def __init__(self, by_sido: dict) -> None:
        self.by_sido = by_sido
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def fetch_sido_realtime(self, sido):
        self.calls.append(sido)
        value = self.by_sido.get(sido, [])
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.asyncio
async def test_air_polls_every_sido_and_isolates_failure(monkeypatch):
    """시도를 모두 돌고, 한 시도의 실패가 나머지를 막지 않는다."""
    _stub_common(monkeypatch, [])

    air = _FakeAir({"서울": [{"stationName": "중구"}]})
    # 한 시도만 실패시킨다.
    air.by_sido["경기"] = AirKoreaApiError("HTTP_ERR", "boom")
    monkeypatch.setattr(hub_scheduler, "AirKoreaClient", lambda _k: air)

    stored: list = []

    async def _upsert(sido, items):
        stored.append(sido)
        return len(items)

    monkeypatch.setattr(hub_scheduler, "upsert_air_snapshots", _upsert)

    await hub_scheduler.air_polling_loop()

    # 발급처 표기 기준 17개 시도를 모두 훑는다.
    assert len(air.calls) == 17
    assert "서울" in air.calls and "경기" in air.calls
    assert "경기" not in stored, "실패한 시도는 저장하지 않는다"
    assert "서울" in stored


@pytest.mark.asyncio
async def test_air_skips_entirely_without_key(monkeypatch):
    """키가 없으면 발급처를 부르지 않는다."""
    _stub_common(monkeypatch, [])
    monkeypatch.setattr(
        hub_scheduler, "places_stub_active", lambda _k: True
    )

    def _boom(_k):
        raise AssertionError("키가 없는데 발급처를 불렀다")

    monkeypatch.setattr(hub_scheduler, "AirKoreaClient", _boom)
    await hub_scheduler.air_polling_loop()


# ── 폴링 시각과 신선도 한계의 관계 ──────────────────────────────
#
# 이 둘은 따로 정해 두면 조용히 어긋난다. 폴링을 이르게 걸면 받는 값이 이미
# 한 시간 넘게 묵은 것이라, 다음 폴링 전에 "지금 값" 한계를 넘겨 화면에서
# 기온이 사라진다. 실제로 그랬다 — 매시 끝자락 몇 분 동안 카드가 비었다.

def test_nowcast_poll_minute_is_after_publication():
    """실황 폴링은 발표 시각을 지나서 물어야 한다.

    발급처는 매시 :40 무렵 그 시각 관측을 내놓는다. 그 전에 물으면 한 시간
    전 관측이 돌아온다(resolve_nowcast_base 가 물러선다).
    """
    from app.scheduler.hub_scheduler import build_scheduler

    job = build_scheduler().get_job("kma_nowcast")
    minute = str(job.trigger.fields[job.trigger.FIELD_NAMES.index("minute")])
    assert int(minute) >= 45, (
        f"실황 폴링이 :{minute} 에 걸려 있다. 발표 전이라 한 시간 전 값을 받는다"
    )


def test_stored_nowcast_stays_fresh_until_next_poll():
    """저장한 실황이 다음 폴링 전까지 신선도 한계 안에 머문다.

    폴링 주기(1시간)와 받는 값의 나이를 더한 값이 한계를 넘으면, 매시 끝자락에
    기온이 사라졌다 돌아오기를 반복한다. 사용자에게는 앱이 고장 난 것으로
    보인다.
    """
    from app.scheduler.hub_scheduler import build_scheduler
    from app.config import settings
    from app.utils.kma_grid import resolve_nowcast_base

    job = build_scheduler().get_job("kma_nowcast")
    poll_minute = int(
        str(job.trigger.fields[job.trigger.FIELD_NAMES.index("minute")])
    )

    # 폴링이 도는 순간 받게 되는 발표분이 그때 기준 몇 시간 전인지 센다.
    poll_at = datetime(2026, 8, 11, 12, poll_minute, tzinfo=KST)
    base_date, base_time = resolve_nowcast_base(poll_at)
    observed = datetime(
        int(base_date[:4]), int(base_date[4:6]), int(base_date[6:]),
        int(base_time[:2]), tzinfo=KST,
    )
    age_at_store = poll_at - observed

    # 다음 폴링 직전이 가장 오래된 순간이다.
    worst_age = age_at_store + timedelta(hours=1)
    limit = timedelta(hours=settings.WEATHER_NOW_MAX_AGE_HOURS)
    assert worst_age < limit, (
        f"다음 폴링 직전 나이 {worst_age} 가 한계 {limit} 를 넘는다 — "
        f"매시 끝자락에 기온이 사라진다"
    )


# ── 측정 시각 해석 ────────────────────────────────────────────────

def test_air_data_time_parses_ordinary_form():
    """보통 표기는 그대로 KST 시각이 된다."""
    got = _parse_air_data_time("2026-08-11 16:00")
    assert got == datetime(2026, 8, 11, 16, 0, tzinfo=KST)


def test_air_data_time_handles_24_hour_form():
    """발급처는 자정을 전날 24시로 적는다 — 다음 날 0시로 옮긴다.

    그대로 파싱하면 실패해서 그 시각 측정분이 통째로 버려진다.
    """
    got = _parse_air_data_time("2026-08-11 24:00")
    assert got == datetime(2026, 8, 12, 0, 0, tzinfo=KST)


@pytest.mark.parametrize("raw", [None, "", "어제", "2026-08-11", 123])
def test_air_data_time_rejects_unusable(raw):
    """읽을 수 없는 값은 None — 그 측정소만 건너뛴다."""
    assert _parse_air_data_time(raw) is None


def test_air_data_time_is_timezone_aware():
    """시각대가 붙어 있어야 저장·비교가 어긋나지 않는다.

    tz 없이 넣으면 DB 가 서버 시각대로 해석해, 신선도 판정이 아홉 시간
    어긋난다.
    """
    got = _parse_air_data_time("2026-08-11 16:00")
    assert got.tzinfo is not None
    assert got.utcoffset() == timedelta(hours=9)


@pytest.mark.asyncio
async def test_short_term_skips_entirely_without_key(monkeypatch):
    """단기예보도 키가 없으면 발급처를 부르지 않는다.

    실황에는 이 가드가 있었는데 단기·중기에는 없었다. 그래서 키 없는 환경에서
    격자 수만큼 401 을 받아 가며 재시도해, 로그 수백 줄이 쌓이고 그 사이에
    진짜 문제가 묻혔다.
    """
    monkeypatch.setattr(
        hub_scheduler, "places_stub_active", lambda _k: True
    )

    def _boom(*_a, **_k):
        raise AssertionError("키가 없는데 격자를 조회했다")

    monkeypatch.setattr(hub_scheduler, "load_active_grids", _boom)
    await hub_scheduler.short_term_polling_loop()


@pytest.mark.asyncio
async def test_mid_term_skips_entirely_without_key(monkeypatch):
    """중기예보도 같다."""
    monkeypatch.setattr(
        hub_scheduler, "places_stub_active", lambda _k: True
    )

    def _boom(*_a, **_k):
        raise AssertionError("키가 없는데 격자를 조회했다")

    monkeypatch.setattr(hub_scheduler, "load_active_grids", _boom)
    await hub_scheduler.mid_term_polling_loop()
