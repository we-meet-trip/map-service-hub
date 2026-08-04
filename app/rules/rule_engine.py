"""결정적 룰 엔진 — 순수 함수 모음.

외부 API/DB 를 참조하지 않고 입력만으로 결과가 정해지는 규칙을 담는다.
FastAPI·DB 를 임포트하지 않으므로 단위 테스트로 경계값까지 검증할 수 있다.

제공 함수:
  RADIUS_M          — 이동수단별 최대 반경(m) 테이블(무제한은 None)
  DWELL_MINUTES     — 장소 분류별 기본 체류시간(분) 테이블
  haversine_m       — 두 (위도, 경도) 사이 대권거리(m)
  filter_by_radius  — 출발지 기준 반경 안의 후보만 남기기
  indoor_bonus      — 강수 확률이 높을 때 실내 장소에 가점
  estimate_dwell    — 장소 분류로 머무는 시간을 추정

호출 관계:
  - app.routers.rules_router 의 4개 엔드포인트가 본 모듈의 함수를 호출.
"""
from __future__ import annotations

import math

# 지구 평균 반지름(m). haversine 계산 상수.
_EARTH_RADIUS_M = 6371000.0

# 이동수단별 최대 반경(m). None 은 반경 무제한(전부 통과)을 뜻한다.
#   foot/walk        — 도보 3km
#   bicycle          — 자전거 10km
#   kickboard/scooter— 킥보드 10km
#   car/transit      — 자동차/대중교통은 반경 무제한
#
# 킥보드는 자전거와 같은 10km 다. 이동 속도 차이는 반경이 아니라 소요시간
# 보정(BFF 가 실측 duration 에 곱하는 계수)으로 반영한다.
#
# scooter 는 kickboard 의 별칭이다. client 와 hub directions(mode Literal)는
# scooter 를, 상위 설계 문서는 kickboard 를 쓴다. 상류가 어느 철자를 보내든
# 같은 반경이 적용되도록 두 키를 모두 둔다 — 한쪽만 두면 어휘가 어긋나는
# 순간 422 로 거절되어 룰이 조용히 무력화된다.
RADIUS_M: dict[str, int | None] = {
    "foot": 3000,
    "walk": 3000,
    "bicycle": 10000,
    "kickboard": 10000,
    "scooter": 10000,
    "car": None,
    "transit": None,
}


# 장소 분류별 기본 체류시간(분). 키는 카카오 카테고리 그룹 코드다.
#
# 이 값이 필요한 이유는, 일정에 시간축을 세우려면 "이 장소에서 얼마나
# 머무는가"가 있어야 하는데 어떤 외부 출처도 그 값을 주지 않기 때문이다.
# 영업시간조차 받아올 수 없어서 실측으로 대체할 방법이 없다.
#
# 그래서 분류로 근사한다. 같은 분류 안의 편차(대형 박물관과 동네 전시관)는
# 이 표로 구분하지 못하며, 그 한계는 그대로 안고 간다 — 시간축이 아예 없는
# 것보다 대략이라도 있는 편이 낫다는 판단이다.
#
# 값은 조정 가능한 기본값이다. 바꾸면 모든 일정의 방문 시각이 함께 움직인다.
DWELL_MINUTES: dict[str, int] = {
    "FD6": 60,   # 음식점 — 한 끼
    "CE7": 45,   # 카페
    "CT1": 90,   # 문화시설 — 전시·공연은 관람 시간이 길다
    "AT4": 60,   # 관광명소
    "MT1": 60,   # 대형마트
    "AD5": 0,    # 숙박 — 머무는 곳이지 들르는 곳이 아니라 일정 시간에 안 넣는다
    "PO3": 30,   # 공공기관
    "SW8": 5,    # 지하철역 — 지나가는 곳
    "PK6": 5,    # 주차장
    "OL7": 5,    # 주유소
    "CS2": 10,   # 편의점
    "PM9": 10,   # 약국
    "BK9": 15,   # 은행
    "HP8": 30,   # 병원
    "SC4": 30,   # 학교
}

# 분류를 모르는 장소에 쓰는 값. 표의 관광명소와 같은 수준으로 잡는다 —
# 여행 일정에 들어오는 미분류 장소는 대개 볼거리이기 때문이다.
DWELL_DEFAULT_MINUTES = 45

# 체류시간이 벗어나면 안 되는 범위. 상류가 제안값을 보내는 경로가 생겼을 때
# 그 값을 여기로 접는다. 하한은 "들렀다"고 말할 수 있는 최소, 상한은 하루
# 활동 시간을 한 장소가 통째로 먹지 않게 하는 선이다.
DWELL_MIN_MINUTES = 10
DWELL_MAX_MINUTES = 240


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


def estimate_dwell(places: list[dict]) -> list[dict]:
    """장소마다 머무는 시간을 분 단위로 정한다.

    places: 각 dict 는 아래 키를 (있으면) 갖는다.
        content_id            — 장소 식별자. 결과를 되짚는 키.
        category_group_code   — 카카오 카테고리 그룹 코드.
        course_minutes        — 코스 출처가 알려 준 실제 소요시간(분).

    우선순위는 실측 > 분류 > 기본값이다.
      1) course_minutes 가 양수면 그 값을 쓴다. 걷기·자전거 코스는 출처가
         실제 소요시간을 주므로 분류로 추측할 이유가 없다.
      2) 분류가 표에 있으면 그 값을 쓴다.
      3) 둘 다 없으면 기본값.
    어느 경로든 결과는 허용 범위로 접는다. 다만 표가 0 으로 정한 분류
    (숙박처럼 일정 시간에 넣지 않는 곳)는 그대로 0 을 남긴다 — 하한으로
    끌어올리면 잠만 자는 곳이 일정 시간을 먹는다.

    반환: {content_id, stay_minutes, source} 리스트(입력 순서 보존).
        source 는 값의 출처다 — course_actual / category / default.
    """
    out: list[dict] = []
    for place in places:
        course = place.get("course_minutes")
        code = place.get("category_group_code")

        if isinstance(course, (int, float)) and course > 0:
            minutes = int(course)
            source = "course_actual"
        elif code in DWELL_MINUTES:
            minutes = DWELL_MINUTES[code]
            source = "category"
        else:
            minutes = DWELL_DEFAULT_MINUTES
            source = "default"

        if minutes > 0:
            minutes = max(DWELL_MIN_MINUTES, min(DWELL_MAX_MINUTES, minutes))

        out.append(
            {
                "content_id": place.get("content_id"),
                "stay_minutes": minutes,
                "source": source,
            }
        )
    return out
