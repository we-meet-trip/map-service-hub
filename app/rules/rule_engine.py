"""결정적 룰 엔진 — 순수 함수 모음.

외부 API/DB 를 참조하지 않고 입력만으로 결과가 정해지는 규칙을 담는다.
FastAPI·DB 를 임포트하지 않으므로 단위 테스트로 경계값까지 검증할 수 있다.

제공 함수:
  RADIUS_M          — 이동수단별 최대 반경(m) 테이블(무제한은 None)
  haversine_m       — 두 (위도, 경도) 사이 대권거리(m)
  filter_by_radius  — 출발지 기준 반경 안의 후보만 남기기
  indoor_bonus      — 강수 확률이 높을 때 실내 장소에 가점

호출 관계:
  - app.routers.rules_router 의 3개 엔드포인트가 본 모듈의 함수를 호출.
"""
from __future__ import annotations

import math

# 지구 평균 반지름(m). haversine 계산 상수.
_EARTH_RADIUS_M = 6371000.0

# 이동수단별 최대 반경(m). None 은 반경 무제한(전부 통과)을 뜻한다.
#   foot/walk  — 도보 3km
#   bicycle    — 자전거 10km
#   kickboard  — 킥보드 7km
#   car/transit— 자동차/대중교통은 반경 무제한
RADIUS_M: dict[str, int | None] = {
    "foot": 3000,
    "walk": 3000,
    "bicycle": 10000,
    "kickboard": 7000,
    "car": None,
    "transit": None,
}


def haversine_m(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    """두 (위도, 경도) 좌표 사이의 대권거리를 미터로 계산한다.

    표준 haversine 공식을 사용한다. 입력은 도(degree) 단위 위경도.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def filter_by_radius(
    origin: tuple[float, float],
    mobility: str,
    candidates: list[dict],
) -> tuple[list[dict], int | None]:
    """출발지 기준 이동수단 반경 안의 후보만 남긴다.

    origin: (위도, 경도) 출발지.
    mobility: RADIUS_M 의 키. 반경이 None(car/transit 등 무제한)이면 모든
        후보를 그대로 통과시킨다.
    candidates: 각 후보 dict. lat/lng 키로 좌표를 읽는다. 좌표가 없거나
        숫자로 변환할 수 없는 후보는 방어적으로 제외한다(반경 무제한일
        때는 좌표 검사 없이 전부 통과).

    반환: (남은 후보 리스트, 적용된 반경 m 또는 None).
    """
    radius = RADIUS_M.get(mobility)
    if radius is None:
        # 반경 무제한: 좌표 검사 없이 전부 통과.
        return list(candidates), radius

    lat0, lng0 = origin
    kept: list[dict] = []
    for c in candidates:
        lat = c.get("lat")
        lng = c.get("lng")
        if lat is None or lng is None:
            continue
        try:
            dist = haversine_m(lat0, lng0, float(lat), float(lng))
        except (TypeError, ValueError):
            continue
        if dist <= radius:
            kept.append(c)
    return kept, radius


def indoor_bonus(pois: list[dict], day_pop_max: int) -> list[dict]:
    """강수 확률이 높은 날 실내 장소에 가점을 준다.

    pois: 각 dict 는 content_id / indoor_flag / base_score 를 가진다.
    day_pop_max: 해당 구간의 최대 강수 확률(%). 50 이상이면 실내 가점을
        적용한다.

    각 poi 의 점수는 base_score + (day_pop_max>=50 이고 indoor_flag 이면
    0.15, 아니면 0.0) 이다.

    반환: {content_id, score} dict 리스트(입력 순서 보존).
    """
    apply_bonus = day_pop_max >= 50
    out: list[dict] = []
    for poi in pois:
        base = poi.get("base_score", 0.0)
        is_indoor = bool(poi.get("indoor_flag"))
        bonus = 0.15 if (apply_bonus and is_indoor) else 0.0
        out.append(
            {
                "content_id": poi.get("content_id"),
                "score": base + bonus,
            }
        )
    return out
