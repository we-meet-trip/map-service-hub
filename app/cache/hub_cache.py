# 외부 데이터 조회용 cascade 캐시를 본 모듈에 정의한다.
# L1(Redis) → L2(PostgreSQL) → L3(외부 API) 순서로 폴백한다.


class RedisCache:
    """L1 인메모리 캐시 어댑터.

    redis.asyncio 기반으로 키 단위 get/set/expire 인터페이스를 제공한다.
    """
    pass


class L2Cache:
    """L2 영속 캐시 어댑터.

    SQLAlchemy async 기반으로 외부 API 응답을 테이블에 적재·조회한다.
    """
    pass
