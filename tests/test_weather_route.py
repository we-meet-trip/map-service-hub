"""GET /v1/weather 응답 조립 테스트.

DB 없이 돌리기 위해 조회 함수만 대역으로 갈아끼우고, 라우터가 그 결과를
어떤 날짜에 어떻게 붙이는지를 검증한다.

가장 중요한 것은 **중기 예보가 실린 날짜**다. 저장된 offset 은 발표일
기준인데 조회를 벽시계 오늘 기준으로 하면, 발표가 하루 지난 상태에서
모든 값이 하루씩 밀린다. 값 자체는 멀쩡해 보여서 응답만으로는 알 수 없다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.forecast_repo import RegionLookup
from app.routers import hub_routers
from app.routers.hub_routers import router as hub_router

KST = ZoneInfo("Asia/Seoul")
_TODAY = date(2026, 8, 5)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


def _region(lv2: str = "강남구") -> RegionLookup:
    return RegionLookup(
        admin_code="1168000000",
        lv1="서울특별시",
        lv2=lv2,
        nx=60,
        ny=127,
        mid_land_reg_id="11B00000",
        mid_temp_reg_id="11B10101",
    )


def _short_row(day: date, category: str, value: str, hour: int = 12):
    return {
        "date": day,
        "category": category,
        "fcst_value": value,
        "fcst_at": datetime(
            day.year, day.month, day.day, hour, tzinfo=KST
        ),
    }


def _full_day(day: date) -> list[dict]:
    return [
        _short_row(day, "TMN", "24", hour=6),
        _short_row(day, "TMX", "33", hour=15),
        _short_row(day, "POP", "30"),
        _short_row(day, "SKY", "1"),
    ]


def _stub(
    monkeypatch,
    *,
    region: RegionLookup | None = None,
    short_rows: list[dict] | None = None,
    short_base_at: datetime | None = None,
    land: tuple[list[dict], datetime | None] = ([], None),
    temp: tuple[list[dict], datetime | None] = ([], None),
):
    """라우트가 부르는 조회를 전부 대역으로 갈아끼운다."""
    async def _lookup(*_a, **_k):
        return _region() if region is None else region

    async def _short(*_a, **_k):
        return list(short_rows or []), short_base_at

    async def _land(*_a, **_k):
        return land

    async def _temp(*_a, **_k):
        return temp

    monkeypatch.setattr(hub_routers, "lookup_region_by_name", _lookup)
    monkeypatch.setattr(hub_routers, "fetch_short_term_range", _short)
    monkeypatch.setattr(hub_routers, "fetch_mid_land_range", _land)
    monkeypatch.setattr(hub_routers, "fetch_mid_temp_range", _temp)
    monkeypatch.setattr(hub_routers, "_today_kst", lambda: _TODAY)


def _get(start: date, end: date):
    return _client().get(
        "/v1/weather",
        params={
            "province": "서울특별시",
            "city": "강남구",
            "date_start": start.isoformat(),
            "date_end": end.isoformat(),
        },
    )


def _by_date(body: dict) -> dict[str, dict]:
    return {d["date"]: d for d in body["daily"]}


# ── 중기 예보가 실리는 날짜 ───────────────────────────────────────

def test_mid_offsets_are_relative_to_announcement_day(monkeypatch):
    """저장된 offset 은 발표일 기준으로 해석해야 한다.

    발표가 어제(08-04)이면 offset 5 는 08-09 예보다. 벽시계 오늘(08-05)
    기준으로 읽으면 그 값이 08-10 자리에 실려 전 구간이 하루 밀린다.
    """
    tm_fc = datetime(2026, 8, 4, 18, 0, tzinfo=KST)
    land = (
        [
            {"offset": 5, "am_pm": "AM", "weather": "맑음",
             "rain_prob_pct": 10},
            {"offset": 6, "am_pm": "AM", "weather": "흐림",
             "rain_prob_pct": 80},
        ],
        tm_fc,
    )
    temp = (
        [
            {"offset": 5, "ta_min": 26, "ta_max": 36},
            {"offset": 6, "ta_min": 27, "ta_max": 37},
        ],
        tm_fc,
    )
    _stub(monkeypatch, land=land, temp=temp)
    body = _get(
        _TODAY + timedelta(days=4), _TODAY + timedelta(days=5)
    ).json()
    daily = _by_date(body)
    # 발표일(08-04) + 5 = 08-09
    assert daily["2026-08-09"]["sky_condition"] == "맑음"
    assert daily["2026-08-09"]["temp_max"] == 36
    # 발표일 + 6 = 08-10
    assert daily["2026-08-10"]["sky_condition"] == "흐림"
    assert daily["2026-08-10"]["temp_max"] == 37


def test_day_beyond_stored_offsets_is_missing_not_shifted(monkeypatch):
    """발표분이 담지 않은 날은 옆 날 값으로 메우지 않고 결측으로 둔다."""
    tm_fc = datetime(2026, 8, 4, 18, 0, tzinfo=KST)
    land = ([{"offset": 10, "am_pm": "NA", "weather": "맑음",
              "rain_prob_pct": 10}], tm_fc)
    _stub(monkeypatch, land=land, temp=([], tm_fc))
    target = _TODAY + timedelta(days=10)  # 발표일 기준 11 — 저장 범위 밖
    body = _get(target, target).json()
    assert body["daily"] == []
    assert body["missing_dates"] == [target.isoformat()]


def test_land_and_temp_may_come_from_different_announcements(monkeypatch):
    """한쪽만 새 발표분을 받아도 같은 대상일끼리 합쳐진다."""
    land_tm = datetime(2026, 8, 5, 6, 0, tzinfo=KST)
    temp_tm = datetime(2026, 8, 4, 18, 0, tzinfo=KST)
    land = ([{"offset": 4, "am_pm": "AM", "weather": "맑음",
              "rain_prob_pct": 10}], land_tm)
    temp = ([{"offset": 5, "ta_min": 26, "ta_max": 36}], temp_tm)
    _stub(monkeypatch, land=land, temp=temp)
    target = _TODAY + timedelta(days=4)  # 08-09
    body = _get(target, target).json()
    daily = _by_date(body)
    assert daily["2026-08-09"]["sky_condition"] == "맑음"
    assert daily["2026-08-09"]["temp_max"] == 36
    assert daily["2026-08-09"]["source"] == "mid_land+mid_temp"


# ── D+3 처리 ──────────────────────────────────────────────────────

def test_d3_uses_short_term_when_the_day_is_complete(monkeypatch):
    """D+3 은 단기에 하루가 온전하면 단기로 채운다."""
    d3 = _TODAY + timedelta(days=3)
    _stub(monkeypatch, short_rows=_full_day(d3))
    body = _get(d3, d3).json()
    daily = _by_date(body)
    assert daily[d3.isoformat()]["source"] == "short_term"
    assert daily[d3.isoformat()]["temp_max"] == 33
    assert body["missing_dates"] == []


def test_d3_falls_back_to_mid_when_short_term_is_truncated(monkeypatch):
    """단기가 잘려 있으면 중기로 넘어간다."""
    d3 = _TODAY + timedelta(days=3)
    tm_fc = datetime(2026, 8, 4, 6, 0, tzinfo=KST)  # 발표일 + 4 = 08-08
    truncated = [_short_row(d3, "TMP", "22", hour=2)]
    land = ([{"offset": 4, "am_pm": "AM", "weather": "구름많음",
              "rain_prob_pct": 40}], tm_fc)
    _stub(monkeypatch, short_rows=truncated, land=land, temp=([], tm_fc))
    body = _get(d3, d3).json()
    daily = _by_date(body)
    assert daily[d3.isoformat()]["source"] == "mid_land"
    assert daily[d3.isoformat()]["sky_condition"] == "구름많음"


def test_d3_is_reported_once_when_neither_source_has_it(monkeypatch):
    """양쪽 다 없으면 결측으로 한 번만 적는다."""
    d3 = _TODAY + timedelta(days=3)
    _stub(monkeypatch)
    body = _get(d3, d3).json()
    assert body["daily"] == []
    assert body["missing_dates"] == [d3.isoformat()]


def test_no_date_appears_twice_across_the_whole_range(monkeypatch):
    """겹침 구간을 두 경로로 처리해도 같은 날이 두 번 실리지 않는다."""
    d3 = _TODAY + timedelta(days=3)
    tm_fc = datetime(2026, 8, 5, 6, 0, tzinfo=KST)
    rows = _full_day(_TODAY) + _full_day(d3)
    land = ([{"offset": o, "am_pm": "AM", "weather": "맑음",
              "rain_prob_pct": 10} for o in range(4, 11)], tm_fc)
    temp = ([{"offset": o, "ta_min": 20, "ta_max": 30}
             for o in range(4, 11)], tm_fc)
    _stub(monkeypatch, short_rows=rows, land=land, temp=temp)
    body = _get(_TODAY, _TODAY + timedelta(days=10)).json()
    dates = [d["date"] for d in body["daily"]] + body["missing_dates"]
    assert len(dates) == len(set(dates))
    assert len(dates) == 11


# ── 응답 계약 ─────────────────────────────────────────────────────

def test_region_fallback_is_visible_in_the_response(monkeypatch):
    """광역 대표로 대신 답했다는 사실을 응답에 남긴다.

    city 에는 요청 문자열이 그대로 실리므로, 표시가 없으면 소비자는
    시군구 예보를 받은 것으로 오인한다.
    """
    _stub(monkeypatch, region=_region(lv2=""))
    body = _get(_TODAY, _TODAY).json()
    assert body["region_fallback"] is True
    assert body["city"] == "강남구"


def test_freshness_fields_expose_the_announcement_times(monkeypatch):
    """예보가 언제 발표분인지 응답만 보고 알 수 있어야 한다."""
    base_at = datetime(2026, 8, 5, 5, 0, tzinfo=KST)
    tm_fc = datetime(2026, 8, 5, 6, 0, tzinfo=KST)
    _stub(
        monkeypatch,
        short_rows=_full_day(_TODAY),
        short_base_at=base_at,
        land=([], tm_fc),
        temp=([], tm_fc),
    )
    body = _get(_TODAY, _TODAY + timedelta(days=4)).json()
    assert body["short_term_base_at"] is not None
    assert body["mid_land_tm_fc"] is not None
    assert body["mid_temp_tm_fc"] is not None


def test_range_of_exactly_fourteen_days_is_allowed(monkeypatch):
    """상한은 총 14 일이다 — 경계에서 하루 더 받아들이면 안 된다."""
    _stub(monkeypatch)
    ok = _get(_TODAY, _TODAY + timedelta(days=13))
    too_long = _get(_TODAY, _TODAY + timedelta(days=14))
    assert ok.status_code == 200
    assert too_long.status_code == 400


def test_range_entirely_in_the_past_is_rejected(monkeypatch):
    """전부 지난 날짜면 빈 응답이 아니라 잘못된 요청으로 답한다."""
    _stub(monkeypatch)
    resp = _get(_TODAY - timedelta(days=3), _TODAY - timedelta(days=1))
    assert resp.status_code == 400


def test_partial_past_still_returns_the_future_part(monkeypatch):
    """시작일만 지난 요청은 남은 날짜를 정상 처리한다."""
    _stub(monkeypatch, short_rows=_full_day(_TODAY))
    body = _get(_TODAY - timedelta(days=1), _TODAY).json()
    assert _TODAY.isoformat() in _by_date(body)
    assert body["missing_dates"] == [
        (_TODAY - timedelta(days=1)).isoformat()
    ]


def test_missing_mid_codes_do_not_break_the_request(monkeypatch):
    """중기 코드가 없는 지역은 그 날짜만 결측으로 둔다."""
    region = RegionLookup(
        admin_code="1168000000", lv1="서울특별시", lv2="강남구",
        nx=60, ny=127,
        mid_land_reg_id=None, mid_temp_reg_id=None,
    )
    _stub(monkeypatch, region=region)
    target = _TODAY + timedelta(days=5)
    body = _get(target, target).json()
    assert body["daily"] == []
    assert body["missing_dates"] == [target.isoformat()]
