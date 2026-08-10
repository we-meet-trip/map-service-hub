# 외부 데이터 조회용 캐시를 본 모듈에 정의한다.
# L1(Redis)을 우선 사용한다. L2(PostgreSQL) 자리표시자는 향후 단계용이다.
from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class RedisCache:
    """L1 캐시 어댑터.

    redis.asyncio 기반으로 JSON 값을 키 단위로 get/set 한다. 외부 장소
    검색 결과처럼 짧은 시간만 재사용하면 되는 응답을 TTL 과 함께 담는다.

    키 네임스페이스는 호출자가 "리소스:식별자" 형태로 만들어 넘긴다.
    캐시는 보조 경로이므로 모든 실패는 미스로 흡수해 호출자가 원본
    소스로 진행하도록 한다.
    """

    def __init__(self, redis_url: str, db: int) -> None:
        """캐시 어댑터 초기화.

        redis_url: redis 접속 URL.
        db: 사용할 논리 DB 번호.
        decode_responses=True 로 두어 저장/조회 값이 문자열로 다뤄진다.
        """
        self._client = aioredis.Redis.from_url(
            redis_url, db=db, decode_responses=True
        )

    async def get_json(self, key: str) -> Any | None:
        """키의 JSON 값을 디코드해 돌려준다. 없거나 깨지면 None."""
        try:
            raw = await self._client.get(key)
        except RedisError as e:
            logger.warning("cache get failed key=%s err=%s", key, e)
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def set_json(
        self, key: str, value: Any, ttl_sec: int
    ) -> None:
        """값을 JSON 으로 직렬화해 TTL 과 함께 저장한다.

        저장 실패는 경고만 남기고 무시한다(캐시는 보조 경로).
        """
        try:
            await self._client.set(
                key, json.dumps(value, ensure_ascii=False), ex=ttl_sec
            )
        except RedisError as e:
            logger.warning("cache set failed key=%s err=%s", key, e)

    async def incr_by(
        self, key: str, amount: int, ttl_sec: int
    ) -> int | None:
        """키 값을 amount 만큼 올리고 누적값을 돌려준다.

        키가 없으면 0 에서 시작하며, 이번 호출로 처음 만들어졌을 때만 TTL 을
        건다. 매번 걸면 호출이 이어지는 동안 만료 시각이 계속 뒤로 밀려
        집계 구간이 닫히지 않는다.

        실패는 None 으로 돌려준다 — 세는 일이 실패했을 때 어떻게 할지는
        호출자가 정한다.
        """
        try:
            total = int(await self._client.incrby(key, amount))
            if total == amount:
                await self._client.expire(key, ttl_sec)
            return total
        except RedisError as e:
            logger.warning("cache incr failed key=%s err=%s", key, e)
            return None

    async def aclose(self) -> None:
        """내부 redis 클라이언트를 닫는다. 앱 종료 시 호출."""
        await self._client.aclose()
