import asyncio

import pytest
from fastapi import HTTPException

from app.clients.hub_clients import KakaoApiError
from app.routers import hub_routers as routes


@pytest.mark.parametrize("code,retryable", [("HTTP_403", False), ("UNAVAILABLE", False), ("HTTP_503", True)])
def test_provider_failure_is_not_empty_search(monkeypatch, code, retryable):
    async def failed(*args):
        raise KakaoApiError(code, "private-provider-body")
    monkeypatch.setattr(routes, "_kakao_places", failed)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes.get_places("서울특별시", "강남구", "산책", None, "car", 15))
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "upstream_unavailable", "retryable": retryable}


def test_healthy_empty_search_remains_success(monkeypatch):
    async def empty(*args):
        return []
    monkeypatch.setattr(routes, "_kakao_places", empty)
    result = asyncio.run(routes.get_places("서울특별시", "강남구", "산책", None, "car", 15))
    assert result.count == 0


def test_provider_failure_preserves_other_source(monkeypatch):
    async def failed(*args):
        raise KakaoApiError("HTTP_403", "private-provider-body")
    async def centroid(*args):
        return 37.5, 127.0
    async def courses(*args, **kwargs):
        return [{"content_id": "durunubi:1", "source": "durunubi", "name": "검증 코스",
                 "lat": 37.5, "lng": 127.0}]
    monkeypatch.setattr(routes, "_kakao_places", failed)
    monkeypatch.setattr(routes, "lookup_region_centroid", centroid)
    monkeypatch.setattr(routes, "search_courses_nearby", courses)
    result = asyncio.run(routes.get_places("서울특별시", "강남구", "산책", None, "walk", 15))
    assert result.count == 1
    assert result.sources == {"durunubi": 1}
