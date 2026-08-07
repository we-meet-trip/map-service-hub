"""중기예보 payload 를 행으로 펼치는 규약 테스트.

실제 upsert 함수를 호출해 DB 로 넘어가는 파라미터를 가로챈다. 규약을
테스트 안에서 다시 구현하면 구현이 바뀌어도 늘 통과해 회귀를 못 잡는다.

전제가 되는 외부 사실(실제 응답으로 확인한 것): 중기예보는 발표 시각에
따라 담기는 일수가 다르다. 06 시 발표는 D+4 부터, 18 시 발표는 D+5 부터
오고 앞쪽 키는 아예 없다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.db import forecast_repo
from app.db.forecast_repo import (
    _safe_int,
    upsert_mid_land,
    upsert_mid_temp,
)

KST = ZoneInfo("Asia/Seoul")
_TM_FC = datetime(2026, 8, 5, 6, 0, tzinfo=KST)


class _Session:
    """execute 로 넘어온 파라미터만 붙잡아 두는 세션 대역."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def execute(self, _sql, params=None):
        self._sink.append(params)
        return None


class _SessionCtx:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def __aenter__(self):
        return _Session(self._sink)

    async def __aexit__(self, *_exc):
        return False


class _Db:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def session(self):
        return _SessionCtx(self._sink)


@pytest.fixture()
def rows(monkeypatch) -> list:
    """upsert 가 DB 로 넘긴 row 목록을 모아 주는 픽스처."""
    sink: list = []
    monkeypatch.setattr(
        forecast_repo, "get_hub_db", lambda: _Db(sink)
    )
    return sink


def _land_payload(days: list[int]) -> dict:
    payload: dict = {"regId": "11B00000"}
    for d in days:
        if d <= 7:
            for sfx in ("Am", "Pm"):
                payload[f"wf{d}{sfx}"] = "맑음"
                payload[f"rnSt{d}{sfx}"] = 30
        else:
            payload[f"wf{d}"] = "맑음"
            payload[f"rnSt{d}"] = 10
    return payload


def _temp_payload(days: list[int]) -> dict:
    payload: dict = {"regId": "11B10101"}
    for d in days:
        for sfx in ("", "Low", "High"):
            payload[f"taMin{d}{sfx}"] = d
            payload[f"taMax{d}{sfx}"] = d + 5
    return payload


# ── _safe_int ────────────────────────────────────────────────────

def test_safe_int_handles_none_and_empty():
    """None / 빈 문자열 / 정수 변환 불가 문자열 모두 None 반환."""
    assert _safe_int(None) is None
    assert _safe_int("") is None
    assert _safe_int("abc") is None


def test_safe_int_parses_int_and_str_int():
    """int / 정수 형식 문자열 / 0 의 정상 변환."""
    assert _safe_int(60) == 60
    assert _safe_int("60") == 60
    assert _safe_int(0) == 0


# ── 육상예보 펼치기 ──────────────────────────────────────────────

def test_morning_land_payload_expands_to_eleven_rows(rows):
    """06 시 발표는 D+4 부터라 오전·오후 8 건과 단일 3 건이 나온다."""
    n = asyncio.run(upsert_mid_land(
        "11B00000", _TM_FC, _land_payload(list(range(4, 11)))
    ))
    assert n == 11
    produced = rows[0]
    assert len(produced) == 11
    offsets = sorted({r["fcst_day_offset"] for r in produced})
    assert offsets == [4, 5, 6, 7, 8, 9, 10]
    assert sum(1 for r in produced if r["am_pm"] in ("AM", "PM")) == 8
    assert sum(1 for r in produced if r["am_pm"] == "NA") == 3


def test_evening_land_payload_skips_the_absent_day(rows):
    """18 시 발표에는 D+4 키가 없다 — 그 날의 빈 행을 만들면 안 된다.

    값이 전부 비어 있는 행을 넣으면 적재 여부 판정이 그 날을 이미 받은
    것으로 보아 재시도를 막고, 조회 쪽에서도 결측이 아니라 "값 없는
    예보"로 나간다.
    """
    n = asyncio.run(upsert_mid_land(
        "11B00000", _TM_FC, _land_payload(list(range(5, 11)))
    ))
    assert n == 9
    offsets = sorted({r["fcst_day_offset"] for r in rows[0]})
    assert offsets == [5, 6, 7, 8, 9, 10]


def test_land_payload_without_any_day_writes_nothing(rows):
    """쓸 날이 하나도 없으면 아무것도 적재하지 않는다."""
    n = asyncio.run(
        upsert_mid_land("11B00000", _TM_FC, {"regId": "11B00000"})
    )
    assert n == 0
    assert rows == []


def test_land_row_carries_weather_and_rain(rows):
    """행에는 하늘상태와 강수확률이 그대로 실린다."""
    asyncio.run(
        upsert_mid_land("11B00000", _TM_FC, _land_payload([5]))
    )
    first = rows[0][0]
    assert first["weather"] == "맑음"
    assert first["rain_prob_pct"] == 30
    assert first["reg_id"] == "11B00000"
    assert first["tm_fc"] == _TM_FC


# ── 기온예보 펼치기 ──────────────────────────────────────────────

def test_morning_temp_payload_expands_to_seven_rows(rows):
    """06 시 발표는 D+4 부터 7 일치가 하루 한 행씩 나온다."""
    n = asyncio.run(upsert_mid_temp(
        "11B10101", _TM_FC, _temp_payload(list(range(4, 11)))
    ))
    assert n == 7
    produced = rows[0]
    assert sorted(r["fcst_day_offset"] for r in produced) == list(
        range(4, 11)
    )
    assert produced[0]["ta_min"] == 4
    assert produced[0]["ta_max"] == 9


def test_evening_temp_payload_skips_the_absent_day(rows):
    """18 시 발표에는 D+4 기온 키가 없다."""
    n = asyncio.run(upsert_mid_temp(
        "11B10101", _TM_FC, _temp_payload(list(range(5, 11)))
    ))
    assert n == 6
    assert sorted(
        r["fcst_day_offset"] for r in rows[0]
    ) == list(range(5, 11))


def test_temp_payload_keeps_low_and_high_bounds(rows):
    """예보 구간의 하한·상한도 함께 저장한다."""
    asyncio.run(
        upsert_mid_temp("11B10101", _TM_FC, _temp_payload([5]))
    )
    first = rows[0][0]
    assert first["ta_min_low"] == 5
    assert first["ta_min_high"] == 5
    assert first["ta_max_low"] == 10
    assert first["ta_max_high"] == 10
