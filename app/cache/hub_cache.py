# 외부 데이터 조회용 cascade 캐시를 본 모듈에 정의한다.
# L1(Redis) → L2(PostgreSQL) → L3(외부 API) 순서로 폴백한다.
#
# 현 시점에서는 클래스 골격(pass-body)만 존재하며, 실제 메서드
# 구현은 향후 단계에서 채워질 자리표시자다. 본 파일을 임포트하는
# 코드가 RedisCache / L2Cache 의 클래스 식별자에만 의존할 수 있도록
# 미리 noop 형태로 선언해 둔 상태이다.


class RedisCache:
    """L1 인메모리 캐시 어댑터.

    redis.asyncio 기반으로 키 단위 get/set/expire 인터페이스를 제공한다.

    구현 예정 책임:
      - Redis 연결/끊김 처리
      - 키 네임스페이스 규칙 (서비스명:리소스:식별자)
      - TTL 단위 만료 / refresh
      - L2 로의 miss 위임

    호출 관계: 아직 호출자 없음(스켈레톤).
    """
    pass


class L2Cache:
    """L2 영속 캐시 어댑터.

    SQLAlchemy async 기반으로 외부 API 응답을 테이블에 적재·조회한다.

    구현 예정 책임:
      - 외부 API 응답 원문 보관
      - expires_at 기반 만료 판정
      - L1 적재 실패/만료 시 fallback 으로 사용
      - L3(외부 API) 재호출 후 자기 자신과 L1 갱신

    호출 관계: 아직 호출자 없음(스켈레톤).
    """
    pass
