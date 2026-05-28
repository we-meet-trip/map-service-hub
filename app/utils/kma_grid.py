"""KMA 격자 좌표 변환 + 발표시각 파서."""
from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_RE = 6371.00877
_GRID = 5.0
_SLAT1 = 30.0
_SLAT2 = 60.0
_OLON = 126.0
_OLAT = 38.0
_XO = 43
_YO = 136

_PI = math.asin(1.0) * 2.0
_DEGRAD = _PI / 180.0

_re = _RE / _GRID
_slat1 = _SLAT1 * _DEGRAD
_slat2 = _SLAT2 * _DEGRAD
_olon = _OLON * _DEGRAD
_olat = _OLAT * _DEGRAD

_sn_t = (
    math.tan(_PI * 0.25 + _slat2 * 0.5)
    / math.tan(_PI * 0.25 + _slat1 * 0.5)
)
_sn = math.log(math.cos(_slat1) / math.cos(_slat2)) / math.log(_sn_t)
_sf_t = math.tan(_PI * 0.25 + _slat1 * 0.5)
_sf = math.pow(_sf_t, _sn) * math.cos(_slat1) / _sn
_ro_t = math.tan(_PI * 0.25 + _olat * 0.5)
_ro = _re * _sf / math.pow(_ro_t, _sn)


def gps_to_grid(lat: float, lon: float) -> tuple[int, int]:
    ra = math.tan(_PI * 0.25 + lat * _DEGRAD * 0.5)
    ra = _re * _sf / math.pow(ra, _sn)
    theta = lon * _DEGRAD - _olon
    if theta > _PI:
        theta -= 2.0 * _PI
    if theta < -_PI:
        theta += 2.0 * _PI
    theta *= _sn
    nx = int(ra * math.sin(theta) + _XO + 0.5)
    ny = int(_ro - ra * math.cos(theta) + _YO + 0.5)
    return nx, ny


def parse_kma_base_at(base_date: str, base_time: str) -> datetime:
    if len(base_date) != 8 or len(base_time) != 4:
        raise ValueError(
            f"invalid kma base date/time: {base_date} {base_time}"
        )
    return datetime(
        int(base_date[0:4]),
        int(base_date[4:6]),
        int(base_date[6:8]),
        int(base_time[0:2]),
        int(base_time[2:4]),
        tzinfo=KST,
    )


def parse_kma_tm_fc(tm_fc: str) -> datetime:
    if len(tm_fc) != 12:
        raise ValueError(f"invalid kma tmFc: {tm_fc}")
    return datetime(
        int(tm_fc[0:4]),
        int(tm_fc[4:6]),
        int(tm_fc[6:8]),
        int(tm_fc[8:10]),
        int(tm_fc[10:12]),
        tzinfo=KST,
    )


def parse_kma_fcst_at(fcst_date: str, fcst_time: str) -> datetime:
    return parse_kma_base_at(fcst_date, fcst_time)
