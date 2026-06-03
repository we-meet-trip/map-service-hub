# 데이터베이스 접근 레이어를 본 모듈에 정의한다.
# SQLAlchemy async engine과 PostGIS 공간 모델을 구성하여 외부 데이터의
# 영속 저장과 공간 질의를 제공한다.
#
# 본 모듈이 제공하는 것:
#   - HubDB 클래스         : 엔진/세션 팩토리/세션 컨텍스트
#   - get_hub_db()         : 프로세스 단위 싱글톤 접근자
#   - dispose_hub_db()     : 앱 종료 시 자원 반환
#
# 호출 관계:
#   - app.db.forecast_repo 의 모든 쿼리 함수가 get_hub_db().session() 를 사용
#   - app.main 의 lifespan 이 종료 시 dispose_hub_db() 호출
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

    필드:
      engine: 비동기 엔진. 커넥션 풀(5+5, pre_ping)을 가진다.
      session_factory: 세션 생성기. expire_on_commit=False 로
        commit 후에도 ORM 객체 속성 접근이 가능하도록 설정.
    """

    def __init__(self, dsn: str) -> None:
        """엔진과 세션 팩토리 초기화.

        dsn: SQLAlchemy 비동기 DSN 문자열 (settings.HUB_DATABASE_URL).
        풀 옵션:
          pool_size=5 / max_overflow=5  — 동시 커넥션 상한
          pool_pre_ping=True            — 사용 직전 ping 으로 끊긴 커넥션 감지
          future=True                   — 2.x 스타일 사용 명시
        """
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
        """단일 트랜잭션 경계 세션 컨텍스트.

        사용 예:
          async with get_hub_db().session() as s:
              await s.execute(...)

        동작:
          - 새 AsyncSession 생성
          - s.begin() 으로 트랜잭션 시작
          - 블록 종료 시 정상이면 commit, 예외 발생 시 rollback (s.begin 책임)
          - 세션 close 는 외부 컨텍스트 매니저가 보장
        """
        async with self.session_factory() as s:
            async with s.begin():
                yield s

    async def dispose(self) -> None:
        """엔진과 그 풀을 비동기로 정리한다. 앱 종료 시 1회 호출."""
        await self.engine.dispose()


# 프로세스 단위 HubDB 싱글톤. get_hub_db / dispose_hub_db 에서만 접근.
_instance: HubDB | None = None


def get_hub_db() -> HubDB:
    """HubDB 싱글톤 접근자.

    최초 호출 시점에 settings.HUB_DATABASE_URL 로 인스턴스를 생성하고
    이후 호출에서는 동일 인스턴스를 반환한다.

    호출처: app.db.forecast_repo 의 모든 함수.
    """
    global _instance
    if _instance is None:
        _instance = HubDB(settings.HUB_DATABASE_URL)
    return _instance


async def dispose_hub_db() -> None:
    """HubDB 싱글톤을 정리하고 _instance 를 None 으로 되돌린다.

    인스턴스가 아직 생성되지 않았다면 아무 일도 하지 않는다.
    호출처: app.main.lifespan 의 shutdown 단계.
    """
    global _instance
    if _instance is not None:
        await _instance.dispose()
        _instance = None
