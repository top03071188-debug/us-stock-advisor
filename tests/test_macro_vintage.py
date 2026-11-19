"""test_macro_vintage — D = realtime_start(공개 빈티지 날짜), 발표 시각 미확인은 D 종료 후 첫 결정 (v1.3.1)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from engine.data.macro import (
    as_of_macro,
    build_limits,
    classify_regime,
    parse_observations,
    resolve_available_at,
    series_limits,
)
from engine.utils.dates import first_decision_after


def test_d_is_publication_vintage_not_observation_date(macro_payload):
    df = parse_observations(macro_payload, "CPIAUCSL")
    row = df[(df["date"] == date(2026, 10, 1)) & (df["realtime_start"] == date(2026, 11, 12))].iloc[0]
    # 관측월은 10-01 이지만 D 는 11-12 → available_at 도 11-12 이후
    assert row["date"] == date(2026, 10, 1)
    assert row["realtime_start"] == date(2026, 11, 12)
    assert row["available_at_source"] == "date_plus_one"
    assert row["available_at"] > pd.Timestamp("2026-11-12T23:59:59", tz="America/New_York")
    # EST 기준 11-13 04:59:59Z 이후 첫 의사결정 = 11-13 12:50Z
    assert row["available_at"] == pd.Timestamp("2026-11-13T12:50:00Z").to_pydatetime()


def test_observation_date_would_leak(macro_payload):
    """관측월 날짜로 잘랐다면 11-12 장전 결정에 값이 들어갔을 것이다(잘못된 방식)."""
    df = parse_observations(macro_payload, "CPIAUCSL")
    early = pd.Timestamp("2026-11-12T12:50:00Z").to_pydatetime()
    visible = as_of_macro(df, early)
    october = visible[visible["date"] == date(2026, 10, 1)]
    assert october.empty          # 11-12 장전에는 아직 못 본다
    later = pd.Timestamp("2026-11-13T12:50:00Z").to_pydatetime()
    assert not as_of_macro(df, later)[lambda d: d["date"] == date(2026, 10, 1)].empty


def test_revision_is_not_visible_before_its_vintage(macro_payload):
    df = parse_observations(macro_payload, "CPIAUCSL")
    at_nov = as_of_macro(df, pd.Timestamp("2026-11-13T12:50:00Z").to_pydatetime())
    val = at_nov[at_nov["date"] == date(2026, 10, 1)]["value"].iloc[0]
    assert val == pytest.approx(320.100)
    at_dec = as_of_macro(df, pd.Timestamp("2026-12-11T12:50:00Z").to_pydatetime())
    val2 = at_dec[at_dec["date"] == date(2026, 10, 1)]["value"].iloc[0]
    assert val2 == pytest.approx(320.450)


def test_missing_value_is_nan_not_zero(macro_payload):
    df = parse_observations(macro_payload, "CPIAUCSL")
    aug = df[df["date"] == date(2026, 8, 1)]
    assert pd.isna(aug["value"].iloc[0])


def test_release_at_and_first_ingested_take_priority():
    ts, src = resolve_available_at("2026-11-12", release_at="2026-11-12T08:30:00-05:00")
    assert src == "release_at"
    assert ts == pd.Timestamp("2026-11-12T13:30:00Z").to_pydatetime()
    ts2, src2 = resolve_available_at("2026-11-12", first_ingested_at="2026-11-12T15:00:00Z")
    assert src2 == "first_ingested_at"
    assert ts2 == pd.Timestamp("2026-11-12T15:00:00Z").to_pydatetime()


def test_first_decision_after_et_day_end():
    # 11-12 23:59:59 ET(EST) = 11-13 04:59:59Z → 다음 결정 11-13 12:50Z
    d = first_decision_after(pd.Timestamp("2026-11-13T04:59:59Z").to_pydatetime())
    assert d == pd.Timestamp("2026-11-13T12:50:00Z").to_pydatetime()
    # EDT 구간(여름): 08-10 23:59:59 ET = 08-11 03:59:59Z → 08-11 12:50Z
    d2 = first_decision_after(pd.Timestamp("2026-08-11T03:59:59Z").to_pydatetime())
    assert d2 == pd.Timestamp("2026-08-11T12:50:00Z").to_pydatetime()


def test_limits_records_real_range_and_hy_note(macro_payload):
    cpi = parse_observations(macro_payload, "CPIAUCSL")
    hy = parse_observations({"observations": [
        {"date": "2023-11-20", "realtime_start": "2023-11-21", "value": "4.10"},
        {"date": "2026-11-18", "realtime_start": "2026-11-19", "value": "3.05"},
    ]}, "BAMLH0A0HYM2")
    limits = build_limits({"CPIAUCSL": cpi, "BAMLH0A0HYM2": hy})
    assert limits["series"]["BAMLH0A0HYM2"]["start"] == "2023-11-20"
    assert limits["series"]["BAMLH0A0HYM2"]["end"] == "2026-11-18"
    assert limits["hy_oas_available_from"] == "2023-11-20"
    assert any("BAMLH0A0HYM2" in n for n in limits["notes"])


def test_series_limits_empty():
    empty = pd.DataFrame(columns=["date", "realtime_start", "value", "available_at"])
    assert series_limits(empty)["rows"] == 0


def test_regime_with_four_signals_when_hy_missing():
    r = classify_regime({"real_rate_trend": "up", "t10y2y": -0.5, "vix": 30, "dxy_3m": 0.07})
    assert "BAMLH0A0HYM2" in r["inputs_missing"]
    assert r["regime"] == "risk_off"
    assert r["multiplier"] == pytest.approx(0.4)
    assert r["macro_extreme"] is False


def test_regime_neutral_when_split():
    r = classify_regime({"real_rate_trend": "flat", "t10y2y": 0.1, "hy_oas_bp": 400, "vix": 20, "dxy_3m": -0.01})
    assert r["regime"] == "neutral"
    assert r["inputs_missing"] == []


def test_macro_extreme_flags():
    r = classify_regime({"real_rate_trend": "flat", "t10y2y": 0.1, "hy_oas_bp": 750, "vix": 20, "dxy_3m": 0.0})
    assert r["macro_extreme"] is True
