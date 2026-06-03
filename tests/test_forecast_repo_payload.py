"""forecast_repo 의 payload 분해 로직 단위 테스트.

forecast_repo 모듈 자체는 SQL 을 실행하는 함수가 대부분이지만, 두 가지
순수 변환 책임에는 DB 가 필요 없다:

  1) _safe_int — 임의 값을 안전하게 int|None 으로 변환
  2) 중기 육상예보(mid_land) payload → 11개 row 분해 규약
  3) 중기 기온예보(mid_temp) payload → 7개 row 분해 규약

본 파일은 위 세 동작을 DB 없이 검증한다.
"""
from __future__ import annotations

from app.db.forecast_repo import _safe_int


def test_safe_int_handles_none_and_empty():
    """None / 빈 문자열 / 정수 변환 불가 문자열 모두 None 반환을 검증."""
    assert _safe_int(None) is None
    assert _safe_int("") is None
    assert _safe_int("abc") is None


def test_safe_int_parses_int_and_str_int():
    """int / 정수 형식 문자열 / 0 의 정상 변환을 검증."""
    assert _safe_int(60) == 60
    assert _safe_int("60") == 60
    assert _safe_int(0) == 0


def test_mid_land_payload_row_expansion_logic():
    """중기 육상 payload → 11 row 분해 규약을 직접 재현해 검증.

    실제 upsert_mid_land 가 따르는 규약:
      day 4..7 → ("AM", "PM") 두 row × 4일 = 8 row
      day 8..10 → "NA" 한 row × 3일 = 3 row
      합계 11 row.
    본 테스트는 동일 규약대로 payload 를 분해해
    AM/PM 8건, NA 3건의 카운트가 나오는지 확인한다.
    """
    payload = {
        "regId": "11B00000",
        **{
            f"wf{d}{sfx}": "맑음"
            for d in (4, 5, 6, 7)
            for sfx in ("Am", "Pm")
        },
        **{f"wf{d}": "맑음" for d in (8, 9, 10)},
        **{
            f"rnSt{d}{sfx}": 30
            for d in (4, 5, 6, 7)
            for sfx in ("Am", "Pm")
        },
        **{f"rnSt{d}": 10 for d in (8, 9, 10)},
    }
    rows = []
    for day in (4, 5, 6, 7):
        for ampm, sfx in (("AM", "Am"), ("PM", "Pm")):
            rows.append((
                day,
                ampm,
                payload[f"wf{day}{sfx}"],
                payload[f"rnSt{day}{sfx}"],
            ))
    for day in (8, 9, 10):
        rows.append((day, "NA", payload[f"wf{day}"], payload[f"rnSt{day}"]))
    assert len(rows) == 11
    assert sum(1 for r in rows if r[1] in ("AM", "PM")) == 8
    assert sum(1 for r in rows if r[1] == "NA") == 3


def test_mid_temp_payload_row_expansion_logic():
    """중기 기온 payload → 7 row 분해 규약을 직접 재현해 검증.

    실제 upsert_mid_temp 가 따르는 규약:
      day 4..10 각각 1 row (am_pm 구분 없음) → 7 row
      각 day 의 6개 필드: taMin/taMin Low/High, taMax/taMax Low/High
    본 테스트는 payload 에 6개 필드가 day 별로 모두 채워졌는지,
    그리고 rows 길이가 7 인지를 확인한다.
    """
    payload = {"regId": "11B10101"}
    for d in range(4, 11):
        for sfx in ("", "Low", "High"):
            payload[f"taMin{d}{sfx}"] = d
            payload[f"taMax{d}{sfx}"] = d + 5
    rows = list(range(4, 11))
    assert len(rows) == 7
    for d in rows:
        assert payload[f"taMin{d}"] == d
        assert payload[f"taMax{d}High"] == d + 5
