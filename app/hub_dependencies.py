"""요청 경로에서 쓰는 외부 클라이언트/캐시 싱글톤 접근자.

lifespan 이 startup 에 인스턴스를 만들어 본 모듈의 슬롯에 주입하고,
라우터가 get_* 로 꺼내 쓴다. 키가 없어 스텁으로 동작할 때 카카오
클라이언트 슬롯은 None 으로 남을 수 있으며, 그 판단은 라우터가 한다.
"""
from __future__ import annotations

from app.cache.hub_cache import RedisCache
from app.clients.hub_clients import KakaoLocalClient

# 프로세스 단위 슬롯. lifespan 이 채우고 라우터가 읽는다.
_kakao: KakaoLocalClient | None = None
_cache: RedisCache | None = None


def set_place_clients(
    kakao: KakaoLocalClient | None, cache: RedisCache | None
) -> None:
    """lifespan startup 에서 카카오 클라이언트와 캐시를 주입한다."""
    global _kakao, _cache
    _kakao = kakao
    _cache = cache


def get_kakao_client() -> KakaoLocalClient | None:
    """주입된 카카오 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _kakao


def get_place_cache() -> RedisCache | None:
    """주입된 L1 캐시를 돌려준다. 미설정이면 None."""
    return _cache


def clear_place_clients() -> None:
    """lifespan shutdown 에서 슬롯을 비운다."""
    global _kakao, _cache
    _kakao = None
    _cache = None
