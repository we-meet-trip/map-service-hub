"""대기오염 정보의 시도 명칭 변환과 미세먼지 등급 판정.

대기오염 API 는 시도를 축약형("서울")으로 받는데, region_grid 가 들고 있는
행정구역 명칭은 정식 명칭("서울특별시")이다. 두 표기를 잇는 매핑과, 농도
수치를 사람이 읽는 등급으로 바꾸는 판정을 함께 둔다.

호출 관계:
  hub_routers.get_weather_now 가 격자 역조회로 얻은 lv1 을 sido_name 으로
  바꿔 대기오염을 조회하고, 받은 농도를 grade_pm10 / grade_pm25 로 등급화해
  응답에 싣는다.
"""
from __future__ import annotations

# 정식 광역시도 명칭 → 대기오염 API 의 sidoName 축약형.
# region_grid 시드가 담고 있는 17개 광역시도를 모두 덮는다.
SIDO_NAME: dict[str, str] = {
    "서울특별시": "서울",
    "부산광역시": "부산",
    "대구광역시": "대구",
    "인천광역시": "인천",
    "광주광역시": "광주",
    "대전광역시": "대전",
    "울산광역시": "울산",
    "세종특별자치시": "세종",
    "경기도": "경기",
    "강원특별자치도": "강원",
    "충청북도": "충북",
    "충청남도": "충남",
    "전북특별자치도": "전북",
    "전라남도": "전남",
    "경상북도": "경북",
    "경상남도": "경남",
    "제주특별자치도": "제주",
}

# 등급 경계 — 각 구간의 상한과 라벨. 상한이 None 이면 그 위 전부.
_PM10_BANDS: tuple[tuple[int | None, str], ...] = (
    (30, "좋음"),
    (80, "보통"),
    (150, "나쁨"),
    (None, "매우나쁨"),
)
_PM25_BANDS: tuple[tuple[int | None, str], ...] = (
    (15, "좋음"),
    (35, "보통"),
    (75, "나쁨"),
    (None, "매우나쁨"),
)


def sido_name(province: str | None) -> str | None:
    """정식 광역시도 명칭을 대기오염 API 의 시도 축약형으로 바꾼다.

    매핑에 없는 값(정식 명칭이 아니거나 명칭 개편으로 어긋난 경우)은 None 을
    돌려주고, 호출 측이 대기오염 조회를 건너뛴다.
    """
    if not province:
        return None
    return SIDO_NAME.get(province)


def _grade(
    value: int | None, bands: tuple[tuple[int | None, str], ...]
) -> str | None:
    """농도를 구간 표에 대입해 등급 라벨을 고른다. 값이 없으면 None."""
    if value is None or value < 0:
        return None
    for upper, label in bands:
        if upper is None or value <= upper:
            return label
    return None


def grade_pm10(value: int | None) -> str | None:
    """미세먼지 농도(㎍/㎥)를 등급 라벨로 바꾼다."""
    return _grade(value, _PM10_BANDS)


def grade_pm25(value: int | None) -> str | None:
    """초미세먼지 농도(㎍/㎥)를 등급 라벨로 바꾼다."""
    return _grade(value, _PM25_BANDS)
