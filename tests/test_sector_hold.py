"""test_sector_hold — 은행·보험·REIT 는 가치평가 보류(신호·워치리스트 제외)."""

from __future__ import annotations

import pytest

from engine.metrics.valuation import SUSPEND_REASON, classify_sector, value_company
from engine.screener import Candidate, screen


def test_classify_sector_variants():
    for s in ("Financials", "Banks", "insurance", "REITs", "Real Estate", "은행"):
        info = classify_sector(s)
        assert info["valuation_suspended"] is True, s
        assert info["reason"] == SUSPEND_REASON
    for s in ("Technology", "Health Care", "Industrials", None):
        assert classify_sector(s)["valuation_suspended"] is False


def test_value_company_returns_suspended_not_a_number(example_inputs=None):
    from tests.helpers import example_inputs as make_inputs

    r = value_company(make_inputs(), sector="Financials")
    assert r["valuation_suspended"] is True
    assert r["reason"] == "SECTOR_FORMULA_PENDING"
    assert r["iv_median"] is None and r["margin"] is None


def test_screener_excludes_suspended_sector():
    from tests.helpers import annual_fundamentals as make_fundamentals

    bank = Candidate(
        ticker="BANKX", sector="Banks", market_cap_usd=5e10, adv20_usd=2e8, price=100,
        history_rows=2000, fundamentals=make_fundamentals(), margin=0.35,
    )
    tech = Candidate(
        ticker="TECHX", sector="Technology", market_cap_usd=5e10, adv20_usd=2e8, price=100,
        history_rows=2000, fundamentals=make_fundamentals(), margin=0.35, peg=1.1, revenue_cagr=0.08,
    )
    result = screen([bank, tech])
    tickers = [w["ticker"] for w in result["watchlist"]]
    assert tickers == ["TECHX"]
    assert result["valuation_suspended"][0]["ticker"] == "BANKX"
    assert result["valuation_suspended"][0]["reason"] == SUSPEND_REASON


def test_suspended_sector_never_produces_signal_even_if_best():
    from tests.helpers import annual_fundamentals as make_fundamentals

    reit = Candidate(
        ticker="REITX", sector="REITs", market_cap_usd=9e10, adv20_usd=9e8, price=50,
        history_rows=3000, fundamentals=make_fundamentals(), margin=0.90, peg=0.5, revenue_cagr=0.2,
    )
    result = screen([reit])
    assert result["watchlist"] == []
    assert result["count"] == 0
    assert len(result["valuation_suspended"]) == 1
