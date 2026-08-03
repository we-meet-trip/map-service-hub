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

응답 모델:
  - WeatherDailyItem / WeatherResponse (app.schemas.hub_schemas)
  - PlaceItem / PlacesResponse
  - ReviewItem / ReviewsResponse
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from app.clients.hub_clients import (
    AirKoreaApiError,
    KakaoApiError,
    KMAApiError,
    NaverApiError,
    OsrmApiError,
)
from app.codes.air_codes import grade_pm10, grade_pm25, sido_name
from app.codes.kma_codes import label_sky
from app.config import settings
from app.db.forecast_repo import (
    RegionLookup,
    fetch_mid_land_range,
    fetch_mid_temp_range,
    fetch_nowcast_snapshot,
    fetch_short_term_range,
    lookup_region_by_grid,
    lookup_region_by_name,
    upsert_nowcast_snapshot,
)
from app.db.places_repo import (
    lookup_region_centroid,
    search_courses_nearby,
)
from app.hub_dependencies import (
    get_airkorea_client,
    get_kakao_client,
    get_kma_client,
    get_naver_client,
    get_osrm_client,
    get_place_cache,
)
from app.place_stubs import (
    kakao_keyword_stub,
    naver_blog_stub,
    places_stub_active,
)
from app.route_stubs import osrm_route_stub, routing_stub_active
from app.routers.guards import public_guard
from app.schemas.hub_schemas import (
    DirectionsBatchRequest,
    DirectionsBatchResponse,
    DirectionsLeg,
    DirectionsRoute,
    PlaceItem,
    PlacesResponse,
    ReviewItem,
    ReviewsResponse,
    WeatherDailyItem,
    WeatherNowAir,
    WeatherNowObservation,
    WeatherNowResponse,
    WeatherNowToday,
    WeatherNowYesterday,
    WeatherResponse,
)
from app.utils.kma_grid import gps_to_grid, resolve_nowcast_base

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

# 단기예보 horizon. D+0..D+2 까지 short_term_forecast 로 응답한다.
_SHORT_TERM_MAX_OFFSET = 2  # D+0..D+2

# 중기예보 horizon. D+3..D+10 까지 mid_land + mid_temp 합본으로 응답.
_MID_MIN_OFFSET = 3
_MID_MAX_OFFSET = 10


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
    float 를 거친 뒤 int 로 캐스트한다.

    value: 변환 대상. str | int | None
        - None 또는 빈 문자열("") 이면 None 반환(누락 데이터 표현)
        - 변환에 실패하면(TypeError, ValueError) 역시 None 반환
    반환: int 또는 None.

    사용처: _aggregate_short_term 의 TMN/TMX/TMP/POP 추출 단계.
    """
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _aggregate_short_term(
    rows: list[dict], day: date
) -> WeatherDailyItem | None:
    """_aggregate_short_term — 단기예보 raw row 를 하루 한 칸으로 집계

    fetch_short_term_range 가 돌려준 카테고리 혼합 row 리스트에서
    특정 day 에 해당하는 row 만 골라 WeatherDailyItem 한 건을 만든다.

    rows: fetch_short_term_range 결과. 각 dict 는 date/category/
        fcst_value/fcst_at 키를 가진다.
    day: 집계 대상 KST 일자.

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
          fcst_value 를 label_sky 로 한글 변환
        source: 항상 "short_term"

    반환:
        해당 day 의 row 가 하나도 없으면 None.
        있으면 WeatherDailyItem(가능한 필드만 채움).

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
    if temp_min is None and tmp:
        tmp_valid = [v for v in tmp if v is not None]
        temp_min = min(tmp_valid) if tmp_valid else None
    temp_max = next((v for v in tmx if v is not None), None)
    if temp_max is None and tmp:
        tmp_valid = [v for v in tmp if v is not None]
        temp_max = max(tmp_valid) if tmp_valid else None
    pop_valid = [v for v in pop if v is not None]
    precipitation_prob = max(pop_valid) if pop_valid else None

    sky_condition = None
    if sky_rows:
        noon = datetime.combine(day, datetime.min.time(), tzinfo=_KST).replace(
            hour=12
        )
        sky_rows.sort(
            key=lambda r: abs((r["fcst_at"] - noon).total_seconds())
        )
        sky_condition = label_sky(sky_rows[0]["fcst_value"])

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
    offset: int,
    day: date,
) -> WeatherDailyItem | None:
    """_aggregate_mid — 중기 육상+기온 row 를 하루 한 칸으로 집계

    fetch_mid_land_range / fetch_mid_temp_range 결과를 합쳐 특정 D+offset
    날짜의 WeatherDailyItem 한 건을 만든다.

    land_rows: fetch_mid_land_range 결과. offset/am_pm/weather/
        rain_prob_pct 키를 가짐
    temp_rows: fetch_mid_temp_range 결과. offset/ta_min/ta_max 키를 가짐
    offset: 집계 대상 D+N 의 N (3..10 예상)
    day: 응답에 채울 KST 일자 (offset 으로부터 계산된 결과)

    집계 규칙:
        temp_min / temp_max: temp_rows 에서 같은 offset row 의 ta_min /
            ta_max 를 그대로 사용. row 가 없으면 None
        precipitation_prob: 같은 offset 의 land_rows AM/PM 강수확률 중
            max (가장 비관적인 값 채택)
        sky_condition: 같은 offset 의 land_rows weather 텍스트 중
            가장 첫 번째 값. KMA 원문(예: "구름많음") 그대로 노출
        source: 항상 "mid_land+mid_temp"

    반환:
        해당 offset 의 land/temp row 가 모두 없으면 None.
        하나라도 있으면 WeatherDailyItem(가능한 필드만 채움).
    """
    day_land = [r for r in land_rows if r["offset"] == offset]
    day_temp = next(
        (r for r in temp_rows if r["offset"] == offset), None
    )
    if not day_land and day_temp is None:
        return None

    rain_values = [
        r["rain_prob_pct"] for r in day_land
        if r["rain_prob_pct"] is not None
    ]
    precipitation_prob = max(rain_values) if rain_values else None
    weathers = [r["weather"] for r in day_land if r["weather"]]
    sky_condition = weathers[0] if weathers else None

    return WeatherDailyItem(
        date=day,
        temp_min=day_temp["ta_min"] if day_temp else None,
        temp_max=day_temp["ta_max"] if day_temp else None,
        precipitation_prob=precipitation_prob,
        sky_condition=sky_condition,
        source="mid_land+mid_temp",
    )


def _split_dates_by_horizon(
    date_start: date, date_end: date, today: date
) -> tuple[list[date], list[date], list[date]]:
    """_split_dates_by_horizon — 요청 날짜를 horizon 별로 3 분할

    [date_start, date_end] 범위의 각 날짜를 today 기준 D+N 으로 환산해
    단기/중기/범위밖 세 그룹으로 나눈다.

    date_start / date_end: 요청 구간(양끝 포함).
    today: 기준 일자 (_today_kst() 결과).

    분류 기준:
        0 <= offset <= _SHORT_TERM_MAX_OFFSET (D+0..D+2) → short
        _MID_MIN_OFFSET <= offset <= _MID_MAX_OFFSET (D+3..D+10) → mid
        그 외(과거 일자 또는 D+11 이후) → out_of_range
            → 응답의 missing_dates 에 그대로 들어감

    반환: (short, mid, out_of_range) 세 리스트의 튜플.
        각 리스트는 입력 순서(오름차순)를 그대로 유지.
    """
    short: list[date] = []
    mid: list[date] = []
    out_of_range: list[date] = []
    cursor = date_start
    while cursor <= date_end:
        offset = (cursor - today).days
        if 0 <= offset <= _SHORT_TERM_MAX_OFFSET:
            short.append(cursor)
        elif _MID_MIN_OFFSET <= offset <= _MID_MAX_OFFSET:
            mid.append(cursor)
        else:
            out_of_range.append(cursor)
        cursor += timedelta(days=1)
    return short, mid, out_of_range


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
        3) lookup_region_by_name 결과 None → 404 ("region not found")

    처리 흐름:
        1) lookup_region_by_name 으로 RegionLookup 확보 (nx/ny + reg_id)
        2) _today_kst 와 _split_dates_by_horizon 으로 단기/중기/범위밖 분할
        3) 단기 날짜가 있으면 fetch_short_term_range 로 raw row 일괄 조회
        4) 중기 날짜가 있고 두 reg_id 모두 존재하면
           fetch_mid_land_range / fetch_mid_temp_range 호출
           (어느 한쪽이라도 None 이면 중기 호출 skip → 해당 날짜 missing)
        5) 단기 날짜: _aggregate_short_term 으로 일별 WeatherDailyItem 생성
           - 집계 결과가 None 인 날짜는 missing 에 적재
        6) 중기 날짜: reg_id 누락 시 missing, 아니면 _aggregate_mid 호출
        7) daily 는 date 오름차순 정렬, missing 도 오름차순 정렬
        8) 응답 province/city 는 RegionLookup.lv1/lv2 기준.
           lv2 가 빈 문자열(광역 fallback)이면 요청 city 를 그대로 사용.

    response_model: WeatherResponse — 직렬화·검증을 본 모델로 강제.
    """
    if date_start > date_end:
        raise HTTPException(
            status_code=400, detail="date_start must be <= date_end"
        )
    if (date_end - date_start).days > _MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"date range must be <= {_MAX_RANGE_DAYS} days",
        )

    region: RegionLookup | None = await lookup_region_by_name(
        province, city
    )
    if region is None:
        raise HTTPException(status_code=404, detail="region not found")

    today = _today_kst()
    short_days, mid_days, out_of_range = _split_dates_by_horizon(
        date_start, date_end, today
    )

    short_rows: list[dict] = []
    if short_days:
        short_rows = await fetch_short_term_range(
            region.nx, region.ny, short_days[0], short_days[-1]
        )

    land_rows: list[dict] = []
    temp_rows: list[dict] = []
    if mid_days and region.mid_land_reg_id and region.mid_temp_reg_id:
        offsets = [(d - today).days for d in mid_days]
        lo, hi = min(offsets), max(offsets)
        land_rows = await fetch_mid_land_range(
            region.mid_land_reg_id, lo, hi
        )
        temp_rows = await fetch_mid_temp_range(
            region.mid_temp_reg_id, lo, hi
        )

    daily: list[WeatherDailyItem] = []
    missing: list[date] = list(out_of_range)
    for day in short_days:
        item = _aggregate_short_term(short_rows, day)
        if item is None:
            missing.append(day)
        else:
            daily.append(item)
    for day in mid_days:
        if not region.mid_land_reg_id or not region.mid_temp_reg_id:
            missing.append(day)
            continue
        offset = (day - today).days
        item = _aggregate_mid(land_rows, temp_rows, offset, day)
        if item is None:
            missing.append(day)
        else:
            daily.append(item)
    daily.sort(key=lambda x: x.date)
    missing.sort()

    return WeatherResponse(
        province=region.lv1,
        city=region.lv2 or city,
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


async def _fetch_air(
    province: str | None, city: str | None = None
) -> WeatherNowAir | None:
    """행정구역의 대기오염 정보를 조회한다. 실패하면 None.

    미세먼지는 부가 정보라 어떤 실패도 날씨 응답 전체를 막지 않는다.
    조회는 시도 단위라 그 결과를 시도 키로 캐싱하고, 대표 측정소만 요청한
    시군구 기준으로 고른다 — 캐시를 시군구 단위로 나누면 같은 조회를 시군구
    수만큼 반복하게 된다.
    """
    sido = sido_name(province)
    if sido is None:
        return None
    client = get_airkorea_client()
    if client is None:
        return None
    cache = get_place_cache()
    key = f"airkorea:sido:{sido}"
    items: list[dict] | None = None
    if cache is not None:
        items = await cache.get_json(key)
    if items is None:
        try:
            items = await client.fetch_sido_realtime(sido)
        except AirKoreaApiError as e:
            logger.warning(
                "airkorea fetch failed sido=%s code=%s", sido, e.code
            )
            return None
        if cache is not None:
            await cache.set_json(
                key, items, settings.AIRKOREA_CACHE_TTL_SEC
            )
    station = _pick_air_station(items, city)
    if station is None:
        return None
    pm10 = _coerce_int(station.get("pm10Value"))
    pm25 = _coerce_int(station.get("pm25Value"))
    return WeatherNowAir(
        pm10=pm10,
        pm25=pm25,
        pm10_grade=grade_pm10(pm10),
        pm25_grade=grade_pm25(pm25),
        station=station.get("stationName"),
    )


@router.get("/v1/weather/now", response_model=WeatherNowResponse)
async def get_weather_now(
    lat: float = Query(..., ge=33.0, le=43.0),
    lng: float = Query(..., ge=124.0, le=132.0),
) -> WeatherNowResponse:
    """GET /v1/weather/now — 좌표 기준 현재 날씨

    홈 화면의 날씨 카드가 필요로 하는 값을 한 번에 모은다. 지금 기온과
    하늘 상태는 실황에서, 최고·최저와 강수 확률은 단기예보에서, 미세먼지는
    대기오염 정보에서 온다.

    Query 파라미터:
        lat / lng: 기기 위치. 국내 범위 밖이면 422(FastAPI 검증).

    위치 취급:
        받은 좌표는 진입 직후 격자로 바꾸고 그 뒤로는 격자만 쓴다. 로그와
        캐시 키, 저장 레코드 어디에도 원시 좌표가 남지 않는다.

    처리 흐름:
        1) 좌표 → 격자 변환, 격자로 행정구역 역조회(없으면 예보/대기는 생략)
        2) 실황 조회(짧은 캐시). 이 값이 없으면 카드를 그릴 수 없어 502
        3) 관측값을 시각 단위로 남긴다 — 내일의 "어제 비교" 근거가 된다
        4) 어제 같은 시간대 기록을 찾아 비교값으로 싣는다(없으면 생략)
        5) 오늘 단기예보를 집계해 최고·최저·강수확률·하늘상태를 채운다
        6) 대기오염을 조회해 농도와 등급을 채운다(실패하면 생략)

    response_model: WeatherNowResponse — 직렬화·검증을 본 모델로 강제.
    """
    nx, ny = gps_to_grid(lat, lng)
    now_kst = datetime.now(tz=_KST)
    base_date, base_time = resolve_nowcast_base(now_kst)

    client = get_kma_client()
    if client is None:
        raise HTTPException(
            status_code=503, detail="weather service not configured"
        )
    cache = get_place_cache()

    def _key(date_str: str, time_str: str) -> str:
        return f"weather:now:{nx}:{ny}:{date_str}{time_str}"

    obs: dict[str, str] | None = None
    if cache is not None:
        obs = await cache.get_json(_key(base_date, base_time))
    if obs is None:
        try:
            obs = await client.fetch_nowcast(nx, ny, base_date, base_time)
        except KMAApiError as e:
            # 발표 직후에는 아직 자료가 없을 수 있어 한 시간 전으로 한 번 물러선다.
            fallback = now_kst - timedelta(hours=1)
            base_date, base_time = resolve_nowcast_base(fallback)
            # 물러선 뒤에는 캐시도 먼저 본다. 앞선 요청이 같은 발표분을 이미
            # 받아 뒀다면 외부를 다시 부를 이유가 없다.
            if cache is not None:
                obs = await cache.get_json(_key(base_date, base_time))
            if obs is None:
                try:
                    obs = await client.fetch_nowcast(
                        nx, ny, base_date, base_time
                    )
                except KMAApiError:
                    logger.warning(
                        "nowcast failed grid=%d,%d code=%s", nx, ny, e.code
                    )
                    raise HTTPException(
                        status_code=502, detail="nowcast unavailable"
                    ) from e
        if cache is not None:
            # 키는 실제로 값을 받은 발표분으로 만든다. 요청 시각으로 만들면
            # 물러서서 받은 한 시간 전 값이 최신 시각 값으로 굳는다.
            await cache.set_json(
                _key(base_date, base_time),
                obs,
                settings.WEATHER_NOW_CACHE_TTL_SEC,
            )

    temp_raw = obs.get("T1H")
    if temp_raw is None:
        raise HTTPException(
            status_code=502, detail="nowcast has no temperature"
        )
    try:
        temp_c = float(temp_raw)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=502, detail="nowcast temperature malformed"
        ) from e
    pty = _coerce_int(obs.get("PTY"))

    base_hour = int(base_time[:2])
    base_day = datetime.strptime(base_date, "%Y%m%d").date()
    await upsert_nowcast_snapshot(base_day, base_hour, nx, ny, temp_c, pty)

    prev = await fetch_nowcast_snapshot(
        base_day - timedelta(days=1), base_hour, nx, ny
    )
    yesterday = (
        WeatherNowYesterday(
            temp_c=prev["temp_c"], hour_kst=prev["hour_kst"]
        )
        if prev is not None
        else None
    )

    region = await lookup_region_by_grid(nx, ny)
    today: WeatherNowToday | None = None
    if region is not None:
        # 예보는 벽시계 오늘로 본다. 실황 발표분은 자정 직후 전날이 되므로
        # 그 날짜로 조회하면 어제 예보가 오늘 자리에 실린다.
        today_kst = now_kst.date()
        rows = await fetch_short_term_range(
            region.nx, region.ny, today_kst, today_kst
        )
        item = _aggregate_short_term(rows, today_kst)
        if item is not None:
            today = WeatherNowToday(
                temp_max=item.temp_max,
                temp_min=item.temp_min,
                precipitation_prob=item.precipitation_prob,
                sky_condition=item.sky_condition,
            )

    air = await _fetch_air(
        region.lv1 if region is not None else None,
        region.lv2 if region is not None else None,
    )

    return WeatherNowResponse(
        nx=nx,
        ny=ny,
        province=region.lv1 if region is not None else None,
        city=(region.lv2 or None) if region is not None else None,
        now=WeatherNowObservation(
            temp_c=temp_c,
            pty=pty,
            base_date=base_date,
            base_time=base_time,
        ),
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
    use_stub = routing_stub_active(_osrm_base_url(req.mode))
    routes = await asyncio.gather(
        *(_route_one_leg(req.mode, leg, use_stub) for leg in req.legs)
    )
    return DirectionsBatchResponse(routes=list(routes))
