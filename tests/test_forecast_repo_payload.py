from __future__ import annotations

from app.db.forecast_repo import _safe_int


def test_safe_int_handles_none_and_empty():
    assert _safe_int(None) is None
    assert _safe_int("") is None
    assert _safe_int("abc") is None


def test_safe_int_parses_int_and_str_int():
    assert _safe_int(60) == 60
    assert _safe_int("60") == 60
    assert _safe_int(0) == 0


def test_mid_land_payload_row_expansion_logic():
    payload = {
        "regId": "11B00000",
        **{
            f"wf{d}{sfx}": "맑음"
            for d in (4, 5, 6, 7)
            for sfx in ("Am", "Pm")
        },
        **{f"wf{d}": "맑음" for d in (8, 9, 10)},
        **{
            f"rnSt{d}{sfx}": 30
            for d in (4, 5, 6, 7)
            for sfx in ("Am", "Pm")
        },
        **{f"rnSt{d}": 10 for d in (8, 9, 10)},
    }
    rows = []
    for day in (4, 5, 6, 7):
        for ampm, sfx in (("AM", "Am"), ("PM", "Pm")):
            rows.append((
                day,
                ampm,
                payload[f"wf{day}{sfx}"],
                payload[f"rnSt{day}{sfx}"],
            ))
    for day in (8, 9, 10):
        rows.append((day, "NA", payload[f"wf{day}"], payload[f"rnSt{day}"]))
    assert len(rows) == 11
    assert sum(1 for r in rows if r[1] in ("AM", "PM")) == 8
    assert sum(1 for r in rows if r[1] == "NA") == 3


def test_mid_temp_payload_row_expansion_logic():
    payload = {"regId": "11B10101"}
    for d in range(4, 11):
        for sfx in ("", "Low", "High"):
            payload[f"taMin{d}{sfx}"] = d
            payload[f"taMax{d}{sfx}"] = d + 5
    rows = list(range(4, 11))
    assert len(rows) == 7
    for d in rows:
        assert payload[f"taMin{d}"] == d
        assert payload[f"taMax{d}High"] == d + 5
