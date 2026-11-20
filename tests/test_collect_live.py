"""test_collect_live — 라이브 수집 배선(가격 6년·메타·XBRL·스크린 편입)을 네트워크 없이 검증."""

from __future__ import annotations

import inspect
import sys
import types

import pandas as pd
import pytest

from engine.data.collect import EXCHANGE_CODES, collect_fundamentals, collect_meta, collect_prices
from engine.data.prices import build_price_frame
from engine.screener import screen
from scripts.run_screen import build_candidates
from tests.conftest import make_history
from tests.test_xbrl_fundamentals import _payload

DECISION = pd.Timestamp("2026-11-19T22:00:00Z").to_pydatetime()


def _panel(price: float = 100.0, days: int = 1300, seed: int = 5):
    hist, splits = make_history(days=days, price=price, seed=seed)
    return build_price_frame("AAA", hist, splits)


def test_collect_prices_defaults_to_six_years():
    assert inspect.signature(collect_prices).parameters["history_years"].default == 6


def test_exchange_codes_map_to_gate_names():
    assert EXCHANGE_CODES["NYQ"] == "NYSE"
    assert EXCHANGE_CODES["NMS"] == "NASDAQ"
    assert EXCHANGE_CODES["PCX"] == "NYSEARCA"


def test_collect_meta_tolerates_single_ticker_failure(monkeypatch):
    class _Fast(dict):
        pass

    class _Ticker:
        def __init__(self, ticker):
            self.ticker = ticker

        @property
        def fast_info(self):
            if self.ticker == "BOOM":
                raise RuntimeError("429 rate limit")
            return _Fast({"marketCap": 5e11, "exchange": "NMS", "shares": 1e9})

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=_Ticker))
    diag = {"sources": {}, "degraded": []}
    meta = collect_meta(["AAA", "BOOM"], diag, pause=0)
    assert meta["AAA"]["market_cap_usd"] == pytest.approx(5e11)
    assert meta["AAA"]["exchange"] == "NASDAQ"
    assert meta["BOOM"]["market_cap_usd"] is None      # 예외를 전파하지 않는다
    assert diag["sources"]["yfinance_meta"]["status"] == "ok"


def test_collect_fundamentals_fills_mult_and_completeness():
    class _FakeSec:
        def company_facts(self, cik):
            assert cik == 123
            return _payload()

    panel = _panel()
    diag = {"sources": {}, "degraded": []}
    fundamentals, valuation = collect_fundamentals(
        ["AAA"], DECISION, diag, {"AAA": panel}, panel, 0.042,
        {"AAA": "Information Technology"}, client=_FakeSec(), cik_by_ticker={"AAA": 123},
    )
    assert "AAA" in fundamentals
    v = valuation["AAA"]
    assert v["iv_mult"] is not None and 0 < v["iv_mult"] < 50
    assert v["xbrl_completeness"] == pytest.approx(1.0)
    assert v["revenue_cagr"] == pytest.approx(0.0, abs=1e-9)
    assert v["beta"] is not None
    assert diag["sources"]["sec_xbrl"]["status"] == "ok"


def test_collect_fundamentals_records_missing_sec_key(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    diag = {"sources": {}, "degraded": []}
    fundamentals, valuation = collect_fundamentals(["AAA"], DECISION, diag, {}, None, None, {}, None)
    assert fundamentals == {} and valuation == {}
    assert diag["sources"]["sec_xbrl"]["status"] == "missing_key"


def _dataset(rows):
    panel = _panel()
    return {
        "source": "live",
        "universe": rows,
        "panels": {"AAA": panel, "BBB": panel},
        "fundamentals": {},
        "valuation_inputs": {},
    }


def test_build_candidates_carries_live_fields_into_watchlist():
    row = {"ticker": "AAA", "sector": "Information Technology", "exchange": "NASDAQ",
           "market_cap_usd": 5e11, "adv20_usd": 5e9, "iv_mult": 1.4, "peg": 1.1,
           "revenue_cagr": 0.08, "xbrl_completeness": 1.0}
    bank = {"ticker": "BBB", "sector": "Financials", "exchange": "NYSE",
            "market_cap_usd": 4e11, "adv20_usd": 2e9, "iv_mult": None, "peg": None,
            "revenue_cagr": 0.05, "xbrl_completeness": 1.0}
    candidates = build_candidates(_dataset([row, bank]))
    assert candidates[0].margin is not None and candidates[0].margin > 0.2
    assert candidates[0].xbrl_completeness == pytest.approx(1.0)
    result = screen(candidates)
    assert [s["ticker"] for s in result["valuation_suspended"]] == ["BBB"]
    # 안전마진이 양수인 종목은 5년 이력·시총·거래대금 게이트를 통과해 워치리스트에 남는다
    assert [w["ticker"] for w in result["watchlist"]] == ["AAA"]


def test_missing_iv_mult_excludes_with_reason():
    row = {"ticker": "AAA", "sector": "Information Technology", "exchange": "NASDAQ",
           "market_cap_usd": 5e11, "adv20_usd": 5e9, "iv_mult": None, "peg": 1.0,
           "revenue_cagr": 0.05, "xbrl_completeness": 1.0}
    result = screen(build_candidates(_dataset([row])))
    assert result["watchlist"] == []
    assert result["excluded"][0]["gates"] == ["V_UNAVAILABLE"]


def test_short_history_is_excluded_by_hg04():
    short = _panel(days=700)
    row = {"ticker": "AAA", "sector": "Information Technology", "exchange": "NASDAQ",
           "market_cap_usd": 5e11, "adv20_usd": 5e9, "iv_mult": 1.4, "peg": 1.0,
           "revenue_cagr": 0.05, "xbrl_completeness": 1.0}
    data = _dataset([row])
    data["panels"] = {"AAA": short}
    result = screen(build_candidates(data))
    assert result["watchlist"] == []
    assert "HG-04" in result["excluded"][0]["gates"]
