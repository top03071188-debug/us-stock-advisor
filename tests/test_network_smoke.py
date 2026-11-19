"""네트워크 스모크 테스트(기본 실행에서는 skip).

    RUN_NETWORK=1 python -m pytest tests/test_network_smoke.py -q

키는 `.env`(gitignore) 또는 환경변수에서만 읽는다. 네트워크가 없는 CI 에서는
`-m "not network"` 로 제외되므로 단위 테스트는 항상 100% 재현된다.
"""

from __future__ import annotations

import os

import pytest

from engine.utils.io import load_dotenv

pytestmark = pytest.mark.network

RUN = os.environ.get("RUN_NETWORK") == "1"


@pytest.mark.skipif(not RUN, reason="RUN_NETWORK=1 일 때만 실행")
def test_yfinance_three_price_kinds():
    import yfinance as yf

    from engine.data.prices import build_price_frame
    from engine.metrics.technical import atr_pct
    from engine.models import PriceKind

    hist = yf.Ticker("AAPL").history(period="5y", auto_adjust=False, actions=True)
    panel = build_price_frame("AAPL", hist, yf.Ticker("AAPL").splits)
    assert len(panel.frame) > 1000
    assert atr_pct(panel.series(PriceKind.split_adj_ohlc)).iloc[-1] > 0
    # 2020 4:1 분할이 5년 창 밖이면 split_factor 는 1 — 최근 구간에서 raw == sadj
    assert abs(float(panel.frame["raw_close"].iloc[-1]) - float(panel.frame["sadj_close"].iloc[-1])) < 1e-6


@pytest.mark.skipif(not RUN, reason="RUN_NETWORK=1 일 때만 실행")
def test_sec_company_tickers():
    from engine.data.sec_edgar import SecClient

    load_dotenv()
    client = SecClient()
    df = client.company_tickers()
    assert len(df) > 5000
    assert (df["ticker"] == "AAPL").any()


@pytest.mark.skipif(not RUN, reason="RUN_NETWORK=1 일 때만 실행")
def test_fred_hy_oas_range():
    from engine.data.macro import FredClient, series_limits

    load_dotenv()
    df = FredClient().observations("BAMLH0A0HYM2", observation_start="2018-01-01")
    lim = series_limits(df)
    assert lim["start"] == "2023-11-20"      # 기획서 2.4.3 의 확인값
    assert lim["end"] >= "2026-01-01"


@pytest.mark.skipif(not RUN, reason="RUN_NETWORK=1 일 때만 실행")
def test_finnhub_quote():
    from engine.data.news import FinnhubClient

    load_dotenv()
    q = FinnhubClient().quote("MSFT")
    assert q.get("c", 0) > 0
