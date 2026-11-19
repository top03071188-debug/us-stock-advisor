"""공통 픽스처 — **모두 오프라인**(네트워크 없이 100% 재현)."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from engine.data.prices import PricePanel, build_price_frame
from engine.metrics.fundamentals import AnnualFundamentals


def make_history(days: int = 420, start: str = "2024-01-02", price: float = 100.0,
                 split_date: str | None = None, seed: int = 7) -> tuple[pd.DataFrame, pd.Series | None]:
    """분할 조정된 Yahoo 형태 OHLC. `split_date` 가 있으면 그 전 구간 raw 는 2배가 된다."""
    idx = pd.bdate_range(start=start, periods=days)
    rng = np.random.default_rng(seed)
    drift = np.linspace(0, 0.10, days)
    noise = rng.normal(0, 0.004, days)
    close = pd.Series(price * (1 + drift + noise), index=idx)
    high = close * 1.005
    low = close * 0.995
    open_ = close.shift(1).fillna(close.iloc[0])
    df = pd.DataFrame({
        "Open": open_, "High": high, "Low": low, "Close": close,
        "Adj Close": close * 0.99, "Volume": rng.integers(1_000_000, 5_000_000, days).astype(float),
    }, index=idx)
    splits = None
    if split_date:
        splits = pd.Series([2.0], index=pd.to_datetime([split_date]))
    return df, splits


@pytest.fixture
def panel_with_split() -> PricePanel:
    df, splits = make_history(split_date="2025-06-02")
    return build_price_frame("TEST", df, splits)


@pytest.fixture
def panel_no_split() -> PricePanel:
    df, _ = make_history(seed=11)
    return build_price_frame("TEST2", df, None)


@pytest.fixture
def flat_panel() -> PricePanel:
    """ATR = $1, 조정 종가 = $50 인 패널 — 리뷰 예시(2:1 분할 전 $100) 재현용."""
    idx = pd.bdate_range(start="2024-01-02", periods=40)
    close = pd.Series(50.0, index=idx)
    df = pd.DataFrame({
        "Open": close, "High": close + 0.5, "Low": close - 0.5, "Close": close,
        "Adj Close": close, "Volume": 1_000_000.0,
    })
    splits = pd.Series([2.0], index=pd.to_datetime(["2024-01-20"]))
    return build_price_frame("SPLITEX", df, splits)


@pytest.fixture
def fundamentals() -> AnnualFundamentals:
    """기획서 3.2.3 예시(XYZ)와 같은 EBIT·D&A·CAPEX·NWC."""
    return AnnualFundamentals(
        ebit=[5000, 4700, 4500, 4300, 4200],
        revenue=[25000, 23500, 22500, 21500, 20500],
        net_income=[3100, 2900, 2800, 2700, 2600],
        ocf=[3800, 3600, 3400, 3300, 3200],
        capex=[700, 680, 650, 640, 620],
        depreciation=[600, 580, 560, 540, 520],
        total_assets=[20000, 19000, 18500, 18000, 17500],
        non_interest_liabilities=[4000, 3800, 3700, 3600, 3500],
        excess_cash=[1000, 900, 850, 800, 750],
        total_debt=[6000, 5800, 5600, 5400, 5200],
        cash=[1000, 900, 850, 800, 750],
        equity=[8000, 7600, 7300, 7000, 6800],
        diluted_shares=[1000, 1000, 1005, 1010, 1015],
        interest_expense=[200, 190, 180, 170, 160],
    )


@pytest.fixture
def macro_payload() -> dict:
    """CPI 관측월 2026-10-01, 공개 빈티지 2026-11-12, 12월 개정 빈티지 포함."""
    return {
        "observations": [
            {"date": "2026-10-01", "realtime_start": "2026-11-12", "realtime_end": "2026-12-09", "value": "320.100"},
            {"date": "2026-10-01", "realtime_start": "2026-12-10", "realtime_end": "9999-12-31", "value": "320.450"},
            {"date": "2026-09-01", "realtime_start": "2026-10-13", "realtime_end": "2026-11-11", "value": "319.800"},
            {"date": "2026-08-01", "realtime_start": "2026-09-11", "realtime_end": "9999-12-31", "value": "."},
        ]
    }


@pytest.fixture
def universe_df() -> pd.DataFrame:
    return pd.DataFrame([
        {"ticker": "AAA", "index_name": "SP500", "sector": "Technology",
         "effective_at": pd.Timestamp("2024-01-02"), "removed_at": pd.NaT},
        {"ticker": "BBB", "index_name": "SP500", "sector": "Health Care",
         "effective_at": pd.Timestamp("2024-01-02"), "removed_at": pd.Timestamp("2025-06-30")},
        {"ticker": "CCC", "index_name": "SP500", "sector": "Technology",
         "effective_at": pd.Timestamp("2025-07-01"), "removed_at": pd.NaT},
    ])


@pytest.fixture
def decision_at() -> datetime:
    return pd.Timestamp("2026-11-13T12:50:00Z").to_pydatetime()
