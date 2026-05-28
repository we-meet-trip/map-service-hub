# 데이터베이스 접근 레이어를 본 모듈에 정의한다.
# SQLAlchemy async engine과 PostGIS 공간 모델을 구성하여 외부 데이터의
# 영속 저장과 공간 질의를 제공한다.
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


class HubDB:
    """SQLAlchemy async engine과 session factory를 보유하는 어댑터.

    엔진 라이프사이클·세션 컨텍스트·트랜잭션 경계를 캡슐화한다.
    """

    def __init__(self, dsn: str) -> None:
        self.engine = create_async_engine(
            dsn,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            future=True,
        )
        self.session_factory: async_sessionmaker[AsyncSession] = (
            async_sessionmaker(self.engine, expire_on_commit=False)
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as s:
            async with s.begin():
                yield s

    async def dispose(self) -> None:
        await self.engine.dispose()


_instance: HubDB | None = None


def get_hub_db() -> HubDB:
    global _instance
    if _instance is None:
        _instance = HubDB(settings.HUB_DATABASE_URL)
    return _instance


async def dispose_hub_db() -> None:
    global _instance
    if _instance is not None:
        await _instance.dispose()
        _instance = None
