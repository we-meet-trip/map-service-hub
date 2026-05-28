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
    assert _is_trusted(ip) is trusted
