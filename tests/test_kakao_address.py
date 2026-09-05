"""/v1/places/address (주소 검색) 라우터·정규화 테스트.

앱이 카카오를 직접 부르던 자리를 hub 로 옮긴 경로다. 옮긴 목적이 발급처
키를 설치 파일에서 빼는 것이므로, 이 경로가 죽으면 앱은 주소 검색을 아예
못 한다 — 되돌아가 앱에서 직접 부르는 선택지는 없다.

app.main.app 은 lifespan 이 스케줄러/DB 를 기동하므로 임포트하지 않는다.
새 FastAPI 에 hub_router 만 실어 라우트만 본다.

다루는 범위:
  - 스텁 모드 200 응답 형태
  - 질의 누락·빈 문자열 검증 실패(422)
  - 캐시 키가 질의마다 갈리고 다른 카카오 캐시와 이름이 섞이지 않는 것
  - KakaoLocalClient.search_address 의 정규화(도로명 없음 → 빈 문자열)
  - 카카오 장애를 빈 목록으로 흡수하는 것(5xx 를 올리지 않는다)
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.clients.hub_clients import KakaoApiError, KakaoLocalClient
from app.config import settings
from app.routers.hub_routers import _kakao_address_cache_key, _kakao_cache_key
from app.routers.hub_routers import router as hub_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


@pytest.fixture
def real_key(monkeypatch):
    """스텁 경로를 벗어나 실호출 분기를 타게 한다."""
    monkeypatch.setattr(
        settings, "KAKAO_REST_API_KEY", SecretStr("test-key")
    )
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)


def test_stub_returns_addresses(stub_mode):
    r = _client().get("/v1/places/address", params={"query": "테헤란로"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == len(body["addresses"]) > 0
    first = body["addresses"][0]
    assert first["address"]
    # 좌표를 실어 보내지 않는다 — 봉투 없이 나가는 통로를 만들지 않기 위해서다.
    assert "lat" not in first and "lng" not in first


@pytest.mark.parametrize("params", [{}, {"query": ""}])
def test_missing_query_is_rejected(stub_mode, params):
    assert _client().get("/v1/places/address", params=params).status_code == 422


def test_cache_key_splits_by_query_and_namespace():
    a = _kakao_address_cache_key("테헤란로")
    b = _kakao_address_cache_key("세종대로")
    assert a != b
    assert a.startswith("kakao:addr:")
    # 장소 검색 캐시와 앞머리가 갈려야 한 쪽 결과가 다른 쪽으로 새지 않는다.
    assert not a.startswith(_kakao_cache_key("", "", "", None, 0)[:13])


@pytest.mark.asyncio
async def test_search_address_normalizes_missing_road(monkeypatch):
    client = KakaoLocalClient("key")

    async def fake_get_json(path, params):
        return {
            "documents": [
                {
                    "address_name": "서울 강남구 역삼동 823",
                    "road_address": {"address_name": "서울 강남구 테헤란로 1"},
                },
                # 도로명이 없는 주소도 있다. None 이 그대로 나가면 화면이 깨진다.
                {"address_name": "서울 종로구 청운동 1", "road_address": None},
            ]
        }

    monkeypatch.setattr(client, "_get_json", fake_get_json)
    out = await client.search_address("역삼")
    await client.aclose()

    assert out == [
        {
            "address": "서울 강남구 역삼동 823",
            "road_address": "서울 강남구 테헤란로 1",
        },
        {"address": "서울 종로구 청운동 1", "road_address": ""},
    ]


def test_kakao_failure_becomes_empty_list(monkeypatch, real_key):
    class Boom:
        async def search_address(self, query):
            raise KakaoApiError("HTTP_500", "boom")

    monkeypatch.setattr(
        "app.routers.hub_routers.get_kakao_client", lambda: Boom()
    )
    r = _client().get("/v1/places/address", params={"query": "역삼"})
    assert r.status_code == 200
    assert r.json() == {"addresses": [], "count": 0}
