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


def transit_stub_active(api_key: str) -> bool:
    """지하철 경로를 스텁으로 다뤄야 하는지 판단한다.

    설정에서 스텁 모드가 켜져 있거나 인증키가 비어 있으면 True.
    """
    return settings.PLACES_STUB_MODE or not api_key


def subway_route_stub(
    start_lat: float,
    start_lng: float,
    goal_lat: float,
    goal_lng: float,
) -> dict:
    """결정적 스텁 지하철 경로를 돌려준다(입력만의 함수).

    화면이 채워지는지 보기 위한 값이라 실제 노선과는 관계가 없다. 걷기 →
    지하철 → 갈아타서 지하철 → 걷기로 네 구간을 두어, 환승과 도보가 함께
    있는 경우의 배치를 확인할 수 있게 한다.

    소요시간은 두 좌표의 대권거리를 지하철 표정속도(약 32 km/h)로 나눈 값에
    승하차 여유를 더해 만든다. 좌표가 멀어지면 값도 함께 늘어나므로 스텁
    상태에서도 화면이 그럴듯하게 보인다.
    """
    straight_m = _haversine_m(start_lat, start_lng, goal_lat, goal_lng)
    ride_min = max(2, int(round(straight_m / 1000.0 / 32.0 * 60.0)))
    walk_min = 4
    total_min = ride_min + walk_min * 2
    # 앞 구간을 조금 길게 나눠 환승 지점이 가운데보다 앞에 오게 한다.
    first_ride = max(1, ride_min * 3 // 5)
    second_ride = max(1, ride_min - first_ride)
    return {
        "total_time_min": total_min,
        "fare": 1400,
        "transfer_count": 1,
        "total_walk_m": 520,
        "steps": [
            {
                "type": "walk",
                "line_name": None,
                "start_name": "출발지",
                "end_name": "출발역",
                "section_time_min": walk_min,
                "station_count": None,
            },
            {
                "type": "subway",
                "line_name": "스텁 1호선",
                "start_name": "출발역",
                "end_name": "환승역",
                "section_time_min": first_ride,
                "station_count": max(1, first_ride // 2),
            },
            {
                "type": "subway",
                "line_name": "스텁 2호선",
                "start_name": "환승역",
                "end_name": "도착역",
                "section_time_min": second_ride,
                "station_count": max(1, second_ride // 2),
            },
            {
                "type": "walk",
                "line_name": None,
                "start_name": "도착역",
                "end_name": "도착지",
                "section_time_min": walk_min,
                "station_count": None,
            },
        ],
    }


def _stub_lerp(
    start_lat: float, start_lng: float, goal_lat: float, goal_lng: float, f: float
) -> list[float]:
    """출발-도착 직선을 f(0~1) 지점에서 보간한 [lat,lng]."""
    return [
        start_lat + (goal_lat - start_lat) * f,
        start_lng + (goal_lng - start_lng) * f,
    ]


def transit_routes_stub(
    start_lat: float,
    start_lng: float,
    goal_lat: float,
    goal_lng: float,
) -> list[dict]:
    """결정적 스텁 경로 후보 목록을 돌려준다(입력만의 함수).

    지하철 전용 하나, 버스 전용 하나를 두어 목록 화면의 모드 아이콘과 지도
    폴리라인 렌더 경로를 실호출 없이도 끝까지 검증할 수 있게 한다. 구간
    geometry 는 출발-도착 직선을 등분한 점으로 만든다 — 실제 노선과는 무관.
    """

    straight_total_m = _haversine_m(start_lat, start_lng, goal_lat, goal_lng)

    def leg(
        step_type: str,
        line_name: str | None,
        start_name: str,
        end_name: str,
        minutes: int,
        station_count: int | None,
        f0: float,
        f1: float,
    ) -> dict:
        geometry = (
            []
            if step_type == "walk"
            else [
                _stub_lerp(start_lat, start_lng, goal_lat, goal_lng, f0),
                _stub_lerp(start_lat, start_lng, goal_lat, goal_lng, f1),
            ]
        )
        # 구간이 차지한 직선 몫으로 거리를 만든다. 이동수단별 비중을 보는
        # 화면·필터가 스텁에서도 그럴듯한 값을 받게 하려는 것이다.
        distance_m = int(round(straight_total_m * (f1 - f0)))
        return {
            "type": step_type,
            "line_name": line_name,
            "start_name": start_name,
            "end_name": end_name,
            "distance_m": distance_m,
            "section_time_min": minutes,
            "station_count": station_count,
            "geometry": geometry,
        }

    straight_m = _haversine_m(start_lat, start_lng, goal_lat, goal_lng)
    subway_ride = max(2, int(round(straight_m / 1000.0 / 32.0 * 60.0)))
    bus_ride = max(3, int(round(straight_m / 1000.0 / 18.0 * 60.0)))

    subway_legs = [
        leg("walk", None, "출발지", "출발역", 4, None, 0.0, 0.05),
        leg(
            "subway", "스텁 1호선", "출발역", "도착역",
            subway_ride, max(1, subway_ride // 2), 0.05, 0.9,
        ),
        leg("walk", None, "도착역", "도착지", 4, None, 0.9, 1.0),
    ]
    bus_legs = [
        leg("walk", None, "출발지", "정류장", 3, None, 0.0, 0.05),
        leg("bus", "스텁 402번", "정류장", "도착 정류장", bus_ride, None, 0.05, 0.9),
        leg("walk", None, "도착 정류장", "도착지", 5, None, 0.9, 1.0),
    ]

    def option(legs: list[dict], fare: int, modes: list[str]) -> dict:
        subway_m = sum(l["distance_m"] for l in legs if l["type"] == "subway")
        bus_m = sum(l["distance_m"] for l in legs if l["type"] == "bus")
        ride_m = subway_m + bus_m
        return {
            "total_time_min": sum(leg["section_time_min"] for leg in legs),
            "fare": fare,
            "transfer_count": 0,
            "total_walk_m": int(round(straight_m * 0.06)),
            "subway_distance_m": subway_m,
            "bus_distance_m": bus_m,
            "bus_distance_ratio": (bus_m / ride_m) if ride_m > 0 else 0.0,
            "modes": modes,
            "legs": legs,
        }

    return [
        option(subway_legs, 1400, ["subway"]),
        option(bus_legs, 1500, ["bus"]),
    ]
