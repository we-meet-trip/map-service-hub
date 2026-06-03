"""hub_scheduler 의 발표 시각 해석 로직 단위 테스트.

검증 대상:
  - resolve_short_term_base — KMA 단기예보 8회/일 슬롯 중 "지금 시점에서
    가장 최근 가용한" 발표 시각을 찾는다. 발표 직후 10분 마진을 둔다.
  - resolve_mid_tm_fc — KMA 중기예보 2회/일(06,18) 슬롯에 같은 규칙 적용.

각 케이스는 경계 시각(슬롯 직전 9분 / 10분 / 11분)을 포함해 마진 처리가
정확한지 확인한다.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.scheduler.hub_scheduler import (
    resolve_mid_tm_fc,
    resolve_short_term_base,
)
from app.utils.kma_grid import KST


@pytest.mark.parametrize(
    "now_kst,expected",
    [
        # 0:05 (전날 23:00 발표 + 마진 10분 이내) → 전날 23:00 slot
        (datetime(2026, 5, 22, 0, 5, tzinfo=KST), ("20260521", "2300")),
        # 2:09 — 02:00 발표 후 10분 마진 미달 → 직전 23:00 slot
        (datetime(2026, 5, 22, 2, 9, tzinfo=KST), ("20260521", "2300")),
        # 2:10 — 정확히 마진 도달 → 당일 02:00 slot
        (datetime(2026, 5, 22, 2, 10, tzinfo=KST), ("20260522", "0200")),
        # 2:15 — 마진 초과 → 02:00 slot
        (datetime(2026, 5, 22, 2, 15, tzinfo=KST), ("20260522", "0200")),
        # 5:11 — 마진 통과 → 05:00 slot
        (datetime(2026, 5, 22, 5, 11, tzinfo=KST), ("20260522", "0500")),
        # 14:30 — 14:00 slot 통과
        (datetime(2026, 5, 22, 14, 30, tzinfo=KST), ("20260522", "1400")),
        # 23:15 — 23:00 slot 통과
        (datetime(2026, 5, 22, 23, 15, tzinfo=KST), ("20260522", "2300")),
        # 다음날 0:01 — 전날 23:00 slot 로 fallback
        (datetime(2026, 5, 23, 0, 1, tzinfo=KST), ("20260522", "2300")),
    ],
)
def test_resolve_short_term_base(now_kst, expected):
    """주어진 KST 시각에서 단기예보의 (base_date, base_time) 가 예상과 일치하는지 검증."""
    assert resolve_short_term_base(now_kst) == expected


@pytest.mark.parametrize(
    "now_kst,expected",
    [
        # 04:00 — 새벽: 전날 18:00 slot
        (datetime(2026, 5, 22, 4, 0, tzinfo=KST), "202605211800"),
        # 06:09 — 06:00 발표 마진 10분 미달 → 전날 18:00 slot
        (datetime(2026, 5, 22, 6, 9, tzinfo=KST), "202605211800"),
        # 06:11 — 마진 통과 → 당일 06:00 slot
        (datetime(2026, 5, 22, 6, 11, tzinfo=KST), "202605220600"),
        # 12:00 — 06:00 slot 유지
        (datetime(2026, 5, 22, 12, 0, tzinfo=KST), "202605220600"),
        # 18:11 — 18:00 발표 마진 통과 → 당일 18:00 slot
        (datetime(2026, 5, 22, 18, 11, tzinfo=KST), "202605221800"),
        # 다음날 00:05 — 전날 18:00 slot
        (datetime(2026, 5, 23, 0, 5, tzinfo=KST), "202605221800"),
    ],
)
def test_resolve_mid_tm_fc(now_kst, expected):
    """주어진 KST 시각에서 중기예보 tm_fc 12자리 문자열이 예상과 일치하는지 검증."""
    assert resolve_mid_tm_fc(now_kst) == expected
