"""예보를 내줄 격자를 고르는 규칙 테스트.

이 규칙이 틀리면 사용자는 자기 지역이 아닌 곳의 날씨를 받는다. 그런데
증상이 "값이 이상하다"가 아니라 "그럴듯한 다른 지역 값"이라 눈으로는
잡히지 않는다. 그래서 실제 시드 좌표로 결정을 못 박아 둔다.

좌표는 전부 `migrations/data/region_grid_seed.sql` 과
`migrations/revisions/003_seed_subscribed_grids.py` 의 실측값이다.
"""
from __future__ import annotations

from app.db.forecast_repo import (
    SubscribedGrid,
    build_region_lookup,
    pick_serving_grid,
)


def _grid(
    grid_id: int, label: str, nx: int, ny: int, lv1: str,
    land: str = "LAND0000", temp: str = "TEMP0000",
) -> SubscribedGrid:
    return SubscribedGrid(
        grid_id=grid_id, label=label, nx=nx, ny=ny,
        mid_land_reg_id=land, mid_temp_reg_id=temp, lv1=lv1,
    )


# 시드에 있는 그대로의 구독 격자 일부.
_SEOUL = _grid(1, "서울특별시", 60, 127, "서울특별시", "11B00000", "11B10101")
_GYEONGGI = _grid(3, "경기도", 60, 120, "경기도", "11B00000", "11B20601")
_YEONGSEO = _grid(
    4, "강원특별자치도(영서)", 73, 134, "강원특별자치도",
    "11D10000", "11D10301",
)
_YEONGDONG = _grid(
    5, "강원특별자치도(영동)", 92, 131, "강원특별자치도",
    "11D20000", "11D20501",
)
_ALL = [_SEOUL, _GYEONGGI, _YEONGSEO, _YEONGDONG]


def test_sigungu_without_own_grid_gets_its_province_grid() -> None:
    """구독하지 않은 시군구는 같은 광역의 격자로 대신 조회한다.

    강남구(61,126)는 구독 격자가 아니라 그 좌표로 조회하면 늘 비어 있었다.
    """
    picked = pick_serving_grid(_ALL, "서울특별시", 61, 126)
    assert picked is _SEOUL


def test_yeongdong_sigungu_no_longer_falls_back_to_yeongseo() -> None:
    """영동 시군구가 영서 격자로 가지 않는다.

    이전 규칙은 정렬 조건이 항상 무효라 grid_id 가 작은 영서만 뽑혔다.
    속초·강릉·동해·삼척은 태백산맥 반대편이라 전혀 다른 날씨다.
    """
    for label, nx, ny in (
        ("속초시", 87, 141),
        ("강릉시", 92, 131),
        ("동해시", 97, 127),
        ("삼척시", 98, 125),
    ):
        picked = pick_serving_grid(_ALL, "강원특별자치도", nx, ny)
        assert picked is _YEONGDONG, label


def test_inland_gangwon_stays_on_yeongseo() -> None:
    """영서 시군구는 그대로 영서다 — 영동 도입이 회귀를 만들면 안 된다."""
    for label, nx, ny in (
        ("춘천시", 73, 134),
        ("원주시", 76, 122),
        ("횡성군", 77, 125),
    ):
        picked = pick_serving_grid(_ALL, "강원특별자치도", nx, ny)
        assert picked is _YEONGSEO, label


def test_province_is_a_hard_filter_not_a_preference() -> None:
    """도를 넘어서는 고르지 않는다.

    가평군(69,133)은 경기 대표(60,120, 거리제곱 250)보다 강원 영서
    (73,134, 17)가 14배 가깝다. 그래도 가평의 예보구역은 경기다 —
    거리로 고르면 그럴듯하지만 틀린 예보가 나간다.
    """
    picked = pick_serving_grid(_ALL, "경기도", 69, 133)
    assert picked is _GYEONGGI

    # 전제 확인: 거리만 보면 실제로 영서가 훨씬 가깝다.
    def d2(g: SubscribedGrid) -> int:
        return (g.nx - 69) ** 2 + (g.ny - 133) ** 2

    assert d2(_YEONGSEO) == 17
    assert d2(_GYEONGGI) == 250


def test_no_active_grid_in_province_returns_none() -> None:
    """도내에 쓸 격자가 없으면 옆 도를 빌려오지 않고 없다고 한다."""
    assert pick_serving_grid(_ALL, "제주특별자치도", 52, 38) is None
    assert pick_serving_grid([], "서울특별시", 60, 127) is None


def test_equal_distance_is_broken_deterministically() -> None:
    """같은 거리면 늘 같은 쪽을 고른다.

    정하지 않으면 목록 순서에 따라 답이 달라져, 같은 요청이 호출마다
    다른 지역 예보를 낸다.
    """
    a = _grid(9, "A", 60, 127, "테스트도")
    b = _grid(2, "B", 60, 129, "테스트도")
    mid_point = (60, 128)
    assert pick_serving_grid([a, b], "테스트도", *mid_point) is b
    assert pick_serving_grid([b, a], "테스트도", *mid_point) is b


def test_lookup_carries_the_serving_grid_coordinates() -> None:
    """조회 결과에는 요청 좌표가 아니라 예보가 쌓인 격자가 실린다."""
    lookup = build_region_lookup(
        "1168000000", "서울특별시", "강남구", 61, 126, _SEOUL,
    )
    assert (lookup.nx, lookup.ny) == (60, 127)
    assert lookup.lv2 == "강남구"
    assert lookup.mid_land_reg_id == "11B00000"
    assert lookup.mid_temp_reg_id == "11B10101"


def test_lookup_without_grid_keeps_request_and_clears_mid_codes() -> None:
    """격자를 못 고르면 요청 좌표를 두고 중기 코드는 비운다.

    중기 코드가 비면 하류가 중기 조회를 건너뛰고 그 날짜를 결측으로 둔다.
    엉뚱한 지역 코드로 조회하는 것보다 낫다.
    """
    lookup = build_region_lookup(
        "5000000000", "제주특별자치도", "서귀포시", 52, 33, None,
    )
    assert (lookup.nx, lookup.ny) == (52, 33)
    assert lookup.mid_land_reg_id is None
    assert lookup.mid_temp_reg_id is None
