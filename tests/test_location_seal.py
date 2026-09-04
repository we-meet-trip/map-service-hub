"""서비스 사이를 오가는 좌표 봉투를 여는 쪽 검사.

만드는 쪽은 자바(BFF)라 여기서 만들 수 없다. 그래서 자바가 만든 값을 그대로
적어 두고 열어 본다 — 형식이 어긋나면 배포한 뒤 모든 좌표 요청이 한꺼번에
거절되는데, 그 사실은 화면에서 "정보를 가져오지 못했어요" 로만 보인다.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pydantic import SecretStr

from app.config import settings
from app.crypto.location_seal import SealError, is_sealed, open_seal

KEY_B64 = "bWFwLXdpcmUtdGVzdC1rZXktMDAwMDAwMDAwMDAwMCE="
KEY = base64.b64decode(KEY_B64)


@pytest.fixture(autouse=True)
def _wire_key():
    """이 파일 안에서만 열쇠를 세운다.

    전역으로 세우면 좌표를 값으로 보내는 다른 검사들이 한꺼번에 거절된다 —
    열쇠가 있으면 봉투만 받도록 해 두었기 때문이다.
    """
    before = settings.LOCATION_WIRE_KEY
    settings.LOCATION_WIRE_KEY = SecretStr(KEY_B64)
    yield
    settings.LOCATION_WIRE_KEY = before


def make(payload: dict) -> str:
    """자바 쪽 LocationSeal 과 같은 형식으로 봉투를 만든다."""
    iv = b"0123456789ab"
    ct = AESGCM(KEY).encrypt(iv, json.dumps(payload).encode(), b"map|loc|v1")

    def b64u(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"v1.{b64u(iv)}.{b64u(ct)}"


def test_opens_sealed_point():
    token = make({"lat": 35.1587, "lng": 129.1604, "iat": int(time.time())})

    assert is_sealed(token)
    assert open_seal(token)["lat"] == 35.1587


def test_opens_value_sealed_by_java(monkeypatch):
    """자바가 만든 봉투를 그대로 연다."""
    # 만드는 쪽은 자바라 여기서 만들 수 없다. 아래 값은 map-service-user 에서
    # `./gradlew test --tests '*LocationSealTest*'` 를 돌려 그 검사가 감싼 값을
    # 그대로 옮겨 적은 것이다. 형식을 바꿀 때는 같은 방법으로 다시 뽑아 갱신한다.
    #
    # 수명은 여기서 보지 않는다. 박아 둔 값은 만든 시각이 고정이라 그대로 두면
    # 잠시 뒤부터 형식과 무관하게 늘 거절된다. 여기서 볼 것은 두 언어가 같은
    # 형식을 쓰는지 하나뿐이고, 수명 정책은 다른 검사가 본다.
    monkeypatch.setattr("app.crypto.location_seal.MAX_AGE_SECONDS", 10 ** 9)
    from_java = (
        "v1.VJr87jPJmpbR4MT9.t5mbqhTwLVC7Zot9wvE6tSmeHUeoCJq0ngqxodQ3NJJa"
        "vvMfjCMes25nyuFT0-eJwjxSj6FDcgfqJJIkGhuZ"
    )

    assert is_sealed(from_java)
    opened = open_seal(from_java)
    assert (opened["lat"], opened["lng"]) == (35.1587, 129.1604)


def test_rejects_tampered_value():
    # 검증표가 있어 한 글자만 바꿔도 열리지 않는다. 열린다면 그 순간부터
    # 아무나 좌표를 만들어 넣을 수 있다.
    token = make({"lat": 35.1587, "lng": 129.1604, "iat": int(time.time())})

    with pytest.raises(SealError):
        open_seal(token[:-4] + "AAAA")


def test_rejects_other_key():
    other = AESGCM(b"0" * 32).encrypt(
        b"0123456789ab", json.dumps({"lat": 1, "lng": 2, "iat": 0}).encode(),
        b"map|loc|v1",
    )
    token = "v1." + base64.urlsafe_b64encode(b"0123456789ab").decode().rstrip("=") \
        + "." + base64.urlsafe_b64encode(other).decode().rstrip("=")

    with pytest.raises(SealError):
        open_seal(token)


def test_rejects_stale_value():
    # 지나간 봉투를 주워 나중에 다시 보내는 것을 막는다. 만든 시각이 없으면
    # 같은 봉투가 영원히 유효하다.
    token = make({"lat": 35.1587, "lng": 129.1604, "iat": int(time.time()) - 3600})

    with pytest.raises(SealError):
        open_seal(token)


def test_rejects_value_without_issue_time():
    token = make({"lat": 35.1587, "lng": 129.1604})

    with pytest.raises(SealError):
        open_seal(token)


def test_plain_value_is_not_sealed():
    assert not is_sealed("35.1587")
    assert not is_sealed(None)


def _client():
    """좌표를 받는 라우터만 실은 TestClient 를 만든다."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers.hub_routers import router as hub_router

    app = FastAPI()
    app.include_router(hub_router)
    return TestClient(app)


def test_endpoint_rejects_plain_coordinates_when_key_is_set():
    # 열쇠가 있는데 값으로 온 좌표를 받아 주면, 감싸는 쪽이 조용히 고장 나도
    # 아무도 알아차리지 못한 채 예전처럼 평문이 흐른다.
    resp = _client().get(
        "/v1/mobility/bike-stations", params={"lat": 37.5665, "lng": 126.978}
    )

    assert resp.status_code == 400


def test_endpoint_accepts_sealed_coordinates():
    token = make({"lat": 37.5665, "lng": 126.978, "iat": int(time.time())})

    resp = _client().get("/v1/mobility/bike-stations", params={"loc": token})

    # 발급처 자격증명이 없어 결과는 비어 있지만, 좌표를 열어 여기까지 왔다는
    # 사실이 중요하다. 열지 못했다면 400 에서 끝났을 것이다.
    assert resp.status_code == 200


def test_endpoint_rejects_sealed_value_from_another_key():
    other = AESGCM(b"1" * 32).encrypt(
        b"0123456789ab",
        json.dumps({"lat": 37.5665, "lng": 126.978, "iat": int(time.time())}).encode(),
        b"map|loc|v1",
    )

    def b64u(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    token = f"v1.{b64u(b'0123456789ab')}.{b64u(other)}"

    assert _client().get(
        "/v1/mobility/bike-stations", params={"loc": token}
    ).status_code == 400


def test_transit_routes_rejects_plain_coordinates_when_key_is_set():
    # 지하철 단독 경로와 같은 규칙이 통합 길찾기에도 걸려 있는지 본다.
    resp = _client().get(
        "/v1/transit/routes",
        params={"start_lat": 37.4979, "start_lng": 127.0276,
                "end_lat": 37.5663, "end_lng": 126.9779},
    )

    assert resp.status_code == 400


def test_transit_routes_accepts_sealed_coordinates():
    token = make({"start_lat": 37.4979, "start_lng": 127.0276,
                  "end_lat": 37.5663, "end_lng": 126.9779,
                  "iat": int(time.time())})

    resp = _client().get(
        "/v1/transit/routes", params={"loc": token, "mode": "all"}
    )

    # 발급처 자격증명이 없어 결과는 비어 있지만, 좌표를 열어 여기까지 왔다는
    # 사실이 중요하다. 열지 못했다면 400 에서 끝났을 것이다.
    assert resp.status_code == 200


def test_sealed_mobility_filter_counts_dropped_correctly():
    # 제외 건수를 봉투 열기 전 목록으로 세면 음수가 된다. 응답 자체는 그럴듯해
    # 보여서, 세는 자리가 틀린 것을 값으로만 알아차릴 수 있다.
    token = make({
        "origin": {"lat": 35.1532, "lng": 129.1187},
        "candidates": [
            {"content_id": "a", "lat": 35.1540, "lng": 129.1190},
            {"content_id": "b", "lat": 37.5665, "lng": 126.9780},
        ],
        "iat": int(time.time()),
    })

    resp = _rules_client().post(
        "/v1/rules/filter/mobility-radius",
        json={"mobility": "walk", "loc": token},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert [p["content_id"] for p in body["filtered"]] == ["a"]
    assert body["dropped"] == 1


def _rules_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers.rules_router import router as rules_router

    app = FastAPI()
    app.include_router(rules_router)
    return TestClient(app)
