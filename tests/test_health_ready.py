"""준비 확인(/health/ready) 테스트.

살아 있음(/health)과 준비됨(/health/ready)을 갈라 두는 것이 요점이다.
저장소가 끊겼을 때 준비 확인만 503 이 되고 살아 있음은 계속 200 이어야
한다. 둘이 같아지면 저장소가 끊긴 상태가 정상으로 보이거나, 반대로 잠깐의
저장소 장애가 과정을 다시 띄우는 신호가 된다.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import app.main as main


class _FakeCache:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    async def ping(self) -> bool:
        return self._alive


class _FakeDb:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    @asynccontextmanager
    async def session(self):
        if not self._alive:
            raise RuntimeError("표에 닿지 못했다")

        class _S:
            async def execute(self, *_a, **_k):
                return None

        yield _S()


def _client(monkeypatch, *, db: bool, cache) -> TestClient:
    monkeypatch.setattr(main, "get_hub_db", lambda: _FakeDb(db))
    monkeypatch.setattr(main, "get_place_cache", lambda: cache)
    return TestClient(main.app)


def test_둘_다_답하면_준비됨(monkeypatch):
    with _client(monkeypatch, db=True, cache=_FakeCache(True)) as c:
        r = c.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["deps"] == {"database": "ok", "cache": "ok"}


def test_캐시가_끊기면_준비되지_않음(monkeypatch):
    with _client(monkeypatch, db=True, cache=_FakeCache(False)) as c:
        r = c.get("/health/ready")
        live = c.get("/health")
    assert r.status_code == 503
    assert r.json()["deps"]["cache"] == "down"
    # 살아 있음은 그대로 200 이어야 한다. 이 둘이 같아지면 구분이 사라진다.
    assert live.status_code == 200


def test_표가_끊기면_준비되지_않음(monkeypatch):
    with _client(monkeypatch, db=False, cache=_FakeCache(True)) as c:
        r = c.get("/health/ready")
        live = c.get("/health")
    assert r.status_code == 503
    assert r.json()["deps"]["database"] == "down"
    assert live.status_code == 200


def test_캐시를_꺼_둔_것은_끊긴_것과_다르다(monkeypatch):
    with _client(monkeypatch, db=True, cache=None) as c:
        r = c.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["deps"]["cache"] == "disabled"
