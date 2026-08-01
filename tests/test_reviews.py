"""/v1/reviews (네이버 블로그) 라우터·정규화 테스트.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
대신 새 FastAPI 에 hub_router 만 include 해 TestClient 로 라우트만 검증한다.
conftest 는 NAVER 자격증명을 채우지 않으므로 라우트는 스텁 경로로 동작하며,
캐시(get_place_cache)는 lifespan 미기동 상태에서 None 이라 자연히 우회된다.

다루는 범위:
  - 스텁 모드 200 응답 형태
  - display 경계(0/11) 및 sort literal 검증 실패(422)
  - NaverBlogClient._normalize_items 의 <b> 마크업/HTML 엔티티 제거(순수 단위)
  - _naver_cache_key 결정성(같은 입력 → 같은 키)
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.clients.hub_clients import NaverApiError, NaverBlogClient
from app.place_stubs import naver_blog_stub
from app.routers.hub_routers import _naver_cache_key
from app.routers.hub_routers import router as hub_router
from app.schemas.hub_schemas import ReviewItem


def _client() -> TestClient:
    """hub_router 만 실은 새 FastAPI 의 TestClient 를 만든다."""
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


def test_reviews_stub_mode_shape():
    """자격증명 미설정 → 스텁 경로로 200 과 기대 형태를 반환한다."""
    resp = _client().get("/v1/reviews", params={"query": "강남 맛집"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "강남 맛집"
    assert body["count"] == len(body["reviews"])
    assert body["count"] >= 1
    first = body["reviews"][0]
    assert set(first.keys()) >= {
        "title",
        "description",
        "bloggername",
        "postdate",
        "link",
    }


def test_reviews_valid_sort_date_ok():
    """sort=date 는 허용 literal 이므로 200 이다."""
    resp = _client().get(
        "/v1/reviews", params={"query": "x", "sort": "date"}
    )
    assert resp.status_code == 200


@pytest.mark.parametrize("display", [0, 11])
def test_reviews_display_out_of_bounds_422(display):
    """display 는 1~10 밖(0/11)이면 422 로 거절된다."""
    resp = _client().get(
        "/v1/reviews", params={"query": "x", "display": display}
    )
    assert resp.status_code == 422


def test_reviews_sort_literal_422():
    """sort 는 sim/date 외 값이면 422 로 거절된다."""
    resp = _client().get(
        "/v1/reviews", params={"query": "x", "sort": "bogus"}
    )
    assert resp.status_code == 422


def test_reviews_missing_query_422():
    """query 는 필수이므로 누락 시 422 이다."""
    resp = _client().get("/v1/reviews")
    assert resp.status_code == 422


def test_normalize_items_strips_markup_and_entities():
    """<b></b> 마크업과 HTML 엔티티가 순수 텍스트로 정규화된다."""
    items = [
        {
            "title": "<b>강남</b> 맛집 &amp; 카페",
            "description": "가격 &lt; 5000 &quot;착한&quot; 집 &gt;",
            "bloggername": "blogger",
            "postdate": "20260101",
            "link": "http://example",
        }
    ]
    out = NaverBlogClient._normalize_items(items)
    assert out[0]["title"] == "강남 맛집 & 카페"
    assert out[0]["description"] == '가격 < 5000 "착한" 집 >'
    assert "<b>" not in out[0]["title"]
    assert "</b>" not in out[0]["title"]


def test_normalize_items_handles_none_values():
    """title/description 이 None 이어도 빈 문자열로 안전하게 정규화된다."""
    out = NaverBlogClient._normalize_items(
        [{"title": None, "description": None}]
    )
    assert out[0]["title"] == ""
    assert out[0]["description"] == ""
    assert out[0]["bloggername"] is None
    assert out[0]["link"] is None


def test_naver_stub_matches_review_item():
    """스텁 데이터가 ReviewItem 스키마와 맞는다."""
    for r in naver_blog_stub("q"):
        ReviewItem(**r)


def test_naver_non_json_200_raises_non_json():
    """200 이지만 비-JSON 본문(점검/프록시 페이지)은 NaverApiError(NON_JSON)로
    변환된다 — 라우터 degrade(빈 리스트) 경로에 태워 5xx 를 막는 계약."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = NaverBlogClient("id", "secret")
    client._client = httpx.AsyncClient(
        base_url=NaverBlogClient.HOST,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(NaverApiError) as ei:
            asyncio.run(client.search_blog("강남"))
        assert ei.value.code == "NON_JSON"
    finally:
        asyncio.run(client.aclose())


def test_naver_cache_key_deterministic():
    """같은 입력은 같은 키를, 다른 입력은 다른 키를 만든다."""
    k1 = _naver_cache_key("강남", 5, "sim")
    k2 = _naver_cache_key("강남", 5, "sim")
    assert k1 == k2
    assert k1.startswith("naver:blog:")
    assert _naver_cache_key("강남", 6, "sim") != k1
    assert _naver_cache_key("강남", 5, "date") != k1
    assert _naver_cache_key("역삼", 5, "sim") != k1


def test_naver_cache_key_separates_pages():
    """구간이 다르면 캐시 키도 달라야 더보기가 같은 목록을 되풀이하지 않는다."""
    first = _naver_cache_key("강남", 5, "sim", 1)
    second = _naver_cache_key("강남", 5, "sim", 6)
    assert first != second


def test_reviews_start_returns_next_page():
    """start 를 옮기면 다음 구간이 온다(스텁도 구간을 흉내 낸다)."""
    client = _client()
    first = client.get(
        "/v1/reviews", params={"query": "강남 맛집", "display": 1, "start": 1}
    ).json()
    second = client.get(
        "/v1/reviews", params={"query": "강남 맛집", "display": 1, "start": 2}
    ).json()
    assert first["start"] == 1 and second["start"] == 2
    assert first["reviews"][0]["link"] != second["reviews"][0]["link"]


def test_reviews_past_last_page_is_empty():
    """구간이 끝을 넘어서면 빈 목록 — 호출 측이 더보기를 멈출 근거가 된다."""
    body = _client().get(
        "/v1/reviews", params={"query": "강남 맛집", "start": 99}
    ).json()
    assert body["count"] == 0
    assert body["reviews"] == []


def test_reviews_start_out_of_range_rejected():
    """start 는 1 이상이어야 한다."""
    resp = _client().get(
        "/v1/reviews", params={"query": "강남 맛집", "start": 0}
    )
    assert resp.status_code == 422
