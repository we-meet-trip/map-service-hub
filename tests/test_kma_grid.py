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
    assert gps_to_grid(lat, lon) == (ex_nx, ex_ny)


def test_parse_kma_base_at_kst_aware():
    dt = parse_kma_base_at("20260522", "0500")
    assert dt == datetime(2026, 5, 22, 5, 0, tzinfo=KST)


def test_parse_kma_fcst_at_kst_aware():
    dt = parse_kma_fcst_at("20260523", "1500")
    assert dt == datetime(2026, 5, 23, 15, 0, tzinfo=KST)


def test_parse_kma_tm_fc_kst_aware():
    dt = parse_kma_tm_fc("202605220600")
    assert dt == datetime(2026, 5, 22, 6, 0, tzinfo=KST)


def test_parse_kma_base_at_invalid_raises():
    with pytest.raises(ValueError):
        parse_kma_base_at("2026052", "0500")
    with pytest.raises(ValueError):
        parse_kma_base_at("20260522", "500")


def test_parse_kma_tm_fc_invalid_raises():
    with pytest.raises(ValueError):
        parse_kma_tm_fc("20260522060")
