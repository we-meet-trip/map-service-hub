"""키 미발급 단계에서 쓰는 장소 출처 스텁 데이터.

외부 키가 비어 있으면 실제 호출 대신 본 모듈의 고정 응답을 사용해
병합/적재/배선 로직을 키 없이도 끝까지 검증할 수 있게 한다. 키가
채워지면 호출부가 실제 클라이언트로 전환한다.
"""
from __future__ import annotations

from app.config import settings


def places_stub_active(secret_value: str) -> bool:
    """해당 출처를 스텁으로 다뤄야 하는지 판단한다.

    설정에서 스텁 모드가 켜져 있거나 키가 비어 있으면 True.
    """
    return settings.PLACES_STUB_MODE or not secret_value


# 카카오 키워드 검색 스텁 — 정규화된 장소 표현 몇 건.
_KAKAO_STUB: list[dict] = [
    {
        "content_id": "kakao:stub-1",
        "source": "kakao",
        "name": "스텁 카페",
        "address": "서울특별시 강남구 테헤란로 1",
        "road_address": "서울특별시 강남구 테헤란로 1",
        "lat": 37.4979,
        "lng": 127.0276,
        "category": "음식점 > 카페",
        "category_group_code": "CE7",
        "phone": None,
        "place_url": None,
        "distance_m": None,
    },
    {
        "content_id": "kakao:stub-2",
        "source": "kakao",
        "name": "스텁 전망대",
        "address": "서울특별시 중구 세종대로 110",
        "road_address": "서울특별시 중구 세종대로 110",
        "lat": 37.5663,
        "lng": 126.9779,
        "category": "관광,명소 > 전망대",
        "category_group_code": "AT4",
        "phone": None,
        "place_url": None,
        "distance_m": None,
    },
]


def kakao_keyword_stub(query: str) -> list[dict]:
    """카카오 키워드 검색 스텁 응답을 돌려준다(질의와 무관한 고정값)."""
    return [dict(p) for p in _KAKAO_STUB]


# 두루누비 코스 스텁 — 적재 입력 형태(좌표/경계 상자 포함).
_DURUNUBI_STUB: list[dict] = [
    {
        "content_id": "durunubi:stub-1",
        "name": "스텁 둘레길 1코스",
        "address": "서울 종로구",
        "lat": 37.5990,
        "lng": 126.9779,
        "category": "걷기길",
        "crs_dstnc_km": 8.0,
        "crs_total_min": 180,
        "crs_level": 2,
        "brd_div": "DNWW",
        "gpx_url": None,
        "route_idx": "durunubi:stub-route-1",
        "bbox_min_lat": 37.59,
        "bbox_min_lng": 126.97,
        "bbox_max_lat": 37.61,
        "bbox_max_lng": 126.99,
    },
    {
        "content_id": "durunubi:stub-2",
        "name": "스텁 자전거길 1코스",
        "address": "서울 송파구",
        "lat": 37.5145,
        "lng": 127.1059,
        "category": "자전거길",
        "crs_dstnc_km": 20.0,
        "crs_total_min": 90,
        "crs_level": 1,
        "brd_div": "DNBW",
        "gpx_url": None,
        "route_idx": "durunubi:stub-route-2",
        "bbox_min_lat": 37.50,
        "bbox_min_lng": 127.09,
        "bbox_max_lat": 37.53,
        "bbox_max_lng": 127.12,
    },
]


def durunubi_course_stub() -> list[dict]:
    """두루누비 코스 스텁(적재 입력 형태)을 돌려준다."""
    return [dict(c) for c in _DURUNUBI_STUB]


# 네이버 블로그 리뷰 스텁 — ReviewItem 형태의 고정 스니펫 몇 건.
_NAVER_BLOG_STUB: list[dict] = [
    {
        "title": "스텁 맛집 방문 후기",
        "description": "분위기 좋고 재방문 의사 있는 곳. 주차도 편리했다.",
        "bloggername": "스텁블로거1",
        "postdate": "20260101",
        "link": "https://blog.example/stub-1",
    },
    {
        "title": "스텁 카페 다녀왔어요",
        "description": "디저트가 훌륭하고 좌석이 넉넉해 오래 머물기 좋다.",
        "bloggername": "스텁블로거2",
        "postdate": "20260102",
        "link": "https://blog.example/stub-2",
    },
]


def naver_blog_stub(
    query: str, display: int = 5, start: int = 1
) -> list[dict]:
    """네이버 블로그 리뷰 스텁 응답을 돌려준다(질의와 무관한 고정값).

    자격증명 없이도 더보기 동작을 확인할 수 있도록 요청 구간을 흉내 낸다.
    고정 항목 수를 넘어선 구간은 빈 목록이 되어, 호출 측은 실제 호출과
    같은 방식으로 "더 없음"을 판정할 수 있다.
    """
    begin = max(start - 1, 0)
    return [dict(r) for r in _NAVER_BLOG_STUB[begin:begin + display]]
