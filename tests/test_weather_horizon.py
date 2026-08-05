"""날짜를 어느 예보로 채울지 정하는 규칙과 일별 집계 테스트.

이 경로가 틀리면 사용자는 "값이 이상하다"가 아니라 **그럴듯한 다른 날의
예보**를 받는다. 눈으로는 잡히지 않으므로 결정을 테스트로 못 박아 둔다.

전제가 되는 외부 사실(실제 응답으로 확인한 것):
  - 중기예보는 발표일 기준 D+4 부터 담겨 온다. 06 시 발표는 4..10,
    18 시 발표는 5..10 이고 D+3 은 어느 발표분에도 없다.
  - 그래서 D+3 은 단기예보로만 채울 수 있고, 단기예보에는 그 날의
    자료가 실제로 들어 있다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.routers.hub_routers import (
    _aggregate_mid,
    _aggregate_short_term,
    _split_dates_by_horizon,
    _tm_fc_kst_date,
)

KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")
_TODAY = date(2026, 8, 5)


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
    """일 최저·최고가 모두 실린 하루."""
    return [
        _short_row(day, "TMN", "24", hour=6),
        _short_row(day, "TMX", "33", hour=15),
        _short_row(day, "TMP", "28"),
        _short_row(day, "POP", "30"),
        _short_row(day, "SKY", "1"),
    ]


# ── 날짜 분할 ─────────────────────────────────────────────────────

def test_d3_is_its_own_bucket_not_mid():
    """D+3 은 단기·중기 어느 한쪽에 고정되지 않고 따로 분류된다.

    중기로 고정하면 중기 테이블에 그 날이 없어 늘 결측이 되고,
    단기로 고정하면 단기가 잘린 날에 폴백할 곳이 없어진다.
    """
    short, overlap, mid, out = _split_dates_by_horizon(
        _TODAY, _TODAY + timedelta(days=4), _TODAY
    )
    assert short == [_TODAY, _TODAY + timedelta(days=1),
                     _TODAY + timedelta(days=2)]
    assert overlap == [_TODAY + timedelta(days=3)]
    assert mid == [_TODAY + timedelta(days=4)]
    assert out == []


def test_every_date_lands_in_exactly_one_bucket():
    """한 날짜가 두 그룹에 들어가면 응답에 같은 날이 두 번 실린다."""
    start = _TODAY - timedelta(days=2)
    end = _TODAY + timedelta(days=12)
    short, overlap, mid, out = _split_dates_by_horizon(
        start, end, _TODAY
    )
    merged = short + overlap + mid + out
    assert len(merged) == len(set(merged))
    assert len(merged) == (end - start).days + 1


def test_past_and_far_future_go_out_of_range():
    """예보 범위 밖은 결측으로 보낸다."""
    _, _, _, out = _split_dates_by_horizon(
        _TODAY - timedelta(days=1), _TODAY - timedelta(days=1), _TODAY
    )
    assert out == [_TODAY - timedelta(days=1)]
    _, _, _, out2 = _split_dates_by_horizon(
        _TODAY + timedelta(days=11), _TODAY + timedelta(days=11), _TODAY
    )
    assert out2 == [_TODAY + timedelta(days=11)]


# ── 발표 시각 → 발표 일자 ─────────────────────────────────────────

def test_tm_fc_is_read_in_kst_not_utc():
    """발표 시각은 KST 로 환산한 뒤 일자를 취해야 한다.

    06 시 발표는 UTC 로 전날 21 시라, 변환 없이 날짜를 뽑으면 발표일이
    하루 앞당겨져 중기 예보 전체가 하루 밀린다.
    """
    tm_fc = datetime(2026, 8, 3, 21, 0, tzinfo=UTC)  # KST 08-04 06:00
    assert _tm_fc_kst_date(tm_fc) == date(2026, 8, 4)
    assert _tm_fc_kst_date(None) is None


# ── 단기 집계 ─────────────────────────────────────────────────────

def test_full_day_is_accepted_when_completeness_required():
    """최저·최고가 다 있으면 완전한 하루로 받아들인다."""
    item = _aggregate_short_term(
        _full_day(_TODAY), _TODAY, require_full=True
    )
    assert item is not None
    assert (item.temp_min, item.temp_max) == (24, 33)


def test_truncated_day_is_rejected_when_completeness_required():
    """최저·최고 중 하나라도 없으면 완전한 하루가 아니다.

    단기예보의 마지막 날은 뒤가 잘려 온다. 남은 시간대만의 min/max 는
    그 날의 최저·최고가 아니라서, 그대로 쓰면 최고기온이 실제보다 크게
    낮아진다. 둘 다 없을 때만 거르면 이 경우가 통과해 버린다.
    """
    only_min = [
        _short_row(_TODAY, "TMN", "24", hour=6),
        _short_row(_TODAY, "TMP", "25", hour=3),
    ]
    assert _aggregate_short_term(
        only_min, _TODAY, require_full=True
    ) is None

    only_max = [
        _short_row(_TODAY, "TMX", "33", hour=15),
        _short_row(_TODAY, "TMP", "31", hour=21),
    ]
    assert _aggregate_short_term(
        only_max, _TODAY, require_full=True
    ) is None


def test_completeness_is_judged_before_temperature_fallback():
    """시간별 기온으로 메운 값은 완전성 판정을 통과시키면 안 된다."""
    partial = [
        _short_row(_TODAY, "TMP", "20", hour=2),
        _short_row(_TODAY, "TMP", "22", hour=5),
    ]
    assert _aggregate_short_term(
        partial, _TODAY, require_full=True
    ) is None
    # 기본 동작은 종전대로 부분 데이터도 채운다.
    loose = _aggregate_short_term(partial, _TODAY)
    assert loose is not None
    assert (loose.temp_min, loose.temp_max) == (20, 22)


def test_missing_value_placeholders_do_not_count_as_present():
    """빈 문자열로 온 항목은 값이 아니다 — 행이 있어도 결측이다."""
    rows = [
        _short_row(_TODAY, "TMN", ""),
        _short_row(_TODAY, "TMX", "33", hour=15),
    ]
    assert _aggregate_short_term(
        rows, _TODAY, require_full=True
    ) is None


def test_day_with_no_usable_value_is_reported_missing():
    """집계할 값이 하나도 없으면 빈 칸을 만들지 않는다.

    값 없는 칸이 응답에 실리면 소비자는 예보가 있다고 오인하고,
    결측 목록에도 안 잡혀 아무도 문제를 모른다.
    """
    rows = [_short_row(_TODAY, "REH", "80"), _short_row(_TODAY, "WSD", "3")]
    assert _aggregate_short_term(rows, _TODAY) is None


def test_out_of_range_precipitation_is_dropped_not_raised():
    """규격 밖 강수확률은 결측으로 떨어뜨린다.

    응답 모델이 0~100 만 받으므로 그대로 넣으면 그 날 하나 때문에
    요청 전체가 실패한다.
    """
    rows = _full_day(_TODAY) + [_short_row(_TODAY, "POP", "-9", hour=3)]
    item = _aggregate_short_term(rows, _TODAY)
    assert item is not None
    assert item.precipitation_prob == 30

    only_bad = [
        _short_row(_TODAY, "TMN", "24", hour=6),
        _short_row(_TODAY, "POP", "999"),
    ]
    item2 = _aggregate_short_term(only_bad, _TODAY)
    assert item2 is not None
    assert item2.precipitation_prob is None


def test_subzero_temperature_rounds_away_from_zero():
    """영하 기온을 0 방향으로 자르면 늘 실제보다 따뜻해진다."""
    rows = [
        _short_row(_TODAY, "TMN", "-3.7", hour=6),
        _short_row(_TODAY, "TMX", "2.4", hour=15),
    ]
    item = _aggregate_short_term(rows, _TODAY)
    assert item is not None
    assert (item.temp_min, item.temp_max) == (-4, 2)


def test_unknown_sky_code_falls_through_to_next_row():
    """모르는 하늘상태 코드가 정오에 가깝다고 그 날을 비우지 않는다."""
    rows = [
        _short_row(_TODAY, "TMN", "24", hour=6),
        _short_row(_TODAY, "SKY", "9", hour=13),
        _short_row(_TODAY, "SKY", "1", hour=9),
    ]
    item = _aggregate_short_term(rows, _TODAY)
    assert item is not None
    assert item.sky_condition == "맑음"


# ── 중기 집계 ─────────────────────────────────────────────────────

def _land(offset: int, ampm: str, weather: str, rain: int | None):
    return {
        "offset": offset, "am_pm": ampm,
        "weather": weather, "rain_prob_pct": rain,
    }


def _temp(offset: int, lo: int, hi: int):
    return {"offset": offset, "ta_min": lo, "ta_max": hi}


def test_mid_uses_each_source_own_offset():
    """육상과 기온의 발표분이 다르면 각자의 기준으로 맞춘다.

    한쪽 폴링만 성공하면 두 테이블의 최신 발표 시각이 달라진다. offset
    하나로 합치면 하루 어긋난 기온이 다른 날의 하늘상태에 붙는다.
    """
    day = _TODAY + timedelta(days=5)
    land_rows = [_land(5, "AM", "맑음", 10), _land(6, "AM", "흐림", 80)]
    temp_rows = [_temp(5, 20, 30), _temp(6, 21, 31)]
    item = _aggregate_mid(
        land_rows, temp_rows, land_offset=5, temp_offset=6, day=day
    )
    assert item is not None
    assert item.sky_condition == "맑음"
    assert item.precipitation_prob == 10
    assert (item.temp_min, item.temp_max) == (21, 31)


def test_mid_takes_worst_precipitation_of_the_day():
    """오전·오후 중 비 올 확률이 높은 쪽을 그 날의 값으로 삼는다."""
    day = _TODAY + timedelta(days=5)
    rows = [_land(5, "AM", "맑음", 10), _land(5, "PM", "비", 70)]
    item = _aggregate_mid(rows, [], 5, None, day)
    assert item is not None
    assert item.precipitation_prob == 70


def test_mid_source_reports_which_side_had_data():
    """실제로 값을 채운 쪽을 출처로 밝힌다."""
    day = _TODAY + timedelta(days=5)
    land_only = _aggregate_mid([_land(5, "AM", "맑음", 10)], [], 5, 5, day)
    temp_only = _aggregate_mid([], [_temp(5, 20, 30)], 5, 5, day)
    both = _aggregate_mid(
        [_land(5, "AM", "맑음", 10)], [_temp(5, 20, 30)], 5, 5, day
    )
    assert land_only is not None and land_only.source == "mid_land"
    assert temp_only is not None and temp_only.source == "mid_temp"
    assert both is not None and both.source == "mid_land+mid_temp"


def test_mid_empty_shell_rows_are_reported_missing():
    """값이 전부 비어 있는 행은 예보가 아니다.

    18 시 발표에는 D+4 항목이 없어, 적재가 빈 행을 만들면 그 날이
    결측이 아니라 "값 없는 예보"로 나간다.
    """
    day = _TODAY + timedelta(days=4)
    empty = [_land(4, "AM", None, None), _land(4, "PM", None, None)]
    assert _aggregate_mid(empty, [], 4, 4, day) is None


def test_mid_without_announcement_yields_nothing():
    """발표분 자체가 없으면(offset None) 채울 값이 없다."""
    day = _TODAY + timedelta(days=5)
    rows = [_land(5, "AM", "맑음", 10)]
    assert _aggregate_mid(rows, [], None, None, day) is None
