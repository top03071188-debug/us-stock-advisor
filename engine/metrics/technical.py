"""engine.metrics.technical — 가격 지표. **입력은 반드시 `split_adj_ohlc`.**

기획서 2.4.1 / 3.8 / v1.3.1:
- ATR(14)은 조정 기준 OHLC 로 계산한다.
- 손절·사이징은 `atr_pct = atr14_sadj / close_sadj` 라는 **비율**로 만들고, 그 비율을 실제 체결가에 적용한다.
  (달러 ATR 을 당시 가격으로 직접 나누면 2:1 분할 전 종목에서 손절 폭이 절반으로 틀어진다.)
- 지표 함수에 `raw_as_traded` 를 넘기면 `PriceKindError`(TypeError) 가 난다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from engine.models import PriceKind, PriceKindError, PriceSeries


def _require_sadj(series: PriceSeries) -> None:
    if series.kind is not PriceKind.split_adj_ohlc:
        raise PriceKindError(
            f"지표는 split_adj_ohlc 만 받는다(받은 값: {series.kind.value}). "
            "체결·손절은 raw_as_traded + atr_pct 비율을 쓴다."
        )


def true_range(series: PriceSeries) -> pd.Series:
    _require_sadj(series)
    o = series.ohlc()
    high, low, close = o["sadj_high"], o["sadj_low"], o["sadj_close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr


def atr14(series: PriceSeries, window: int = 14) -> pd.Series:
    """Wilder ATR(14) — 달러 단위(조정 기준)."""
    tr = true_range(series)
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()


def atr_pct(series: PriceSeries, window: int = 14) -> pd.Series:
    """`atr14_sadj / close_sadj` — 무차원 변동성 비율 [고정]."""
    _require_sadj(series)
    return atr14(series, window) / series.close()


def stop_distance(series: PriceSeries, atr_mult: float = 3.0, cap: float = 0.20, window: int = 14) -> pd.Series:
    """`min(3 × atr_pct, 20%)` — 사이징·청산 공통 손절 거리."""
    return (atr_mult * atr_pct(series, window)).clip(upper=cap)


def stop_price(fill_price: float, distance: float) -> float:
    """실제 체결가(raw_as_traded)에 비율을 적용한 손절가."""
    return float(fill_price) * (1.0 - float(distance))


def dma(series: PriceSeries, window: int) -> pd.Series:
    _require_sadj(series)
    return series.close().rolling(window, min_periods=window).mean()


def rsi14(series: PriceSeries, window: int = 14) -> pd.Series:
    _require_sadj(series)
    close = series.close()
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100.0)


def bollinger_pct_b(series: PriceSeries, window: int = 20, k: float = 2.0) -> pd.Series:
    _require_sadj(series)
    close = series.close()
    mid = close.rolling(window, min_periods=window).mean()
    sd = close.rolling(window, min_periods=window).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    width = (upper - lower).replace(0.0, np.nan)
    return ((close - lower) / width).clip(lower=-1.0)


def relative_strength(stock: PriceSeries, benchmark: PriceSeries, window: int = 126) -> float:
    """6개월 상대강도(총수익 기준). 두 시리즈 모두 total_return_adj 여야 한다."""
    if stock.kind is not PriceKind.total_return_adj or benchmark.kind is not PriceKind.total_return_adj:
        raise PriceKindError("상대강도는 total_return_adj 두 시리즈로만 계산한다")
    s = stock.close().dropna()
    b = benchmark.close().dropna()
    if len(s) <= window or len(b) <= window:
        return float("nan")
    return float((s.iloc[-1] / s.iloc[-window - 1]) - (b.iloc[-1] / b.iloc[-window - 1]))


def dip_conditions(series: PriceSeries, day_index: int = -1) -> dict[str, bool]:
    """3.4.2 눌림 조건 4가지 중 지표로 판정 가능한 3개(+신저가 반등은 가격 시퀀스로)."""
    _require_sadj(series)
    close = series.close()
    rsi = rsi14(series)
    dma50 = dma(series, 50)
    pctb = bollinger_pct_b(series)
    i = day_index
    dev = close.iloc[i] / dma50.iloc[i] - 1 if pd.notna(dma50.iloc[i]) else np.nan
    recent_low = close.iloc[max(0, i - 19): i + 1].min()
    rebound = close.iloc[i] >= recent_low * 1.02 and close.iloc[i - 3:i].min() <= recent_low * 1.001
    return {
        "rsi_le_35": bool(pd.notna(rsi.iloc[i]) and rsi.iloc[i] <= 35),
        "below_dma50_8pct": bool(pd.notna(dev) and dev <= -0.08),
        "pctb_le_0_2": bool(pd.notna(pctb.iloc[i]) and pctb.iloc[i] <= 0.2),
        "rebound_after_low": bool(rebound),
    }


def snapshot_indicators(panel, day=None) -> dict:
    """한 시점의 지표 묶음(화면·게이트·토론 context 공통)."""
    sadj = panel.series(PriceKind.split_adj_ohlc)
    frame = sadj.frame
    idx = frame.index if day is None else frame.index[frame.index <= day]
    if len(idx) == 0:
        return {}
    row = frame.loc[idx[-1]]
    a = atr_pct(sadj).loc[idx[-1]]
    d50 = dma(sadj, 50).loc[idx[-1]]
    d200 = dma(sadj, 200).loc[idx[-1]]
    r = rsi14(sadj).loc[idx[-1]]
    pb = bollinger_pct_b(sadj).loc[idx[-1]]
    return {
        "date": idx[-1].date().isoformat(),
        "sadj_close": float(row["sadj_close"]),
        "raw_close": float(row["raw_close"]),
        "atr_pct": float(a) if pd.notna(a) else None,
        "dma50": float(d50) if pd.notna(d50) else None,
        "dma200": float(d200) if pd.notna(d200) else None,
        "rsi14": float(r) if pd.notna(r) else None,
        "pct_b": float(pb) if pd.notna(pb) else None,
    }
