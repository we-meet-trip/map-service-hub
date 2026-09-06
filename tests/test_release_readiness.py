"""실제 공급자 실패와 스텁을 구분하고 nearby wire 계약을 지킨다."""
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import settings
from app.crypto.location_seal import open_seal, seal
from app.place_stubs import places_stub_active
from app.route_stubs import routing_stub_active, transit_stub_active
from app.routers import hub_routers
from tests.test_location_seal import KEY_B64


@pytest.mark.parametrize("checker", [places_stub_active, routing_stub_active, transit_stub_active])
def test_missing_provider_configuration_never_enables_stub(monkeypatch, checker):
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    assert not checker("")
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", True)
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    assert not checker("")


def test_stub_and_live_cache_entries_are_isolated(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", True)
    stub = hub_routers._nearby_cache_key(37.5, 127, "CE7", 1000, 10)
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    live = hub_routers._nearby_cache_key(37.5, 127, "CE7", 1000, 10)
    assert stub != live and "verified-v2" in live


def _client():
    app = FastAPI()
    app.include_router(hub_routers.router)
    return TestClient(app)


def test_nearby_sealed_request_and_response(monkeypatch):
    monkeypatch.setattr(settings, "LOCATION_WIRE_KEY", SecretStr(KEY_B64))
    fetch = AsyncMock(return_value=[{
        "content_id": "kakao:1", "source": "kakao", "name": "확인된 카페",
        "address": "주소", "lat": 37.5, "lng": 127.0,
    }])
    monkeypatch.setattr(hub_routers, "_kakao_nearby", fetch)
    response = _client().get("/v1/places/nearby", params={
        "loc": seal({"lat": 37.5, "lng": 127.0}), "category": "cafe",
    })
    assert response.status_code == 200
    assert set(response.json()) == {"loc"}
    assert "37.5" not in response.text and "확인된 카페" not in response.text
    payload = open_seal(response.json()["loc"])
    assert payload["count"] == 1 and payload["places"][0]["lat"] == 37.5
    fetch.assert_awaited_once_with(37.5, 127.0, "CE7", 1000, 10)


@pytest.mark.parametrize("point", [{"lat": 999, "lng": 127}, {"lat": "bad", "lng": 127}])
def test_nearby_revalidates_decrypted_coordinates(monkeypatch, point):
    monkeypatch.setattr(settings, "LOCATION_WIRE_KEY", SecretStr(KEY_B64))
    fetch = AsyncMock()
    monkeypatch.setattr(hub_routers, "_kakao_nearby", fetch)
    response = _client().get("/v1/places/nearby", params={"loc": seal(point), "category": "cafe"})
    assert response.status_code == 400
    fetch.assert_not_awaited()


def test_nearby_requires_sealed_point_when_key_is_set(monkeypatch):
    monkeypatch.setattr(settings, "LOCATION_WIRE_KEY", SecretStr(KEY_B64))
    response = _client().get("/v1/places/nearby", params={"lat": 37.5, "lng": 127, "category": "cafe"})
    assert response.status_code == 400


def test_nearby_unavailable_is_not_an_empty_success(monkeypatch):
    monkeypatch.setattr(settings, "LOCATION_WIRE_KEY", SecretStr(""))
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    monkeypatch.setattr(hub_routers, "get_place_cache", lambda: None)
    monkeypatch.setattr(hub_routers, "get_kakao_client", lambda: None)
    response = _client().get("/v1/places/nearby", params={"lat": 37.5, "lng": 127, "category": "cafe"})
    assert response.status_code == 503


def test_legacy_stub_course_rows_are_not_public_places(monkeypatch):
    monkeypatch.setattr(settings, "PLACES_STUB_MODE", False)
    monkeypatch.setattr(hub_routers, "_kakao_places", AsyncMock(return_value=[]))
    monkeypatch.setattr(hub_routers, "lookup_region_centroid", AsyncMock(return_value=(37.5, 127)))
    monkeypatch.setattr(hub_routers, "search_courses_nearby", AsyncMock(return_value=[{
        "content_id": "durunubi:stub-1", "source": "durunubi", "name": "스텁 코스",
        "address": "주소", "lat": 37.5, "lng": 127.0,
    }]))
    response = _client().get("/v1/places", params={"province": "서울특별시"})
    assert response.status_code == 200 and response.json()["places"] == []
