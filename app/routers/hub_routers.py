"""hub-service 라우터 모듈.

외부 데이터 조회 라우터를 본 모듈에 정의한다.
장소·날씨·리뷰·룰 도메인 중 장소(/v1/places)·날씨(/v1/weather)·
리뷰(/v1/reviews) 엔드포인트를 본 모듈에 정의한다.

호출 관계:
  - GET /v1/weather (get_weather) 는 agent 의 HubClient.fetch_weather 가
    호출하는 public API 엔드포인트이다.
  - 내부적으로 forecast_repo 의 lookup_region_by_name / fetch_* 를
    호출해 raw row 를 모은 뒤, _aggregate_* 헬퍼로 일별 집계한다.
  - GET /v1/places (get_places) 는 카카오 점 장소와 두루누비 코스를
    병합 조회하는 public API 엔드포인트이다.
  - GET /v1/reviews (get_reviews) 는 네이버 블로그 검색 결과를 리뷰로
    노출하는 public API 엔드포인트이다.
  - GET /v1/places/photos (get_place_photos) 는 장소명과 좌표로 사진을
    조회하는 public API 엔드포인트이다. 리뷰 조회와 분리해 둔 이유는
    사진 조회에만 건당 과금이 붙기 때문이다 — 합쳐 두면 요약·더보기처럼
    사진이 필요 없는 호출까지 과금을 일으킨다.
  - GET /v1/transit/subway (get_subway_route) 는 두 좌표 사이의 지하철
    단독 경로를 조회하는 public API 엔드포인트이다.
  - GET /v1/transit/routes (get_transit_routes) 는 같은 두 좌표에 대해
    지하철 전용 필터 없이 버스·혼합 경로까지 소요시간 순으로 나열하는
    public API 엔드포인트이다 — "이동수단을 모두 보여주는" 통합 길찾기용.
  - GET /v1/mobility/bike-stations (get_bike_stations) 는 좌표 주변의
    따릉이 대여소 현황을 조회하는 public API 엔드포인트이다.

응답 모델:
  - WeatherDailyItem / WeatherResponse (app.schemas.hub_schemas)
  - PlaceItem / PlacesResponse
  - ReviewItem / ReviewsResponse
  - PlacePhotoItem / PlacePhotosResponse
  - SubwayRoute / SubwayRouteResponse
  - TransitRouteOption / TransitRouteOptionsResponse
  - BikeStation / BikeStationsResponse
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Request, APIRouter, Depends, HTTPException, Query

from app.clients.hub_clients import (
    GooglePlacesApiError,
    KakaoApiError,
    NaverApiError,
    OdsayApiError,
    OdsayClient,
    OsrmApiError,
    PmApiError,
    SeoulBikeApiError,
)
from app.codes.air_codes import grade_pm10, grade_pm25, sido_name
from app.codes.kma_codes import label_sky
from app.config import settings
from app.routers.location_params import resolve_legs, resolve_pair, resolve_point
from app.db.forecast_repo import (
    RegionLookup,
    fetch_mid_land_range,
    fetch_mid_temp_range,
    fetch_nowcast_snapshot,
    fetch_recent_air,
    fetch_recent_nowcast,
    fetch_short_term_range,
    load_sido_grids,
    lookup_region_by_grid,
    lookup_region_by_name,
)
from app.db.places_repo import (
    lookup_region_centroid,
    search_courses_nearby,
)
from app.hub_dependencies import (
    get_google_client,
    get_kakao_client,
    get_naver_client,
    get_odsay_client,
    get_odsay_fallback_client,
    get_osrm_client,
    get_place_cache,
    get_pm_client,
    get_seoul_bike_client,
)
from app.place_stubs import (
    google_photos_stub,
    kakao_keyword_stub,
    naver_blog_stub,
    places_stub_active,
    pm_vehicle_stub,
    seoul_bike_stub,
)
from app.route_stubs import (
    osrm_route_stub,
    routing_stub_active,
    subway_route_stub,
    transit_routes_stub,
    transit_stub_active,
)
from app.routers.guards import public_guard
# 대여소를 요청 좌표 주변으로 잘라낼 때 쓴다. 룰 엔진이 후보를 반경으로
# 거르는 데 쓰는 것과 같은 함수라, 거리 계산을 두 벌 두지 않는다.
from app.rules.rule_engine import haversine_m
from app.schemas.hub_schemas import (
    BikeStation,
    BikeStationsResponse,
    DirectionsBatchRequest,
    DirectionsBatchResponse,
    DirectionsLeg,
    DirectionsRoute,
    PlaceItem,
    PlacePhotoItem,
    PlacePhotosResponse,
    PlacesResponse,
    PmVehicle,
    PmVehiclesResponse,
    ReviewItem,
    ReviewsResponse,
    SubwayRoute,
    SubwayRouteResponse,
    TransitRouteOption,
    TransitRouteOptionsResponse,
    WeatherDailyItem,
    WeatherNowAir,
    WeatherNowObservation,
    WeatherNowResponse,
    WeatherNowToday,
    WeatherNowYesterday,
    WeatherResponse,
)
from app.utils.kma_grid import gps_to_grid

logger = logging.getLogger(__name__)

# 본 모듈의 모든 라우트를 묶는 APIRouter. main 앱에서 include_router 로 등록.
# public_guard 는 AUTH_ENFORCED=false 면 no-op 이므로 기본 데모 동작을 유지하고,
# true 면 모든 /v1/* 라우트에 X-Internal-Token 을 요구한다(/health 는 제외 —
# 라우터가 아닌 app 에 직접 선언되어 있어 본 의존성 밖).
router = APIRouter(dependencies=[Depends(public_guard)])

# 한국 표준시 타임존. KMA 예보 시각은 KST 기준이므로 일자 비교/오프셋 계산은
# 반드시 본 타임존을 거쳐야 한다.
_KST = ZoneInfo("Asia/Seoul")

# 한 번에 요청 가능한 최대 날짜 범위(days). (date_end - date_start) > 14 면
# 400 으로 거절한다 — 과도한 row 스캔 방지.
_MAX_RANGE_DAYS = 14

# 단기예보 horizon. D+0..D+2 는 단기예보만으로 응답한다.
_SHORT_TERM_MAX_OFFSET = 2  # D+0..D+2

# D+3 은 두 출처가 겹치는 구간이다. 단기예보에 그 날의 완전한 하루가 들어
# 있으면 단기를 쓰고, 잘려 있으면 중기로 넘긴다. KMA 단기예보는 발표시각에
# 따라 D+3 중반까지만 담겨 오므로 어느 쪽이 채워질지는 조회 시각에 달렸다.
_OVERLAP_OFFSET = 3

# 중기예보 horizon 상한. D+10 까지 응답 대상으로 삼는다.
_MID_MAX_OFFSET = 10

# 중기예보 테이블에 실제로 쌓이는 offset 범위(발표일 기준 D+N 의 N).
# KMA 중기예보는 06 시 발표가 D+4 부터, 18 시 발표가 D+5 부터 담겨 오며
# D+3 은 어느 발표분에도 없다. 조회는 이 범위로 고정하고, 요청 날짜와의
# 대응은 발표일을 기준으로 역산한다.
_MID_STORED_MIN_OFFSET = 4

# 강수확률로 인정하는 값의 범위(%). 밖의 값은 결측으로 떨어뜨린다.
_POP_MIN = 0
_POP_MAX = 100

# KMA 격자판의 유효 범위. 허용 위경도 사각형이 이 범위보다 넓어서,
# 남동쪽 모서리 좌표는 격자로 바꾸면 판을 벗어난다.
_GRID_NX_MIN, _GRID_NX_MAX = 1, 149
_GRID_NY_MIN, _GRID_NY_MAX = 1, 253


def _today_kst() -> date:
    """_today_kst — KST 기준 오늘 일자

    현재 시각을 _KST 타임존으로 인식해 date 부분만 잘라 반환한다.
    날짜 오프셋(D+N) 계산의 기준점으로 사용되므로 절대 utcnow 로
    대체해서는 안 된다(자정 부근 1일 어긋남 발생).
    """
    return datetime.now(tz=_KST).date()


def _coerce_int(value: str | int | None) -> int | None:
    """_coerce_int — 예보값 문자열을 안전하게 int 로 변환

    short_term_forecast 의 fcst_value 는 문자열로 저장되며 "21.0" 같이
    소수점이 붙은 형태로 올 수 있다. 이를 int 로 다루기 위해 한 번
    float 를 거친 뒤 반올림한다. 0 방향으로 잘라내면 영하 기온이 늘
    실제보다 따뜻하게 표시된다(-3.7 → -3).

    value: 변환 대상. str | int | None
        - None 또는 빈 문자열("") 이면 None 반환(누락 데이터 표현)
        - 변환에 실패하면(TypeError, ValueError) 역시 None 반환
    반환: int 또는 None.

    사용처: _aggregate_short_term 의 TMN/TMX/TMP/POP 추출 단계.
    """
    if value is None or value == "":
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _valid_pop(value: int | None) -> int | None:
    """_valid_pop — 강수확률로 인정할 수 있는 값만 통과시킨다

    외부 응답에는 결측을 뜻하는 관례값(-9)이나 규격 밖의 큰 수가 섞여
    들어올 수 있다. 그대로 응답 모델에 넣으면 0~100 제약에 걸려 요청
    전체가 실패하므로, 범위 밖 값은 결측으로 떨어뜨린다.

    반환: 0..100 이면 그대로, 그 밖이거나 None 이면 None.
    """
    if value is None:
        return None
    if _POP_MIN <= value <= _POP_MAX:
        return value
    logger.warning("precipitation prob out of range value=%s", value)
    return None


def _aggregate_short_term(
    rows: list[dict], day: date, *, require_full: bool = False
) -> WeatherDailyItem | None:
    """_aggregate_short_term — 단기예보 raw row 를 하루 한 칸으로 집계

    fetch_short_term_range 가 돌려준 카테고리 혼합 row 리스트에서
    특정 day 에 해당하는 row 만 골라 WeatherDailyItem 한 건을 만든다.

    rows: fetch_short_term_range 결과. 각 dict 는 date/category/
        fcst_value/fcst_at 키를 가진다.
    day: 집계 대상 KST 일자.
    require_full: 하루가 통째로 담긴 날만 받아들일지 여부. 키워드 전용
        이며 기본값은 종전 동작(부분 데이터도 채택)이다.

    집계 규칙:
        temp_min:
          1순위 — 같은 날 첫 TMN row 의 값
          2순위 — TMN 이 없으면 같은 날 TMP 값들의 min
        temp_max:
          1순위 — 같은 날 첫 TMX row 의 값
          2순위 — TMX 가 없으면 같은 날 TMP 값들의 max
        precipitation_prob:
          같은 날 POP 값들의 max (가장 비관적인 강수확률 채택)
        sky_condition:
          같은 날 SKY row 중 KST 정오(12:00)에 가장 가까운 시각의
          fcst_value 를 label_sky 로 변환. 코드가 알려진 값이 아니면
          그 다음으로 가까운 시각을 순서대로 시도한다
        source: 항상 "short_term"

    require_full=True 일 때:
        일 최저·최고가 **둘 다** 제 값(TMN/TMX)으로 있어야 채택한다.
        판정은 TMP 대체를 적용하기 **전에** 한다 — 단기예보의 마지막
        날은 뒤가 잘려 오는데, 남은 시간대만의 min/max 는 그 날의
        최저·최고가 아니다. 예컨대 새벽까지만 담긴 날은 최고기온이
        실제보다 크게 낮아진다.

    반환:
        해당 day 의 row 가 하나도 없으면 None.
        집계 결과가 전부 결측이어도 None — 값 없는 칸을 응답에 넣으면
        소비자가 "예보가 있다"고 오인하고 결측 목록에도 안 잡힌다.
        그 밖에는 WeatherDailyItem(가능한 필드만 채움).

    도메인 용어:
        TMN/TMX: 일 최저/최고 기온
        TMP: 시간별 기온
        POP: 강수 확률(%)
        SKY: 하늘 상태 코드 (label_sky 로 한글 변환)
    """
    day_rows = [r for r in rows if r["date"] == day]
    if not day_rows:
        return None

    tmn = [_coerce_int(r["fcst_value"]) for r in day_rows
           if r["category"] == "TMN"]
    tmx = [_coerce_int(r["fcst_value"]) for r in day_rows
           if r["category"] == "TMX"]
    tmp = [_coerce_int(r["fcst_value"]) for r in day_rows
           if r["category"] == "TMP"]
    pop = [_coerce_int(r["fcst_value"]) for r in day_rows
           if r["category"] == "POP"]
    sky_rows = [r for r in day_rows if r["category"] == "SKY"]

    temp_min = next((v for v in tmn if v is not None), None)
    temp_max = next((v for v in tmx if v is not None), None)
    if require_full and (temp_min is None or temp_max is None):
        return None

    if temp_min is None and tmp:
        tmp_valid = [v for v in tmp if v is not None]
        temp_min = min(tmp_valid) if tmp_valid else None
    if temp_max is None and tmp:
        tmp_valid = [v for v in tmp if v is not None]
        temp_max = max(tmp_valid) if tmp_valid else None
    pop_valid = [
        v for v in (_valid_pop(p) for p in pop) if v is not None
    ]
    precipitation_prob = max(pop_valid) if pop_valid else None

    sky_condition = None
    if sky_rows:
        noon = datetime.combine(day, datetime.min.time(), tzinfo=_KST).replace(
            hour=12
        )
        sky_rows.sort(
            key=lambda r: abs((r["fcst_at"] - noon).total_seconds())
        )
        for row in sky_rows:
            sky_condition = label_sky(row["fcst_value"])
            if sky_condition is not None:
                break

    if (
        temp_min is None
        and temp_max is None
        and precipitation_prob is None
        and sky_condition is None
    ):
        return None

    return WeatherDailyItem(
        date=day,
        temp_min=temp_min,
        temp_max=temp_max,
        precipitation_prob=precipitation_prob,
        sky_condition=sky_condition,
        source="short_term",
    )


def _aggregate_mid(
    land_rows: list[dict],
    temp_rows: list[dict],
    land_offset: int | None,
    temp_offset: int | None,
    day: date,
) -> WeatherDailyItem | None:
    """_aggregate_mid — 중기 육상+기온 row 를 하루 한 칸으로 집계

    fetch_mid_land_range / fetch_mid_temp_range 결과를 합쳐 특정 날짜의
    WeatherDailyItem 한 건을 만든다.

    land_rows: fetch_mid_land_range 결과. offset/am_pm/weather/
        rain_prob_pct 키를 가짐
    temp_rows: fetch_mid_temp_range 결과. offset/ta_min/ta_max 키를 가짐
    land_offset: land_rows 가 속한 발표일 기준으로 환산한 day 의 D+N.
        발표분이 없으면 None
    temp_offset: temp_rows 쪽 발표일 기준으로 환산한 day 의 D+N.
        발표분이 없으면 None
    day: 응답에 채울 KST 일자

    육상과 기온은 각자 최신 발표분을 따로 고르므로 두 발표 시각이 다를
    수 있다(한쪽 폴링만 성공한 경우). 그래서 offset 을 하나로 합쳐 쓰지
    않고 각자의 발표일 기준으로 받아, **같은 대상일끼리만** 합친다.
    하나로 쓰면 하루 어긋난 기온이 다른 날의 하늘상태에 붙는다.

    집계 규칙:
        temp_min / temp_max: temp_rows 에서 temp_offset 이 같은 row 의
            ta_min / ta_max. row 가 없으면 None
        precipitation_prob: land_offset 이 같은 row 들의 AM/PM 강수확률
            중 max (가장 비관적인 값 채택)
        sky_condition: 같은 land_offset row 의 weather 텍스트 중 첫
            번째 값. KMA 원문(예: "구름많음") 그대로 노출
        source: 실제로 값을 채운 출처를 밝힌다 — "mid_land",
            "mid_temp", "mid_land+mid_temp" 중 하나

    반환:
        채울 값이 하나도 없으면 None.
    """
    day_land = (
        [r for r in land_rows if r["offset"] == land_offset]
        if land_offset is not None
        else []
    )
    day_temp = (
        next(
            (r for r in temp_rows if r["offset"] == temp_offset), None
        )
        if temp_offset is not None
        else None
    )

    rain_values = [
        v for v in (
            _valid_pop(r["rain_prob_pct"]) for r in day_land
        ) if v is not None
    ]
    precipitation_prob = max(rain_values) if rain_values else None
    weathers = [r["weather"] for r in day_land if r["weather"]]
    sky_condition = weathers[0] if weathers else None
    temp_min = day_temp["ta_min"] if day_temp else None
    temp_max = day_temp["ta_max"] if day_temp else None

    has_land = precipitation_prob is not None or sky_condition is not None
    has_temp = temp_min is not None or temp_max is not None
    if not has_land and not has_temp:
        return None
    if has_land and has_temp:
        source = "mid_land+mid_temp"
    elif has_land:
        source = "mid_land"
    else:
        source = "mid_temp"

    return WeatherDailyItem(
        date=day,
        temp_min=temp_min,
        temp_max=temp_max,
        precipitation_prob=precipitation_prob,
        sky_condition=sky_condition,
        source=source,
    )


def _split_dates_by_horizon(
    date_start: date, date_end: date, today: date
) -> tuple[list[date], list[date], list[date], list[date]]:
    """_split_dates_by_horizon — 요청 날짜를 horizon 별로 4 분할

    [date_start, date_end] 범위의 각 날짜를 today 기준 D+N 으로 환산해
    단기/겹침/중기/범위밖 네 그룹으로 나눈다.

    date_start / date_end: 요청 구간(양끝 포함).
    today: 기준 일자 (_today_kst() 결과).

    분류 기준:
        0 <= offset <= _SHORT_TERM_MAX_OFFSET (D+0..D+2) → short
        offset == _OVERLAP_OFFSET (D+3) → overlap
            단기예보에 하루가 온전히 들어 있으면 단기, 아니면 중기.
            KMA 중기예보는 어느 발표분에도 D+3 이 없지만, 발표가 하루
            지난 상태에서는 그 발표분의 D+4 가 오늘의 D+3 에 해당해
            중기로도 채워질 수 있다
        offset <= _MID_MAX_OFFSET (D+4..D+10) → mid
        그 외(과거 일자 또는 D+11 이후) → out_of_range
            → 응답의 missing_dates 에 그대로 들어감

    반환: (short, overlap, mid, out_of_range) 네 리스트의 튜플.
        각 리스트는 입력 순서(오름차순)를 그대로 유지하며, 한 날짜는
        정확히 한 리스트에만 들어간다.
    """
    short: list[date] = []
    overlap: list[date] = []
    mid: list[date] = []
    out_of_range: list[date] = []
    cursor = date_start
    while cursor <= date_end:
        offset = (cursor - today).days
        if 0 <= offset <= _SHORT_TERM_MAX_OFFSET:
            short.append(cursor)
        elif offset == _OVERLAP_OFFSET:
            overlap.append(cursor)
        elif _OVERLAP_OFFSET < offset <= _MID_MAX_OFFSET:
            mid.append(cursor)
        else:
            out_of_range.append(cursor)
        cursor += timedelta(days=1)
    return short, overlap, mid, out_of_range


def _tm_fc_kst_date(tm_fc: datetime | None) -> date | None:
    """_tm_fc_kst_date — 발표 시각을 KST 기준 발표 일자로 환산

    tm_fc 는 TIMESTAMPTZ 라 드라이버가 UTC 로 인식된 값을 돌려준다.
    KST 06 시 발표는 UTC 로 전날 21 시이므로, 변환 없이 date 를 취하면
    발표일이 하루 앞당겨져 모든 중기 예보가 하루 밀린다.

    반환: KST 기준 발표 일자. tm_fc 가 None 이면 None.
    """
    if tm_fc is None:
        return None
    return tm_fc.astimezone(_KST).date()


@router.get("/v1/weather", response_model=WeatherResponse)
async def get_weather(
    province: str = Query(..., min_length=1, max_length=20),
    city: str = Query(..., min_length=1, max_length=20),
    date_start: date = Query(...),
    date_end: date = Query(...),
) -> WeatherResponse:
    """GET /v1/weather — 광역시도+시군구 + 날짜 구간에 대한 날씨 응답

    agent 의 HubClient.fetch_weather 가 호출하는 public 엔드포인트.
    단기/중기 두 출처를 호라이즌(horizon)에 따라 자동으로 섞어 응답한다.

    Query 파라미터:
        province: 광역시도 명 (1~20 글자). 예: "서울특별시"
        city: 시군구 명 (1~20 글자). 예: "강남구"
        date_start / date_end: 조회 구간(양끝 포함, KST 일자).

    검증 단계:
        1) date_start > date_end → 400 ("date_start must be <= date_end")
        2) 구간이 _MAX_RANGE_DAYS(14)일 초과 → 400
        3) 구간 전체가 과거 → 400
        4) lookup_region_by_name 결과 None → 404 ("region not found")

    처리 흐름:
        1) lookup_region_by_name 으로 RegionLookup 확보 (nx/ny + reg_id)
        2) _today_kst 와 _split_dates_by_horizon 으로
           단기/겹침/중기/범위밖 분할
        3) 단기·겹침 날짜가 있으면 fetch_short_term_range 로 일괄 조회
        4) 겹침·중기 날짜가 있고 두 reg_id 모두 존재하면
           fetch_mid_land_range / fetch_mid_temp_range 호출.
           조회 범위는 저장 범위로 고정하고, 반환된 발표 시각으로
           요청 날짜에 해당하는 offset 을 역산한다
        5) 단기 날짜: _aggregate_short_term
        6) 겹침 날짜(D+3): 단기를 완전성 조건으로 먼저 시도하고,
           실패하면 중기로 넘긴다. 둘 다 없으면 missing 에 한 번만 적재
        7) 중기 날짜: reg_id 누락 시 missing, 아니면 _aggregate_mid
        8) daily 는 date 오름차순 정렬, missing 도 오름차순 정렬
        9) 응답 province/city 는 RegionLookup.lv1/lv2 기준.
           lv2 가 빈 문자열(광역 대체)이면 요청 city 를 그대로 쓰되
           region_fallback 을 세워 대체 사실을 알린다.

    response_model: WeatherResponse — 직렬화·검증을 본 모델로 강제.
    """
    if date_start > date_end:
        raise HTTPException(
            status_code=400, detail="date_start must be <= date_end"
        )
    if (date_end - date_start).days >= _MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"date range must be <= {_MAX_RANGE_DAYS} days",
        )

    today = _today_kst()
    if date_end < today:
        raise HTTPException(
            status_code=400, detail="date range is entirely in the past"
        )

    region: RegionLookup | None = await lookup_region_by_name(
        province, city
    )
    if region is None:
        raise HTTPException(status_code=404, detail="region not found")

    short_days, overlap_days, mid_days, out_of_range = (
        _split_dates_by_horizon(date_start, date_end, today)
    )

    # 겹침 날짜는 단기 조회 대상에도 포함된다 — 그 날의 단기 데이터가
    # 온전하면 중기보다 정밀하기 때문이다.
    short_lookup_days = short_days + overlap_days
    short_rows: list[dict] = []
    short_base_at: datetime | None = None
    if short_lookup_days:
        short_rows, short_base_at = await fetch_short_term_range(
            region.nx, region.ny,
            short_lookup_days[0], short_lookup_days[-1],
        )

    land_rows: list[dict] = []
    temp_rows: list[dict] = []
    land_tm_fc: datetime | None = None
    temp_tm_fc: datetime | None = None
    mid_lookup_days = overlap_days + mid_days
    have_mid_codes = bool(
        region.mid_land_reg_id and region.mid_temp_reg_id
    )
    if mid_lookup_days and have_mid_codes:
        land_rows, land_tm_fc = await fetch_mid_land_range(
            region.mid_land_reg_id,
            _MID_STORED_MIN_OFFSET,
            _MID_MAX_OFFSET,
        )
        temp_rows, temp_tm_fc = await fetch_mid_temp_range(
            region.mid_temp_reg_id,
            _MID_STORED_MIN_OFFSET,
            _MID_MAX_OFFSET,
        )
    land_base = _tm_fc_kst_date(land_tm_fc)
    temp_base = _tm_fc_kst_date(temp_tm_fc)

    def _mid_item(day: date) -> WeatherDailyItem | None:
        """요청 날짜를 각 발표일 기준 offset 으로 바꿔 중기를 집계한다."""
        if not have_mid_codes:
            return None
        land_offset = (day - land_base).days if land_base else None
        temp_offset = (day - temp_base).days if temp_base else None
        return _aggregate_mid(
            land_rows, temp_rows, land_offset, temp_offset, day
        )

    daily: list[WeatherDailyItem] = []
    missing: list[date] = list(out_of_range)
    for day in short_days:
        item = _aggregate_short_term(short_rows, day)
        if item is None:
            missing.append(day)
        else:
            daily.append(item)
    for day in overlap_days:
        item = _aggregate_short_term(
            short_rows, day, require_full=True
        )
        if item is None:
            item = _mid_item(day)
        if item is None:
            missing.append(day)
        else:
            daily.append(item)
    for day in mid_days:
        item = _mid_item(day)
        if item is None:
            missing.append(day)
        else:
            daily.append(item)
    daily.sort(key=lambda x: x.date)
    missing.sort()

    return WeatherResponse(
        province=region.lv1,
        city=region.lv2 or city,
        region_fallback=not region.lv2,
        short_term_base_at=short_base_at,
        mid_land_tm_fc=land_tm_fc,
        mid_temp_tm_fc=temp_tm_fc,
        daily=daily,
        missing_dates=missing,
    )


def _pick_air_station(
    items: list[dict], city: str | None = None
) -> dict | None:
    """시도 측정소 목록에서 대표 한 곳을 고른다.

    사용자의 시군구에 있는 측정소를 먼저 찾는다. 시도는 넓어서 아무 측정소나
    고르면 반대편 도시의 농도를 보여 주게 된다. 측정소 이름에 시군구 이름이
    들어가는 경우가 흔해(예: 강남구 → "강남구") 이름 일치로 좁힌다.

    그 안에서는 미세먼지·초미세먼지 값이 둘 다 유효한 곳을 우선한다. 점검
    중이거나 통신 장애인 측정소는 값이 비거나 "-" 로 오는데, 그런 곳을
    대표로 쓰면 농도가 통째로 비어 버린다.

    시군구 안에 쓸 만한 측정소가 없으면 시도 전체에서 고른다. 먼 측정소라도
    보여 주는 편이 값이 아예 없는 것보다 낫다. 하나도 없으면 None.
    """

    def _both(pool: list[dict]) -> dict | None:
        for it in pool:
            if _coerce_int(it.get("pm10Value")) is not None and \
                    _coerce_int(it.get("pm25Value")) is not None:
                return it
        return None

    def _either(pool: list[dict]) -> dict | None:
        for it in pool:
            if _coerce_int(it.get("pm10Value")) is not None:
                return it
        return None

    pools: list[list[dict]] = []
    if city:
        near = [
            it for it in items
            if isinstance(it.get("stationName"), str)
            and city in it["stationName"]
        ]
        if near:
            pools.append(near)
    pools.append(items)
    for pool in pools:
        picked = _both(pool) or _either(pool)
        if picked is not None:
            return picked
    return None


async def _fetch_recent_snapshot(
    base_day: date, base_hour: int, nx: int, ny: int
) -> dict | None:
    """_fetch_recent_snapshot — 비교용 과거 실황을 하루씩 거슬러 찾는다

    어제 같은 시간대를 먼저 보고, 없으면 보관 기간 안에서 하루씩 더
    거슬러 올라간다. 스냅샷은 사용자가 그 격자에서 조회했을 때만 남으므로
    어제 기록이 비어 있는 일이 흔하고, 하루만 보고 포기하면 비교가 자주
    끊긴다. 보관 기간을 며칠로 둔 이유도 여기에 있다.

    반환: 처음 찾은 스냅샷 dict(temp_c/hour_kst). 없으면 None.
    """
    for back in range(1, settings.WEATHER_SNAPSHOT_RETENTION_DAYS + 1):
        prev = await fetch_nowcast_snapshot(
            base_day - timedelta(days=back), base_hour, nx, ny
        )
        if prev is not None:
            return prev
    return None


async def _stored_air(
    province: str | None, city: str | None = None
) -> WeatherNowAir | None:
    """저장해 둔 대기오염 측정값을 읽는다. 신선한 기록이 없으면 None.

    발급처를 여기서 부르지 않는다. 매시 미리 받아 두므로 화면이 열릴 때는
    읽기만 하면 되고, 그래서 발급처가 느리거나 멈춰 있어도 이 조회가 늦어
    지지 않는다. 발급처가 오래 멈춰 마지막 측정분이 낡으면 아무것도 주지
    않는다 — 어제 농도를 지금 농도로 보여 주는 것보다 비우는 편이 낫다.

    측정소는 요청한 시군구 이름에 가까운 곳을 고른다. 없으면 값이 있는
    측정소 중 하나를 쓴다(같은 시도라 크게 다르지 않다).
    """
    sido = sido_name(province)
    if sido is None:
        return None
    rows = await fetch_recent_air(sido, settings.AIR_MAX_AGE_HOURS)
    if not rows:
        return None
    # 저장 형태를 기존 선택 로직이 읽는 모양으로 맞춘다.
    items = [
        {
            "stationName": r["station_name"],
            "pm10Value": r["pm10"],
            "pm25Value": r["pm25"],
        }
        for r in rows
    ]
    station = _pick_air_station(items, city)
    if station is None:
        return None
    pm10 = _coerce_int(station.get("pm10Value"))
    pm25 = _coerce_int(station.get("pm25Value"))
    observed = _to_kst(rows[0]["data_time"])
    return WeatherNowAir(
        pm10=pm10,
        pm25=pm25,
        pm10_grade=grade_pm10(pm10),
        pm25_grade=grade_pm25(pm25),
        station=station.get("stationName"),
        observed_at=observed.isoformat() if observed is not None else None,
    )


def _to_kst(value: datetime | None) -> datetime | None:
    """저장소가 돌려준 시각을 KST 로 옮긴다.

    저장소는 시각을 UTC 로 돌려준다. 화면에 내보내는 "몇 시 기준"과 발표
    일자·시각은 KST 로 세는 값이라, 옮기지 않으면 아홉 시간 어긋난다.
    시각대가 없는 값이 오면 이미 KST 로 적힌 것으로 본다.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=_KST)
    return value.astimezone(_KST)


async def sido_representative_grid(
    province: str | None,
) -> tuple[int, int] | None:
    """시도의 대표 격자를 찾는다. 못 찾으면 None.

    사용자 좌표의 격자에 실황이 없을 때 갈음할 자리다. 매시 받아 두는 대상이
    시도 대표 격자뿐이라, 그 밖의 격자는 언제나 여기로 넘어온다.
    """
    if not province:
        return None
    for g in await load_sido_grids():
        # 대표 격자의 이름은 시도 이름 그대로다. 강원처럼 둘로 나뉜 시도는
        # 이름 뒤에 괄호가 붙어, 앞부분만 맞춰 본다.
        if g.label == province or g.label.startswith(province):
            return g.nx, g.ny
    return None


@router.get("/v1/weather/now", response_model=WeatherNowResponse)
async def get_weather_now(
    request: Request,
    loc: str | None = Query(None, description="감싼 좌표"),
    lat: float | None = Query(None, ge=33.0, le=43.0),
    lng: float | None = Query(None, ge=124.0, le=132.0),
) -> WeatherNowResponse:
    """GET /v1/weather/now — 좌표 기준 현재 날씨

    홈 화면의 날씨 카드가 필요로 하는 값을 한 번에 모은다. 지금 기온과
    하늘 상태는 실황에서, 최고·최저와 강수 확률은 단기예보에서, 미세먼지는
    대기오염 정보에서 온다.

    **읽기만 한다.** 세 값 모두 hub 가 매시 미리 받아 둔 것을 저장소에서
    꺼내 온다. 화면이 열릴 때 발급처를 부르지 않으므로 발급처가 느리거나
    멈춰 있어도 이 조회는 빠르고, 직전에 받아 둔 값으로 답한다.

    받아 둔 값이 너무 오래됐으면 그 항목을 비워 보낸다. 새벽 기온을 지금
    기온이라고 내보내는 것보다 그 자리를 비우는 편이 낫다. 얼마나 오래된
    것까지 쓸지는 설정(WEATHER_NOW_MAX_AGE_HOURS / AIR_MAX_AGE_HOURS)에 있다.

    Query 파라미터:
        lat / lng: 기기 위치. 국내 범위 밖이면 422(FastAPI 검증).

    위치 취급:
        받은 좌표는 진입 직후 격자로 바꾸고 그 뒤로는 격자만 쓴다. 로그와
        저장 레코드 어디에도 원시 좌표가 남지 않는다.

    처리 흐름:
        1) 좌표 → 격자 변환, 격자로 행정구역 역조회
        2) 그 격자의 실황을 저장소에서 읽는다. 없으면 같은 시도의 대표
           격자 값으로 갈음한다 — 지금 기온은 시도 안에서 크게 갈리지 않는다
        3) 어제 같은 시간대 기록을 찾아 비교값으로 싣는다(없으면 생략)
        4) 오늘 단기예보를 집계해 최고·최저·강수확률·하늘상태를 채운다
        5) 대기오염을 저장소에서 읽어 농도와 등급을 채운다(없으면 생략)

    response_model: WeatherNowResponse — 직렬화·검증을 본 모델로 강제.
    """
    # 좌표는 감싸서 온다. 여는 데 실패하면 여기서 요청이 끝난다 —
    # 평문으로 물러서면 감싸는 쪽이 고장 나도 아무도 알아차리지 못한다.
    lat, lng = resolve_point(request, loc, lat, lng)
    nx, ny = gps_to_grid(lat, lng)
    # 허용 위경도 사각형의 모서리는 격자 격자판 밖으로 벗어난다.
    if not (_GRID_NX_MIN <= nx <= _GRID_NX_MAX) or not (
        _GRID_NY_MIN <= ny <= _GRID_NY_MAX
    ):
        raise HTTPException(
            status_code=422,
            detail="coordinates outside KMA grid coverage",
        )
    now_kst = datetime.now(tz=_KST)

    region = await lookup_region_by_grid(nx, ny)

    async def _observation() -> tuple[WeatherNowObservation | None, float | None]:
        """저장된 실황을 읽는다. 요청 격자에 없으면 시도 대표 격자로 갈음."""
        max_age = settings.WEATHER_NOW_MAX_AGE_HOURS
        snap = await fetch_recent_nowcast(nx, ny, max_age)
        if snap is None and region is not None:
            rep = await sido_representative_grid(region.lv1)
            if rep is not None and rep != (nx, ny):
                snap = await fetch_recent_nowcast(rep[0], rep[1], max_age)
        if snap is None:
            return None, None
        # 저장소는 시각을 UTC 기준으로 돌려준다. 발표 일자·시각은 KST 로
        # 세는 값이라 여기서 옮겨 놓지 않으면 아홉 시간 어긋난 시각이
        # 그대로 화면에 나가고, "어제 이맘때" 비교도 엉뚱한 시각을 찾는다.
        observed = _to_kst(snap["observed_at"])
        return (
            WeatherNowObservation(
                temp_c=snap["temp_c"],
                pty=snap["pty"],
                base_date=observed.strftime("%Y%m%d"),
                base_time=observed.strftime("%H00"),
                observed_at=observed.isoformat(),
            ),
            snap["temp_c"],
        )

    async def _yesterday(base_day, base_hour) -> WeatherNowYesterday | None:
        prev = await _fetch_recent_snapshot(base_day, base_hour, nx, ny)
        if prev is None:
            return None
        return WeatherNowYesterday(
            temp_c=prev["temp_c"], hour_kst=prev["hour_kst"]
        )

    async def _today() -> WeatherNowToday | None:
        if region is None:
            return None
        # 예보는 벽시계 오늘로 본다. 실황 발표분은 자정 직후 전날이 되므로
        # 그 날짜로 조회하면 어제 예보가 오늘 자리에 실린다.
        today_kst = now_kst.date()
        rows, _ = await fetch_short_term_range(
            region.nx, region.ny, today_kst, today_kst
        )
        item = _aggregate_short_term(rows, today_kst)
        if item is None:
            return None
        return WeatherNowToday(
            temp_max=item.temp_max,
            temp_min=item.temp_min,
            precipitation_prob=item.precipitation_prob,
            sky_condition=item.sky_condition,
        )

    now_obs, _temp = await _observation()
    # 어제 비교는 실황이 있을 때만 뜻이 있다. 기준 시각이 없으면 무엇과
    # 비교하는지 정할 수 없다.
    if now_obs is not None:
        base_day = datetime.strptime(now_obs.base_date, "%Y%m%d").date()
        base_hour = int(now_obs.base_time[:2])
        yesterday, today, air = await asyncio.gather(
            _yesterday(base_day, base_hour),
            _today(),
            _stored_air(
                region.lv1 if region is not None else None,
                region.lv2 if region is not None else None,
            ),
        )
    else:
        yesterday = None
        today, air = await asyncio.gather(
            _today(),
            _stored_air(
                region.lv1 if region is not None else None,
                region.lv2 if region is not None else None,
            ),
        )

    return WeatherNowResponse(
        nx=nx,
        ny=ny,
        province=region.lv1 if region is not None else None,
        city=(region.lv2 or None) if region is not None else None,
        now=now_obs,
        yesterday=yesterday,
        today=today,
        air=air,
    )


def _brd_div_for_mobility(mobility: str | None) -> str | None:
    """이동수단을 코스 걷기/자전거 구분 코드로 매핑한다.

    walk/foot → 걷기길(DNWW), bicycle/scooter/kickboard → 자전거길(DNBW),
    그 외/미지정 → 구분 없음(전체).

    킥보드를 자전거길로 보내는 이유: 걷기길에는 계단·산책로처럼 바퀴가
    들어갈 수 없는 구간이 포함된다. 구분을 비워 두면 그런 코스가 후보로
    올라오고, 반경 필터는 거리만 보므로 걸러내지 못한다.
    """
    if mobility in ("walk", "foot"):
        return "DNWW"
    if mobility in ("bicycle", "scooter", "kickboard"):
        return "DNBW"
    return None


def _kakao_cache_key(
    province: str,
    city: str,
    query: str,
    category_group_code: str | None,
    size: int,
) -> str:
    """카카오 검색 결과 캐시 키를 만든다(동일 질의 재호출 회피)."""
    raw = f"{province}|{city}|{query}|{category_group_code or ''}|{size}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"kakao:places:{digest}"


async def _kakao_places(
    province: str,
    city: str,
    query: str,
    category_group_code: str | None,
    size: int,
) -> list[dict]:
    """카카오 출처 장소 후보를 얻는다(캐시 → 스텁/실호출 순).

    스텁 모드면 고정 응답을 쓴다. 실호출은 행정구역을 좌표로 변환해
    그 주변으로 키워드 검색을 하고 결과를 L1 캐시에 담는다. 카카오 호출
    실패는 빈 리스트로 흡수해 다른 출처가 계속 응답되게 한다.
    """
    cache = get_place_cache()
    key = _kakao_cache_key(province, city, query, category_group_code, size)
    if cache is not None:
        cached = await cache.get_json(key)
        if cached is not None:
            return cached

    secret = settings.KAKAO_REST_API_KEY.get_secret_value()
    if places_stub_active(secret):
        results = kakao_keyword_stub(query)
    else:
        client = get_kakao_client()
        if client is None:
            return []
        try:
            center = await client.geocode_address(
                f"{province} {city}".strip()
            )
            x = y = None
            radius = None
            if center is not None:
                lat0, lng0 = center
                x, y = lng0, lat0
                radius = settings.KAKAO_DEFAULT_RADIUS_M
            results = await client.search_keyword(
                query,
                x=x,
                y=y,
                radius=radius,
                size=size,
                category_group_code=category_group_code,
            )
        except KakaoApiError as e:
            logger.warning("kakao search failed msg=%s", e.msg)
            return []

    if cache is not None:
        await cache.set_json(key, results, settings.KAKAO_CACHE_TTL_SEC)
    return results


@router.get("/v1/places", response_model=PlacesResponse)
async def get_places(
    province: str = Query(..., min_length=1, max_length=20),
    city: str = Query("", max_length=20),
    keyword: str | None = Query(None, max_length=40),
    category_group_code: str | None = Query(None, max_length=10),
    mobility: str | None = Query(None, max_length=10),
    size: int = Query(15, ge=1, le=15),
) -> PlacesResponse:
    """GET /v1/places — 행정구역 기준 장소 후보 병합 조회.

    점 장소(카카오)와 코스(걷기/자전거) 두 출처를 모두 조회해 하나의
    목록으로 합친다.

    Query 파라미터:
        province: 광역시도 명(필수). 예: "서울특별시"
        city: 시군구 명(선택). 예: "강남구"
        keyword: 검색어(선택). 없으면 행정구역명을 검색어로 쓴다.
        category_group_code: 카카오 카테고리 그룹 코드(선택).
        mobility: 이동수단(선택). 코스 출처를 걷기/자전거로 거른다.
        size: 출처별 최대 결과 수.

    처리 흐름:
        1) 카카오 키워드 검색(좌표 주변, L1 캐시) → 점 장소 후보
        2) 행정구역 중심 좌표 주변의 코스 조회 → 코스 후보
        3) 두 결과를 합쳐 출처별 건수와 함께 반환

    한 출처의 실패/부재는 다른 출처 결과만으로 응답한다.
    """
    query = keyword or f"{province} {city}".strip()
    kakao = await _kakao_places(
        province, city, query, category_group_code, size
    )

    courses: list[dict] = []
    # 자동차/대중교통 요청에는 걷기/자전거 코스가 부적합하므로 코스 출처를
    # 건너뛰고 점 장소만 반환한다. 도보/자전거 및 미지정은 코스를 포함한다.
    if mobility not in ("car", "transit"):
        centroid = await lookup_region_centroid(province, city)
        if centroid is not None:
            lat, lng = centroid
            courses = await search_courses_nearby(
                lat,
                lng,
                settings.DURUNUBI_RADIUS_M,
                brd_div=_brd_div_for_mobility(mobility),
                limit=size,
            )

    items = [PlaceItem(**p) for p in (kakao + courses)]
    sources: dict[str, int] = {}
    for it in items:
        sources[it.source] = sources.get(it.source, 0) + 1
    return PlacesResponse(
        places=items, count=len(items), sources=sources
    )


def _naver_cache_key(
    query: str, display: int, sort: str, start: int = 1
) -> str:
    """네이버 블로그 검색 결과 캐시 키를 만든다(동일 질의 재호출 회피).

    구간(start)도 키에 넣는다. 넣지 않으면 첫 장을 담은 캐시가 뒷장 요청에도
    그대로 나가 더보기가 같은 목록만 되풀이한다.
    """
    raw = f"{query}|{display}|{start}|{sort}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"naver:blog:{digest}"


async def _naver_reviews(
    query: str, display: int, sort: str, start: int = 1
) -> list[dict]:
    """네이버 블로그 리뷰 후보를 얻는다(캐시 → 스텁/실호출 순).

    스텁 모드(자격증명 미발급 포함)면 고정 응답을 쓴다. 실호출 실패는 빈
    리스트로 흡수해 다른 흐름을 막지 않는다(hub degrade 원칙 — 5xx 대신
    빈 리뷰). 성공 결과는 L1 캐시에 담는다.
    """
    cache = get_place_cache()
    key = _naver_cache_key(query, display, sort, start)
    if cache is not None:
        cached = await cache.get_json(key)
        if cached is not None:
            return cached

    # 네이버는 ID/시크릿 두 자격증명이 모두 있어야 실호출한다. 하나라도
    # 비어 있으면(또는 강제 스텁 모드면) 스텁 응답으로 대체한다.
    naver_id = settings.NAVER_CLIENT_ID.get_secret_value()
    naver_secret = settings.NAVER_CLIENT_SECRET.get_secret_value()
    if places_stub_active(naver_id) or places_stub_active(naver_secret):
        results = naver_blog_stub(query, display=display, start=start)
    else:
        client = get_naver_client()
        if client is None:
            return []
        try:
            results = await client.search_blog(
                query, display=display, start=start, sort=sort
            )
        except NaverApiError as e:
            logger.warning("naver blog search failed msg=%s", e.msg)
            return []

    if cache is not None:
        await cache.set_json(
            key, results, settings.NAVER_BLOG_CACHE_TTL_SEC
        )
    return results


@router.get("/v1/reviews", response_model=ReviewsResponse)
async def get_reviews(
    query: str = Query(..., min_length=1, max_length=60),
    display: int = Query(5, ge=1, le=10),
    start: int = Query(1, ge=1, le=100),
    sort: Literal["sim", "date"] = Query("sim"),
) -> ReviewsResponse:
    """GET /v1/reviews — 검색어에 대한 네이버 블로그 리뷰 조회.

    장소 보강용 블로그 리뷰를 조회한다. 결과는 L1 캐시(6h)에 담기며,
    자격증명이 없으면 스텁으로 동작한다.

    Query 파라미터:
        query: 검색어(1~60 글자, 필수).
        display: 반환 건수(1~10, 기본 5).
        start: 검색 시작 위치(1부터). 더보기를 누를 때마다 앞 구간 길이를
            더해 보내면 다음 구간이 온다.
        sort: "sim"(정확도) 또는 "date"(최신순).

    더 있는지 판정: 돌려준 건수가 요청한 display 보다 적으면 그 구간이
    마지막이다. 총 건수를 따로 싣지 않는 이유는, 네이버가 주는 총계가
    실제로 받을 수 있는 건수와 어긋나는 경우가 있어 기준으로 삼기 어렵기
    때문이다.

    네이버 호출 실패는 빈 리뷰 목록으로 흡수한다(5xx 를 내지 않는다).

    response_model: ReviewsResponse — 직렬화·검증을 본 모델로 강제.
    """
    results = await _naver_reviews(query, display, sort, start)
    reviews = [ReviewItem(**r) for r in results]
    return ReviewsResponse(
        query=query, reviews=reviews, count=len(reviews), start=start
    )


# ── 장소 사진(Google 장소) ────────────────────────────────────────────

def _google_placeid_cache_key(query: str, lat: float, lng: float) -> str:
    """장소 식별자 캐시 키를 만든다.

    좌표까지 넣는다. 같은 상호가 여러 지역에 있어 이름만으로는 키가 겹치고,
    그러면 다른 동네 지점의 사진이 나간다. 좌표는 5자리(약 1.1m)로 라운딩해
    미세 편차를 흡수한다.
    """
    raw = f"{query}|{lat:.5f}|{lng:.5f}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"google:placeid:{digest}"


def _google_media_budget_key(now: datetime) -> str:
    """이미지 URL 발급 횟수를 하루 단위로 세는 카운터 키."""
    return f"google:photos:media:{now.strftime('%Y%m%d')}"


def _seconds_to_next_kst_midnight(now: datetime) -> int:
    """다음 날 자정까지 남은 초. 하루치 카운터의 정리 시점으로 쓴다."""
    tomorrow = (now + timedelta(days=1)).date()
    midnight = datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=_KST
    )
    return max(int((midnight - now).total_seconds()), 1)


async def _google_place_id(
    query: str, lat: float, lng: float
) -> str | None:
    """검색어+좌표에 대응하는 장소 식별자를 얻는다(캐시 → 실호출 순).

    식별자는 캐시에 담지만 사진 이름과 이미지 URL 은 담지 않는다 — 뒤의
    둘은 만료되는 값이라 다시 쓸 수 없다.

    대응하는 장소가 없었다는 사실도 짧게 남긴다. 남기지 않으면 사진이 없는
    장소를 열 때마다 검색이 다시 나간다.
    """
    cache = get_place_cache()
    key = _google_placeid_cache_key(query, lat, lng)
    if cache is not None:
        cached = await cache.get_json(key)
        if isinstance(cached, dict) and "place_id" in cached:
            return cached["place_id"]

    client = get_google_client()
    if client is None:
        return None
    place_id = await client.search_place_id(
        query, lat, lng, settings.GOOGLE_PLACES_BIAS_RADIUS_M
    )

    if cache is not None:
        ttl = (
            settings.GOOGLE_PLACEID_CACHE_TTL_SEC
            if place_id
            else settings.GOOGLE_PLACEID_NEG_CACHE_TTL_SEC
        )
        await cache.set_json(key, {"place_id": place_id}, ttl)
    return place_id


async def _google_media_allowed(count: int) -> bool:
    """이미지 URL 을 이번에 count 개 더 발급해도 되는지 판단한다.

    발급 전에 먼저 세고 상한을 넘겼는지 본다. 발급한 뒤에 세면 동시에 들어온
    요청들이 상한을 함께 넘어선다.

    셀 수 없으면(카운터 미설정·Redis 장애) 발급하지 않는다. 이 상한은 요금이
    붙지 않는 범위를 지키려고 두는 것이라, 셈이 안 되는 동안 그냥 내보내면
    상한이 아예 없는 것과 같아진다. 사진은 부가 정보이므로 이때는 사진만
    빠지고 화면의 나머지는 그대로 나간다.

    상한에 걸려 발급하지 않기로 했으면 방금 더한 몫을 되돌린다. 되돌리지
    않으면 거절된 요청이 쓰지도 않은 몫을 차지해, 남은 하루치가 실제보다
    일찍 바닥난 것처럼 보인다.
    """
    cap = settings.GOOGLE_PHOTOS_DAILY_MEDIA_CAP
    if cap <= 0:
        return False
    cache = get_place_cache()
    if cache is None:
        return False
    now = datetime.now(_KST)
    key = _google_media_budget_key(now)
    ttl = _seconds_to_next_kst_midnight(now)
    total = await cache.incr_by(key, count, ttl)
    if total is None:
        return False
    if total > cap:
        await cache.incr_by(key, -count, ttl)
        return False
    return True


async def _google_photos(query: str, lat: float, lng: float) -> list[dict]:
    """장소 사진 목록을 얻는다(스텁 → 식별자 → 사진 이름 → 이미지 URL 순).

    캐시에 담는 것은 장소 식별자뿐이다. 사진 이름은 만료될 수 있고 이미지
    URL 도 수명이 짧아 둘 다 요청마다 새로 받는다. 앞 단계들은 과금이 없고
    이미지 URL 발급만 과금되므로 상한도 그 지점에만 건다.

    어떤 실패도 빈 목록으로 흡수한다(hub degrade 원칙 — 5xx 대신 빈 사진).
    사진은 부가 정보라 이것 때문에 화면이 막히면 안 된다.
    """
    if places_stub_active(settings.GOOGLE_MAPS_API_KEY.get_secret_value()):
        return google_photos_stub(query)

    client = get_google_client()
    if client is None:
        return []

    try:
        place_id = await _google_place_id(query, lat, lng)
        if not place_id:
            return []
        metas = await client.fetch_photos(place_id)
    except GooglePlacesApiError as e:
        logger.warning("google photo lookup failed msg=%s", e.msg)
        return []

    metas = metas[: settings.GOOGLE_PHOTOS_MAX_COUNT]
    if not metas:
        return []
    if not await _google_media_allowed(len(metas)):
        logger.warning("google photo media budget exhausted")
        return []

    # 사진마다 URL 발급이 한 번씩 나가므로 병렬로 돌린다. 한 건의 실패가
    # 나머지 사진까지 없애지 않도록 예외는 건별로 흡수한다.
    issued = await asyncio.gather(
        *(
            client.fetch_photo_uri(
                m["name"], settings.GOOGLE_PHOTOS_MAX_WIDTH_PX
            )
            for m in metas
        ),
        return_exceptions=True,
    )

    out: list[dict] = []
    for meta, uri in zip(metas, issued):
        if isinstance(uri, BaseException):
            logger.warning("google photo media failed err=%s", uri)
            continue
        if not uri:
            continue
        out.append(
            {
                "photo_uri": uri,
                "width_px": meta.get("width_px"),
                "height_px": meta.get("height_px"),
                "attributions": meta.get("attributions") or [],
                "google_maps_uri": meta.get("google_maps_uri"),
                "flag_content_uri": meta.get("flag_content_uri"),
            }
        )
    return out


@router.get("/v1/places/photos", response_model=PlacePhotosResponse)
async def get_place_photos(
    request: Request,
    query: str = Query(..., min_length=1, max_length=60),
    loc: str | None = Query(None, description="감싼 좌표"),
    lat: float | None = Query(None, ge=33.0, le=43.0),
    lng: float | None = Query(None, ge=124.0, le=132.0),
) -> PlacePhotosResponse:
    """GET /v1/places/photos — 장소 사진 조회.

    장소명과 좌표로 사진을 찾아 이미지 URL 과 출처 표기를 돌려준다. 좌표를
    함께 받는 이유는 같은 상호가 여러 지역에 있어 이름만으로는 다른 동네
    지점이 잡히기 때문이다.

    Query 파라미터:
        query: 장소명(1~60 글자, 필수).
        lat / lng: 장소 좌표(필수). 국내 범위를 벗어나면 검증 실패.

    이미지 URL 은 수명이 짧다. 호출 측은 받은 URL 을 저장하지 말고 화면에
    쓸 때마다 이 endpoint 를 다시 부른다.

    사진을 못 찾았거나 조회에 실패해도 빈 목록으로 200 을 낸다(5xx 금지).
    하루 발급 상한을 넘긴 뒤에도, 조회가 전체 제한 시간을 넘겨도 같은 방식
    으로 빈 목록이 나간다. 조회는 세 단계를 이어 밟아 단계마다 느려지면 합이
    호출 측의 대기 시간을 넘기므로, 여기서 먼저 끊고 답을 돌려준다.

    response_model: PlacePhotosResponse — 직렬화·검증을 본 모델로 강제.
    """
    # 좌표는 감싸서 온다. 여는 데 실패하면 여기서 요청이 끝난다 —
    # 평문으로 물러서면 감싸는 쪽이 고장 나도 아무도 알아차리지 못한다.
    lat, lng = resolve_point(request, loc, lat, lng)
    try:
        photos = await asyncio.wait_for(
            _google_photos(query, lat, lng),
            timeout=settings.GOOGLE_PHOTOS_TOTAL_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning("google photo lookup exceeded budget")
        photos = []
    items = [PlacePhotoItem(**p) for p in photos]
    return PlacePhotosResponse(
        query=query, photos=items, count=len(items)
    )


# ── 경로 라우팅(OSRM 프록시) ──────────────────────────────────────────

def _osrm_base_url(mode: str) -> str:
    """이동수단에 맞는 OSRM 프로파일 base URL 을 돌려준다.

    walk→FOOT, bicycle/scooter→BICYCLE. 그 외는 빈 문자열(스텁 판정용).
    """
    if mode == "walk":
        return settings.OSRM_FOOT_BASE_URL
    if mode in ("bicycle", "scooter"):
        return settings.OSRM_BICYCLE_BASE_URL
    return ""


def _route_cache_key(mode: str, leg: DirectionsLeg) -> str:
    """경로 결과 캐시 키. mode 는 프로파일 구분에만 관여한다.

    walk/bicycle/scooter 는 각각 foot/bicycle/bicycle 프로파일로 갈리므로
    프로파일 이름을 키에 넣는다(scooter=bicycle 캐시 공유). 좌표는 5자리
    (약 1.1m) 라운딩해 미세 편차를 흡수한다.
    """
    profile = "foot" if mode == "walk" else "bicycle"
    raw = (
        f"{leg.start.lat:.5f}|{leg.start.lng:.5f}|"
        f"{leg.goal.lat:.5f}|{leg.goal.lng:.5f}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"osrm:{profile}:{digest}"


async def _route_one_leg(
    mode: str, leg: DirectionsLeg, use_stub: bool
) -> DirectionsRoute | None:
    """한 구간의 경로를 조회한다(캐시 → 스텁/실호출 순).

    실호출 실패는 None 으로 흡수해(전 구간 응답을 막지 않음) 호출측이
    직선 폴백하게 한다. 스텁 결과는 결정적이라 캐시하지 않는다.
    """
    cache = get_place_cache()
    key = _route_cache_key(mode, leg)
    if cache is not None:
        cached = await cache.get_json(key)
        if cached is not None:
            return DirectionsRoute(**cached)

    if use_stub:
        data = osrm_route_stub(
            leg.start.lat, leg.start.lng, leg.goal.lat, leg.goal.lng
        )
        return DirectionsRoute(**data)

    client = get_osrm_client(mode)
    if client is None:
        return None
    try:
        data = await client.route(
            leg.start.lat, leg.start.lng, leg.goal.lat, leg.goal.lng
        )
    except OsrmApiError as e:
        logger.warning("osrm route failed mode=%s msg=%s", mode, e.msg)
        return None

    route = DirectionsRoute(**data)
    if cache is not None:
        await cache.set_json(key, data, settings.ROUTE_CACHE_TTL_SEC)
    return route


@router.post("/v1/directions/batch", response_model=DirectionsBatchResponse)
async def get_directions_batch(
    request: Request,
    req: DirectionsBatchRequest,
) -> DirectionsBatchResponse:
    """POST /v1/directions/batch — 여러 구간의 도로 추종 경로 일괄 조회.

    이동수단(mode)에 맞는 OSRM 프로파일로 각 구간의 경로 지오메트리와
    실측 거리·시간을 구해 돌려준다. 구간들은 병렬(asyncio.gather)로
    조회하며, 특정 구간 실패는 해당 인덱스 null 로 흡수한다 — 업스트림
    장애가 전 구간에 걸쳐도 200 + 전부 null 로 응답한다(hub degrade 원칙).

    스텁 모드(프로파일 base URL 미설정 또는 PLACES_STUB_MODE)면 결정적
    스텁 지오메트리를 반환한다.

    response_model: DirectionsBatchResponse — routes 는 legs 와 같은
        길이·인덱스로 정렬된다.
    """
    # 구간은 감싸서 온다. 여는 데 실패하면 여기서 요청이 끝난다 —
    # 평문으로 물러서면 감싸는 쪽이 고장 나도 아무도 알아차리지 못한다.
    legs = [
        leg if isinstance(leg, DirectionsLeg) else DirectionsLeg(**leg)
        for leg in resolve_legs(request, req.loc, req.legs)
    ]
    use_stub = routing_stub_active(_osrm_base_url(req.mode))
    routes = await asyncio.gather(
        *(_route_one_leg(req.mode, leg, use_stub) for leg in legs)
    )
    return DirectionsBatchResponse(routes=list(routes))


# ── 지하철 경로(ODsay 프록시) ─────────────────────────────────────────

def _odsay_cache_key(
    start_lat: float, start_lng: float, goal_lat: float, goal_lng: float
) -> str:
    """지하철 경로 캐시 키를 만든다.

    좌표를 반올림해 키를 만든다. 그러지 않으면 같은 건물에서 출발한 요청이
    소수점 끝자리 차이만으로 매번 새 키가 되어, 캐시가 사실상 비어 있는 것과
    같아지고 하루 호출 한도가 금방 바닥난다.
    """
    d = settings.ODSAY_CACHE_COORD_DIGITS
    raw = (
        f"{round(start_lat, d)}|{round(start_lng, d)}"
        f"|{round(goal_lat, d)}|{round(goal_lng, d)}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"odsay:subway:{digest}"


def _odsay_budget_key(now: datetime) -> str:
    """지하철 경로 외부 호출 횟수를 하루 단위로 세는 카운터 키."""
    return f"odsay:calls:{now.strftime('%Y%m%d')}"


async def _odsay_call_allowed() -> bool:
    """외부 호출을 한 번 더 해도 되는지 판단한다.

    호출 전에 먼저 세고 상한을 넘겼는지 본다. 호출한 뒤에 세면 동시에 들어온
    요청들이 상한을 함께 넘어선다. 넘겨서 호출하지 않기로 했으면 방금 더한
    몫을 되돌린다 — 되돌리지 않으면 거절된 요청이 쓰지도 않은 몫을 차지한다.

    셀 수 없으면(카운터 미설정·Redis 장애) 호출하지 않는다. 상한은 무료 등급
    한도를 지키려고 두는 것이라, 셈이 안 되는 동안 그냥 내보내면 상한이 아예
    없는 것과 같아진다.
    """
    cap = settings.ODSAY_DAILY_CALL_CAP
    if cap <= 0:
        return False
    cache = get_place_cache()
    if cache is None:
        return False
    now = datetime.now(_KST)
    key = _odsay_budget_key(now)
    ttl = _seconds_to_next_kst_midnight(now)
    total = await cache.incr_by(key, 1, ttl)
    if total is None:
        return False
    if total > cap:
        await cache.incr_by(key, -1, ttl)
        return False
    return True


# 예비 키로 다시 시도할 만한 실패인지 가리는 표시. 발급처는 키에 얽힌 문제를
# 본문 메시지에 대괄호 이름으로 담아 준다(인증 실패는 ApiKeyAuthFailed).
# 좌표가 틀린 것 같은 실패까지 다시 부르면 남은 하루치만 두 배로 쓴다.
_ODSAY_KEY_FAILURE_HINTS = ("apikey", "limit", "exceed", "quota")


def _odsay_is_key_failure(error: OdsayApiError) -> bool:
    """예비 키로 다시 시도할 만한 실패인지 판단한다.

    본문에 담겨 온 실패(API_ERR)만 대상이다. 연결이 끊긴 경우는 키를 바꿔도
    같은 결과라 다시 부르지 않는다.
    """
    if error.code != "API_ERR":
        return False
    msg = error.msg.lower()
    return any(hint in msg for hint in _ODSAY_KEY_FAILURE_HINTS)


async def _odsay_retry_with_fallback(
    error: OdsayApiError,
    start_lat: float,
    start_lng: float,
    goal_lat: float,
    goal_lng: float,
) -> tuple[str, dict | None]:
    """주 키가 막혔을 때 예비 키로 한 번 더 시도한다.

    예비 키가 없거나 키와 무관한 실패면 그대로 조회 불가로 돌려준다.
    다시 부르는 것도 외부 호출이므로 하루 상한을 똑같이 거친다.
    """
    if not _odsay_is_key_failure(error):
        return "unavailable", None
    fallback = get_odsay_fallback_client()
    if fallback is None:
        return "unavailable", None
    if not await _odsay_call_allowed():
        logger.warning("odsay fallback skipped — daily call budget exhausted")
        return "unavailable", None
    try:
        route = await fallback.fastest_subway_route(
            start_lat, start_lng, goal_lat, goal_lng
        )
    except OdsayApiError as e:
        logger.warning(
            "odsay fallback also failed code=%s msg=%s", e.code, e.msg
        )
        return "unavailable", None
    logger.warning("odsay primary key rejected — served by fallback key")
    return ("ok" if route is not None else "not_found"), route


async def _subway_route(
    start_lat: float, start_lng: float, goal_lat: float, goal_lng: float
) -> tuple[str, dict | None]:
    """지하철 경로를 얻는다(스텁 → 캐시 → 실호출 순).

    돌려주는 값은 (상태, 경로) 쌍이다. 상태는 "ok"·"not_found"·"unavailable"
    셋 중 하나이며, 경로 없음과 조회 불가를 합치지 않는다 — 화면이 둘을 다른
    문구로 보여주기 때문이다.

    실패도 짧게 캐시한다. 남기지 않으면 외부가 죽어 있는 동안 매 요청이 제한
    시간까지 기다린다.
    """
    if transit_stub_active(settings.ODSAY_API_KEY.get_secret_value()):
        stub = subway_route_stub(start_lat, start_lng, goal_lat, goal_lng)
        return "ok", stub

    cache = get_place_cache()
    key = _odsay_cache_key(start_lat, start_lng, goal_lat, goal_lng)
    if cache is not None:
        cached = await cache.get_json(key)
        if isinstance(cached, dict) and "status" in cached:
            return cached["status"], cached.get("route")

    client = get_odsay_client()
    if client is None:
        return "unavailable", None
    if not await _odsay_call_allowed():
        logger.warning("odsay daily call budget exhausted")
        # 상한에 걸린 사실은 캐시하지 않는다. 남기면 자정에 한도가 풀린 뒤에도
        # 남은 TTL 동안 계속 조회 불가로 답하게 된다.
        return "unavailable", None

    try:
        route = await client.fastest_subway_route(
            start_lat, start_lng, goal_lat, goal_lng
        )
    except OdsayApiError as e:
        logger.warning(
            "odsay route lookup failed code=%s msg=%s", e.code, e.msg
        )
        status, route = await _odsay_retry_with_fallback(
            e, start_lat, start_lng, goal_lat, goal_lng
        )
    else:
        status = "ok" if route is not None else "not_found"

    if cache is not None:
        ttl = (
            settings.ODSAY_FAIL_CACHE_TTL_SEC
            if status == "unavailable"
            else settings.ODSAY_CACHE_TTL_SEC
        )
        await cache.set_json(key, {"status": status, "route": route}, ttl)
    return status, route


@router.get("/v1/transit/subway", response_model=SubwayRouteResponse)
async def get_subway_route(
    request: Request,
    loc: str | None = Query(None, description="감싼 좌표"),
    start_lat: float | None = Query(None, ge=33.0, le=43.0),
    start_lng: float | None = Query(None, ge=124.0, le=132.0),
    end_lat: float | None = Query(None, ge=33.0, le=43.0),
    end_lng: float | None = Query(None, ge=124.0, le=132.0),
) -> SubwayRouteResponse:
    """GET /v1/transit/subway — 지하철 단독 경로 조회.

    두 좌표 사이를 지하철만으로 가는 경로 중 가장 빠른 하나를 돌려준다.
    버스가 섞인 경로는 제외한다 — 화면이 지하철 전용이라 섞인 경로를 주면
    안내와 실제가 어긋난다.

    Query 파라미터:
        start_lat / start_lng: 출발 좌표(필수).
        end_lat / end_lng: 도착 좌표(필수). 국내 범위를 벗어나면 검증 실패.

    조회에 실패해도 5xx 를 내지 않는다. 대신 status 로 구분해 200 을 낸다
    (hub degrade 원칙). 외부가 느려 전체 제한 시간을 넘겨도 마찬가지다.

    response_model: SubwayRouteResponse — status 가 "ok" 일 때만 route 가
        채워진다.
    """
    # 좌표는 감싸서 온다. 여는 데 실패하면 여기서 요청이 끝난다 —
    # 평문으로 물러서면 감싸는 쪽이 고장 나도 아무도 알아차리지 못한다.
    start_lat, start_lng, end_lat, end_lng = resolve_pair(
        request, loc, start_lat, start_lng, end_lat, end_lng
    )
    try:
        status, route = await asyncio.wait_for(
            _subway_route(start_lat, start_lng, end_lat, end_lng),
            timeout=settings.ODSAY_TOTAL_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning("odsay route lookup exceeded budget")
        status, route = "unavailable", None
    return SubwayRouteResponse(
        status=status,
        route=SubwayRoute(**route) if route is not None else None,
    )


def _transit_routes_cache_key(
    start_lat: float, start_lng: float, goal_lat: float, goal_lng: float
) -> str:
    """통합 길찾기 캐시 키. /v1/transit/subway 와 네임스페이스를 분리해
    같은 좌표 요청이 서로 다른 캐시 항목(지하철 전용 vs 전체)을 쓰게 한다.

    네임스페이스에 판(v2)을 박는다. 담아 둔 값의 모양이 바뀌면 예전 판이
    그대로 읽혀 필드가 비어 오고, 그 값을 쓰는 쪽이 깨진다. 판을 올리면
    지우러 다니지 않아도 새 키로 갈리고 옛 항목은 TTL 로 사라진다.
    거리 필드(distance_m·subway_distance_m·…)를 더하면서 v2 가 됐다.
    """
    d = settings.ODSAY_CACHE_COORD_DIGITS
    raw = (
        f"{round(start_lat, d)}|{round(start_lng, d)}"
        f"|{round(goal_lat, d)}|{round(goal_lng, d)}"
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"odsay:routes:v2:{digest}"


async def _odsay_retry_with_fallback_routes(
    error: OdsayApiError,
    start_lat: float,
    start_lng: float,
    goal_lat: float,
    goal_lng: float,
) -> tuple[str, list[dict]]:
    """주 키가 막혔을 때 예비 키로 통합 길찾기를 한 번 더 시도한다.

    판단 기준은 _odsay_retry_with_fallback 과 같다(예비 키·호출 한도 검사도
    동일). route_options 는 dict|None 대신 list 를 돌려주므로 별도로 둔다.
    """
    if not _odsay_is_key_failure(error):
        return "unavailable", []
    fallback = get_odsay_fallback_client()
    if fallback is None:
        return "unavailable", []
    if not await _odsay_call_allowed():
        logger.warning("odsay fallback skipped — daily call budget exhausted")
        return "unavailable", []
    try:
        routes = await fallback.route_options(
            start_lat, start_lng, goal_lat, goal_lng
        )
    except OdsayApiError as e:
        logger.warning(
            "odsay fallback also failed code=%s msg=%s", e.code, e.msg
        )
        return "unavailable", []
    logger.warning("odsay primary key rejected — served by fallback key")
    return ("ok" if routes else "not_found"), routes


async def _transit_routes(
    start_lat: float, start_lng: float, goal_lat: float, goal_lng: float
) -> tuple[str, list[dict]]:
    """대중교통 통합 경로 후보를 얻는다(스텁 → 캐시 → 실호출 순).

    _subway_route 와 같은 구조다. 지하철 전용 대신 route_options(pathType
    필터 없음)를 쓰고, 캐시 키 네임스페이스를 분리한다.
    """
    if transit_stub_active(settings.ODSAY_API_KEY.get_secret_value()):
        stub = transit_routes_stub(start_lat, start_lng, goal_lat, goal_lng)
        return "ok", stub

    cache = get_place_cache()
    key = _transit_routes_cache_key(start_lat, start_lng, goal_lat, goal_lng)
    if cache is not None:
        cached = await cache.get_json(key)
        if isinstance(cached, dict) and "status" in cached:
            return cached["status"], cached.get("routes") or []

    client = get_odsay_client()
    if client is None:
        return "unavailable", []
    if not await _odsay_call_allowed():
        logger.warning("odsay daily call budget exhausted")
        return "unavailable", []

    try:
        routes = await client.route_options(
            start_lat, start_lng, goal_lat, goal_lng
        )
    except OdsayApiError as e:
        logger.warning(
            "odsay route options lookup failed code=%s msg=%s", e.code, e.msg
        )
        status, routes = await _odsay_retry_with_fallback_routes(
            e, start_lat, start_lng, goal_lat, goal_lng
        )
    else:
        status = "ok" if routes else "not_found"

    if cache is not None:
        ttl = (
            settings.ODSAY_FAIL_CACHE_TTL_SEC
            if status == "unavailable"
            else settings.ODSAY_CACHE_TTL_SEC
        )
        await cache.set_json(key, {"status": status, "routes": routes}, ttl)
    return status, routes


# 버스 버튼이 받아들이는 이동수단. walk 는 어느 경로에나 붙는 연결 구간이라
# 함께 둔다. 지하철·열차·항공이 하나라도 있으면 버스 전용이 아니다.
_BUS_BUTTON_MODES = {"walk", "bus", "express", "intercity"}

# 지하철 버튼에서도 빼야 하는 이동수단. 지하철을 조금 타고 KTX 로 갈아타는
# 경로를 "지하철 경로"라고 부를 수는 없다.
_NON_ROAD_MODES = {"train", "air"}


def _filter_routes_by_mode(routes: list[dict], mode: str) -> list[dict]:
    """화면이 고른 이동수단에 맞게 후보를 걸러 낸다.

    "all"  — 거르지 않는다.
    "bus"  — 버스로만 가는 후보. 지하철·열차·항공이 섞인 것을 뺀다. 버스 버튼을
             눌렀는데 목록 맨 위가 지하철 전용이면 버튼과 결과가 어긋난다.
             고속버스·시외버스는 여기 든다 — 도시 간 이동을 버스 버튼에서 빼면
             그 구간은 어느 버튼에서도 안 나온다.
    "subway" — 지하철이 들어간 후보만. 그중 타는 거리의 대부분이 버스인 것은
             뺀다(TRANSIT_BUS_DOMINANCE_RATIO). 지하철을 두세 정거장 타려고
             버스를 한 시간 타는 경로를 "지하철 경로"로 보여주지 않기 위함이다.

    열차·항공은 두 버튼 어디에도 넣지 않는다. 지하철 거리 0 만 보고 "버스"로
    치면 KTX 후보가 버스 버튼에 뜨고, 비중만 보면 열차 단독 후보가 "버스 비중
    0%"라 지하철 버튼에 샌다. 그래서 종류 자체를 본다 — 서울 → 부산은 후보
    19개 중 열차 10건·항공 1건이라 그냥 두면 두 버튼이 전부 오염된다.

    다만 비중 규칙이 후보를 전부 지워 버리면 규칙을 적용하지 않는다. 지하철이
    들어간 길이 분명히 있는데 화면에 "없다"고 내보내는 편이 더 나쁘다.
    지하철이 아예 없는 지역이라 빈 목록이 되는 것은 사실 그대로이므로 둔다.
    """
    if mode == "bus":
        return [
            r
            for r in routes
            if set(r.get("modes") or []) <= _BUS_BUTTON_MODES
        ]
    if mode == "subway":
        with_subway = [
            r
            for r in routes
            if r.get("subway_distance_m", 0) > 0
            and not (set(r.get("modes") or []) & _NON_ROAD_MODES)
        ]
        kept = [
            r
            for r in with_subway
            if r.get("bus_distance_ratio", 0.0)
            < settings.TRANSIT_BUS_DOMINANCE_RATIO
        ]
        return kept or with_subway
    return routes


@router.get("/v1/transit/routes", response_model=TransitRouteOptionsResponse)
async def get_transit_routes(
    start_lat: float = Query(..., ge=33.0, le=43.0),
    start_lng: float = Query(..., ge=124.0, le=132.0),
    end_lat: float = Query(..., ge=33.0, le=43.0),
    end_lng: float = Query(..., ge=124.0, le=132.0),
    mode: Literal["all", "subway", "bus"] = Query("all"),
) -> TransitRouteOptionsResponse:
    """GET /v1/transit/routes — 대중교통 통합 길찾기(지하철·버스 모두).

    두 좌표 사이를 대중교통으로 가는 방법을 소요시간 순으로 나열한다.
    /v1/transit/subway 와 달리 버스 전용·혼합 경로도 그대로 담는다 —
    "가능한 이동수단을 모두 보여주는" 화면은 이쪽을 쓴다.

    Query 파라미터:
        start_lat / start_lng: 출발 좌표(필수).
        end_lat / end_lng: 도착 좌표(필수). 국내 범위를 벗어나면 검증 실패.
        mode: 화면이 고른 이동수단(기본 all). 거르는 규칙은
            _filter_routes_by_mode 참고.

    조회에 실패해도 5xx 를 내지 않는다. 대신 status 로 구분해 200 을 낸다
    (hub degrade 원칙). 외부가 느려 전체 제한 시간을 넘겨도 마찬가지다.

    거른 뒤 남은 것이 없으면 status 를 "not_found" 로 내린다. 조회는 됐는데
    그 수단으로 갈 방법이 없는 상태라, 외부 장애("unavailable")와 구분해야
    화면이 다른 문구를 보여줄 수 있다.

    response_model: TransitRouteOptionsResponse — status 가 "ok" 일 때만
        routes 가 채워진다.
    """
    try:
        status, routes = await asyncio.wait_for(
            _transit_routes(start_lat, start_lng, end_lat, end_lng),
            timeout=settings.ODSAY_TOTAL_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning("odsay route options lookup exceeded budget")
        status, routes = "unavailable", []
    if status == "ok":
        # 거른 뒤에 자른다. 순서를 바꾸면 버튼과 맞는 후보가 상한 밖으로
        # 밀려난다 — 서울 → 부산은 빠른 순으로 열차·항공이 앞을 채워,
        # 고속버스가 4건 있는데도 버스 버튼이 빈 목록이었다.
        routes = _filter_routes_by_mode(routes, mode)[
            : OdsayClient.ROUTE_OPTIONS_MAX
        ]
        if not routes:
            status = "not_found"
    return TransitRouteOptionsResponse(
        status=status,
        routes=[TransitRouteOption(**r) for r in routes],
    )


# ── 따릉이 대여소(서울 열린데이터광장 프록시) ────────────────────────

def _seoul_bike_cache_key() -> str:
    """따릉이 전량 스냅샷 캐시 키.

    좌표를 키에 넣지 않는다. 좌표별로 캐시하면 지도를 조금 움직일 때마다 새
    키가 되어 그때마다 여러 장을 다시 받아 오고, 하루 호출 한도가 곧 바닥난다.
    전량을 한 벌만 담아 두고 요청한 좌표 주변만 잘라 보낸다.
    """
    return "seoulbike:all"


async def _seoul_bike_all() -> tuple[str, list[dict]]:
    """따릉이 대여소 전량을 얻는다(스텁 → 캐시 → 실호출 순).

    돌려주는 값은 (상태, 목록) 쌍이다. 한 번에 주는 행 수가 정해져 있어
    범위를 옮겨 가며 나눠 받되, 받은 행이 요청한 범위보다 적으면 마지막
    장으로 본다.

    도중에 실패하면 그때까지 받은 몫으로 답한다. 지도에 일부라도 찍히는 편이
    빈 지도보다 낫다. 다만 그런 스냅샷은 온전하지 않으므로 짧은 시간만
    담아 두고 다음 요청에서 다시 채운다.

    스텁 판정은 여기서 하지 않는다. 스텁 목록은 요청 좌표를 기준으로 만들어야
    해서 좌표를 아는 라우터가 직접 만든다.
    """
    cache = get_place_cache()
    key = _seoul_bike_cache_key()
    if cache is not None:
        cached = await cache.get_json(key)
        if isinstance(cached, dict) and "stations" in cached:
            return cached["status"], cached["stations"]

    client = get_seoul_bike_client()
    if client is None:
        return "unavailable", []

    page_size = settings.SEOUL_BIKE_PAGE_SIZE
    stations: list[dict] = []
    complete = False
    for page in range(settings.SEOUL_BIKE_MAX_PAGES):
        start = page * page_size + 1
        end = start + page_size - 1
        try:
            rows, raw_count = await client.fetch_page(start, end)
        except SeoulBikeApiError as e:
            logger.warning(
                "seoul bike page %d failed code=%s msg=%s",
                page, e.code, e.msg,
            )
            break
        stations.extend(rows)
        # 요청한 만큼 채워 오지 않았으면 뒤에 남은 장이 없다. 판정에는 버리기
        # 전의 행 수를 쓴다 — 좌표 없는 행을 걸러낸 뒤의 길이로 보면 한 장이
        # 꽉 차서 왔는데도 덜 왔다고 읽혀 뒤쪽 대여소를 통째로 놓친다.
        if raw_count < page_size:
            complete = True
            break
    else:
        # 상한까지 다 돌았다. 더 있을 수 있으나 여기서 끊는다.
        logger.warning("seoul bike page cap reached")

    if not stations:
        return "unavailable", []

    status = "ok" if complete else "partial"
    if cache is not None:
        ttl = (
            settings.SEOUL_BIKE_CACHE_TTL_SEC
            if complete
            else settings.SEOUL_BIKE_PARTIAL_CACHE_TTL_SEC
        )
        await cache.set_json(
            key, {"status": status, "stations": stations}, ttl
        )
    return status, stations


@router.get(
    "/v1/mobility/bike-stations", response_model=BikeStationsResponse
)
async def get_bike_stations(
    request: Request,
    loc: str | None = Query(None, description="감싼 좌표"),
    lat: float | None = Query(None, ge=33.0, le=43.0),
    lng: float | None = Query(None, ge=124.0, le=132.0),
    radius_m: int = Query(
        settings.SEOUL_BIKE_DEFAULT_RADIUS_M, ge=100, le=20000
    ),
) -> BikeStationsResponse:
    """GET /v1/mobility/bike-stations — 좌표 주변 따릉이 대여소 조회.

    Query 파라미터:
        lat / lng: 기준 좌표(필수). 국내 범위를 벗어나면 검증 실패.
        radius_m: 잘라 보낼 반경(m, 100~20000). 기본값은 설정에서 온다.

    발급처가 전량을 통째로 주므로 hub 가 한 벌만 받아 두고 요청 좌표 주변만
    잘라 보낸다. 그래서 지도를 움직이며 여러 번 물어도 외부 호출은 늘지
    않고, 앱이 받는 양도 전체가 아니라 주변 몇 곳으로 줄어든다.

    서비스 지역이 서울이라 그 밖 좌표로 물으면 빈 목록이 온다. 그것은 실패가
    아니므로 status 는 "ok" 다.

    조회에 실패해도 5xx 를 내지 않는다(hub degrade 원칙). 전체 제한 시간을
    넘겨도 마찬가지로 빈 목록에 status 만 다르게 나간다.

    response_model: BikeStationsResponse.
    """
    # 좌표는 감싸서 온다. 여는 데 실패하면 여기서 요청이 끝난다 —
    # 평문으로 물러서면 감싸는 쪽이 고장 나도 아무도 알아차리지 못한다.
    lat, lng = resolve_point(request, loc, lat, lng)
    if places_stub_active(settings.SEOUL_OPENAPI_KEY.get_secret_value()):
        rows = seoul_bike_stub(lat, lng)
        items = [BikeStation(**s) for s in rows]
        return BikeStationsResponse(
            status="ok", stations=items, count=len(items)
        )

    try:
        status, stations = await asyncio.wait_for(
            _seoul_bike_all(),
            timeout=settings.SEOUL_BIKE_TOTAL_BUDGET_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning("seoul bike lookup exceeded budget")
        status, stations = "unavailable", []

    if status == "unavailable":
        return BikeStationsResponse(
            status="unavailable", stations=[], count=0
        )

    # 일부만 받아 온 스냅샷도 결과로는 정상으로 다룬다. 화면에는 "받아 온
    # 만큼"과 "전부"의 차이가 보이지 않고, 다음 요청에서 다시 채워진다.
    nearby = [
        s
        for s in stations
        if haversine_m(lat, lng, s["lat"], s["lng"]) <= radius_m
    ]
    items = [BikeStation(**s) for s in nearby]
    return BikeStationsResponse(status="ok", stations=items, count=len(items))


# ── 공유 킥보드(국토교통부 퍼스널모빌리티 프록시) ─────────────────────

def _pm_providers() -> list[str]:
    """조회할 사업자 목록. 빈 항목과 앞뒤 공백은 걸러낸다."""
    return [p.strip() for p in settings.PM_PROVIDERS.split(",") if p.strip()]


def _pm_cache_key(city: str | None) -> str:
    """공유 킥보드 캐시 키.

    좌표를 키에 넣지 않는다. 발급처가 좌표가 아니라 사업자·지역으로만 받고
    한 번 조회에 사업자 수만큼 호출이 나가므로, 좌표별로 담으면 지도를 조금
    움직일 때마다 그 횟수가 통째로 다시 나간다.
    """
    return f"pm:vehicles:{city or 'all'}"


async def _pm_vehicles(city: str | None) -> tuple[str, list[dict]]:
    """공유 킥보드 목록을 얻는다(캐시 → 실호출 순).

    사업자마다 따로 물어 합친다. 일부 사업자만 실패하면 받은 만큼으로
    "ok" 를 낸다 — 한 사업자의 장애로 나머지가 함께 사라지면 화면이 실제보다
    비어 보인다. 전부 실패했을 때만 조회 불가로 본다.

    스텁 판정은 여기서 하지 않는다. 스텁 목록은 요청 좌표를 기준으로 만들어야
    해서 좌표를 아는 라우터가 직접 만든다.
    """
    cache = get_place_cache()
    key = _pm_cache_key(city)
    if cache is not None:
        cached = await cache.get_json(key)
        if isinstance(cached, dict) and "vehicles" in cached:
            return cached["status"], cached["vehicles"]

    client = get_pm_client()
    if client is None:
        return "unavailable", []

    providers = _pm_providers()
    # 사업자별 호출은 서로 독립이라 함께 내보낸다. 순차로 돌리면 사업자 수만큼
    # 시간이 곱해져 전체 제한 시간을 넘긴다.
    results = await asyncio.gather(
        *(
            client.fetch_by_provider(
                p, city=city, num_of_rows=settings.PM_NUMOFROWS
            )
            for p in providers
        ),
        return_exceptions=True,
    )

    vehicles: list[dict] = []
    failed = 0
    for provider, result in zip(providers, results):
        if isinstance(result, BaseException):
            failed += 1
            msg = result.msg if isinstance(result, PmApiError) else str(result)
            logger.warning("pm provider %s failed msg=%s", provider, msg)
            continue
        vehicles.extend(result)

    if providers and failed == len(providers):
        return "unavailable", []

    status = "ok" if failed == 0 else "partial"
    if cache is not None:
        ttl = (
            settings.PM_CACHE_TTL_SEC
            if failed == 0
            else settings.PM_FAIL_CACHE_TTL_SEC
        )
        await cache.set_json(
            key, {"status": status, "vehicles": vehicles}, ttl
        )
    return status, vehicles


@router.get("/v1/mobility/pm-vehicles", response_model=PmVehiclesResponse)
async def get_pm_vehicles(
    request: Request,
    loc: str | None = Query(None, description="감싼 좌표"),
    lat: float | None = Query(None, ge=33.0, le=43.0),
    lng: float | None = Query(None, ge=124.0, le=132.0),
    radius_m: int = Query(settings.PM_DEFAULT_RADIUS_M, ge=100, le=20000),
    city: str | None = Query(None, min_length=1, max_length=30),
) -> PmVehiclesResponse:
    """GET /v1/mobility/pm-vehicles — 좌표 주변 공유 킥보드 조회.

    Query 파라미터:
        lat / lng: 기준 좌표(필수). 국내 범위를 벗어나면 검증 실패.
        radius_m: 잘라 보낼 반경(m, 100~20000). 기본값은 설정에서 온다.
        city: 시군구명(선택). 발급처가 지역으로 좁혀 받을 수 있어 열어 둔다.

    발급처가 좌표로 받지 않고 사업자·지역으로만 주므로, hub 가 사업자별로
    모아 한 벌로 담아 두고 요청 좌표 주변만 잘라 보낸다.

    조회에 실패해도 5xx 를 내지 않는다(hub degrade 원칙). 전체 제한 시간을
    넘겨도 마찬가지로 빈 목록에 status 만 다르게 나간다.

    response_model: PmVehiclesResponse.
    """
    # 좌표는 감싸서 온다. 여는 데 실패하면 여기서 요청이 끝난다 —
    # 평문으로 물러서면 감싸는 쪽이 고장 나도 아무도 알아차리지 못한다.
    lat, lng = resolve_point(request, loc, lat, lng)
    pm_key = (
        settings.PM_SERVICE_KEY.get_secret_value()
        or settings.KMA_SERVICE_KEY.get_secret_value()
    )
    if places_stub_active(pm_key):
        rows = pm_vehicle_stub(lat, lng)
        items = [PmVehicle(**v) for v in rows]
        return PmVehiclesResponse(
            status="ok", vehicles=items, count=len(items)
        )

    try:
        status, vehicles = await asyncio.wait_for(
            _pm_vehicles(city), timeout=settings.PM_TOTAL_BUDGET_SEC
        )
    except asyncio.TimeoutError:
        logger.warning("pm lookup exceeded budget")
        status, vehicles = "unavailable", []

    if status == "unavailable":
        return PmVehiclesResponse(status="unavailable", vehicles=[], count=0)

    nearby = [
        v
        for v in vehicles
        if haversine_m(lat, lng, v["lat"], v["lng"]) <= radius_m
    ]
    items = [PmVehicle(**v) for v in nearby]
    return PmVehiclesResponse(status="ok", vehicles=items, count=len(items))
