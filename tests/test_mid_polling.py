"""중기예보 폴링 루프와 신선도 감시 테스트.

중기는 하루 두 번만 발표되어 한 번 놓치면 열두 시간이 빈다. 그 공백이
생기는 경로와, 다시 채우는 경로를 DB·외부 호출 없이 검증한다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.clients.hub_clients import KMAApiError
from app.db.forecast_repo import SubscribedGrid
from app.scheduler import hub_scheduler

KST = ZoneInfo("Asia/Seoul")


def _grid(gid: int, land: str, temp: str) -> SubscribedGrid:
    return SubscribedGrid(
        grid_id=gid, label=f"g{gid}", nx=60, ny=127,
        mid_land_reg_id=land, mid_temp_reg_id=temp, lv1="서울특별시",
    )


class _FakeKma:
    """중기 조회를 고정 payload 로 답하는 대역.

    fail_once 에 든 구역은 첫 호출만 실패하고 다음 라운드에서는 성공한다
    — 무한 재시도 없이 실패 처리 경로를 지나가게 하려는 것이다.
    """

    def __init__(self, fail_once: set[str] | None = None) -> None:
        self.fail_once = set(fail_once or ())
        self.land_calls: list[str] = []
        self.temp_calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def _maybe_fail(self, reg_id: str) -> None:
        if reg_id in self.fail_once:
            self.fail_once.discard(reg_id)
            raise KMAApiError("BOOM", "forced")

    async def fetch_mid_land(self, reg_id, _tm_fc_str):
        self.land_calls.append(reg_id)
        self._maybe_fail(reg_id)
        return {"regId": reg_id}

    async def fetch_mid_temp(self, reg_id, _tm_fc_str):
        self.temp_calls.append(reg_id)
        self._maybe_fail(reg_id)
        return {"regId": reg_id}


def _stub_loop(monkeypatch, grids, kma, *, loaded_land=None,
               loaded_temp=None):
    """루프가 부르는 조회·적재·대기를 전부 대역으로 갈아끼운다."""
    async def _grids():
        return grids

    async def _land_loaded(_tm_fc):
        return set(loaded_land or ())

    async def _temp_loaded(_tm_fc):
        return set(loaded_temp or ())

    async def _upsert(*_a, **_k):
        return 1

    async def _sleep(_sec):
        return None

    monkeypatch.setattr(hub_scheduler, "load_active_grids", _grids)
    monkeypatch.setattr(
        hub_scheduler, "loaded_mid_land_regs", _land_loaded
    )
    monkeypatch.setattr(
        hub_scheduler, "loaded_mid_temp_regs", _temp_loaded
    )
    monkeypatch.setattr(hub_scheduler, "upsert_mid_land", _upsert)
    monkeypatch.setattr(hub_scheduler, "upsert_mid_temp", _upsert)
    monkeypatch.setattr(hub_scheduler, "KMAClient", lambda _key: kma)
    monkeypatch.setattr(hub_scheduler.asyncio, "sleep", _sleep)


def test_shared_regions_are_polled_once_per_round(monkeypatch):
    """여러 격자가 같은 구역을 공유하면 한 번만 조회한다.

    서울·인천·경기가 같은 육상 구역을 쓰듯 공유가 흔하다. 격자 단위로
    돌면 같은 구역을 격자 수만큼 반복해서 부른다.
    """
    grids = [
        _grid(1, "11B00000", "11B10101"),
        _grid(2, "11B00000", "11B20201"),
        _grid(3, "11B00000", "11B20601"),
    ]
    kma = _FakeKma()
    _stub_loop(monkeypatch, grids, kma)
    asyncio.run(hub_scheduler.mid_term_polling_loop())
    assert kma.land_calls == ["11B00000"]
    assert sorted(kma.temp_calls) == [
        "11B10101", "11B20201", "11B20601"
    ]


def test_already_loaded_regions_are_skipped(monkeypatch):
    """이미 받은 구역은 다시 부르지 않는다."""
    grids = [
        _grid(1, "11B00000", "11B10101"),
        _grid(2, "11D10000", "11D10301"),
    ]
    kma = _FakeKma()
    _stub_loop(
        monkeypatch, grids, kma,
        loaded_land={"11B00000"}, loaded_temp={"11B10101"},
    )
    asyncio.run(hub_scheduler.mid_term_polling_loop())
    assert kma.land_calls == ["11D10000"]
    assert kma.temp_calls == ["11D10301"]


def test_round_without_failure_ends_immediately(monkeypatch):
    """다 받았으면 재시도 간격을 기다리지 않고 끝낸다.

    예전에는 성공한 뒤에도 한 번 더 자고 같은 조회를 반복하고서야
    종료했다. 조회 횟수로 그 낭비를 못 박는다.
    """
    grids = [_grid(1, "11B00000", "11B10101")]
    kma = _FakeKma()
    _stub_loop(monkeypatch, grids, kma)
    asyncio.run(hub_scheduler.mid_term_polling_loop())
    assert kma.land_calls == ["11B00000"]


def test_one_failed_region_does_not_stop_the_rest(monkeypatch):
    """구역 하나가 실패해도 나머지와 기온 적재는 계속된다."""
    grids = [
        _grid(1, "11B00000", "11B10101"),
        _grid(2, "11D10000", "11D10301"),
    ]
    kma = _FakeKma(fail_once={"11B00000"})
    _stub_loop(monkeypatch, grids, kma)
    asyncio.run(hub_scheduler.mid_term_polling_loop())
    # 실패한 구역 뒤의 육상 구역과 기온 구역이 모두 처리됐다.
    assert "11D10000" in kma.land_calls
    assert "11B10101" in kma.temp_calls
    assert "11D10301" in kma.temp_calls


def test_failed_region_is_retried_next_round(monkeypatch):
    """실패한 구역은 다음 라운드에서 다시 시도한다."""
    grids = [_grid(1, "11B00000", "11B10101")]
    kma = _FakeKma(fail_once={"11B00000"})
    _stub_loop(monkeypatch, grids, kma)
    asyncio.run(hub_scheduler.mid_term_polling_loop())
    assert kma.land_calls == ["11B00000", "11B00000"]


def test_watchdog_is_quiet_when_the_current_release_is_present(
    monkeypatch,
):
    """기대하는 발표분이 이미 있으면 아무것도 하지 않는다."""
    ran: list = []

    async def _latest():
        return hub_scheduler.parse_kma_tm_fc(
            hub_scheduler.resolve_mid_tm_fc(datetime.now(KST))
        )

    async def _loop():
        ran.append(1)

    monkeypatch.setattr(hub_scheduler, "latest_mid_tm_fc", _latest)
    monkeypatch.setattr(
        hub_scheduler, "mid_term_polling_loop", _loop
    )
    asyncio.run(hub_scheduler.mid_freshness_watchdog())
    assert ran == []


def test_watchdog_refills_when_the_release_is_stale(monkeypatch):
    """발표분이 낡았으면 원인을 가리지 않고 다시 채운다.

    잡이 늦게 깨어나 폐기된 경우뿐 아니라, 폴링이 실패로 끝났거나
    적재분이 지워진 경우에도 같은 공백이 생긴다.
    """
    ran: list = []

    async def _latest():
        return datetime.now(KST) - timedelta(days=2)

    async def _loop():
        ran.append(1)

    monkeypatch.setattr(hub_scheduler, "latest_mid_tm_fc", _latest)
    monkeypatch.setattr(
        hub_scheduler, "mid_term_polling_loop", _loop
    )
    asyncio.run(hub_scheduler.mid_freshness_watchdog())
    assert ran == [1]


def test_watchdog_refills_when_nothing_is_loaded(monkeypatch):
    """적재분이 아예 없는 상태도 낡은 것으로 본다."""
    ran: list = []

    async def _latest():
        return None

    async def _loop():
        ran.append(1)

    monkeypatch.setattr(hub_scheduler, "latest_mid_tm_fc", _latest)
    monkeypatch.setattr(
        hub_scheduler, "mid_term_polling_loop", _loop
    )
    asyncio.run(hub_scheduler.mid_freshness_watchdog())
    assert ran == [1]


def test_scheduler_registers_the_freshness_watch():
    """감시 잡이 스케줄에 실제로 등록된다."""
    sched = hub_scheduler.build_scheduler()
    ids = {job.id for job in sched.get_jobs()}
    assert "kma_mid_watch" in ids
    assert "kma_mid" in ids
