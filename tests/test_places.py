"""장소 출처(카카오/두루누비) 순수 로직 단위 테스트.

외부 호출 없이 검증 가능한 부분만 다룬다:
  - 카카오 응답 정규화의 좌표 교차(x=경도/y=위도 → lat/lng)
  - 두루누비 응답 envelope 의 item 정규화/에러 처리
  - GPX 좌표 파싱과 요약(시작점/경계 상자)
  - 코스 메타 변환 헬퍼와 이동수단→구분 매핑
  - 스텁 데이터가 공용 스키마/적재 입력 형태와 맞는지
"""
from __future__ import annotations

import asyncio

import pytest

from app.clients.hub_clients import (
    DurunubiApiError,
    DurunubiClient,
    KakaoLocalClient,
    _redact_service_key,
)
from app.place_stubs import durunubi_course_stub, kakao_keyword_stub
from app.routers.hub_routers import _brd_div_for_mobility
from app.schemas.hub_schemas import PlaceItem
from app.scheduler import places_sync
from app.scheduler.places_sync import (
    _course_category,
    _to_float,
    _to_int,
)
from app.utils.gpx import parse_points, summarize


def test_kakao_normalize_swaps_xy_to_latlng():
    """카카오 문서의 x=경도/y=위도가 lat/lng 로 바르게 교차된다."""
    doc = {
        "id": "123",
        "place_name": "X",
        "address_name": "addr",
        "road_address_name": "road",
        "x": "127.05",
        "y": "37.55",
        "category_name": "cat",
        "category_group_code": "CE7",
        "phone": "02",
        "place_url": "http://example",
        "distance": "100",
    }
    out = KakaoLocalClient._normalize(doc)
    assert out["lat"] == pytest.approx(37.55)
    assert out["lng"] == pytest.approx(127.05)
    assert out["content_id"] == "kakao:123"
    assert out["source"] == "kakao"
    assert out["distance_m"] == 100
    # 정규화 결과가 공용 스키마로 그대로 구성되어야 한다.
    PlaceItem(**out)


def test_kakao_normalize_missing_distance_is_none():
    """거리 필드가 없으면 distance_m 은 None 이다."""
    doc = {"id": "1", "place_name": "Y", "x": "127.0", "y": "37.0"}
    out = KakaoLocalClient._normalize(doc)
    assert out["distance_m"] is None


def _env(code, item):
    """두루누비 응답 envelope 한 건을 만든다."""
    return {
        "response": {
            "header": {"resultCode": code, "resultMsg": "msg"},
            "body": {"items": {"item": item} if item is not None else ""},
        }
    }


def test_durunubi_items_list():
    """item 이 리스트면 그대로 리스트로 반환한다."""
    data = _env("0000", [{"crsIdx": "a"}, {"crsIdx": "b"}])
    assert DurunubiClient._items(data) == [
        {"crsIdx": "a"},
        {"crsIdx": "b"},
    ]


def test_durunubi_items_single_dict_normalized_to_list():
    """item 이 단건 dict 면 1건 리스트로 통일한다."""
    data = _env("00", {"crsIdx": "a"})
    assert DurunubiClient._items(data) == [{"crsIdx": "a"}]


def test_durunubi_items_nodata_returns_empty():
    """데이터 없음 코드는 빈 리스트로 처리한다."""
    assert DurunubiClient._items(_env("03", None)) == []


def test_durunubi_items_error_raises():
    """정상/데이터없음 외 코드는 예외로 바뀐다."""
    with pytest.raises(DurunubiApiError):
        DurunubiClient._items(_env("99", None))


def test_gpx_parse_and_summarize():
    """GPX 트랙에서 시작점과 경계 상자를 계산한다."""
    gpx = (
        '<?xml version="1.0"?>'
        '<gpx xmlns="http://www.topografix.com/GPX/1/1">'
        "<trk><trkseg>"
        '<trkpt lat="37.5" lon="127.0"/>'
        '<trkpt lat="37.6" lon="127.1"/>'
        "</trkseg></trk></gpx>"
    )
    points = parse_points(gpx)
    assert points == [(37.5, 127.0), (37.6, 127.1)]
    geom = summarize(points)
    assert geom is not None
    assert (geom.start_lat, geom.start_lng) == (37.5, 127.0)
    assert geom.min_lat == 37.5 and geom.max_lat == 37.6
    assert geom.min_lng == 127.0 and geom.max_lng == 127.1
    assert geom.point_count == 2


def test_gpx_empty_summarize_none():
    """좌표가 없으면 요약은 None 이다."""
    assert summarize([]) is None


def test_course_category_and_casts():
    """걷기/자전거 구분과 숫자 변환 헬퍼."""
    assert _course_category("DNWW") == "걷기길"
    assert _course_category("DNBW") == "자전거길"
    assert _course_category(None) is None
    assert _to_int("19") == 19
    assert _to_int("19.0") == 19
    assert _to_int("x") is None
    assert _to_float("8.5") == pytest.approx(8.5)
    assert _to_float(None) is None


def test_brd_div_for_mobility():
    """이동수단이 코스 구분 코드로 매핑된다."""
    assert _brd_div_for_mobility("walk") == "DNWW"
    assert _brd_div_for_mobility("foot") == "DNWW"
    assert _brd_div_for_mobility("bicycle") == "DNBW"
    assert _brd_div_for_mobility("car") is None
    assert _brd_div_for_mobility("transit") is None
    assert _brd_div_for_mobility(None) is None


def test_brd_div_kickboard_uses_bicycle_course():
    """킥보드는 걷기길이 아니라 자전거길로 매핑된다.

    구분을 비워 두면 계단·산책로가 포함된 걷기 전용 코스가 후보로 올라오고,
    반경 필터는 거리만 보므로 그것을 걸러내지 못한다.
    """
    assert _brd_div_for_mobility("scooter") == "DNBW"
    assert _brd_div_for_mobility("kickboard") == "DNBW"
    assert _brd_div_for_mobility("scooter") == _brd_div_for_mobility("bicycle")


def test_stub_shapes_valid():
    """스텁 데이터가 공용 스키마/적재 입력 형태와 맞는다."""
    for p in kakao_keyword_stub("q"):
        PlaceItem(**p)
    course_keys = {
        "content_id", "name", "address", "lat", "lng", "category",
        "crs_dstnc_km", "crs_total_min", "crs_level", "brd_div",
        "gpx_url", "route_idx", "bbox_min_lat", "bbox_min_lng",
        "bbox_max_lat", "bbox_max_lng",
    }
    for c in durunubi_course_stub():
        assert course_keys.issubset(c.keys())


def test_normalize_docs_skips_missing_or_bad_xy():
    """좌표가 없거나 깨진 카카오 문서는 정규화에서 제외된다."""
    docs = [
        {"id": "1", "place_name": "A", "x": "127.0", "y": "37.0"},
        {"id": "2", "place_name": "B"},  # x/y 없음
        {"id": "3", "place_name": "C", "x": "bad", "y": "37.0"},  # 비정상
    ]
    out = KakaoLocalClient._normalize_docs(docs)
    assert [p["content_id"] for p in out] == ["kakao:1"]


def test_redact_service_key():
    """오류 본문의 serviceKey 값이 가려진다."""
    txt = "<error>url=...&serviceKey=ABCDEF123456&pageNo=1</error>"
    red = _redact_service_key(txt)
    assert "serviceKey=***" in red
    assert "ABCDEF123456" not in red


def test_durunubi_sync_skips_when_already_loaded(monkeypatch):
    """부팅 동기화(force=False)는 이미 적재돼 있으면 외부 호출을 건너뛴다."""
    calls = {"upsert": 0}

    async def fake_count():
        return 5

    async def fake_upsert(course):
        calls["upsert"] += 1

    monkeypatch.setattr(places_sync, "count_courses", fake_count)
    monkeypatch.setattr(places_sync, "upsert_course", fake_upsert)
    asyncio.run(places_sync.durunubi_sync_loop(force=False))
    assert calls["upsert"] == 0


def test_safe_upsert_absorbs_error(monkeypatch):
    """한 코스 적재 실패는 흡수되어 False 를 반환한다(예외 미전파)."""
    async def boom(course):
        raise RuntimeError("db down")

    monkeypatch.setattr(places_sync, "upsert_course", boom)
    ok = asyncio.run(places_sync._safe_upsert({"content_id": "x"}))
    assert ok is False
