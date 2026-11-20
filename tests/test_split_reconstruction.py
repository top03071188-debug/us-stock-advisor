"""test_split_reconstruction — 분할 이력 역산·대조·이상 탐지 (2.4.1, LA-9)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from engine.data.prices import (
    build_price_frame,
    reconstruct_raw,
    split_factor_series,
    validate_frame,
    verify_against_raw_source,
)
from engine.models import PriceKind


def test_split_factor_before_ex_date():
    idx = pd.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-06-03", "2024-06-04"])
    f = split_factor_series(idx, pd.Series([2.0], index=pd.to_datetime(["2024-06-03"])))
    assert list(f) == [2.0, 2.0, 1.0, 1.0]


def test_raw_reconstruction(panel_with_split):
    df = panel_with_split.frame
    ex = pd.Timestamp("2025-06-02")
    pre = df.loc[df.index < ex]
    post = df.loc[df.index >= ex]
    assert (pre["split_factor"] == 2.0).all()
    assert (post["split_factor"] == 1.0).all()
    np.testing.assert_allclose(pre["raw_close"], pre["sadj_close"] * 2.0)
    np.testing.assert_allclose(post["raw_close"], post["sadj_close"])
    # 역산 함수와 저장값이 같다
    np.testing.assert_allclose(
        reconstruct_raw(df["sadj_close"], pd.Series([2.0], index=pd.to_datetime(["2025-06-02"]))),
        df["raw_close"],
    )


def test_verify_against_direct_raw_source(panel_with_split):
    df = panel_with_split.frame
    direct = pd.DataFrame({"Open": df["raw_open"], "High": df["raw_high"],
                           "Low": df["raw_low"], "Close": df["raw_close"]})
    result = verify_against_raw_source(panel_with_split, direct)
    assert result["raw_verified"] is True
    assert result["mismatch_days"] == 0
    # 일부러 1% 틀리게 만들면 mismatch 로 잡힌다
    bad = direct.copy()
    bad["Close"] = bad["Close"] * 1.01
    result2 = verify_against_raw_source(panel_with_split, bad)
    assert result2["raw_verified"] is False
    assert result2["mismatch_days"] > 0


def test_adjustment_mismatch_detected():
    idx = pd.bdate_range("2024-01-02", periods=30)
    close = pd.Series(np.linspace(100, 110, 30), index=idx)
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Adj Close": close, "Volume": 1e6})
    splits = pd.Series([2.0], index=pd.to_datetime([idx[10]]))
    panel = build_price_frame("BAD", df, splits)
    # 일부러 원시가를 조정해 불일치를 만든다
    panel.frame.loc[panel.frame.index[0], "raw_close"] *= 1.05
    issues = validate_frame("BAD", panel.frame, splits)
    assert "ADJUSTMENT_MISMATCH" in issues


def test_split_anomaly_without_split_history():
    idx = pd.bdate_range("2024-01-02", periods=30)
    close = pd.Series(100.0, index=idx)
    close.iloc[15] = 50.0  # 하루에 -50% 인데 분할 이력이 없다
    df = pd.DataFrame({"Open": close, "High": close, "Low": close, "Close": close,
                       "Adj Close": close, "Volume": 1e6})
    panel = build_price_frame("ANOM", df, None)
    assert "SPLIT_ANOMALY" in panel.issues
    # 분할 이력이 있으면 이상으로 잡지 않는다
    with_split = build_price_frame("ANOM2", df, pd.Series([2.0], index=pd.to_datetime([idx[15]])))
    assert "SPLIT_ANOMALY" not in with_split.issues


def test_price_kind_columns_are_distinct(panel_with_split):
    frame = panel_with_split.frame
    for kind, cols in (
        (PriceKind.split_adj_ohlc, ["sadj_open", "sadj_high", "sadj_low", "sadj_close"]),
        (PriceKind.raw_as_traded, ["raw_open", "raw_high", "raw_low", "raw_close"]),
    ):
        series = panel_with_split.series(kind)
        assert list(series.ohlc().columns) == cols
    # 두 기준은 분할 전 구간에서 2배 차이, 이후에는 같다
    ex = pd.Timestamp("2025-06-02")
    assert (frame.loc[frame.index < ex, "raw_close"] > frame.loc[frame.index < ex, "sadj_close"]).all()
    np.testing.assert_allclose(frame.loc[frame.index >= ex, "raw_close"], frame.loc[frame.index >= ex, "sadj_close"])


def test_available_at_is_close_time(panel_with_split):
    """봉의 available_at = NYSE 16:00 ET → UTC (EDT 20:00Z / EST 21:00Z)."""
    frame = panel_with_split.frame
    ts = pd.to_datetime(frame["available_at"], utc=True)
    hours = set(ts.dt.hour.unique())
    assert hours <= {20, 21}
    assert ts.is_monotonic_increasing
