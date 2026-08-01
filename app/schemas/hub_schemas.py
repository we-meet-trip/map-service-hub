"""hub-service public API 응답 스키마.

본 모듈은 hub 서비스가 외부(agent 등)에 노출하는 HTTP 응답 본문의
타입을 정의한다. FastAPI 라우터의 `response_model` 로 지정되어
직렬화 형식과 검증 규칙(예: 범위 제약)을 보장한다.

여기서 정의된 모델은 다음 위치에서 소비된다:
  - WeatherDailyItem / WeatherResponse → hub_routers.get_weather 의
    response_model. agent 의 HubClient.fetch_weather 가 이 형태로 받는다.
  - PlaceItem / PlacesResponse → hub_routers.get_places 의 response_model.
    여러 출처(점 장소·코스)를 한 형태로 합쳐 노출한다.
  - ReviewItem / ReviewsResponse → hub_routers.get_reviews 의 response_model.
    네이버 블로그 검색 결과를 리뷰 형태로 노출한다.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class WeatherDailyItem(BaseModel):
    """WeatherDailyItem — 하루치 날씨

    하루 단위 날씨 한 칸을 표현하는 모델.

    date: 해당 날짜 (datetime.date 타입)
    temp_min / temp_max: 최저/최고 기온(℃). 값이 없을 수 있어
        int | None, 기본 None
    precipitation_prob: 강수 확률. ge=0, le=100 제약 → 0~100 범위 밖이면
        검증 실패. 단위는 %
    sky_condition: 하늘 상태 텍스트(예: "맑음"). 없을 수 있음.
        단기예보 출처일 때는 KMA SKY 코드를 label_sky 로 한글 변환한 값,
        중기예보 출처일 때는 원문 weather 텍스트(wf*) 가 그대로 들어간다.
    source: 데이터 출처. Literal["short_term", "mid_land+mid_temp"]
        → 둘 중 하나만 허용
        short_term: 단기예보 (대략 오늘~D+2)
        mid_land+mid_temp: 중기육상예보 + 중기기온예보 합본
            (대략 D+3~D+10)
    """

    date: date
    temp_min: int | None = None
    temp_max: int | None = None
    precipitation_prob: int | None = Field(default=None, ge=0, le=100)
    sky_condition: str | None = None
    source: Literal["short_term", "mid_land+mid_temp"]


class WeatherResponse(BaseModel):
    """WeatherResponse — 날씨 조회 응답 본문

    /v1/weather 엔드포인트가 반환하는 최종 응답 모델.
    요청한 (province, city, date_start..date_end) 구간에 대해
    가능한 날짜는 daily 에, 데이터 부재 날짜는 missing_dates 에 담는다.

    province: 응답 기준 광역시도 명. lookup 결과의 region.lv1 을 그대로 채움
        (요청값과 다를 수 있음 — fallback 매칭이 일어난 경우).
    city: 응답 기준 시군구 명. lookup 결과의 region.lv2 가 비어있으면
        요청한 city 문자열을 그대로 사용.
    daily: 날짜별 WeatherDailyItem 리스트.
        date 오름차순으로 정렬되어 들어간다.
        단기/중기 출처가 섞일 수 있으며 source 필드로 구분 가능.
    missing_dates: 데이터를 만들 수 없었던 날짜 목록.
        - 요청 범위가 단기·중기 예보 horizon(D+0..D+10) 밖이거나
        - DB 에 해당 날짜의 row 가 비어 있거나
        - 중기 reg_id 가 매핑되지 않은 경우
        오름차순 정렬되어 들어간다.
    """

    province: str
    city: str
    daily: list[WeatherDailyItem]
    missing_dates: list[date]


class WeatherNowObservation(BaseModel):
    """WeatherNowObservation — 지금 관측된 값

    temp_c: 관측 기온(℃).
    pty: 강수 형태 코드. 0 이면 강수 없음.
    base_date / base_time: 관측 발표 일자·시각("YYYYMMDD" / "HHMM").
        같은 발표분을 여러 번 조회해도 값이 같다는 것을 호출 측이 알 수 있다.
    """

    temp_c: float
    pty: int | None = None
    base_date: str
    base_time: str


class WeatherNowYesterday(BaseModel):
    """WeatherNowYesterday — 어제 같은 시간대 기록

    temp_c: 그때 관측된 기온(℃).
    hour_kst: 실제로 기록이 남아 있던 시각(시). 요청 시각과 다를 수 있어
        그대로 노출한다 — 몇 시 기준 비교인지 화면이 판단할 수 있어야 한다.
    """

    temp_c: float
    hour_kst: int


class WeatherNowToday(BaseModel):
    """WeatherNowToday — 오늘 하루의 예보 요약

    실황에는 없는 값들이라 단기예보에서 가져온다. 예보가 아직 없거나 격자로
    행정구역을 찾지 못하면 각 항목이 비어 있을 수 있다.

    temp_max / temp_min: 일 최고/최저 기온(℃).
    precipitation_prob: 강수 확률(%).
    sky_condition: 하늘 상태 텍스트(예: "맑음").
    """

    temp_max: int | None = None
    temp_min: int | None = None
    precipitation_prob: int | None = Field(default=None, ge=0, le=100)
    sky_condition: str | None = None


class WeatherNowAir(BaseModel):
    """WeatherNowAir — 대기오염 측정값과 등급

    pm10 / pm25: 미세먼지·초미세먼지 농도(㎍/㎥).
    pm10_grade / pm25_grade: 농도를 구간표에 대입한 한글 등급.
    station: 대표로 채택한 측정소 이름. 시도 안에서 여러 측정소가 오므로
        어느 지점 값인지 밝힌다.
    """

    pm10: int | None = None
    pm25: int | None = None
    pm10_grade: str | None = None
    pm25_grade: str | None = None
    station: str | None = None


class WeatherNowResponse(BaseModel):
    """WeatherNowResponse — 현재 위치 기준 날씨 응답 본문

    /v1/weather/now 가 반환한다. 위경도로 받은 위치는 격자로 바꾼 뒤 버리고,
    응답에도 격자와 행정구역 명만 싣는다.

    nx / ny: 요청 좌표가 속한 격자.
    province / city: 격자로 역조회한 행정구역. 매칭이 없으면 비어 있다.
    now: 지금 관측값. 이 엔드포인트의 핵심이라 항상 채운다.
    yesterday: 어제 같은 시간대 기록. 기록이 없으면 None — 화면은 이때
        비교 문구를 그리지 않는다.
    today: 오늘 예보 요약. 격자로 행정구역을 못 찾으면 None.
    air: 대기오염 정보. 조회 실패·미지원 지역이면 None.
    """

    nx: int
    ny: int
    province: str | None = None
    city: str | None = None
    now: WeatherNowObservation
    yesterday: WeatherNowYesterday | None = None
    today: WeatherNowToday | None = None
    air: WeatherNowAir | None = None


class PlaceItem(BaseModel):
    """PlaceItem — 장소 후보 1건(출처 통합 표현)

    점 장소(카페·관광지 등)와 코스(걷기/자전거길)를 한 형태로 담는다.
    출처마다 의미 있는 필드만 채워지고 나머지는 None 으로 남는다.

    공통:
      content_id: 출처 접두사를 붙인 식별자(예: "kakao:123",
          "durunubi:T_CRS_MNG...").
      source: 출처 구분("kakao" | "durunubi").
      name: 장소/코스 이름.
      address: 주소 또는 행정구역 텍스트.
      road_address: 도로명 주소(있을 때).
      lat / lng: 위도 / 경도. 코스는 시작점 좌표를 대표값으로 쓴다.
      category: 분류 텍스트(점 장소의 업종 또는 "걷기길"/"자전거길").
      distance_m: 검색 중심으로부터의 거리(m). 좌표 검색 시에만 채워진다.

    점 장소 전용:
      category_group_code / phone / place_url.

    코스 전용:
      crs_dstnc_km: 코스 길이(km).
      crs_total_min: 총 소요 시간(분).
      crs_level: 난이도(1 하 / 2 중 / 3 상).
      brd_div: 걷기("DNWW") / 자전거("DNBW") 구분.
      gpx_url: 전체 트랙 GPX 파일 URL(지도 렌더용).
      route_idx: 코스가 속한 노선 식별자.
    """

    content_id: str
    source: Literal["kakao", "durunubi"]
    name: str
    address: str = ""
    road_address: str | None = None
    lat: float
    lng: float
    category: str | None = None
    distance_m: int | None = None

    category_group_code: str | None = None
    phone: str | None = None
    place_url: str | None = None

    crs_dstnc_km: float | None = None
    crs_total_min: int | None = None
    crs_level: int | None = None
    brd_div: str | None = None
    gpx_url: str | None = None
    route_idx: str | None = None


class PlacesResponse(BaseModel):
    """PlacesResponse — 장소 조회 응답 본문

    여러 출처에서 모은 장소 후보를 합쳐 노출한다.

    places: 장소 후보 리스트.
    count: places 길이(편의 필드).
    sources: 응답에 포함된 출처별 건수(예: {"kakao": 5, "durunubi": 2}).
    """

    places: list[PlaceItem]
    count: int
    sources: dict[str, int]


class ReviewItem(BaseModel):
    """ReviewItem — 블로그 리뷰 1건

    네이버 블로그 검색 결과 한 건을 표현한다. title/description 은
    클라이언트에서 <b></b> 마크업과 HTML 엔티티가 제거된 순수 텍스트다.

    title: 글 제목.
    description: 본문 요약 스니펫.
    bloggername: 블로거 이름(없을 수 있음).
    postdate: 작성일 "YYYYMMDD"(없을 수 있음).
    link: 글 URL(없을 수 있음).
    """

    title: str
    description: str
    bloggername: str | None = None
    postdate: str | None = None
    link: str | None = None


class ReviewsResponse(BaseModel):
    """ReviewsResponse — 리뷰 조회 응답 본문

    /v1/reviews 엔드포인트가 반환하는 최종 응답 모델.

    query: 검색에 사용한 질의 문자열.
    reviews: 리뷰 리스트.
    count: reviews 길이(편의 필드).
    """

    query: str
    reviews: list[ReviewItem]
    count: int


class DirectionsPoint(BaseModel):
    """DirectionsPoint — 경로 요청의 한 좌표(위도/경도).

    lat: 위도. ge=33.0, le=43.0 — 한국 국내 위도 범위 밖이면 검증 실패.
    lng: 경도. ge=124.0, le=132.0 — 한국 국내 경도 범위 밖이면 검증 실패.

    범위 값은 agent 의 `Place`(lat 33~43 / lng 124~132)와 통일한다 —
    BFF 가 agent 후보 POI 좌표로 이 leg 를 구성하므로 동일 범위여야
    하고, 범위 밖 좌표(오작동·주입)를 라우팅 엔진에 넘기기 전에 차단한다.
    """

    lat: float = Field(ge=33.0, le=43.0)
    lng: float = Field(ge=124.0, le=132.0)


class DirectionsLeg(BaseModel):
    """DirectionsLeg — 한 구간(출발→도착) 요청.

    start/goal: 구간 양 끝 좌표.
    start_name/goal_name: 표시용 명칭(엔진 로그·향후 확장용, 필수).
    """

    start: DirectionsPoint
    goal: DirectionsPoint
    start_name: str = Field(min_length=1, max_length=60)
    goal_name: str = Field(min_length=1, max_length=60)


class DirectionsBatchRequest(BaseModel):
    """DirectionsBatchRequest — 여러 구간의 경로를 한 번에 요청.

    mode: 이동수단. walk|bicycle|scooter 만 허용(bus/transit 은 라우팅
        대상이 아니므로 BFF 가 호출 자체를 하지 않는다).
    legs: 1~20개 구간. 응답 routes 는 이와 같은 길이·인덱스로 정렬된다.
    """

    mode: Literal["walk", "bicycle", "scooter"]
    legs: list[DirectionsLeg] = Field(min_length=1, max_length=20)


class DirectionsRoute(BaseModel):
    """DirectionsRoute — 한 구간의 도로 추종 경로 결과.

    path: [lat, lng] 점 목록(2~ROUTE_MAX_POINTS). 첫 점=출발, 끝 점=도착.
    distance_m: 실측 이동 거리(m). duration_s: 실측 이동 시간(초).
    """

    path: list[list[float]]
    distance_m: int
    duration_s: int


class DirectionsBatchResponse(BaseModel):
    """DirectionsBatchResponse — 배치 경로 응답.

    routes: 요청 legs 와 같은 길이·인덱스. 특정 구간 경로가 없거나 실패한
        경우 해당 인덱스는 null(호출측이 직선 폴백). 업스트림 장애가
        전 구간에 걸쳐도 200 + 전부 null 로 응답한다(hub degrade 원칙).
    """

    routes: list[DirectionsRoute | None]
