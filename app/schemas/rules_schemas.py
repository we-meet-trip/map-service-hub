"""hub-service 룰 엔진 API 요청/응답 스키마.

/v1/rules/* 엔드포인트(rules_router)의 요청 본문과 응답 본문 타입을
정의한다. FastAPI 가 본 모델로 요청을 검증(범위/길이 제약)하고 응답을
직렬화한다. SoT §7.3 의 본문 구조를 따른다.

여기서 정의된 모델은 다음 위치에서 소비된다:
  - MobilityRadiusRequest / MobilityRadiusResponse →
    rules_router.filter_mobility_radius
  - IndoorBonusRequest / IndoorBonusResponse → rules_router.score_indoor_bonus
  - ForbiddenZonesRequest / ForbiddenZonesResponse →
    rules_router.filter_forbidden_zones
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LatLng(BaseModel):
    """LatLng — 위경도 한 쌍.

    lat: 위도. lng: 경도.
    """

    lat: float
    lng: float


class MobilityRadiusRequest(BaseModel):
    """MobilityRadiusRequest — 이동수단 반경 필터 요청.

    origin: 반경 기준 출발지 좌표.
    mobility: 이동수단 문자열(foot/walk/bicycle/kickboard/car/transit).
    candidates: 필터 대상 후보 dict 리스트(각 후보는 lat/lng 를 가진다).
        최대 100건.
    """

    origin: LatLng
    mobility: str = Field(min_length=1, max_length=12)
    candidates: list[dict] = Field(max_length=100)


class MobilityRadiusResponse(BaseModel):
    """MobilityRadiusResponse — 이동수단 반경 필터 응답.

    filtered: 반경 안에 남은 후보 리스트.
    radius_m: 적용된 반경(m). 무제한(car/transit)이면 None.
    dropped: 반경 밖으로 제외된(또는 좌표 누락으로 빠진) 건수.
    """

    filtered: list[dict]
    radius_m: int | None
    dropped: int


class IndoorBonusPoi(BaseModel):
    """IndoorBonusPoi — 실내 가점 대상 장소 1건.

    content_id: 장소 식별자.
    indoor_flag: 실내 여부.
    base_score: 가점 전 기본 점수(0~10).
    """

    content_id: str
    indoor_flag: bool
    base_score: float = Field(ge=0, le=10)


class IndoorBonusRequest(BaseModel):
    """IndoorBonusRequest — 실내 우선 가점 요청.

    pois: 가점 대상 장소 리스트(최대 100건).
    day_pop_max: 해당 구간 최대 강수 확률(%). 50 이상이면 실내 가점 적용.
    """

    pois: list[IndoorBonusPoi] = Field(max_length=100)
    day_pop_max: int = Field(ge=0, le=100)


class ScoredPoi(BaseModel):
    """ScoredPoi — 가점 적용 후 장소 점수.

    content_id: 장소 식별자.
    score: 가점이 반영된 최종 점수.
    """

    content_id: str
    score: float


class IndoorBonusResponse(BaseModel):
    """IndoorBonusResponse — 실내 우선 가점 응답.

    scored: 각 장소의 최종 점수 리스트(입력 순서 보존).
    """

    scored: list[ScoredPoi]


class ForbiddenZonesRequest(BaseModel):
    """ForbiddenZonesRequest — 금지구역 교차 판정 요청.

    polyline: 경로 폴리라인(2~500 점). 각 점은 LatLng.
    """

    polyline: list[LatLng] = Field(min_length=2, max_length=500)


class ForbiddenZonesResponse(BaseModel):
    """ForbiddenZonesResponse — 금지구역 교차 판정 응답.

    intersects: 하나라도 교차하면 True.
    zones: 교차하는 금지구역 이름 리스트.
    suggested_detour: 우회 경로 제안(PoC 에서는 항상 None).
    """

    intersects: bool
    zones: list[str]
    suggested_detour: list[LatLng] | None = None
