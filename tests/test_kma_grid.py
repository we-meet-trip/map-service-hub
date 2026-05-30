"""kma_grid 모듈의 좌표 변환과 시각 파서 단위 테스트.

검증 대상:
  - gps_to_grid: KMA 가 배포한 xlsx 의 광역시도 대표 18건과 일치하는가
  - parse_kma_base_at / parse_kma_fcst_at / parse_kma_tm_fc:
      KST timezone-aware datetime 을 정확히 생성하는가
  - 입력 길이가 명세와 다르면 ValueError 를 던지는가
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.utils.kma_grid import (
    KST,
    gps_to_grid,
    parse_kma_base_at,
    parse_kma_fcst_at,
    parse_kma_tm_fc,
)

# KMA 가 배포한 xlsx 에서 발췌한 광역 대표 좌표 ↔ 격자 매핑 18건.
# (label, lat, lon, expected_nx, expected_ny)
_XLSX_GRIDS = [
    ("서울", 37.5635694, 126.9800083, 60, 127),
    ("부산", 35.17702, 129.07695, 98, 76),
    ("대구", 35.86854, 128.60355, 89, 90),
    ("인천", 37.45323, 126.70735, 55, 124),
    ("광주", 35.15697, 126.85336, 58, 74),
    ("대전", 36.34711, 127.38657, 67, 100),
    ("울산", 35.53541, 129.31369, 102, 84),
    ("세종", 36.48001, 127.28907, 66, 103),
    ("경기", 37.27184, 127.01169, 60, 120),
    ("강원광역", 37.88269, 127.73198, 73, 134),
    ("강릉", 37.74914, 128.87850, 92, 131),
    ("충북", 36.6325, 127.49359, 69, 107),
    ("충남", 36.65881, 126.67280, 55, 107),
    ("전북", 35.81728, 127.11105, 63, 89),
    ("전남", 34.81304, 126.465, 51, 67),
    ("경북", 36.57600, 128.50583, 87, 106),
    ("경남", 35.23474, 128.69417, 91, 77),
    ("제주", 33.48569, 126.50033, 52, 38),
]


@pytest.mark.parametrize("label,lat,lon,ex_nx,ex_ny", _XLSX_GRIDS)
def test_gps_to_grid_matches_kma_xlsx(label, lat, lon, ex_nx, ex_ny):
    """광역 18건 각각에 대해 gps_to_grid 결과가 xlsx 기댓값과 동일한지 검증."""
    assert gps_to_grid(lat, lon) == (ex_nx, ex_ny)


def test_parse_kma_base_at_kst_aware():
    """단기예보 발표 시각 문자열이 KST tz 가 붙은 datetime 으로 정확히 변환되는지 검증."""
    dt = parse_kma_base_at("20260522", "0500")
    assert dt == datetime(2026, 5, 22, 5, 0, tzinfo=KST)


def test_parse_kma_fcst_at_kst_aware():
    """단기예보 대상 시각 문자열 변환의 정합성 검증 (base_at 과 동일 로직 위임)."""
    dt = parse_kma_fcst_at("20260523", "1500")
    assert dt == datetime(2026, 5, 23, 15, 0, tzinfo=KST)


def test_parse_kma_tm_fc_kst_aware():
    """중기예보 12자리 발표 시각 문자열 변환 정합성 검증."""
    dt = parse_kma_tm_fc("202605220600")
    assert dt == datetime(2026, 5, 22, 6, 0, tzinfo=KST)


def test_parse_kma_base_at_invalid_raises():
    """date/time 길이가 8/4 가 아니면 ValueError 가 발생하는지 검증."""
    with pytest.raises(ValueError):
        parse_kma_base_at("2026052", "0500")
    with pytest.raises(ValueError):
        parse_kma_base_at("20260522", "500")


def test_parse_kma_tm_fc_invalid_raises():
    """tm_fc 길이가 12 가 아니면 ValueError 가 발생하는지 검증."""
    with pytest.raises(ValueError):
        parse_kma_tm_fc("20260522060")
