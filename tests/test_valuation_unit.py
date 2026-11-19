"""test_valuation_unit — 3.2.3 예시 검산 + 단위 강제(USD/share)."""

from __future__ import annotations

import pytest

from engine.metrics.valuation import (
    ValuationInputs,
    equity_per_share,
    graham_number,
    margin_of_safety,
    median_of,
    v_score,
    value_company,
)
from engine.models import PerShareValue, UnitMismatchError


from tests.helpers import example_inputs  # noqa: E402


def test_doc_example_numbers():
    r = value_company(example_inputs())
    assert r["unit"] == "USD/share"
    assert r["fcff_normalized"] == pytest.approx(3750.0)
    assert r["ev_dcf_base"] == pytest.approx(65836, abs=1.0)      # 65,836
    assert r["iv_dcf"] == pytest.approx(60.64, abs=0.01)          # $60.64
    assert r["ev_epv"] == pytest.approx(43888.9, abs=0.5)
    assert r["iv_epv"] == pytest.approx(38.69, abs=0.01)          # $38.69
    assert r["iv_graham"] == pytest.approx(37.35, abs=0.01)       # $37.35
    assert r["iv_median"] == pytest.approx(38.69, abs=0.01)
    assert margin_of_safety(r["iv_median"], 31.00) == pytest.approx(0.199, abs=0.001)   # 19.9%
    # IV_low/high 도 같은 주당 달러 단위
    assert r["iv_low"] < r["iv_median"] < r["iv_high"]
    assert all(v > 0 for v in r["methods"].values())


def test_ev_is_not_a_per_share_value():
    """EV(65,836)·시총·주식수를 그대로 중앙값에 넣으면 실패해야 한다."""
    with pytest.raises(UnitMismatchError):
        PerShareValue(65836.0, "dcf_ev", unit="USD")
    with pytest.raises(UnitMismatchError):
        median_of([PerShareValue(38.69, "epv"), 65836.0])  # type: ignore[list-item]
    ok = median_of([PerShareValue(38.69, "epv"), PerShareValue(37.35, "graham")])
    assert ok.unit == "USD/share"
    assert ok.value == pytest.approx(38.02)


def test_equity_per_share_subtracts_debt_and_minority():
    assert equity_per_share(65836, 5000, 200, 1000) == pytest.approx(60.636)
    with pytest.raises(ValueError):
        equity_per_share(100, 0, 0, 0)


def test_graham_requires_positive_eps_and_bps():
    assert graham_number(3.10, 20.00) == pytest.approx(37.3497, abs=1e-3)
    with pytest.raises(ValueError):
        graham_number(-1, 20)


def test_dead_method_is_dropped():
    """지분가치가 0 이하인 방법은 폐기되고 남은 방법으로 중앙값을 낸다."""
    inp = ValuationInputs(
        ebit=5000, tax_rate=0.21, depreciation=600, capex=700, delta_nwc=100,
        net_debt=200_000, minority_interest=0, diluted_shares=1000, wacc=0.09,
        growth=0.05, eps=3.10, bps=20.0,
    )
    r = value_company(inp)
    assert "dcf_base" not in r["methods"] and "epv" not in r["methods"]
    assert r["method_count"] == 1
    assert r["v_confidence"] == "C"


def test_v_score_mapping():
    assert v_score(0.0) == pytest.approx(50)
    assert v_score(0.30) == pytest.approx(100)
    assert v_score(-0.20) == pytest.approx(0)
    # 감점은 0~100으로 자른다
    assert v_score(0.30, penalties=20) == pytest.approx(80)
    assert v_score(0.0, penalties=99) == pytest.approx(0)
