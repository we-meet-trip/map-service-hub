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
  - PlacePhotoItem / PlacePhotosResponse → hub_routers.get_place_photos 의
    response_model. 장소 사진과 그 출처 표기를 노출한다.
"""
from __future__ import annotations

from datetime import date, datetime
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
    source: 데이터 출처. 실제로 값을 채운 쪽을 밝힌다.
        short_term: 단기예보 (대략 오늘~D+3)
        mid_land+mid_temp: 중기육상예보 + 중기기온예보 합본
        mid_land: 중기육상만 채워짐(기온 발표분 결측)
        mid_temp: 중기기온만 채워짐(육상 발표분 결측)
    """

    date: date
    temp_min: int | None = None
    temp_max: int | None = None
    precipitation_prob: int | None = Field(default=None, ge=0, le=100)
    sky_condition: str | None = None
    source: Literal[
        "short_term", "mid_land", "mid_temp", "mid_land+mid_temp"
    ]


class WeatherResponse(BaseModel):
    """WeatherResponse — 날씨 조회 응답 본문

    /v1/weather 엔드포인트가 반환하는 최종 응답 모델.
    요청한 (province, city, date_start..date_end) 구간에 대해
    가능한 날짜는 daily 에, 데이터 부재 날짜는 missing_dates 에 담는다.

    province: 응답 기준 광역시도 명. lookup 결과의 region.lv1 을 그대로 채움
        (요청값과 다를 수 있음 — fallback 매칭이 일어난 경우).
    city: 응답 기준 시군구 명. lookup 결과의 region.lv2 가 비어있으면
        요청한 city 문자열을 그대로 사용.
    region_fallback: 요청한 시군구를 찾지 못해 광역 대표 지점의 예보로
        대신했는지 여부. city 필드에는 요청 문자열이 그대로 실리므로,
        이 플래그가 없으면 소비자가 대체 사실을 알 수 없다.
    short_term_base_at: daily 의 단기예보 항목이 속한 발표 시각.
        단기 조회를 하지 않았거나 적재분이 없으면 None.
    mid_land_tm_fc / mid_temp_tm_fc: 중기 육상·기온 항목이 각각 속한
        발표 시각. 두 값이 다를 수 있다(한쪽 폴링만 성공한 경우).
        폴링이 멈춰 값이 낡았는지를 소비자가 판단할 근거가 된다.
    daily: 날짜별 WeatherDailyItem 리스트.
        date 오름차순으로 정렬되어 들어간다.
        단기/중기 출처가 섞일 수 있으며 source 필드로 구분 가능.
    missing_dates: 데이터를 만들 수 없었던 날짜 목록.
        - 요청 범위가 단기·중기 예보 horizon(D+0..D+10) 밖이거나
        - 해당 날짜의 예보가 비어 있거나 값이 하나도 없거나
        - 중기 reg_id 가 매핑되지 않은 경우
        오름차순 정렬되어 들어간다.
    """

    province: str
    city: str
    region_fallback: bool = False
    short_term_base_at: datetime | None = None
    mid_land_tm_fc: datetime | None = None
    mid_temp_tm_fc: datetime | None = None
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
    # 이 값이 실제로 관측된 시각(ISO8601, KST). 미리 받아 둔 값을 내보내므로
    # 지금 시각과 다를 수 있다. 화면이 "몇 시 기준"을 밝힐 수 있어야 한다.
    observed_at: str | None = None


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
    # 이 값이 실제로 측정된 시각(ISO8601, KST). 위 observed_at 과 같은 뜻이다.
    observed_at: str | None = None


class WeatherNowResponse(BaseModel):
    """WeatherNowResponse — 현재 위치 기준 날씨 응답 본문

    /v1/weather/now 가 반환한다. 위경도로 받은 위치는 격자로 바꾼 뒤 버리고,
    응답에도 격자와 행정구역 명만 싣는다.

    값들은 hub 가 미리 받아 둔 것을 읽어 온다. 화면이 열릴 때 발급처를 부르지
    않으므로 발급처가 잠시 멈춰도 직전에 받아 둔 값으로 답한다. 다만 너무
    오래된 값은 지금 값이 아니므로, 그때는 해당 항목을 비워 보낸다 — 새벽
    기온을 지금 기온이라고 내보내는 것보다 그 자리를 비우는 편이 낫다.

    nx / ny: 요청 좌표가 속한 격자.
    province / city: 격자로 역조회한 행정구역. 매칭이 없으면 비어 있다.
    now: 지금 관측값. **받아 둔 값이 없거나 너무 오래됐으면 None** — 화면은
        이때 기온을 그리지 않는다.
    yesterday: 어제 같은 시간대 기록. 기록이 없으면 None — 화면은 이때
        비교 문구를 그리지 않는다.
    today: 오늘 예보 요약. 격자로 행정구역을 못 찾으면 None.
    air: 대기오염 정보. 받아 둔 값이 없거나 너무 오래됐으면 None.
    """

    nx: int
    ny: int
    province: str | None = None
    city: str | None = None
    now: WeatherNowObservation | None = None
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
    count: reviews 길이(편의 필드). 요청한 display 보다 작으면 그 구간이
        마지막이다 — 호출 측은 이 값으로 더보기를 멈춘다.
    start: 이 응답이 담은 구간의 시작 위치(요청값을 그대로 되돌려준다).
    """

    query: str
    reviews: list[ReviewItem]
    count: int
    start: int = 1


class PhotoAttribution(BaseModel):
    """PhotoAttribution — 사진 제공자 표기 1건

    사진을 올린 사람을 밝히는 값이다. 사진을 화면에 쓰는 쪽은 이 표기를
    함께 보여야 하므로, 이름이 없는 항목은 hub 가 미리 걸러 낸다.

    display_name: 제공자 이름.
    uri: 제공자 프로필 URL(없을 수 있음).
    """

    display_name: str
    uri: str | None = None


class PlacePhotoItem(BaseModel):
    """PlacePhotoItem — 장소 사진 1건

    photo_uri 는 요청할 때마다 새로 발급되는 짧은 수명의 이미지 URL 이다.
    저장하거나 다시 쓰지 않고 이번 응답에만 실어 보낸다.

    photo_uri: 이미지 URL.
    width_px / height_px: 원본 크기(없을 수 있음).
    attributions: 제공자 표기 목록.
    google_maps_uri: 원본 사진을 여는 지도 URL.
    flag_content_uri: 부적절한 사진을 신고하는 URL.
    """

    photo_uri: str
    width_px: int | None = None
    height_px: int | None = None
    attributions: list[PhotoAttribution] = Field(default_factory=list)
    google_maps_uri: str | None = None
    flag_content_uri: str | None = None


class PlacePhotosResponse(BaseModel):
    """PlacePhotosResponse — 장소 사진 조회 응답 본문

    query: 검색에 사용한 장소명.
    photos: 사진 리스트. 사진을 못 찾았거나 조회에 실패하면 빈 리스트다.
    count: photos 길이(편의 필드).
    """

    query: str
    photos: list[PlacePhotoItem]
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


class SubwayRouteStep(BaseModel):
    """SubwayRouteStep — 지하철 경로의 한 구간.

    type: 이동 방식. walk|subway|bus.
    line_name: 노선명. 걷는 구간에는 없다.
    start_name/end_name: 구간 양 끝 이름(역명 또는 출발지·도착지).
    section_time_min: 이 구간 소요 시간(분).
    station_count: 지나는 역 수. 걷는 구간에는 없다.
    """

    type: Literal["walk", "subway", "bus"]
    line_name: str | None = None
    start_name: str
    end_name: str
    section_time_min: int
    station_count: int | None = None


class SubwayRoute(BaseModel):
    """SubwayRoute — 지하철 단독 경로 한 건.

    total_time_min: 총 소요 시간(분). fare: 요금(원).
    transfer_count: 환승 횟수. total_walk_m: 총 도보 거리(m).
    steps: 구간 목록(출발 순서).
    """

    total_time_min: int
    fare: int
    transfer_count: int
    total_walk_m: int
    steps: list[SubwayRouteStep]


class SubwayRouteResponse(BaseModel):
    """SubwayRouteResponse — 지하철 경로 조회 응답 본문

    status: 조회 결과 구분.
        "ok"          — 경로를 찾았다. route 가 채워진다.
        "not_found"   — 지하철만으로 갈 수 있는 경로가 없다. route 는 null.
        "unavailable" — 외부 조회에 실패했거나 하루 한도를 넘겼다. route 는 null.
    route: status 가 "ok" 일 때만 채워진다.

    "경로 없음"과 "조회 불가"를 한 값으로 뭉치지 않는다. 화면이 둘을 다른
    문구로 보여주는데, 합쳐 두면 외부 장애가 "갈 수 있는 길이 없다"로
    표시되어 사용자가 잘못된 결론을 얻는다.
    """

    status: Literal["ok", "not_found", "unavailable"]
    route: SubwayRoute | None = None


class TransitRouteLeg(BaseModel):
    """TransitRouteLeg — 통합 길찾기 경로 후보의 한 구간.

    SubwayRouteStep 과 필드가 같되 geometry 가 더 있다. 지도에 그릴 수
    있게 이 구간이 지나는 [lat,lng] 좌표열을 순서대로 담는다. 좌표가 없는
    순수 도보 연결 구간은 빈 리스트다.
    """

    type: Literal["walk", "subway", "bus"]
    line_name: str | None = None
    start_name: str
    end_name: str
    section_time_min: int
    station_count: int | None = None
    geometry: list[list[float]] = []


class TransitRouteOption(BaseModel):
    """TransitRouteOption — 통합 길찾기 경로 후보 한 건.

    SubwayRoute 와 달리 지하철 단독으로 거르지 않은 후보다. modes 는 이
    경로에 실제 등장하는 이동수단(지하철·버스)을 순서대로 담아, 목록
    화면이 아이콘을 조립하지 않고 그대로 쓸 수 있게 한다.
    """

    total_time_min: int
    fare: int
    transfer_count: int
    total_walk_m: int
    modes: list[Literal["walk", "subway", "bus"]]
    legs: list[TransitRouteLeg]


class TransitRouteOptionsResponse(BaseModel):
    """TransitRouteOptionsResponse — 통합 길찾기 조회 응답 본문.

    status: SubwayRouteResponse 와 같은 세 값. "not_found" 는 routes 가
        빈 리스트임을 뜻하고, "unavailable" 도 마찬가지로 빈 리스트다 —
        두 상태 모두 화면에서 다른 문구로 보여줘야 하므로 status 로 구분한다.
    routes: 소요시간 오름차순, 최대 OdsayClient.ROUTE_OPTIONS_MAX 건.
    """

    status: Literal["ok", "not_found", "unavailable"]
    routes: list[TransitRouteOption] = []


class BikeStation(BaseModel):
    """BikeStation — 따릉이 대여소 한 곳.

    station_id: 대여소 식별자. name: 대여소 이름.
    rack_total: 거치대 수. parking_bike_total: 지금 세워져 있는 자전거 수.
    lat/lng: 대여소 좌표.
    """

    station_id: str
    name: str
    rack_total: int
    parking_bike_total: int
    lat: float
    lng: float


class BikeStationsResponse(BaseModel):
    """BikeStationsResponse — 따릉이 대여소 조회 응답 본문

    status: 조회 결과 구분.
        "ok"          — 조회에 성공했다. 주변에 대여소가 없으면 빈 목록이며
                        그것도 정상이다(서비스 지역 밖).
        "unavailable" — 외부 조회에 실패했다. stations 는 빈 목록.
    stations: 요청 좌표에서 radius_m 안에 있는 대여소.
    count: stations 길이(편의 필드).
    """

    status: Literal["ok", "unavailable"]
    stations: list[BikeStation]
    count: int


class PmVehicle(BaseModel):
    """PmVehicle — 공유 킥보드 한 대.

    provider: 사업자명. device_id: 기기 식별자.
    battery_level: 배터리 잔량(%). 발급처가 안 줄 수 있다.
    vehicle_type: 기기 종류 표기. 빈 문자열일 수 있다.
    lat/lng: 기기 좌표.
    """

    provider: str
    device_id: str
    battery_level: int | None = None
    vehicle_type: str = ""
    lat: float
    lng: float


class PmVehiclesResponse(BaseModel):
    """PmVehiclesResponse — 공유 킥보드 조회 응답 본문

    status: 조회 결과 구분.
        "ok"          — 조회에 성공했다. 주변에 기기가 없으면 빈 목록이며
                        그것도 정상이다.
        "unavailable" — 사업자 전부에서 조회에 실패했다. vehicles 는 빈 목록.
    vehicles: 요청 좌표에서 radius_m 안에 있는 기기.
    count: vehicles 길이(편의 필드).

    사업자별로 따로 물어 합치므로 일부 사업자만 실패할 수 있다. 그 경우는
    받은 만큼으로 "ok" 를 낸다 — 한 사업자의 장애로 나머지가 함께 사라지면
    화면이 실제보다 비어 보인다.
    """

    status: Literal["ok", "unavailable"]
    vehicles: list[PmVehicle]
    count: int
