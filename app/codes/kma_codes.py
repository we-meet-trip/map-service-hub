"""KMA 카테고리 코드 -> 한글 라벨 매핑.

KMA(기상청) 단기예보의 일부 카테고리는 정수 코드 형태로 값이 내려온다.
본 모듈은 그 코드를 사람이 읽을 수 있는 한글 텍스트로 변환하는
조회 테이블과 헬퍼 함수를 제공한다.

도메인 용어:
  SKY: 하늘 상태 카테고리 코드
  PTY: 강수 형태(precipitation type) 카테고리 코드

호출 관계:
  label_sky 는 hub_routers._aggregate_short_term 에서 호출되어
  단기예보 SKY 코드를 sky_condition 텍스트로 변환한다.
"""
from __future__ import annotations


# SKY(하늘 상태) 코드 → 한글 라벨.
# 키는 문자열로 보관하므로(아래 label_sky 가 str(value) 로 정규화) 입력이
# int 든 str 이든 동일하게 조회된다.
# 2 번 코드(구름조금)는 KMA 운영상 사용되지 않아 매핑에 포함하지 않는다.
SKY_LABEL: dict[str, str] = {
    "1": "맑음",
    "3": "구름많음",
    "4": "흐림",
}

# PTY(강수 형태) 코드 → 한글 라벨.
# 0 은 강수 없음을 의미하며, 1~7 은 비/눈/소나기 등 세부 형태를 구분한다.


def label_sky(value: str | int | None) -> str | None:
    """label_sky — SKY 코드를 한글 라벨로 변환

    하늘 상태(SKY) 코드를 사람이 읽는 텍스트로 바꿔 돌려준다.

    value: 입력 코드. str | int | None 을 모두 받는다.
        - None 또는 빈 문자열("") 이면 변환 없이 None 반환
        - 그 외에는 str(value) 로 정규화한 뒤 SKY_LABEL 에서 조회
    반환: 매핑이 존재하면 한글 라벨, 매핑이 없으면 None
        (예: 알 수 없는 코드 "2" 가 들어오면 None)

    호출처: hub_routers._aggregate_short_term — 단기예보 SKY 카테고리
        row 의 fcst_value 를 본 함수로 변환해 sky_condition 에 채운다.
    """
    if value is None or value == "":
        return None
    return SKY_LABEL.get(str(value))
