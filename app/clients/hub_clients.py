# 외부 API 클라이언트를 본 모듈에 정의한다.
# 모두 httpx.AsyncClient 기반으로 비동기 호출을 캡슐화한다.
#
# 클라이언트 목록:
#   KakaoLocalClient    — 장소 주소/키워드/카테고리 검색 (실 구현)
#   DurunubiClient      — 걷기/자전거 코스 정보 (실 구현)
#   KMAClient           — 기상청 단기/중기 예보 (실 구현)
#   NaverBlogClient     — 블로그 리뷰 텍스트 (실 구현)
#
# 호출 관계:
#   - KMAClient → app.scheduler.hub_scheduler 의 폴링 루프에서 사용
#   - KakaoLocalClient → app.routers.hub_routers 의 장소 조회에서 사용
#   - DurunubiClient → app.scheduler 의 코스 동기화에서 사용
#   - NaverBlogClient → app.routers.hub_routers 의 리뷰 조회에서 사용
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _redact_service_key(text: str) -> str:
    """오류 본문에 섞여 나올 수 있는 serviceKey 값을 가린다.

    data.go.kr 포털 오류 페이지는 요청 URL(인증키 포함)을 본문에 그대로
    echo 하기도 한다. 본 함수로 로그/예외 메시지에 키가 노출되지 않게 한다.
    """
    return re.sub(r"serviceKey=[^&\s\"'<>]+", "serviceKey=***", text)


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


class KakaoApiError(Exception):
    """카카오 로컬 API 호출 실패를 표현하는 예외.

    code: 분류 문자열.
        "HTTP_ERR"           — 전송 실패(연결/타임아웃)
        "HTTP_<status_code>" — 200 이외 응답
    msg: 응답 본문 앞부분(최대 200자) 또는 예외 메시지.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"kakao code={code} msg={msg}")
        self.code = code
        self.msg = msg


class KakaoLocalClient:
    """카카오 로컬 API 호출을 캡슐화하는 클라이언트.

    주소→좌표 변환, 키워드 검색, 카테고리 검색을 제공한다. REST 키는
    모든 요청에 Authorization 헤더로 첨부한다.

    좌표 표기 차이 처리: 카카오 응답의 좌표는 x=경도/y=위도 순서다.
    본 클라이언트가 결과를 돌려줄 때 우리 표현(lat=위도, lng=경도)으로
    바꿔 담으므로, 좌표 교차는 이 한 곳에서만 일어난다.

    제공 endpoint(클래스 변수):
      ADDRESS_EP  — 주소/행정구역 문자열을 좌표로 변환
      KEYWORD_EP  — 키워드로 장소 검색
      CATEGORY_EP — 카테고리 코드로 좌표 주변 장소 검색
    """

    HOST = "https://dapi.kakao.com"
    ADDRESS_EP = "/v2/local/search/address.json"
    KEYWORD_EP = "/v2/local/search/keyword.json"
    CATEGORY_EP = "/v2/local/search/category.json"

    def __init__(
        self,
        rest_api_key: str,
        timeout: float = settings.KAKAO_REQUEST_TIMEOUT_SEC,
    ) -> None:
        """카카오 클라이언트 초기화.

        rest_api_key: 카카오 REST 키(평문). Authorization 헤더로 쓰인다.
        timeout: 단일 HTTP 요청 타임아웃(초).
        """
        self._client = httpx.AsyncClient(
            base_url=self.HOST,
            headers={"Authorization": f"KakaoAK {rest_api_key}"},
            timeout=timeout,
        )

    async def __aenter__(self) -> "KakaoLocalClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자의 명시적 close 용."""
        await self._client.aclose()

    async def _get_json(self, path: str, params: dict) -> dict:
        """공통 GET 헬퍼.

        전송 실패는 KakaoApiError("HTTP_ERR"), 200 이외 응답은
        KakaoApiError("HTTP_<status>") 로 변환한다. 성공 시 JSON dict 반환.
        """
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise KakaoApiError("HTTP_ERR", str(e)) from e
        if r.status_code != 200:
            raise KakaoApiError(f"HTTP_{r.status_code}", r.text[:200])
        return r.json()

    async def geocode_address(
        self, query: str
    ) -> tuple[float, float] | None:
        """주소/행정구역 문자열을 대표 좌표로 변환한다.

        query: 검색할 주소 문자열(예: "서울특별시 강남구").
        반환: 첫 결과의 (lat, lng). 결과가 없으면 None.
        """
        data = await self._get_json(self.ADDRESS_EP, {"query": query})
        docs = data.get("documents") or []
        if not docs:
            return None
        first = docs[0]
        return (float(first["y"]), float(first["x"]))

    async def search_keyword(
        self,
        query: str,
        *,
        x: float | None = None,
        y: float | None = None,
        radius: int | None = None,
        page: int = 1,
        size: int | None = None,
        sort: str = "accuracy",
        category_group_code: str | None = None,
    ) -> list[dict]:
        """키워드로 장소를 검색해 정규화된 장소 dict 리스트를 반환한다.

        x/y(경도/위도)와 radius 가 주어지면 그 좌표 주변으로 한정한다.
        size 기본값은 설정의 KAKAO_DEFAULT_SIZE.
        """
        params: dict = {
            "query": query,
            "page": page,
            "size": size or settings.KAKAO_DEFAULT_SIZE,
            "sort": sort,
        }
        if x is not None and y is not None:
            params["x"] = x
            params["y"] = y
            if radius is not None:
                params["radius"] = radius
        if category_group_code:
            params["category_group_code"] = category_group_code
        data = await self._get_json(self.KEYWORD_EP, params)
        return self._normalize_docs(data.get("documents") or [])

    async def search_category(
        self,
        category_group_code: str,
        *,
        x: float,
        y: float,
        radius: int | None = None,
        page: int = 1,
        size: int | None = None,
        sort: str = "distance",
    ) -> list[dict]:
        """카테고리 코드로 좌표 주변 장소를 검색한다.

        카테고리 검색은 좌표(x=경도, y=위도) 기준이 필수다.
        radius 기본값은 설정의 KAKAO_DEFAULT_RADIUS_M.
        """
        params: dict = {
            "category_group_code": category_group_code,
            "x": x,
            "y": y,
            "radius": radius or settings.KAKAO_DEFAULT_RADIUS_M,
            "page": page,
            "size": size or settings.KAKAO_DEFAULT_SIZE,
            "sort": sort,
        }
        data = await self._get_json(self.CATEGORY_EP, params)
        return self._normalize_docs(data.get("documents") or [])

    @classmethod
    def _normalize_docs(cls, docs: list[dict]) -> list[dict]:
        """문서 목록을 정규화하되, 좌표가 없거나 깨진 문서는 건너뛴다.

        한 문서의 비정상이 검색 응답 전체를 깨지 않도록 개별 변환 실패를
        흡수한다.
        """
        out: list[dict] = []
        for d in docs:
            try:
                out.append(cls._normalize(d))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _normalize(doc: dict) -> dict:
        """카카오 문서 한 건을 공용 장소 표현으로 바꾼다.

        좌표 x=경도/y=위도를 lat/lng 로 교차해 담는다. distance 는
        좌표 검색 시에만 채워지며 문자열이라 정수로 변환한다.
        """
        distance = doc.get("distance")
        return {
            "content_id": f"kakao:{doc.get('id', '')}",
            "source": "kakao",
            "name": doc.get("place_name", ""),
            "address": doc.get("address_name", ""),
            "road_address": doc.get("road_address_name") or None,
            "lat": float(doc["y"]),
            "lng": float(doc["x"]),
            "category": doc.get("category_name") or None,
            "category_group_code": doc.get("category_group_code") or None,
            "phone": doc.get("phone") or None,
            "place_url": doc.get("place_url") or None,
            "distance_m": (
                int(distance) if distance not in (None, "") else None
            ),
        }


class DurunubiApiError(Exception):
    """두루누비 코스 API 호출 실패를 표현하는 예외.

    code: 분류 문자열.
        "HTTP_ERR"           — 전송 실패(연결/타임아웃)
        "HTTP_<status_code>" — 200 이외 응답
        "NON_JSON"           — JSON 으로 디코드할 수 없는 응답
        그 외 — 응답 envelope 의 resultCode 원본 문자열
    msg: 응답 본문 앞부분(최대 200자) 또는 resultMsg / 예외 메시지.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"durunubi code={code} msg={msg}")
        self.code = code
        self.msg = msg


class DurunubiClient:
    """두루누비(걷기/자전거 코스) 정보 API 호출을 캡슐화하는 클라이언트.

    길(노선) 목록과 코스 목록을 조회한다. 인증키는 쿼리 파라미터로
    전달하며 응답은 JSON 으로 받는다(기본 응답형식이 XML 이라 _type=json
    을 항상 명시한다). MobileOS / MobileApp 은 필수 파라미터다.

    응답 정규화:
      - envelope 의 resultCode 가 정상이 아니면 예외로 바꾼다. 단,
        데이터 없음 코드는 빈 리스트로 처리한다.
      - items 는 단건일 때 dict, 다건일 때 list 로 오므로 항상 list 로
        통일한다.

    좌표 주의: 코스 응답에는 좌표가 들어 있지 않고 gpxpath(GPX 파일 URL)만
    제공된다. 대표 좌표는 app.utils.gpx 가 GPX 를 내려받아 계산한다.

    제공 endpoint(클래스 변수):
      ROUTE_EP  — 길(노선) 목록
      COURSE_EP — 코스 목록
    """

    BASE = "https://apis.data.go.kr/B551011/Durunubi"
    ROUTE_EP = "/routeList"
    COURSE_EP = "/courseList"
    # 필수 공통 파라미터. OS 구분과 호출 앱명을 식별값으로 보낸다.
    MOBILE_OS = "ETC"
    MOBILE_APP = "map-service"

    def __init__(
        self,
        service_key: str,
        timeout: float = settings.DURUNUBI_REQUEST_TIMEOUT_SEC,
    ) -> None:
        """두루누비 클라이언트 초기화.

        service_key: data.go.kr 인증키. 쿼리 파라미터 serviceKey 로
            첨부된다. httpx 가 파라미터를 URL 인코딩하므로 디코딩된
            원본 키를 넘겨야 이중 인코딩을 피한다.
        timeout: 단일 HTTP 요청 타임아웃(초).
        """
        self._key = service_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "DurunubiClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자의 명시적 close 용."""
        await self._client.aclose()

    async def _get_json(self, url: str, params: dict) -> dict:
        """공통 GET 헬퍼.

        필수 공통 파라미터(serviceKey/MobileOS/MobileApp/_type)를 채워
        호출하고, 전송 실패·비200·비JSON 응답을 DurunubiApiError 로
        변환한다.
        """
        full = {
            "serviceKey": self._key,
            "MobileOS": self.MOBILE_OS,
            "MobileApp": self.MOBILE_APP,
            "_type": "json",
            **params,
        }
        try:
            r = await self._client.get(url, params=full)
        except httpx.HTTPError as e:
            raise DurunubiApiError("HTTP_ERR", str(e)) from e
        if r.status_code != 200:
            raise DurunubiApiError(
                f"HTTP_{r.status_code}", _redact_service_key(r.text)[:200]
            )
        try:
            return r.json()
        except ValueError as e:
            # 인증/포털 오류는 _type=json 이어도 XML 로 내려올 수 있다.
            raise DurunubiApiError(
                "NON_JSON", _redact_service_key(r.text)[:200]
            ) from e

    @staticmethod
    def _items(data: dict) -> list[dict]:
        """응답 envelope 검증 + items 정규화.

        resultCode 가 정상이면 item 목록을, 데이터 없음 코드면 빈
        리스트를 반환한다. 그 외 코드는 DurunubiApiError 로 변환한다.
        item 이 단건 dict 인 경우도 list 로 통일한다.
        """
        resp = data.get("response") or {}
        header = resp.get("header") or {}
        code = header.get("resultCode")
        if code not in ("00", "0000"):
            if code in ("03", "0003"):
                return []
            raise DurunubiApiError(
                str(code), str(header.get("resultMsg", ""))
            )
        body = resp.get("body") or {}
        items = body.get("items")
        if not items:
            return []
        item = items.get("item") if isinstance(items, dict) else items
        if item is None:
            return []
        if isinstance(item, dict):
            return [item]
        return list(item)

    async def fetch_routes(
        self,
        *,
        num_rows: int | None = None,
        page_no: int = 1,
        brd_div: str | None = None,
        theme_nm: str | None = None,
    ) -> list[dict]:
        """길(노선) 목록 한 페이지를 조회한다.

        brd_div 는 걷기("DNWW")/자전거("DNBW") 구분 필터, theme_nm 은
        노선명 검색어다. 둘 다 선택값이다.
        """
        params: dict = {
            "numOfRows": num_rows or settings.DURUNUBI_NUMOFROWS,
            "pageNo": page_no,
        }
        if brd_div:
            params["brdDiv"] = brd_div
        if theme_nm:
            params["themeNm"] = theme_nm
        data = await self._get_json(self.BASE + self.ROUTE_EP, params)
        return self._items(data)

    async def fetch_courses(
        self,
        *,
        num_rows: int | None = None,
        page_no: int = 1,
        brd_div: str | None = None,
        crs_kor_nm: str | None = None,
        crs_level: str | None = None,
        route_idx: str | None = None,
    ) -> list[dict]:
        """코스 목록 한 페이지를 조회한다.

        brd_div(걷기/자전거), crs_kor_nm(코스명), crs_level(난이도 1/2/3),
        route_idx(소속 노선) 으로 필터링할 수 있다. 모두 선택값이다.
        """
        params: dict = {
            "numOfRows": num_rows or settings.DURUNUBI_NUMOFROWS,
            "pageNo": page_no,
        }
        if brd_div:
            params["brdDiv"] = brd_div
        if crs_kor_nm:
            params["crsKorNm"] = crs_kor_nm
        if crs_level:
            params["crsLevel"] = crs_level
        if route_idx:
            params["routeIdx"] = route_idx
        data = await self._get_json(self.BASE + self.COURSE_EP, params)
        return self._items(data)


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


class NaverApiError(Exception):
    """네이버 검색 API 호출 실패를 표현하는 예외.

    code: 분류 문자열.
        "HTTP_ERR"           — 전송 실패(연결/타임아웃)
        "HTTP_<status_code>" — 200 이외 응답
        "NON_JSON"           — 200 이지만 본문이 JSON 이 아님(점검/프록시 페이지)
    msg: 응답 본문 앞부분(최대 200자) 또는 예외 메시지.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"naver code={code} msg={msg}")
        self.code = code
        self.msg = msg


class NaverBlogClient:
    """Naver Blog 검색 API 호출을 캡슐화하는 클라이언트.

    장소 보강을 위한 블로그 리뷰 텍스트를 조회한다. 인증 자격증명(클라이언트
    ID/시크릿)은 모든 요청에 헤더(X-Naver-Client-Id / X-Naver-Client-Secret)로
    첨부한다 — 시크릿은 헤더로만 이동하며 URL 쿼리에는 실리지 않으므로 별도의
    쿼리 키 마스킹(_redact_service_key)이 필요 없다.

    응답 정규화: 네이버는 title/description 에 검색어 강조용 <b></b> 마크업과
    HTML 엔티티(&lt; &gt; &amp; &quot;)를 섞어 내려주므로, 결과를 돌려주기 전에
    _normalize_items 가 이를 걷어내 순수 텍스트로 만든다.

    제공 endpoint(클래스 변수):
      BLOG_EP — 블로그 검색
    """

    HOST = "https://openapi.naver.com"
    BLOG_EP = "/v1/search/blog"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        timeout: float = settings.NAVER_BLOG_TIMEOUT_SEC,
    ) -> None:
        """네이버 블로그 클라이언트 초기화.

        client_id / client_secret: 네이버 개발자센터 애플리케이션 자격증명
            (평문). 각각 X-Naver-Client-Id / X-Naver-Client-Secret 헤더로
            쓰인다. 시크릿은 헤더로만 전달되어 URL 에 노출되지 않는다.
        timeout: 단일 HTTP 요청 타임아웃(초).
        """
        self._client = httpx.AsyncClient(
            base_url=self.HOST,
            headers={
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
            },
            timeout=timeout,
        )

    async def __aenter__(self) -> "NaverBlogClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자의 명시적 close 용."""
        await self._client.aclose()

    async def _get_json(self, path: str, params: dict) -> dict:
        """공통 GET 헬퍼.

        전송 실패는 NaverApiError("HTTP_ERR"), 200 이외 응답은
        NaverApiError("HTTP_<status>") 로 변환한다. 성공 시 JSON dict 반환.
        """
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise NaverApiError("HTTP_ERR", str(e)) from e
        if r.status_code != 200:
            raise NaverApiError(f"HTTP_{r.status_code}", r.text[:200])
        try:
            return r.json()
        except ValueError as e:
            # 프록시·점검 페이지 등은 200 이어도 비-JSON(HTML)을 내려줄 수 있다.
            # 이를 NaverApiError 로 변환해 라우터의 degrade(빈 리스트) 경로에
            # 태운다 — /v1/reviews 는 5xx 를 내지 않는다는 계약 유지.
            raise NaverApiError("NON_JSON", r.text[:200]) from e

    async def search_blog(
        self,
        query: str,
        *,
        display: int = settings.NAVER_BLOG_DEFAULT_DISPLAY,
        start: int = 1,
        sort: str = "sim",
    ) -> list[dict]:
        """블로그를 검색해 정규화된 리뷰 dict 리스트를 반환한다.

        query: 검색어.
        display: 반환 건수(네이버 상한 100). 기본은 설정값.
        start: 검색 시작 위치(1-base).
        sort: "sim"(정확도) 또는 "date"(최신순).

        반환: {title, description, bloggername, postdate, link} dict 리스트.
        """
        params = {
            "query": query,
            "display": display,
            "start": start,
            "sort": sort,
        }
        data = await self._get_json(self.BLOG_EP, params)
        return self._normalize_items(data.get("items") or [])

    @staticmethod
    def _clean(value: str) -> str:
        """네이버 텍스트의 <b></b> 마크업과 HTML 엔티티를 걷어낸다.

        &amp; 는 이중 디코딩(예: "&amp;lt;" → "<")을 피하려고 맨 마지막에
        치환한다.
        """
        return (
            value.replace("<b>", "")
            .replace("</b>", "")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&amp;", "&")
        )

    @classmethod
    def _normalize_items(cls, items: list[dict]) -> list[dict]:
        """블로그 검색 item 목록을 리뷰 표현으로 정규화한다.

        한 item 의 비정상이 응답 전체를 깨지 않도록 개별 변환 실패는
        흡수한다. title/description 은 _clean 으로 마크업/엔티티를 제거한다.
        """
        out: list[dict] = []
        for it in items:
            try:
                out.append(
                    {
                        "title": cls._clean(it.get("title") or ""),
                        "description": cls._clean(
                            it.get("description") or ""
                        ),
                        "bloggername": it.get("bloggername") or None,
                        "postdate": it.get("postdate") or None,
                        "link": it.get("link") or None,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out
