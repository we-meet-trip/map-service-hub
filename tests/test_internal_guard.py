"""internal_guard 의 CIDR 화이트리스트·토큰 비교 판정 테스트.

_is_trusted: settings.HUB_INTERNAL_TRUSTED_CIDRS 기본값
  "172.16.0.0/12, 10.0.0.0/8, 192.168.0.0/16"
에 대해 각 IP 가 신뢰 대역에 들어가는지 검사한다.

127.0.0.1 (loopback) 은 명시적으로 신뢰 대역에 포함되지 않으므로 False 가
정상이다. 잘못된 입력(빈 문자열, 비-IP 문자열)도 False.

internal_guard: CIDR 통과 후의 토큰 비교 경로를 직접 호출해 검증한다.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routers.internal_router import _is_trusted, internal_guard


@pytest.mark.parametrize(
    "ip,trusted",
    [
        ("172.18.0.5", True),
        ("172.20.10.1", True),
        ("10.42.7.1", True),
        ("192.168.1.100", True),
        ("127.0.0.1", False),
        ("203.0.113.5", False),
        ("8.8.8.8", False),
        ("", False),
        ("not-an-ip", False),
    ],
)
def test_is_trusted(ip, trusted):
    """ip 가 신뢰 CIDR 에 속하는지의 판정 결과가 기대값과 일치하는지 검증."""
    assert _is_trusted(ip) is trusted


class _FakeReq:
    """internal_guard 직접 호출용 최소 Request 대역.

    internal_guard 는 request.client.host 를 먼저 읽어 CIDR 을 판정하므로
    headers 뿐 아니라 client 속성도 필요하다. host 를 신뢰 CIDR IP 로 두어야
    토큰 비교 경로까지 도달한다.
    """

    class _Client:
        def __init__(self, host: str) -> None:
            self.host = host

    def __init__(self, host: str, token: str | None) -> None:
        self.client = self._Client(host)
        self.headers = {} if token is None else {"X-Internal-Token": token}


def test_internal_guard_non_ascii_token_is_403_not_typeerror():
    """비-ASCII 토큰 → HTTPException(403). TypeError(→500) 가 아니어야 한다.

    Starlette 는 수신 헤더를 latin-1 로 디코딩하므로 0x80-0xFF 바이트는
    비-ASCII str 이 된다(httpx TestClient 는 그런 헤더의 '송신'을 거부하므로
    수신측 가드를 직접 호출해 검증한다). 토큰을 str 로 비교하면
    hmac.compare_digest 가 TypeError 를 던져 500 이 되므로, 바이트로 비교해
    조용히 불일치(403)로 fail-closed 되는지 확인한다.
    """
    with pytest.raises(HTTPException) as ei:
        asyncio.run(internal_guard(_FakeReq("10.0.0.1", "\xff\x80")))
    assert ei.value.status_code == 403


def test_internal_guard_wrong_ascii_token_is_403():
    """대조군 — 평범한 불일치 토큰도 동일하게 403 이어야 한다."""
    with pytest.raises(HTTPException) as ei:
        asyncio.run(internal_guard(_FakeReq("10.0.0.1", "wrong-token")))
    assert ei.value.status_code == 403


def test_internal_guard_valid_token_passes():
    """대조군 — 신뢰 IP + 정상 토큰이면 예외 없이 통과해야 한다."""
    token = settings.INTERNAL_SERVICE_TOKEN.get_secret_value()
    assert asyncio.run(internal_guard(_FakeReq("10.0.0.1", token))) is None


def test_internal_guard_untrusted_ip_is_403_before_token_check():
    """미신뢰 IP 는 토큰과 무관하게 CIDR 단계에서 403 이어야 한다."""
    token = settings.INTERNAL_SERVICE_TOKEN.get_secret_value()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(internal_guard(_FakeReq("8.8.8.8", token)))
    assert ei.value.status_code == 403
