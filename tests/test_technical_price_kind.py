"""test_technical_price_kind — 지표는 split_adj_ohlc 만, ATR 비율은 같은 기준에서 (v1.3.1 핵심)."""

from __future__ import annotations

import pandas as pd
import pytest

from engine.backtest.execution import build_entry_band, opening_fill
from engine.metrics.technical import (
    atr14,
    atr_pct,
    bollinger_pct_b,
    dma,
    rsi14,
    stop_distance,
    stop_price,
)
from engine.metrics.technical import true_range
from engine.models import PriceKind, PriceKindError


def test_raw_as_traded_is_rejected(panel_with_split):
    raw = panel_with_split.series(PriceKind.raw_as_traded)
    for fn in (atr14, atr_pct, lambda s: dma(s, 50), rsi14, bollinger_pct_b, true_range):
        with pytest.raises(PriceKindError):
            fn(raw)
        with pytest.raises(TypeError):  # PriceKindError 는 TypeError 다
            fn(raw)


def test_total_return_adj_has_no_ohlc(panel_no_split):
    tr = panel_no_split.series(PriceKind.total_return_adj)
    with pytest.raises(PriceKindError):
        atr14(tr)


def test_atr_pct_is_ratio_not_dollar_division(flat_panel):
    """리뷰 예시: 2:1 분할 전 당시가 $100, 조정 종가 $50, 조정 ATR $1 → 3%가 아니라 6%."""
    sadj = flat_panel.series(PriceKind.split_adj_ohlc)
    a = atr_pct(sadj).iloc[-1]
    assert a == pytest.approx(1.0 / 50.0, rel=1e-6)          # 2%
    dist = stop_distance(sadj).iloc[-1]
    assert dist == pytest.approx(0.06, rel=1e-6)             # 3 × 2% = 6%
    # 실제 체결가($100, 분할 전 당시 거래가)에 비율을 적용
    assert stop_price(100.0, dist) == pytest.approx(94.0)
    # 잘못된 계산(조정 ATR ÷ 당시 가격)이었다면 3% → 손절 97.0 이 되어 3%p 틀어진다
    wrong = 3 * (1.0 / 100.0)
    assert stop_price(100.0, wrong) == pytest.approx(97.0)
    assert abs(stop_price(100.0, dist) - stop_price(100.0, wrong)) == pytest.approx(3.0)


def test_atr_pct_continuous_across_split(panel_with_split):
    """분할 전후 atr_pct 는 같은 수준이어야 한다(비율이라 주식 수 기준이 없다)."""
    sadj = panel_with_split.series(PriceKind.split_adj_ohlc)
    a = atr_pct(sadj).dropna()
    split_day = pd.Timestamp("2025-06-02")
    before = a.loc[a.index < split_day].iloc[-5:]
    after = a.loc[a.index >= split_day].iloc[10:]
    assert len(before) and len(after)
    assert before.median() == pytest.approx(after.median(), rel=0.55)


def test_stop_distance_cap_at_20pct():
    """ATR% 가 6.67% 를 넘으면 손절 거리는 20% 상한에 걸린다."""
    from engine.data.prices import build_price_frame

    idx = pd.bdate_range("2024-01-02", periods=40)
    close = pd.Series(100.0, index=idx)
    df = pd.DataFrame({
        "Open": close, "High": close + 10, "Low": close - 10, "Close": close,
        "Adj Close": close, "Volume": 1e6,
    })
    panel = build_price_frame("VOL", df, None)
    dist = stop_distance(panel.series(PriceKind.split_adj_ohlc)).iloc[-1]
    assert dist == pytest.approx(0.20)


def test_entry_band_and_open_fill():
    """장 전 계산 범위 안에서만 체결된다(시가를 보고 범위를 고치지 않는다)."""
    band = build_entry_band(iv_median_sadj=120.0, required_margin=0.20, dma200_sadj=90.0,
                            signal_price_sadj=95.0, stop_distance=0.06, dma200_mult=0.97)
    assert band.limit_max_sadj == pytest.approx(96.0)
    assert band.stop_min_sadj == pytest.approx(max(87.3, 89.3))
    filled = opening_fill(band, open_price=95.0)
    assert filled["execution_event"] == "buy_filled"
    assert filled["fill_price"] == pytest.approx(95.0 * 1.001, rel=1e-6)
    rejected = opening_fill(band, open_price=97.0)          # 상한 초과 → 미체결
    assert rejected["execution_event"] == "no_action"
    assert rejected["rejected_reason"] == "outside_order_range"
    gapped = opening_fill(band, open_price=85.0)            # 하한 미달 → 미체결
    assert gapped["execution_event"] == "no_action"
