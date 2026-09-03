"""hub-service 룰 엔진 라우터.

결정적 룰 3종에 대한 REST 엔드포인트를 본 모듈에 정의한다. 좌표/점수
계산은 app.rules.rule_engine 의 순수 함수가, 금지구역 교차는
app.db.rules_repo 가 담당하고, 본 모듈은 요청 검증과 응답 조립만 한다.

제공 엔드포인트:
  POST /v1/rules/filter/mobility-radius — 이동수단 반경 필터
  POST /v1/rules/score/indoor-bonus     — 강수 시 실내 우선 가점
  POST /v1/rules/filter/forbidden-zones — 금지구역 교차 판정
  POST /v1/rules/estimate/dwell         — 장소 분류 기반 체류시간 추정

호출 관계:
  - app.main 이 본 모듈의 router 를 include
  - agent 의 룰 적용 단계가 호출하는 것을 상정한 public API
"""
from __future__ import annotations

import logging

from fastapi import Request, APIRouter, Depends, HTTPException, status

from app.db.rules_repo import zones_intersecting_polyline
from app.routers.guards import public_guard
from app.routers.location_params import resolve_origin_and_candidates
from app.rules.rule_engine import (
    RADIUS_M,
    estimate_dwell,
    filter_by_radius,
    indoor_bonus,
)
from app.schemas.rules_schemas import (
    DwellEstimate,
    DwellEstimateRequest,
    DwellEstimateResponse,
    ForbiddenZonesRequest,
    ForbiddenZonesResponse,
    IndoorBonusRequest,
    IndoorBonusResponse,
    MobilityRadiusRequest,
    MobilityRadiusResponse,
    ScoredPoi,
)

logger = logging.getLogger(__name__)

# 본 모듈의 룰 라우트를 묶는 APIRouter. main 앱에서 include_router 로 등록.
# public_guard 는 AUTH_ENFORCED=false 면 no-op, true 면 /v1/rules/* 전체에
# X-Internal-Token 을 요구한다.
router = APIRouter(dependencies=[Depends(public_guard)])


@router.post(
    "/v1/rules/filter/mobility-radius",
    response_model=MobilityRadiusResponse,
)
async def filter_mobility_radius(
    request: Request,
    body: MobilityRadiusRequest,
) -> MobilityRadiusResponse:
    """POST /v1/rules/filter/mobility-radius — 이동수단 반경 필터.

    출발지 기준으로 이동수단 반경 안의 후보만 남긴다. 반경이 무제한인
    이동수단(car/transit)은 후보를 그대로 통과시킨다.

    mobility 가 알려진 이동수단(RADIUS_M 키)이 아니면 422 로 거절한다.
    """
    if body.mobility not in RADIUS_M:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown mobility: {body.mobility}",
        )
    # 출발지와 후보는 감싸서 온다. 여는 데 실패하면 여기서 요청이 끝난다 —
    # 평문으로 물러서면 감싸는 쪽이 고장 나도 아무도 알아차리지 못한다.
    opened_origin, candidates = resolve_origin_and_candidates(
        request, body.loc, body.origin, body.candidates
    )
    origin = (opened_origin["lat"], opened_origin["lng"])
    filtered, radius_m = filter_by_radius(
        origin, body.mobility, candidates
    )
    return MobilityRadiusResponse(
        filtered=filtered,
        radius_m=radius_m,
        # 봉투를 열기 전 목록이 아니라 연 뒤의 목록으로 센다. 감싸서 오면
        # 열기 전 목록은 비어 있어, 그것으로 세면 제외 건수가 음수가 된다.
        dropped=len(candidates) - len(filtered),
    )


@router.post(
    "/v1/rules/score/indoor-bonus",
    response_model=IndoorBonusResponse,
)
async def score_indoor_bonus(
    body: IndoorBonusRequest,
) -> IndoorBonusResponse:
    """POST /v1/rules/score/indoor-bonus — 강수 시 실내 우선 가점.

    day_pop_max 가 50 이상이면 실내 장소(indoor_flag=true)에 0.15 를 더한다.
    """
    pois = [p.model_dump() for p in body.pois]
    scored = indoor_bonus(pois, body.day_pop_max)
    return IndoorBonusResponse(
        scored=[ScoredPoi(**s) for s in scored]
    )


@router.post(
    "/v1/rules/filter/forbidden-zones",
    response_model=ForbiddenZonesResponse,
)
async def filter_forbidden_zones(
    body: ForbiddenZonesRequest,
) -> ForbiddenZonesResponse:
    """POST /v1/rules/filter/forbidden-zones — 금지구역 교차 판정.

    폴리라인이 금지구역과 교차하는지 PostGIS 로 판정한다. 금지구역
    백엔드(DB) 장애 시 503 을 반환한다. 우회 경로 제안은 PoC 범위에서
    항상 None 이다.
    """
    points = [(p.lat, p.lng) for p in body.polyline]
    try:
        zones = await zones_intersecting_polyline(points)
    except Exception as e:
        logger.warning("forbidden-zones query failed err=%s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="rules backend unavailable",
        ) from e
    return ForbiddenZonesResponse(
        intersects=bool(zones),
        zones=zones,
        suggested_detour=None,
    )


@router.post(
    "/v1/rules/estimate/dwell",
    response_model=DwellEstimateResponse,
)
async def estimate_dwell_minutes(
    body: DwellEstimateRequest,
) -> DwellEstimateResponse:
    """POST /v1/rules/estimate/dwell — 장소마다 머무는 시간을 정한다.

    일정에 시간축을 세우려면 이동시간만으로는 부족하고 "이 장소에서 얼마나
    머무는가"가 있어야 한다. 그런데 그 값을 주는 외부 출처가 없어서
    분류로 근사한다. 코스 출처가 실제 소요시간을 알려 준 장소는 그 값을 쓴다.

    외부 호출도 DB 조회도 없는 순수 계산이라 같은 입력에 항상 같은 결과다.
    """
    places = [p.model_dump() for p in body.places]
    estimates = estimate_dwell(places)
    return DwellEstimateResponse(
        estimates=[DwellEstimate(**e) for e in estimates]
    )
