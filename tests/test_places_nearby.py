"""한 지점 주변에서 분류로 장소를 찾는 경로.

일정의 방문지 하나를 놓고 "이 근처에 묵을 곳·먹을 곳·카페가 무엇이 있나" 를
묻는 자리다. 여기서 지키는 것은 셋이다.

  - 바깥에는 우리 말(stay/food/cafe)로 받고, 발급처 분류 코드는 안에서만 쓴다
  - 모르는 분류는 거절한다 — 조용히 빈 목록을 주면 "근처에 없다" 로 잘못 읽힌다
  - 같은 자리를 여러 번 물으면 캐시가 받는다. 좌표를 너무 잘게 나누면 사실상
    매번 다시 물어보게 되므로, 몇 미터 어긋난 좌표는 같은 자리로 본다
"""
from __future__ import annotations

import pytest

from app.place_stubs import kakao_category_stub
from app.routers.hub_routers import (
    _NEARBY_CATEGORIES,
    _nearby_cache_key,
)


class TestCategoryNaming:
    def test_바깥_이름이_발급처_코드로_바뀐다(self):
        assert _NEARBY_CATEGORIES["stay"] == "AD5"
        assert _NEARBY_CATEGORIES["food"] == "FD6"
        assert _NEARBY_CATEGORIES["cafe"] == "CE7"

    def test_모르는_이름은_매핑에_없다(self):
        # 라우트가 이것을 보고 400 을 낸다. 빈 목록으로 답하면 안 된다.
        assert _NEARBY_CATEGORIES.get("hotel") is None
        assert _NEARBY_CATEGORIES.get("AD5") is None


class TestCacheKey:
    def test_몇_미터_어긋난_좌표는_같은_자리로_본다(self):
        # 소수 넷째 자리 = 약 11m. 방문지 주변을 보는 용도라 그보다 잘게
        # 나누면 같은 자리를 매번 다시 물어보게 된다.
        a = _nearby_cache_key(37.56650, 126.97800, "AD5", 1000, 10)
        b = _nearby_cache_key(37.566501, 126.978004, "AD5", 1000, 10)
        assert a == b

    def test_분류나_반경이_다르면_다른_자리다(self):
        base = _nearby_cache_key(37.5665, 126.9780, "AD5", 1000, 10)
        assert base != _nearby_cache_key(37.5665, 126.9780, "FD6", 1000, 10)
        assert base != _nearby_cache_key(37.5665, 126.9780, "AD5", 2000, 10)
        assert base != _nearby_cache_key(37.5665, 126.9780, "AD5", 1000, 5)

    def test_충분히_떨어진_좌표는_다른_자리다(self):
        a = _nearby_cache_key(37.5665, 126.9780, "AD5", 1000, 10)
        b = _nearby_cache_key(37.5700, 126.9780, "AD5", 1000, 10)
        assert a != b


class TestStub:
    def test_스텁이_요청한_분류를_달고_온다(self):
        # 키를 안 넣고 도는 환경에서도 화면이 무엇을 받는지 확인할 수 있어야 한다.
        items = kakao_category_stub("AD5")
        assert items
        assert all(p["category_group_code"] == "AD5" for p in items)

    def test_스텁이_공용_장소_형태를_지킨다(self):
        from app.schemas.hub_schemas import PlaceItem

        for p in kakao_category_stub("CE7"):
            PlaceItem(**p)
