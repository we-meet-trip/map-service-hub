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


# 장소 사진 스텁 — PlacePhotoItem 형태의 고정 항목 몇 건.
# 실제 응답과 같은 자리에 표기 정보를 채워, 키 없이도 화면의 출처 표기
# 경로까지 확인할 수 있게 한다.
_GOOGLE_PHOTO_STUB: list[dict] = [
    {
        "photo_uri": "https://photos.example/stub-place-1.jpg",
        "width_px": 1600,
        "height_px": 1200,
        "attributions": [
            {
                "display_name": "스텁 제공자1",
                "uri": "https://maps.example/contrib/stub-1",
            }
        ],
        "google_maps_uri": "https://maps.example/photo/stub-1",
        "flag_content_uri": "https://maps.example/flag/stub-1",
    },
    {
        "photo_uri": "https://photos.example/stub-place-2.jpg",
        "width_px": 1200,
        "height_px": 1600,
        "attributions": [
            {
                "display_name": "스텁 제공자2",
                "uri": None,
            }
        ],
        "google_maps_uri": "https://maps.example/photo/stub-2",
        "flag_content_uri": "https://maps.example/flag/stub-2",
    },
]


def google_photos_stub(query: str) -> list[dict]:
    """장소 사진 스텁 응답을 돌려준다(질의와 무관한 고정값).

    표기 목록까지 새로 만들어 돌려준다 — 얕은 복사만 하면 호출 측이 목록을
    건드릴 때 모듈 상수인 원본이 함께 바뀐다.
    """
    out: list[dict] = []
    for p in _GOOGLE_PHOTO_STUB:
        item = dict(p)
        item["attributions"] = [dict(a) for a in p["attributions"]]
        out.append(item)
    return out


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


# 따릉이 대여소 스텁 — 요청 좌표를 기준으로 만들어 낸다. 고정 좌표를 쓰면
# 지도가 대여소 없는 곳을 비춰 화면이 비어 보이므로, 어디를 보고 있든 주변에
# 몇 개가 찍히도록 요청한 좌표에서 밀어 만든다.
#
# 밀어 내는 폭은 위도 0.004° 남짓(약 400m)까지라 기본 반경 안에 들어온다.
_SEOUL_BIKE_STUB_OFFSETS: list[tuple[float, float, str, int, int]] = [
    (0.0031, 0.0018, "스텁 대여소 A", 20, 7),
    (-0.0024, 0.0035, "스텁 대여소 B", 15, 0),
    (0.0012, -0.0040, "스텁 대여소 C", 10, 3),
]


# 공유 킥보드 스텁 — 대여소와 같은 이유로 요청 좌표를 기준으로 만든다.
# 기기는 길가에 흩어져 있으므로 대여소보다 촘촘하게 둔다(위도 0.002° ≈ 220m).
_PM_STUB_OFFSETS: list[tuple[float, float, str, int, str]] = [
    (0.0016, 0.0009, "Beam", 82, "전동킥보드"),
    (-0.0012, 0.0018, "GCOO", 45, "전동킥보드"),
    (0.0007, -0.0021, "SWING", 17, "전동킥보드"),
    (-0.0019, -0.0006, "지쿠", 96, "전동자전거"),
]


def pm_vehicle_stub(lat: float, lng: float) -> list[dict]:
    """공유 킥보드 스텁 응답을 돌려준다(요청 좌표만의 함수).

    같은 좌표로 물으면 같은 목록이 온다. 배터리 잔량을 넓게 흩어 두어 잔량에
    따라 표시를 달리하는 화면 동작을 키 없이도 확인할 수 있다.
    """
    out: list[dict] = []
    for i, (dlat, dlng, provider, battery, kind) in enumerate(
        _PM_STUB_OFFSETS, start=1
    ):
        out.append(
            {
                "provider": provider,
                "device_id": f"PM-STUB-{i}",
                "battery_level": battery,
                "vehicle_type": kind,
                "lat": round(lat + dlat, 7),
                "lng": round(lng + dlng, 7),
            }
        )
    return out


def seoul_bike_stub(lat: float, lng: float) -> list[dict]:
    """따릉이 대여소 스텁 응답을 돌려준다(요청 좌표만의 함수).

    같은 좌표로 물으면 같은 목록이 온다. 대여 가능 수를 0 인 곳까지 섞어 두어
    "빌릴 수 있는 곳만 보기" 같은 걸러내기 동작을 키 없이도 확인할 수 있다.
    """
    out: list[dict] = []
    for i, (dlat, dlng, name, rack, parked) in enumerate(
        _SEOUL_BIKE_STUB_OFFSETS, start=1
    ):
        out.append(
            {
                "station_id": f"ST-STUB-{i}",
                "name": name,
                "rack_total": rack,
                "parking_bike_total": parked,
                "lat": round(lat + dlat, 7),
                "lng": round(lng + dlng, 7),
            }
        )
    return out
