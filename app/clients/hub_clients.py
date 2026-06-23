# 외부 API 클라이언트 6종을 본 모듈에 정의한다.
# 모두 httpx.AsyncClient 기반으로 비동기 호출을 캡슐화한다.
#
# 클라이언트 목록:
#   KakaoLocalClient    — 장소 키워드/좌표 검색 (스켈레톤)
#   KakaoMobilityClient — 자동차 경로 (스켈레톤)
#   TourAPIClient       — 관광 정보 (스켈레톤)
#   KMAClient           — 기상청 단기/중기 예보 (실 구현)
#   NaverBlogClient     — 블로그 텍스트 (스켈레톤)
#   OSRMClient          — 보행/자전거 라우팅 (스켈레톤)
#
# 호출 관계:
#   - KMAClient → app.scheduler.hub_scheduler 의 폴링 루프에서 사용
#   - 그 외 5개는 현재 호출자 없음(자리표시자)
from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class KMAApiError(Exception):
    """KMA API 호출 실패를 표현하는 예외.

    KMA 응답 본문의 resultCode 가 정상값("00")이 아니거나, HTTP 레벨
    에러(타임아웃/4xx/5xx/네트워크 실패)를 한 가지 예외 타입으로 통합한다.

    code: 분류 문자열.
        "HTTP_ERR"             — httpx 가 던진 전송 실패(연결/타임아웃)
        "HTTP_<status_code>"   — 200 이외 응답 (예: "HTTP_500")
        "EMPTY_ITEMS"          — 정상 응답이지만 items 배열이 비어 있음
        그 외 — KMA 응답 header.resultCode 의 원본 문자열
    msg: 사람이 읽을 수 있는 부가 메시지. KMA 의 resultMsg 또는 응답 본문
        앞부분(최대 200자) / 예외 메시지.

    호출처: KMAClient 내부의 _get_json / _check / fetch_* 메서드에서 발생.
    포착처: hub_scheduler 의 폴링 루프가 try/except 로 받아 warning 로그
        만 남기고 다음 grid 로 진행한다(전체 루프는 계속 돌도록).
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"KMA resultCode={code} msg={msg}")
        self.code = code
        self.msg = msg


class KakaoLocalClient:
    """Kakao Local Search API 호출을 캡슐화하는 클라이언트.

    장소 키워드 검색과 좌표 기반 검색을 제공한다.

    현 시점에서는 본문이 비어 있는 자리표시자(skeleton)이며,
    실제 메서드는 향후 단계에서 채워진다. 호출자 없음.
    """
    pass


class KakaoMobilityClient:
    """Kakao Mobility API 호출을 캡슐화하는 클라이언트.

    자동차 경로 탐색을 제공한다. 현재는 스켈레톤. 호출자 없음.
    """
    pass


class TourAPIClient:
    """TourAPI KorService 호출을 캡슐화하는 클라이언트.

    관광 정보(장소 메타데이터·이미지·상세 설명)를 조회한다.
    현재는 스켈레톤. 호출자 없음.
    """
    pass


class KMAClient:
    """기상청(KMA) 단기·중기 예보 API 호출을 캡슐화하는 클라이언트.

    좌표·발표 시각 기반으로 강수·기온 등 예보를 조회한다.

    인스턴스 단위 책임:
      - 하나의 httpx.AsyncClient 를 소유하고, 비동기 컨텍스트(`async with`)
        종료 시 자동 close 한다.
      - 모든 호출에 KMA 서비스 키를 자동 첨부한다.
      - HTTP 429 응답은 일정 시간 대기 후 1회 재시도한다.
      - JSON 응답의 resultCode 가 "00" 이 아니면 KMAApiError 로 변환.

    제공 endpoint 3종(클래스 변수):
      SHORT_EP — 단기예보 (마을예보 3시간 단위)
      LAND_EP  — 중기 육상예보 (D+4 ~ D+10 의 날씨/강수확률)
      TEMP_EP  — 중기 기온예보 (D+4 ~ D+10 의 최저/최고 기온)

    호출 관계:
      - app.scheduler.hub_scheduler.short_term_polling_loop
        → fetch_short_term
      - app.scheduler.hub_scheduler.mid_term_polling_loop
        → fetch_mid_land / fetch_mid_temp
    """

    # KMA 단기예보 endpoint. fetch_short_term 이 사용.
    SHORT_EP = (
        "https://apis.data.go.kr/1360000/"
        "VilageFcstInfoService_2.0/getVilageFcst"
    )
    # KMA 중기 육상예보 endpoint. fetch_mid_land 가 사용.
    LAND_EP = (
        "https://apis.data.go.kr/1360000/"
        "MidFcstInfoService/getMidLandFcst"
    )
    # KMA 중기 기온예보 endpoint. fetch_mid_temp 가 사용.
    TEMP_EP = (
        "https://apis.data.go.kr/1360000/MidFcstInfoService/getMidTa"
    )

    def __init__(
        self,
        service_key: str,
        timeout: float = settings.KMA_REQUEST_TIMEOUT_SEC,
    ) -> None:
        """KMA 클라이언트 초기화.

        service_key: KMA 서비스 키. 일반적으로 settings.KMA_SERVICE_KEY.
        timeout: 단일 HTTP 요청의 타임아웃(초). 기본은 settings 값.

        내부 상태:
          _key: 모든 요청의 serviceKey 파라미터로 첨부됨.
          _client: 본 인스턴스 수명 동안 재사용되는 httpx.AsyncClient.
        """
        self._key = service_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "KMAClient":
        """`async with KMAClient(...) as kma` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 블록 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자가 명시적으로 close 할 때 사용."""
        await self._client.aclose()

    async def _get_json(
        self, url: str, params: dict[str, str | int]
    ) -> dict:
        """공통 GET 헬퍼.

        url: KMA endpoint 절대 URL.
        params: 쿼리 파라미터 dict. serviceKey 는 호출자가 미리 채워 둔다.

        동작:
          1) httpx 로 GET. 전송 단계 실패는 KMAApiError("HTTP_ERR", ...) 로 변환.
          2) 응답이 429 면 settings.KMA_RATE_LIMIT_SLEEP_SEC 만큼 대기 후 1회 재시도.
          3) 200 이 아니면 KMAApiError("HTTP_<status>", body[:200]) 로 변환.
          4) 성공 시 JSON 디코드 결과 dict 반환.

        반환: 디코드된 KMA 응답 본문(dict).
        """
        try:
            r = await self._client.get(url, params=params)
            # 429 재시도 GET 도 같은 try 안에 둔다. 재시도 중 발생하는
            # 네트워크/타임아웃(httpx.HTTPError)이 raw 로 누출되지 않고
            # KMAApiError("HTTP_ERR") 로 일관 변환되도록 한다.
            if r.status_code == 429:
                await asyncio.sleep(settings.KMA_RATE_LIMIT_SLEEP_SEC)
                r = await self._client.get(url, params=params)
        except httpx.HTTPError as e:
            raise KMAApiError("HTTP_ERR", str(e)) from e
        if r.status_code != 200:
            raise KMAApiError(
                f"HTTP_{r.status_code}", r.text[:200]
            )
        return r.json()

    @staticmethod
    def _check(data: dict) -> dict:
        """KMA 응답 envelope 검증 + items 정규화.

        data: _get_json 이 반환한 응답 본문.

        검증:
          response.header.resultCode 가 "00" 이 아니면 KMAApiError 로 변환.
        정규화:
          items 가 단일 dict 인 경우(KMA 가 1건 응답 시 dict 로 보냄)도
          list[dict] 로 통일.

        반환: {"header": ..., "body": ..., "items": list[dict]}
        호출처: fetch_short_term / fetch_mid_land / fetch_mid_temp.
        """
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
        """단기예보 raw item 목록 조회.

        nx / ny: KMA 격자 좌표 (gps_to_grid 변환 결과).
        base_date: 발표 일자 "YYYYMMDD" (resolve_short_term_base 결과).
        base_time: 발표 시각 "HHMM"   (resolve_short_term_base 결과).

        page 크기는 settings.KMA_NUMOFROWS 로 충분히 크게 잡아 한 번에
        모든 (시간 × 카테고리) row 를 받는다.

        반환: KMA item 의 list[dict] (각 row 는 fcstDate/fcstTime/
            category/fcstValue 등 KMA 원본 키를 그대로 가진다).
        호출처: hub_scheduler.short_term_polling_loop.
        """
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
        """중기 육상예보 단일 payload 조회.

        reg_id: 중기 육상예보 지역코드(예: subscribed_grids.mid_land_reg_id).
        tm_fc: 발표 시각 "YYYYMMDDHHMM" (resolve_mid_tm_fc 결과).

        중기예보는 한 (reg_id, tm_fc) 당 1건만 존재하므로 numOfRows=10
        은 안전 마진. items 가 비어 있으면 EMPTY_ITEMS 로 KMAApiError.

        반환: 첫 번째(유일한) item dict — wf4Am/rnSt4Am 등 KMA 원본 키.
        호출처: hub_scheduler.mid_term_polling_loop.
        """
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
        """중기 기온예보 단일 payload 조회.

        reg_id: 중기 기온예보 지역코드(예: subscribed_grids.mid_temp_reg_id).
        tm_fc: 발표 시각 "YYYYMMDDHHMM".

        의미상 fetch_mid_land 와 짝을 이루지만 지역코드 체계와 endpoint 가
        다르므로 별도 메서드로 분리되어 있다.

        반환: 첫 번째(유일한) item dict — taMin4/taMax4/... 등 KMA 원본 키.
        호출처: hub_scheduler.mid_term_polling_loop.
        """
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
    현재는 스켈레톤. 호출자 없음.
    """
    pass


class OSRMClient:
    """OSRM 라우팅 엔진 호출을 캡슐화하는 클라이언트.

    foot·bicycle 프로파일에 대해 경로·소요 시간을 조회한다.
    현재는 스켈레톤. 호출자 없음.

    인프라 측에서 docker-compose 의 osrm-foot(호스트 5000) /
    osrm-bicycle(호스트 5001) 컨테이너가 본 클라이언트의 호출 대상.
    """
    pass
