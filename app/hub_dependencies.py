"""요청 경로에서 쓰는 외부 클라이언트/캐시 싱글톤 접근자.

lifespan 이 startup 에 인스턴스를 만들어 본 모듈의 슬롯에 주입하고,
라우터가 get_* 로 꺼내 쓴다. 키가 없어 스텁으로 동작할 때 카카오
클라이언트 슬롯은 None 으로 남을 수 있으며, 그 판단은 라우터가 한다.
"""
from __future__ import annotations

from app.cache.hub_cache import RedisCache
from app.clients.hub_clients import (
    KakaoLocalClient,
    NaverBlogClient,
    OsrmClient,
)

# 프로세스 단위 슬롯. lifespan 이 채우고 라우터가 읽는다.
_kakao: KakaoLocalClient | None = None
_naver: NaverBlogClient | None = None
_cache: RedisCache | None = None
# 프로파일별 OSRM 클라이언트. base URL 미설정(스텁 모드)이면 None 으로 남고,
# 그 판단(스텁 사용)은 라우터가 한다.
_osrm_foot: OsrmClient | None = None
_osrm_bicycle: OsrmClient | None = None


def set_place_clients(
    kakao: KakaoLocalClient | None, cache: RedisCache | None
) -> None:
    """lifespan startup 에서 카카오 클라이언트와 캐시를 주입한다."""
    global _kakao, _cache
    _kakao = kakao
    _cache = cache


def set_naver_client(naver: NaverBlogClient | None) -> None:
    """lifespan startup 에서 네이버 블로그 클라이언트를 주입한다.

    set_place_clients 와 분리해 둔다(카카오 시그니처 보존). 키가 없어
    스텁으로 동작할 때는 None 이 주입되며, 그 판단은 라우터가 한다.
    """
    global _naver
    _naver = naver


def get_kakao_client() -> KakaoLocalClient | None:
    """주입된 카카오 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _kakao


def get_naver_client() -> NaverBlogClient | None:
    """주입된 네이버 블로그 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _naver


def get_place_cache() -> RedisCache | None:
    """주입된 L1 캐시를 돌려준다. 미설정이면 None."""
    return _cache


def set_osrm_clients(
    foot: OsrmClient | None, bicycle: OsrmClient | None
) -> None:
    """lifespan startup 에서 프로파일별 OSRM 클라이언트를 주입한다.

    base URL 이 없어 스텁으로 동작하는 프로파일은 None 이 주입되며, 그
    판단(스텁 사용)은 라우터가 한다.
    """
    global _osrm_foot, _osrm_bicycle
    _osrm_foot = foot
    _osrm_bicycle = bicycle


def get_osrm_client(mode: str) -> OsrmClient | None:
    """이동수단에 맞는 OSRM 클라이언트를 돌려준다. 스텁 모드면 None.

    walk→foot, bicycle/scooter→bicycle. 그 외(bus 등)는 None.
    """
    if mode == "walk":
        return _osrm_foot
    if mode in ("bicycle", "scooter"):
        return _osrm_bicycle
    return None


def clear_place_clients() -> None:
    """lifespan shutdown 에서 슬롯을 비운다."""
    global _kakao, _naver, _cache, _osrm_foot, _osrm_bicycle
    _kakao = None
    _naver = None
    _cache = None
    _osrm_foot = None
    _osrm_bicycle = None
