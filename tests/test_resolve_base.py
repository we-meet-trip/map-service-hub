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
        (datetime(2026, 5, 22, 0, 5, tzinfo=KST), ("20260521", "2300")),
        (datetime(2026, 5, 22, 2, 9, tzinfo=KST), ("20260521", "2300")),
        (datetime(2026, 5, 22, 2, 10, tzinfo=KST), ("20260522", "0200")),
        (datetime(2026, 5, 22, 2, 15, tzinfo=KST), ("20260522", "0200")),
        (datetime(2026, 5, 22, 5, 11, tzinfo=KST), ("20260522", "0500")),
        (datetime(2026, 5, 22, 14, 30, tzinfo=KST), ("20260522", "1400")),
        (datetime(2026, 5, 22, 23, 15, tzinfo=KST), ("20260522", "2300")),
        (datetime(2026, 5, 23, 0, 1, tzinfo=KST), ("20260522", "2300")),
    ],
)
def test_resolve_short_term_base(now_kst, expected):
    assert resolve_short_term_base(now_kst) == expected


@pytest.mark.parametrize(
    "now_kst,expected",
    [
        (datetime(2026, 5, 22, 4, 0, tzinfo=KST), "202605211800"),
        (datetime(2026, 5, 22, 6, 9, tzinfo=KST), "202605211800"),
        (datetime(2026, 5, 22, 6, 11, tzinfo=KST), "202605220600"),
        (datetime(2026, 5, 22, 12, 0, tzinfo=KST), "202605220600"),
        (datetime(2026, 5, 22, 18, 11, tzinfo=KST), "202605221800"),
        (datetime(2026, 5, 23, 0, 5, tzinfo=KST), "202605221800"),
    ],
)
def test_resolve_mid_tm_fc(now_kst, expected):
    assert resolve_mid_tm_fc(now_kst) == expected
