"""engine.data.fixtures — 오프라인 파이프라인 검증용 **합성** 데이터셋.

[고정] 이 모듈은 네트워크 없이 `run_daily.py --offline` 경로 전체(수집 → 지표 → 신호 →
Ledger → 스냅샷 → 스키마 검증)를 재현하기 위한 고정 시드 합성 데이터다.
**실데이터가 아니며 실전 신호가 아니다.** 그래서 산출물 상태는 항상 `sample` 로 표기되고
(대시보드가 "샘플" 배지를 붙인다), 실 API 키가 없을 때의 **폴백 경로**로도 쓰인다.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd

from engine.data.prices import PricePanel, build_price_frame
from engine.metrics.fundamentals import AnnualFundamentals
from engine.metrics.valuation import ValuationInputs
from engine.utils.dates import prev_trading_day

#: 합성 유니버스 — `pattern` 이 가격 모양을, `earnings_in` 이 V01 실적 창을 만든다.
UNIVERSE = (
    {"ticker": "FIXA", "sector": "Information Technology", "pattern": "dip",
     "market_cap_usd": 2.4e12, "adv20_usd": 9.0e9, "iv_mult": 1.42, "q": 76.0, "g": 58.0,
     "earnings_in": 14, "news_state": "read_done"},
    {"ticker": "FIXB", "sector": "Health Care", "pattern": "flat",
     "market_cap_usd": 3.1e11, "adv20_usd": 1.4e9, "iv_mult": 1.18, "q": 72.0, "g": 44.0,
     "earnings_in": 25, "news_state": "not_read"},
    {"ticker": "FIXC", "sector": "Consumer Discretionary", "pattern": "dip",
     "market_cap_usd": 1.8e11, "adv20_usd": 2.2e9, "iv_mult": 1.55, "q": 69.0, "g": 51.0,
     "earnings_in": 1, "news_state": "read_done"},
    {"ticker": "FIXD", "sector": "Financials", "pattern": "flat",
     "market_cap_usd": 4.2e11, "adv20_usd": 1.1e9, "iv_mult": None, "q": 66.0, "g": 40.0,
     "earnings_in": 40, "news_state": "not_read"},
    {"ticker": "FIXE", "sector": "Energy", "pattern": "down",
     "market_cap_usd": 9.0e10, "adv20_usd": 6.0e8, "iv_mult": 1.30, "q": 61.0, "g": 33.0,
     "earnings_in": 33, "news_state": "not_read"},
    {"ticker": "FIXF", "sector": "Industrials", "pattern": "up",
     "market_cap_usd": 1.5e11, "adv20_usd": 7.5e8, "iv_mult": 1.10, "q": 70.0, "g": 47.0,
     "earnings_in": 19, "news_state": "insufficient_evidence"},
)
EXTRA_TICKERS = ("FIXG", "FIXH")


def _fundamentals(scale: float = 1.0) -> AnnualFundamentals:
    def s(xs: list[float]) -> list[float]:
        return [x * scale for x in xs]
    return AnnualFundamentals(
        ebit=s([5000, 4900, 4800, 4700, 4600]),
        revenue=s([25000, 24000, 23000, 22000, 21100]),
        net_income=s([3100, 3000, 2900, 2800, 2700]),
        ocf=s([3800, 3700, 3600, 3500, 3400]),
        capex=s([700, 690, 680, 670, 660]),
        depreciation=s([600, 590, 580, 570, 560]),
        total_assets=s([20000, 19500, 19000, 18500, 18000]),
        non_interest_liabilities=s([4000, 3900, 3800, 3700, 3600]),
        excess_cash=s([1000, 950, 900, 850, 800]),
        total_debt=s([3000, 3000, 3100, 3100, 3200]),
        cash=s([1200, 1150, 1100, 1050, 1000]),
        equity=s([9000, 8800, 8600, 8400, 8200]),
        diluted_shares=s([1000, 1000, 1005, 1010, 1015]),
        interest_expense=s([150, 150, 155, 160, 165]),
    )


def _valuation_inputs() -> ValuationInputs:
    return ValuationInputs(
        ebit=5000, tax_rate=0.21, depreciation=600, capex=700, delta_nwc=100,
        net_debt=1800, minority_interest=200, diluted_shares=1000, wacc=0.09,
        terminal_growth=0.025, growth=0.06, eps=3.10, bps=20.00,
    )


def _seed(ticker: str) -> int:
    return abs(hash(ticker)) % 10_000 if not ticker else sum(ord(c) * (i + 3) for i, c in enumerate(ticker)) % 9973


def synthetic_history(ticker: str, days: int = 1300, start: str = "2024-11-01",
                      end: date | None = None, pattern: str = "flat") -> pd.DataFrame:
    """패턴별 합성 OHLC — Yahoo 형태(분할 조정된) 로 만든다. 결정론(고정 시드).

    - `up`   : 완만한 상승
    - `down` : 완만한 하락(200일선 아래)
    - `dip`  : 장기 상승 뒤 12% 눌림(RSI 하락·50일선 이탈 → B4 조건)
    - `flat` : 횡보
    """
    if end is not None:
        idx = pd.bdate_range(end=pd.Timestamp(end), periods=days)
    else:
        idx = pd.bdate_range(start=start, periods=days)
    rng = np.random.default_rng(_seed(ticker))
    n = len(idx)
    if pattern == "dip":
        up = np.linspace(0.0, 0.85, n - 20)
        pull = np.linspace(0.85, 0.74, 20)
        trend = np.concatenate([up, pull])
    elif pattern == "up":
        trend = np.linspace(0.0, 0.45, n)
    elif pattern == "down":
        trend = np.linspace(0.0, -0.30, n)
    else:
        trend = np.linspace(0.0, 0.04, n)
    noise = rng.normal(0, 0.0045, n)
    close = pd.Series(100.0 * (1.0 + trend + noise), index=idx)
    high = close * 1.006
    low = close * 0.994
    open_ = close.shift(1).fillna(close.iloc[0]) * (1.0 + rng.normal(0, 0.0012, n))
    return pd.DataFrame({
        "Open": open_, "High": np.maximum(high, open_), "Low": np.minimum(low, open_),
        "Close": close, "Adj Close": close * 0.985,
        "Volume": rng.integers(2_000_000, 9_000_000, n).astype(float),
    }, index=idx)


def panels(as_of: date, days: int = 1300) -> dict[str, PricePanel]:
    end = prev_trading_day(as_of)
    out: dict[str, PricePanel] = {}
    for row in UNIVERSE:
        hist = synthetic_history(row["ticker"], days=days, end=end, pattern=row["pattern"])
        hist = hist.loc[hist.index <= pd.Timestamp(end)].copy()
        out[row["ticker"]] = build_price_frame(row["ticker"], hist, None)
    return out


def macro_values() -> dict:
    return {"dff": 3.85, "cpi_yoy": 2.9, "t10y2y": 0.42, "hy_oas_bp": 355.0, "vix": 18.4, "dtwexbg": 121.6}


def macro_previous() -> dict:
    return {"dff": 4.10, "cpi_yoy": 3.05, "t10y2y": 0.30, "hy_oas_bp": 372.0, "vix": 20.1, "dtwexbg": 120.4}


def filings(as_of: date) -> dict[str, list[dict]]:
    """10-Q·8-K 제출(접수 시각 포함) — `acceptance_datetime ≤ decision_at` 을 만족해야 쓰인다."""
    out: dict[str, list[dict]] = {}
    for row in UNIVERSE:
        ticker = row["ticker"]
        filed = datetime.combine(as_of - timedelta(days=12), time(20, 5), tzinfo=timezone.utc)
        out[ticker] = [
            {"form": "10-Q", "filed_date": (as_of - timedelta(days=12)).isoformat(),
             "acceptance_datetime": filed.isoformat().replace("+00:00", "Z"),
             "items": [],
             "url": f"https://www.sec.gov/Archives/edgar/data/{ticker}/10q.htm", "accession": f"0000{ticker}-26-0001"},
            {"form": "8-K", "filed_date": (as_of - timedelta(days=30)).isoformat(),
             "acceptance_datetime": (filed - timedelta(days=18)).isoformat().replace("+00:00", "Z"),
             "items": ["2.02"], "url": f"https://www.sec.gov/Archives/edgar/data/{ticker}/8k.htm",
             "accession": f"0000{ticker}-26-0002"},
        ]
    return out


def news(as_of: date) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in UNIVERSE:
        published = datetime.combine(as_of - timedelta(days=1), time(21, 40), tzinfo=timezone.utc)
        out[row["ticker"]] = [{
            "headline": f"{row['ticker']} 합성 뉴스 헤드라인(픽스처) — 실제 기사가 아니다",
            "source": "FixtureWire",
            "published_at": published.isoformat().replace("+00:00", "Z"),
            "url": f"https://example.com/news/{row['ticker'].lower()}",
            "durability": "structural",
            "impact": "neutral",
        }]
    return out


def universe_rows() -> list[dict]:
    rows = []
    for row in UNIVERSE:
        rows.append({**row, "exchange": "NYSE", "history_rows": 1300,
                     "days_to_earnings": row.get("earnings_in")})
    return rows


def dataset(as_of: date) -> dict:
    return {
        "as_of": as_of.isoformat(),
        "universe": universe_rows(),
        "panels": panels(as_of),
        "macro": macro_values(),
        "macro_previous": macro_previous(),
        "filings": filings(as_of),
        "news": news(as_of),
        "fundamentals": {row["ticker"]: _fundamentals(1.0) for row in UNIVERSE},
        "valuation_inputs": {row["ticker"]: _valuation_inputs() for row in UNIVERSE},
        "source": "fixtures",
    }
