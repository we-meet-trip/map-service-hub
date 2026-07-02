"""hub-service 라우터 모듈.

외부 데이터 조회 라우터를 본 모듈에 정의한다.
장소·날씨·경로·룰 4개 도메인에 대한 REST 엔드포인트를 묶어 단일
APIRouter 인스턴스로 노출하기 위한 모듈이며, 현재는 날씨(/v1/weather)
엔드포인트만 구현되어 있다.

호출 관계:
  - GET /v1/weather (get_weather) 는 agent 의 HubClient.fetch_weather 가
    호출하는 public API 엔드포인트이다.
  - 내부적으로 forecast_repo 의 lookup_region_by_name / fetch_* 를
    호출해 raw row 를 모은 뒤, _aggregate_* 헬퍼로 일별 집계한다.

응답 모델:
  - WeatherDailyItem / WeatherResponse (app.schemas.hub_schemas)
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from app.clients.hub_clients import KakaoApiError
from app.codes.kma_codes import label_sky
from app.config import settings
from app.db.forecast_repo import (
    RegionLookup,
    fetch_mid_land_range,
    fetch_mid_temp_range,
    fetch_short_term_range,
    lookup_region_by_name,
)
from app.db.places_repo import (
    lookup_region_centroid,
    search_courses_nearby,
)
from app.hub_dependencies import get_kakao_client, get_place_cache
from app.place_stubs import kakao_keyword_stub, places_stub_active
from app.schemas.hub_schemas import (
    PlaceItem,
    PlacesResponse,
    WeatherDailyItem,
    WeatherResponse,
)

logger = logging.getLogger(__name__)

# 본 모듈의 모든 라우트를 묶는 APIRouter. main 앱에서 include_router 로 등록.
router = APIRouter()

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


def _brd_div_for_mobility(mobility: str | None) -> str | None:
    """이동수단을 코스 걷기/자전거 구분 코드로 매핑한다.

    walk → 걷기길(DNWW), bicycle → 자전거길(DNBW),
    그 외/미지정 → 구분 없음(전체).
    """
    if mobility == "walk":
        return "DNWW"
    if mobility == "bicycle":
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
