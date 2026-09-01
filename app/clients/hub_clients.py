# 외부 API 클라이언트를 본 모듈에 정의한다.
# 모두 httpx.AsyncClient 기반으로 비동기 호출을 캡슐화한다.
#
# 클라이언트 목록:
#   KakaoLocalClient    — 장소 주소/키워드/카테고리 검색 (실 구현)
#   DurunubiClient      — 걷기/자전거 코스 정보 (실 구현)
#   KMAClient           — 기상청 단기/중기 예보 (실 구현)
#   NaverBlogClient     — 블로그 리뷰 텍스트 (실 구현)
#   GooglePlacesClient  — 장소 사진 (실 구현)
#   OdsayClient         — 지하철 경로 (실 구현)
#   SeoulBikeClient     — 따릉이 대여소 현황 (실 구현)
#   PmClient            — 공유 킥보드 위치 (실 구현)
#
# 호출 관계:
#   - KMAClient → app.scheduler.hub_scheduler 의 폴링 루프에서 사용
#   - KakaoLocalClient → app.routers.hub_routers 의 장소 조회에서 사용
#   - DurunubiClient → app.scheduler 의 코스 동기화에서 사용
#   - NaverBlogClient → app.routers.hub_routers 의 리뷰 조회에서 사용
#   - GooglePlacesClient → app.routers.hub_routers 의 사진 조회에서 사용
#   - OdsayClient → app.routers.hub_routers 의 지하철 경로 조회에서 사용
#   - SeoulBikeClient → app.routers.hub_routers 의 대여소 조회에서 사용
#   - PmClient → app.routers.hub_routers 의 킥보드 조회에서 사용
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


def _redact_secret(text: str) -> str:
    """오류 본문·예외 메시지에 섞여 나올 수 있는 인증키를 모두 가린다.

    키가 URL 에 실려 나가는 곳이 세 가지 모양이라 한 함수에서 함께 본다.
      - serviceKey=<값>  data.go.kr 계열
      - apiKey=<값>      ODsay
      - :8088/<값>/json  서울 열린데이터광장. 키가 쿼리가 아니라 경로
                         한 칸을 차지해, 앞의 두 규칙으로는 걸리지 않는다.

    전송 실패 예외(str(e))에는 요청 URL 이 그대로 들어가므로, 본문뿐 아니라
    예외 메시지에도 이 함수를 통과시킨다.
    """
    text = _redact_service_key(text)
    text = re.sub(r"apiKey=[^&\s\"'<>]+", "apiKey=***", text)
    return re.sub(r"(:8088/)[^/\s\"'<>]+(/)", r"\1***\2", text)


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
    """

    HOST = "https://dapi.kakao.com"
    ADDRESS_EP = "/v2/local/search/address.json"
    KEYWORD_EP = "/v2/local/search/keyword.json"

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
      COURSE_EP — 코스 목록
    """

    BASE = "https://apis.data.go.kr/B551011/Durunubi"
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
    # KMA 초단기실황 endpoint. fetch_nowcast 가 사용.
    NOWCAST_EP = (
        "https://apis.data.go.kr/1360000/"
        "VilageFcstInfoService_2.0/getUltraSrtNcst"
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
                f"HTTP_{r.status_code}", _redact_service_key(r.text)[:200]
            )
        try:
            return r.json()
        except ValueError as e:
            # 키 미신청·서비스 점검 시 200 으로 XML/HTML 오류 문서가 온다.
            # 그대로 두면 디코드 오류가 호출 측 degrade 를 지나쳐 500 이 된다.
            raise KMAApiError(
                "NON_JSON", _redact_service_key(r.text)[:200]
            ) from e

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

        빈 목록은 예외로 올린다. 조용히 빈 목록을 돌려주면 호출하는 쪽이
        성공으로 보고 그 격자를 재시도 대상에서 빼기 때문에, 그 발표분
        내내 그 지역만 예보가 비어도 아무 기록이 남지 않는다.

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
        items = self._check(data)["items"]
        if not items:
            raise KMAApiError(
                "EMPTY_ITEMS",
                f"short_term empty grid={nx},{ny} "
                f"base={base_date}{base_time}",
            )
        return items

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

    async def fetch_nowcast(
        self, nx: int, ny: int, base_date: str, base_time: str
    ) -> dict[str, str]:
        """초단기실황 조회 — 지금 관측된 기온·강수형태를 읽는다.

        예보가 아니라 관측값이라 "지금 몇 도"를 물을 수 있는 유일한 경로다.
        단기예보에는 현재 시각의 기온이 없다(3시간 간격 예보값뿐).

        nx / ny: 격자 좌표.
        base_date: 발표 일자 "YYYYMMDD".
        base_time: 발표 시각 "HHMM". 매시 정시 관측이 40분에 공개되므로
            호출 측이 resolve_nowcast_base 로 안전한 시각을 고른다.

        반환: 카테고리 → 관측값 문자열 맵. T1H(기온), PTY(강수형태),
            REH(습도) 등 KMA 원본 카테고리를 그대로 키로 쓴다.
        """
        params = {
            "serviceKey": self._key,
            "pageNo": 1,
            "numOfRows": 60,
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": base_time,
            "nx": nx,
            "ny": ny,
        }
        data = await self._get_json(self.NOWCAST_EP, params)
        items = self._check(data)["items"]
        if not items:
            raise KMAApiError("EMPTY_ITEMS", "nowcast response empty")
        return {
            str(it.get("category")): str(it.get("obsrValue"))
            for it in items
            if isinstance(it, dict) and it.get("category") is not None
        }


class AirKoreaApiError(Exception):
    """대기오염 정보 API 호출 실패를 표현하는 예외.

    전송 실패와 응답 본문의 오류 코드를 한 타입으로 모은다. 호출 측은 이
    예외를 잡아 대기 정보 없이 응답을 이어 간다 — 미세먼지는 부가 정보라
    이것 때문에 날씨 전체가 실패하면 안 된다.

    code: "HTTP_ERR"(전송 실패) / "HTTP_<상태코드>" / "EMPTY_ITEMS" /
        응답이 준 오류 코드 문자열.
    msg: 사람이 읽을 수 있는 부가 메시지.
    """

    def __init__(self, code: str, msg: str = "") -> None:
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


class AirKoreaClient:
    """대기오염 정보 API 호출을 캡슐화하는 클라이언트.

    시도 단위 실시간 측정 정보를 받아 미세먼지·초미세먼지 농도를 얻는다.
    측정소가 여럿이라 응답은 여러 건이며, 호출 측이 대표값을 고른다.

    인스턴스 단위 책임:
      - 하나의 httpx.AsyncClient 를 소유하고 컨텍스트 종료 시 닫는다.
      - 모든 호출에 서비스 키를 자동 첨부한다.
      - 응답 본문이 JSON 이 아니거나 오류 코드를 담고 있으면 예외로 바꾼다.

    호출 관계: hub_routers.get_weather_now → fetch_sido_realtime.
    """

    # 시도별 실시간 측정정보 endpoint.
    SIDO_EP = (
        "https://apis.data.go.kr/B552584/"
        "ArpltnInforInqireSvc/getCtprvnRltmMesureDnsty"
    )

    def __init__(
        self,
        service_key: str,
        timeout: float = settings.AIRKOREA_REQUEST_TIMEOUT_SEC,
    ) -> None:
        """서비스 키와 요청 타임아웃으로 클라이언트를 만든다."""
        self._key = service_key
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "AirKoreaClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자가 명시적으로 닫을 때 사용."""
        await self._client.aclose()

    async def fetch_sido_realtime(self, sido: str) -> list[dict]:
        """시도의 측정소별 실시간 측정 정보를 받는다.

        sido: 대기오염 API 의 시도 축약형(예: "서울"). air_codes.sido_name
            이 정식 명칭에서 변환한 값이 들어온다.

        반환: 측정소 dict 리스트. 각 항목은 stationName / pm10Value /
            pm25Value 등 원본 키를 그대로 가진다.

        키가 미신청 상태면 응답이 JSON 이 아닌 오류 문서로 오는데, 그 경우도
        디코드 실패를 잡아 예외로 바꾼다.

        한 번에 다 받는 것을 전제로 페이지 크기를 넉넉히 잡는다. 목록이
        잘리면 뒤쪽 측정소가 후보에서 통째로 빠져, 요청한 시군구에 측정소가
        있는데도 먼 곳의 농도가 대표로 뽑힌다. 그래도 잘렸다면 로그로 남겨
        페이지 크기를 조정할 근거를 만든다.
        """
        params = {
            "serviceKey": self._key,
            "returnType": "json",
            "numOfRows": settings.AIRKOREA_NUMOFROWS,
            "pageNo": 1,
            "sidoName": sido,
            "ver": "1.0",
        }
        try:
            r = await self._client.get(self.SIDO_EP, params=params)
        except httpx.HTTPError as e:
            raise AirKoreaApiError("HTTP_ERR", str(e)) from e
        if r.status_code != 200:
            raise AirKoreaApiError(
                f"HTTP_{r.status_code}", _redact_service_key(r.text)[:200]
            )
        try:
            data = r.json()
        except ValueError as e:
            # 키 미신청·서비스 중단 시 XML/HTML 오류 문서가 200 으로 온다.
            raise AirKoreaApiError(
                "DECODE_ERR", _redact_service_key(r.text)[:200]
            ) from e
        body = data.get("response", {}).get("body", {})
        header = data.get("response", {}).get("header", {})
        code = header.get("resultCode")
        # 정상 코드는 서비스에 따라 "00" 또는 "0" 으로 온다.
        if code is not None and str(code) not in ("00", "0"):
            raise AirKoreaApiError(
                str(code), str(header.get("resultMsg", ""))
            )
        items = body.get("items") or []
        if isinstance(items, dict):
            items = [items]
        if not items:
            raise AirKoreaApiError("EMPTY_ITEMS", f"no station for {sido}")
        total = body.get("totalCount")
        if isinstance(total, (int, str)):
            try:
                if int(total) > len(items):
                    logger.warning(
                        "airkorea station list truncated sido=%s "
                        "total=%s received=%d",
                        sido, total, len(items),
                    )
            except (TypeError, ValueError):
                pass
        return [it for it in items if isinstance(it, dict)]


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


class GooglePlacesApiError(Exception):
    """Google 장소 API 호출 실패를 표현하는 예외.

    code: 분류 문자열.
        "HTTP_ERR"           — 전송 실패(연결/타임아웃)
        "HTTP_<status_code>" — 200 이외 응답
        "NON_JSON"           — 200 이지만 본문이 JSON 이 아님
        "EMPTY"              — 정상 응답이지만 필요한 값이 비어 있음
    msg: 응답 본문 앞부분(최대 200자) 또는 예외 메시지.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"google places code={code} msg={msg}")
        self.code = code
        self.msg = msg


class GooglePlacesClient:
    """Google 장소 사진 조회를 캡슐화하는 클라이언트.

    API 키는 모든 요청에 X-Goog-Api-Key 헤더로 첨부한다 — 키가 URL 쿼리에
    실리지 않으므로 별도의 쿼리 키 마스킹(_redact_service_key)이 필요 없다.

    이 API 는 응답에 담을 필드를 요청마다 헤더(X-Goog-FieldMask)로 지정해야
    하고, 지정하지 않으면 오류를 돌려준다. 게다가 어떤 필드를 요구하느냐가
    곧 과금 등급이라, 마스크는 공통 헤더로 고정하지 않고 메서드마다 필요한
    최소값을 직접 넘긴다. 사진 목록까지는 식별자 등급(무료)만 쓰고, 과금은
    이미지 URL 발급에서만 발생한다.

    검색은 본문을 실어 보내는 POST 라, 다른 클라이언트들의 GET 전용
    헬퍼와 달리 메서드를 인자로 받는 요청 헬퍼를 둔다.

    제공 endpoint:
      SEARCH_EP — 검색어+좌표로 장소 식별자 조회
      /v1/places/{id}        — 장소의 사진 목록
      /v1/{photo_name}/media — 사진 이미지 URL 발급(과금 지점)
    """

    HOST = "https://places.googleapis.com"
    SEARCH_EP = "/v1/places:searchText"

    def __init__(
        self,
        api_key: str,
        timeout: float = settings.GOOGLE_PLACES_TIMEOUT_SEC,
    ) -> None:
        """Google 장소 클라이언트 초기화.

        api_key: Google Cloud 콘솔에서 발급한 API 키(평문).
        timeout: 단일 HTTP 요청 타임아웃(초).
        """
        self._client = httpx.AsyncClient(
            base_url=self.HOST,
            headers={"X-Goog-Api-Key": api_key},
            timeout=timeout,
        )

    async def __aenter__(self) -> "GooglePlacesClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자의 명시적 close 용."""
        await self._client.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        field_mask: str | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        """공통 요청 헬퍼.

        field_mask 가 주어지면 그 요청에만 붙는다. 이미지 URL 발급처럼
        마스크를 받지 않는 endpoint 는 생략한다.

        전송 실패는 GooglePlacesApiError("HTTP_ERR"), 200 이외 응답은
        ("HTTP_<status>"), 비-JSON 본문은 ("NON_JSON") 으로 변환한다.
        """
        headers = {"X-Goog-FieldMask": field_mask} if field_mask else None
        try:
            r = await self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            raise GooglePlacesApiError("HTTP_ERR", str(e)) from e
        if r.status_code != 200:
            raise GooglePlacesApiError(
                f"HTTP_{r.status_code}", r.text[:200]
            )
        try:
            return r.json()
        except ValueError as e:
            raise GooglePlacesApiError("NON_JSON", r.text[:200]) from e

    async def search_place_id(
        self, query: str, lat: float, lng: float, radius_m: int
    ) -> str | None:
        """검색어와 좌표로 장소 식별자 한 건을 찾는다.

        좌표 주변으로 검색을 치우치게 해 동명 장소가 엉뚱하게 잡히는 것을
        줄인다. 마스크를 식별자 하나로 좁혀 과금 등급을 최저로 유지한다.

        반환: 첫 결과의 식별자. 결과가 없으면 None.
        """
        body = {
            "textQuery": query,
            "languageCode": "ko",
            "pageSize": 1,
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": float(radius_m),
                }
            },
        }
        data = await self._request_json(
            "POST",
            self.SEARCH_EP,
            field_mask="places.id",
            json_body=body,
        )
        places = data.get("places") or []
        if not places:
            return None
        return places[0].get("id") or None

    async def fetch_photos(self, place_id: str) -> list[dict]:
        """장소의 사진 목록(이름과 표기 정보)을 조회한다.

        여기서 받는 사진 이름은 만료될 수 있어 캐시하지 않고 매번 새로
        받는다. 마스크가 사진 필드 하나뿐이라 이 호출에는 과금이 없다.
        """
        data = await self._request_json(
            "GET", f"/v1/places/{place_id}", field_mask="photos"
        )
        return self._normalize_photos(data.get("photos") or [])

    async def fetch_photo_uri(
        self, photo_name: str, max_width_px: int
    ) -> str:
        """사진 이름으로 이미지 URL 을 발급받는다(과금 지점).

        기본 동작은 이미지로의 리다이렉트라, 우리는 URL 자체가 필요하므로
        리다이렉트를 건너뛰고 URL 을 본문으로 받는다. 발급된 URL 은 수명이
        짧아 저장하지 않고 응답에 실어 보낸다.
        """
        data = await self._request_json(
            "GET",
            f"/v1/{photo_name}/media",
            params={
                "maxWidthPx": max_width_px,
                "skipHttpRedirect": "true",
            },
        )
        uri = data.get("photoUri") or ""
        if not uri:
            raise GooglePlacesApiError("EMPTY", "no photoUri in response")
        return uri

    @classmethod
    def _normalize_photos(cls, photos: list[dict]) -> list[dict]:
        """사진 목록을 우리 표현으로 정규화한다.

        한 건의 비정상이 응답 전체를 깨지 않도록 개별 변환 실패는 흡수한다.
        이름이 없는 항목은 이미지 URL 을 발급받을 수 없어 버린다.
        """
        out: list[dict] = []
        for p in photos:
            try:
                name = p.get("name") or ""
                if not name:
                    continue
                out.append(
                    {
                        "name": name,
                        "width_px": cls._as_int(p.get("widthPx")),
                        "height_px": cls._as_int(p.get("heightPx")),
                        "attributions": cls._normalize_attributions(
                            p.get("authorAttributions") or []
                        ),
                        "google_maps_uri": p.get("googleMapsUri") or None,
                        "flag_content_uri": p.get("flagContentUri") or None,
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _as_int(value: object) -> int | None:
        """치수 값을 정수로 바꾼다. 없거나 숫자가 아니면 None."""
        if value is None:
            return None
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_attributions(items: list[dict]) -> list[dict]:
        """사진 제공자 표기를 정규화한다.

        이름이 비어 있으면 표기로서 의미가 없으므로 버린다. 표기는 화면에
        반드시 노출해야 하는 값이라 정규화 단계에서 형태를 고정한다.
        """
        out: list[dict] = []
        for a in items:
            if not isinstance(a, dict):
                continue
            name = (a.get("displayName") or "").strip()
            if not name:
                continue
            out.append({"display_name": name, "uri": a.get("uri") or None})
        return out


class OsrmApiError(Exception):
    """OSRM 라우팅 호출 실패를 표현하는 예외.

    code: 분류 문자열.
        "HTTP_ERR"           — 전송 실패(연결/타임아웃)
        "HTTP_<status_code>" — 200 이외 응답
        "CODE_<osrm_code>"   — 200 이지만 OSRM code 가 "Ok" 아님(예: NoRoute)
        "EMPTY"              — routes 배열이 비어 있음
    msg: 응답 본문 앞부분(최대 200자) 또는 예외 메시지.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"osrm code={code} msg={msg}")
        self.code = code
        self.msg = msg


class OsrmClient:
    """자체 호스팅 OSRM 라우팅 호출을 캡슐화하는 클라이언트.

    한 인스턴스는 하나의 프로파일(foot 또는 bicycle) OSRM 서버를 가리킨다.
    프로파일 선택은 base_url 로 결정되므로(mode→base_url 매핑은 호출부),
    요청 경로의 profile 세그먼트는 서버가 무시한다.

    좌표 표기 차이 처리: OSRM 응답 geometry 는 GeoJSON [경도, 위도] 순서다.
    본 클라이언트가 결과를 돌려줄 때 우리 표현(lat=위도, lng=경도)으로
    바꿔 담으므로, 좌표 교차는 이 한 곳(_normalize_route)에서만 일어난다.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = settings.OSRM_TIMEOUT_SEC,
    ) -> None:
        """OSRM 클라이언트 초기화.

        base_url: 프로파일 OSRM 서버 base URL(예: "http://osrm-foot:5000").
        timeout: 단일 HTTP 요청 타임아웃(초).
        """
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def __aenter__(self) -> "OsrmClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자의 명시적 close 용."""
        await self._client.aclose()

    async def route(
        self,
        start_lat: float,
        start_lng: float,
        goal_lat: float,
        goal_lng: float,
    ) -> dict:
        """두 좌표 사이의 도로 추종 경로를 조회해 정규화 형태로 돌려준다.

        전송 실패는 OsrmApiError("HTTP_ERR"), 200 이외 응답은
        OsrmApiError("HTTP_<status>"), code!="Ok" 는 OsrmApiError("CODE_*")
        로 변환한다. 성공 시 {"path":[[lat,lng],...],"distance_m":int,
        "duration_s":int}.

        OSRM 좌표 순서는 경도,위도이므로 요청 URL 도 lng,lat 로 만든다.
        """
        # /route/v1/{profile}/{coords} — profile 세그먼트는 서버가 무시하나
        # 경로 형식을 지키기 위해 관용적으로 "driving" 을 둔다.
        coords = f"{start_lng},{start_lat};{goal_lng},{goal_lat}"
        path = f"/route/v1/driving/{coords}"
        params = {
            "overview": "full",
            "geometries": "geojson",
            "alternatives": "false",
            "steps": "false",
        }
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise OsrmApiError("HTTP_ERR", str(e)) from e
        if r.status_code != 200:
            raise OsrmApiError(f"HTTP_{r.status_code}", r.text[:200])
        return self._normalize_route(r.json())

    @staticmethod
    def _normalize_route(data: dict) -> dict:
        """OSRM 응답을 우리 표현으로 정규화한다(좌표 스왑의 유일한 지점).

        - code 가 "Ok" 아니면 OsrmApiError("CODE_*").
        - routes[0].geometry.coordinates([[lng,lat],...]) 를 [[lat,lng]] 로
          스왑한 뒤 단순화(≤ROUTE_MAX_POINTS)해 path 로 담는다.
        - distance(m)/duration(s) 는 정수로 반올림.
        """
        # 지연 임포트: 유틸 모듈이 clients 를 역참조하지 않게 함수 내부에서 임포트.
        from app.utils.polyline_simplify import simplify

        code = data.get("code")
        if code != "Ok":
            raise OsrmApiError(f"CODE_{code}", str(data.get("message") or ""))
        routes = data.get("routes") or []
        if not routes:
            raise OsrmApiError("EMPTY", "no routes")
        route0 = routes[0]
        coords = (route0.get("geometry") or {}).get("coordinates") or []
        # GeoJSON [lng,lat] → 우리 [lat,lng].
        latlng = [[float(c[1]), float(c[0])] for c in coords if len(c) >= 2]
        latlng = simplify(latlng, max_points=settings.ROUTE_MAX_POINTS)
        return {
            "path": latlng,
            "distance_m": int(round(float(route0.get("distance") or 0.0))),
            "duration_s": int(round(float(route0.get("duration") or 0.0))),
        }


class OdsayApiError(Exception):
    """ODsay 지하철 경로 호출 실패를 표현하는 예외.

    code: 분류 문자열.
        "HTTP_ERR"           — 전송 실패(연결/타임아웃)
        "HTTP_<status_code>" — 200 이외 응답
        "NON_JSON"           — 200 이지만 본문이 JSON 이 아님
        "API_ERR"            — 200 이고 JSON 이지만 본문이 오류를 담고 있음
    msg: 응답 본문 앞부분(최대 200자) 또는 예외 메시지. 키는 가려서 담는다.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"odsay code={code} msg={msg}")
        self.code = code
        self.msg = msg


class OdsayClient:
    """ODsay 대중교통 경로 호출을 캡슐화하는 클라이언트.

    인증키를 URL 쿼리(apiKey)로 보내야 하는 형태라, 오류 본문과 전송 실패
    메시지를 모두 _redact_secret 에 통과시킨 뒤 예외에 담는다.

    이 발급처는 인증 실패도 상태코드 200 으로 내려주고 본문에 오류를 담는다.
    게다가 오류를 객체 하나가 아니라 배열로 감싼다. 그대로 두면 "정상 응답인데
    경로가 하나도 없다"로 읽혀 사용자에게 "갈 수 있는 길이 없다"로 보이므로,
    본문 판별을 여기서 끝내고 실패는 예외로 올린다.
    """

    HOST = "https://api.odsay.com"
    SEARCH_EP = "/v1/api/searchPubTransPathT"
    # 응답의 경로 종류 값 중 지하철만으로 가는 경로를 가리키는 값.
    PATH_TYPE_SUBWAY = 1
    # 구간 종류 값. 그 밖의 값은 걷는 구간으로 본다.
    #
    # 6(시외버스)을 따로 두는 이유: 이 값이 없으면 도시 간 이동이 통째로 도보로
    # 떨어진다. 안산 → 속초 234 km 가 "도보 217분"으로 표시됐다.
    # 시내버스로 합치지 않는 것은 요금대와 정류장 표기가 서로 달라서다.
    TRAFFIC_TYPE_SUBWAY = 1
    TRAFFIC_TYPE_BUS = 2
    TRAFFIC_TYPE_INTERCITY_BUS = 6

    def __init__(
        self,
        api_key: str,
        timeout: float = settings.ODSAY_REQUEST_TIMEOUT_SEC,
    ) -> None:
        """ODsay 클라이언트 초기화.

        api_key: 발급받은 인증키(평문). 매 요청 쿼리에 실린다.
        timeout: 단일 HTTP 요청 타임아웃(초).
        """
        self._key = api_key
        self._client = httpx.AsyncClient(base_url=self.HOST, timeout=timeout)

    async def __aenter__(self) -> "OdsayClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자의 명시적 close 용."""
        await self._client.aclose()

    # route_options 가 한 번에 돌려주는 경로 후보 상한. 발급처가 한 요청에
    # 많게는 십수 건을 돌려주는데, 리스트 화면에 그만큼 다 늘어놓을 필요는
    # 없다 — 소요시간 오름차순으로 앞에서부터 이만큼만 자른다.
    ROUTE_OPTIONS_MAX = 8

    async def _search_path(
        self,
        start_lat: float,
        start_lng: float,
        goal_lat: float,
        goal_lng: float,
    ) -> dict:
        """searchPubTransPathT 원본 응답 JSON을 그대로 돌려준다.

        fastest_subway_route/route_options 가 공유하는 전송 계층. 오류 본문
        판별과 정규화는 각 호출부가 맡는다(목적에 따라 필터·모양이 다르다).

        전송 실패는 OdsayApiError("HTTP_ERR"), 200 이외는 "HTTP_<status>",
        비-JSON 은 "NON_JSON" 으로 올린다.
        """
        params = {
            "SX": str(start_lng),
            "SY": str(start_lat),
            "EX": str(goal_lng),
            "EY": str(goal_lat),
            "apiKey": self._key,
        }
        try:
            r = await self._client.get(self.SEARCH_EP, params=params)
        except httpx.HTTPError as e:
            raise OdsayApiError("HTTP_ERR", _redact_secret(str(e))) from e
        if r.status_code != 200:
            raise OdsayApiError(
                f"HTTP_{r.status_code}", _redact_secret(r.text)[:200]
            )
        try:
            return r.json()
        except ValueError as e:
            raise OdsayApiError(
                "NON_JSON", _redact_secret(r.text)[:200]
            ) from e

    async def fastest_subway_route(
        self,
        start_lat: float,
        start_lng: float,
        goal_lat: float,
        goal_lng: float,
    ) -> dict | None:
        """지하철만으로 가는 경로 중 가장 빠른 것을 정규화해 돌려준다.

        지하철만으로 갈 수 없으면 None. 이것은 실패가 아니라 조회 결과이므로
        예외로 올리지 않는다 — 호출부가 "경로 없음"과 "조회 불가"를 구분해
        화면에 다르게 보여줘야 하기 때문이다.

        본문에 담긴 오류는 "API_ERR" 로 올린다(전송 계층 오류는 _search_path
        참고).
        """
        data = await self._search_path(start_lat, start_lng, goal_lat, goal_lng)
        return self._normalize(data)

    async def route_options(
        self,
        start_lat: float,
        start_lng: float,
        goal_lat: float,
        goal_lng: float,
    ) -> list[dict]:
        """지하철·버스를 아우른 경로 후보를 소요시간 순으로 정규화해 돌려준다.

        fastest_subway_route 와 달리 pathType 으로 거르지 않는다 — 여기는
        버스 전용·혼합 경로까지 그대로 보여주는 "이동수단 모두 보기" 용
        엔드포인트다. 각 구간에는 지도에 그릴 수 있게 geometry 좌표열도
        함께 담는다.
        """
        data = await self._search_path(start_lat, start_lng, goal_lat, goal_lng)
        return self._normalize_routes(data)

    @classmethod
    def _raise_if_error(cls, data: dict) -> None:
        """응답 본문에 담긴 오류를 OdsayApiError("API_ERR") 로 올린다.

        오류가 배열로 오는 경우와 객체 하나로 오는 경우를 함께 받는다.
        """
        error = data.get("error")
        if not error:
            return
        first = error[0] if isinstance(error, list) and error else error
        msg = ""
        if isinstance(first, dict):
            msg = str(first.get("message") or first.get("msg") or first)
        elif first is not None:
            msg = str(first)
        raise OdsayApiError("API_ERR", _redact_secret(msg)[:200])

    @classmethod
    def _normalize(cls, data: dict) -> dict | None:
        """응답 본문을 우리 표현으로 정규화한다.

        오류 본문이면 OdsayApiError("API_ERR"). 지하철 단독 경로가 없으면
        None. 있으면 그중 총 소요시간이 가장 짧은 하나를 담아 돌려준다.
        """
        cls._raise_if_error(data)
        paths = ((data.get("result") or {}).get("path")) or []
        subway_only = [
            p
            for p in paths
            if isinstance(p, dict)
            and p.get("pathType") == cls.PATH_TYPE_SUBWAY
        ]
        if not subway_only:
            return None
        best = min(
            subway_only,
            key=lambda p: cls._as_int((p.get("info") or {}).get("totalTime")),
        )
        info = best.get("info") or {}
        return {
            "total_time_min": cls._as_int(info.get("totalTime")),
            "fare": cls._as_int(info.get("payment")),
            "transfer_count": cls._as_int(info.get("subwayTransitCount")),
            "total_walk_m": cls._as_int(info.get("totalWalk")),
            "steps": [
                cls._normalize_step(s)
                for s in (best.get("subPath") or [])
                if isinstance(s, dict)
            ],
        }

    @classmethod
    def _normalize_routes(cls, data: dict) -> list[dict]:
        """응답 본문에서 경로 후보 전부를 정규화해 최대 ROUTE_OPTIONS_MAX개
        돌려준다.

        지하철 전용 조회(_normalize)와 달리 pathType 으로 거르지 않는다.
        소요시간 오름차순으로 정렬해 앞에서부터 자른다.
        """
        cls._raise_if_error(data)
        paths = ((data.get("result") or {}).get("path")) or []
        valid = [p for p in paths if isinstance(p, dict)]
        valid.sort(
            key=lambda p: cls._as_int((p.get("info") or {}).get("totalTime"))
        )
        return [cls._to_route_option(p) for p in valid[: cls.ROUTE_OPTIONS_MAX]]

    @classmethod
    def _to_route_option(cls, path: dict) -> dict:
        """경로 후보 한 건을 정규화한다(_normalize 의 최단 하나 대신 목록용).

        modes 는 구간에 실제 등장한 지하철·버스만 순서(지하철 먼저) 담는다.
        걷기만으로 이뤄진 경로는 오지 않지만, 방어적으로 빈 경우 "walk" 를
        채운다.
        """
        info = path.get("info") or {}
        legs = [
            cls._normalize_step(s)
            for s in (path.get("subPath") or [])
            if isinstance(s, dict)
        ]
        present = {leg["type"] for leg in legs}
        modes = [
            t for t in ("subway", "bus", "intercity") if t in present
        ] or ["walk"]
        subway_m = sum(
            leg["distance_m"] for leg in legs if leg["type"] == "subway"
        )
        # 시외버스를 버스 거리에 합친다. 비중은 "이 경로가 지하철 경로냐,
        # 사실상 버스 경로냐"를 가르는 값이라, 시내든 시외든 지하철이 아니라는
        # 점에서는 같다. 빼면 도시 간 경로가 ride_m=0 이 되어 버스 비중 0% —
        # 곧 "지하철 위주"로 잘못 읽힌다. 화면 표기만 intercity 로 가른다.
        bus_m = sum(
            leg["distance_m"]
            for leg in legs
            if leg["type"] in ("bus", "intercity")
        )
        ride_m = subway_m + bus_m
        return {
            "total_time_min": cls._as_int(info.get("totalTime")),
            "fare": cls._as_int(info.get("payment")),
            "transfer_count": (
                cls._as_int(info.get("busTransitCount"))
                + cls._as_int(info.get("subwayTransitCount"))
            ),
            "total_walk_m": cls._as_int(info.get("totalWalk")),
            "subway_distance_m": subway_m,
            "bus_distance_m": bus_m,
            # 타는 구간이 없으면 0.0 — 도보뿐인 경로에서 0 으로 나누지 않는다.
            "bus_distance_ratio": (bus_m / ride_m) if ride_m > 0 else 0.0,
            "modes": modes,
            "legs": legs,
        }

    @classmethod
    def _normalize_step(cls, step: dict) -> dict:
        """구간 하나를 정규화한다.

        노선명은 배열의 첫 항목에만 들어 있고, 걷는 구간에는 아예 없다.
        geometry 는 지도에 그릴 [lat,lng] 좌표열이다 — 지하철 단독 경로
        응답(SubwayRouteStep)은 이 필드를 쓰지 않지만, 정규화 지점을
        하나로 유지하려고 여기서 함께 채운다(pydantic 이 남는 필드는
        무시한다).
        """
        traffic = cls._as_int(step.get("trafficType"))
        if traffic == cls.TRAFFIC_TYPE_SUBWAY:
            step_type = "subway"
        elif traffic == cls.TRAFFIC_TYPE_BUS:
            step_type = "bus"
        elif traffic == cls.TRAFFIC_TYPE_INTERCITY_BUS:
            step_type = "intercity"
        else:
            step_type = "walk"
        lanes = step.get("lane")
        line_name = None
        if isinstance(lanes, list) and lanes and isinstance(lanes[0], dict):
            line_name = lanes[0].get("name") or None
        raw_count = step.get("stationCount")
        station_count = (
            cls._as_int(raw_count) if raw_count is not None else None
        )
        return {
            "type": step_type,
            "line_name": line_name,
            "start_name": str(step.get("startName") or ""),
            "end_name": str(step.get("endName") or ""),
            "section_time_min": cls._as_int(step.get("sectionTime")),
            "station_count": station_count,
            # 이동수단별 비중을 재는 근거값. 도보 연결 구간은 0 으로 온다.
            "distance_m": cls._as_int(step.get("distance")),
            "geometry": cls._step_geometry(step),
            "stops": cls._step_stop_names(step),
        }

    @classmethod
    def _step_stop_names(cls, step: dict) -> list[str]:
        """구간이 지나는 역/정류장 이름을 순서대로 담는다.

        passStopList.stations 가 없으면(버스는 가끔 비어 온다, 도보 연결
        구간은 아예 없다) 빈 리스트다 — geometry 와 달리 시작/끝 이름으로
        대체하지 않는다. "지나는 정류장" 목록에 시작/끝만 지어내 채우면
        실제로 몇 개를 거치는지 오인시킨다.
        """
        stops = (step.get("passStopList") or {}).get("stations")
        if not isinstance(stops, list):
            return []
        names: list[str] = []
        for s in stops:
            if not isinstance(s, dict):
                continue
            name = s.get("stationName")
            if name:
                names.append(str(name))
        return names

    @classmethod
    def _step_geometry(cls, step: dict) -> list[list[float]]:
        """구간 좌표열을 [lat,lng] 로 만든다.

        passStopList.stations 가 있으면 지나는 역/정류장 순서 그대로 담아
        실제 굴곡을 살린다. 없으면 시작/끝 좌표 2점으로 대체한다(버스는
        가끔 이 목록이 비어 온다). 좌표 필드가 아예 없는 순수 도보 연결
        구간(trafficType=3, 거리 0)은 빈 리스트를 돌려준다 — loadLane API가
        실호출에서 "-8 mapObject 형식이 잘못되었습니다"로 실패해(공식 문서와
        어긋남) 대신 찾은 대안이다.
        """
        stops = (step.get("passStopList") or {}).get("stations")
        if isinstance(stops, list) and stops:
            pts: list[list[float]] = []
            for s in stops:
                if not isinstance(s, dict):
                    continue
                x, y = s.get("x"), s.get("y")
                if x is None or y is None:
                    continue
                try:
                    pts.append([float(y), float(x)])
                except (TypeError, ValueError):
                    continue
            if pts:
                return pts
        sx, sy = step.get("startX"), step.get("startY")
        ex, ey = step.get("endX"), step.get("endY")
        if sx is not None and sy is not None and ex is not None and ey is not None:
            try:
                return [[float(sy), float(sx)], [float(ey), float(ex)]]
            except (TypeError, ValueError):
                return []
        return []

    @staticmethod
    def _as_int(value: object) -> int:
        """숫자로 읽히지 않는 값은 0 으로 본다.

        일부 필드가 문자열로 오거나 아예 빠지는 경우가 있어, 정규화 단계에서
        형태를 고정해 두고 아래로는 정수만 흘려보낸다.
        """
        try:
            return int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0


class SeoulBikeApiError(Exception):
    """서울 열린데이터광장 따릉이 조회 실패를 표현하는 예외.

    code: 분류 문자열.
        "HTTP_ERR"           — 전송 실패(연결/타임아웃)
        "HTTP_<status_code>" — 200 이외 응답
        "NON_JSON"           — 200 이지만 본문이 JSON 이 아님
        "API_<result_code>"  — 200 이고 JSON 이지만 결과 코드가 정상이 아님
    msg: 응답 본문 앞부분(최대 200자) 또는 예외 메시지. 키는 가려서 담는다.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"seoulbike code={code} msg={msg}")
        self.code = code
        self.msg = msg


class SeoulBikeClient:
    """서울 열린데이터광장 따릉이 대여소 현황 호출을 캡슐화하는 클라이언트.

    인증키가 쿼리가 아니라 URL 경로 한 칸을 차지하는 형태다. 그래서 오류
    본문과 전송 실패 메시지를 모두 _redact_secret 에 통과시킨다.

    발급처가 https 를 받지 않아 이 호출만 평문으로 나간다. 그 구간을 사용자
    기기가 아니라 hub 하나로 좁히려고 여기로 옮겼다.

    한 번에 주는 행 수가 정해져 있어 범위를 옮겨 가며 나눠 받는다. 응답의
    개수 필드는 전체 개수가 아니라 그 응답에 담긴 행 수라, 받은 행이 요청한
    범위보다 적으면 마지막 장으로 본다.
    """

    HOST = "http://openapi.seoul.go.kr:8088"
    DATASET = "bikeList"
    # 결과 코드가 이 값이면 정상. 범위를 넘어서 요청하면 다른 값이 온다.
    RESULT_OK = "INFO-000"

    def __init__(
        self,
        api_key: str,
        timeout: float = settings.SEOUL_BIKE_REQUEST_TIMEOUT_SEC,
    ) -> None:
        """따릉이 클라이언트 초기화.

        api_key: 발급받은 인증키(평문). 매 요청 URL 경로에 실린다.
        timeout: 단일 HTTP 요청 타임아웃(초).
        """
        self._key = api_key
        self._client = httpx.AsyncClient(base_url=self.HOST, timeout=timeout)

    async def __aenter__(self) -> "SeoulBikeClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자의 명시적 close 용."""
        await self._client.aclose()

    async def fetch_page(self, start: int, end: int) -> tuple[list[dict], int]:
        """범위 하나를 받아 (정규화된 대여소 목록, 받아 온 행 수)를 돌려준다.

        start/end 는 1 부터 세는 행 번호이며 양끝을 포함한다. 범위를 넘어선
        요청은 결과 코드가 정상이 아니게 오므로 SeoulBikeApiError 로 올린다.

        받아 온 행 수를 따로 돌려주는 이유는 마지막 장 판정 때문이다. 좌표가
        없는 행은 정규화에서 버리므로, 목록 길이로 판정하면 한 장이 꽉 차서
        왔는데도 덜 왔다고 보고 뒤쪽 대여소를 통째로 놓친다.
        """
        path = f"/{self._key}/json/{self.DATASET}/{start}/{end}/"
        try:
            r = await self._client.get(path)
        except httpx.HTTPError as e:
            raise SeoulBikeApiError("HTTP_ERR", _redact_secret(str(e))) from e
        if r.status_code != 200:
            raise SeoulBikeApiError(
                f"HTTP_{r.status_code}", _redact_secret(r.text)[:200]
            )
        try:
            data = r.json()
        except ValueError as e:
            raise SeoulBikeApiError(
                "NON_JSON", _redact_secret(r.text)[:200]
            ) from e
        return self._normalize(data)

    @classmethod
    def _normalize(cls, data: dict) -> tuple[list[dict], int]:
        """응답 본문을 우리 표현으로 정규화한다.

        결과 코드가 정상이 아니면 SeoulBikeApiError. 좌표가 없는 행은 지도에
        찍을 수 없으므로 버린다. 수치 필드가 모두 문자열로 오기 때문에 여기서
        형태를 고정한다.

        버리기 전의 행 수를 함께 돌려준다 — 호출부의 마지막 장 판정에 쓴다.
        """
        body = data.get("rentBikeStatus")
        if not isinstance(body, dict):
            # 범위를 벗어나면 본문 모양 자체가 달라진다. 그 경우도 오류로 본다.
            raise SeoulBikeApiError("API_UNKNOWN", str(data)[:200])
        result_code = str((body.get("RESULT") or {}).get("CODE") or "")
        if result_code != cls.RESULT_OK:
            raise SeoulBikeApiError(
                f"API_{result_code or 'UNKNOWN'}",
                str((body.get("RESULT") or {}).get("MESSAGE") or "")[:200],
            )
        rows = body.get("row") or []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            lat = cls._as_float(row.get("stationLatitude"))
            lng = cls._as_float(row.get("stationLongitude"))
            if lat is None or lng is None or (lat == 0.0 and lng == 0.0):
                continue
            out.append(
                {
                    "station_id": str(row.get("stationId") or ""),
                    "name": str(row.get("stationName") or ""),
                    "rack_total": cls._as_int(row.get("rackTotCnt")),
                    "parking_bike_total": cls._as_int(
                        row.get("parkingBikeTotCnt")
                    ),
                    "lat": lat,
                    "lng": lng,
                }
            )
        return out, len(rows)

    @staticmethod
    def _as_int(value: object) -> int:
        """숫자로 읽히지 않는 값은 0 으로 본다."""
        try:
            return int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _as_float(value: object) -> float | None:
        """숫자로 읽히지 않으면 None. 좌표가 없는 행을 걸러내는 데 쓴다."""
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


class PmApiError(Exception):
    """공유 킥보드 조회 실패를 표현하는 예외.

    code: 분류 문자열.
        "HTTP_ERR"           — 전송 실패(연결/타임아웃)
        "HTTP_<status_code>" — 200 이외 응답
        "NON_JSON"           — 200 이지만 본문이 JSON 이 아님
        "AUTH"               — 인증키가 등록돼 있지 않거나 거부됨
        "API_<result_code>"  — 200 이고 JSON 이지만 결과 코드가 정상이 아님
    msg: 응답 본문 앞부분(최대 200자) 또는 예외 메시지. 키는 가려서 담는다.
    """

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(f"pm code={code} msg={msg}")
        self.code = code
        self.msg = msg


class PmClient:
    """국토교통부 공유 퍼스널모빌리티 조회를 캡슐화하는 클라이언트.

    인증키를 URL 쿼리(serviceKey)로 보내는 형태라 오류 본문과 전송 실패
    메시지를 모두 _redact_secret 에 통과시킨다.

    사업자를 지정해야만 조회가 되고 사업자 목록을 주는 오퍼레이션은 없다.
    그래서 한 번 조회에 사업자 수만큼 호출이 나간다 — 호출부가 그 값을
    설정에서 읽어 넘긴다.

    본문 형태가 두 가지다. 정상 경로는 `response.header.resultCode` 를 주고,
    인증 단계에서 막히면 아예 다른 껍데기(`OpenAPI_ServiceResponse`)로 온다.
    상태코드는 둘 다 200 이라 본문을 보고 갈라야 한다.
    """

    HOST = "https://apis.data.go.kr"
    LIST_EP = "/1613000/PersonalMobilityInfo/GetPMListByProvider"
    # 결과 코드가 이 값이면 정상.
    RESULT_OK = "00"

    def __init__(
        self,
        service_key: str,
        timeout: float = settings.PM_REQUEST_TIMEOUT_SEC,
    ) -> None:
        """공유 킥보드 클라이언트 초기화.

        service_key: data.go.kr 인증키(디코딩 키 권장). 매 요청 쿼리에 실린다.
        timeout: 단일 HTTP 요청 타임아웃(초).
        """
        self._key = service_key
        self._client = httpx.AsyncClient(base_url=self.HOST, timeout=timeout)

    async def __aenter__(self) -> "PmClient":
        """`async with` 진입 훅. self 를 그대로 반환."""
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """`async with` 탈출 시 내부 httpx 클라이언트를 닫는다."""
        await self._client.aclose()

    async def aclose(self) -> None:
        """`async with` 를 쓰지 않는 호출자의 명시적 close 용."""
        await self._client.aclose()

    async def fetch_by_provider(
        self,
        provider: str,
        *,
        city: str | None = None,
        num_of_rows: int = settings.PM_NUMOFROWS,
    ) -> list[dict]:
        """사업자 하나의 기기 목록을 정규화해 돌려준다.

        해당 사업자에 기기가 없으면 빈 목록이며, 그것은 실패가 아니다.
        """
        params: dict[str, str] = {
            "serviceKey": self._key,
            "numOfRows": str(num_of_rows),
            "pageNo": "1",
            "_type": "json",
            "providerName": provider,
        }
        if city:
            params["cityName"] = city
        try:
            r = await self._client.get(self.LIST_EP, params=params)
        except httpx.HTTPError as e:
            raise PmApiError("HTTP_ERR", _redact_secret(str(e))) from e
        if r.status_code != 200:
            raise PmApiError(
                f"HTTP_{r.status_code}", _redact_secret(r.text)[:200]
            )
        try:
            data = r.json()
        except ValueError as e:
            raise PmApiError("NON_JSON", _redact_secret(r.text)[:200]) from e
        return self._normalize(data, provider)

    @classmethod
    def _normalize(cls, data: dict, provider: str) -> list[dict]:
        """응답 본문을 우리 표현으로 정규화한다.

        좌표가 없는 기기는 지도에 찍을 수 없으므로 버린다. 항목이 하나뿐일
        때 배열이 아니라 객체 하나로 오는 경우가 있어 양쪽을 함께 받는다.
        """
        # 인증 단계에서 막히면 껍데기부터 다르다. 이것을 정상 경로로 읽으면
        # "조회는 됐는데 결과가 없다"로 오인해 화면이 조용히 빈 채로 남는다.
        gateway = data.get("OpenAPI_ServiceResponse")
        if isinstance(gateway, dict):
            head = gateway.get("cmmMsgHeader") or {}
            raise PmApiError(
                "AUTH", str(head.get("returnAuthMsg") or head)[:200]
            )

        response = data.get("response")
        if not isinstance(response, dict):
            raise PmApiError("API_UNKNOWN", str(data)[:200])
        header = response.get("header") or {}
        result_code = str(header.get("resultCode") or "")
        if result_code != cls.RESULT_OK:
            raise PmApiError(
                f"API_{result_code or 'UNKNOWN'}",
                str(header.get("resultMsg") or "")[:200],
            )

        items = ((response.get("body") or {}).get("items")) or {}
        rows = items.get("item") if isinstance(items, dict) else None
        if rows is None:
            return []
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return []

        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            lat = cls._as_float(row.get("lat") or row.get("latitude"))
            lng = cls._as_float(row.get("lng") or row.get("longitude"))
            if lat is None or lng is None or (lat == 0.0 and lng == 0.0):
                continue
            out.append(
                {
                    "provider": str(row.get("providerName") or provider),
                    "device_id": str(row.get("deviceId") or ""),
                    "battery_level": cls._as_int(row.get("batteryLevel")),
                    "vehicle_type": str(row.get("vehicleType") or ""),
                    "lat": lat,
                    "lng": lng,
                }
            )
        return out

    @staticmethod
    def _as_int(value: object) -> int | None:
        """숫자로 읽히지 않으면 None. 배터리 잔량이 빠질 수 있다."""
        try:
            return int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_float(value: object) -> float | None:
        """숫자로 읽히지 않으면 None. 좌표가 없는 기기를 걸러내는 데 쓴다."""
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
