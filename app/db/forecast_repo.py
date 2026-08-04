"""hub_data forecast 테이블 read/upsert 레이어.

본 모듈은 KMA 단기/중기 예보 데이터를 hub_data 스키마의 4개 테이블
(subscribed_grids / short_term_forecast / mid_land_forecast /
mid_temp_forecast) 에 적재·조회·만료하는 모든 SQL 을 한 곳에 모은다.

호출 관계:
  - app.scheduler.hub_scheduler  → load_active_grids / is_*_loaded /
      upsert_* / housekeeping_expire
  - app.routers.hub_routers      → lookup_region_by_name /
      fetch_short_term_range / fetch_mid_land_range / fetch_mid_temp_range
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import text

from app.config import settings
from app.db.hub_db import get_hub_db
from app.utils.kma_grid import KST, parse_kma_fcst_at

logger = logging.getLogger(__name__)


# 단기예보 캐시 만료 — base_at 발표 시각 기준 6시간 후 만료.
# upsert_short_term_items 가 모든 row 에 base_at + _SHORT_TTL 로 expires_at 지정.
_SHORT_TTL = timedelta(hours=6)
# 중기예보 캐시 만료 — tm_fc 발표 시각 기준 24시간 후 만료.
# upsert_mid_land / upsert_mid_temp 가 사용.
_MID_TTL = timedelta(hours=24)

# 저장하는 단기예보 항목. short_term_forecast 테이블의 category CHECK 와
# 같은 목록이어야 한다 — 여기가 넓으면 적재가 제약에 걸려 그 격자의 예보가
# 통째로 롤백되고, 좁으면 받아 온 값을 조용히 버린다.
# 기상청이 항목을 추가하면 이 상수와 테이블 제약을 함께 넓혀야 한다.
_SHORT_TERM_CATEGORIES = frozenset(
    {
        "PCP", "POP", "PTY", "REH", "SKY", "SNO", "TMN",
        "TMP", "TMX", "UUU", "VEC", "VVV", "WAV", "WSD",
    }
)


@dataclass(slots=True)
class SubscribedGrid:
    """SubscribedGrid — 폴링 대상 격자/지역코드 묶음

    hub_data.subscribed_grids 의 활성(is_active) row 한 건을 표현한다.
    load_active_grids 가 반환하는 형태이며, 폴링 루프가 한 건씩 순회해
    KMA API 호출 키로 사용한다.

    grid_id: subscribed_grids 의 PK (BIGSERIAL).
    label: 사람이 읽을 수 있는 식별자(예: "서울특별시"). 로그용.
    nx / ny: KMA 격자 좌표. 단기예보 API 호출 시 사용된다.
    mid_land_reg_id: 중기 육상예보 지역코드.
    mid_temp_reg_id: 중기 기온예보 지역코드.

    호출처: load_active_grids 의 반환 타입 / hub_scheduler 의 폴링 루프.
    """

    grid_id: int
    label: str
    nx: int
    ny: int
    mid_land_reg_id: str
    mid_temp_reg_id: str
    # 이 격자가 속한 광역시도. 예보를 내줄 격자를 고를 때 도를 넘지 않도록
    # 거르는 데 쓴다. 참조가 깨진 행도 폴링 대상에서 빠지지 않게 기본값을 둔다.
    lv1: str = ""


@dataclass(slots=True)
class RegionLookup:
    """RegionLookup — 행정구역 + 격자 + 중기 reg_id 묶음

    (province, city) 명으로 region_grid 를 조회해 얻은 대표 행정구역
    한 건과, 거기에 매칭되는 subscribed_grids 의 중기예보 reg_id 를
    하나의 결과 객체로 묶어 반환할 때 사용한다.

    admin_code: 행정 표준코드(region_grid.admin_code). 시군구 식별자
    lv1: 광역시도 명 (예: "서울특별시")
    lv2: 시군구 명 (예: "강남구"). 광역 대표 row(fallback)일 때는
        빈 문자열 "" 일 수 있다.
    nx / ny: 단기예보용 KMA 격자 X/Y 좌표. fetch_short_term_range 의
        조회 키로 사용된다.
    mid_land_reg_id: 중기육상예보 지역코드. 매칭되는 활성 grid 가 없으면
        None — 이 경우 중기 horizon 의 해당 날짜들은 missing 처리된다.
    mid_temp_reg_id: 중기기온예보 지역코드. None 이면 위와 동일.

    사용처: lookup_region_by_name 의 반환 타입.
        호출자 hub_routers.get_weather 가 본 객체의 nx/ny 와 mid_*_reg_id
        를 그대로 fetch_* 계열 쿼리에 넘긴다.
    """

    admin_code: str
    lv1: str
    lv2: str
    nx: int
    ny: int
    mid_land_reg_id: str | None
    mid_temp_reg_id: str | None


def pick_serving_grid(
    grids: "list[SubscribedGrid]", lv1: str, nx: int, ny: int
) -> "SubscribedGrid | None":
    """예보를 내줄 격자를 고른다 — 같은 광역 안에서 가장 가까운 곳.

    예보는 구독한 격자에만 쌓인다. 요청한 시군구의 격자가 구독 대상이
    아니면 그 좌표로 조회해 봐야 늘 비어 있으므로, 실제로 값이 있는
    격자로 바꿔서 조회해야 한다.

    **광역은 넘지 않는다.** 거리만 보면 경기 가평군은 경기 대표(60,120,
    거리제곱 250)보다 강원 영서(73,134, 17)가 14배 가깝다. 그런데 중기
    예보구역은 기하가 아니라 행정 경계로 나뉘어서, 가평의 중기 구역은
    경기이지 영서가 아니다. 도를 넘어 고르면 그럴듯하지만 틀린 예보가
    나간다. 도내에 쓸 격자가 없으면 None 을 돌려 지금처럼 결측으로 둔다 —
    옆 도 예보를 내주는 것보다 없다고 말하는 편이 정직하다.

    거리는 격자 좌표의 제곱합으로 잰다. KMA 격자는 5km 등간격이고
    한반도 범위에서 칸 크기 편차가 1.3% 미만이라 실거리로 환산할 필요가 없다.
    같은 거리면 grid_id 가 작은 쪽을 고른다 — 정하지 않으면 같은 요청이
    호출마다 다른 답을 낼 수 있다.

    grids: 활성 구독 격자 목록.
    lv1 / nx / ny: 요청한 행정구역의 광역명과 격자 좌표.
    반환: 고른 격자. 그 광역에 활성 격자가 없으면 None.
    """
    in_province = [g for g in grids if g.lv1 == lv1]
    if not in_province:
        return None
    return min(
        in_province,
        key=lambda g: (
            (g.nx - nx) * (g.nx - nx) + (g.ny - ny) * (g.ny - ny),
            g.grid_id,
        ),
    )


def build_region_lookup(
    admin_code: str,
    lv1: str,
    lv2: str,
    nx: int,
    ny: int,
    grid: "SubscribedGrid | None",
) -> "RegionLookup":
    """행정구역과 고른 격자를 합쳐 조회 결과를 만든다.

    격자를 골랐으면 그 좌표를 싣는다 — 예보가 실제로 쌓여 있는 자리다.
    못 골랐으면 요청한 좌표를 그대로 두고 중기 코드는 비운다. 그러면
    하류가 중기 조회를 건너뛰고 그 날짜들을 결측으로 처리한다.
    """
    return RegionLookup(
        admin_code=admin_code,
        lv1=lv1,
        lv2=lv2,
        nx=grid.nx if grid else nx,
        ny=grid.ny if grid else ny,
        mid_land_reg_id=grid.mid_land_reg_id if grid else None,
        mid_temp_reg_id=grid.mid_temp_reg_id if grid else None,
    )


# 활성 구독 격자 조회. 광역명은 region_grid 에서 끌어오되 LEFT JOIN 이다 —
# 참조가 깨져도 폴링 대상에서 격자가 조용히 사라지지 않게 한다. 조회가 하나
# 틀리는 것보다 적재가 멎는 쪽이 훨씬 나쁘다.
_ACTIVE_GRIDS_SQL = text(
    """
    SELECT sg.grid_id, sg.label, sg.nx, sg.ny,
           sg.mid_land_reg_id, sg.mid_temp_reg_id,
           COALESCE(rg.lv1, '') AS lv1
    FROM hub_data.subscribed_grids sg
    LEFT JOIN hub_data.region_grid rg ON rg.admin_code = sg.admin_code
    WHERE sg.is_active
    ORDER BY sg.grid_id
    """
)


async def _select_active_grids(s) -> list[SubscribedGrid]:
    """이미 열려 있는 세션에서 활성 격자를 읽는다.

    조회 함수들이 자기 세션 안에서 이걸 부르게 해, 요청 하나가 커넥션을
    두 개 잡지 않도록 한다(풀이 5+5뿐이다).
    """
    rows = (await s.execute(_ACTIVE_GRIDS_SQL)).all()
    # 위치가 아니라 이름으로 만든다. 필드를 하나 더하는 순간 위치 결합은
    # 조용히 어긋나고, 그 사고는 런타임에야 드러난다.
    return [SubscribedGrid(**r._mapping) for r in rows]


async def load_active_grids() -> list[SubscribedGrid]:
    """load_active_grids — 활성 폴링 대상 격자 전체 조회

    hub_data.subscribed_grids 에서 is_active = TRUE 인 row 를 grid_id
    오름차순으로 모두 가져와 SubscribedGrid 의 list 로 반환한다.

    호출처: hub_scheduler.short_term_polling_loop /
        hub_scheduler.mid_term_polling_loop — 매 라운드 시작 시 호출.
    """
    async with get_hub_db().session() as s:
        return await _select_active_grids(s)


async def loaded_short_term_grids(
    base_at: datetime,
) -> set[tuple[int, int]]:
    """이 발표분이 이미 적재된 격자들을 한 번에 돌려준다.

    격자마다 따로 물어보면 격자 수만큼 트랜잭션이 열린다.
    구독 격자가 늘어날수록 그 왕복이 폴링 라운드마다 반복되고, 커넥션 풀을
    공개 엔드포인트와 다투게 된다. 판정에 필요한 것은 "어느 격자가 이미
    있는가" 하나뿐이라 조회도 한 번이면 된다.

    base_at: KMA 단기예보 발표 시각(KST).
    반환: 적재된 (nx, ny) 집합.

    호출처: hub_scheduler.short_term_polling_loop.
    """
    sql = text(
        """
        SELECT DISTINCT nx, ny
        FROM hub_data.short_term_forecast
        WHERE base_at = :base_at
        """
    )
    async with get_hub_db().session() as s:
        rows = (await s.execute(sql, {"base_at": base_at})).all()
    return {(r.nx, r.ny) for r in rows}


async def is_mid_land_loaded(reg_id: str, tm_fc: datetime) -> bool:
    """is_mid_land_loaded — 중기 육상예보 발표분 적재 여부 확인

    (reg_id, tm_fc) 조합으로 mid_land_forecast 에 row 가 존재하면 True.

    reg_id: 중기 육상예보 지역코드.
    tm_fc: KMA 중기예보 발표 시각(timezone-aware datetime, KST).

    호출처: hub_scheduler.mid_term_polling_loop —
        활성 grid 의 mid_land_reg_id 별로 호출.
    """
    sql = text(
        """
        SELECT 1 FROM hub_data.mid_land_forecast
        WHERE reg_id = :reg_id AND tm_fc = :tm_fc
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        r = await s.execute(
            sql, {"reg_id": reg_id, "tm_fc": tm_fc}
        )
        return r.first() is not None


async def is_mid_temp_loaded(reg_id: str, tm_fc: datetime) -> bool:
    """is_mid_temp_loaded — 중기 기온예보 발표분 적재 여부 확인

    (reg_id, tm_fc) 조합으로 mid_temp_forecast 에 row 가 존재하면 True.

    reg_id: 중기 기온예보 지역코드 (육상예보 코드와 다른 체계).
    tm_fc: KMA 중기예보 발표 시각.

    호출처: hub_scheduler.mid_term_polling_loop.
    """
    sql = text(
        """
        SELECT 1 FROM hub_data.mid_temp_forecast
        WHERE reg_id = :reg_id AND tm_fc = :tm_fc
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        r = await s.execute(
            sql, {"reg_id": reg_id, "tm_fc": tm_fc}
        )
        return r.first() is not None


async def upsert_short_term_items(
    nx: int,
    ny: int,
    base_at: datetime,
    items: list[dict],
) -> int:
    """upsert_short_term_items — 단기예보 item 일괄 적재

    KMAClient.fetch_short_term 이 반환한 raw item 목록을
    short_term_forecast 테이블에 ON CONFLICT 로 upsert 한다.

    nx / ny: KMA 격자 좌표.
    base_at: 발표 시각(KST). 모든 row 의 base_at 컬럼과
        expires_at = base_at + _SHORT_TTL 계산에 사용된다.
    items: KMA item dict 의 list. 각 항목은 fcstDate/fcstTime/
        category/fcstValue 키를 가진다.

    동작:
      - items 가 비어 있으면 0 반환 (no-op).
      - 각 item 의 (fcstDate, fcstTime) 을 parse_kma_fcst_at 으로
        timezone-aware datetime 으로 변환해 fcst_at 컬럼에 저장.
      - PK (nx, ny, fcst_at, category) 충돌 시 base_at / fcst_value /
        expires_at / updated_at 만 갱신.

    **아는 카테고리만 담는다.** 테이블에 카테고리 화이트리스트 제약이
    걸려 있어서, 목록 밖 값이 하나라도 섞이면 이 격자의 예보 전체가
    한 트랜잭션에서 롤백된다. 기상청이 항목을 추가하면 그 순간 그 격자가
    통째로 비는 것이라, 모르는 항목은 세어서 남기고 나머지를 저장한다.
    형식이 깨진 항목도 같은 이유로 건너뛴다.

    반환: 실제로 담은 row 수(int).
    호출처: hub_scheduler.short_term_polling_loop.
    """
    if not items:
        return 0
    expires_at = base_at + _SHORT_TTL
    rows: list[dict] = []
    skipped: dict[str, int] = {}
    for it in items:
        category = it.get("category")
        if category not in _SHORT_TERM_CATEGORIES:
            skipped[str(category)] = skipped.get(str(category), 0) + 1
            continue
        try:
            fcst_at = parse_kma_fcst_at(it["fcstDate"], it["fcstTime"])
        except (KeyError, ValueError, TypeError):
            skipped["malformed"] = skipped.get("malformed", 0) + 1
            continue
        rows.append(
            {
                "nx": nx,
                "ny": ny,
                "fcst_at": fcst_at,
                "category": category,
                "base_at": base_at,
                "fcst_value": str(it.get("fcstValue")),
                "expires_at": expires_at,
            }
        )
    if skipped:
        # 조용히 버리면 기상청이 항목을 바꾼 것을 아무도 모른다.
        logger.warning(
            "short_term skipped unknown items nx=%s ny=%s detail=%s",
            nx, ny, skipped,
        )
    if not rows:
        return 0
    sql = text(
        """
        INSERT INTO hub_data.short_term_forecast
          (nx, ny, fcst_at, category, base_at, fcst_value,
           expires_at, updated_at)
        VALUES
          (:nx, :ny, :fcst_at, :category, :base_at, :fcst_value,
           :expires_at, now())
        ON CONFLICT (nx, ny, fcst_at, category) DO UPDATE SET
          base_at    = EXCLUDED.base_at,
          fcst_value = EXCLUDED.fcst_value,
          expires_at = EXCLUDED.expires_at,
          updated_at = now()
        """
    )
    async with get_hub_db().session() as s:
        await s.execute(sql, rows)
    logger.info(
        "short_term upserted nx=%s ny=%s base_at=%s rows=%d",
        nx, ny, base_at, len(rows),
    )
    return len(rows)


def _safe_int(v: object) -> int | None:
    """_safe_int — 임의 값을 안전하게 int 로 변환

    KMA 중기예보 payload 의 정수 필드는 결측 시 None / "" / "-" 등으로
    오기 때문에 일률적으로 int() 를 호출하면 예외가 난다. 본 함수는
    None / 빈 문자열 / 변환 실패 케이스를 모두 None 으로 통일한다.

    v: 변환 대상. payload.get(...) 의 결과(임의 타입).
    반환: 정수로 해석 가능하면 int, 그 외 None.
    호출처: upsert_mid_land / upsert_mid_temp 내부의 row 구성 단계.
    """
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def upsert_mid_land(
    reg_id: str,
    tm_fc: datetime,
    payload: dict,
) -> int:
    """upsert_mid_land — 중기 육상예보 1발표분 적재

    KMAClient.fetch_mid_land 가 반환한 단일 payload 를 11개 row 로 분해해
    mid_land_forecast 테이블에 upsert 한다.

    reg_id: 중기 육상예보 지역코드.
    tm_fc: 발표 시각(KST). expires_at = tm_fc + _MID_TTL 로 만료 지정.
    payload: KMA 원본 dict.
        - day 4..7: wf{day}Am / wf{day}Pm / rnSt{day}Am / rnSt{day}Pm
          (오전/오후 분리 → AM/PM 2 row × 4일 = 8 row)
        - day 8..10: wf{day} / rnSt{day}
          (단일 → NA 1 row × 3일 = 3 row)
        합계 11 row.

    PK (reg_id, tm_fc, fcst_day_offset, am_pm) 충돌 시 weather /
    rain_prob_pct / expires_at / updated_at 만 갱신.

    반환: 처리된 row 수(int) — 항상 11.
    호출처: hub_scheduler.mid_term_polling_loop.
    """
    expires_at = tm_fc + _MID_TTL
    rows: list[dict] = []
    for day in (4, 5, 6, 7):
        for ampm, suffix in (("AM", "Am"), ("PM", "Pm")):
            rows.append(
                {
                    "reg_id": reg_id,
                    "tm_fc": tm_fc,
                    "fcst_day_offset": day,
                    "am_pm": ampm,
                    "weather": payload.get(f"wf{day}{suffix}"),
                    "rain_prob_pct": _safe_int(
                        payload.get(f"rnSt{day}{suffix}")
                    ),
                    "expires_at": expires_at,
                }
            )
    for day in (8, 9, 10):
        rows.append(
            {
                "reg_id": reg_id,
                "tm_fc": tm_fc,
                "fcst_day_offset": day,
                "am_pm": "NA",
                "weather": payload.get(f"wf{day}"),
                "rain_prob_pct": _safe_int(payload.get(f"rnSt{day}")),
                "expires_at": expires_at,
            }
        )
    sql = text(
        """
        INSERT INTO hub_data.mid_land_forecast
          (reg_id, tm_fc, fcst_day_offset, am_pm, weather,
           rain_prob_pct, expires_at, updated_at)
        VALUES
          (:reg_id, :tm_fc, :fcst_day_offset, :am_pm, :weather,
           :rain_prob_pct, :expires_at, now())
        ON CONFLICT (reg_id, tm_fc, fcst_day_offset, am_pm) DO UPDATE
        SET weather       = EXCLUDED.weather,
            rain_prob_pct = EXCLUDED.rain_prob_pct,
            expires_at    = EXCLUDED.expires_at,
            updated_at    = now()
        """
    )
    async with get_hub_db().session() as s:
        await s.execute(sql, rows)
    logger.info(
        "mid_land upserted reg_id=%s tm_fc=%s rows=%d",
        reg_id, tm_fc, len(rows),
    )
    return len(rows)


async def upsert_mid_temp(
    reg_id: str,
    tm_fc: datetime,
    payload: dict,
) -> int:
    """upsert_mid_temp — 중기 기온예보 1발표분 적재

    KMAClient.fetch_mid_temp 가 반환한 단일 payload 를 7개 row 로 분해해
    mid_temp_forecast 테이블에 upsert 한다.

    reg_id: 중기 기온예보 지역코드.
    tm_fc: 발표 시각(KST). expires_at = tm_fc + _MID_TTL.
    payload: KMA 원본 dict. day 4..10 각각에 대해
        taMin{day}, taMin{day}Low, taMin{day}High,
        taMax{day}, taMax{day}Low, taMax{day}High 6개 정수 필드.

    PK (reg_id, tm_fc, fcst_day_offset) 충돌 시 ta_* 6개 컬럼과
    expires_at / updated_at 만 갱신.

    반환: 처리된 row 수(int) — 항상 7.
    호출처: hub_scheduler.mid_term_polling_loop.
    """
    expires_at = tm_fc + _MID_TTL
    rows: list[dict] = []
    for day in range(4, 11):
        rows.append(
            {
                "reg_id": reg_id,
                "tm_fc": tm_fc,
                "fcst_day_offset": day,
                "ta_min": _safe_int(payload.get(f"taMin{day}")),
                "ta_min_low": _safe_int(
                    payload.get(f"taMin{day}Low")
                ),
                "ta_min_high": _safe_int(
                    payload.get(f"taMin{day}High")
                ),
                "ta_max": _safe_int(payload.get(f"taMax{day}")),
                "ta_max_low": _safe_int(
                    payload.get(f"taMax{day}Low")
                ),
                "ta_max_high": _safe_int(
                    payload.get(f"taMax{day}High")
                ),
                "expires_at": expires_at,
            }
        )
    sql = text(
        """
        INSERT INTO hub_data.mid_temp_forecast
          (reg_id, tm_fc, fcst_day_offset, ta_min, ta_min_low,
           ta_min_high, ta_max, ta_max_low, ta_max_high,
           expires_at, updated_at)
        VALUES
          (:reg_id, :tm_fc, :fcst_day_offset, :ta_min, :ta_min_low,
           :ta_min_high, :ta_max, :ta_max_low, :ta_max_high,
           :expires_at, now())
        ON CONFLICT (reg_id, tm_fc, fcst_day_offset) DO UPDATE
        SET ta_min      = EXCLUDED.ta_min,
            ta_min_low  = EXCLUDED.ta_min_low,
            ta_min_high = EXCLUDED.ta_min_high,
            ta_max      = EXCLUDED.ta_max,
            ta_max_low  = EXCLUDED.ta_max_low,
            ta_max_high = EXCLUDED.ta_max_high,
            expires_at  = EXCLUDED.expires_at,
            updated_at  = now()
        """
    )
    async with get_hub_db().session() as s:
        await s.execute(sql, rows)
    logger.info(
        "mid_temp upserted reg_id=%s tm_fc=%s rows=%d",
        reg_id, tm_fc, len(rows),
    )
    return len(rows)


async def lookup_region_by_name(
    province: str, city: str
) -> RegionLookup | None:
    """lookup_region_by_name — (광역시도, 시군구) → RegionLookup 조회

    행정구역 명으로 region_grid 의 대표 row 를 찾고, 그 행정구역에
    가장 가까운 활성 subscribed_grids row 의 중기 reg_id 한 쌍을
    묶어 RegionLookup 으로 반환한다.

    province: 광역시도 명. region_grid.lv1 과 일치해야 한다.
    city: 시군구 명. 우선 region_grid.lv2 와 일치하는 lv3='' 인
        대표 row 를 찾는다.

    조회 절차:
      1) primary_sql — (lv1=province, lv2=city, lv3='') 대표 row 시도
      2) 실패 시 fallback_sql — (lv1=province, lv2='', lv3='') 광역 대표
         row 로 fallback (city 매칭 실패 케이스)
      3) 두 단계 모두 row 가 없으면 None 반환
      4) pick_serving_grid 로 같은 광역 안에서 가장 가까운 활성 격자를 고른다
      5) build_region_lookup 이 그 격자의 좌표와 중기 코드를 실어 돌려준다

    **돌려주는 nx/ny 는 요청한 시군구의 격자가 아니라 예보를 적재해 둔
    격자다.** 예보는 구독 격자에만 쌓이므로, 요청 격자로 조회하면 그 격자를
    구독하지 않는 한 늘 비어 나온다.

    반환:
        매칭된 행정구역이 없으면 None.
        있으면 RegionLookup — 중기 reg_id 는 고른 격자가 없으면 None.

    호출처: hub_routers.get_weather — 요청의 region 식별 단계에서
        사용되며, 결과가 None 이면 404 를 발생시킨다.
    """
    # 시군구 대표행이 있으면 그것을, 없으면 그 시군구의 첫 읍면동을 쓴다.
    # 시드에 대표행이 빠진 시군구가 실제로 있다(강원 18곳 중 9곳). 대표행만
    # 찾으면 그런 곳이 통째로 광역 폴백으로 떨어져, 시군구를 정확히 적어
    # 보낸 요청이 오히려 광역 예보를 받는다. 읍면동 좌표라도 그 시군구
    # 안에 있으므로 격자는 대체로 맞는다.
    # admin_code 로 순서를 못박아 같은 요청이 늘 같은 답을 내게 한다.
    primary_sql = text(
        """
        SELECT admin_code, lv1, lv2, nx, ny
        FROM hub_data.region_grid
        WHERE lv1 = :province AND lv2 = :city
        ORDER BY (CASE WHEN lv3 = '' THEN 0 ELSE 1 END), admin_code
        LIMIT 1
        """
    )
    fallback_sql = text(
        """
        SELECT admin_code, lv1, lv2, nx, ny
        FROM hub_data.region_grid
        WHERE lv1 = :province AND lv2 = '' AND lv3 = ''
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        row = (
            await s.execute(
                primary_sql, {"province": province, "city": city}
            )
        ).first()
        if row is None:
            row = (
                await s.execute(fallback_sql, {"province": province})
            ).first()
        if row is None:
            return None
        grids = await _select_active_grids(s)
    grid = pick_serving_grid(grids, row.lv1, row.nx, row.ny)
    return build_region_lookup(
        row.admin_code, row.lv1, row.lv2, row.nx, row.ny, grid
    )


async def fetch_short_term_range(
    nx: int, ny: int, date_start: date, date_end: date
) -> list[dict]:
    """fetch_short_term_range — 단기예보 raw row 범위 조회

    KMA 격자 (nx, ny) 위치에서 [date_start, date_end] 구간(KST 일자 기준)
    에 해당하는 short_term_forecast row 를 그대로 노출한다.
    일별 집계(min/max/대표값 선택)는 본 함수가 아니라 호출자가 수행한다.

    nx / ny: KMA 격자 좌표. region 의 nx/ny 가 그대로 들어온다.
    date_start / date_end: 조회 구간(양끝 포함, KST 일자).
        WHERE 절에서 fcst_at 을 'Asia/Seoul' 로 캐스트한 뒤 BETWEEN 비교.

    반환: list[dict] — 각 row 는 다음 키를 가진다
        date: 예보가 가리키는 KST 일자 (datetime.date)
        category: KMA 카테고리 문자열 (TMN/TMX/TMP/POP/SKY/PTY/REH/...)
        fcst_value: 해당 카테고리의 예보값(문자열 그대로)
        fcst_at: 예보 시각(timezone-aware datetime)
        ORDER BY (fcst_at, category) 로 정렬되어 들어온다.

    도메인 용어:
        TMN/TMX: 일 최저/최고 기온
        TMP: 시간별 기온
        POP: 강수 확률(%)
        SKY: 하늘 상태 코드 (kma_codes.SKY_LABEL 참조)
        PTY: 강수 형태 코드

    호출처: hub_routers.get_weather — 단기 horizon(D+0..D+2) 날짜가
        있을 때 본 함수를 호출하고 _aggregate_short_term 으로 집계.
    """
    # KST 일자 경계를 datetime 으로 환산해 fcst_at 컬럼에 직접 범위 비교한다.
    # WHERE 에 (fcst_at AT TIME ZONE 'Asia/Seoul')::date 캐스트를 쓰면 PK
    # (nx, ny, fcst_at, category) 인덱스 범위 스캔을 못 타므로, 캐스트 대신
    # 반열린 구간 [start_dt, end_dt) 로 비교한다(끝 배타 = date_end + 1일).
    # KST 는 DST 가 없어 ::date BETWEEN 과 의미가 동일하다.
    start_dt = datetime.combine(date_start, datetime.min.time(), tzinfo=KST)
    end_dt = datetime.combine(
        date_end + timedelta(days=1), datetime.min.time(), tzinfo=KST
    )
    sql = text(
        """
        SELECT
          (fcst_at AT TIME ZONE 'Asia/Seoul')::date AS d,
          category,
          fcst_value,
          fcst_at
        FROM hub_data.short_term_forecast
        WHERE nx = :nx AND ny = :ny
          AND fcst_at >= :start_dt AND fcst_at < :end_dt
        ORDER BY fcst_at, category
        """
    )
    async with get_hub_db().session() as s:
        rows = (
            await s.execute(
                sql,
                {"nx": nx, "ny": ny, "start_dt": start_dt, "end_dt": end_dt},
            )
        ).all()
    return [
        {
            "date": r.d,
            "category": r.category,
            "fcst_value": r.fcst_value,
            "fcst_at": r.fcst_at,
        }
        for r in rows
    ]


async def fetch_mid_land_range(
    reg_id: str, offset_lo: int, offset_hi: int
) -> list[dict]:
    """fetch_mid_land_range — 중기 육상예보 raw row 범위 조회

    중기 육상예보(mid_land_forecast)에서 reg_id 에 대해
    "가장 최근 발표분(tm_fc)" 만을 골라, fcst_day_offset 이
    [offset_lo, offset_hi] 인 row 를 반환한다.

    reg_id: 중기 육상예보 지역코드 (RegionLookup.mid_land_reg_id).
    offset_lo / offset_hi: 발표 기준 D+N 의 N 범위(양끝 포함).
        실제 mid_land 저장 정책상 4..10 사이의 값이 들어온다
        (upsert_mid_land 가 4..10 만 저장하기 때문).

    SQL 구조:
        latest CTE 가 같은 reg_id 의 최신 tm_fc 를 구해
        본 쿼리에서 그 발표분만 필터링한다.
        ORDER BY (fcst_day_offset, am_pm) 로 정렬.

    반환: list[dict] — 각 row 는 다음 키를 가진다
        offset: fcst_day_offset (int, D+N 의 N)
        am_pm: "AM" | "PM" | "NA" — 4..7 은 AM/PM 둘 다 들어오고,
            8..10 은 NA 한 건만 들어온다(upsert 정책에 따름).
        weather: 날씨 텍스트 (예: 원문 "구름많음"). 없으면 None
        rain_prob_pct: 강수 확률(%). 없으면 None

    호출처: hub_routers.get_weather — 중기 horizon(D+3..D+10) 날짜가
        있고 mid_land_reg_id 가 존재할 때 호출. _aggregate_mid 가
        offset 별로 묶어 일별 WeatherDailyItem 으로 집계한다.
    """
    sql = text(
        """
        WITH latest AS (
          SELECT MAX(tm_fc) AS tm_fc
          FROM hub_data.mid_land_forecast
          WHERE reg_id = :reg_id
        )
        SELECT fcst_day_offset, am_pm, weather, rain_prob_pct
        FROM hub_data.mid_land_forecast
        WHERE reg_id = :reg_id
          AND fcst_day_offset BETWEEN :lo AND :hi
          AND tm_fc = (SELECT tm_fc FROM latest)
        ORDER BY fcst_day_offset, am_pm
        """
    )
    async with get_hub_db().session() as s:
        rows = (
            await s.execute(
                sql,
                {"reg_id": reg_id, "lo": offset_lo, "hi": offset_hi},
            )
        ).all()
    return [
        {
            "offset": r.fcst_day_offset,
            "am_pm": r.am_pm,
            "weather": r.weather,
            "rain_prob_pct": r.rain_prob_pct,
        }
        for r in rows
    ]


async def fetch_mid_temp_range(
    reg_id: str, offset_lo: int, offset_hi: int
) -> list[dict]:
    """fetch_mid_temp_range — 중기 기온예보 raw row 범위 조회

    중기 기온예보(mid_temp_forecast)에서 reg_id 에 대해
    "가장 최근 발표분(tm_fc)" 만을 골라, fcst_day_offset 이
    [offset_lo, offset_hi] 인 row 를 반환한다.

    reg_id: 중기 기온예보 지역코드 (RegionLookup.mid_temp_reg_id).
        육상예보 reg_id 와 코드 체계가 다르므로 혼동하지 말 것.
    offset_lo / offset_hi: D+N 의 N 범위(양끝 포함).
        upsert_mid_temp 는 4..10 까지 저장한다.

    SQL 구조:
        latest CTE 가 같은 reg_id 의 최신 tm_fc 를 구해
        본 쿼리에서 그 발표분만 필터링한다.
        ORDER BY fcst_day_offset 정렬.

    반환: list[dict] — 각 row 는 다음 키를 가진다
        offset: fcst_day_offset (int)
        ta_min: 일 최저기온(℃). 없으면 None
        ta_max: 일 최고기온(℃). 없으면 None

    호출처: hub_routers.get_weather — 중기 horizon 날짜가 있고
        mid_temp_reg_id 가 존재할 때 호출. _aggregate_mid 에서
        offset 매칭으로 land row 와 합쳐져 WeatherDailyItem 이 된다.
    """
    sql = text(
        """
        WITH latest AS (
          SELECT MAX(tm_fc) AS tm_fc
          FROM hub_data.mid_temp_forecast
          WHERE reg_id = :reg_id
        )
        SELECT fcst_day_offset, ta_min, ta_max
        FROM hub_data.mid_temp_forecast
        WHERE reg_id = :reg_id
          AND fcst_day_offset BETWEEN :lo AND :hi
          AND tm_fc = (SELECT tm_fc FROM latest)
        ORDER BY fcst_day_offset
        """
    )
    async with get_hub_db().session() as s:
        rows = (
            await s.execute(
                sql,
                {"reg_id": reg_id, "lo": offset_lo, "hi": offset_hi},
            )
        ).all()
    return [
        {
            "offset": r.fcst_day_offset,
            "ta_min": r.ta_min,
            "ta_max": r.ta_max,
        }
        for r in rows
    ]


async def lookup_region_by_grid(nx: int, ny: int) -> RegionLookup | None:
    """lookup_region_by_grid — 격자 좌표 → RegionLookup 역조회

    기기 위치를 격자로 바꾼 뒤, 그 격자를 대표 격자로 쓰는 행정구역을 찾는다.
    행정구역 명을 얻어야 예보 조회와 대기오염 조회로 이어갈 수 있다.

    nx / ny: gps_to_grid 로 변환한 격자 좌표.

    조회 절차:
      1) 같은 격자를 쓰는 region_grid row 를 모두 후보로 본다. 읍면동까지
         포함해야 한다 — 격자가 5km 라 시군구 대표 격자와 정확히 겹치는
         위치는 드물고, 대표 row 만 찾으면 국내 상당수 지점에서 행정구역을
         못 찾아 예보와 대기 정보가 통째로 빠진다.
      2) 같은 격자 안에서는 시군구 대표 → 읍면동 → 광역 대표 순으로 고른다.
         행정구역 명만 필요하므로 어느 읍면동을 고르든 시군구·광역은 같다.
      3) 후보가 없으면 None — 호출 측이 예보 없이 실황만 돌려준다.
      4) 있으면 같은 광역시도의 활성 grid 에서 중기 reg_id 와 그 격자를
         함께 싣는다.

    돌려주는 nx/ny 는 요청 격자가 아니라 예보를 적재해 둔 격자다. 예보는
    구독 격자에만 쌓이므로 요청 격자로 조회하면 그 18곳에 서 있지 않은 한
    늘 비어 버린다. 같은 시도의 구독 격자 예보를 대신 쓰면 하루 최고·최저와
    강수 확률 정도는 보여 줄 수 있다. 구독 격자가 없으면 요청 격자를 그대로
    둔다(그 경우 예보는 비게 된다).

    반환: 매칭되는 행정구역이 없으면 None, 있으면 RegionLookup.
    호출처: hub_routers.get_weather_now.
    """
    region_sql = text(
        """
        SELECT admin_code, lv1, lv2, nx, ny
        FROM hub_data.region_grid
        WHERE nx = :nx AND ny = :ny
        ORDER BY
          CASE
            WHEN lv2 <> '' AND lv3 = '' THEN 0
            WHEN lv2 <> '' THEN 1
            ELSE 2
          END,
          admin_code
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        row = (
            await s.execute(region_sql, {"nx": nx, "ny": ny})
        ).first()
        if row is None:
            return None
        grids = await _select_active_grids(s)
    # 요청 격자에서 가장 가까운 구독 격자를 고른다. 기기 위치가 이미
    # 격자로 들어오므로, 시군구 대표 좌표가 아니라 그 격자를 기준으로 잰다.
    grid = pick_serving_grid(grids, row.lv1, nx, ny)
    return build_region_lookup(
        row.admin_code, row.lv1, row.lv2, nx, ny, grid
    )


async def upsert_nowcast_snapshot(
    date_kst: date, hour_kst: int, nx: int, ny: int,
    temp_c: float, pty: int | None,
) -> None:
    """upsert_nowcast_snapshot — 실황 관측값을 시각 단위로 남긴다

    같은 시간대를 여러 번 조회해도 row 는 하나만 남고 마지막 값으로 덮인다.
    이 기록이 다음 날 "어제 같은 시각" 비교의 유일한 근거다.

    date_kst / hour_kst: 관측 발표 시각의 KST 일자와 시.
    nx / ny: 격자 좌표. 원시 위경도는 저장하지 않는다.
    temp_c: 관측 기온(섭씨). pty: 강수 형태 코드(없으면 None).
    """
    sql = text(
        """
        INSERT INTO hub_data.weather_nowcast_snapshots
          (date_kst, hour_kst, nx, ny, temp_c, pty)
        VALUES (:d, :h, :nx, :ny, :temp_c, :pty)
        ON CONFLICT (date_kst, hour_kst, nx, ny) DO UPDATE
          SET temp_c = EXCLUDED.temp_c,
              pty = EXCLUDED.pty,
              captured_at = now()
        """
    )
    async with get_hub_db().session() as s:
        await s.execute(
            sql,
            {
                "d": date_kst, "h": hour_kst, "nx": nx, "ny": ny,
                "temp_c": temp_c, "pty": pty,
            },
        )


# 어제 비교에 쓸 기록의 시각 차 상한(시간). 이보다 멀면 비교하지 않는다 —
# 하루 안의 기온 차가 어제와의 차이로 보이는 것을 막는다.
_SNAPSHOT_MAX_HOUR_GAP = 3


async def fetch_nowcast_snapshot(
    date_kst: date, hour_kst: int, nx: int, ny: int
) -> dict | None:
    """fetch_nowcast_snapshot — 특정 일자·시각의 실황 기록 조회

    같은 시각의 기록이 있으면 그대로, 없으면 같은 날 안에서 시각이 가장
    가까운 기록을 돌려준다. 사용자가 매시 정각에 앱을 열지는 않으므로,
    한두 시간 어긋난 기록이라도 비교 대상으로 쓰는 편이 낫다.

    다만 너무 멀리 떨어진 시각은 쓰지 않는다. 새벽 기록으로 한낮 기온을
    비교하면 하루 안의 기온 차가 어제와의 차이로 둔갑한다. 시각 차가 같을
    때는 이른 쪽을 골라 결과가 호출마다 흔들리지 않게 한다.

    반환: {"temp_c": float, "hour_kst": int} 또는 기록이 없으면 None.
    """
    sql = text(
        """
        SELECT temp_c, hour_kst
        FROM hub_data.weather_nowcast_snapshots
        WHERE date_kst = :d AND nx = :nx AND ny = :ny
          AND abs(hour_kst - :h) <= :max_gap
        ORDER BY abs(hour_kst - :h), hour_kst
        LIMIT 1
        """
    )
    async with get_hub_db().session() as s:
        row = (
            await s.execute(
                sql,
                {
                    "d": date_kst, "h": hour_kst, "nx": nx, "ny": ny,
                    "max_gap": _SNAPSHOT_MAX_HOUR_GAP,
                },
            )
        ).first()
    if row is None:
        return None
    return {"temp_c": float(row.temp_c), "hour_kst": int(row.hour_kst)}


async def housekeeping_expire() -> int:
    """housekeeping_expire — 만료된 예보 row 일괄 삭제

    세 forecast 테이블에서 expires_at < now() 인 row 를 모두 DELETE 한다.
    단일 트랜잭션 내에서 3개의 DELETE 를 순차 실행하고, 삭제 row 수를 합산.

    반환: 삭제된 총 row 수(int).
    호출처:
      - hub_scheduler.housekeeping_job (cron: 매시 :05)
      - routers.internal_router.run_now(which="housekeep")
    """
    sqls = [
        text("DELETE FROM hub_data.short_term_forecast "
             "WHERE expires_at < now()"),
        text("DELETE FROM hub_data.mid_land_forecast "
             "WHERE expires_at < now()"),
        text("DELETE FROM hub_data.mid_temp_forecast "
             "WHERE expires_at < now()"),
    ]
    # 실황 스냅샷은 만료 컬럼 대신 보관 일수로 걷어낸다 — 조회할 때마다 쌓여
    # 그대로 두면 격자 수 × 시각만큼 계속 늘어난다.
    cutoff = (
        datetime.now(KST).date()
        - timedelta(days=settings.WEATHER_SNAPSHOT_RETENTION_DAYS)
    )
    total = 0
    async with get_hub_db().session() as s:
        for q in sqls:
            r = await s.execute(q)
            total += r.rowcount or 0
        r = await s.execute(
            text(
                "DELETE FROM hub_data.weather_nowcast_snapshots "
                "WHERE date_kst < :cutoff"
            ),
            {"cutoff": cutoff},
        )
        total += r.rowcount or 0
    logger.info("housekeeping deleted=%d", total)
    return total
