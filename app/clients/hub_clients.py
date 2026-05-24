# 외부 API 클라이언트 6종을 본 모듈에 정의한다.
# 모두 httpx.AsyncClient 기반으로 비동기 호출을 캡슐화한다.
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class KMAApiError(Exception):
    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"KMA resultCode={code} msg={msg}")
        self.code = code
        self.msg = msg


class KakaoLocalClient:
    """Kakao Local Search API 호출을 캡슐화하는 클라이언트.

    장소 키워드 검색과 좌표 기반 검색을 제공한다.
    """
    pass


class KakaoMobilityClient:
    """Kakao Mobility API 호출을 캡슐화하는 클라이언트.

    자동차 경로 탐색을 제공한다.
    """
    pass


class TourAPIClient:
    """TourAPI KorService 호출을 캡슐화하는 클라이언트.

    관광 정보(장소 메타데이터·이미지·상세 설명)를 조회한다.
    """
    pass


class KMAClient:
    """기상청(KMA) 단기·중기 예보 API 호출을 캡슐화하는 클라이언트.

    좌표·발표 시각 기반으로 강수·기온 등 예보를 조회한다.
    """

    SHORT_EP = (
        "https://apis.data.go.kr/1360000/"
        "VilageFcstInfoService_2.0/getVilageFcst"
    )
    LAND_EP = (
        "https://apis.data.go.kr/1360000/"
        "MidFcstInfoService/getMidLandFcst"
    )
    TEMP_EP = (
        "https://apis.data.go.kr/1360000/MidFcstInfoService/getMidTa"
    )

    def __init__(
        self,
        service_key: str,
        timeout: float = settings.KMA_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._key = service_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "KMAClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(
        self, url: str, params: dict[str, str | int]
    ) -> dict:
        try:
            r = await self._client.get(url, params=params)
        except httpx.HTTPError as e:
            raise KMAApiError("HTTP_ERR", str(e)) from e
        if r.status_code == 429:
            await asyncio.sleep(settings.KMA_RATE_LIMIT_SLEEP_SEC)
            r = await self._client.get(url, params=params)
        if r.status_code != 200:
            raise KMAApiError(
                f"HTTP_{r.status_code}", r.text[:200]
            )
        return r.json()

    @staticmethod
    def _check(data: dict) -> dict:
        header = data.get("response", {}).get("header", {})
        code = header.get("resultCode")
        if code != "00":
            raise KMAApiError(
                str(code), str(header.get("resultMsg", ""))
            )
        body = data.get("response", {}).get("body", {})
        items = body.get("items", {}).get("item", [])
        if isinstance(items, dict):
            items = [items]
        return {"header": header, "body": body, "items": items}

    async def fetch_short_term(
        self, nx: int, ny: int, base_date: str, base_time: str
    ) -> list[dict]:
        params = {
            "serviceKey": self._key,
            "pageNo": 1,
            "numOfRows": settings.KMA_NUMOFROWS,
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": base_time,
            "nx": nx,
            "ny": ny,
        }
        data = await self._get_json(self.SHORT_EP, params)
        return self._check(data)["items"]

    async def fetch_mid_land(
        self, reg_id: str, tm_fc: str
    ) -> dict:
        params = {
            "serviceKey": self._key,
            "pageNo": 1,
            "numOfRows": 10,
            "dataType": "JSON",
            "regId": reg_id,
            "tmFc": tm_fc,
        }
        data = await self._get_json(self.LAND_EP, params)
        items = self._check(data)["items"]
        if not items:
            raise KMAApiError("EMPTY_ITEMS", "mid_land response empty")
        return items[0]

    async def fetch_mid_temp(
        self, reg_id: str, tm_fc: str
    ) -> dict:
        params = {
            "serviceKey": self._key,
            "pageNo": 1,
            "numOfRows": 10,
            "dataType": "JSON",
            "regId": reg_id,
            "tmFc": tm_fc,
        }
        data = await self._get_json(self.TEMP_EP, params)
        items = self._check(data)["items"]
        if not items:
            raise KMAApiError("EMPTY_ITEMS", "mid_temp response empty")
        return items[0]


class NaverBlogClient:
    """Naver Blog 검색 API 호출을 캡슐화하는 클라이언트.

    장소 보강을 위한 블로그 텍스트를 조회한다.
    """
    pass


class OSRMClient:
    """OSRM 라우팅 엔진 호출을 캡슐화하는 클라이언트.

    foot·bicycle 프로파일에 대해 경로·소요 시간을 조회한다.
    """
    pass
