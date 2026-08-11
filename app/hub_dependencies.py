"""요청 경로에서 쓰는 외부 클라이언트/캐시 싱글톤 접근자.

lifespan 이 startup 에 인스턴스를 만들어 본 모듈의 슬롯에 주입하고,
라우터가 get_* 로 꺼내 쓴다. 키가 없어 스텁으로 동작할 때 카카오
클라이언트 슬롯은 None 으로 남을 수 있으며, 그 판단은 라우터가 한다.
"""
from __future__ import annotations

from app.cache.hub_cache import RedisCache
from app.clients.hub_clients import (
    AirKoreaClient,
    GooglePlacesClient,
    KakaoLocalClient,
    KMAClient,
    NaverBlogClient,
    OdsayClient,
    OsrmClient,
    PmClient,
    SeoulBikeClient,
)

# 프로세스 단위 슬롯. lifespan 이 채우고 라우터가 읽는다.
_kakao: KakaoLocalClient | None = None
_naver: NaverBlogClient | None = None
_google: GooglePlacesClient | None = None
_cache: RedisCache | None = None
# 프로파일별 OSRM 클라이언트. base URL 미설정(스텁 모드)이면 None 으로 남고,
# 그 판단(스텁 사용)은 라우터가 한다.
_osrm_foot: OsrmClient | None = None
_osrm_bicycle: OsrmClient | None = None
# 현재 날씨 조회용. 실황은 폴링이 아니라 요청 때마다 부르므로 스케줄러가
# 쓰는 인스턴스와 별도로 요청 경로용 슬롯을 둔다.
_kma: KMAClient | None = None
_airkorea: AirKoreaClient | None = None
# 지하철 경로/따릉이 대여소/공유 킥보드. 키가 없어 스텁으로 동작하면 None 으로
# 남고, 그 판단은 라우터가 한다.
_odsay: OdsayClient | None = None
# 예비 키로 만든 두 번째 지하철 클라이언트. 예비 키가 없으면 None 이다.
_odsay_fallback: OdsayClient | None = None
_seoul_bike: SeoulBikeClient | None = None
_pm: PmClient | None = None


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


def set_google_client(google: GooglePlacesClient | None) -> None:
    """lifespan startup 에서 Google 장소 클라이언트를 주입한다.

    set_place_clients 와 분리해 둔다(카카오 시그니처 보존). 키가 없어
    스텁으로 동작할 때는 None 이 주입되며, 그 판단은 라우터가 한다.
    """
    global _google
    _google = google


def get_kakao_client() -> KakaoLocalClient | None:
    """주입된 카카오 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _kakao


def get_naver_client() -> NaverBlogClient | None:
    """주입된 네이버 블로그 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _naver


def get_google_client() -> GooglePlacesClient | None:
    """주입된 Google 장소 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _google


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


def set_weather_clients(
    kma: KMAClient | None, airkorea: AirKoreaClient | None
) -> None:
    """lifespan startup 에서 현재 날씨 조회용 클라이언트를 주입한다.

    키가 없어 스텁으로 동작하면 None 이 주입되고, 그 판단은 라우터가 한다.
    """
    global _kma, _airkorea
    _kma = kma
    _airkorea = airkorea


def get_kma_client() -> KMAClient | None:
    """주입된 KMA 클라이언트를 돌려준다. 미설정이면 None."""
    return _kma


def get_airkorea_client() -> AirKoreaClient | None:
    """주입된 대기오염 클라이언트를 돌려준다. 미설정이면 None."""
    return _airkorea


def set_odsay_clients(
    odsay: OdsayClient | None, fallback: OdsayClient | None
) -> None:
    """lifespan startup 에서 지하철 경로 클라이언트를 주입한다.

    키가 없어 스텁으로 동작할 때는 None 이 주입되며, 그 판단은 라우터가 한다.
    예비 키를 채우지 않았으면 fallback 도 None 이다.
    """
    global _odsay, _odsay_fallback
    _odsay = odsay
    _odsay_fallback = fallback


def get_odsay_client() -> OdsayClient | None:
    """주입된 지하철 경로 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _odsay


def get_odsay_fallback_client() -> OdsayClient | None:
    """예비 키로 만든 지하철 경로 클라이언트를 돌려준다. 없으면 None."""
    return _odsay_fallback


def set_pm_client(pm: PmClient | None) -> None:
    """lifespan startup 에서 공유 킥보드 클라이언트를 주입한다.

    키가 없어 스텁으로 동작할 때는 None 이 주입되며, 그 판단은 라우터가 한다.
    """
    global _pm
    _pm = pm


def get_pm_client() -> PmClient | None:
    """주입된 공유 킥보드 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _pm


def set_seoul_bike_client(seoul_bike: SeoulBikeClient | None) -> None:
    """lifespan startup 에서 따릉이 대여소 클라이언트를 주입한다.

    키가 없어 스텁으로 동작할 때는 None 이 주입되며, 그 판단은 라우터가 한다.
    """
    global _seoul_bike
    _seoul_bike = seoul_bike


def get_seoul_bike_client() -> SeoulBikeClient | None:
    """주입된 따릉이 대여소 클라이언트를 돌려준다. 스텁 모드면 None."""
    return _seoul_bike


def clear_place_clients() -> None:
    """lifespan shutdown 에서 슬롯을 비운다."""
    global _kakao, _naver, _google, _cache, _osrm_foot, _osrm_bicycle
    global _kma, _airkorea, _odsay, _odsay_fallback, _seoul_bike, _pm
    _kakao = None
    _naver = None
    _google = None
    _cache = None
    _osrm_foot = None
    _osrm_bicycle = None
    _kma = None
    _airkorea = None
    _odsay = None
    _odsay_fallback = None
    _seoul_bike = None
    _pm = None
