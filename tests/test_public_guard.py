"""public_guard (AUTH_ENFORCED) 공개 endpoint 인증 가드 테스트.

settings.AUTH_ENFORCED 를 monkeypatch 로 토글하며 검증한다:
  - off(기본) → 토큰 없이도 통과
  - on + 토큰 누락/불일치 → 401
  - on + 올바른 토큰 → 통과

가드가 실제 라우터에 배선됐는지까지 함께 확인하려고 DB 없는
indoor-bonus 엔드포인트로 요청한다. conftest 가 주입한
INTERNAL_SERVICE_TOKEN="test-internal-token" 이 올바른 토큰이다.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routers.rules_router import router as rules_router

_PATH = "/v1/rules/score/indoor-bonus"
_BODY = {
    "pois": [{"content_id": "a", "indoor_flag": True, "base_score": 5.0}],
    "day_pop_max": 50,
}


def _client() -> TestClient:
    """rules_router(public_guard 부착)만 실은 새 FastAPI 의 TestClient."""
    app = FastAPI()
    app.include_router(rules_router)
    return TestClient(app)


def test_public_guard_disabled_passes_without_token(monkeypatch):
    """AUTH_ENFORCED=false 면 토큰 없이도 200(현행 데모 동작)."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", False)
    resp = _client().post(_PATH, json=_BODY)
    assert resp.status_code == 200


def test_public_guard_enabled_missing_token_401(monkeypatch):
    """AUTH_ENFORCED=true 인데 토큰이 없으면 401."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    resp = _client().post(_PATH, json=_BODY)
    assert resp.status_code == 401


def test_public_guard_enabled_wrong_token_401(monkeypatch):
    """AUTH_ENFORCED=true + 틀린 토큰이면 401."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    resp = _client().post(
        _PATH, json=_BODY, headers={"X-Internal-Token": "wrong-token"}
    )
    assert resp.status_code == 401


def test_public_guard_enabled_correct_token_passes(monkeypatch):
    """AUTH_ENFORCED=true + 올바른 토큰이면 통과(200)."""
    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    token = settings.INTERNAL_SERVICE_TOKEN.get_secret_value()
    resp = _client().post(
        _PATH, json=_BODY, headers={"X-Internal-Token": token}
    )
    assert resp.status_code == 200


def test_public_guard_non_ascii_token_is_401_not_typeerror(monkeypatch):
    """AUTH_ENFORCED=true + 비-ASCII 토큰 → HTTPException(401), TypeError 아님.

    Starlette 는 수신 헤더를 latin-1 로 디코딩하므로 0x80-0xFF 바이트는
    비-ASCII str 이 된다(httpx TestClient 는 이런 헤더의 '송신'을 ascii 로
    거부하므로, 수신측 가드를 직접 호출해 검증한다). 헤더를 바이트로
    인코딩해 비교하지 않으면 hmac.compare_digest 가 TypeError 를 던져 500 이
    되는데, 그렇지 않고 조용히 불일치(401)로 fail-closed 됨을 확인한다."""
    import asyncio as _asyncio

    from fastapi import HTTPException

    from app.routers.guards import public_guard

    class _FakeReq:
        def __init__(self, token: str) -> None:
            self.headers = {"X-Internal-Token": token}

    monkeypatch.setattr(settings, "AUTH_ENFORCED", True)
    with pytest.raises(HTTPException) as ei:
        _asyncio.run(public_guard(_FakeReq("\xff\x80")))
    assert ei.value.status_code == 401
