"""internal_router._is_trusted CIDR 화이트리스트 판정 테스트.

settings.HUB_INTERNAL_TRUSTED_CIDRS 기본값
  "172.16.0.0/12, 10.0.0.0/8, 192.168.0.0/16"
에 대해 각 IP 가 신뢰 대역에 들어가는지 검사한다.

127.0.0.1 (loopback) 은 명시적으로 신뢰 대역에 포함되지 않으므로 False 가
정상이다. 잘못된 입력(빈 문자열, 비-IP 문자열)도 False.
"""
from __future__ import annotations

import pytest

from app.routers.internal_router import _is_trusted


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
