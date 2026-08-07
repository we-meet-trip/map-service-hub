"""KMA 격자 좌표 변환 + 발표시각 파서.

본 모듈은 외부 라이브러리 의존성 없이 다음을 제공한다:

  - KST: Asia/Seoul 타임존 객체. 다른 모듈이 시간 비교/생성 시 재사용.
  - gps_to_grid(lat, lon): WGS84 위경도 → KMA 격자 (nx, ny) 변환.
        KMA 가 채택한 Lambert Conformal Conic 투영을 그대로 구현.
  - parse_kma_base_at("YYYYMMDD", "HHMM") — 단기예보 발표 시각 파싱
  - parse_kma_fcst_at("YYYYMMDD", "HHMM") — 단기예보 예보 대상 시각 파싱
        (실제 계산 로직은 base_at 과 동일)
  - parse_kma_tm_fc("YYYYMMDDHHMM")       — 중기예보 발표 시각 파싱

도메인 용어:
  격자(nx, ny): KMA 가 한반도를 5km × 5km 셀로 분할한 정수 좌표.
                단기예보 API 의 호출 키.
  base_at:      단기예보가 발표된 시각.
  fcst_at:      단기예보가 가리키는 예보 대상 시각.
  tm_fc:        중기예보가 발표된 시각.

호출 관계:
  - app.scheduler.hub_scheduler — KST, parse_kma_base_at, parse_kma_tm_fc
  - app.db.forecast_repo        — parse_kma_fcst_at
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# KST — Asia/Seoul IANA 타임존. tzdata 패키지가 이 정의를 제공한다.
KST = ZoneInfo("Asia/Seoul")

# === KMA Lambert Conformal Conic 투영 파라미터 ===
# 다음 상수들은 KMA 가 공식 문서에서 명시한 격자 정의값을 그대로 옮긴 것이며,
# 임의로 변경하면 격자 변환 결과가 달라진다.
_RE = 6371.00877       # 지구 반경 (km)
_GRID = 5.0            # 격자 간격 (km)
_SLAT1 = 30.0          # 표준 위도 1 (degree)
_SLAT2 = 60.0          # 표준 위도 2 (degree)
_OLON = 126.0          # 기준점 경도 (degree)
_OLAT = 38.0           # 기준점 위도 (degree)
_XO = 43               # 기준점의 격자 X 좌표
_YO = 136              # 기준점의 격자 Y 좌표

# 보조 상수: 라디안 변환 계수와 사전 계산값.
# 모듈 로드 시점에 한 번만 계산해 gps_to_grid 호출마다 재계산을 피한다.
_PI = math.asin(1.0) * 2.0
_DEGRAD = _PI / 180.0

_re = _RE / _GRID
_slat1 = _SLAT1 * _DEGRAD
_slat2 = _SLAT2 * _DEGRAD
_olon = _OLON * _DEGRAD
_olat = _OLAT * _DEGRAD

# Lambert 투영의 표준 위도 두 개로부터 수렴 인자 _sn, 스케일 _sf, 기준점
# 반지름 _ro 를 도출. 본 값들이 gps_to_grid 의 핵심 계수다.
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
    """gps_to_grid — WGS84 위경도 → KMA 격자 (nx, ny)

    lat: 위도 (degree). 한국 영역 외 입력의 동작은 KMA 명세 외 영역이라 의미 없음.
    lon: 경도 (degree).

    동작:
      Lambert Conformal Conic 역투영 공식을 적용해 격자 셀의 좌표를 얻고,
      0.5 를 더한 뒤 int() 로 반올림한다(가장 가까운 셀로 사상).

    반환: (nx, ny) — KMA API 호출에 사용되는 정수 격자 좌표.
    검증: tests/test_kma_grid.py 가 KMA 가 배포한 xlsx 의 18개 대표 좌표를
        통과하는지 확인한다.
    호출처: 현재 외부 호출자는 없으나, 테스트가 본 함수의 정합성을 보증한다.
    """
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


def resolve_nowcast_base(now: datetime) -> tuple[str, str]:
    """resolve_nowcast_base — 조회 가능한 초단기실황 발표분 계산

    now: KST timezone-aware 현재 시각.

    초단기실황은 매시 정시 관측분이 40분경 공개된다. 45분 안전 마진을 두고
    45분을 넘겼으면 이번 시각(HH00), 아직이면 한 시간 전을 고른다. 마진 없이
    이번 시각을 그대로 요청하면 아직 없는 자료를 물어 실패한다.

    반환: (base_date, base_time) — "YYYYMMDD", "HHMM" 형식 문자열.
        KMAClient.fetch_nowcast 의 base_date/base_time 으로 그대로 쓴다.
    """
    base = now if now.minute >= 45 else now - timedelta(hours=1)
    base = base.replace(minute=0, second=0, microsecond=0)
    return base.strftime("%Y%m%d"), base.strftime("%H00")


def parse_kma_base_at(base_date: str, base_time: str) -> datetime:
    """parse_kma_base_at — KMA 발표 시각 문자열을 KST datetime 으로

    base_date: "YYYYMMDD" 8자리.
    base_time: "HHMM"     4자리.
        길이가 정확히 8/4 가 아니면 ValueError.

    반환: tzinfo=KST 인 timezone-aware datetime.
    호출처: hub_scheduler 의 폴링 루프 — DB 저장 키 base_at 으로 사용.
    """
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
    """parse_kma_tm_fc — KMA 중기예보 발표 시각 문자열을 KST datetime 으로

    tm_fc: "YYYYMMDDHHMM" 12자리. 길이가 정확히 12 가 아니면 ValueError.

    반환: tzinfo=KST 인 timezone-aware datetime.
    호출처: hub_scheduler.mid_term_polling_loop —
        DB 저장 키 tm_fc 로 사용.
    """
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
    """parse_kma_fcst_at — KMA 예보 대상 시각 문자열을 KST datetime 으로

    KMA 단기예보 item 의 fcstDate / fcstTime 을 합쳐 timezone-aware
    datetime 으로 변환한다. 입력 형식과 계산 로직이 parse_kma_base_at 과
    동일하므로 내부적으로 그 함수에 위임한다.

    호출처: app.db.forecast_repo.upsert_short_term_items —
        DB 저장 키 fcst_at 으로 사용.
    """
    return parse_kma_base_at(fcst_date, fcst_time)
