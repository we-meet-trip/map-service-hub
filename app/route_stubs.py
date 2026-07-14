"""OSRM 미기동/미설정 단계에서 쓰는 경로 라우팅 스텁.

프로파일별 base URL 이 비어 있으면(또는 강제 스텁 모드) 실제 OSRM 호출
대신 본 모듈의 결정적 지오메트리를 사용해 배선/렌더 로직을 인프라 없이
끝까지 검증할 수 있게 한다. base URL 이 채워지면 호출부가 실 클라이언트로
전환한다.

스텁은 처음부터 [lat, lng] 로 생성한다(실 어댑터의 좌표 스왑 경로를
통과하지 않는다 — 스왑은 어댑터 `_normalize_route` 단일 지점 원칙 유지).
"""
from __future__ import annotations

import math

from app.config import settings


def routing_stub_active(base_url: str) -> bool:
    """해당 프로파일을 스텁으로 다뤄야 하는지 판단한다.

    설정에서 스텁 모드가 켜져 있거나 base URL 이 비어 있으면 True.
    """
    return settings.PLACES_STUB_MODE or not base_url


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 간 대권 거리(m)."""
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    )
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def osrm_route_stub(
    start_lat: float,
    start_lng: float,
    goal_lat: float,
    goal_lng: float,
) -> dict:
    """결정적 스텁 경로를 돌려준다(입력만의 함수).

    직선의 1/3·2/3 지점을 진행 방향의 수직으로 서로 반대로 밀어 지그재그
    4점 경로를 만든다 → 스텁 E2E 에서 직선과 육안 구분이 된다. 거리는
    대권거리에 도로 우회 계수(1.25)를, 시간은 보행 속도(1.2 m/s)를 적용.

    반환: {"path": [[lat,lng], ...], "distance_m": int, "duration_s": int}
    """
    dlat = goal_lat - start_lat
    dlng = goal_lng - start_lng
    seg_len_deg = math.hypot(dlat, dlng)
    # 수직 오프셋 크기(도). 짧은 leg 는 길이 비례, 상한 0.0015°(약 165m).
    off = min(0.0015, seg_len_deg * 0.15)
    if seg_len_deg == 0.0:
        # 동일 좌표 leg — 굴곡 없이 두 점만.
        path = [[start_lat, start_lng], [goal_lat, goal_lng]]
    else:
        # 진행 방향(dlat,dlng)에 수직인 단위벡터 (-dlng, dlat)/len.
        perp_lat = -dlng / seg_len_deg
        perp_lng = dlat / seg_len_deg
        p1 = [
            start_lat + dlat / 3 + perp_lat * off,
            start_lng + dlng / 3 + perp_lng * off,
        ]
        p2 = [
            start_lat + dlat * 2 / 3 - perp_lat * off,
            start_lng + dlng * 2 / 3 - perp_lng * off,
        ]
        path = [[start_lat, start_lng], p1, p2, [goal_lat, goal_lng]]

    straight_m = _haversine_m(start_lat, start_lng, goal_lat, goal_lng)
    distance_m = int(round(straight_m * 1.25))
    duration_s = int(round(distance_m / 1.2)) if distance_m > 0 else 0
    return {"path": path, "distance_m": distance_m, "duration_s": duration_s}
