"""/v1/places/photos (Google 장소 사진) 라우터·클라이언트 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
대신 새 FastAPI 에 hub_router 만 include 해 TestClient 로 라우트만 검증한다.
conftest 는 Google 키를 채우지 않으므로 라우트는 스텁 경로로 동작하며,
캐시(get_place_cache)는 lifespan 미기동 상태에서 None 이라 자연히 우회된다.

다루는 범위:
  - 스텁 모드 200 응답 형태와 출처 표기 노출
  - 좌표/검색어 경계 검증 실패(422)
  - 캐시 키 결정성(같은 입력 → 같은 키, 좌표가 다르면 다른 키)
  - GooglePlacesClient 의 요청 형태(POST 본문·필드 마스크 헤더)와 정규화
  - 하루 발급 상한 초과 시 빈 목록으로 흡수되고 발급 호출이 나가지 않음
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clients.hub_clients import GooglePlacesApiError, GooglePlacesClient
from app.config import settings
from app.place_stubs import google_photos_stub
from app.routers import hub_routers
from app.routers.hub_routers import (
    _google_media_budget_key,
    _google_placeid_cache_key,
    _seconds_to_next_kst_midnight,
)
from app.routers.hub_routers import router as hub_router
from app.schemas.hub_schemas import PlacePhotoItem

_KST = ZoneInfo("Asia/Seoul")

# 서울 시청 부근. 국내 좌표 범위 안이면 값 자체는 검증에 영향이 없다.
_LAT = 37.5663
_LNG = 126.9779


def _client() -> TestClient:
    """hub_router 만 실은 새 FastAPI 의 TestClient 를 만든다."""
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


def _params(**over) -> dict:
    """기본 좌표가 채워진 질의 파라미터를 만든다."""
    base = {"query": "경복궁", "lat": _LAT, "lng": _LNG}
    base.update(over)
    return base


def test_place_photos_stub_mode_shape():
    """키 미설정 → 스텁 경로로 200 과 기대 형태를 반환한다."""
    resp = _client().get("/v1/places/photos", params=_params())
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "경복궁"
    assert body["count"] == len(body["photos"])
    assert body["count"] >= 1
    first = body["photos"][0]
    assert set(first.keys()) >= {
        "photo_uri",
        "width_px",
        "height_px",
        "attributions",
        "google_maps_uri",
        "flag_content_uri",
    }


def test_place_photos_exposes_attribution():
    """출처 표기는 화면이 반드시 보여야 하므로 응답에 실려 나간다."""
    body = _client().get("/v1/places/photos", params=_params()).json()
    first = body["photos"][0]
    assert first["attributions"]
    assert first["attributions"][0]["display_name"]
    assert first["google_maps_uri"]


def test_place_photos_missing_query_422():
    """query 는 필수이므로 누락 시 422 이다."""
    resp = _client().get(
        "/v1/places/photos", params={"lat": _LAT, "lng": _LNG}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("missing", ["lat", "lng"])
def test_place_photos_missing_coordinate_422(missing):
    """좌표는 필수다 — 이름만으로는 다른 동네 지점이 잡힌다."""
    params = _params()
    params.pop(missing)
    resp = _client().get("/v1/places/photos", params=params)
    # 좌표를 감쌀 수 있게 되면서 lat/lng 는 선택 인자가 됐다. 그래서 빠졌을 때
    # 걸리는 자리가 형식 검증기(422)에서 좌표 해석기(400)로 옮겨졌다.
    # 둘 다 요청이 잘못됐다는 뜻이라 부르는 쪽 동작은 달라지지 않는다.
    assert resp.status_code in (400, 422)


@pytest.mark.parametrize("lat", [32.9, 43.1])
def test_place_photos_lat_out_of_range_422(lat):
    """위도가 국내 범위를 벗어나면 422 로 거절된다."""
    resp = _client().get("/v1/places/photos", params=_params(lat=lat))
    assert resp.status_code == 422


@pytest.mark.parametrize("lng", [123.9, 132.1])
def test_place_photos_lng_out_of_range_422(lng):
    """경도가 국내 범위를 벗어나면 422 로 거절된다."""
    resp = _client().get("/v1/places/photos", params=_params(lng=lng))
    assert resp.status_code == 422


def test_place_photos_query_too_long_422():
    """query 상한(60자)을 넘기면 422 로 거절된다."""
    resp = _client().get("/v1/places/photos", params=_params(query="가" * 61))
    assert resp.status_code == 422


def test_google_stub_matches_photo_item():
    """스텁 데이터가 PlacePhotoItem 스키마와 맞는다."""
    for p in google_photos_stub("q"):
        PlacePhotoItem(**p)


def test_google_stub_is_isolated_per_call():
    """스텁을 받아 고쳐도 다음 호출의 값이 오염되지 않는다."""
    first = google_photos_stub("q")
    first[0]["attributions"].append({"display_name": "x", "uri": None})
    second = google_photos_stub("q")
    assert len(second[0]["attributions"]) == 1


def test_placeid_cache_key_deterministic():
    """같은 입력은 같은 키를, 다른 입력은 다른 키를 만든다."""
    k1 = _google_placeid_cache_key("경복궁", _LAT, _LNG)
    k2 = _google_placeid_cache_key("경복궁", _LAT, _LNG)
    assert k1 == k2
    assert k1.startswith("google:placeid:")
    assert _google_placeid_cache_key("창덕궁", _LAT, _LNG) != k1


def test_placeid_cache_key_separates_coordinates():
    """이름이 같아도 좌표가 다르면 키가 갈린다(동명 지점 오염 방지)."""
    seoul = _google_placeid_cache_key("스타벅스", 37.5663, 126.9779)
    busan = _google_placeid_cache_key("스타벅스", 35.1796, 129.0756)
    assert seoul != busan


def test_media_budget_key_changes_daily():
    """카운터 키는 날짜별로 갈려 하루가 지나면 새로 센다."""
    d1 = datetime(2026, 8, 9, 23, 59, tzinfo=_KST)
    d2 = datetime(2026, 8, 10, 0, 1, tzinfo=_KST)
    assert _google_media_budget_key(d1) != _google_media_budget_key(d2)
    assert _google_media_budget_key(d1).endswith("20260809")


def test_seconds_to_next_kst_midnight_is_positive_and_bounded():
    """자정까지 남은 시간은 항상 양수이고 하루를 넘지 않는다."""
    for hour in (0, 12, 23):
        now = datetime(2026, 8, 9, hour, 30, tzinfo=_KST)
        ttl = _seconds_to_next_kst_midnight(now)
        assert 0 < ttl <= 86400


def _mock_client(handler) -> GooglePlacesClient:
    """MockTransport 를 물린 Google 클라이언트를 만든다."""
    client = GooglePlacesClient("test-key")
    client._client = httpx.AsyncClient(
        base_url=GooglePlacesClient.HOST,
        headers={"X-Goog-Api-Key": "test-key"},
        transport=httpx.MockTransport(handler),
    )
    return client


def test_search_place_id_sends_post_with_id_only_field_mask():
    """검색은 POST 본문으로 나가고, 필드 마스크는 식별자 하나로 좁혀진다.

    마스크에 사진 필드를 넣으면 과금 등급이 올라가므로 이 요청 형태 자체가
    비용 설계의 일부다.
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["mask"] = request.headers.get("X-Goog-FieldMask")
        seen["key"] = request.headers.get("X-Goog-Api-Key")
        seen["url"] = str(request.url)
        seen["body"] = httpx.Response(200, content=request.content).json()
        return httpx.Response(200, json={"places": [{"id": "PID-1"}]})

    client = _mock_client(handler)
    try:
        got = asyncio.run(client.search_place_id("경복궁", _LAT, _LNG, 500))
    finally:
        asyncio.run(client.aclose())

    assert got == "PID-1"
    assert seen["method"] == "POST"
    assert seen["mask"] == "places.id"
    assert seen["key"] == "test-key"
    # 키가 URL 에 실리면 로그·중계 구간에 그대로 남는다.
    assert "test-key" not in seen["url"]
    circle = seen["body"]["locationBias"]["circle"]
    assert circle["center"] == {"latitude": _LAT, "longitude": _LNG}
    assert circle["radius"] == 500.0
    assert seen["body"]["pageSize"] == 1


def test_search_place_id_returns_none_when_no_match():
    """검색 결과가 없으면 None 이다(무매칭도 정상 경로)."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    try:
        assert asyncio.run(
            client.search_place_id("없는장소", _LAT, _LNG, 500)
        ) is None
    finally:
        asyncio.run(client.aclose())


def test_fetch_photos_uses_photos_field_mask_and_normalizes():
    """사진 목록 조회는 사진 필드만 요구하고, 응답을 우리 표현으로 바꾼다."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["mask"] = request.headers.get("X-Goog-FieldMask")
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "photos": [
                    {
                        "name": "places/PID-1/photos/AAA",
                        "widthPx": 1600,
                        "heightPx": 1200,
                        "authorAttributions": [
                            {
                                "displayName": "홍길동",
                                "uri": "https://maps.example/u/1",
                            },
                            {"displayName": "  "},
                        ],
                        "googleMapsUri": "https://maps.example/p/1",
                        "flagContentUri": "https://maps.example/f/1",
                    },
                    {"widthPx": 100},
                ]
            },
        )

    client = _mock_client(handler)
    try:
        out = asyncio.run(client.fetch_photos("PID-1"))
    finally:
        asyncio.run(client.aclose())

    assert seen["mask"] == "photos"
    assert seen["path"] == "/v1/places/PID-1"
    # 이름 없는 항목은 URL 을 발급받을 수 없어 빠진다.
    assert len(out) == 1
    photo = out[0]
    assert photo["name"] == "places/PID-1/photos/AAA"
    assert photo["width_px"] == 1600
    assert photo["height_px"] == 1200
    assert photo["google_maps_uri"] == "https://maps.example/p/1"
    assert photo["flag_content_uri"] == "https://maps.example/f/1"
    # 이름이 빈 표기는 표기로서 의미가 없어 빠진다.
    assert photo["attributions"] == [
        {"display_name": "홍길동", "uri": "https://maps.example/u/1"}
    ]


def test_fetch_photo_uri_skips_redirect_and_requests_width():
    """이미지 URL 발급은 리다이렉트를 건너뛰고 요청 폭을 함께 보낸다."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200, json={"photoUri": "https://lh3.example/img=w800"}
        )

    client = _mock_client(handler)
    try:
        uri = asyncio.run(
            client.fetch_photo_uri("places/PID-1/photos/AAA", 800)
        )
    finally:
        asyncio.run(client.aclose())

    assert uri == "https://lh3.example/img=w800"
    assert seen["path"] == "/v1/places/PID-1/photos/AAA/media"
    assert seen["params"]["skipHttpRedirect"] == "true"
    assert seen["params"]["maxWidthPx"] == "800"


def test_fetch_photo_uri_empty_raises():
    """URL 이 비어 오면 예외로 바꿔 라우터의 degrade 경로에 태운다."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _mock_client(handler)
    try:
        with pytest.raises(GooglePlacesApiError) as ei:
            asyncio.run(
                client.fetch_photo_uri("places/PID-1/photos/AAA", 800)
            )
        assert ei.value.code == "EMPTY"
    finally:
        asyncio.run(client.aclose())


def test_non_json_200_raises_non_json():
    """200 이지만 비-JSON 본문은 NON_JSON 으로 변환된다."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = _mock_client(handler)
    try:
        with pytest.raises(GooglePlacesApiError) as ei:
            asyncio.run(client.search_place_id("q", _LAT, _LNG, 500))
        assert ei.value.code == "NON_JSON"
    finally:
        asyncio.run(client.aclose())


def test_http_error_status_is_wrapped():
    """200 이외 응답은 상태코드를 담은 예외로 변환된다."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="denied")

    client = _mock_client(handler)
    try:
        with pytest.raises(GooglePlacesApiError) as ei:
            asyncio.run(client.fetch_photos("PID-1"))
        assert ei.value.code == "HTTP_403"
    finally:
        asyncio.run(client.aclose())


class _FakeCache:
    """place_id 캐시와 발급 카운터를 흉내 내는 최소 캐시.

    incr_total 이 None 이면 카운터를 못 쓰는 상황(Redis 장애)을 흉내 낸다.
    올리고 내린 몫은 incr_calls 에 순서대로 남겨 되돌림까지 확인할 수 있게 한다.
    """

    def __init__(self, incr_total: int | None) -> None:
        self._incr_total = incr_total
        self.stored: dict = {}
        self.incr_calls: list[int] = []

    async def get_json(self, key: str):
        return self.stored.get(key)

    async def set_json(self, key: str, value, ttl_sec: int) -> None:
        self.stored[key] = value

    async def incr_by(
        self, key: str, amount: int, ttl_sec: int
    ) -> int | None:
        self.incr_calls.append(amount)
        return self._incr_total


class _CountingGoogleClient:
    """사진 목록은 돌려주되 URL 발급 호출 수를 세는 대역."""

    def __init__(self, photo_count: int) -> None:
        self.media_calls = 0
        self._photos = [
            {
                "name": f"places/PID-1/photos/{i}",
                "width_px": 800,
                "height_px": 600,
                "attributions": [{"display_name": "제공자", "uri": None}],
                "google_maps_uri": "https://maps.example/p",
                "flag_content_uri": "https://maps.example/f",
            }
            for i in range(photo_count)
        ]

    async def search_place_id(self, query, lat, lng, radius_m):
        return "PID-1"

    async def fetch_photos(self, place_id):
        return list(self._photos)

    async def fetch_photo_uri(self, photo_name, max_width_px):
        self.media_calls += 1
        return f"https://lh3.example/{photo_name}"


def _run_photos(monkeypatch, cache, client):
    """스텁 모드를 끄고 대역을 물린 채 사진 조회를 한 번 돌린다."""
    monkeypatch.setattr(
        settings, "GOOGLE_MAPS_API_KEY", type(settings.KMA_SERVICE_KEY)("k")
    )
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: cache)
    monkeypatch.setattr(hub_routers, "get_google_client", lambda: client)
    return asyncio.run(hub_routers._google_photos("경복궁", _LAT, _LNG))


def test_photos_returned_within_budget(monkeypatch):
    """상한 안에서는 사진이 그대로 나가고 URL 이 사진 수만큼 발급된다."""
    cache = _FakeCache(incr_total=3)
    client = _CountingGoogleClient(photo_count=3)
    out = _run_photos(monkeypatch, cache, client)
    assert len(out) == 3
    assert client.media_calls == 3
    assert out[0]["photo_uri"].startswith("https://lh3.example/")


def test_photos_capped_by_max_count(monkeypatch):
    """사진이 많아도 장소당 상한까지만 발급한다."""
    cache = _FakeCache(incr_total=1)
    client = _CountingGoogleClient(photo_count=10)
    out = _run_photos(monkeypatch, cache, client)
    assert len(out) == settings.GOOGLE_PHOTOS_MAX_COUNT
    assert client.media_calls == settings.GOOGLE_PHOTOS_MAX_COUNT


def test_photos_blocked_when_daily_budget_exceeded(monkeypatch):
    """하루 상한을 넘기면 빈 목록이고, 유료 발급 호출은 나가지 않는다."""
    cache = _FakeCache(
        incr_total=settings.GOOGLE_PHOTOS_DAILY_MEDIA_CAP + 1
    )
    client = _CountingGoogleClient(photo_count=3)
    out = _run_photos(monkeypatch, cache, client)
    assert out == []
    assert client.media_calls == 0


def test_rejected_request_returns_its_budget(monkeypatch):
    """상한에 걸려 거절하면 미리 더해 둔 몫을 되돌린다.

    되돌리지 않으면 거절된 요청이 쓰지도 않은 하루치를 갉아먹어, 남은 몫이
    실제보다 일찍 바닥난다.
    """
    cache = _FakeCache(
        incr_total=settings.GOOGLE_PHOTOS_DAILY_MEDIA_CAP + 1
    )
    _run_photos(monkeypatch, cache, _CountingGoogleClient(photo_count=3))
    assert cache.incr_calls == [3, -3]


def test_accepted_request_keeps_its_budget(monkeypatch):
    """상한 안에서 발급했으면 되돌리지 않는다(실제로 쓴 몫이다)."""
    cache = _FakeCache(incr_total=3)
    _run_photos(monkeypatch, cache, _CountingGoogleClient(photo_count=3))
    assert cache.incr_calls == [3]


def test_photos_blocked_when_counter_unavailable(monkeypatch):
    """카운터를 못 쓰면(Redis 장애) 유료 발급을 하지 않는다.

    상한이 요금 없는 범위를 지키려는 장치이므로, 셀 수 없는 동안 그냥
    내보내면 상한이 없는 것과 같아진다.
    """
    cache = _FakeCache(incr_total=None)
    client = _CountingGoogleClient(photo_count=3)
    out = _run_photos(monkeypatch, cache, client)
    assert out == []
    assert client.media_calls == 0


def test_photos_blocked_when_cache_missing(monkeypatch):
    """캐시 자체가 없으면 셀 수단이 없으므로 유료 발급을 하지 않는다."""
    monkeypatch.setattr(
        settings, "GOOGLE_MAPS_API_KEY", type(settings.KMA_SERVICE_KEY)("k")
    )
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    client = _CountingGoogleClient(photo_count=3)
    monkeypatch.setattr(hub_routers, "get_google_client", lambda: client)

    out = asyncio.run(hub_routers._google_photos("경복궁", _LAT, _LNG))
    assert out == []
    assert client.media_calls == 0


def test_photos_blocked_when_cap_is_zero(monkeypatch):
    """상한을 0 으로 두면 사진 조회를 끈다(유료 호출 0)."""
    monkeypatch.setattr(settings, "GOOGLE_PHOTOS_DAILY_MEDIA_CAP", 0)
    cache = _FakeCache(incr_total=1)
    client = _CountingGoogleClient(photo_count=3)
    out = _run_photos(monkeypatch, cache, client)
    assert out == []
    assert client.media_calls == 0
    # 셀 필요조차 없으므로 카운터를 건드리지 않는다.
    assert cache.incr_calls == []


def test_place_id_is_cached_but_photo_uri_is_not(monkeypatch):
    """캐시에 남는 것은 장소 식별자뿐이다(사진 URL 은 저장 금지 대상)."""
    cache = _FakeCache(incr_total=1)
    client = _CountingGoogleClient(photo_count=2)
    _run_photos(monkeypatch, cache, client)

    assert cache.stored
    dumped = repr(cache.stored)
    assert "PID-1" in dumped
    assert "lh3.example" not in dumped


def test_no_match_yields_empty_photos(monkeypatch):
    """대응하는 장소가 없으면 빈 목록이고 사진 조회로 넘어가지 않는다."""

    class _NoMatch(_CountingGoogleClient):
        async def search_place_id(self, query, lat, lng, radius_m):
            return None

    cache = _FakeCache(incr_total=1)
    client = _NoMatch(photo_count=3)
    assert _run_photos(monkeypatch, cache, client) == []
    assert client.media_calls == 0


def test_upstream_failure_degrades_to_empty(monkeypatch):
    """조회가 실패해도 예외를 밖으로 내지 않고 빈 목록으로 흡수한다."""

    class _Failing(_CountingGoogleClient):
        async def fetch_photos(self, place_id):
            raise GooglePlacesApiError("HTTP_500", "boom")

    cache = _FakeCache(incr_total=1)
    client = _Failing(photo_count=3)
    assert _run_photos(monkeypatch, cache, client) == []
    assert client.media_calls == 0


def test_single_media_failure_keeps_other_photos(monkeypatch):
    """URL 발급 한 건이 실패해도 나머지 사진은 살아남는다."""

    class _PartialFail(_CountingGoogleClient):
        async def fetch_photo_uri(self, photo_name, max_width_px):
            self.media_calls += 1
            if photo_name.endswith("1"):
                raise GooglePlacesApiError("HTTP_ERR", "timeout")
            return f"https://lh3.example/{photo_name}"

    cache = _FakeCache(incr_total=1)
    client = _PartialFail(photo_count=3)
    out = _run_photos(monkeypatch, cache, client)
    assert len(out) == 2


def test_endpoint_degrades_to_empty_list(monkeypatch):
    """업스트림이 죽어도 endpoint 는 5xx 가 아니라 빈 목록 200 을 낸다."""

    class _Failing(_CountingGoogleClient):
        async def search_place_id(self, query, lat, lng, radius_m):
            raise GooglePlacesApiError("HTTP_ERR", "connect timeout")

    monkeypatch.setattr(
        settings, "GOOGLE_MAPS_API_KEY", type(settings.KMA_SERVICE_KEY)("k")
    )
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(
        hub_routers, "get_google_client", lambda: _Failing(3)
    )

    resp = _client().get("/v1/places/photos", params=_params())
    assert resp.status_code == 200
    assert resp.json() == {"query": "경복궁", "photos": [], "count": 0}


def test_endpoint_degrades_when_lookup_exceeds_budget(monkeypatch):
    """조회가 전체 제한 시간을 넘기면 끊고 빈 목록으로 답한다.

    끊지 않으면 세 단계의 지연이 합쳐져 호출 측의 대기 시간을 넘기고, 그러면
    준비해 둔 "사진 없음" 응답 대신 끊긴 연결이 전달된다.
    """

    async def _never_returns(query, lat, lng):
        await asyncio.sleep(5)
        return [{"photo_uri": "https://lh3.example/late"}]

    monkeypatch.setattr(settings, "GOOGLE_PHOTOS_TOTAL_BUDGET_SEC", 0.05)
    monkeypatch.setattr(hub_routers, "_google_photos", _never_returns)

    resp = _client().get("/v1/places/photos", params=_params())
    assert resp.status_code == 200
    assert resp.json() == {"query": "경복궁", "photos": [], "count": 0}


def test_places_route_still_works_alongside_photos_route(monkeypatch):
    """사진 경로를 추가해도 기존 장소 목록 경로가 가려지지 않는다.

    코스 조회는 DB 를 타므로 중심 좌표를 없는 것으로 두어 카카오 스텁만으로
    응답하게 한다 — 여기서 보려는 것은 경로 매칭이지 코스 병합이 아니다.
    """

    async def _no_centroid(province: str, city: str):
        return None

    monkeypatch.setattr(
        hub_routers, "lookup_region_centroid", _no_centroid
    )
    resp = _client().get(
        "/v1/places", params={"province": "서울특별시", "city": "강남구"}
    )
    assert resp.status_code == 200
    assert "places" in resp.json()
