"""hub-service public API 응답 스키마.

본 모듈은 hub 서비스가 외부(agent 등)에 노출하는 HTTP 응답 본문의
타입을 정의한다. FastAPI 라우터의 `response_model` 로 지정되어
직렬화 형식과 검증 규칙(예: 범위 제약)을 보장한다.

여기서 정의된 모델은 다음 위치에서 소비된다:
  - WeatherDailyItem / WeatherResponse → hub_routers.get_weather 의
    response_model. agent 의 HubClient.fetch_weather 가 이 형태로 받는다.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class WeatherDailyItem(BaseModel):
    """WeatherDailyItem — 하루치 날씨

    하루 단위 날씨 한 칸을 표현하는 모델.

    date: 해당 날짜 (datetime.date 타입)
    temp_min / temp_max: 최저/최고 기온(℃). 값이 없을 수 있어
        int | None, 기본 None
    precipitation_prob: 강수 확률. ge=0, le=100 제약 → 0~100 범위 밖이면
        검증 실패. 단위는 %
    sky_condition: 하늘 상태 텍스트(예: "맑음"). 없을 수 있음.
        단기예보 출처일 때는 KMA SKY 코드를 label_sky 로 한글 변환한 값,
        중기예보 출처일 때는 원문 weather 텍스트(wf*) 가 그대로 들어간다.
    source: 데이터 출처. Literal["short_term", "mid_land+mid_temp"]
        → 둘 중 하나만 허용
        short_term: 단기예보 (대략 오늘~D+2)
        mid_land+mid_temp: 중기육상예보 + 중기기온예보 합본
            (대략 D+3~D+10)
    """

    date: date
    temp_min: int | None = None
    temp_max: int | None = None
    precipitation_prob: int | None = Field(default=None, ge=0, le=100)
    sky_condition: str | None = None
    source: Literal["short_term", "mid_land+mid_temp"]


class WeatherResponse(BaseModel):
    """WeatherResponse — 날씨 조회 응답 본문

    /v1/weather 엔드포인트가 반환하는 최종 응답 모델.
    요청한 (province, city, date_start..date_end) 구간에 대해
    가능한 날짜는 daily 에, 데이터 부재 날짜는 missing_dates 에 담는다.

    province: 응답 기준 광역시도 명. lookup 결과의 region.lv1 을 그대로 채움
        (요청값과 다를 수 있음 — fallback 매칭이 일어난 경우).
    city: 응답 기준 시군구 명. lookup 결과의 region.lv2 가 비어있으면
        요청한 city 문자열을 그대로 사용.
    daily: 날짜별 WeatherDailyItem 리스트.
        date 오름차순으로 정렬되어 들어간다.
        단기/중기 출처가 섞일 수 있으며 source 필드로 구분 가능.
    missing_dates: 데이터를 만들 수 없었던 날짜 목록.
        - 요청 범위가 단기·중기 예보 horizon(D+0..D+10) 밖이거나
        - DB 에 해당 날짜의 row 가 비어 있거나
        - 중기 reg_id 가 매핑되지 않은 경우
        오름차순 정렬되어 들어간다.
    """

    province: str
    city: str
    daily: list[WeatherDailyItem]
    missing_dates: list[date]
